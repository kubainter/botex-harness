# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Tests for pre-compiled recall briefings — query-time serving (plan 055, W2).
===========================================================================
Covers the routing contract of ``server._recall_memory_impl`` and the
``_try_briefing_first_line`` helper:

1. Covered query -> BRIEFING_SERVED with zero LLM cascade invocations.
2. Weak query<->briefing overlap -> falls through to the cascade.
3. Stale briefing (age or superseded source) -> cascade path.
4. Cascade failure with all cloud tiers down -> QUOTA_EXHAUSTED_DEFER.
5. Answer shape stays additive (answer/sources/confidence/tier_used).
6. The briefing note never appears inside its own digest section.
7. gather_recall_context nodes carry tags (W2-1 contract).

All vault writes hit tmp_path workspaces and the conftest-isolated global
memory dir; no LLM or network access.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

import server
from botex import memory
from botex.ops.curator.recall_briefing import BRIEFING_TAG

QUERY = "jakie są zasady token budget dla recall?"


def _briefing_body() -> str:
    return (
        "## Decisions\n- Recall token budget stays bounded at 5000 tokens [[src_a]]\n\n"
        "## Conventions\n- POSIX paths in handoffs [[src_b]]\n\n"
        "## Gotchas\n- num_ctx guard truncates oversized prompts [[src_a]]\n\n"
        "## Open threads\n- None documented.\n"
    )


def _header(project: str, sources: str, generated: str) -> str:
    return (
        f"<!-- recall-briefing v1 project={project} generated={generated} "
        f"inputs=sha256:{'ab' * 32} sources={sources} -->"
    )


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".botex" / "memory").mkdir(parents=True)
    return workspace


def _seed_sources(ws, project: str = "alpha", count: int = 2) -> list[str]:
    ids = []
    for i in range(count):
        res = memory.save_memory(
            workspace_dir=str(ws),
            title=f"{project} source {i}",
            content=f"{project} fact {i}: recall token budget discipline.",
            kind="context",
            tags=[project],
            origin="botex",
            scope="global",
            project=project,
        )
        assert res.get("ok"), res
        ids.append(res["id"])
    return ids


def _seed_briefing(ws, sources: list[str], project: str = "alpha", age_h: float = 1.0) -> str:
    generated = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat()
    content = f"{_header(project, ','.join(sources), generated)}\n\n{_briefing_body()}"
    res = memory.save_memory(
        workspace_dir=str(ws),
        title=f"Recall Briefing: {project}",
        content=content,
        kind="context",
        tags=[BRIEFING_TAG, f"project:{project}"],
        origin="curator",
        scope="global",
        project=project,
    )
    assert res.get("ok"), res
    return res["id"]


def _cascade_payload(answer="synthesized answer", tier="nvidia"):
    return {
        "answer": answer,
        "sources": ["x"],
        "confidence": "HIGH",
        "tier_used": tier,
        "tier_attempts": [{"tier": tier, "status": "ok"}],
    }


def _failing_cloud_attempts():
    return [
        {"tier": "nvidia", "status": "unavailable"},
        {"tier": "gemini", "status": "error"},
        {"tier": "openrouter-free", "status": "timeout"},
    ]


@pytest.mark.anyio
async def test_covered_query_serves_briefing_without_cascade(ws):
    src_ids = _seed_sources(ws)
    bid = _seed_briefing(ws, src_ids)
    with patch(
        "botex.ops.curator.synthesize_recall",
        new=AsyncMock(side_effect=AssertionError("cascade must not fire")),
    ):
        result = await server._recall_memory_impl(str(ws), QUERY, project="alpha")
    assert result["ok"] is True
    assert result["recall_status"] == "BRIEFING_SERVED"
    assert result["tier_used"] == "briefing"
    assert result["briefing_id"] == bid
    assert result["confidence"] in ("MEDIUM", "HIGH")
    assert bid in result["sources"]
    assert "token budget" in result["answer"]
    assert result["answer"].count("<!--") == 0  # machine header never leaks


@pytest.mark.anyio
async def test_weak_overlap_falls_through_to_cascade(ws):
    src_ids = _seed_sources(ws)
    _seed_briefing(ws, src_ids)
    with patch(
        "botex.ops.curator.synthesize_recall", new=AsyncMock(return_value=_cascade_payload())
    ) as mock_cascade:
        result = await server._recall_memory_impl(str(ws), "alpha fact discipline", project="alpha")
    assert mock_cascade.await_count == 1
    assert result["recall_status"] == "SYNTHESIZED"
    assert result["answer"] == "synthesized answer"


@pytest.mark.anyio
async def test_stale_briefing_by_age_uses_cascade(ws):
    src_ids = _seed_sources(ws)
    _seed_briefing(ws, src_ids, age_h=200.0)  # beyond the 72h window
    with patch(
        "botex.ops.curator.synthesize_recall", new=AsyncMock(return_value=_cascade_payload())
    ) as mock_cascade:
        result = await server._recall_memory_impl(str(ws), QUERY, project="alpha")
    assert mock_cascade.await_count == 1
    assert result["recall_status"] == "SYNTHESIZED"


@pytest.mark.anyio
async def test_superseded_source_stales_briefing(ws):
    src_ids = _seed_sources(ws)
    _seed_briefing(ws, src_ids)
    mem = memory.read_memory(workspace_dir=str(ws), memory_id=src_ids[0], scope="global")
    assert mem.get("ok")
    memory.save_memory(
        workspace_dir=str(ws),
        memory_id=src_ids[0],
        title="alpha source 0",
        content="updated",
        kind="context",
        tags=["alpha"],
        origin="botex",
        scope="global",
        project="alpha",
        status="superseded",
    )
    with patch(
        "botex.ops.curator.synthesize_recall", new=AsyncMock(return_value=_cascade_payload())
    ) as mock_cascade:
        result = await server._recall_memory_impl(str(ws), QUERY, project="alpha")
    assert mock_cascade.await_count == 1
    assert result["recall_status"] == "SYNTHESIZED"


@pytest.mark.anyio
async def test_all_cloud_exhausted_yields_defer_status(ws):
    _seed_sources(ws)  # no briefing seeded -> cascade path
    failing = {
        "answer": None,
        "sources": [],
        "confidence": "LOW",
        "tier_used": None,
        "tier_attempts": _failing_cloud_attempts(),
    }
    with patch("botex.ops.curator.synthesize_recall", new=AsyncMock(return_value=failing)):
        result = await server._recall_memory_impl(str(ws), QUERY, project="alpha")
    assert result["recall_status"] == "QUOTA_EXHAUSTED_DEFER"
    assert "odpytaj ponownie" in result["answer"]
    assert result["tier_used"] == "deterministic"


@pytest.mark.anyio
async def test_partial_cloud_failure_is_not_defer(ws):
    _seed_sources(ws)
    failing = {
        "answer": None,
        "sources": [],
        "confidence": "LOW",
        "tier_used": None,
        "tier_attempts": [
            {"tier": "nvidia", "status": "unavailable"},
            {"tier": "gemini", "status": "prompt_guard"},
        ],
    }
    with patch("botex.ops.curator.synthesize_recall", new=AsyncMock(return_value=failing)):
        result = await server._recall_memory_impl(str(ws), QUERY, project="alpha")
    assert result["recall_status"] == "FALLBACK_MODELS_FAILED"


@pytest.mark.anyio
async def test_briefing_excluded_from_own_digest(ws):
    src_ids = _seed_sources(ws)
    bid = _seed_briefing(ws, src_ids)
    with patch(
        "botex.ops.curator.synthesize_recall",
        new=AsyncMock(side_effect=AssertionError("cascade must not fire")),
    ):
        result = await server._recall_memory_impl(str(ws), QUERY, project="alpha")
    assert result["recall_status"] == "BRIEFING_SERVED"
    digest_pos = result["answer"].find("Powiązane notatki")
    if digest_pos >= 0:
        assert "Recall Briefing" not in result["answer"][digest_pos:]


def test_gather_nodes_carry_tags(ws):
    _seed_sources(ws)
    ctx = memory.gather_recall_context(
        workspace_dir=str(ws), query="recall token budget", scope="all"
    )
    assert ctx.get("ok")
    assert ctx["nodes"], "expected retrieval hits"
    assert all("tags" in n for n in ctx["nodes"])


def test_overlap_scoring_uses_stop_token_gate():
    from botex.ops.curator.recall_briefing import briefing_overlap_score

    body = "Decisions: token budget bounded at 5000 tokens for recall packs."
    assert briefing_overlap_score("token budget recall", body) >= 2
    assert briefing_overlap_score("what is the and for", body) == 0
    assert briefing_overlap_score("xyzzy quantum", body) == 0


def test_cloud_tiers_exhausted_vocabulary():
    assert server._cloud_tiers_exhausted(_failing_cloud_attempts()) is True
    assert server._cloud_tiers_exhausted([{"tier": "nvidia", "status": "ok"}]) is False
    assert server._cloud_tiers_exhausted([]) is False
    assert server._cloud_tiers_exhausted([{"tier": "local-ollama", "status": "timeout"}]) is False


@pytest.mark.anyio
async def test_compile_then_serve_roundtrip(ws):
    """End-to-end: nightly compile output is served by the query-time path."""
    from botex.ops.curator.recall_briefing import run_briefing_compilation

    ids = []
    for i in range(5):
        res = memory.save_memory(
            workspace_dir=str(ws),
            title=f"alpha roundtrip {i}",
            content=f"alpha roundtrip {i}: recall token budget discipline and POSIX paths.",
            kind="context",
            tags=["alpha"],
            origin="botex",
            scope="global",
            project="alpha",
        )
        assert res.get("ok"), res
        ids.append(res["id"])

    async def _fake_infer(prompt: str) -> str:
        return (
            f"## Decisions\n- Recall token budget stays bounded [[{ids[0]}]]\n\n"
            f"## Conventions\n- POSIX paths everywhere [[{ids[1]}]]\n\n"
            f"## Gotchas\n- num_ctx guard truncates prompts [[{ids[0]}]]\n\n"
            "## Open threads\n- None.\n"
        )

    report = await run_briefing_compilation(str(ws), _fake_infer, min_notes=5, max_projects=2)
    assert report["compiled"] == 1, report

    with patch(
        "botex.ops.curator.synthesize_recall",
        new=AsyncMock(side_effect=AssertionError("cascade must not fire")),
    ):
        result = await server._recall_memory_impl(str(ws), QUERY, project="alpha")
    assert result["recall_status"] == "BRIEFING_SERVED"
    assert result["tier_used"] == "briefing"
    assert result["briefing_id"]
    assert "token budget" in result["answer"]
