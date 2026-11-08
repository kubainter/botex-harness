# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for server-side recall: graph expansion, budget, fallback, MCP wiring."""

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
        memory.save_memory(
            workspace_dir=str(ws_dir),
            title="Unrelated pizza note",
            content="Margherita is best.",
            kind="context",
        )
        yield ws_dir, {"adr": adr["id"], "main": main["id"], "ref": ref["id"]}


def test_gather_expands_links_and_backlinks(vault):
    ws_dir, ids = vault
    res = memory.gather_recall_context(workspace_dir=str(ws_dir), query="JWT refresh cron")
    assert res["ok"] is True
    by_id = {n["id"]: n for n in res["nodes"]}
    assert by_id[ids["main"]]["relation"] == "match"
    assert ids["adr"] in by_id
    assert ids["ref"] in by_id
    assert all(
        set(n) == {"id", "title", "kind", "scope", "relation", "content"} for n in res["nodes"]
    )
    # matches come before neighbours
    relations = [n["relation"] for n in res["nodes"]]
    assert relations.index("match") == 0


def test_gather_respects_token_budget(vault):
    ws_dir, _ = vault
    res = memory.gather_recall_context(
        workspace_dir=str(ws_dir), query="JWT refresh cron", token_budget=500
    )
    assert res["ok"] is True
    assert res["token_estimate"] <= 500


def test_gather_no_hits_returns_empty(vault):
    ws_dir, _ = vault
    res = memory.gather_recall_context(workspace_dir=str(ws_dir), query="kubernetes helm zzqx")
    assert res["ok"] is True
    assert res["nodes"] == []


def test_deterministic_recall_not_found():
    out = memory.deterministic_recall("x", [])
    assert out["confidence"] == "NOT_FOUND"
    assert out["sources"] == []
    assert out["tier_used"] == "deterministic"


def test_deterministic_recall_digest():
    nodes = [
        {"id": "a", "title": "A", "relation": "match", "content": "# H\nfact one\nfact two"},
        {"id": "b", "title": "B", "relation": "link", "content": "linked fact"},
    ]
    out = memory.deterministic_recall("q", nodes)
    assert out["sources"] == ["a", "b"]
    assert out["confidence"] == "MEDIUM"
    assert "fact one" in out["answer"]


def _fake_curator(result):
    mod = types.ModuleType("ops.curator")

    async def synthesize_recall(query, nodes):
        return result(query, nodes) if callable(result) else result

    mod.synthesize_recall = synthesize_recall
    return mod


def test_recall_uses_llm_result(vault):
    ws_dir, ids = vault
    fake = _fake_curator(
        lambda q, n: {
            "answer": "JWT refresh every 15 min.",
            "sources": [ids["main"]],
            "confidence": "HIGH",
            "tier_used": "local-ollama",
        }
    )
    with patch.dict(sys.modules, {"ops.curator": fake}):
        out = asyncio.run(server._recall_memory_impl(str(ws_dir), "JWT refresh cron"))
    assert out["ok"] is True
    assert out["tier_used"] == "local-ollama"
    assert out["sources"] == [ids["main"]]
    assert out["context_nodes"] >= 3


def test_recall_falls_back_when_cascade_returns_none(vault):
    ws_dir, ids = vault
    with patch.dict(sys.modules, {"ops.curator": _fake_curator(None)}):
        out = asyncio.run(server._recall_memory_impl(str(ws_dir), "JWT refresh cron"))
    assert out["tier_used"] == "deterministic"
    assert ids["main"] in out["sources"]


def test_recall_falls_back_when_cascade_raises(vault):
    ws_dir, _ = vault

    def boom(q, n):
        raise RuntimeError("provider down")

    with patch.dict(sys.modules, {"ops.curator": _fake_curator(boom)}):
        out = asyncio.run(server._recall_memory_impl(str(ws_dir), "JWT refresh cron"))
    assert out["ok"] is True
    assert out["tier_used"] == "deterministic"


def test_recall_not_found_skips_llm(vault):
    ws_dir, _ = vault

    def must_not_call(q, n):
        raise AssertionError("LLM called without context")

    with patch.dict(sys.modules, {"ops.curator": _fake_curator(must_not_call)}):
        out = asyncio.run(server._recall_memory_impl(str(ws_dir), "kubernetes helm zzqx"))
    assert out["confidence"] == "NOT_FOUND"
    assert out["sources"] == []
