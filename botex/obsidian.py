# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Obsidian PKM Vault Bridge and visual graph generator for BoteX."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .memory import (
    _ensure_global_memory_dir,
    _ensure_workspace_memory_dir,
    _get_index_db,
    _resolve_scope_dirs,
    find_graph_health,
    sanitize_for_storage,
    sync_memory_index,
)

# Obsidian Graph Color Profiles for BoteX Knowledge Taxonomies
DEFAULT_COLOR_GROUPS: List[Dict[str, str]] = [
    {"query": "tag:#decision", "color": {"a": 1, "rgb": 11032055}},  # #a855f7 (Purple)
    {"query": "tag:#lesson", "color": {"a": 1, "rgb": 16109067}},  # #f59e0b (Amber)
    {"query": "tag:#handoff", "color": {"a": 1, "rgb": 1096065}},  # #10b981 (Emerald)
    {"query": "tag:#context", "color": {"a": 1, "rgb": 3718648}},  # #38bdf8 (Sky)
    {"query": "tag:#deprecated", "color": {"a": 1, "rgb": 15680580}},  # #ef4444 (Red)
    {"query": "tag:#compiled", "color": {"a": 1, "rgb": 6737151}},  # #66d9ff (Bright Cyan)
]


def generate_graph_config(color_groups: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Generates the content for `.obsidian/graph.json`."""
    return {
        "collapse-filter": False,
        "search": "",
        "threshold": -3.0,
        "colorGroups": color_groups or DEFAULT_COLOR_GROUPS,
        "lineSizeMultiplier": 1.2,
        "nodeSizeMultiplier": 1.15,
        "showArrow": True,
        "textFadeMultiplier": 0.0,
        "nodeSize": 1.1,
        "linkThickness": 1.2,
        "linkDistance": 180,
        "scale": 1.0,
        "close": True,
    }


def generate_app_config() -> Dict[str, Any]:
    """Generates the content for `.obsidian/app.json`."""
    return {
        "useMarkdownLinks": False,  # Enable [[wikilinks]] as native
        "livePreview": True,
        "showLineNumber": True,
        "foldHeading": True,
        "foldIndent": True,
        "autoPairMarkdown": True,
    }


def generate_core_plugins_config() -> List[str]:
    """Generates the enabled core plugins list for Obsidian."""
    return [
        "file-explorer",
        "global-search",
        "switcher",
        "graph",
        "backlink",
        "outgoing-link",
        "tag-pane",
        "page-preview",
        "command-palette",
        "markdown-importer",
        "word-count",
        "outline",
    ]


def init_obsidian_vault(
    workspace_dir: str = ".",
    scope: str = "local",
    override_existing: bool = False,
) -> Dict[str, Any]:
    """
    Initializes or synchronizes the `.obsidian` configuration inside the Memory Vault.
    This enables Obsidian to immediately visualize BoteX memories as an interactive knowledge graph.
    """
    try:
        search_dirs = _resolve_scope_dirs(workspace_dir, scope)
        initialized_vaults: List[str] = []

        for mem_dir, sc in search_dirs:
            if sc == "local":
                _ensure_workspace_memory_dir(Path(workspace_dir).resolve())
            else:
                _ensure_global_memory_dir(mem_dir)

            obs_dir = mem_dir / ".obsidian"
            obs_dir.mkdir(parents=True, exist_ok=True)

            graph_file = obs_dir / "graph.json"
            if override_existing or not graph_file.exists():
                graph_file.write_text(
                    json.dumps(generate_graph_config(), indent=2), encoding="utf-8"
                )

            app_file = obs_dir / "app.json"
            if override_existing or not app_file.exists():
                app_file.write_text(json.dumps(generate_app_config(), indent=2), encoding="utf-8")

            plugins_file = obs_dir / "core-plugins.json"
            if override_existing or not plugins_file.exists():
                plugins_file.write_text(
                    json.dumps(generate_core_plugins_config(), indent=2), encoding="utf-8"
                )

            initialized_vaults.append(str(mem_dir))

        return {
            "ok": True,
            "scope": scope,
            "vaults": initialized_vaults,
            "count": len(initialized_vaults),
        }
    except Exception as exc:
        return {"ok": False, "error": f"Failed to initialize Obsidian vault: {exc}"}


def export_graph_index_markdown(
    workspace_dir: str = ".",
    scope: str = "local",
) -> Dict[str, Any]:
    """
    Generates a dynamic `_GRAPH_INDEX.md` note inside the vault summarizing:
    1. Total nodes, edges and graph health.
    2. Central hub notes (most referenced/backlinked).
    3. Categorized memory entries with Obsidian wikilinks.
    """
    try:
        search_dirs = _resolve_scope_dirs(workspace_dir, scope)
        results: List[Dict[str, Any]] = []

        for mem_dir, sc in search_dirs:
            if not mem_dir.exists() or not mem_dir.is_dir():
                continue

            sync_memory_index(mem_dir)
            conn = _get_index_db(mem_dir)
            try:
                # 1. Total counts
                meta_rows = conn.execute(
                    "SELECT id, title, kind, tags, status, compiled_truth, timestamp FROM memories_meta ORDER BY timestamp DESC;"
                ).fetchall()
                if not meta_rows:
                    continue

                # 2. Hub notes by in-degree (incoming wikilinks)
                hub_rows = conn.execute(
                    """
                    SELECT target_id, COUNT(*) as in_count
                    FROM memories_edges
                    GROUP BY target_id
                    ORDER BY in_count DESC
                    LIMIT 10;
                    """
                ).fetchall()

                # 3. Health report
                health = find_graph_health(workspace_dir=str(mem_dir.parent.parent), scope=sc)

                # Format markdown
                md_lines = [
                    "---",
                    "id: _graph_index",
                    "title: Knowledge Graph Index",
                    "kind: context",
                    "tags: [index, graph, pkm, obsidian]",
                    "status: active",
                    "compiled_truth: true",
                    "---",
                    "",
                    "# 🧠 BoteX Knowledge Graph & PKM Vault Index",
                    "",
                    f"> **Scope:** `{sc}` | **Nodes:** {len(meta_rows)} | **Healthy:** {'✅ Yes' if health.get('is_healthy') else '⚠️ Issues detected'}",
                    "",
                    "## 🌟 Central Knowledge Hubs (Most Linked)",
                    "",
                ]
                if hub_rows:
                    for h in hub_rows:
                        tid = h["target_id"]
                        cnt = h["in_count"]
                        md_lines.append(f"- [[{tid}]] — referenced by **{cnt}** notes")
                else:
                    md_lines.append("_No cross-linked hubs established yet._")

                md_lines.extend(["", "## 📚 Memory Entries by Kind", ""])
                by_kind: Dict[str, List[sqlite3.Row]] = {}
                for r in meta_rows:
                    k = r["kind"] or "context"
                    by_kind.setdefault(k, []).append(r)

                for k, rows in sorted(by_kind.items()):
                    md_lines.append(f"### {k.capitalize()} ({len(rows)})")
                    for r in rows[:15]:
                        tid = r["id"]
                        title = r["title"] or tid
                        tag_str = f" `#{r['tags']}`" if r["tags"] else ""
                        truth_badge = " ⭐" if r["compiled_truth"] else ""
                        md_lines.append(f"- [[{tid}]] - {title}{truth_badge}{tag_str}")
                    if len(rows) > 15:
                        md_lines.append(f"- _... and {len(rows) - 15} more {k} entries_")
                    md_lines.append("")

                index_file = mem_dir / "_GRAPH_INDEX.md"
                clean_md = sanitize_for_storage("\n".join(md_lines))
                index_file.write_text(clean_md, encoding="utf-8")
                results.append({"path": str(index_file), "scope": sc, "nodes": len(meta_rows)})
            finally:
                conn.close()

        return {"ok": True, "generated": results}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to generate graph index: {exc}"}
