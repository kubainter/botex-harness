# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Curator task registry: interval-gated background housekeeping.

Replaces the ad-hoc Python heredocs of ``botex-maintenance.sh`` with a single
registry. Per-task ``last_run`` state is persisted so a frequent external tick
(systemd timer or in-process loop) only executes tasks that are actually due.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

SCRATCHPAD_LOG_MAX_AGE_S = 3 * 86400.0
# Below the 30 min systemd tick so run-time jitter never skips a whole cycle.
TICK_INTERVAL_S = 1500.0
_STATE_FILENAME = "tasks_state.json"


@dataclass(frozen=True)
class CuratorTask:
    name: str
    interval_s: float
    fn: Callable[[str], Dict[str, Any]]


def _task_index_sync(workspace_dir: str) -> Dict[str, Any]:
    from botex import memory, obsidian

    ws = Path(workspace_dir).resolve()
    synced = 0
    for d in (ws / ".botex" / "memory", Path.home() / ".botex" / "memory"):
        if not d.is_dir():
            continue
        memory.sync_memory_index(d)
        conn = memory._get_index_db(d)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.execute("PRAGMA optimize;")
        finally:
            conn.close()
        synced += 1
    obsidian.export_graph_index_markdown(workspace_dir=str(ws), scope="all")
    health = memory.find_graph_health(workspace_dir=str(ws), scope="all")
    return {
        "vaults_synced": synced,
        "total_nodes": health.get("total_nodes", 0),
        "is_healthy": health.get("is_healthy", False),
    }


def _task_frontmatter_repair(workspace_dir: str) -> Dict[str, Any]:
    from botex.ops.maintenance import repair_frontmatter_ids

    return dict(repair_frontmatter_ids(workspace_dir=workspace_dir))


def _task_chat_sweep(workspace_dir: str) -> Dict[str, Any]:
    from botex.ops.maintenance import sweep_empty_chat_sessions

    return {"removed": sweep_empty_chat_sessions(workspace_dir=workspace_dir)}


def _task_ledger_rotation(workspace_dir: str) -> Dict[str, Any]:
    from botex.ops.maintenance import rotate_analytics_ledger

    return dict(rotate_analytics_ledger(workspace_dir=workspace_dir))


def _task_provider_warmup(workspace_dir: str) -> Dict[str, Any]:
    from botex.ops.maintenance import warm_provider_metadata

    return dict(warm_provider_metadata(workspace_dir=workspace_dir))


def _task_scratchpad_prune(workspace_dir: str) -> Dict[str, Any]:
    scratch = Path(workspace_dir).resolve() / ".botex" / "scratchpad"
    removed = 0
    if scratch.is_dir():
        cutoff = time.time() - SCRATCHPAD_LOG_MAX_AGE_S
        for p in scratch.rglob("*.log"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
    return {"removed": removed}


_TEMP_HANDOFF_EXPIRES_RE = re.compile(r"(?im)^expires\s*:\s*(\d{4}-\d{2}-\d{2})\b")


def _task_temp_handoff_sweep(workspace_dir: str) -> Dict[str, Any]:
    """Delete expired ``temp-handoff`` notes from local and global memory roots.

    Temp-handoff notes are ephemeral coordination crumbs: they carry
    ``tags: [temp-handoff]`` and an ``expires: YYYY-MM-DD`` line (frontmatter or
    body). Notes whose expiry is older than today (UTC) are removed through the
    memory facade so the SQLite index stays consistent.
    """
    from botex import memory

    ws = Path(workspace_dir).resolve()
    mem_dirs: List[tuple] = [
        (ws / ".botex" / "memory", "local"),
        (memory.get_global_memory_dir(), "global"),
    ]
    today = datetime.now(timezone.utc).date().isoformat()

    scanned = 0
    deleted: List[str] = []
    seen_dirs: set = set()

    for mem_dir, scope in mem_dirs:
        if not mem_dir.is_dir():
            continue
        try:
            dir_key = str(mem_dir.resolve())
        except Exception:
            dir_key = str(mem_dir)
        if dir_key in seen_dirs:
            continue
        seen_dirs.add(dir_key)

        for md_file in sorted(mem_dir.glob("*.md")):
            if not md_file.is_file() or md_file.name.startswith("_"):
                continue
            try:
                raw = md_file.read_text(encoding="utf-8", errors="replace")
                meta, body = memory._parse_frontmatter(raw)
            except Exception:
                continue
            if not meta:
                continue
            scanned += 1

            raw_tags = meta.get("tags") or []
            if isinstance(raw_tags, str):
                raw_tags = [t.strip() for t in raw_tags.strip("[]").split(",") if t.strip()]
            norm_tags = {str(t).strip().lower() for t in raw_tags}
            if "temp-handoff" not in norm_tags:
                continue

            expires = str(meta.get("expires") or "").strip()
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", expires):
                m = _TEMP_HANDOFF_EXPIRES_RE.search(body or raw)
                expires = m.group(1) if m else ""
            if not expires or not (today > expires):
                continue

            mid = md_file.stem
            res = memory.delete_memory(workspace_dir=str(ws), memory_id=mid, scope=scope)
            if res.get("ok"):
                deleted.append(mid)
                continue
            try:
                md_file.unlink()
                memory.sync_memory_index(mem_dir)
                deleted.append(mid)
            except OSError:
                continue

    return {"scanned": scanned, "expired_deleted": len(deleted), "deleted_ids": deleted}


def _task_dead_link_repair(workspace_dir: str) -> Dict[str, Any]:
    from botex.ops.maintenance import repair_dead_links

    return dict(repair_dead_links(workspace_dir=workspace_dir, apply=True))


def _task_purge_ephemeral(workspace_dir: str) -> Dict[str, Any]:
    from botex.memory.memory import purge_ephemeral_memories

    deleted = purge_ephemeral_memories(workspace_dir=workspace_dir, scope="all")
    return {"deleted": deleted}


DEFAULT_TASKS: Sequence[CuratorTask] = (
    CuratorTask("index_sync", TICK_INTERVAL_S, _task_index_sync),
    CuratorTask("frontmatter_repair", TICK_INTERVAL_S, _task_frontmatter_repair),
    CuratorTask("chat_sweep", TICK_INTERVAL_S, _task_chat_sweep),
    CuratorTask("ledger_rotation", TICK_INTERVAL_S, _task_ledger_rotation),
    CuratorTask("provider_warmup", TICK_INTERVAL_S, _task_provider_warmup),
    CuratorTask("scratchpad_prune", 86400.0, _task_scratchpad_prune),
    CuratorTask("temp_handoff_sweep", 3600.0, _task_temp_handoff_sweep),
    CuratorTask("dead_link_repair", 3600.0, _task_dead_link_repair),
    CuratorTask("purge_ephemeral_memories", 86400.0, _task_purge_ephemeral),
)


def _state_path(workspace_dir: str) -> Path:
    return Path(workspace_dir).resolve() / ".botex" / "curator" / _STATE_FILENAME


def load_task_state(workspace_dir: str) -> Dict[str, Any]:
    try:
        data = json.loads(_state_path(workspace_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_task_state(workspace_dir: str, state: Dict[str, Any]) -> None:
    path = _state_path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def run_due_tasks(
    workspace_dir: str = ".",
    *,
    only: Optional[Sequence[str]] = None,
    force: bool = False,
    dry_run: bool = False,
    registry: Optional[Sequence[CuratorTask]] = None,
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Run every task whose interval elapsed; return a per-task report.

    ``dry_run`` only reports which tasks are due and never executes them or
    touches persisted state. A failing task is recorded and does not block the
    others; its ``last_run`` is not advanced, so it is retried on the next tick.
    """
    tasks: List[CuratorTask] = list(registry if registry is not None else DEFAULT_TASKS)
    known = {t.name for t in tasks}
    unknown = sorted(set(only or ()) - known)
    if unknown:
        raise ValueError(f"Unknown curator task(s): {', '.join(unknown)}")

    state = load_task_state(workspace_dir)
    now = clock()
    report: Dict[str, Any] = {}

    for task in tasks:
        if only and task.name not in only:
            continue
        last = state.get(task.name, {}).get("last_run")
        age = None if not isinstance(last, (int, float)) else now - float(last)
        due = force or age is None or age >= task.interval_s
        if not due:
            report[task.name] = {"status": "skipped", "next_in_s": round(task.interval_s - age, 1)}
            continue
        if dry_run:
            report[task.name] = {"status": "due"}
            continue
        started = time.monotonic()
        try:
            result = task.fn(workspace_dir)
        except Exception as exc:
            logger.warning("curator task %s failed: %s", task.name, exc)
            report[task.name] = {"status": "failed", "error": str(exc)}
            continue
        state[task.name] = {"last_run": now}
        report[task.name] = {
            "status": "ran",
            "duration_s": round(time.monotonic() - started, 3),
            "result": result,
        }

    if not dry_run and any(r["status"] == "ran" for r in report.values()):
        _save_task_state(workspace_dir, state)
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="BoteX Curator housekeeping tasks")
    parser.add_argument("--workspace", default=os.environ.get("BOTEX_WORKSPACE", "."))
    parser.add_argument("--only", action="append", help="Run only this task (repeatable)")
    parser.add_argument("--force", action="store_true", help="Ignore intervals")
    parser.add_argument("--dry-run", action="store_true", help="Report due tasks, run nothing")
    parser.add_argument("--list", action="store_true", help="List registered tasks")
    args = parser.parse_args(argv)

    if args.list:
        for t in DEFAULT_TASKS:
            print(f"{t.name}\t{int(t.interval_s)}s")
        return 0

    report = run_due_tasks(args.workspace, only=args.only, force=args.force, dry_run=args.dry_run)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 1 if any(r["status"] == "failed" for r in report.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
