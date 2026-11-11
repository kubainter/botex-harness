# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Idle-time background maintenance jobs (maintenance.timer / curator layer).

Request-path work should only ever *read* warm state; this module moves the
network-bound refreshes and filesystem sweeps that today happen lazily — at
request time or once at startup — into deterministic idle windows:

- ``warm_provider_metadata``: model catalogs + pricing guardrail + benchmarks.
  Without it the first request after TTL expiry pays the network wait or
  silently degrades to stale data (the 2026-11-10 chat 403/400 incident).
- ``sweep_empty_chat_sessions``: ghost session files (empty history on disk)
  resurface in the PWA list and can resurrect dead payloads after a reload.
- ``repair_frontmatter_ids``: non-canonical or missing frontmatter IDs are
  rewritten to match file stem, preventing silent drops from filtered searches.

Every failure here is logged and swallowed — a broken idle job must never
touch the user-facing path.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Safety window: never delete a session file that was touched this recently —
# guards against racing a live server that is mid-write.
_SWEEP_MIN_AGE_S = 600.0

_OPENROUTER_PROVIDERS = ("openrouter", "openrouter-free")


def _load_config() -> Dict[str, Any]:
    from botex.server.config import load_config

    return load_config()


def _provider_api_key(prov_cfg: Dict[str, Any]) -> str:
    key_env = str(prov_cfg.get("api_key_env") or "").strip()
    if not key_env:
        return ""
    return os.environ.get(key_env, "").strip()


def warm_provider_metadata(workspace_dir: str = ".") -> Dict[str, Any]:
    """Refresh provider metadata caches: model catalog, pricing, benchmarks.

    Respects each cache's TTL — an already-fresh cache is a cheap no-op, an
    expired one is refetched here instead of during a user's chat turn.
    """
    results: Dict[str, Any] = {"workspace": str(workspace_dir)}
    try:
        cfg = _load_config()
    except Exception as exc:
        logger.warning("provider metadata warmup: config load failed: %s", type(exc).__name__)
        return results

    providers_cfg = cfg.get("providers") or {}
    for name in _OPENROUTER_PROVIDERS:
        prov_cfg = providers_cfg.get(name) or {}
        base_url = str(prov_cfg.get("base_url") or "").strip()
        if not base_url:
            continue
        try:
            from botex.providers.catalog import ModelCatalog

            models = ModelCatalog(provider_name=name, provider_cfg=prov_cfg).load()
            results[f"catalog:{name}"] = len(models or {})
        except Exception as exc:
            logger.warning(
                "provider metadata warmup: catalog %s failed: %s", name, type(exc).__name__
            )

        try:
            from botex.providers.pricing import load_catalog as load_pricing_catalog

            pricing = load_pricing_catalog(base_url, api_key=_provider_api_key(prov_cfg))
            results[f"pricing:{name}"] = sum(len(c) for c in (pricing or {}).values())
        except Exception as exc:
            logger.warning(
                "provider metadata warmup: pricing %s failed: %s", name, type(exc).__name__
            )

    try:
        from botex.providers.benchmarks import fetch_openrouter_benchmarks

        data = fetch_openrouter_benchmarks()
        if isinstance(data, (list, dict)):
            results["benchmarks"] = len(data)
        else:
            results["benchmarks"] = bool(data)
    except Exception as exc:
        logger.warning("provider metadata warmup: benchmarks failed: %s", type(exc).__name__)

    return results


def sweep_empty_chat_sessions(workspace_dir: str = ".") -> int:
    """Delete persisted chat-session files whose message history is empty.

    Only files with a genuine BoteX session shape (``session_id`` present,
    ``messages == []``) older than ``_SWEEP_MIN_AGE_S`` are removed; anything
    unparsable or non-conforming is left untouched.
    """
    try:
        from botex.server.config import get_chat_sessions_dir

        sessions_dir = Path(get_chat_sessions_dir(workspace_dir))
    except Exception as exc:
        logger.warning("ghost session sweep: sessions dir resolve failed: %s", type(exc).__name__)
        return 0
    if not sessions_dir.is_dir():
        return 0

    removed = 0
    now = time.time()
    for file_path in sessions_dir.glob("*.json"):
        try:
            if not file_path.is_file():
                continue
            mtime_seen = file_path.stat().st_mtime
            if now - mtime_seen < _SWEEP_MIN_AGE_S:
                continue
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "session_id" not in data:
            continue
        if data.get("messages") != []:
            continue
        try:
            # Re-stat immediately before unlink: a concurrent server write
            # (fresh mtime) means the session is alive — skip, never race it.
            if file_path.stat().st_mtime != mtime_seen:
                continue
            file_path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _rewrite_id_frontmatter(raw_text: str, clean_stem: str) -> str:
    """Rewrite only the id: line of YAML frontmatter to match the file stem."""
    if raw_text.startswith("---"):
        parts = raw_text.split("---", 2)
        if len(parts) >= 3:
            yaml_block = parts[1]
            if re.search(r"^id\s*:.*$", yaml_block, flags=re.MULTILINE):
                new_yaml = re.sub(
                    r"^id\s*:.*$",
                    f"id: {clean_stem}",
                    yaml_block,
                    count=1,
                    flags=re.MULTILINE,
                )
            else:
                nl = "\r\n" if "\r\n" in yaml_block else "\n"
                if yaml_block.startswith(nl):
                    new_yaml = f"{nl}id: {clean_stem}{yaml_block}"
                else:
                    new_yaml = f"{nl}id: {clean_stem}{nl}{yaml_block}"
            return f"---{new_yaml}---{parts[2]}"

    nl = "\r\n" if "\r\n" in raw_text else "\n"
    return f"---{nl}id: {clean_stem}{nl}---{nl}{nl}{raw_text}"


def repair_frontmatter_ids(workspace_dir: str = ".") -> Dict[str, int]:
    """Canonicalize frontmatter IDs to match file stem across memory directories.

    Scans workspace, global, and swarm memory roots. Rewrites only invalid, missing,
    or mismatched id: lines atomically to avoid search drops while leaving note
    content and wikilinks untouched.
    """
    results: Dict[str, int] = {"scanned": 0, "repaired": 0, "skipped": 0}
    try:
        from botex.memory.memory import (
            _get_index_db,
            _index_memory_file,
            _parse_frontmatter,
            _resolve_scope_dirs,
            _sanitize_id,
            sync_memory_index,
        )
    except Exception as exc:
        logger.warning("repair frontmatter ids: memory imports failed: %s", type(exc).__name__)
        return results

    try:
        scope_dirs = _resolve_scope_dirs(workspace_dir, "all")
    except Exception as exc:
        logger.warning("repair frontmatter ids: directory resolve failed: %s", type(exc).__name__)
        return results

    seen_dirs: set[Path] = set()
    for mem_dir, _ in scope_dirs:
        try:
            resolved_dir = mem_dir.resolve()
        except Exception:
            continue
        if resolved_dir in seen_dirs or not resolved_dir.is_dir():
            continue
        seen_dirs.add(resolved_dir)

        dir_repaired = 0
        for file_p in sorted(resolved_dir.glob("*.md")):
            if not file_p.is_file() or file_p.name.startswith("_"):
                continue

            results["scanned"] += 1
            try:
                clean_stem = _sanitize_id(file_p.stem)
            except Exception as exc:
                logger.warning(
                    "repair frontmatter ids: file stem validation failed: %s",
                    type(exc).__name__,
                )
                results["skipped"] += 1
                continue

            try:
                raw_text = file_p.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning(
                    "repair frontmatter ids: file read failed: %s",
                    type(exc).__name__,
                )
                results["skipped"] += 1
                continue

            try:
                meta, _ = _parse_frontmatter(raw_text)
            except Exception as exc:
                logger.warning(
                    "repair frontmatter ids: frontmatter parse failed: %s",
                    type(exc).__name__,
                )
                meta = {}

            fid = meta.get("id")
            needs_repair = False
            if fid is None or not str(fid).strip():
                needs_repair = True
            else:
                try:
                    clean_fid = _sanitize_id(str(fid))
                    if clean_fid != clean_stem:
                        needs_repair = True
                except Exception:
                    needs_repair = True

            if not needs_repair:
                continue

            try:
                new_text = _rewrite_id_frontmatter(raw_text, clean_stem)
                tmp_path = file_p.with_name(f"{file_p.name}.{os.getpid()}.tmp")
                try:
                    tmp_path.write_text(new_text, encoding="utf-8")
                    os.replace(str(tmp_path), str(file_p))
                finally:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass

                results["repaired"] += 1
                dir_repaired += 1

                try:
                    conn = _get_index_db(resolved_dir)
                    try:
                        _index_memory_file(resolved_dir, conn, file_p)
                    finally:
                        conn.close()
                except Exception as exc:
                    logger.warning(
                        "repair frontmatter ids: index update failed: %s",
                        type(exc).__name__,
                    )
            except Exception as exc:
                logger.warning(
                    "repair frontmatter ids: file rewrite failed: %s",
                    type(exc).__name__,
                )
                results["skipped"] += 1
                continue

        if dir_repaired > 0:
            try:
                sync_memory_index(resolved_dir)
            except Exception as exc:
                logger.warning(
                    "repair frontmatter ids: index sync failed: %s",
                    type(exc).__name__,
                )

    return results


_LEDGER_KEEP_DAYS = 90


def rotate_analytics_ledger(workspace_dir: str = ".") -> Dict[str, int]:
    """Move ledger rows older than the retention window into per-month
    ``analytics-archive/YYYY-MM.jsonl.gz`` rollups.

    Live ``.agent_analytics.jsonl`` stays small for fast ``load_analytics``;
    archived rows remain fully readable (load_analytics merges all sources),
    so ``botex stats`` keeps its historical horizon. Legacy ``.json`` data
    is folded into the live segment first.
    """
    results: Dict[str, int] = {"archived": 0, "kept": 0, "migrated": 0}
    try:
        from botex.providers.analytics import (
            _cfg_analytics_file,
            _ledger_archive_dir,
            _ledger_jsonl,
            _read_jsonl,
        )
    except Exception as exc:
        logger.warning("analytics rotation: imports failed: %s", type(exc).__name__)
        return results

    live = _ledger_jsonl()
    legacy = _cfg_analytics_file()
    cutoff = datetime.now(timezone.utc) - timedelta(days=_LEDGER_KEEP_DAYS)

    rows: list = []
    if legacy.exists() and legacy != live:
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(data, list):
                rows.extend(r for r in data if isinstance(r, dict))
                results["migrated"] = len(rows)
                legacy.unlink()
        except (OSError, ValueError) as exc:
            logger.warning("analytics rotation: legacy migrate failed: %s", type(exc).__name__)
    rows.extend(_read_jsonl(live))
    if not rows:
        return results

    def _month_of(rec: Dict[str, Any]) -> str:
        try:
            return str(rec.get("timestamp") or "")[:7] or "unknown"
        except Exception:
            return "unknown"

    def _ts_of(rec: Dict[str, Any]):
        try:
            ts = str(rec.get("timestamp") or "")
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None

    keep: list = []
    by_month: Dict[str, list] = {}
    for rec in rows:
        ts = _ts_of(rec)
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts is not None and ts < cutoff:
            by_month.setdefault(_month_of(rec), []).append(rec)
        else:
            keep.append(rec)

    if not by_month:
        results["kept"] = len(keep)
        # Still rewrite live if legacy rows were folded in.
        if results["migrated"] or not live.exists():
            _rewrite_ledger(live, keep)
        return results

    archive = _ledger_archive_dir()
    try:
        archive.mkdir(parents=True, exist_ok=True)
        for month, recs in by_month.items():
            gz_path = archive / f"{month}.jsonl.gz"
            with gzip.open(gz_path, "at", encoding="utf-8") as fh:
                for rec in recs:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            results["archived"] += len(recs)
    except (OSError, ValueError) as exc:
        logger.warning("analytics rotation: archive write failed: %s", type(exc).__name__)
        return results

    _rewrite_ledger(live, keep)
    results["kept"] = len(keep)
    return results


def _rewrite_ledger(live: Path, rows: list) -> None:
    tmp = live.with_suffix(live.suffix + ".tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    os.replace(tmp, live)


_APPLY_LINK_MIN_RATIO = 0.9


def repair_dead_links(workspace_dir: str = ".", apply: bool = False) -> Dict[str, Any]:
    """Reconcile broken wikilinks across memory vaults.

    Report mode (default): returns deterministic suggestions mapping each
    dead [[target]] to the nearest living note id — rendered by the
    curator beacon for human review.

    Apply mode: rewrites only high-confidence links (difflib ratio >=
    0.9, e.g. redacted ids repaired to stem), atomically per file, then
    re-indexes. Anything ambiguous stays a suggestion — never guess.
    """
    results: Dict[str, Any] = {"broken": 0, "suggested": 0, "applied": 0, "skipped": 0}
    try:
        import difflib

        from botex.memory.memory import (
            _get_index_db,
            _index_memory_file,
            _resolve_scope_dirs,
            find_graph_health,
        )
        from botex.ops.curator.curator_service import _compute_dead_link_repairs
    except Exception as exc:
        logger.warning("dead-link repair: imports failed: %s", type(exc).__name__)
        return results

    try:
        health = find_graph_health(workspace_dir=str(workspace_dir), scope="all")
    except Exception as exc:
        logger.warning("dead-link repair: graph health failed: %s", type(exc).__name__)
        return results
    broken = health.get("broken_links") or []
    results["broken"] = len(broken)
    if not broken:
        return results

    try:
        scope_dirs = _resolve_scope_dirs(str(workspace_dir), "all")
    except Exception:
        scope_dirs = []
    all_ids: list = []
    file_by_id: Dict[str, Path] = {}
    for mem_dir, _ in scope_dirs:
        if not mem_dir.is_dir():
            continue
        for f in mem_dir.glob("*.md"):
            if f.is_file() and not f.name.startswith("_"):
                all_ids.append(f.stem)
                file_by_id[f.stem] = f

    repairs = _compute_dead_link_repairs(broken, [{"id": i} for i in all_ids])
    results["suggested"] = len(repairs)
    if not apply or not repairs:
        results["suggestions"] = repairs
        return results

    changed_dirs: set = set()
    for rep in repairs:
        src_id, dead, suggested = (
            rep["source_id"],
            rep["dead_target"],
            rep["suggested_target"],
        )
        if difflib.SequenceMatcher(None, dead, suggested).ratio() < _APPLY_LINK_MIN_RATIO:
            results["skipped"] += 1
            continue
        src_file = file_by_id.get(src_id)
        if src_file is None:
            results["skipped"] += 1
            continue
        try:
            text = src_file.read_text(encoding="utf-8")
            new_text = text.replace(f"[[{dead}]]", f"[[{suggested}]]")
            if new_text == text:
                results["skipped"] += 1
                continue
            tmp = src_file.with_name(f"{src_file.name}.{os.getpid()}.tmp")
            try:
                tmp.write_text(new_text, encoding="utf-8")
                os.replace(str(tmp), str(src_file))
            finally:
                if tmp.exists():
                    tmp.unlink()
            results["applied"] += 1
            changed_dirs.add(src_file.parent)
        except Exception as exc:
            logger.warning("dead-link apply failed: %s", type(exc).__name__)
            results["skipped"] += 1

    for mem_dir in changed_dirs:
        try:
            conn = _get_index_db(mem_dir)
            try:
                for f in mem_dir.glob("*.md"):
                    if f.is_file() and not f.name.startswith("_"):
                        _index_memory_file(mem_dir, conn, f)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("dead-link reindex failed: %s", type(exc).__name__)
    return results
