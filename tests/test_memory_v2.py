# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for ecc.memory.v2, wikilinks, FTS5 index and RRF fusion."""

from pathlib import Path
from unittest.mock import patch
import pytest

from botex import memory


@pytest.fixture
def memory_dirs(tmp_path: Path):
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    global_dir = tmp_path / "global_memory"
    global_dir.mkdir()

    with patch("botex.memory.get_global_memory_dir", return_value=global_dir):
        yield ws_dir, global_dir


def test_wikilinks_extraction():
    """Verify regex extraction of Obsidian-style wikilinks."""
    text = "Check [[auth/jwt]] and [[ref_doc|Custom Title]] and repeated [[auth/jwt]]."
    links = memory.extract_wikilinks(text)
    assert links == ["auth/jwt", "ref_doc"]


def test_save_memory_v2_with_wikilinks_and_status(memory_dirs):
    """Verify ecc.memory.v2 saving auto-extracts links and supports status."""
    ws_dir, _ = memory_dirs
    res = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Architecture Decision: Modular Vault",
        content="This links to [[mem_arch_01]] and [[mem_arch_02]].",
        kind="decision",
        tags=["vault", "v2"],
        status="active",
        compiled_truth=True,
    )
    assert res["ok"] is True
    assert res["links"] == ["mem_arch_01", "mem_arch_02"]
    assert res["status"] == "active"
    assert res["compiled_truth"] is True

    # Read back
    read_res = memory.read_memory(workspace_dir=str(ws_dir), memory_id=res["id"])
    assert read_res["ok"] is True
    meta = read_res["metadata"]
    assert meta["schema"] == "ecc.memory.v2"
    assert meta["links"] == ["mem_arch_01", "mem_arch_02"]
    assert meta["status"] == "active"
    assert meta["compiled_truth"] is True


def test_fts5_indexing_and_search(memory_dirs):
    """Verify SQLite WAL FTS5 full-text indexing and fast search."""
    ws_dir, _ = memory_dirs
    m1 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Database Optimization",
        content="PostgreSQL connection pooling configuration with PgBouncer.",
        kind="context",
        tags=["db", "postgres"],
    )
    m2 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Frontend Styling",
        content="Tailwind CSS design tokens and responsive flexbox layouts.",
        kind="context",
        tags=["frontend", "css"],
    )

    # Search with keyword matching PostgreSQL
    res_db = memory.search_memory(workspace_dir=str(ws_dir), query="PostgreSQL")
    assert res_db["ok"] is True
    assert res_db["count"] >= 1
    ids = [m["id"] for m in res_db["memories"]]
    assert m1["id"] in ids
    assert m2["id"] not in ids

    # Search with keyword matching flexbox
    res_fe = memory.search_memory(workspace_dir=str(ws_dir), query="flexbox")
    assert res_fe["ok"] is True
    assert res_fe["count"] >= 1
    ids_fe = [m["id"] for m in res_fe["memories"]]
    assert m2["id"] in ids_fe
    assert m1["id"] not in ids_fe


def test_reciprocal_rank_fusion():
    """Verify RRF rank aggregation logic."""
    fts_ranks = {"docA": 0, "docB": 1, "docC": 2}
    vec_ranks = {"docB": 0, "docD": 1, "docA": 2}

    merged = memory.reciprocal_rank_fusion(fts_ranks, vec_ranks, k=60)
    # docB is rank 1 in FTS and rank 0 in Vec -> 1/61 + 1/60 = 0.01639 + 0.01667 = 0.03306 (highest)
    assert merged[0][0] == "docB"
    ids = [item[0] for item in merged]
    assert set(ids) == {"docA", "docB", "docC", "docD"}
