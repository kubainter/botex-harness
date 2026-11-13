# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX In-Flight Task State & Checkpointing
=========================================
Captures runtime progress (plan, observations, inspected files, failure logs)
during task execution so that when a model or provider fails (e.g. 429 rate limit,
timeout, stagnation), this state can be carried forward across fallbacks
without losing reasoning progress, even if dirty workspace files are rolled back.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from botex.tools.security import coerce_text, mask_secrets, sanitize_identifier
except (ImportError, ValueError):
    from security import coerce_text, mask_secrets, sanitize_identifier

_MAX_SYNTAX_REJECTIONS = 5
_MAX_ROLLBACK_REASONS = 5
_MAX_REJECTION_FIELD_CHARS = 200


@dataclass
class CanonicalAgentTurn:
    """Canonical, provider-agnostic representation of a single agent turn."""

    agent_type: str = ""
    role: str = "assistant"
    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    status: Optional[str] = None
    mutated_files: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the turn to a plain dictionary."""
        return {
            "agent_type": self.agent_type,
            "role": self.role,
            "content": self.content,
            "tool_calls": [dict(c) for c in self.tool_calls],
            "status": self.status,
            "mutated_files": list(self.mutated_files),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CanonicalAgentTurn:
        """Reconstruct a turn from a dictionary."""
        return cls(
            agent_type=str(data.get("agent_type", "")),
            role=str(data.get("role", "assistant")),
            content=str(data.get("content", "")),
            tool_calls=list(data.get("tool_calls") or []),
            status=data.get("status"),
            mutated_files=list(data.get("mutated_files") or []),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class InFlightTaskState:
    """Snapshot of execution progress preserved across model/provider fallbacks."""

    task_id: str = ""
    goal: str = ""
    plan_steps: List[str] = field(default_factory=list)
    observations: List[str] = field(default_factory=list)
    inspected_files: List[str] = field(default_factory=list)
    failed_attempts: List[Dict[str, Any]] = field(default_factory=list)
    agent_turns: List[CanonicalAgentTurn] = field(default_factory=list)
    mutated_files: List[str] = field(default_factory=list)
    shared_memory_keys: List[str] = field(default_factory=list)
    active_blocking_reason: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    task_plugins: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    step_snapshots: Dict[int, str] = field(default_factory=dict)
    staged_plugins: List[Dict[str, Any]] = field(default_factory=list)
    syntax_rejections: List[Dict[str, str]] = field(default_factory=list)
    rollback_reasons: List[str] = field(default_factory=list)

    def add_plan_step(self, step: str) -> None:
        """Add a planned action step if not already recorded."""
        cleaned = step.strip()
        if cleaned and cleaned not in self.plan_steps:
            self.plan_steps.append(cleaned)

    def add_observation(self, observation: str) -> None:
        """Record a key observation or diagnostic deduction."""
        cleaned = observation.strip()
        if cleaned and cleaned not in self.observations:
            self.observations.append(cleaned)

    def add_inspected_file(self, path: str) -> None:
        """Track inspected or outlined files to avoid redundant reads."""
        cleaned = path.replace("\\", "/").strip()
        if cleaned and cleaned not in self.inspected_files:
            self.inspected_files.append(cleaned)

    def record_attempt(
        self,
        model: str,
        provider: str,
        failure_kind: Optional[str] = None,
        reason: str = "",
        turns: int = 0,
    ) -> None:
        """Log a failed execution attempt before fallback."""
        self.failed_attempts.append(
            {
                "model": model,
                "provider": provider,
                "failure_kind": failure_kind,
                "reason": reason,
                "turns": turns,
                "timestamp": time.time(),
            }
        )

    def record_syntax_rejection(self, file_path: str, error: str) -> None:
        """Record an in-memory patch rejection (file + error) for cross-fallback hints."""
        fp = mask_secrets(str(file_path or "").replace("\\", "/").strip())[
            :_MAX_REJECTION_FIELD_CHARS
        ]
        err = mask_secrets(" ".join(str(error or "").split()))[:_MAX_REJECTION_FIELD_CHARS]
        if not fp and not err:
            return
        self.syntax_rejections.append({"file": fp, "error": err})
        self.syntax_rejections = self.syntax_rejections[-_MAX_SYNTAX_REJECTIONS:]

    def record_rollback_reason(self, reason: str) -> None:
        """Record why a rollback fired (stagnation, verify failure, provider error)."""
        cleaned = mask_secrets(" ".join(str(reason or "").split()))[:_MAX_REJECTION_FIELD_CHARS]
        if cleaned:
            self.rollback_reasons.append(cleaned)
            self.rollback_reasons = self.rollback_reasons[-_MAX_ROLLBACK_REASONS:]

    def ingest_swarm_payload(self, raw_payload: Any, agent_type: str) -> CanonicalAgentTurn:
        """Normalize a raw agent payload and fold its effects into this state."""
        from botex.core.context_inclusion import OutputNormalizer

        turn = OutputNormalizer().normalize(raw_payload, agent_type)
        self.agent_turns.append(turn)

        for path in turn.mutated_files:
            cleaned = str(path).replace("\\", "/").strip()
            if cleaned and cleaned not in self.mutated_files:
                self.mutated_files.append(cleaned)
            if cleaned and cleaned not in self.inspected_files:
                self.inspected_files.append(cleaned)

        memory_keys = turn.metadata.get("shared_memory_keys") or []
        for key in memory_keys:
            key_str = str(key).strip()
            if key_str and key_str not in self.shared_memory_keys:
                self.shared_memory_keys.append(key_str)

        blocking = turn.metadata.get("blocking_reason")
        if blocking:
            self.active_blocking_reason = str(blocking).strip() or None

        return turn

    def format_handoff_context(self, max_observations: int = 5) -> str:
        """Render a concise markdown handoff block for injection into the next candidate's context."""
        parts: List[str] = ["### RESUMED CHECKPOINT CONTEXT"]
        if self.goal:
            parts.append(f"Original Goal: {self.goal}")

        if self.plan_steps:
            steps_preview = "\n".join(f"- {step}" for step in self.plan_steps[:8])
            parts.append(f"Established Plan Steps:\n{steps_preview}")

        if self.inspected_files:
            files_preview = ", ".join(self.inspected_files[:10])
            parts.append(f"Files Already Inspected: {files_preview}")

        if self.observations:
            obs_preview = "\n".join(f"- {obs}" for obs in self.observations[-max_observations:])
            parts.append(f"Key Findings from Previous Attempt:\n{obs_preview}")

        if self.failed_attempts:
            last = self.failed_attempts[-1]
            parts.append(
                f"Previous Fallback Trigger: model={last.get('model')}, "
                f"provider={last.get('provider')}, failure={last.get('failure_kind') or 'error'}"
            )

        if self.syntax_rejections:
            rejects_preview = "\n".join(
                f"- {sr.get('file') or 'unknown'}: {sr.get('error') or ''}"
                for sr in self.syntax_rejections[-3:]
            )
            parts.append(f"Rejected Edits (do NOT repeat these):\n{rejects_preview}")

        if self.rollback_reasons:
            parts.append(f"Rollback Reasons: {'; '.join(self.rollback_reasons[-3:])}")

        parts.append(
            "Instructions: Use these established findings to proceed immediately without re-inspecting "
            "already examined files from scratch."
        )
        return "\n\n".join(parts)

    def register_task_plugin(self, name: str, plugin_meta: Dict[str, Any]) -> None:
        """Register a task plugin with its metadata."""
        self.task_plugins[str(name).strip()] = dict(plugin_meta)

    def record_step_snapshot(self, step: int, snapshot_id: str) -> None:
        """Record a snapshot ID for a given step number."""
        self.step_snapshots[int(step)] = str(snapshot_id)

    def add_staged_plugin(self, plugin_meta: Dict[str, Any]) -> None:
        """Add a plugin to staged list, avoiding duplicates by 'name' key."""
        plugin_name = str(plugin_meta.get("name", "")).strip()
        if not plugin_name:
            return
        # Remove any existing plugin with the same name
        self.staged_plugins = [
            p for p in self.staged_plugins if str(p.get("name", "")).strip() != plugin_name
        ]
        self.staged_plugins.append(dict(plugin_meta))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state for storage or transmission."""
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "plan_steps": list(self.plan_steps),
            "observations": list(self.observations),
            "inspected_files": list(self.inspected_files),
            "failed_attempts": [dict(a) for a in self.failed_attempts],
            "agent_turns": [t.to_dict() for t in self.agent_turns],
            "mutated_files": list(self.mutated_files),
            "shared_memory_keys": list(self.shared_memory_keys),
            "active_blocking_reason": self.active_blocking_reason,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "task_plugins": dict(self.task_plugins),
            "step_snapshots": {int(k): str(v) for k, v in self.step_snapshots.items()},
            "staged_plugins": [dict(p) for p in self.staged_plugins],
            "syntax_rejections": [dict(s) for s in self.syntax_rejections],
            "rollback_reasons": list(self.rollback_reasons),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> InFlightTaskState:
        """Reconstruct state from a dictionary."""
        state = cls(
            task_id=str(data.get("task_id", "")),
            goal=str(data.get("goal", "")),
            plan_steps=list(data.get("plan_steps") or []),
            observations=list(data.get("observations") or []),
            inspected_files=list(data.get("inspected_files") or []),
            failed_attempts=list(data.get("failed_attempts") or []),
            agent_turns=[CanonicalAgentTurn.from_dict(t) for t in (data.get("agent_turns") or [])],
            mutated_files=list(data.get("mutated_files") or []),
            shared_memory_keys=list(data.get("shared_memory_keys") or []),
            active_blocking_reason=data.get("active_blocking_reason"),
            created_at=float(data.get("created_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
            task_plugins=dict(data.get("task_plugins") or {}),
            step_snapshots={int(k): str(v) for k, v in (data.get("step_snapshots") or {}).items()},
            staged_plugins=list(data.get("staged_plugins") or []),
        )
        # Restored contrastive fields re-enter through the recorders so
        # masking, whitespace normalization, and bounds are re-enforced.
        for s in data.get("syntax_rejections") or []:
            if isinstance(s, dict):
                state.record_syntax_rejection(str(s.get("file") or ""), str(s.get("error") or ""))
        for r in data.get("rollback_reasons") or []:
            state.record_rollback_reason(str(r))
        return state


def _inflight_dir(workspace_dir: str | Path) -> Path:
    return (Path(workspace_dir).resolve() / ".botex" / "inflight").resolve()


def _inflight_path(task_id: str, workspace_dir: str | Path) -> Optional[Path]:
    clean_id = sanitize_identifier(str(task_id or ""))
    if not clean_id:
        return None
    base_dir = _inflight_dir(workspace_dir)
    target = (base_dir / f"{clean_id}.json").resolve()
    if not target.is_relative_to(base_dir):
        return None
    return target


def _mask_json_value(value: Any) -> Any:
    """Recursively mask secrets in strings before JSON serialization."""
    if isinstance(value, str):
        return coerce_text(mask_secrets(value))
    if isinstance(value, dict):
        return {
            coerce_text(mask_secrets(str(k))) if isinstance(k, str) else k: _mask_json_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_json_value(item) for item in value]
    return value


def save_inflight_checkpoint(
    state: InFlightTaskState,
    workspace_dir: str | Path,
) -> Optional[Path]:
    """Atomically persist lightweight ``InFlightTaskState`` metadata to ``.botex/inflight/``."""
    target = _inflight_path(state.task_id, workspace_dir)
    if target is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    # Mask values before serialization so key=value regexes cannot eat JSON quotes.
    payload = coerce_text(json.dumps(_mask_json_value(state.to_dict()), ensure_ascii=False))
    tmp_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f".{target.stem}-",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
        return target
    except OSError:
        return None
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def load_inflight_checkpoints(workspace_dir: str | Path) -> List[InFlightTaskState]:
    """Load all valid ``InFlightTaskState`` checkpoints from ``.botex/inflight/``."""
    base_dir = _inflight_dir(workspace_dir)
    if not base_dir.is_dir():
        return []
    states: List[InFlightTaskState] = []
    for entry in sorted(base_dir.glob("*.json")):
        if not entry.is_file():
            continue
        try:
            raw = entry.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                states.append(InFlightTaskState.from_dict(parsed))
        except Exception:
            continue
    return states


def clear_inflight_checkpoint(task_id: str, workspace_dir: str | Path) -> bool:
    """Remove the ``.botex/inflight/<task_id>.json`` checkpoint if present."""
    target = _inflight_path(task_id, workspace_dir)
    if target is None or not target.is_file():
        return False
    try:
        target.unlink()
        return True
    except OSError:
        return False
