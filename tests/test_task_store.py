# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for botex/server/task_store.py.

Covers the O(1) point restore (`load_task_persistent`), the pure-scan
bulk loader, and thread-safety of the TTL-miss restore path (worker
threads must never mutate the shared ``_TASKS`` registry).
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import server as botex_server
from botex.config import get_tasks_dir
from botex.server import server_cloud
from botex.server.server_cloud import app
from botex.server.task_store import (
    load_task_persistent,
    load_tasks_persistent,
    save_task_persistent,
)


@pytest.fixture
def client(monkeypatch):
    """TestClient fixture with unauthenticated environment."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _block_lifespan_project_migration(monkeypatch):
    """Stub the warmup backfill so TestClient lifespan never migrates the real vault."""
    monkeypatch.setattr(
        "botex.ops.migrate_projects.ensure_projects_migrated",
        lambda *args, **kwargs: {"ok": True, "roots": [], "total_updated": 0},
    )


def _seed_task_file(task_id: str, status: str = "DONE"):
    """Persist a terminal task straight to disk (as if its TTL already evicted it)."""
    entry = {
        "task_id": task_id,
        "task": "task-store test",
        "request": {"workspace_dir": ""},
        "status": status,
        "created_at": time.time() - 7200,
        "finished_at": time.time() - 3700,
        "result": "done",
        "result_data": {"ok": True, "status": status},
        "async_task": None,
    }
    save_task_persistent(task_id, entry, "")
    path = get_tasks_dir("") / f"{task_id}.json"
    assert path.exists()
    return path


def test_load_task_persistent_point_lookup():
    """O(1) restore reads a single file and never touches _TASKS."""
    ids = [uuid.uuid4().hex[:12] for _ in range(3)]
    paths = [_seed_task_file(t) for t in ids]
    try:
        got = load_task_persistent(ids[0])
        assert got is not None
        assert got["task_id"] == ids[0]
        assert got["status"] == "DONE"
        # Pure read — the registry is untouched by the loader itself.
        assert not any(t in botex_server._TASKS for t in ids)
    finally:
        for p in paths:
            p.unlink(missing_ok=True)


def test_load_task_persistent_rejects_bad_ids():
    """Traversal attempts, empty ids and oversized ids return None without I/O."""
    for bad in ("../secret", "a/b", "..", "a/../b", "", " ", "x" * 129, None):
        assert load_task_persistent(bad) is None


def test_load_task_persistent_corrupt_and_non_dict():
    """Corrupt JSON and non-dict payloads return None, never raise."""
    tid_corrupt = uuid.uuid4().hex[:12]
    tid_list = uuid.uuid4().hex[:12]
    p1 = get_tasks_dir("") / f"{tid_corrupt}.json"
    p2 = get_tasks_dir("") / f"{tid_list}.json"
    p1.write_text("{not valid json", encoding="utf-8")
    p2.write_text('["not", "a", "dict"]', encoding="utf-8")
    try:
        assert load_task_persistent(tid_corrupt) is None
        assert load_task_persistent(tid_list) is None
    finally:
        p1.unlink(missing_ok=True)
        p2.unlink(missing_ok=True)


def test_load_task_persistent_reaps_interrupted_status():
    """Persisted RUNNING/QUEUED entries restore as server_restarted failures."""
    tid = uuid.uuid4().hex[:12]
    path = _seed_task_file(tid, status="RUNNING")
    try:
        got = load_task_persistent(tid)
        assert got is not None
        assert got["status"] == "FAILED"
        assert got["failure_kind"] == "server_restarted"
    finally:
        path.unlink(missing_ok=True)


def test_load_tasks_persistent_pure_scan_no_mutation():
    """Bulk scan returns a fresh dict and leaves _TASKS untouched."""
    ids = [uuid.uuid4().hex[:12] for _ in range(2)]
    paths = [_seed_task_file(t) for t in ids]
    try:
        restored = load_tasks_persistent("")
        for t in ids:
            assert t in restored
            assert restored[t]["status"] == "DONE"
            assert t not in botex_server._TASKS
    finally:
        for p in paths:
            p.unlink(missing_ok=True)


def test_get_task_status_restores_only_requested_task():
    """TTL-miss on get_task_status hydrates exactly one task — no full rehydrate."""
    ids = [uuid.uuid4().hex[:12] for _ in range(3)]
    paths = [_seed_task_file(t) for t in ids]
    try:
        result = asyncio.run(botex_server.get_task_status(ids[0]))
        assert result["ok"] is True
        assert result["status"] == "DONE"
        assert ids[0] in botex_server._TASKS
        assert ids[1] not in botex_server._TASKS
        assert ids[2] not in botex_server._TASKS
        # Restored entry gets a fresh TTL window so the next cleanup keeps it.
        assert botex_server._TASKS[ids[0]].get("restored_at")
        botex_server._cleanup_tasks()
        assert ids[0] in botex_server._TASKS
    finally:
        for t in ids:
            botex_server._TASKS.pop(t, None)
        for p in paths:
            p.unlink(missing_ok=True)


def test_get_task_status_unknown_id_no_side_effects():
    """Unknown ids stay NOT_FOUND and trigger no disk-wide hydration."""
    other = uuid.uuid4().hex[:12]
    path = _seed_task_file(other)
    try:
        result = asyncio.run(botex_server.get_task_status("ghost-id-123"))
        assert result == {"ok": False, "status": "NOT_FOUND", "task_id": "ghost-id-123"}
        assert "ghost-id-123" not in botex_server._TASKS
        assert other not in botex_server._TASKS
    finally:
        path.unlink(missing_ok=True)


def test_mcp_list_tasks_merges_persisted_without_rehydrate():
    """MCP list_tasks serves disk-backed terminal tasks after process state loss."""
    ids = [uuid.uuid4().hex[:12] for _ in range(2)]
    paths = [_seed_task_file(t) for t in ids]
    try:
        result = asyncio.run(botex_server.list_tasks())
        assert result["ok"] is True
        listed = {t["task_id"] for t in result["tasks"]}
        assert ids[0] in listed and ids[1] in listed
        assert ids[0] not in botex_server._TASKS
        assert ids[1] not in botex_server._TASKS
    finally:
        for t in ids:
            botex_server._TASKS.pop(t, None)
        for p in paths:
            p.unlink(missing_ok=True)


def test_get_task_route_restores_only_requested_task(client):
    """REST GET /api/tasks/{id} also restores O(1) — no full scan on 404 path."""
    ids = [uuid.uuid4().hex[:12] for _ in range(3)]
    paths = [_seed_task_file(t) for t in ids]
    try:
        resp = client.get(f"/api/tasks/{ids[0]}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "DONE"
        assert ids[0] in server_cloud._TASKS
        assert ids[1] not in server_cloud._TASKS
        assert ids[2] not in server_cloud._TASKS
    finally:
        for t in ids:
            server_cloud._TASKS.pop(t, None)
        for p in paths:
            p.unlink(missing_ok=True)


def test_get_task_route_404_no_full_scan(client):
    """A 404 on an unknown id must not hydrate unrelated persisted tasks."""
    other = uuid.uuid4().hex[:12]
    path = _seed_task_file(other)
    try:
        resp = client.get("/api/tasks/ghost-id-456")
        assert resp.status_code == 404
        assert other not in server_cloud._TASKS
    finally:
        path.unlink(missing_ok=True)


def test_list_tasks_does_not_rehydrate_evicted_into_ram(client):
    """GET /api/tasks serves persisted tasks but must NOT grow _TASKS.

    Regression for the pre-fix behavior where every poll re-seeded all
    historical disk entries into the in-memory registry (unbounded RAM).
    """
    ids = [uuid.uuid4().hex[:12] for _ in range(2)]
    paths = [_seed_task_file(t) for t in ids]
    try:
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        listed = {t["task_id"] for t in resp.json()["tasks"]}
        assert ids[0] in listed and ids[1] in listed
        # Served from the merged local view — _TASKS stays un-hydrated.
        assert ids[0] not in server_cloud._TASKS
        assert ids[1] not in server_cloud._TASKS
    finally:
        for t in ids:
            server_cloud._TASKS.pop(t, None)
        for p in paths:
            p.unlink(missing_ok=True)


def test_concurrent_point_restores_no_dict_mutation_race():
    """Parallel TTL-miss restores + _cleanup_tasks iteration: no RuntimeError."""
    ids = [uuid.uuid4().hex[:12] for _ in range(8)]
    paths = [_seed_task_file(t) for t in ids]
    try:

        async def _iterate_and_cleanup():
            for _ in range(200):
                # Raw dict iteration — a worker-thread insert would raise
                # RuntimeError: dictionary changed size during iteration.
                for _k in botex_server._TASKS.values():
                    pass
                botex_server._cleanup_tasks()
                await asyncio.sleep(0)

        async def _run():
            await asyncio.gather(
                _iterate_and_cleanup(),
                *(botex_server.get_task_status(t) for t in ids),
            )

        asyncio.run(_run())
        assert all(t in botex_server._TASKS for t in ids)
    finally:
        for t in ids:
            botex_server._TASKS.pop(t, None)
        for p in paths:
            p.unlink(missing_ok=True)
