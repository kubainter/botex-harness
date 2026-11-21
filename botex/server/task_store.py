# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Persistent background-task store (.botex/tasks/<task_id>.json).

Shared by the FastAPI cloud gateway and the standalone MCP server so the
in-memory task registry can fall back to disk before answering NOT_FOUND.
The registry dict is passed in (it lives in ``server.py`` as ``_TASKS``)
to keep this module free of import cycles.
"""

import json
import logging
import time
from typing import Any, Dict

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


def load_tasks_persistent(tasks: Dict[str, Dict[str, Any]], workspace_dir: str = "") -> None:
    """Restore historical background tasks from disk into the given registry."""
    try:
        tasks_dir = get_tasks_dir(workspace_dir)
        for f in tasks_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                tid = data.get("task_id") or f.stem
                if tid not in tasks:
                    data["async_task"] = None
                    # Restart reaper: a persisted RUNNING/QUEUED entry has no
                    # live worker after a server restart — mark it failed
                    # instead of leaving an unkillable zombie in the PWA.
                    if data.get("status") in {"RUNNING", "QUEUED"}:
                        data["status"] = "FAILED"
                        data["failure_kind"] = "server_restarted"
                        data["result"] = data.get("result") or (
                            "Task interrupted by server restart."
                        )
                        data["finished_at"] = data.get("finished_at") or time.time()
                    tasks[tid] = data
            except Exception as exc:
                logger.warning(
                    "Skipping corrupt persisted task file %s: %s",
                    f.name,
                    type(exc).__name__,
                )
    except Exception as exc:
        logger.warning("Failed to load persistent tasks: %s", exc)
