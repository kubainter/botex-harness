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
from botex.state import (
    InFlightTaskState,
    clear_inflight_checkpoint,
    load_inflight_checkpoints,
    save_inflight_checkpoint,
)


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


def test_in_flight_state_plugins_and_step_snapshots():
    """Verify task_plugins, step_snapshots, and staged_plugins serialization on InFlightTaskState."""
    state = InFlightTaskState(task_id="task-plug-1", goal="Test plugin state")
    state.register_task_plugin("proto_lint", {"name": "proto_lint", "reusability": "reusable"})
    state.record_step_snapshot(1, "step_1")
    state.add_staged_plugin({"name": "proto_lint", "scope": "staging"})
    state.add_staged_plugin({"name": "proto_lint", "scope": "staging"})  # duplicate ignored

    serialized = state.to_dict()
    assert "proto_lint" in serialized["task_plugins"]
    assert serialized["step_snapshots"][1] == "step_1"
    assert len(serialized["staged_plugins"]) == 1

    restored = InFlightTaskState.from_dict(serialized)
    assert restored.task_plugins["proto_lint"]["reusability"] == "reusable"
    assert restored.step_snapshots[1] == "step_1"
    assert len(restored.staged_plugins) == 1


def test_snapshot_manager_step_indexed_snapshots(tmp_path: Path):
    """Verify O(1) create_step_snapshot and restore_step_snapshot across existing and newly created files."""
    from botex.patch_engine import SnapshotManager

    snap_dir = tmp_path / ".snapshots"
    ws = tmp_path / "ws"
    ws.mkdir()
    existing_file = ws / "app.py"
    existing_file.write_text("x = 1\n", encoding="utf-8")
    future_file = ws / "new_helper.py"

    mgr = SnapshotManager(base_dir=snap_dir)
    mgr.begin_task("t1", workspace_root=ws)

    step_id = mgr.create_step_snapshot("t1", 1, [existing_file, future_file], workspace_root=ws)
    assert step_id == "step_1"

    # Mutate existing file and create future_file in step 1
    existing_file.write_text("x = 999  # formatted and mutated\n", encoding="utf-8")
    future_file.write_text("def helper(): pass\n", encoding="utf-8")

    restored = mgr.restore_step_snapshot("t1", 1)
    assert len(restored) == 2
    assert existing_file.read_text(encoding="utf-8") == "x = 1\n"
    assert not future_file.exists()
    mgr.end_task("t1")


# ==============================================================================
# Integration Tests: Explicit Model Binding & Free/Quota Fallback Filter
# (cost-safety remediation follow-up, arena_chat403_20261110 / provider binding)
# ==============================================================================


@pytest.mark.anyio
async def test_run_botex_task_explicit_model_disables_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An explicit model binds strictly to its provider — never rotate cross-provider."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    monkeypatch.setenv("OPENROUTER_API_FREE_KEY", "test-or-free-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nv-key")
    cfg = copy.deepcopy(load_config())
    cfg["provider_fallbacks"] = {"openrouter-free": ["nvidia", "openrouter"]}

    mock_client = MockClient(
        [_make_response("STATUS: UNSUPPORTED\nProvider cannot perform this task")]
    )

    def fake_resolve_candidates(model, profile, provider):
        return ["explicit-model-x"]

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
            task="Explicit model must not rotate providers",
            workspace_dir=str(tmp_path),
            model="explicit-model-x",
            provider="openrouter-free",
            mode="readonly",
        )

        assert result["status"] != "DONE"
        assert len(result["attempts"]) == 1
        assert result["attempts"][0]["model"] == "explicit-model-x"
        assert result["attempts"][0]["provider"] == "openrouter-free"
        assert all(a["provider"] != "openrouter" for a in result["attempts"])


@pytest.mark.anyio
async def test_run_botex_task_free_quota_fallbacks_exclude_paid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Free/quota providers never silently spill over into paid openrouter fallbacks."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    monkeypatch.setenv("OPENROUTER_API_FREE_KEY", "test-or-free-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nv-key")
    cfg = copy.deepcopy(load_config())
    cfg["provider_fallbacks"] = {"openrouter-free": ["nvidia", "openrouter"]}

    mock_client = MockClient(
        [
            _make_response("STATUS: UNSUPPORTED\nfree primary failed"),
            _make_response("STATUS: UNSUPPORTED\nnvidia fallback failed"),
        ]
    )

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
            task="Free provider must not fall back to paid openrouter",
            workspace_dir=str(tmp_path),
            model="",
            provider="openrouter-free",
            mode="readonly",
        )

        assert result["status"] != "DONE"
        attempt_providers = [a["provider"] for a in result["attempts"]]
        assert attempt_providers == ["openrouter-free", "nvidia"]
        assert "openrouter" not in attempt_providers


def test_in_flight_task_state_contrastive_fields_roundtrip_and_bounds():
    """Syntax rejections & rollback reasons normalize whitespace, mask secrets, cap at 5."""
    state = InFlightTaskState(task_id="t-pr2", goal="Fix bug")

    long_err = "SyntaxError: bad token  sk-1234567890abcdef1234567890  " + ("x" * 300)
    for i in range(7):
        state.record_syntax_rejection(f"botex\\core\\file_{i}.py", f"err {i} {long_err}")
        state.record_rollback_reason(f"reason\n{i}\t sk-1234567890abcdef1234567890 " + ("y" * 250))

    assert len(state.syntax_rejections) == 5
    assert state.syntax_rejections[0]["file"] == "botex/core/file_2.py"
    assert state.syntax_rejections[-1]["file"] == "botex/core/file_6.py"
    assert len(state.syntax_rejections[-1]["error"]) <= 200
    assert "\n" not in state.syntax_rejections[-1]["error"]
    assert "sk-1234567890" not in state.syntax_rejections[-1]["error"]

    assert len(state.rollback_reasons) == 5
    assert state.rollback_reasons[0].startswith("reason 2")
    assert len(state.rollback_reasons[-1]) <= 200
    assert "\n" not in state.rollback_reasons[-1]
    assert "sk-1234567890" not in state.rollback_reasons[-1]

    handoff = state.format_handoff_context()
    assert "Rejected Edits (do NOT repeat these):" in handoff
    assert "botex/core/file_6.py" in handoff
    assert "botex/core/file_2.py" not in handoff  # only last 3 rendered
    assert "Rollback Reasons:" in handoff
    assert "reason 6" in handoff
    assert "reason 2" not in handoff

    restored = InFlightTaskState.from_dict(state.to_dict())
    assert restored.syntax_rejections == state.syntax_rejections
    assert restored.rollback_reasons == state.rollback_reasons

    # Tampered checkpoint dict must also re-enforce bounds, whitespace flattening, and masking.
    tampered = state.to_dict()
    tampered["syntax_rejections"] = [
        {"file": f"a\\b_{i}.py", "error": f"bad\nsk-secretkey1234567890abcdef {i}"}
        for i in range(8)
    ]
    tampered["rollback_reasons"] = [f"rb\n{i} sk-secretkey1234567890abcdef" for i in range(8)]
    hardened = InFlightTaskState.from_dict(tampered)
    assert len(hardened.syntax_rejections) == 5
    assert len(hardened.rollback_reasons) == 5
    assert all("sk-secretkey1234567890abcdef" not in str(s) for s in hardened.syntax_rejections)
    assert all("sk-secretkey1234567890abcdef" not in r for r in hardened.rollback_reasons)


@pytest.mark.anyio
async def test_run_botex_task_forwards_contrastive_failures_into_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Attempt 1 syntax rejections and rollback reasons reach attempt 2 handoff_context."""
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nv-key")
    cfg = copy.deepcopy(load_config())
    cfg["provider_fallbacks"] = {"nvidia": []}
    captured_handoffs: List[str] = []
    attempt_calls = 0

    async def fake_once(*args, **kwargs):
        nonlocal attempt_calls
        attempt_calls += 1
        captured_handoffs.append(str(kwargs.get("handoff_context") or ""))
        if attempt_calls == 1:
            return {
                "ok": False,
                "task_id": "t1",
                "status": "MAX_TURNS_REACHED",
                "failure_kind": "turns_exhausted",
                "message": "No progress",
                "steps": 4,
                "cost_usd": 0.0,
                "files_touched": [],
                "syntax_rejections_detail": [
                    {"file": "botex/core/foo.py", "error": "SyntaxError: invalid syntax"}
                ],
                "rollback_reason": "rollback: no-progress loop (3 consecutive failed mutations)",
            }
        return {
            "ok": True,
            "task_id": "t2",
            "status": "DONE",
            "message": "Fixed cleanly",
            "steps": 1,
            "cost_usd": 0.0,
            "files_touched": ["botex/core/foo.py"],
        }

    with (
        patch("botex.engine.load_config", return_value=cfg),
        patch.object(engine, "resolve_model_candidates", return_value=["m1", "m2"]),
        patch.object(engine, "_run_botex_task_once", side_effect=fake_once),
        patch.object(engine, "check_model_price", return_value=(True, "")),
        patch.object(engine, "check_model_tools", return_value=(True, "")),
        patch.object(engine, "check_budget_limit", return_value=(False, 0.0)),
        patch.object(engine, "extract_meta_lesson", return_value=None) as mock_extract,
    ):
        res = await engine.run_botex_task(
            task="Repair foo.py",
            workspace_dir=str(tmp_path),
            provider="nvidia",
            mode="edit",
        )

    assert res["status"] == "DONE"
    assert len(captured_handoffs) == 2
    assert captured_handoffs[0] == ""
    assert "Rejected Edits (do NOT repeat these):" in captured_handoffs[1]
    assert "botex/core/foo.py: SyntaxError: invalid syntax" in captured_handoffs[1]
    assert "rollback: no-progress loop (3 consecutive failed mutations)" in captured_handoffs[1]
    mock_extract.assert_called_once()


def test_inflight_checkpoint_persistence_masking_and_corrupt_tolerance(tmp_path: Path):
    state = InFlightTaskState(
        task_id="task-ckpt-1",
        goal="Inspect sk-secretkey1234567890abcdef and fix bug",
        plan_steps=["step 1", "step 2"],
    )
    state.add_observation("token=sk-secretkey1234567890abcdef")

    saved_path = save_inflight_checkpoint(state, tmp_path)
    assert saved_path is not None
    assert saved_path.is_file()
    raw = saved_path.read_text(encoding="utf-8")
    assert "sk-secretkey1234567890abcdef" not in raw

    # Corrupt checkpoint alongside valid one must be tolerated without crashing
    corrupt_path = tmp_path / ".botex" / "inflight" / "corrupt.json"
    corrupt_path.write_text("{broken json", encoding="utf-8")

    # Path traversal task_id is rejected
    evil = InFlightTaskState(task_id="../../outside", goal="bad")
    assert save_inflight_checkpoint(evil, tmp_path) is None
    # Empty task_id never produces a checkpoint; missing dir loads empty
    assert save_inflight_checkpoint(InFlightTaskState(goal="anon"), tmp_path) is None
    assert load_inflight_checkpoints(tmp_path / "does-not-exist") == []

    loaded = load_inflight_checkpoints(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].task_id == "task-ckpt-1"
    assert loaded[0].plan_steps == ["step 1", "step 2"]

    assert clear_inflight_checkpoint("task-ckpt-1", tmp_path) is True
    assert clear_inflight_checkpoint("task-ckpt-1", tmp_path) is False
    assert load_inflight_checkpoints(tmp_path) == []


@pytest.mark.anyio
async def test_run_botex_task_clears_inflight_checkpoint_on_completion(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    mock_client = MockClient([_make_response("STATUS: DONE\nAll good")])
    seen_saves: List[str] = []
    real_save = save_inflight_checkpoint

    def spy_save(st: InFlightTaskState, ws: str | Path):
        seen_saves.append(st.task_id)
        return real_save(st, ws)

    with (
        patch.object(engine, "resolve_model_candidates", return_value=["cand-1"]),
        patch.object(engine, "build_async_client", return_value=mock_client),
        patch.object(engine, "check_model_price", return_value=(True, "")),
        patch.object(engine, "check_model_tools", return_value=(True, "")),
        patch.object(engine, "check_budget_limit", return_value=(False, 0.0)),
        patch.object(engine, "log_run", return_value={"cost_usd": 0.0}),
        patch.object(engine, "print_run_summary", return_value=None),
        patch.object(engine, "save_inflight_checkpoint", side_effect=spy_save),
    ):
        res = await engine.run_botex_task(
            task="Verify app.py",
            workspace_dir=str(tmp_path),
            provider="openrouter",
            api_key="test-or-key",
            mode="readonly",
        )

    assert res["status"] == "DONE"
    assert len(seen_saves) >= 1
    assert load_inflight_checkpoints(tmp_path) == []
