# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for worker memory tools: TOOL_REGISTRY, tool_allowed, check_agency_policy, worker_bridge."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Set
from unittest.mock import AsyncMock

import pytest

from botex.core.capabilities import MODES, TOOL_REGISTRY, tool_allowed, check_agency_policy
from botex.memory.worker_bridge import (
    _cap_wrapped_text,
    worker_read_memory,
    worker_recall_memory,
)
from botex.memory.memory import reset_memory_index_cache, save_memory


def test_memory_read_tools_allowed_in_readonly_and_edit_modes():
    """Test tool allowance across modes and restricted tools."""
    # Read/write tools are allowed in readonly, chat, and edit modes
    for mode in ("readonly", "chat", "edit"):
        caps: Set[str] = MODES[mode]
        assert tool_allowed("read_memory", caps) is True
        assert tool_allowed("recall_memory", caps) is True
        assert tool_allowed("read_scratchpad", caps) is True
        assert check_agency_policy("read_memory", caps, confirm_fn=None) == ""
        assert check_agency_policy("recall_memory", caps, confirm_fn=None) == ""

    # Restricted tools not allowed even with can_confirm=True
    for name in ("save_memory", "delete_memory", "patch_memory_section"):
        assert name not in TOOL_REGISTRY
        assert tool_allowed(name, MODES["full"], can_confirm=True) is False


def test_worker_read_memory_resolves_global_swarm_note_by_default(tmp_path, monkeypatch):
    """Test that worker_read_memory resolves a global swarm note."""
    from botex import server

    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setenv("BOTEX_GLOBAL_MEMORY_DIR", str(global_dir))
    server.config.load_config(reload=True)

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    # Save a global swarm note
    mid = save_memory(
        workspace_dir=str(ws_dir),
        title="Swarm contract",
        content="Token: sk-test-1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        kind="decision",
        scope="global",
        project="swarm",
    )["id"]

    res = worker_read_memory(mid, workspace_dir=ws_dir)
    assert res["ok"] is True
    assert "<untrusted_memory_content" in res["content"]
    assert "sk-test-1234567890abcdef123456" not in res["content"]

    # Cleanup
    reset_memory_index_cache()


def test_worker_bridge_edge_workspace_isolates_from_cloud_local_dir(tmp_path, monkeypatch):
    """Test that edge workspace isolates from cloud/local dirs correctly."""
    from botex import server

    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setenv("BOTEX_GLOBAL_MEMORY_DIR", str(global_dir))
    server.config.load_config(reload=True)

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    # Save decoy local note
    local_mid = save_memory(
        workspace_dir=str(ws_dir),
        title="Local decoy",
        content="local content only",
        kind="context",
        scope="local",
    )["id"]

    # Save real global swarm note
    global_mid = save_memory(
        workspace_dir=str(ws_dir),
        title="Swarm contract",
        content="global content for swarm",
        kind="decision",
        scope="global",
        project="swarm",
    )["id"]

    # Edge workspace with scope='all' should read global vault
    assert (
        worker_read_memory(local_mid, scope="all", workspace_dir=ws_dir, is_edge_workspace=True)[
            "ok"
        ]
        is False
    )
    assert (
        worker_read_memory(global_mid, scope="all", workspace_dir=ws_dir, is_edge_workspace=True)[
            "ok"
        ]
        is True
    )

    # Edge workspace with scope='local' on global note should fail
    assert (
        worker_read_memory(global_mid, scope="local", workspace_dir=ws_dir, is_edge_workspace=True)[
            "error"
        ]
        == "LOCAL_VAULT_UNAVAILABLE_ON_EDGE"
    )

    # Edge recall with scope='local' should fail
    assert (
        asyncio.run(
            worker_recall_memory(
                "query", scope="local", workspace_dir=ws_dir, is_edge_workspace=True
            )
        )["error"]
        == "LOCAL_VAULT_UNAVAILABLE_ON_EDGE"
    )

    # Cleanup
    reset_memory_index_cache()


def test_worker_recall_memory_deterministic_and_payload_cap(tmp_path, monkeypatch):
    """Test deterministic recall and payload size capping."""
    from botex import server

    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setenv("BOTEX_GLOBAL_MEMORY_DIR", str(global_dir))
    server.config.load_config(reload=True)

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    # Empty query returns EMPTY_QUERY
    empty_res = asyncio.run(worker_recall_memory("", scope="global", workspace_dir=ws_dir))
    assert empty_res["ok"] is False
    assert empty_res["error"] == "EMPTY_QUERY"

    # Save a large global note (>20_000 bytes)
    large_content = "A" * 25000
    large_mid = save_memory(
        workspace_dir=str(ws_dir),
        title="Large decision",
        content=large_content,
        kind="decision",
        scope="global",
        project="swarm",
    )["id"]

    # Deterministic recall without LLM synthesis
    res = asyncio.run(
        worker_recall_memory(
            "large",
            scope="global",
            project="swarm",
            workspace_dir=ws_dir,
            use_llm_synthesis=False,
        )
    )
    assert res["ok"] is True
    assert res["recall_status"] == "DETERMINISTIC_BUDGET"
    assert "<untrusted_memory_recall>" in res["answer"]

    # Read memory should respect payload cap and truncate
    read_res = worker_read_memory(large_mid, scope="global", workspace_dir=ws_dir)
    assert read_res["ok"] is True
    assert read_res.get("truncated") is True
    assert len(json.dumps(read_res).encode("utf-8")) <= 16_384  # MAX_WORKER_MEMORY_PAYLOAD_BYTES
    assert read_res["content"].rstrip().endswith("</untrusted_memory_content>")

    # Cleanup
    reset_memory_index_cache()


def _failing_cloud_attempts() -> list[dict]:
    return [
        {"tier": "nvidia", "status": "error", "ms": 10},
        {"tier": "gemini", "status": "error", "ms": 10},
        {"tier": "openrouter-free", "status": "error", "ms": 10},
    ]


def test_cap_wrapped_text_preserves_trailing_suffix_when_truncated():
    suffix = "\n\nSTATIC RETRY HINT"
    wrapped, truncated = _cap_wrapped_text(
        "A" * 25000,
        tag="untrusted_memory_recall",
        trailing_suffix=suffix,
    )

    assert truncated is True
    assert "STATIC RETRY HINT" in wrapped
    assert wrapped.index("STATIC RETRY HINT") < wrapped.index("</untrusted_memory_recall>")


def _seed_global_recall_note(tmp_path, monkeypatch):
    from botex import server

    global_dir = tmp_path / "global"
    global_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("BOTEX_GLOBAL_MEMORY_DIR", str(global_dir))
    server.config.load_config(reload=True)

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(exist_ok=True)
    save_memory(
        workspace_dir=str(ws_dir),
        title="Alpha recall note",
        content="Alpha service uses bearer tokens.",
        kind="context",
        scope="global",
        project="swarm",
    )
    return ws_dir


@pytest.mark.anyio
async def test_worker_recall_transient_cloud_failure_defers_and_logs(tmp_path, monkeypatch, caplog):
    ws_dir = _seed_global_recall_note(tmp_path, monkeypatch)
    failing = {
        "answer": None,
        "sources": [],
        "confidence": "LOW",
        "tier_used": None,
        "tier_attempts": _failing_cloud_attempts(),
    }
    monkeypatch.setattr("botex.ops.curator.synthesize_recall", AsyncMock(return_value=failing))

    with caplog.at_level(logging.INFO, logger="botex.memory.worker_bridge"):
        res = await worker_recall_memory(
            "alpha",
            scope="global",
            project="swarm",
            workspace_dir=ws_dir,
        )

    assert res["ok"] is True
    assert res["recall_status"] == "QUOTA_EXHAUSTED_DEFER"
    assert "odpytaj ponownie" in res["answer"]
    assert any(
        "recall outcome status=QUOTA_EXHAUSTED_DEFER" in rec.getMessage()
        and "path=worker" in rec.getMessage()
        for rec in caplog.records
    )
    reset_memory_index_cache()


@pytest.mark.anyio
async def test_worker_recall_deterministic_budget_never_defers(tmp_path, monkeypatch, caplog):
    ws_dir = _seed_global_recall_note(tmp_path, monkeypatch)
    synth = AsyncMock()
    monkeypatch.setattr("botex.ops.curator.synthesize_recall", synth)

    with caplog.at_level(logging.INFO, logger="botex.memory.worker_bridge"):
        res = await worker_recall_memory(
            "alpha",
            scope="global",
            project="swarm",
            workspace_dir=ws_dir,
            use_llm_synthesis=False,
        )

    assert res["ok"] is True
    assert res["recall_status"] == "DETERMINISTIC_BUDGET"
    assert "odpytaj ponownie" not in res["answer"]
    assert synth.await_count == 0
    assert any(
        "recall outcome status=DETERMINISTIC_BUDGET" in rec.getMessage()
        and "path=worker" in rec.getMessage()
        for rec in caplog.records
    )
    reset_memory_index_cache()
