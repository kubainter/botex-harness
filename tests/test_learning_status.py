# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for GET /api/learning/status — self-improvement loop telemetry (Layers A-C)."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from botex.analytics import save_analytics
from botex.memory import save_memory
from botex.server_cloud import app


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


def _save_lesson(ws: str, memory_id: str, status: str, confidence: float, scope="local"):
    res = save_memory(
        workspace_dir=ws,
        title=f"Lesson {memory_id}",
        content="episodic lesson body",
        kind="lesson",
        memory_id=memory_id,
        status=status,
        confidence=confidence,
        scope=scope,
        tags=["path:src"],
    )
    assert res["ok"] is True
    return res["id"]


def test_learning_status_empty_workspace(client, tmp_path):
    """Empty workspace yields zeroed lessons/proposals/ledger and curator keys."""
    resp = client.get(f"/api/learning/status?workspace_dir={tmp_path}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True

    lessons = data["lessons"]
    assert lessons["counts"] == {
        "candidate": 0,
        "active": 0,
        "deprecated": 0,
        "superseded": 0,
        "total": 0,
    }
    assert lessons["avg_confidence"] is None
    assert lessons["items"] == []

    assert data["proposals"] == []

    ledger = data["ledger"]
    assert ledger["runs_total"] == 0
    assert ledger["runs_with_applied_lessons"] == 0
    assert ledger["runs_with_applied_skills"] == 0
    assert ledger["runs_with_shadow_advice"] == 0
    assert ledger["shadow_recommendations"] == []

    assert "is_healthy" in data["curator"]
    assert "last_run_timestamp" in data["curator"]


def test_learning_status_lesson_counts_and_confidence(client, tmp_path):
    """Lifecycle counts, avg confidence, and item shape over real vault lessons."""
    ws = str(tmp_path)
    _save_lesson(ws, "mem_cand1", "candidate", 0.4)
    _save_lesson(ws, "mem_cand2", "candidate", 0.6)
    _save_lesson(ws, "mem_act1", "active", 0.9)
    _save_lesson(ws, "mem_dep1", "deprecated", 0.1)
    save_memory(
        workspace_dir=ws,
        title="Not a lesson",
        content="decision body",
        kind="decision",
        scope="local",
    )

    resp = client.get(f"/api/learning/status?workspace_dir={ws}")
    assert resp.status_code == 200
    lessons = resp.json()["lessons"]
    assert lessons["counts"] == {
        "candidate": 2,
        "active": 1,
        "deprecated": 1,
        "superseded": 0,
        "total": 4,
    }
    assert lessons["avg_confidence"] == pytest.approx((0.4 + 0.6 + 0.9 + 0.1) / 4, abs=1e-3)

    items = lessons["items"]
    assert len(items) == 4
    for item in items:
        assert set(item.keys()) == {
            "memory_id",
            "title",
            "status",
            "confidence",
            "tags",
            "scope",
            "updated_at",
        }
    assert {i["memory_id"] for i in items} == {
        "mem_cand1",
        "mem_cand2",
        "mem_act1",
        "mem_dep1",
    }
    assert all(i["scope"] == "local" for i in items)


def test_learning_status_dedup_across_local_and_global(client, tmp_path):
    """Same memory_id in local + global scopes is counted once (local wins)."""
    ws = str(tmp_path)
    _save_lesson(ws, "mem_dup", "candidate", 0.4, scope="local")
    _save_lesson(ws, "mem_dup", "active", 0.9, scope="global")
    _save_lesson(ws, "mem_glob", "active", 0.8, scope="global")

    resp = client.get(f"/api/learning/status?workspace_dir={ws}")
    assert resp.status_code == 200
    lessons = resp.json()["lessons"]
    assert lessons["counts"]["total"] == 2
    assert lessons["counts"]["candidate"] == 1
    assert lessons["counts"]["active"] == 1

    by_id = {i["memory_id"]: i for i in lessons["items"]}
    assert by_id["mem_dup"]["scope"] == "local"
    assert by_id["mem_dup"]["status"] == "candidate"
    assert by_id["mem_glob"]["scope"] == "global"


def test_learning_status_items_capped_at_20(client, tmp_path):
    """Only the 20 most recently updated lessons are returned in items."""
    ws = str(tmp_path)
    mem_dir = tmp_path / ".botex" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(25):
        (mem_dir / f"mem_l{idx:02d}.md").write_text(
            "---\n"
            f"id: mem_l{idx:02d}\n"
            f"title: Lesson {idx}\n"
            "kind: lesson\n"
            "status: active\n"
            "confidence: 0.5\n"
            f"timestamp: 2026-01-{idx + 1:02d}T00:00:00+00:00\n"
            "---\n\n"
            "body\n",
            encoding="utf-8",
        )

    resp = client.get(f"/api/learning/status?workspace_dir={ws}")
    assert resp.status_code == 200
    lessons = resp.json()["lessons"]
    assert lessons["counts"]["total"] == 25
    assert lessons["counts"]["active"] == 25
    items = lessons["items"]
    assert len(items) == 20
    # Most recent first: mem_l24 (Jan 25) down to mem_l05 (Jan 6).
    assert items[0]["memory_id"] == "mem_l24"
    assert items[-1]["memory_id"] == "mem_l05"
    assert items[0]["updated_at"] == "2026-01-25T00:00:00+00:00"


def test_learning_status_proposals_listing(client, tmp_path):
    """Subdirectories under .botex/proposals are listed with files and mtime."""
    ws = str(tmp_path)
    skill_dir = tmp_path / ".botex" / "proposals" / "skills" / "learned-bugfix-foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: learned-bugfix-foo\n---\n\nbody\n")
    recipes_dir = tmp_path / ".botex" / "proposals" / "recipes"
    recipes_dir.mkdir(parents=True)
    (recipes_dir / "learned-bugfix-foo.md").write_text("recipe body\n")

    resp = client.get(f"/api/learning/status?workspace_dir={ws}")
    assert resp.status_code == 200
    proposals = resp.json()["proposals"]
    by_name = {p["name"]: p for p in proposals}
    assert by_name["learned-bugfix-foo"]["files"] == ["SKILL.md"]
    assert by_name["recipes"]["files"] == ["learned-bugfix-foo.md"]
    for p in proposals:
        assert isinstance(p["updated_at"], str)
        assert datetime.fromisoformat(p["updated_at"])


def test_learning_status_ledger_telemetry(client, tmp_path):
    """Ledger counters and shadow recommendation aggregation from real analytics."""
    records = [
        {
            "task_id": "t1",
            "timestamp": "2026-01-01T00:00:00",
            "model": "m1",
            "status": "DONE",
            "recipe": "code-explorer",
            "taxonomy": "bugfix",
            "applied_lessons": ["mem_a"],
            "applied_skills": ["build-resolver"],
            "shadow_recommended_model": "mA",
        },
        {
            "task_id": "t2",
            "timestamp": "2026-01-02T00:00:00",
            "model": "m2",
            "status": "DONE",
            "recipe": "code-explorer",
            "taxonomy": "bugfix",
            "applied_lessons": ["mem_b"],
            "shadow_recommended_model": "mA",
        },
        {
            "task_id": "t3",
            "timestamp": "2026-01-03T00:00:00",
            "model": "m3",
            "status": "DONE",
            "recipe": "planner",
            "taxonomy": "refactor",
            "applied_skills": ["tdd-verification"],
            "shadow_recommended_model": "mB",
        },
        {
            "task_id": "t4",
            "timestamp": "2026-01-04T00:00:00",
            "model": "m4",
            "status": "FAILED",
        },
    ]
    save_analytics(records)

    resp = client.get(f"/api/learning/status?workspace_dir={tmp_path}")
    assert resp.status_code == 200
    ledger = resp.json()["ledger"]
    assert ledger["runs_total"] == 4
    assert ledger["runs_with_applied_lessons"] == 2
    assert ledger["runs_with_applied_skills"] == 2
    assert ledger["runs_with_shadow_advice"] == 3

    shadow = ledger["shadow_recommendations"]
    assert len(shadow) == 2
    assert shadow[0] == {
        "recipe": "code-explorer",
        "taxonomy": "bugfix",
        "model": "mA",
        "count": 2,
    }
    assert shadow[1] == {
        "recipe": "planner",
        "taxonomy": "refactor",
        "model": "mB",
        "count": 1,
    }


def test_learning_status_shadow_recommendations_capped_at_10(client, tmp_path):
    """Top-10 cap on (recipe, taxonomy, model) shadow groups."""
    records = [
        {
            "task_id": f"t{i}",
            "timestamp": f"2026-02-{i + 1:02d}T00:00:00",
            "model": "mx",
            "status": "DONE",
            "recipe": f"r{i}",
            "taxonomy": "t",
            "shadow_recommended_model": f"model{i}",
        }
        for i in range(12)
    ]
    save_analytics(records)

    resp = client.get(f"/api/learning/status?workspace_dir={tmp_path}")
    assert resp.status_code == 200
    shadow = resp.json()["ledger"]["shadow_recommendations"]
    assert len(shadow) == 10
    assert all(s["count"] == 1 for s in shadow)


def test_learning_status_curator_passthrough(client, tmp_path, monkeypatch):
    """Curator sub-object is a faithful passthrough of get_curator_status."""
    monkeypatch.setattr(
        "botex.curator.get_curator_status",
        lambda workspace_dir=".": {
            "is_healthy": False,
            "last_run_timestamp": "2026-11-14T01:02:03+00:00",
        },
    )
    resp = client.get(f"/api/learning/status?workspace_dir={tmp_path}")
    assert resp.status_code == 200
    assert resp.json()["curator"] == {
        "is_healthy": False,
        "last_run_timestamp": "2026-11-14T01:02:03+00:00",
    }


def test_learning_status_requires_auth_when_token_set(client, monkeypatch):
    """Strict cloud mode rejects unauthenticated learning-status reads."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    resp = client.get("/api/learning/status")
    assert resp.status_code == 401
