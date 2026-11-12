# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Memory Vault — Project Metadata Backfill & Migration
==========================================================
Idempotent migration utility that populates the ``project: <slug>`` YAML
frontmatter field across existing Markdown memory notes and synchronizes
the SQLite ``index.db`` cache.

Uses a sentinel file (``.project_backfill_v2.done``) inside each memory root
so subsequent startups exit in <1ms without scanning disk.
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from botex.memory.memory import (
    _normalize_project_slug,
    _parse_frontmatter,
    _resolve_scope_dirs,
    _serialize_frontmatter,
    get_global_memory_dir,
    sync_memory_index,
)
from botex.tools.security import sanitize_for_storage

logger = logging.getLogger(__name__)

SENTINEL_FILENAME = ".project_backfill_v2.done"

# Canonical renames applied to previously assigned project slugs.
_LEGACY_PROJECT_SLUGS = {"swarm-skills": "swarm"}


def infer_project_from_note(meta: Dict[str, Any], body: str, stem: str) -> str:
    """Deterministically infer a canonical project slug from note metadata and content."""
    raw_tags = meta.get("tags") or []
    if isinstance(raw_tags, str):
        tags = [t.strip().lower() for t in raw_tags.strip("[]").split(",") if t.strip()]
    else:
        tags = [str(t).strip().lower() for t in raw_tags if str(t).strip()]

    title_low = str(meta.get("title") or "").lower()
    stem_low = stem.lower()
    body_low = body[:1500].lower()
    combined_meta = f"{stem_low} {title_low} {' '.join(tags)}"

    # 1. Explicit project:<slug> tag convention if present
    for t in tags:
        if t.startswith("project:") and len(t) > 8:
            slug = _normalize_project_slug(t[8:])
            if slug and slug != "_unassigned":
                return _LEGACY_PROJECT_SLUGS.get(slug, slug)

    # 2. Witcher 3 / REDengine / TW3SE modding
    w3_markers = (
        "witcher3",
        "w3",
        "w2scene",
        "tw3se",
        "wolvenkit",
        "cr2w",
        "mcme",
        "ar-scenes",
        "blob0",
    )
    if any(m in tags or m in combined_meta for m in w3_markers):
        return "witcher3"

    # 3. Unreal Engine / UE4SS / Dawnwalker / Alkimya
    ue_markers = ("ue4ss", "dawnwalker", "tbod", "alkimya", "unreal")
    if any(m in tags or m in combined_meta for m in ue_markers):
        return "ue4ss"

    # 4. Prompt Improver
    if "prompt-improver" in combined_meta or "prompt_improver" in combined_meta:
        return "prompt-improver"

    # 5. Media / Video Generation
    media_markers = ("video-gen", "veo", "moneyprinter", "replicate", "kling")
    if any(m in tags or m in combined_meta for m in media_markers):
        return "media-gen"

    # 6. Swarm / Multi-Agent Skills & Ecosystem
    swarm_markers = (
        "swarm",
        "arena:",
        "trio:",
        "temp-handoff",
        "knowledge-synthesis",
        "swarm-brainstorm",
        "release-readiness",
        "step-beyond",
        "trio-worker",
        "zcode",
        "followup-routing",
        "arena-contract",
    )
    if any(m in tags or m in combined_meta for m in swarm_markers) and "botex" not in tags:
        return "swarm"

    # 7. BoteX Harness / Cloud / PWA
    botex_markers = (
        "botex",
        "pwa",
        "curator",
        "recall",
        "meta-harness",
        "task-receipt",
        "context_guard",
        "openrouter",
        "bielik",
        "oracle-vps",
        "skill-vault",
    )
    if any(m in tags or m in combined_meta or m in body_low for m in botex_markers):
        return "botex"

    return "botex"


def migrate_memory_dir(
    mem_dir: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Backfill missing ``project`` frontmatter in ``mem_dir`` and sync ``index.db``."""
    if not mem_dir.exists() or not mem_dir.is_dir():
        return {"ok": True, "skipped": True, "updated": 0, "projects": {}}

    sentinel = mem_dir / SENTINEL_FILENAME
    if sentinel.exists() and not force and not dry_run:
        return {"ok": True, "skipped": True, "updated": 0, "projects": {}}

    updated_count = 0
    project_counts: Dict[str, int] = {}

    md_files = sorted(f for f in mem_dir.glob("*.md") if f.is_file() and not f.name.startswith("_"))
    for file_p in md_files:
        try:
            raw_text = file_p.read_text(encoding="utf-8", errors="replace")
            meta, body = _parse_frontmatter(raw_text)
            if not meta and not raw_text.lstrip().startswith("---"):
                continue

            existing_proj = _normalize_project_slug(meta.get("project"))
            remap = _LEGACY_PROJECT_SLUGS.get(existing_proj)
            if existing_proj and existing_proj != "_unassigned" and not force and remap is None:
                project_counts[existing_proj] = project_counts.get(existing_proj, 0) + 1
                continue

            inferred = remap or infer_project_from_note(meta, body, file_p.stem)
            project_counts[inferred] = project_counts.get(inferred, 0) + 1

            if existing_proj == inferred:
                continue

            meta["project"] = inferred
            if not dry_run:
                full_text = sanitize_for_storage(_serialize_frontmatter(meta, body))
                file_path_str = str(file_p)
                Path(file_path_str).write_text(full_text, encoding="utf-8")
            updated_count += 1
        except Exception as exc:
            logger.debug("Skipping note during project migration: %s", type(exc).__name__)

    if not dry_run:
        try:
            sync_memory_index(mem_dir)
            sentinel.write_text("ok\n", encoding="utf-8")
        except Exception as exc:
            logger.debug("Failed to write migration sentinel: %s", type(exc).__name__)

    return {
        "ok": True,
        "skipped": False,
        "updated": updated_count,
        "total": len(md_files),
        "projects": project_counts,
    }


def consolidate_mangled_windows_dirs(
    base_dir: Path,
    global_mem_dir: Path,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Move notes stranded in POSIX-mangled Windows workspace dirs to the global vault.

    On a POSIX gateway host a Windows ``workspace_dir`` (``C:\\...``) lands as a
    single directory name under the botex root, e.g.
    ``/opt/botex/C:\\Users\\kubai\\.ai\\harness``. Notes under its
    ``.botex/memory`` are unreachable from every scope; relocate them into the
    global vault with ``scope: global`` frontmatter.
    """
    result: Dict[str, Any] = {
        "ok": True,
        "scanned_dirs": 0,
        "moved": 0,
        "skipped_newer": 0,
        "moved_ids": [],
    }
    base = Path(base_dir)
    gdir = Path(global_mem_dir)
    if not base.is_dir():
        return result

    try:
        entries = sorted(base.iterdir())
    except OSError:
        return result

    for entry in entries:
        if not entry.is_dir() or not re.match(r"^[A-Za-z]:\\", entry.name):
            continue
        result["scanned_dirs"] += 1
        mem_dir = entry / ".botex" / "memory"
        if not mem_dir.is_dir():
            continue
        for src in sorted(mem_dir.glob("*.md")):
            if not src.is_file() or src.name.startswith("_"):
                continue
            dest = gdir / src.name
            try:
                if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
                    result["skipped_newer"] += 1
                    continue
            except OSError:
                result["skipped_newer"] += 1
                continue
            try:
                raw = src.read_text(encoding="utf-8", errors="replace")
                meta, body = _parse_frontmatter(raw)
                meta["scope"] = "global"
                existing = _normalize_project_slug(meta.get("project"))
                remapped = _LEGACY_PROJECT_SLUGS.get(existing)
                if remapped:
                    meta["project"] = remapped
                elif existing and existing != "_unassigned":
                    meta["project"] = existing
                else:
                    meta["project"] = infer_project_from_note(meta, body, src.stem)
                if not dry_run:
                    gdir.mkdir(parents=True, exist_ok=True)
                    dest.write_text(
                        sanitize_for_storage(_serialize_frontmatter(meta, body)),
                        encoding="utf-8",
                    )
                    src.unlink()
                result["moved"] += 1
                result["moved_ids"].append(src.stem)
            except Exception as exc:
                logger.debug(
                    "Mangled-dir consolidation skipped %s: %s", src.name, type(exc).__name__
                )

    if not dry_run and result["moved"]:
        try:
            gdir.mkdir(parents=True, exist_ok=True)
            sync_memory_index(gdir)
        except Exception as exc:
            logger.debug("Post-consolidation index sync failed: %s", type(exc).__name__)
    return result


def ensure_projects_migrated(
    workspace_dir: str = ".",
    *,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Run project metadata backfill across local and global memory directories."""
    dirs = _resolve_scope_dirs(workspace_dir, "all")
    summary: Dict[str, Any] = {"ok": True, "roots": [], "total_updated": 0}
    summary["mangled_consolidated"] = 0
    seen_dirs: set[str] = set()

    # Rescue notes stranded in POSIX-mangled Windows workspace dirs (e.g.
    # /opt/botex/C:\Users\...) before the backfill pass so they join the
    # normal migration pipeline. Best-effort: never blocks startup.
    try:
        gdir = get_global_memory_dir()
        seen_bases: set[str] = set()
        for base in (Path(workspace_dir).resolve().parent, Path("/opt/botex")):
            try:
                base_key = str(base.resolve())
            except Exception:
                base_key = str(base)
            if base_key in seen_bases or not base.is_dir():
                continue
            seen_bases.add(base_key)
            cons = consolidate_mangled_windows_dirs(base, gdir, dry_run=dry_run)
            summary["mangled_consolidated"] += int(cons.get("moved") or 0)
    except Exception as exc:
        logger.debug("Mangled-dir consolidation notice: %s", type(exc).__name__)

    for mem_dir, scope_label in dirs:
        if scope_label == "swarm":
            continue
        key = str(mem_dir.resolve())
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        res = migrate_memory_dir(mem_dir, dry_run=dry_run, force=force)
        summary["roots"].append({"scope": scope_label, "path": key, **res})
        summary["total_updated"] += int(res.get("updated") or 0)

    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill project metadata in BoteX Memory Vault.")
    parser.add_argument("--workspace", default=".", help="Workspace directory (default: .)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview classification without writing"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-evaluate all notes ignoring sentinel"
    )
    args = parser.parse_args(argv)

    res = ensure_projects_migrated(args.workspace, dry_run=args.dry_run, force=args.force)
    for root in res.get("roots", []):
        print(
            f"[{root['scope']}] {root['path']}: "
            f"updated={root.get('updated', 0)}, skipped={root.get('skipped', False)}, "
            f"projects={root.get('projects', {})}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
