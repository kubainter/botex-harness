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
import math
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


def _dedup_append(target: list, seen: set, incoming: list) -> None:
    """Append incoming records, deduplicating only rows with both task_id and timestamp."""
    for r in incoming:
        if not isinstance(r, dict):
            continue
        tid = r.get("task_id")
        ts = r.get("timestamp")
        if tid and ts:
            k = (tid, ts)
            if k in seen:
                continue
            seen.add(k)
        target.append(r)


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
    seen_keys: set = set()
    legacy_rows: list = []
    if legacy.exists() and legacy != live:
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(data, list):
                legacy_rows = [r for r in data if isinstance(r, dict)]
                _dedup_append(rows, seen_keys, legacy_rows)
                results["migrated"] = len(legacy_rows)
        except (OSError, ValueError) as exc:
            logger.warning("analytics rotation: legacy migrate failed: %s", type(exc).__name__)

    staged = live.with_suffix(live.suffix + ".rotating")
    staged2 = live.with_suffix(live.suffix + ".rotating2")
    staged_ok = False
    try:
        # Fold in any orphan .rotating segments left by a prior crash before staging live.
        for orphan in (staged, staged2):
            if orphan.exists():
                orphan_recs = _read_jsonl(orphan, strict=True)
                if orphan.stat().st_size > 0 and not orphan_recs:
                    logger.warning(
                        "analytics rotation: non-empty orphan %s yielded 0 rows", orphan.name
                    )
                    return results
                _dedup_append(rows, seen_keys, orphan_recs)
                staged_ok = True
        if live.exists():
            os.replace(live, staged)
            staged_ok = True
            staged_recs = _read_jsonl(staged, strict=True)
            if staged.exists() and staged.stat().st_size > 0 and not staged_recs:
                os.replace(staged, live)
                logger.warning("analytics rotation: staged read returned empty on non-empty ledger")
                return results
            _dedup_append(rows, seen_keys, staged_recs)
    except OSError as exc:
        logger.warning("analytics rotation: live stage failed: %s", type(exc).__name__)
        if staged_ok and staged.exists() and not live.exists():
            try:
                os.replace(staged, live)
            except OSError:
                pass
        return results

    try:
        tail = _read_jsonl(live, strict=True)
        _dedup_append(rows, seen_keys, tail)
    except OSError as exc:
        logger.warning("analytics rotation: tail read failed: %s", type(exc).__name__)
        if staged_ok:
            try:
                _rewrite_ledger(live, rows)
                if staged.exists():
                    staged.unlink()
            except OSError:
                pass
        return results

    if not rows:
        if staged_ok:
            for s in (staged, staged2):
                if s.exists():
                    try:
                        if not live.exists():
                            os.replace(s, live)
                        else:
                            s.unlink()
                    except OSError:
                        pass
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
    seen_keep: set = set()
    by_month: Dict[str, list] = {}
    for rec in rows:
        ts = _ts_of(rec)
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts is not None and ts < cutoff:
            by_month.setdefault(_month_of(rec), []).append(rec)
        else:
            _dedup_append(keep, seen_keep, [rec])

    archive = _ledger_archive_dir()
    try:
        if by_month:
            archive.mkdir(parents=True, exist_ok=True)
            for month, recs in by_month.items():
                gz_path = archive / f"{month}.jsonl.gz"
                with gzip.open(gz_path, "at", encoding="utf-8") as fh:
                    for rec in recs:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                results["archived"] += len(recs)
    except (OSError, ValueError) as exc:
        logger.warning("analytics rotation: archive write failed: %s", type(exc).__name__)
        if staged_ok:
            try:
                _rewrite_ledger(live, rows)
                for s in (staged, staged2):
                    if s.exists():
                        s.unlink()
            except OSError:
                pass
        return results

    # Stage+merge any appends that arrived while gzip archives were being written
    try:
        if live.exists():
            os.replace(live, staged2)
            late_tail = _read_jsonl(staged2, strict=True)
            _dedup_append(keep, seen_keep, late_tail)
            _dedup_append(rows, seen_keys, late_tail)
        _rewrite_ledger(live, keep)
    except OSError as exc:
        logger.warning("analytics rotation: live rewrite failed: %s", type(exc).__name__)
        if staged_ok:
            try:
                _rewrite_ledger(live, rows)
                for s in (staged, staged2):
                    if s.exists():
                        s.unlink()
            except OSError:
                pass
        return results

    for s in (staged, staged2):
        if s.exists():
            try:
                s.unlink()
            except OSError:
                pass
    if legacy_rows:
        try:
            legacy.unlink()
        except OSError:
            pass
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


def _replace_outside_fences(text: str, old: str, new: str) -> str:
    """Replace literal ``old`` with ``new`` only outside fenced code blocks."""
    parts = re.split(r"(```[\s\S]*?```)", text)
    for idx in range(0, len(parts), 2):
        parts[idx] = parts[idx].replace(old, new)
    return "".join(parts)


def _contains_outside_fences(text: str, needle: str) -> bool:
    parts = re.split(r"(```[\s\S]*?```)", text)
    return any(needle in parts[idx] for idx in range(0, len(parts), 2))


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
    files_by_id: Dict[str, list] = {}
    for mem_dir, _ in scope_dirs:
        if not mem_dir.is_dir():
            continue
        for f in mem_dir.glob("*.md"):
            if f.is_file() and not f.name.startswith("_"):
                all_ids.append(f.stem)
                files_by_id.setdefault(f.stem, []).append(f)

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
        raw_candidates = files_by_id.get(src_id) or []
        candidates: list = []
        for cand_f in raw_candidates:
            try:
                if _contains_outside_fences(cand_f.read_text(encoding="utf-8"), f"[[{dead}]]"):
                    candidates.append(cand_f)
            except OSError:
                continue
        if not candidates:
            results["skipped"] += 1
            continue
        for src_file in candidates:
            try:
                text = src_file.read_text(encoding="utf-8")
                new_text = _replace_outside_fences(text, f"[[{dead}]]", f"[[{suggested}]]")
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


# ============================================================================
# Catalog drift sync (extracted from root server.py — plan 062)
# ============================================================================


def catalog_drift_report_path() -> Path:
    """Single report-path source: rooted at the installation/config root,
    never at an arbitrary task workspace or process cwd. The fallback derives
    from this file's own location (ops -> botex -> installation root), which is
    deterministic for both repo checkouts and installed deployments."""
    try:
        from botex.server.config import PROJECT_ROOT

        return PROJECT_ROOT / ".botex" / "catalog_drift.json"
    except Exception:
        return Path(__file__).resolve().parents[2] / ".botex" / "catalog_drift.json"


def load_cached_catalogs(
    provider_names: list,
    providers_cfg: Dict[str, Any],
    cache_file: Path,
) -> Dict[str, Dict[str, Any]]:
    """Load per-provider catalogs from the shared cache file only — never network."""
    from botex.providers.catalog import ModelCatalog, ModelMetadata

    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    except Exception:
        raw = {}
    entries = raw.get("catalogs") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        entries = {}

    catalogs: Dict[str, Dict[str, Any]] = {}
    for name in provider_names:
        models: Dict[str, Any] = {}
        try:
            pcfg = providers_cfg.get(name) if isinstance(providers_cfg, dict) else None
            cat = ModelCatalog(
                provider_name=name,
                provider_cfg=pcfg if isinstance(pcfg, dict) else None,
                cache_file=cache_file,
            )
            entry = entries.get(cat.cache_key)
            raw_cat = (
                (entry.get("catalog") or entry.get("models")) if isinstance(entry, dict) else None
            )
            if isinstance(raw_cat, dict):
                models = {
                    mid: ModelMetadata.from_dict(d if isinstance(d, dict) else {}, default_id=mid)
                    for mid, d in raw_cat.items()
                    if isinstance(mid, str)
                }
        except Exception:
            models = {}
        catalogs[name] = models
    return catalogs


def count_provider_evidence(sync_results: Any) -> tuple:
    """(ok_count, error_count) over per-provider sync result entries.

    Shared by the freshness gate and the health reader so positive-evidence
    semantics cannot drift apart."""
    ok_count = 0
    error_count = 0
    if isinstance(sync_results, dict):
        for entry in sync_results.values():
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if status == "ok":
                ok_count += 1
            elif status == "error":
                error_count += 1
    return ok_count, error_count


def run_catalog_drift_sync(
    cfg: Dict[str, Any],
    *,
    force: bool = False,
    report_path: "Path | None" = None,
) -> Dict[str, Any]:
    """Live catalog sync + config drift detection. Never raises.

    Refreshes the shared provider-model cache (one GET /models per provider),
    diffs configured model ids against the freshly loaded cached catalogs,
    persists the drift report atomically, and emits count-only stderr lines.
    The static config is never written.

    Frequency contract: a fresh previous report skips the network only when it
    carries positive evidence (at least one provider ``ok``). A fresh but
    all-error report must not suppress retry for 12 hours; ``force`` bypasses
    the gate outright. A sync-level exception still writes countable
    per-provider error evidence so health never reads a failed run as a clean
    empty report.
    """
    try:
        return _run_catalog_drift_sync_impl(cfg, force=force, report_path=report_path)
    except Exception:
        # Absolute never-raises: even import/config catastrophes surface as a
        # compact countable status, never as an exception into the curator tick.
        try:
            import sys

            sys.stderr.write("[BoteX] catalog drift sync failed\n")
        except Exception:
            pass
        return {"status": "error", "providers_ok": 0, "providers_error": 0}


def _run_catalog_drift_sync_impl(
    cfg: Dict[str, Any],
    *,
    force: bool = False,
    report_path: "Path | None" = None,
) -> Dict[str, Any]:
    import sys

    from botex.providers import catalog as catalog_mod
    from botex.providers.providers import ProviderRegistry

    sync_cfg = cfg.get("catalog_sync") if isinstance(cfg, dict) else None
    if not isinstance(sync_cfg, dict):
        sync_cfg = {}
    if not bool(sync_cfg.get("enabled", True)):
        return {"status": "disabled"}

    default_providers = ["google", "groq", "nvidia", "cloudflare", "openrouter", "openrouter-free"]
    raw_providers = sync_cfg.get("providers")
    if raw_providers is None or not isinstance(raw_providers, (list, tuple)):
        provider_names = list(default_providers)
    else:
        provider_names = [str(p).strip() for p in raw_providers if str(p).strip()]
        if not provider_names:
            # Explicit empty configuration means no providers — distinguishable
            # no-data, never silently reinterpreted as the default set.
            return {"status": "no_providers", "providers_ok": 0, "providers_error": 0}
    try:
        timeout = float(sync_cfg.get("timeout_s", 10.0) or 10.0)
    except (ValueError, TypeError):
        timeout = 10.0
    raw_interval = sync_cfg.get("interval_s")
    try:
        interval_s = 43200.0 if raw_interval is None else float(raw_interval)
    except (ValueError, TypeError):
        interval_s = 43200.0
    if not math.isfinite(interval_s) or interval_s < 0.0:
        interval_s = 43200.0

    resolved_report = Path(report_path) if report_path is not None else catalog_drift_report_path()

    # Frequency gate: a live sync is a per-provider network call and must stay
    # a ~daily maintenance job. Positive-evidence requirement: only a report
    # with at least one successful provider counts as a successful fresh sync.
    if interval_s > 0.0 and not force:
        try:
            if resolved_report.exists():
                prev = json.loads(resolved_report.read_text(encoding="utf-8"))
                prev_ts = float(prev.get("generated_at") or 0.0) if isinstance(prev, dict) else 0.0
                age = time.time() - prev_ts
                # Negative age (clock skew / future timestamp) or non-finite ts
                # must not suppress syncs indefinitely — re-sync rewrites the ts.
                if math.isfinite(prev_ts) and prev_ts > 0.0 and 0.0 <= age < interval_s:
                    prev_ok, _prev_err = count_provider_evidence(
                        prev.get("sync") if isinstance(prev, dict) else None
                    )
                    if prev_ok >= 1:
                        return {
                            "status": "skipped_fresh",
                            "age_s": round(age, 1),
                            "sync": prev.get("sync") if isinstance(prev, dict) else {},
                            "summary": prev.get("summary") if isinstance(prev, dict) else {},
                        }
        except Exception:
            pass

    cat_cfg = cfg.get("catalog") if isinstance(cfg, dict) else None
    cat_cache = cat_cfg.get("cache_file") if isinstance(cat_cfg, dict) else None
    if isinstance(cat_cache, str) and cat_cache.strip():
        cache_file = Path(cat_cache.strip())
        if not cache_file.is_absolute():
            try:
                from botex.server.config import PROJECT_ROOT

                cache_file = PROJECT_ROOT / cache_file
            except Exception:
                cache_file = cache_file.resolve()
    else:
        cache_file = catalog_mod.CACHE_FILE_DEFAULT

    sync_fn = getattr(catalog_mod, "sync_provider_catalogs", None)
    drift_fn = getattr(catalog_mod, "compute_config_drift", None)
    if sync_fn is None or drift_fn is None:
        return {"status": "unavailable", "reason": "catalog sync API missing"}

    providers_cfg = cfg.get("providers") if isinstance(cfg, dict) else {}
    if not isinstance(providers_cfg, dict):
        providers_cfg = {}

    sync_results: Dict[str, Any] = {}
    sync_exception = ""
    try:
        sync_results = sync_fn(
            ProviderRegistry,
            provider_names,
            cache_file=cache_file,
            timeout=timeout,
            provider_configs=providers_cfg,
        )
        if not isinstance(sync_results, dict):
            sync_results = {}
    except Exception as exc:
        sys.stderr.write("[BoteX] catalog sync failed\n")
        try:
            from botex.tools.security import exception_evidence

            sync_exception = exception_evidence(exc)
        except Exception:
            sync_exception = type(exc).__name__
        # Countable no-evidence: every attempted provider must surface as an
        # error entry — an empty sync section would read as zero errors.
        sync_results = {
            name: {
                "status": "error",
                "model_count": 0,
                "error": sync_exception,
                "fetched_at": 0.0,
            }
            for name in provider_names
        }

    # No persistence-boundary scrubber here by design: catalog.py emits typed
    # error evidence only (class name + numeric codes, never exception free
    # text), so secret-bearing text cannot enter the report at the source —
    # regex re-masking at this boundary would only multiply taint-graph edges.
    try:
        catalogs = load_cached_catalogs(provider_names, providers_cfg, cache_file)
        drift_entries = drift_fn(providers_cfg, catalogs)
        if not isinstance(drift_entries, list):
            drift_entries = []
    except Exception:
        drift_entries = []
        sys.stderr.write("[BoteX] catalog drift computation failed\n")

    drifted = [
        e for e in drift_entries if isinstance(e, dict) and e.get("present_in_catalog") is False
    ]
    if drifted:
        sys.stderr.write(
            f"[BoteX] DRIFT: {len(drifted)} configured model entries absent from live catalog\n"
        )

    report = {
        "generated_at": time.time(),
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "sync": sync_results,
        "drift": drift_entries,
        "summary": {
            "drifted": len(drifted),
            "no_data": sum(
                1
                for e in drift_entries
                if isinstance(e, dict) and e.get("present_in_catalog") is None
            ),
            "present": sum(
                1
                for e in drift_entries
                if isinstance(e, dict) and e.get("present_in_catalog") is True
            ),
        },
    }
    if sync_exception:
        report["sync_exception"] = sync_exception
    written_path: "Path | None" = None
    tmp_path: "Path | None" = None
    try:
        written_path = resolved_report
        written_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = written_path.with_name(f"{written_path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        os.replace(str(tmp_path), str(written_path))
    except Exception:
        written_path = None
        sys.stderr.write("[BoteX] catalog drift report write failed\n")
        try:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass

    ok_count, error_count = count_provider_evidence(sync_results)
    return {
        "status": "ok",
        "sync": sync_results,
        "drift": drift_entries,
        "summary": report["summary"],
        "providers_ok": ok_count,
        "providers_error": error_count,
        "report_path": str(written_path) if written_path else "",
    }
