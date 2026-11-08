# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Tests for recall_memory observability (contract v1.0 §4)
========================================================
Covers the additive response fields of server._recall_memory_impl:
- recall_status: SYNTHESIZED | ZERO_NODES_FOUND | FALLBACK_MODELS_FAILED
- tier_attempts: [{tier, status, ms}] propagated from the cascade
- workspace_searched: resolved workspace dir
- deterministic fallback preserved as answer-source on cascade failure.

All LLM calls are stubbed via a fake ops.curator module (no live network).
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import server
from botex import memory


@pytest.fixture
def vault(tmp_path: Path):
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    global_dir = tmp_path / "global_memory"
    global_dir.mkdir()
    with patch("botex.memory.get_global_memory_dir", return_value=global_dir):
        adr = memory.save_memory(
            workspace_dir=str(ws_dir),
            title="ADR token rotation",
            content="Signing keys rotate every 30 days via KMS.",
            kind="decision",
        )
        main = memory.save_memory(
            workspace_dir=str(ws_dir),
            title="VPS auth setup",
            content=f"JWT refresh runs every 15 minutes from cron. See [[{adr['id']}]].",
            kind="context",
        )
        ref = memory.save_memory(
            workspace_dir=str(ws_dir),
            title="Incident review",
            content=f"Outage traced to stale cron, cf. [[{main['id']}]].",
            kind="lesson",
        )
        yield ws_dir, {"adr": adr["id"], "main": main["id"], "ref": ref["id"]}


def _fake_curator(result):
    mod = types.ModuleType("ops.curator")

    async def synthesize_recall(query, nodes):
        return result(query, nodes) if callable(result) else result

    mod.synthesize_recall = synthesize_recall
    return mod


ATTEMPTS = [
    {"tier": "local-ollama", "status": "timeout", "ms": 12001},
    {"tier": "nvidia", "status": "ok", "ms": 830},
]


def test_recall_status_synthesized(vault):
    ws_dir, ids = vault
    fake = _fake_curator(
        {
            "answer": "JWT refresh every 15 min.",
            "sources": [ids["main"]],
            "confidence": "HIGH",
            "tier_used": "nvidia",
            "tier_attempts": ATTEMPTS,
        }
    )
    with patch.dict(sys.modules, {"ops.curator": fake}):
        out = asyncio.run(server._recall_memory_impl(str(ws_dir), "JWT refresh cron"))

    assert out["ok"] is True
    assert out["recall_status"] == "SYNTHESIZED"
    assert out["tier_attempts"] == ATTEMPTS
    assert out["workspace_searched"] == str(ws_dir)
    assert out["tier_used"] == "nvidia"
    assert out["context_nodes"] >= 3
    assert "truncated" in out


def test_recall_status_fallback_models_failed_on_failure_dict(vault):
    """Cascade failure dict (answer=None) -> deterministic + attempts preserved."""
    ws_dir, ids = vault
    fake = _fake_curator(
        {
            "answer": None,
            "sources": [],
            "confidence": None,
            "tier_used": None,
            "tier_attempts": ATTEMPTS,
        }
    )
    with patch.dict(sys.modules, {"ops.curator": fake}):
        out = asyncio.run(server._recall_memory_impl(str(ws_dir), "JWT refresh cron"))

    assert out["ok"] is True
    assert out["recall_status"] == "FALLBACK_MODELS_FAILED"
    assert out["tier_used"] == "deterministic"
    assert out["tier_attempts"] == ATTEMPTS
    assert ids["main"] in out["sources"]


def test_recall_status_fallback_when_cascade_returns_none(vault):
    ws_dir, _ = vault
    with patch.dict(sys.modules, {"ops.curator": _fake_curator(None)}):
        out = asyncio.run(server._recall_memory_impl(str(ws_dir), "JWT refresh cron"))

    assert out["recall_status"] == "FALLBACK_MODELS_FAILED"
    assert out["tier_used"] == "deterministic"
    assert out["tier_attempts"] == []


def test_recall_status_fallback_when_cascade_raises(vault):
    ws_dir, _ = vault

    def boom(q, n):
        raise RuntimeError("provider down")

    with patch.dict(sys.modules, {"ops.curator": _fake_curator(boom)}):
        out = asyncio.run(server._recall_memory_impl(str(ws_dir), "JWT refresh cron"))

    assert out["ok"] is True
    assert out["recall_status"] == "FALLBACK_MODELS_FAILED"
    assert out["tier_used"] == "deterministic"


def test_recall_status_zero_nodes_found_skips_llm(vault):
    ws_dir, _ = vault

    def must_not_call(q, n):
        raise AssertionError("LLM called without context")

    with patch.dict(sys.modules, {"ops.curator": _fake_curator(must_not_call)}):
        out = asyncio.run(server._recall_memory_impl(str(ws_dir), "kubernetes helm zzqx"))

    assert out["ok"] is True
    assert out["recall_status"] == "ZERO_NODES_FOUND"
    assert out["confidence"] == "NOT_FOUND"
    assert out["sources"] == []
    assert out["tier_attempts"] == []
    assert out["workspace_searched"] == str(ws_dir)


def test_recall_observability_with_real_cascade_no_keys(vault, monkeypatch):
    """End-to-end: real cascade with no provider keys -> FALLBACK_MODELS_FAILED
    and a real per-tier tier_attempts trace (hermetic: sockets blocked)."""
    for var in (
        "NVIDIA_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_FREE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    ws_dir, _ = vault
    from ops.curator import recall_curator

    # Real vault notes are ~4k chars; lift the prompt guard so tier-1 reaches
    # the (mocked) probe instead of short-circuiting on size.
    monkeypatch.setattr(recall_curator, "DEFAULT_NUM_CTX_GUARD_TOKENS", 100000.0)
    recall_curator._ANSWER_CACHE.clear()
    recall_curator._NEGATIVE_TIER_MARKS.clear()
    with (
        patch.object(recall_curator, "_get_json", side_effect=OSError("no ollama")),
        patch.object(recall_curator, "_probe_tcp", return_value=False),
    ):
        out = asyncio.run(server._recall_memory_impl(str(ws_dir), "JWT refresh cron"))
    recall_curator._ANSWER_CACHE.clear()
    recall_curator._NEGATIVE_TIER_MARKS.clear()

    assert out["ok"] is True
    assert out["recall_status"] == "FALLBACK_MODELS_FAILED"
    assert out["tier_used"] == "deterministic"
    tiers = {a["tier"]: a["status"] for a in out["tier_attempts"]}
    assert tiers["local-ollama"] == "unavailable"
    assert tiers["nvidia"] == "unavailable"
    assert tiers["gemini"] == "unavailable"
    assert tiers["openrouter-free"] == "unavailable"
    assert all(isinstance(a["ms"], int) and a["ms"] >= 0 for a in out["tier_attempts"])
