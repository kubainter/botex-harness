# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for Hindsight-inspired Memory Evolution features:
belief/heuristic kinds, confidence scoring, chronological filters,
graph spreading activation, and the reflect_memory MCP tool."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import server
from botex import memory


@pytest.fixture
def memory_dirs(tmp_path: Path):
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    global_dir = tmp_path / "global_memory"
    global_dir.mkdir()

    with patch("botex.memory.get_global_memory_dir", return_value=global_dir):
        yield ws_dir, global_dir


def _rewrite_timestamp(ws_dir: Path, mem_id: str, iso_ts: str) -> None:
    """Overwrite the frontmatter timestamp of a stored memory note."""
    f = ws_dir / ".botex" / "memory" / f"{mem_id}.md"
    text = f.read_text(encoding="utf-8")
    new = re.sub(
        r'^timestamp: "[^"]*"\r?$',
        f'timestamp: "{iso_ts}"',
        text,
        count=1,
        flags=re.M,
    )
    assert new != text, "timestamp line not found in frontmatter"
    f.write_text(new, encoding="utf-8")


def test_belief_and_heuristic_kinds_registered(memory_dirs):
    """Verify 'belief' and 'heuristic' are valid kinds and round-trip correctly."""
    ws_dir, _ = memory_dirs
    assert "belief" in memory.VALID_KINDS
    assert "heuristic" in memory.VALID_KINDS

    res_b = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Belief: cache is safe",
        content="We believe the local cache cannot corrupt state.",
        kind="belief",
    )
    assert res_b["ok"] is True
    assert res_b["kind"] == "belief"

    res_h = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Heuristic: retry backoff",
        content="Rule of thumb: exponential backoff starting at 250ms.",
        kind="HEURISTIC",
    )
    assert res_h["ok"] is True
    assert res_h["kind"] == "heuristic"

    read_b = memory.read_memory(workspace_dir=str(ws_dir), memory_id=res_b["id"])
    assert read_b["ok"] is True
    assert read_b["metadata"]["kind"] == "belief"

    # Unknown kinds still degrade to 'context'
    res_x = memory.save_memory(
        workspace_dir=str(ws_dir), title="Misc", content="x", kind="something"
    )
    assert res_x["kind"] == "context"


def test_confidence_persisted_in_frontmatter_and_result(memory_dirs):
    """Verify confidence is stored in YAML frontmatter, result dict, and readback."""
    ws_dir, _ = memory_dirs
    res = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Partially trusted claim",
        content="The provider may reorder fallback candidates.",
        kind="belief",
        confidence=0.35,
    )
    assert res["ok"] is True
    assert res["confidence"] == pytest.approx(0.35)

    mem_file = ws_dir / ".botex" / "memory" / f"{res['id']}.md"
    text = mem_file.read_text(encoding="utf-8")
    assert "confidence: 0.35" in text

    read_res = memory.read_memory(workspace_dir=str(ws_dir), memory_id=res["id"])
    assert read_res["ok"] is True
    assert read_res["metadata"]["confidence"] == pytest.approx(0.35)


def test_confidence_defaults_and_clamping(memory_dirs):
    """Verify confidence defaults to 1.0 and out-of-range/invalid inputs are normalized."""
    ws_dir, _ = memory_dirs

    default_res = memory.save_memory(
        workspace_dir=str(ws_dir), title="Default", content="d", kind="belief"
    )
    assert default_res["confidence"] == pytest.approx(1.0)

    high_res = memory.save_memory(
        workspace_dir=str(ws_dir), title="High", content="h", confidence=1.7
    )
    assert high_res["confidence"] == pytest.approx(1.0)

    low_res = memory.save_memory(
        workspace_dir=str(ws_dir), title="Low", content="l", confidence=-0.3
    )
    assert low_res["confidence"] == pytest.approx(0.0)

    bad_res = memory.save_memory(
        workspace_dir=str(ws_dir), title="Bad", content="b", confidence="not-a-float"
    )
    assert bad_res["confidence"] == pytest.approx(1.0)

    bool_res = memory.save_memory(
        workspace_dir=str(ws_dir), title="Bool", content="b", confidence=True
    )
    assert bool_res["confidence"] == pytest.approx(1.0)

    nan_res = memory.save_memory(
        workspace_dir=str(ws_dir), title="NaN", content="n", confidence=float("nan")
    )
    assert nan_res["confidence"] == pytest.approx(1.0)


def test_sqlite_confidence_column_and_legacy_migration(memory_dirs, tmp_path: Path):
    """Verify the confidence column is indexed and migrated into legacy databases."""
    ws_dir, _ = memory_dirs
    res = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Indexed confidence",
        content="confidence indexing check",
        kind="heuristic",
        confidence=0.42,
    )
    assert res["ok"] is True

    db_path = ws_dir / ".botex" / "memory" / "index.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT confidence FROM memories_meta WHERE id = ?;", (res["id"],)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["confidence"] == pytest.approx(0.42)

    # Simulate a legacy index.db lacking the confidence column
    legacy_dir = tmp_path / "legacy" / ".botex" / "memory"
    legacy_dir.mkdir(parents=True)
    legacy_db = legacy_dir / "index.db"
    conn = sqlite3.connect(str(legacy_db))
    conn.execute(
        """
        CREATE TABLE memories_meta (
            id TEXT PRIMARY KEY, title TEXT, kind TEXT, tags TEXT, links TEXT,
            timestamp TEXT, status TEXT, compiled_truth INTEGER DEFAULT 0, mtime REAL
        );
        """
    )
    conn.execute("INSERT INTO memories_meta (id, title) VALUES ('mem_old', 'Old');")
    conn.commit()
    conn.close()

    conn = memory._get_index_db(legacy_dir)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories_meta);").fetchall()}
    assert "confidence" in cols
    row = conn.execute("SELECT confidence FROM memories_meta WHERE id = 'mem_old';").fetchone()
    conn.close()
    assert row["confidence"] == pytest.approx(1.0)


def test_search_min_confidence_filter(memory_dirs):
    """Verify min_confidence threshold filtering through the SQLite index."""
    ws_dir, _ = memory_dirs
    for label, conf in (("weak", 0.2), ("medium", 0.7), ("strong", 0.95)):
        res = memory.save_memory(
            workspace_dir=str(ws_dir),
            title=f"Database claim {label}",
            content=f"database reliability note ({label})",
            kind="belief",
            confidence=conf,
            memory_id=f"mem_conf_{label}",
        )
        assert res["ok"] is True

    res = memory.search_memory(workspace_dir=str(ws_dir), query="database", min_confidence=0.5)
    assert res["ok"] is True
    ids = {m["id"] for m in res["memories"]}
    assert ids == {"mem_conf_medium", "mem_conf_strong"}
    for m in res["memories"]:
        assert m["confidence"] >= 0.5

    res_all = memory.search_memory(workspace_dir=str(ws_dir), query="database", min_confidence=0.0)
    assert {m["id"] for m in res_all["memories"]} == {
        "mem_conf_weak",
        "mem_conf_medium",
        "mem_conf_strong",
    }


def test_search_since_until_temporal_filters(memory_dirs):
    """Verify since/until chronological bounds filter entries by timestamp."""
    ws_dir, _ = memory_dirs
    old = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Ancient note",
        content="old knowledge about migrations",
        memory_id="mem_old_note",
    )
    new = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Fresh note",
        content="new knowledge about migrations",
        memory_id="mem_new_note",
    )
    assert old["ok"] and new["ok"]
    _rewrite_timestamp(ws_dir, "mem_old_note", "2020-01-15T10:00:00+00:00")

    # since excludes the ancient note
    res_since = memory.search_memory(
        workspace_dir=str(ws_dir), query="migrations", since="2021-01-01"
    )
    ids = {m["id"] for m in res_since["memories"]}
    assert "mem_new_note" in ids
    assert "mem_old_note" not in ids

    # until includes only the ancient note
    res_until = memory.search_memory(
        workspace_dir=str(ws_dir), query="migrations", until="2021-01-01"
    )
    ids = {m["id"] for m in res_until["memories"]}
    assert ids == {"mem_old_note"}

    # Bounded window with date-only bounds captures only the 2020 note
    res_window = memory.search_memory(
        workspace_dir=str(ws_dir),
        query="migrations",
        since="2020-01-01",
        until="2020-12-31",
    )
    ids = {m["id"] for m in res_window["memories"]}
    assert ids == {"mem_old_note"}

    # Combined with min_confidence
    res_combo = memory.search_memory(
        workspace_dir=str(ws_dir),
        query="migrations",
        until="2021-01-01",
        min_confidence=0.5,
    )
    assert {m["id"] for m in res_combo["memories"]} == {"mem_old_note"}


def test_search_invalid_temporal_and_confidence_filters(memory_dirs):
    """Verify malformed since/until/min_confidence produce explicit errors."""
    ws_dir, _ = memory_dirs

    res = memory.search_memory(workspace_dir=str(ws_dir), since="not-a-date")
    assert res["ok"] is False

    res = memory.search_memory(workspace_dir=str(ws_dir), until="2026-13-45")
    assert res["ok"] is False

    res = memory.search_memory(workspace_dir=str(ws_dir), min_confidence="high")
    assert res["ok"] is False

    res = memory.search_memory(workspace_dir=str(ws_dir), min_confidence=True)
    assert res["ok"] is False


def test_spreading_activation_surfaces_linked_notes(memory_dirs):
    """Verify 1-hop graph neighbours of FTS hits surface and linked hits get an RRF bonus."""
    ws_dir, _ = memory_dirs

    h2 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="WAL checkpoints",
        content="postgres wal checkpoint tuning",
        memory_id="mem_h2",
    )
    h1 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Postgres guide",
        content="postgres tuning guide, see [[mem_h2]]",
        memory_id="mem_h1",
    )
    h3 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Isolation levels",
        content="postgres isolation level notes",
        memory_id="mem_h3",
    )
    nb = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Vacuum chores",
        content="operational chores, see [[mem_h1]]",
        memory_id="mem_nb",
    )
    assert all(r["ok"] for r in (h1, h2, h3, nb))

    res = memory.search_memory(workspace_dir=str(ws_dir), query="postgres")
    assert res["ok"] is True
    memories = {m["id"]: m for m in res["memories"]}
    ids = list(memories.keys())

    # The non-matching but directly linked note is surfaced via activation
    assert "mem_nb" in memories
    assert memories["mem_nb"]["activated"] is True
    assert memories["mem_h3"]["activated"] is False

    # Linked hits outrank the unlinked hit regardless of raw FTS order:
    # RRF adds a graph component on top of the FTS component.
    assert ids.index("mem_h1") < ids.index("mem_h3")
    assert ids.index("mem_h2") < ids.index("mem_h3")


def test_reciprocal_rank_fusion_blends_activation_list():
    """Verify RRF merges FTS ranks with the graph-activation rank list."""
    fused = memory.reciprocal_rank_fusion({"a": 0, "c": 1}, {"b": 0, "a": 1})
    order = [mid for mid, _score in fused]
    assert set(order) == {"a", "b", "c"}
    # 'a' appears in both rank lists -> highest fused score
    assert order[0] == "a"
    # 'b' (graph rank 0 -> 1/60) outranks 'c' (FTS rank 1 -> 1/61)
    assert order.index("b") < order.index("c")


def test_reflect_memory_groups_and_synthesis(memory_dirs):
    """Verify reflect_memory groups hits by kind in reasoning order and synthesizes a digest."""
    ws_dir, _ = memory_dirs
    entries = [
        ("decision", 0.9, "Decision alpha", "mem_ref_dec"),
        ("belief", 0.8, "Belief alpha", "mem_ref_bel"),
        ("heuristic", 0.95, "Heuristic alpha", "mem_ref_heu"),
        ("lesson", 0.4, "Weak lesson alpha", "mem_ref_les"),
        ("context", 0.3, "Weak context alpha", "mem_ref_ctx"),
    ]
    for kind, conf, title, mid in entries:
        res = memory.save_memory(
            workspace_dir=str(ws_dir),
            title=title,
            content="alpha reflection body",
            kind=kind,
            confidence=conf,
            memory_id=mid,
        )
        assert res["ok"] is True

    ref = memory.reflect_memory(
        workspace_dir=str(ws_dir), query="alpha", min_confidence=0.5, scope="local"
    )
    assert ref["ok"] is True
    # entries below the confidence threshold are excluded
    assert ref["count"] == 3
    # canonical reasoning order: decision -> belief -> heuristic
    assert ref["kinds"] == ["decision", "belief", "heuristic"]
    assert ref["groups"]["decision"][0]["id"] == "mem_ref_dec"
    assert ref["groups"]["belief"][0]["confidence"] == pytest.approx(0.8)
    assert ref["avg_confidence"] == pytest.approx((0.9 + 0.8 + 0.95) / 3, abs=1e-3)
    assert "### Decision" in ref["synthesis"]
    assert "### Belief" in ref["synthesis"]
    assert "### Heuristic" in ref["synthesis"]
    assert "Belief alpha" in ref["synthesis"]


def test_reflect_memory_topic_filter_and_empty(memory_dirs):
    """Verify the topic tag scopes reflection and empty results synthesize cleanly."""
    ws_dir, _ = memory_dirs
    memory.save_memory(
        workspace_dir=str(ws_dir),
        title="DB belief",
        content="alpha storage belief",
        kind="belief",
        tags=["db"],
        confidence=0.9,
        memory_id="mem_topic_db",
    )
    memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Web belief",
        content="alpha frontend belief",
        kind="belief",
        tags=["web"],
        confidence=0.9,
        memory_id="mem_topic_web",
    )

    ref = memory.reflect_memory(workspace_dir=str(ws_dir), query="alpha", topic="db", scope="local")
    assert ref["ok"] is True
    assert ref["count"] == 1
    assert ref["topic"] == "db"
    assert ref["groups"]["belief"][0]["id"] == "mem_topic_db"

    ref_empty = memory.reflect_memory(
        workspace_dir=str(ws_dir), query="alpha", topic="nonexistent-tag", scope="local"
    )
    assert ref_empty["ok"] is True
    assert ref_empty["count"] == 0
    assert "No memories matched" in ref_empty["synthesis"]


def test_reflect_memory_mcp_tool_registered(memory_dirs):
    """Verify the reflect_memory MCP tool exists and returns a structured digest."""
    ws_dir, _ = memory_dirs

    async def _test():
        tools = await server.mcp.list_tools()
        names = {t.name for t in tools}
        assert "reflect_memory" in names

        saved = await server.save_memory(
            title="MCP belief",
            content="gamma mcp reflection body",
            kind="belief",
            confidence=0.9,
            workspace_dir=str(ws_dir),
            scope="local",
        )
        assert saved["ok"] is True
        assert saved["confidence"] == pytest.approx(0.9)

        res = await server.reflect_memory(
            query="gamma",
            workspace_dir=str(ws_dir),
            min_confidence=0.5,
        )
        assert res["ok"] is True
        assert res["count"] == 1
        assert "belief" in res["groups"]
        assert "MCP belief" in res["synthesis"]

        # New optional filters are exposed on the search_memory tool as well
        hits = await server.search_memory(
            workspace_dir=str(ws_dir),
            query="gamma",
            min_confidence=0.5,
            scope="local",
        )
        assert hits["ok"] is True
        assert hits["count"] == 1
        assert hits["memories"][0]["confidence"] == pytest.approx(0.9)
        assert "content" not in hits["memories"][0]

    asyncio.run(_test())
