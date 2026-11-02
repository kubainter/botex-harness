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
class InFlightTaskState:
    """Snapshot of execution progress preserved across model/provider fallbacks."""

    task_id: str = ""
    goal: str = ""
    plan_steps: List[str] = field(default_factory=list)
    observations: List[str] = field(default_factory=list)
    inspected_files: List[str] = field(default_factory=list)
    failed_attempts: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

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

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state for storage or transmission."""
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "plan_steps": list(self.plan_steps),
            "observations": list(self.observations),
            "inspected_files": list(self.inspected_files),
            "failed_attempts": [dict(a) for a in self.failed_attempts],
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
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
            created_at=float(data.get("created_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )
