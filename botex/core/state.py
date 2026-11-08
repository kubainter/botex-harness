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

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> InFlightTaskState:
        """Reconstruct state from a dictionary."""
        return cls(
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
