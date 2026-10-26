# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Targeted edge-case and regression tests for PKM Obsidian Vault, Section Patching, and Graph Health."""

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


def test_patch_h3_h4_sections_preserves_siblings(memory_dirs):
    """C1 Regression Test: Patching an H3 or H4 subsection must NOT erase following sibling subsections."""
    ws_dir, _ = memory_dirs

    content = """# Deep Architecture Spec

## Section Two
Intro to section two.

### Subsection Alpha
Original content of Alpha.

### Subsection Beta
Original content of Beta that must remain intact.

### Subsection Gamma
Original content of Gamma that must remain intact.

## Timeline
- 2026-10-23 Initial entry
"""
    saved = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Deep Architecture",
        content=content,
        kind="decision",
    )
    mid = saved["id"]

    # Patch H3 Subsection Alpha
    res = memory.patch_memory_section(
        workspace_dir=str(ws_dir),
        memory_id=mid,
        section="Subsection Alpha",
        new_content="Updated content of Alpha.",
    )
    assert res["ok"] is True

    # Read back and verify siblings are completely preserved
    read_back = memory.read_memory(workspace_dir=str(ws_dir), memory_id=mid)["content"]
    assert "Updated content of Alpha." in read_back
    assert "### Subsection Beta" in read_back
    assert "Original content of Beta that must remain intact." in read_back
    assert "### Subsection Gamma" in read_back
    assert "Original content of Gamma that must remain intact." in read_back
    assert "## Timeline" in read_back


def test_patch_section_boundary_with_code_fences(memory_dirs):
    """C2 Regression Test: Header markers inside fenced code blocks must NOT split the section."""
    ws_dir, _ = memory_dirs

    content = """# Script Guide

## Code Samples
Here is a sample bash script:

```markdown
## Fake Header Inside Code Fence
This is inside the code block.
```

And some text after the code block.

## Next Section
Important subsequent notes.
"""
    saved = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Script Guide",
        content=content,
    )
    mid = saved["id"]

    # Replace Code Samples section
    new_sample = "Replacement code samples without fake headers."
    res = memory.patch_memory_section(
        workspace_dir=str(ws_dir),
        memory_id=mid,
        section="Code Samples",
        new_content=new_sample,
    )
    assert res["ok"] is True

    read_back = memory.read_memory(workspace_dir=str(ws_dir), memory_id=mid)["content"]
    assert new_sample in read_back
    assert "## Next Section" in read_back
    assert "Important subsequent notes." in read_back


def test_patch_timeline_overwriting_rejected(memory_dirs):
    """C3 Regression Test: Attempting to overwrite Timeline section must be rejected to protect audit trail."""
    ws_dir, _ = memory_dirs

    content = """# Audit Note

## Overview
Status overview.

## Timeline
- 2026-10-01 Creation
- 2026-10-02 Modification
"""
    saved = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Audit Note",
        content=content,
    )
    mid = saved["id"]

    # Overwrite attempt should fail
    res = memory.patch_memory_section(
        workspace_dir=str(ws_dir),
        memory_id=mid,
        section="Timeline",
        new_content="- 2026-10-99 Malicious Overwrite",
        append=False,
    )
    assert res["ok"] is False
    assert "append-only audit trail" in res["error"]

    # Append attempt should succeed
    res_app = memory.patch_memory_section(
        workspace_dir=str(ws_dir),
        memory_id=mid,
        section="Timeline",
        new_content="- 2026-10-03 Authorized Append",
        append=True,
    )
    assert res_app["ok"] is True

    read_back = memory.read_memory(workspace_dir=str(ws_dir), memory_id=mid)["content"]
    assert "- 2026-10-01 Creation" in read_back
    assert "- 2026-10-03 Authorized Append" in read_back


def test_sync_index_ignores_underscore_and_canonical_stem(memory_dirs):
    """W1 Regression Test: System index files (_GRAPH_INDEX.md) and notes are not deleted repeatedly."""
    ws_dir, _ = memory_dirs

    # Save a normal memory note
    m = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Standard Note",
        content="Testing [[target_x]].",
    )
    mid = m["id"]

    # Export graph index (_GRAPH_INDEX.md)
    exp = obsidian.export_graph_index_markdown(workspace_dir=str(ws_dir))
    assert exp["ok"] is True

    mem_dir = ws_dir / ".botex" / "memory"
    assert (mem_dir / "_GRAPH_INDEX.md").is_file()

    # Re-sync index
    memory.sync_memory_index(mem_dir)

    # Verify normal memory note is still present and indexed
    read_res = memory.read_memory(workspace_dir=str(ws_dir), memory_id=mid)
    assert read_res["ok"] is True
    assert read_res["id"] == mid


def test_inline_tags_and_links_parsing(memory_dirs):
    """Regression Test: Frontmatter inline-lists (tags: [a, b, c]) are parsed as list of strings, not char-split."""
    ws_dir, _ = memory_dirs

    raw_markdown = """---
id: test_inline_note
title: Inline Tags Note
kind: decision
tags: [architecture, obsidian, graph]
links: [target_alpha, target_beta]
status: active
compiled_truth: true
---

Body of the note referencing [[target_gamma]].
"""
    mem_dir = ws_dir / ".botex" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    note_path = mem_dir / "test_inline_note.md"
    note_path.write_text(raw_markdown, encoding="utf-8")

    memory.sync_memory_index(mem_dir)

    res = memory.read_memory(workspace_dir=str(ws_dir), memory_id="test_inline_note")
    assert res["ok"] is True
    tags = res["metadata"]["tags"]
    assert isinstance(tags, list)
    assert "architecture" in tags
    assert "obsidian" in tags
    assert "graph" in tags
    # Verify no single character tokens
    assert "a" not in tags
    assert "[" not in tags


def test_find_graph_health_iterative_deep_chain(memory_dirs):
    """Stress Test: Deep graph chain (1200 nodes) does not cause RecursionError in iterative DFS."""
    ws_dir, _ = memory_dirs
    mem_dir = ws_dir / ".botex" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)

    # Create a linear chain of 1200 nodes
    chain_length = 1200
    for i in range(chain_length):
        nid = f"chain_node_{i}"
        nxt = (
            f"chain_node_{i + 1}" if i < chain_length - 1 else "chain_node_0"
        )  # closed cycle at end
        md = f"""---
id: {nid}
title: Chain Node {i}
kind: context
status: active
---

Links to [[{nxt}]].
"""
        (mem_dir / f"{nid}.md").write_text(md, encoding="utf-8")

    memory.sync_memory_index(mem_dir)

    # This would crash with RecursionError under recursive DFS (default sys.getrecursionlimit() is 1000)
    health = memory.find_graph_health(workspace_dir=str(ws_dir))
    assert health["ok"] is True
    assert health["total_nodes"] == chain_length
    assert health["cycle_count"] >= 1
