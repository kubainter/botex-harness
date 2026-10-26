# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for PKM Obsidian Vault Bridge, SQLite Graph Edges, and Backlinks."""

from pathlib import Path
from unittest.mock import patch
import pytest

from botex import memory, obsidian


@pytest.fixture
def memory_dirs(tmp_path: Path):
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    global_dir = tmp_path / "global_memory"
    global_dir.mkdir()

    with patch("botex.memory.get_global_memory_dir", return_value=global_dir):
        yield ws_dir, global_dir


def test_edge_indexing_and_backlinks(memory_dirs):
    """Verify SQLite edges table is populated and backlinks are queried correctly."""
    ws_dir, _ = memory_dirs

    # Create target notes
    m_target = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Authentication Standard",
        content="Core JWT and OAuth2 specifications.",
        kind="decision",
        tags=["auth", "spec"],
    )
    tid = m_target["id"]

    # Note A links to target
    m_a = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="User Login Implementation",
        content=f"Implemented login following [[{tid}]] and [[external_ref]].",
        kind="context",
        tags=["login"],
    )

    # Note B also links to target
    m_b = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="API Security Audit",
        content=f"Audited endpoints against [[{tid}]].",
        kind="lesson",
        tags=["security"],
    )

    # Query backlinks for target
    bl = memory.get_backlinks(workspace_dir=str(ws_dir), memory_id=tid)
    assert bl["ok"] is True
    assert bl["count"] == 2
    sources = {item["id"] for item in bl["backlinks"]}
    assert m_a["id"] in sources
    assert m_b["id"] in sources
    kinds = {item["kind"] for item in bl["backlinks"]}
    assert "context" in kinds
    assert "lesson" in kinds


def test_patch_memory_section_compiled_truth(memory_dirs):
    """Verify patch_memory_section updates Compiled Truth while keeping frontmatter and Timeline intact."""
    ws_dir, _ = memory_dirs

    initial_content = """# Memory Title

## Overview
This is a general overview of the project component.

## Compiled Truth
Initial draft of compiled truth state.

## Timeline
- 2026-10-01 Initial design discussion
- 2026-10-05 Alpha release
"""

    saved = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Component Architecture",
        content=initial_content,
        kind="decision",
        tags=["arch"],
        compiled_truth=False,
    )
    mid = saved["id"]

    # Surgically patch the Compiled Truth section
    new_truth = "Synthesized definitive state: v2 engine active with [[mem_edge_01]] support."
    res = memory.patch_memory_section(
        workspace_dir=str(ws_dir),
        memory_id=mid,
        section="Compiled Truth",
        new_content=new_truth,
    )
    assert res["ok"] is True
    assert res["compiled_truth"] is True
    assert "mem_edge_01" in res["links"]

    # Read back and verify sections
    read_back = memory.read_memory(workspace_dir=str(ws_dir), memory_id=mid)
    assert read_back["ok"] is True
    body = read_back["content"]

    assert "## Overview" in body
    assert "This is a general overview" in body
    assert new_truth in body
    assert "Initial draft of compiled truth" not in body
    assert "## Timeline" in body
    assert "- 2026-10-01 Initial design discussion" in body
    assert "- 2026-10-05 Alpha release" in body


def test_patch_memory_section_append_and_create(memory_dirs):
    """Verify patch_memory_section can append to sections and insert new sections before Timeline."""
    ws_dir, _ = memory_dirs

    initial_content = """# Note

## Timeline
- Event 1
"""
    saved = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Event Note",
        content=initial_content,
        kind="context",
    )
    mid = saved["id"]

    # 1. Create section before timeline
    r1 = memory.patch_memory_section(
        workspace_dir=str(ws_dir),
        memory_id=mid,
        section="Key Decisions",
        new_content="Decision 1: Use SQLite WAL",
    )
    assert r1["ok"] is True

    # 2. Append to existing section
    r2 = memory.patch_memory_section(
        workspace_dir=str(ws_dir),
        memory_id=mid,
        section="Key Decisions",
        new_content="Decision 2: Use FTS5 Tokenizer",
        append=True,
    )
    assert r2["ok"] is True

    read_back = memory.read_memory(workspace_dir=str(ws_dir), memory_id=mid)["content"]
    assert "Decision 1: Use SQLite WAL" in read_back
    assert "Decision 2: Use FTS5 Tokenizer" in read_back
    # Verify section appears before Timeline
    assert read_back.find("## Key Decisions") < read_back.find("## Timeline")


def test_find_graph_health_broken_links_and_orphans(memory_dirs):
    """Verify graph health auditor identifies broken links, orphan notes, and cycles."""
    ws_dir, _ = memory_dirs

    # 1. Note with broken link
    m_broken = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Broken Note",
        content="References [[nonexistent_target_999]].",
    )

    # 2. Isolated orphan note
    m_orphan = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Orphan Note",
        content="Zero incoming and zero outgoing references.",
    )

    # 3. Cyclic notes (A -> B -> A)
    mid_cycle1 = "cycle_node_1"
    mid_cycle2 = "cycle_node_2"

    memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id=mid_cycle1,
        title="Cycle Node 1",
        content=f"Links to [[{mid_cycle2}]].",
    )
    memory.save_memory(
        workspace_dir=str(ws_dir),
        memory_id=mid_cycle2,
        title="Cycle Node 2",
        content=f"Links back to [[{mid_cycle1}]].",
    )

    # Run health audit
    health = memory.find_graph_health(workspace_dir=str(ws_dir))
    assert health["ok"] is True
    assert health["is_healthy"] is False

    # Verify broken links
    broken_targets = {b["target"] for b in health["broken_links"]}
    assert "nonexistent_target_999" in broken_targets

    # Verify orphan notes
    orphan_ids = {o["id"] for o in health["orphans"]}
    assert m_orphan["id"] in orphan_ids

    # Verify cycle detected
    assert health["cycle_count"] >= 1


def test_obsidian_vault_initialization(memory_dirs):
    """Verify init_obsidian_vault generates valid Obsidian configs and index markdown."""
    ws_dir, _ = memory_dirs

    # Add a memory note to index
    m1 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Core Engine Decision",
        content="Decided to use async streaming with [[bielik_adapter]].",
        kind="decision",
        tags=["engine", "async"],
    )

    # Initialize vault
    init_res = obsidian.init_obsidian_vault(workspace_dir=str(ws_dir))
    assert init_res["ok"] is True
    assert init_res["count"] >= 1

    mem_dir = Path(init_res["vaults"][0])
    obs_dir = mem_dir / ".obsidian"
    assert obs_dir.is_dir()
    assert (obs_dir / "graph.json").is_file()
    assert (obs_dir / "app.json").is_file()
    assert (obs_dir / "core-plugins.json").is_file()

    # Verify graph.json color groups
    graph_cfg = obsidian.generate_graph_config()
    queries = {g["query"] for g in graph_cfg["colorGroups"]}
    assert "tag:#decision" in queries
    assert "tag:#lesson" in queries
    assert "tag:#handoff" in queries

    # Verify export index markdown
    exp_res = obsidian.export_graph_index_markdown(workspace_dir=str(ws_dir))
    assert exp_res["ok"] is True
    index_file = mem_dir / "_GRAPH_INDEX.md"
    assert index_file.is_file()
    index_text = index_file.read_text(encoding="utf-8")
    assert "# 🧠 BoteX Knowledge Graph & PKM Vault Index" in index_text
    assert f"[[{m1['id']}]]" in index_text
