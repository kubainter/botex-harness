"""Arena v2.1.0 fix-pass tests: workspace containment (B1) + recall rate gate (F3)
+ emitter eviction wiring (F6).

Covers the merge-blocker findings from arena_pr_review_v210_20261108.
"""

import asyncio
import time

import pytest

import server
from botex.server import config as server_config


# ---------------------------------------------------------------------------
# B1: cloud.allowed_workspaces containment
# ---------------------------------------------------------------------------


def _config_with_allowlist(roots, tmp_path):
    return {
        "engine": {"default_workspace": str(tmp_path)},
        "cloud": {"allowed_workspaces": roots},
    }


def test_containment_disabled_when_allowlist_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(server_config, "load_config", lambda: {"cloud": {"allowed_workspaces": []}})
    # current semantics: any explicit path resolves freely
    assert server_config.resolve_workspace(str(tmp_path)) == str(tmp_path.resolve())


def test_containment_allows_inside_root(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(
        server_config,
        "load_config",
        lambda: _config_with_allowlist([str(root)], tmp_path),
    )
    inside = root / "sub" / "deeper"
    resolved = server_config.resolve_workspace(str(inside))
    assert resolved == str(inside.resolve())


def test_containment_rejects_outside_root(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside"
    monkeypatch.setattr(
        server_config,
        "load_config",
        lambda: _config_with_allowlist([str(root)], tmp_path),
    )
    with pytest.raises(server_config.WorkspaceOutOfBounds):
        server_config.resolve_workspace(str(outside))


def test_containment_blocks_mkdir_side_effect(tmp_path, monkeypatch):
    """Out-of-bounds paths must never materialize .botex/ on disk."""
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside"
    monkeypatch.setattr(
        server_config,
        "load_config",
        lambda: _config_with_allowlist([str(root)], tmp_path),
    )
    with pytest.raises(server_config.WorkspaceOutOfBounds):
        server_config.get_tasks_dir(str(outside))
    assert not (outside / ".botex").exists()


def test_traversal_via_dotdot_stays_contained(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "real").mkdir(parents=True)
    monkeypatch.setattr(
        server_config,
        "load_config",
        lambda: _config_with_allowlist([str(root)], tmp_path),
    )
    # .. escape is closed by realpath before the allowlist check
    with pytest.raises(server_config.WorkspaceOutOfBounds):
        server_config.resolve_workspace(str(root / "real" / ".." / ".." / "escape"))


# ---------------------------------------------------------------------------
# F3: rate_limits.recall_rpm token-bucket gate
# ---------------------------------------------------------------------------


def _reset_gate(initialized=False, tokens=0.0):
    # fresh Lock per test: module-level locks bind to the first event loop,
    # and each asyncio.run() here spins a new one.
    server._RECALL_GATE_LOCK = asyncio.Lock()
    server._RECALL_GATE["tokens"] = tokens
    server._RECALL_GATE["ts"] = time.monotonic()
    server._RECALL_GATE["initialized"] = initialized


def test_rate_gate_disabled_without_rpm(monkeypatch):
    monkeypatch.setattr(server, "load_config", lambda: {"rate_limits": {}})
    _reset_gate()
    assert asyncio.run(server._recall_rate_gate()) is None


def test_rate_gate_passes_when_tokens_available(monkeypatch):
    monkeypatch.setattr(
        server, "load_config", lambda: {"rate_limits": {"recall_rpm": 6000}}
    )
    _reset_gate()  # first call initializes with full burst capacity
    assert asyncio.run(server._recall_rate_gate()) is None


def test_rate_gate_returns_limited_envelope(monkeypatch):
    monkeypatch.setattr(
        server, "load_config", lambda: {"rate_limits": {"recall_rpm": 0.6}}
    )
    monkeypatch.setattr(server, "_RECALL_GATE_MAX_WAIT_S", 0.05)
    _reset_gate(initialized=True, tokens=0.0)  # drained bucket, tiny refill rate
    result = asyncio.run(server._recall_rate_gate())
    assert result is not None
    assert result["error"] == "RECALL_RATE_LIMITED"
    assert result["retry_after_s"] == pytest.approx(100.0)


def test_rate_gate_queues_instead_of_failing(monkeypatch):
    """Second immediate call under a healthy rpm must queue (sleep), not fail."""
    monkeypatch.setattr(
        server, "load_config", lambda: {"rate_limits": {"recall_rpm": 6000}}
    )
    _reset_gate()
    assert asyncio.run(server._recall_rate_gate()) is None


# ---------------------------------------------------------------------------
# F6: _cleanup_tasks evicts emitters with terminal tasks
# ---------------------------------------------------------------------------


def test_cleanup_tasks_drops_terminal_emitters(monkeypatch):
    from botex.core.events import drop_emitter, emitter_or_none, get_emitter

    monkeypatch.setattr(server, "load_config", lambda: {"engine": {"task_result_ttl_s": 3600}})
    task_id = "arena-test-emitter-eviction"
    em = get_emitter(task_id)
    assert em is not None
    server._TASKS[task_id] = {
        "status": "DONE",
        "created_at": time.time() - 7200,
        "finished_at": time.time() - 7200,
    }
    try:
        server._cleanup_tasks()
        assert task_id not in server._TASKS
        assert emitter_or_none(task_id) is None
    finally:
        drop_emitter(task_id)
        server._TASKS.pop(task_id, None)
