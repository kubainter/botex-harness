# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Human-in-the-Loop Approval & Action Registry
===================================================
Asynchronous, blocking approval manager for risky agent actions (OWASP LLM03).
Supports indefinite blocking via asyncio.Future, per-session pre-approvals,
rich diff/command previews, multi-channel callbacks (CLI, WebSocket, REST),
and task cancellation (Abort Task).
"""

import asyncio
import inspect
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

try:
    from .security import mask_secrets
except (ImportError, ValueError):
    from security import mask_secrets


class ActionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ABORTED = "aborted"


class TaskAbortedError(Exception):
    """Raised when an action or task is aborted by the operator."""

    pass


class ApprovalPolicy(str, Enum):
    NONE = "none"  # Auto-allow all permitted by capability mode
    HIGH_RISK = "high_risk"  # delete_file, move_file, run_command
    MUTATIONS = "mutations"  # create_file, apply_patch, delete_file, move_file, run_command
    ALL_RISKY = "all_risky"  # mutations + run_command + read_url


# Mapping of policy levels to tool sets requiring approval
POLICY_TOOL_MAP: Dict[ApprovalPolicy, Set[str]] = {
    ApprovalPolicy.NONE: set(),
    ApprovalPolicy.HIGH_RISK: {"delete_file", "move_file", "run_command"},
    ApprovalPolicy.MUTATIONS: {
        "create_file",
        "apply_patch",
        "delete_file",
        "move_file",
        "run_command",
    },
    ApprovalPolicy.ALL_RISKY: {
        "create_file",
        "apply_patch",
        "delete_file",
        "move_file",
        "run_command",
        "read_url",
    },
}


@dataclass
class PendingAction:
    """An action awaiting operator confirmation."""

    action_id: str
    task_id: str
    tool_name: str
    params: Dict[str, Any]
    preview: str
    created_at: float = field(default_factory=time.time)
    status: ActionStatus = ActionStatus.PENDING
    decision_reason: str = ""
    resolved_at: Optional[float] = None
    _future: Optional[asyncio.Future] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary with masked secrets."""
        return {
            "action_id": self.action_id,
            "task_id": self.task_id,
            "tool_name": self.tool_name,
            "params": self.params,
            "preview": mask_secrets(self.preview),
            "created_at": self.created_at,
            "status": self.status.value,
            "decision_reason": mask_secrets(self.decision_reason),
            "resolved_at": self.resolved_at,
        }


def format_action_preview(tool_name: str, params: Dict[str, Any]) -> str:
    """Generate a clean, informative human-readable preview of what the action will do."""
    if tool_name == "apply_patch":
        path = params.get("path", "")
        search_block = params.get("search_block", "")
        replace_block = params.get("replace_block", "")
        diff_lines = [f"File: {path}"]
        diff_lines.append("--- Search Block ---")
        for line in search_block.splitlines():
            diff_lines.append(f"- {line}")
        diff_lines.append("+++ Replace Block +++")
        for line in replace_block.splitlines():
            diff_lines.append(f"+ {line}")
        return "\n".join(diff_lines)

    elif tool_name == "create_file":
        path = params.get("path", "")
        content = params.get("content", "")
        lines = content.splitlines()
        preview_body = "\n".join(lines[:10])
        if len(lines) > 10:
            preview_body += f"\n... ({len(lines) - 10} more lines)"
        return f"Create File: {path} ({len(lines)} lines, {len(content)} bytes)\n{preview_body}"

    elif tool_name == "delete_file":
        path = params.get("path", "")
        return f"DELETE File: {path}"

    elif tool_name == "move_file":
        src = params.get("src_path", "")
        dst = params.get("dst_path", "")
        return f"MOVE File: {src} -> {dst}"

    elif tool_name == "run_command":
        cmd = params.get("command", "")
        return f"EXEC Command: {cmd}"

    elif tool_name == "read_url":
        url = params.get("url", "")
        return f"FETCH URL: {url}"

    # Default fallback
    return f"Tool: {tool_name} with params: {params}"


class ApprovalManager:
    """
    Coordinates action requests, blocking futures, and approval state.
    Thread-safe and async-safe.
    """

    def __init__(self, default_policy: ApprovalPolicy = ApprovalPolicy.HIGH_RISK):
        self.default_policy = default_policy
        self._actions: Dict[str, PendingAction] = {}
        self._session_preapprovals: Dict[str, Set[str]] = {}  # task_id -> set of tool_names
        self._listeners: List[Callable[[PendingAction], Any]] = []
        self._lock = threading.RLock()

    def set_policy(self, policy: ApprovalPolicy) -> None:
        """Set the global default approval policy."""
        with self._lock:
            self.default_policy = policy

    def add_listener(self, listener: Callable[[PendingAction], Any]) -> None:
        """Add an event listener notified whenever a new action request is registered."""
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[PendingAction], Any]) -> None:
        """Remove a previously added event listener."""
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def is_action_requiring_approval(
        self,
        tool_name: str,
        policy: Optional[ApprovalPolicy] = None,
        task_id: str = "",
    ) -> bool:
        """Determine whether the given tool call needs operator approval."""
        with self._lock:
            # Check task-scoped preapprovals (e.g. 'always allow write_file for this task')
            if task_id and task_id in self._session_preapprovals:
                if tool_name in self._session_preapprovals[task_id]:
                    return False

            effective_policy = policy or self.default_policy
            tools_requiring = POLICY_TOOL_MAP.get(effective_policy, set())
            return tool_name in tools_requiring

    def preapprove_tool(self, task_id: str, tool_name: str) -> None:
        """Allow a specific tool to execute without prompting for the rest of this task."""
        with self._lock:
            if task_id not in self._session_preapprovals:
                self._session_preapprovals[task_id] = set()
            self._session_preapprovals[task_id].add(tool_name)

    async def request_approval(
        self,
        task_id: str,
        tool_name: str,
        params: Dict[str, Any],
        preview: str = "",
        policy: Optional[ApprovalPolicy] = None,
        custom_confirm_fn: Optional[Callable[..., Any]] = None,
        timeout: Optional[float] = None,
    ) -> bool:
        """
        Request approval for an action.
        If approval is not required by policy, returns True immediately.
        If a custom synchronous or asynchronous confirm_fn is provided, it is evaluated first.
        Otherwise, registers a PendingAction with an asyncio.Future and blocks until resolved.
        """
        if not self.is_action_requiring_approval(tool_name, policy=policy, task_id=task_id):
            return True

        if not preview:
            preview = format_action_preview(tool_name, params)

        # If a custom confirm_fn was supplied directly (e.g. from CLI or legacy caller)
        if custom_confirm_fn:
            op_target = (
                params.get("path")
                or params.get("src_path")
                or params.get("command")
                or params.get("url")
                or ""
            )
            try:
                if inspect.iscoroutinefunction(custom_confirm_fn):
                    return bool(await custom_confirm_fn(tool_name, op_target, params))
                sig = inspect.signature(custom_confirm_fn)
                if len(sig.parameters) >= 4:
                    res = custom_confirm_fn(tool_name, op_target, params, task_id)
                elif len(sig.parameters) == 3:
                    res = custom_confirm_fn(tool_name, op_target, params)
                elif len(sig.parameters) == 2:
                    res = custom_confirm_fn(tool_name, op_target)
                else:
                    res = custom_confirm_fn(tool_name)
                if inspect.isawaitable(res):
                    return bool(await res)
                return bool(res)
            except TaskAbortedError:
                raise
            except Exception:
                return False

        # Async Future-based registration for remote/PWA or decoupling
        action_id = str(uuid.uuid4())[:8]
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        action = PendingAction(
            action_id=action_id,
            task_id=task_id,
            tool_name=tool_name,
            params=params,
            preview=preview,
            _future=future,
        )

        with self._lock:
            self._actions[action_id] = action
            listeners_snapshot = list(self._listeners)

        # Notify registered listeners (e.g. WebSocket broadcaster)
        for listener in listeners_snapshot:
            try:
                res = listener(action)
                if inspect.isawaitable(res):
                    asyncio.create_task(res)
            except Exception:
                pass

        try:
            if timeout and timeout > 0:
                approved = await asyncio.wait_for(future, timeout=timeout)
            else:
                approved = await future
            return bool(approved)
        except asyncio.TimeoutError:
            action.status = ActionStatus.REJECTED
            action.decision_reason = f"Approval timed out after {timeout}s"
            action.resolved_at = time.time()
            return False
        except TaskAbortedError:
            action.status = ActionStatus.ABORTED
            action.decision_reason = "Task aborted by operator"
            action.resolved_at = time.time()
            raise
        except asyncio.CancelledError:
            action.status = ActionStatus.ABORTED
            action.decision_reason = "Task cancelled"
            action.resolved_at = time.time()
            raise TaskAbortedError("Task cancelled")

    def resolve(
        self,
        action_id: str,
        approved: bool,
        reason: str = "",
        remember: bool = False,
    ) -> bool:
        """
        Resolve a pending action.
        Returns True if the action was successfully found and resolved.
        """
        with self._lock:
            action = self._actions.get(action_id)
            if not action or action.status != ActionStatus.PENDING:
                return False

            action.status = ActionStatus.APPROVED if approved else ActionStatus.REJECTED
            action.decision_reason = reason
            action.resolved_at = time.time()

            if remember and approved and action.task_id:
                self.preapprove_tool(action.task_id, action.tool_name)

            if action._future and not action._future.done():
                try:
                    f_loop = action._future.get_loop()
                    if f_loop.is_running():
                        f_loop.call_soon_threadsafe(action._future.set_result, approved)
                    else:
                        action._future.set_result(approved)
                except Exception:
                    action._future.set_result(approved)

            return True

    def abort_task(self, task_id: str, reason: str = "Task aborted by operator") -> int:
        """
        Cancel all pending actions for a task with ABORTED status.
        Returns the number of aborted actions.
        """
        with self._lock:
            count = 0
            for action in self._actions.values():
                if action.task_id == task_id and action.status == ActionStatus.PENDING:
                    action.status = ActionStatus.ABORTED
                    action.decision_reason = reason
                    action.resolved_at = time.time()
                    if action._future and not action._future.done():
                        exc = TaskAbortedError(reason)
                        try:
                            f_loop = action._future.get_loop()
                            if f_loop.is_running():
                                f_loop.call_soon_threadsafe(action._future.set_exception, exc)
                            else:
                                action._future.set_exception(exc)
                        except Exception:
                            action._future.set_exception(exc)
                    count += 1
            return count

    def get_pending_actions(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return all currently pending actions (optionally filtered by task_id)."""
        with self._lock:
            return [
                a.to_dict()
                for a in self._actions.values()
                if a.status == ActionStatus.PENDING and (task_id is None or a.task_id == task_id)
            ]

    def get_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        """Get details of an action by its ID."""
        with self._lock:
            action = self._actions.get(action_id)
            return action.to_dict() if action else None

    def get_actions_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        """Return all actions recorded for a given task (pending, resolved, or aborted)."""
        with self._lock:
            return [a.to_dict() for a in self._actions.values() if a.task_id == task_id]

    def clear_task(self, task_id: str) -> None:
        """Clean up completed or aborted actions and pre-approvals for a finished task."""
        with self._lock:
            self._session_preapprovals.pop(task_id, None)
            to_del = [aid for aid, a in self._actions.items() if a.task_id == task_id]
            for aid in to_del:
                self._actions.pop(aid, None)


# Global default instance
default_approval_manager = ApprovalManager()
