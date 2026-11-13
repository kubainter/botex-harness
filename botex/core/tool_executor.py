# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Tool Executor Abstraction
===============================
Decouples the autonomous engine loop from direct local filesystem/process
calls so local runs (``LocalToolExecutor``) and future remote edge bridges
share one explicit async execution & read-batching contract.
"""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

try:
    from botex.core.context_guard import read_scratchpad
    from botex.tools.exec_tools import run_command
    from botex.tools.file_tools import (
        create_file,
        delete_file,
        get_file_outline,
        list_dir,
        move_file,
        read_file,
        read_file_lines,
    )
    from botex.tools.net_tools import read_url
    from botex.tools.patch_engine import apply_patch
except (ImportError, ValueError):
    from context_guard import read_scratchpad
    from exec_tools import run_command
    from file_tools import (
        create_file,
        delete_file,
        get_file_outline,
        list_dir,
        move_file,
        read_file,
        read_file_lines,
    )
    from net_tools import read_url
    from patch_engine import apply_patch

READ_ONLY_TOOLS = frozenset(
    {"get_file_outline", "read_file_lines", "read_file", "list_dir", "read_url"}
)


def _accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    """Return True if *fn* accepts *name* explicitly or via ``**kwargs``."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == name:
            return True
    return False


_EDGE_EXECUTOR_RESOLVER: Optional[Callable[[str], Optional[ToolExecutor]]] = None


def register_edge_executor_resolver(
    resolver: Optional[Callable[[str], Optional[ToolExecutor]]],
) -> None:
    """Register (or clear) the cloud edge-bridge resolver for logical edge workspaces."""
    global _EDGE_EXECUTOR_RESOLVER
    _EDGE_EXECUTOR_RESOLVER = resolver


def resolve_edge_executor(workspace_id: str) -> Optional[ToolExecutor]:
    """Return an active remote ``ToolExecutor`` for ``workspace_id`` if one is registered."""
    fn = _EDGE_EXECUTOR_RESOLVER
    if fn is None or not str(workspace_id or "").strip():
        return None
    try:
        return fn(str(workspace_id).strip())
    except Exception:
        return None


class ToolExecutor(ABC):
    """Abstract interface for executing workspace tools on behalf of the engine."""

    @abstractmethod
    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        workspace_root: Path,
        task_id: str = "default",
        call_id: str = "",
    ) -> Dict[str, Any]:
        """Execute a single tool call and return a structured result dict."""

    async def execute_batch(
        self,
        calls: Sequence[Dict[str, Any]],
        workspace_root: Path,
        task_id: str = "default",
    ) -> List[Dict[str, Any]]:
        """Execute a batch of calls concurrently when all belong to ``READ_ONLY_TOOLS``.

        Mutating or mixed batches run strictly sequentially in order.
        """
        call_list = list(calls)
        if not call_list:
            return []
        if len(call_list) > 1 and all(
            str(c.get("tool_name") or "") in READ_ONLY_TOOLS for c in call_list
        ):
            coros = [
                self.execute(
                    str(c.get("tool_name") or ""),
                    dict(c.get("args") or {}),
                    workspace_root,
                    task_id=task_id,
                    call_id=str(c.get("call_id") or ""),
                )
                for c in call_list
            ]
            return list(await asyncio.gather(*coros))

        results: List[Dict[str, Any]] = []
        for c in call_list:
            results.append(
                await self.execute(
                    str(c.get("tool_name") or ""),
                    dict(c.get("args") or {}),
                    workspace_root,
                    task_id=task_id,
                    call_id=str(c.get("call_id") or ""),
                )
            )
        return results


class LocalToolExecutor(ToolExecutor):
    """Default local implementation delegating to BoteX filesystem/patch/exec tools."""

    def __init__(
        self,
        *,
        net_policy: str = "off",
        net_allowed_hosts: Optional[List[str]] = None,
        net_allowed_urls: Optional[List[str]] = None,
        net_timeout_s: int = 20,
        net_max_bytes: int = 200000,
        net_max_redirects: int = 3,
        exec_allowlist_fn: Optional[Callable[[], Dict[str, Any]]] = None,
        handlers: Optional[Dict[str, Callable[..., Any]]] = None,
    ) -> None:
        self.net_policy = net_policy
        self.net_allowed_hosts = list(net_allowed_hosts or [])
        self.net_allowed_urls = list(net_allowed_urls or [])
        self.net_timeout_s = int(net_timeout_s)
        self.net_max_bytes = int(net_max_bytes)
        self.net_max_redirects = int(net_max_redirects)
        self._exec_cfg_fn = exec_allowlist_fn
        self._handlers: Dict[str, Callable[..., Any]] = {
            "get_file_outline": get_file_outline,
            "read_file_lines": read_file_lines,
            "read_file": read_file,
            "read_scratchpad": read_scratchpad,
            "apply_patch": apply_patch,
            "create_file": create_file,
            "delete_file": delete_file,
            "move_file": move_file,
            "list_dir": list_dir,
            "read_url": read_url,
            "run_command": run_command,
        }
        if handlers:
            self._handlers.update(handlers)

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        workspace_root: Path,
        task_id: str = "default",
        call_id: str = "",
    ) -> Dict[str, Any]:
        del call_id  # Reserved for remote idempotency bridges
        fn = self._handlers.get(tool_name)
        if fn is None:
            return {"ok": False, "error": f"Unknown tool '{tool_name}'."}

        if tool_name == "get_file_outline":
            return fn(args.get("path", ""), workspace_root)
        if tool_name == "read_file_lines":
            return fn(
                args.get("path", ""),
                int(args.get("start", 1)),
                int(args.get("end", 50)),
                workspace_root,
            )
        if tool_name == "read_file":
            return fn(args.get("path", ""), workspace_root)
        if tool_name == "read_scratchpad":
            return fn(args.get("memory_id", ""), cwd=workspace_root)
        if tool_name == "list_dir":
            return fn(args.get("path", "."), workspace_root)
        if tool_name == "apply_patch":
            base_sha = args.get("base_sha256")
            patch_kw: Dict[str, Any] = {"task_id": task_id}
            if base_sha is not None and _accepts_kwarg(fn, "base_sha256"):
                patch_kw["base_sha256"] = base_sha
            return fn(
                args.get("path", ""),
                args.get("search_block", ""),
                args.get("replace_block", ""),
                workspace_root,
                **patch_kw,
            )
        if tool_name == "create_file":
            return fn(
                args.get("path", ""),
                args.get("content", ""),
                workspace_root,
                task_id=task_id,
            )
        if tool_name == "delete_file":
            return fn(args.get("path", ""), workspace_root, task_id=task_id)
        if tool_name == "move_file":
            return fn(
                args.get("src_path", ""),
                args.get("dst_path", ""),
                workspace_root,
                task_id=task_id,
            )
        if tool_name == "read_url":
            requested_max = int(args.get("max_bytes", self.net_max_bytes))
            return await fn(
                args.get("url", ""),
                policy=self.net_policy,
                allowed_hosts=self.net_allowed_hosts,
                allowed_urls=self.net_allowed_urls,
                timeout_s=self.net_timeout_s,
                max_bytes=max(1, min(requested_max, self.net_max_bytes)),
                max_redirects=self.net_max_redirects,
            )
        if tool_name == "run_command":
            exec_cfg = self._exec_cfg_fn() if callable(self._exec_cfg_fn) else {}
            return await fn(
                args.get("command", ""),
                workspace_root,
                allowlist=exec_cfg.get("allowlist", []),
                deny_args=exec_cfg.get("deny_args", []),
                timeout_s=int(exec_cfg.get("timeout_s", 120)),
                max_output_bytes=int(exec_cfg.get("max_output_bytes", 20000)),
            )
        return {"ok": False, "error": f"Unsupported tool '{tool_name}'."}
