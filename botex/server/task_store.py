# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Persistent background-task store (.botex/tasks/<task_id>.json).

Shared by the FastAPI cloud gateway and the standalone MCP server so the
in-memory task registry can fall back to disk before answering NOT_FOUND.
Loaders are pure disk scans returning fresh dicts — callers merge into
the live ``_TASKS`` registry on the event loop, so worker threads spawned
via ``asyncio.to_thread`` never mutate shared state.
"""

import json
import logging
import time
from typing import Any, Dict, Optional

from botex.server.config import get_tasks_dir
from botex.tools.security import sanitize_identifier

logger = logging.getLogger(__name__)


def save_task_persistent(task_id: str, entry: Dict[str, Any], workspace_dir: str = "") -> None:
    """Persist task metadata and receipt to .botex/tasks/<task_id>.json."""
    try:
        safe_tid = sanitize_identifier(task_id)
        if not safe_tid:
            return
        tasks_dir = get_tasks_dir(workspace_dir)
        existing = next(
            (f for f in tasks_dir.iterdir() if f.is_file() and f.name == f"{safe_tid}.json"),
            None,
        )
        p = existing if existing is not None else (tasks_dir / f"{safe_tid}.json")
        clean = {k: v for k, v in entry.items() if k != "async_task"}
        p.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to persist task metadata: %s", type(exc).__name__)


def _normalize_restored_task(tid: str, data: Dict[str, Any]) -> Dict[str, Any]:
    data["task_id"] = tid
    data["async_task"] = None
    # Restart reaper: a persisted RUNNING/QUEUED entry has no live worker
    # after a server restart — mark it failed instead of leaving an
    # unkillable zombie in the PWA.
    if data.get("status") in {"RUNNING", "QUEUED"}:
        data["status"] = "FAILED"
        data["failure_kind"] = "server_restarted"
        data["result"] = data.get("result") or "Task interrupted by server restart."
        data["finished_at"] = data.get("finished_at") or time.time()
    return data


def load_task_persistent(task_id: str, workspace_dir: str = "") -> Optional[Dict[str, Any]]:
    """O(1) single-task restore from .botex/tasks/<task_id>.json.

    Returns a normalized entry dict or None — never touches the in-memory
    registry, so it is safe to run inside ``asyncio.to_thread``.
    """
    raw_tid = str(task_id or "").strip()
    safe_tid = sanitize_identifier(raw_tid)
    if not safe_tid or safe_tid != raw_tid:
        return None
    root = get_tasks_dir(workspace_dir).resolve()
    candidate = (root / f"{safe_tid}.json").resolve()
    if candidate.parent != root or not candidate.is_file():
        return None
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Cannot read persisted task %s: %s", safe_tid, type(exc).__name__)
        return None
    if not isinstance(data, dict):
        return None
    return _normalize_restored_task(safe_tid, data)


def load_tasks_persistent(workspace_dir: str = "") -> Dict[str, Dict[str, Any]]:
    """Scan persisted tasks on disk into a fresh dict.

    Pure read — callers merge the result into ``_TASKS`` on the event
    loop. Used only for full hydrates (task listing); single-task lookups
    must go through ``load_task_persistent`` instead.
    """
    restored: Dict[str, Dict[str, Any]] = {}
    try:
        tasks_dir = get_tasks_dir(workspace_dir)
        for f in tasks_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                tid = str(data.get("task_id") or f.stem)
                if tid not in restored:
                    restored[tid] = _normalize_restored_task(tid, data)
            except Exception as exc:
                logger.warning(
                    "Skipping corrupt persisted task file %s: %s",
                    f.name,
                    type(exc).__name__,
                )
    except Exception as exc:
        logger.warning("Failed to load persistent tasks: %s", exc)
    return restored
