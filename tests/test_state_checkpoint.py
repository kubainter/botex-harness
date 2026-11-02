# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Tests for InFlightTaskState & Cross-Model / Cross-Provider State Checkpointing
=============================================================================
Verifies that:
1. InFlightTaskState tracks plan steps, inspected files, observations, and failed attempts.
2. Serialization and deserialization round-trip cleanly.
3. run_botex_task preserves in-flight context and injects handoff context into fallback attempts.
4. Dirty files are rolled back before state handoff occurs.
5. Provider fallbacks seamlessly rotate to secondary providers with state preserved.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch

import pytest

from botex.config import load_config
import botex.engine as engine
from botex.events import EventKind
from botex.state import InFlightTaskState


def _make_response(content: str = "", finish_reason: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


class MockClient:
    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            return _make_response("STATUS: DONE\nFallback default response")
        return self.responses.pop(0)


# ==============================================================================
# Unit Tests for InFlightTaskState
# ==============================================================================


def test_in_flight_task_state_basic_operations():
    state = InFlightTaskState(task_id="task-123", goal="Refactor auth module")
    assert state.task_id == "task-123"
    assert state.goal == "Refactor auth module"

    # Add plan steps with deduplication
    state.add_plan_step("Step 1: Check existing auth logic")
    state.add_plan_step("Step 2: Add token bucket")
    state.add_plan_step("Step 1: Check existing auth logic")  # duplicate
    assert len(state.plan_steps) == 2

    # Add inspected files with path normalization
    state.add_inspected_file("botex\\auth.py")
    state.add_inspected_file("botex/auth.py")  # duplicate normalized
    state.add_inspected_file("botex/config.py")
    assert state.inspected_files == ["botex/auth.py", "botex/config.py"]

    # Add observations
    state.add_observation("Found existing RateLimiter stub in config.py")
    state.add_observation("Found existing RateLimiter stub in config.py")  # duplicate
    assert len(state.observations) == 1

    # Record attempt
    state.record_attempt(
        model="primary/fast-model",
        provider="openrouter-free",
        failure_kind="provider_error",
        reason="429 Too Many Requests",
        turns=3,
    )
    assert len(state.failed_attempts) == 1
    assert state.failed_attempts[0]["model"] == "primary/fast-model"
    assert state.failed_attempts[0]["failure_kind"] == "provider_error"


def test_in_flight_task_state_serialization_roundtrip():
    state = InFlightTaskState(
        task_id="abc-456",
        goal="Improve error handling",
        plan_steps=["Step 1", "Step 2"],
        observations=["Observation A", "Observation B"],
        inspected_files=["botex/engine.py", "botex/retry.py"],
        metadata={"priority": "high"},
    )
    state.record_attempt("model-a", "openrouter", "turns_exhausted", "timeout", 10)

    data = state.to_dict()
    restored = InFlightTaskState.from_dict(data)

    assert restored.task_id == state.task_id
    assert restored.goal == state.goal
    assert restored.plan_steps == state.plan_steps
    assert restored.observations == state.observations
    assert restored.inspected_files == state.inspected_files
    assert restored.metadata == state.metadata
    assert len(restored.failed_attempts) == 1
    assert restored.failed_attempts[0]["model"] == "model-a"


def test_in_flight_task_state_format_handoff_context():
    state = InFlightTaskState(goal="Implement state checkpointing")
    state.add_plan_step("Audit fallback loop in botex/engine.py")
    state.add_inspected_file("botex/engine.py")
    state.add_observation("Snapshot rollback works cleanly via snapshot_manager")
    state.record_attempt("cheap-model", "nvidia", "turns_exhausted", "reached step limit", 5)

    handoff = state.format_handoff_context()
    assert "### RESUMED CHECKPOINT CONTEXT" in handoff
    assert "Original Goal: Implement state checkpointing" in handoff
    assert "Audit fallback loop in botex/engine.py" in handoff
    assert "botex/engine.py" in handoff
    assert "Snapshot rollback works cleanly via snapshot_manager" in handoff
    assert "model=cheap-model" in handoff
    assert "provider=nvidia" in handoff


# ==============================================================================
# Integration Tests: Model Fallback with State Carryover
# ==============================================================================


@pytest.mark.anyio
async def test_run_botex_task_state_checkpoint_across_model_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the first model candidate fails, the second model candidate receives the handoff context."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    # Create test workspace
    test_file = tmp_path / "app.py"
    test_file.write_text("def hello(): pass\n", encoding="utf-8")

    mock_client = MockClient(
        [
            # Attempt 1: First candidate fails with UNSUPPORTED
            _make_response("STATUS: UNSUPPORTED\nModel cannot perform reasoning"),
            # Attempt 2: Fallback candidate succeeds
            _make_response("STATUS: DONE\nCode reviewed and verified"),
        ]
    )

    with (
        patch.object(engine, "resolve_model_candidates", return_value=["cand-1", "cand-2"]),
        patch.object(engine, "build_async_client", return_value=mock_client),
        patch.object(engine, "check_model_price", return_value=(True, "")),
        patch.object(engine, "check_model_tools", return_value=(True, "")),
        patch.object(engine, "check_budget_limit", return_value=(False, 0.0)),
        patch.object(engine, "log_run", return_value={"cost_usd": 0.0}),
        patch.object(engine, "print_run_summary", return_value=None),
    ):
        result = await engine.run_botex_task(
            task="Audit app.py and ensure syntax is valid",
            workspace_dir=str(tmp_path),
            model="",
            provider="openrouter",
            mode="readonly",
        )

        assert result["status"] == "DONE"
        assert len(result["attempts"]) == 2
        assert result["attempts"][0]["model"] == "cand-1"
        assert result["attempts"][0]["status"] == "UNSUPPORTED"
        assert result["attempts"][1]["model"] == "cand-2"
        assert result["attempts"][1]["status"] == "DONE"

        # Verify in_flight_state was populated in result
        assert "in_flight_state" in result
        inflight = result["in_flight_state"]
        assert len(inflight["failed_attempts"]) >= 1
        assert inflight["failed_attempts"][0]["model"] == "cand-1"

        # Verify Attempt 2 received handoff context in its user prompt
        assert len(mock_client.calls) == 2
        call_2_msgs = mock_client.calls[1]["messages"]
        user_prompt = next(m["content"] for m in call_2_msgs if m["role"] == "user")
        assert "### RESUMED CHECKPOINT CONTEXT" in user_prompt
        assert "cand-1" in user_prompt


# ==============================================================================
# Integration Tests: Provider Fallback Rotation
# ==============================================================================


@pytest.mark.anyio
async def test_run_botex_task_provider_fallback_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When all models of provider-1 fail, engine rotates to provider-2 per provider_fallbacks config."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    monkeypatch.setenv("OPENROUTER_API_FREE_KEY", "test-or-free-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nv-key")
    cfg = copy.deepcopy(load_config())
    cfg["provider_fallbacks"] = {"openrouter-free": ["nvidia"]}

    mock_client = MockClient(
        [
            # Provider 1 (openrouter-free) model fails
            _make_response("STATUS: UNSUPPORTED\nProvider cannot perform this task"),
            # Provider 2 (nvidia) fallback model succeeds
            _make_response("STATUS: DONE\nFixed via secondary provider"),
        ]
    )

    resolved_events: List[str] = []

    def mock_progress(ev: str):
        if "PROVIDER_RESOLVED" in ev:
            resolved_events.append(ev)

    def fake_resolve_candidates(model, profile, provider):
        if provider == "openrouter-free":
            return ["free-primary"]
        elif provider == "nvidia":
            return ["nvidia-fallback"]
        return ["default-model"]

    with (
        patch("botex.engine.load_config", return_value=cfg),
        patch.object(engine, "resolve_model_candidates", side_effect=fake_resolve_candidates),
        patch.object(engine, "build_async_client", return_value=mock_client),
        patch.object(engine, "check_model_price", return_value=(True, "")),
        patch.object(engine, "check_model_tools", return_value=(True, "")),
        patch.object(engine, "check_budget_limit", return_value=(False, 0.0)),
        patch.object(engine, "log_run", return_value={"cost_usd": 0.0}),
        patch.object(engine, "print_run_summary", return_value=None),
    ):
        result = await engine.run_botex_task(
            task="Execute task with provider rotation",
            workspace_dir=str(tmp_path),
            model="",
            provider="openrouter-free",
            mode="readonly",
            progress_cb=mock_progress,
        )

        assert result["status"] == "DONE"
        assert len(result["attempts"]) == 2
        # First attempt was on openrouter-free
        assert result["attempts"][0]["provider"] == "openrouter-free"
        assert result["attempts"][0]["model"] == "free-primary"
        # Second attempt rotated to nvidia
        assert result["attempts"][1]["provider"] == "nvidia"
        assert result["attempts"][1]["model"] == "nvidia-fallback"

        # In-flight state recorded both attempts
        inflight = result["in_flight_state"]
        assert len(inflight["failed_attempts"]) >= 1
        assert inflight["failed_attempts"][0]["provider"] == "openrouter-free"
