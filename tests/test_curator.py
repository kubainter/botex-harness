# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Tests for BoteX Autonomous Knowledge Graph Curator & Telegram Notifier
======================================================================
Covers:
1. Telegram configuration, token masking (CWE-312), formatting, and graceful skip when unconfigured.
2. Curator lockfile concurrency protection and active agent tasks collision deferral.
3. Multi-tier LLM fallback: Nemotron -> Local Bielik -> Deterministic heuristics.
4. Zero Memory Vault Bloat Invariant: Single living beacon note (curator_health_status.md).
5. Persistence of run records (.botex/curator/runs/) and status (.botex/curator/status.json).
6. Cloud Gateway API endpoints: GET /api/curator/status and POST /api/curator/run.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from botex.curator import (
    CuratorResult,
    CuratorService,
    TelegramNotifier,
    get_curator_status,
    run_curation_async,
    send_telegram_notification,
)
from botex.memory import read_memory, save_memory, search_memory
from botex.server_cloud import app
from botex.ops.curator.curator_service import CuratorLock, check_active_agent_tasks
from botex.ops.curator.telegram import (
    format_curator_telegram_message,
    get_telegram_config,
    mask_telegram_token,
)


@pytest.fixture
def client(monkeypatch):
    """TestClient fixture with unauthenticated environment."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 1. Telegram Notifier Tests
# ---------------------------------------------------------------------------


def test_telegram_token_masking():
    """Verify Telegram token is masked in URLs and logs to prevent credential leakage."""
    raw_url = "https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345/sendMessage"
    masked = mask_telegram_token(raw_url)
    assert "123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345" not in masked
    assert "bot***MASKED_TOKEN***" in masked

    empty_masked = mask_telegram_token("")
    assert empty_masked == ""


def test_telegram_config_defaults(monkeypatch):
    """Verify configuration loads safely when environment variables are missing."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ALERT_ON_SUCCESS", raising=False)

    cfg = get_telegram_config()
    assert cfg["bot_token"] == ""
    assert cfg["chat_id"] == ""
    assert cfg["alert_on_success"] is False
    assert cfg["is_configured"] is False


def test_telegram_send_skipped_when_unconfigured(monkeypatch):
    """Notification send must gracefully return skipped status without throwing."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    res = send_telegram_notification("Test message")
    assert res["ok"] is False
    assert res["skipped"] is True
    assert "missing" in res["reason"].lower()


def test_telegram_format_message():
    """Verify Telegram message format contains key topology metrics."""
    report = {
        "run_id": "test_run_123",
        "is_healthy": True,
        "total_nodes": 42,
        "total_edges": 84,
        "orphan_count": 3,
        "broken_count": 0,
        "tier_used": "nvidia-nemotron",
        "duration_seconds": 1.25,
        "broken_links": [],
    }
    msg = format_curator_telegram_message(report)
    assert "✅ *BoteX Knowledge Graph Curator*" in msg
    assert "• Total Notes: *42*" in msg
    assert "• Total Edges: *84*" in msg
    assert "• Orphan Notes: *3*" in msg
    assert "curator_health_status.md" in msg


def test_telegram_notifier_class_mock_delivery(monkeypatch):
    """Verify TelegramNotifier dispatches correctly when mocked."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:FAKE_TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999888777")
    monkeypatch.setenv("TELEGRAM_ALERT_ON_SUCCESS", "true")

    notifier = TelegramNotifier()
    assert notifier.is_configured is True

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = json.dumps({"ok": True, "result": {"message_id": 42}}).encode(
            "utf-8"
        )
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = notifier.notify_curator_run(
            {
                "run_id": "r1",
                "is_healthy": True,
                "total_nodes": 5,
                "total_edges": 4,
                "orphan_count": 1,
                "broken_count": 0,
            }
        )
        assert res["ok"] is True
        assert res["message_id"] == 42


# ---------------------------------------------------------------------------
# 2. Lockfile & Collision Protection Tests
# ---------------------------------------------------------------------------


def test_curator_lock_lifecycle(tmp_path):
    """Verify CuratorLock acquire, collision rejection, and release."""
    lock_file = tmp_path / "test_curator.lock"
    lock = CuratorLock(lock_file)

    assert lock.acquire("run_1") is True
    assert lock_file.exists()

    # Second acquire should fail without force
    lock2 = CuratorLock(lock_file)
    assert lock2.acquire("run_2", force=False) is False

    # Force acquire should succeed
    assert lock2.acquire("run_2", force=True) is True

    lock2.release()
    assert not lock_file.exists()


def test_active_tasks_collision_deferral(monkeypatch):
    """Curator must defer execution when active agent tasks are running."""
    import server as botex_server

    fake_tasks = {
        "task_1": {"task_id": "task_1", "status": "RUNNING"},
    }
    with patch.object(botex_server, "_TASKS", fake_tasks):
        assert check_active_agent_tasks() is True


# ---------------------------------------------------------------------------
# 3. Deterministic Curation & Living Beacon Note Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_curator_full_cycle_and_zero_bloat(tmp_path, monkeypatch):
    """
    Test full curation cycle in a temporary workspace:
    1. Populates notes.
    2. Runs curation.
    3. Validates beacon note curator_health_status.md is created.
    4. Runs curation 2 more times and ensures no duplicate notes exist (Zero-Bloat Invariant).
    """
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    mem_dir = ws / ".botex" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)

    # Disable remote API keys for deterministic fallback testing
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    # Note 1: Architecture note linking to auth
    save_memory(
        workspace_dir=str(ws),
        memory_id="arch_overview",
        title="Architecture Overview",
        content="Overview of system. Uses [[auth_service]] for tokens.",
        kind="decision",
        tags=["arch", "core"],
    )

    # Note 2: Auth service note
    save_memory(
        workspace_dir=str(ws),
        memory_id="auth_service",
        title="Authentication Service",
        content="Bearer tokens and security protocol.",
        kind="context",
        tags=["auth", "security"],
    )

    # Note 3: Orphan note with tag 'security'
    save_memory(
        workspace_dir=str(ws),
        memory_id="orphan_guard",
        title="Security Guardrail",
        content="Taint checks and input sanitization.",
        kind="lesson",
        tags=["security", "audit"],
    )

    service = CuratorService(workspace_dir=str(ws))

    # Run 1
    res1 = await service.run(scope="local", force=True)
    assert res1.status == "COMPLETED"
    assert res1.is_healthy is True
    assert res1.total_nodes >= 3
    assert res1.orphan_count >= 1
    assert res1.broken_count == 0
    assert res1.tier_used == "deterministic"

    beacon_file = mem_dir / "curator_health_status.md"
    assert beacon_file.exists()
    content1 = beacon_file.read_text(encoding="utf-8")
    assert "Living Beacon Note" in content1
    assert "Raport Kuratora Grafu Wiedzy BoteX" in content1

    # Status and Run history files
    status_file = ws / ".botex" / "curator" / "status.json"
    assert status_file.exists()
    status_data = json.loads(status_file.read_text(encoding="utf-8"))
    assert status_data["is_healthy"] is True
    assert status_data["last_run_id"] == res1.run_id

    runs_dir = ws / ".botex" / "curator" / "runs"
    assert len(list(runs_dir.glob("*.json"))) == 1

    # Run 2 & Run 3: Verify Zero-Bloat Invariant (beacon note is updated in-place)
    time.sleep(0.05)
    res2 = await service.run(scope="local", force=True)
    assert res2.status == "COMPLETED"

    time.sleep(0.05)
    res3 = await service.run(scope="local", force=True)
    assert res3.status == "COMPLETED"

    # Verify run history records were preserved
    assert len(list(runs_dir.glob("*.json"))) == 3

    # CRITICAL: Verify Memory Vault contains exactly ONE beacon note, not multiple
    all_memories = search_memory(query="curator", workspace_dir=str(ws), scope="local")
    beacon_mems = [
        m for m in all_memories.get("memories", []) if m.get("id") == "curator_health_status"
    ]
    assert len(beacon_mems) == 1, (
        f"Zero-Bloat Invariant violated! Found {len(beacon_mems)} beacon notes in memory vault."
    )


# ---------------------------------------------------------------------------
# 4. Multi-Tier Fallback Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_llm_inference_primary_and_fallback(tmp_path, monkeypatch):
    """Verify tier progression from Nemotron mock to Local mock to Deterministic."""
    ws = tmp_path / "workspace_fallback"
    service = CuratorService(workspace_dir=str(ws))

    # Test Tier 1 mock returning valid JSON
    mock_json_reply = json.dumps(
        {
            "summary": "Graf wiedzy w stanie optymalnym z dwoma odrębnymi klastrami.",
            "cluster_themes": ["auth", "database"],
            "recommended_links": [
                {
                    "source_id": "orphan_a",
                    "target_id": "node_b",
                    "rationale": "Semantyczna zbieżność modułów.",
                }
            ],
            "orphan_insights": [],
        }
    )

    with patch.object(service, "_try_nemotron_inference", AsyncMock(return_value=mock_json_reply)):
        tier, analysis = await service._execute_batch_llm_analysis(
            health={"total_nodes": 5, "total_edges": 4, "broken_links": []},
            recent_nodes=[{"id": "node_b", "title": "Node B"}],
            orphan_nodes=[{"id": "orphan_a", "title": "Orphan A"}],
        )
        assert tier == "nvidia-nemotron"
        assert len(analysis["recommended_links"]) == 1
        assert analysis["recommended_links"][0]["source_id"] == "orphan_a"

    # Test Tier 1 returns None -> Falls to Tier 2 (Local)
    with (
        patch.object(service, "_try_nemotron_inference", AsyncMock(return_value=None)),
        patch.object(service, "_try_local_llm_inference", AsyncMock(return_value=mock_json_reply)),
    ):
        tier, analysis = await service._execute_batch_llm_analysis(
            health={"total_nodes": 5, "total_edges": 4, "broken_links": []},
            recent_nodes=[{"id": "node_b", "title": "Node B"}],
            orphan_nodes=[{"id": "orphan_a", "title": "Orphan A"}],
        )
        assert tier == "local-bielik"
        assert analysis["cluster_themes"] == ["auth", "database"]

    # Test Both Tier 1 and Tier 2 return None -> Falls to Tier 3 (Deterministic)
    with (
        patch.object(service, "_try_nemotron_inference", AsyncMock(return_value=None)),
        patch.object(service, "_try_local_llm_inference", AsyncMock(return_value=None)),
    ):
        tier, analysis = await service._execute_batch_llm_analysis(
            health={"total_nodes": 5, "total_edges": 4, "broken_links": []},
            recent_nodes=[{"id": "node_b", "title": "Auth Manager", "tags": ["auth"]}],
            orphan_nodes=[{"id": "orphan_a", "title": "Auth Token Helper", "tags": ["auth"]}],
        )
        assert tier == "deterministic"
        assert "Analiza deterministyczna" in analysis["summary"]
        assert len(analysis["recommended_links"]) >= 1


# ---------------------------------------------------------------------------
# 5. Cloud Gateway REST API Endpoints
# ---------------------------------------------------------------------------


def test_api_curator_endpoints(client, tmp_path):
    """Test GET /api/curator/status and POST /api/curator/run in FastAPI server_cloud."""
    ws = tmp_path / "api_workspace"
    ws.mkdir(parents=True, exist_ok=True)

    # 1. Status before run
    resp = client.get(f"/api/curator/status?workspace_dir={ws}")
    assert resp.status_code == 200
    status_data = resp.json()
    assert "is_healthy" in status_data
    assert "total_nodes" in status_data
    assert "lock_active" in status_data

    # 2. Trigger run
    run_payload = {
        "workspace_dir": str(ws),
        "scope": "local",
        "force": True,
        "notify_telegram": False,
    }
    post_resp = client.post("/api/curator/run", json=run_payload)
    assert post_resp.status_code == 200
    run_res = post_resp.json()
    assert run_res["status"] == "COMPLETED"
    assert "run_id" in run_res
    assert run_res["beacon_path"] != ""

    # 3. Status after run
    resp2 = client.get(f"/api/curator/status?workspace_dir={ws}")
    assert resp2.status_code == 200
    updated_status = resp2.json()
    assert updated_status["last_run_id"] == run_res["run_id"]
    assert updated_status["status"] in ("HEALTHY", "DEGRADED")


# ---------------------------------------------------------------------------
# 6. Hardening & Edge Case Regression Tests
# ---------------------------------------------------------------------------


def test_curator_lock_non_owner_release_protection(tmp_path):
    """A non-owner CuratorLock instance must not delete another process's lock file."""
    lock_file = tmp_path / "protected.lock"
    # Process 1 acquires lock
    lock1 = CuratorLock(lock_file)
    assert lock1.acquire("run_owner") is True
    assert lock_file.exists()

    # Process 2 (did not acquire) tries to release
    lock2 = CuratorLock(lock_file)
    lock2.release(force=False)
    # Lockfile MUST still exist
    assert lock_file.exists()

    # Process 1 legitimately releases
    lock1.release()
    assert not lock_file.exists()


def test_telegram_send_fallback_on_parse_error(monkeypatch):
    """If Telegram returns HTTP 400 due to bad markdown, send must retry as plain text."""
    import urllib.error

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")

    calls = []

    def mock_urlopen(req, timeout=10.0):
        data = json.loads(req.data.decode("utf-8"))
        calls.append(data)
        if data.get("parse_mode") == "Markdown":
            # Simulate Telegram 400 Bad Request: can't parse entities
            err = urllib.error.HTTPError(
                url=req.full_url,
                code=400,
                msg="Bad Request: can't parse entities",
                hdrs={},
                fp=MagicMock(
                    read=lambda: (
                        b'{"ok": false, "description": "Bad Request: can\'t parse entities"}'
                    )
                ),
            )
            raise err
        # Fallback without parse_mode succeeds
        resp = MagicMock()
        resp.getcode.return_value = 200
        resp.read.return_value = b'{"ok": true, "result": {"message_id": 999}}'
        resp.__enter__.return_value = resp
        return resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        res = send_telegram_notification("Test *message with _broken tag")
        assert res["ok"] is True
        assert res["message_id"] == 999
        # Verified 2 calls: first with Markdown, second with empty parse_mode
        assert len(calls) == 2
        assert calls[0]["parse_mode"] == "Markdown"
        assert calls[1]["parse_mode"] == ""


def test_orphan_tags_preserved_in_graph_health(tmp_path):
    """Verify that find_graph_health properly populates tags on orphan notes."""
    from botex.memory import find_graph_health

    ws = tmp_path / "ws_tags"
    ws.mkdir(parents=True, exist_ok=True)
    save_memory(
        workspace_dir=str(ws),
        memory_id="tagged_orphan",
        title="Tagged Orphan Note",
        content="No links anywhere.",
        tags=["audit", "security-review"],
    )
    health = find_graph_health(workspace_dir=str(ws), scope="local")
    orphans = health.get("orphans", [])
    assert len(orphans) == 1
    assert "audit" in orphans[0].get("tags", [])
    assert "security-review" in orphans[0].get("tags", [])


@pytest.mark.anyio
async def test_llm_json_parsing_with_preamble(tmp_path):
    """LLM output with conversational preamble before JSON must still parse cleanly."""
    ws = tmp_path / "ws_preamble"
    service = CuratorService(workspace_dir=str(ws))

    conversational_output = (
        "Here is my analysis of the graph topology:\n\n"
        "{\n"
        '  "summary": "Graf wiedzy wykazuje silną spójność.",\n'
        '  "cluster_themes": ["telemetry"],\n'
        '  "recommended_links": [],\n'
        '  "orphan_insights": []\n'
        "}\n\n"
        "Let me know if you need anything else!"
    )

    with patch.object(
        service, "_try_nemotron_inference", AsyncMock(return_value=conversational_output)
    ):
        tier, analysis = await service._execute_batch_llm_analysis(
            health={"total_nodes": 2, "total_edges": 1, "broken_links": []},
            recent_nodes=[],
            orphan_nodes=[],
        )
        assert tier == "nvidia-nemotron"
        assert analysis["summary"] == "Graf wiedzy wykazuje silną spójność."
        assert analysis["cluster_themes"] == ["telemetry"]


@pytest.mark.anyio
async def test_curator_harvest_inactive_chat_sessions(tmp_path, monkeypatch):
    """Curator harvests inactive chat sessions (>24h, >=6 messages) once into Memory Vault."""
    monkeypatch.delenv("BOTEX_CHAT_SESSIONS_DIR", raising=False)
    ws = tmp_path / "ws_harvest"
    chat_dir = ws / ".botex" / "chat_sessions"
    chat_dir.mkdir(parents=True, exist_ok=True)

    old_ts = time.time() - 90000.0
    sess_payload = {
        "session_id": "chat_abc12345",
        "title": "VPS Context Guard Architecture",
        "updated_at": old_ts,
        "messages": [
            {"role": "user", "content": "How should we handle 64k context overflow?"},
            {"role": "assistant", "content": "Use in-process tool pruning and token budgets."},
            {"role": "user", "content": "Should we keep full history on disk?"},
            {
                "role": "assistant",
                "content": "Yes, keep 100% on disk and slice only the wire payload.",
            },
            {"role": "user", "content": "What about OpenRouter?"},
            {"role": "assistant", "content": "Enable context-compression plugin as a safety net."},
        ],
    }
    (chat_dir / "chat_abc12345.json").write_text(
        json.dumps(sess_payload, indent=2), encoding="utf-8"
    )

    service = CuratorService(workspace_dir=str(ws))
    harvested = await service.harvest_inactive_chat_sessions()
    assert len(harvested) == 1
    assert harvested[0]["memory_id"] == "chat_harvest_chat_abc12345"

    note = read_memory(memory_id="chat_harvest_chat_abc12345", workspace_dir=str(ws), scope="local")
    assert note.get("ok") is True
    assert "VPS Context Guard Architecture" in note.get("content", "")

    # Second run skips already harvested session with unchanged updated_at
    harvested_again = await service.harvest_inactive_chat_sessions()
    assert harvested_again == []


def test_local_llm_inference_passes_decision_mode_for_bielik(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import types
    from botex.ops.curator import curator_service as cs

    captured = {}

    class DummySocket:
        def settimeout(self, t):
            pass

        def connect(self, addr):
            pass

        def close(self):
            pass

    class DummyCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            msg = types.SimpleNamespace(content='{"summary": "ok"}')
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    class DummyClient:
        def __init__(self, **kwargs):
            captured["client_timeout"] = kwargs.get("timeout")
            self.chat = types.SimpleNamespace(completions=DummyCompletions())

    import socket as real_socket

    monkeypatch.setattr(
        cs,
        "socket",
        types.SimpleNamespace(
            AF_INET=real_socket.AF_INET,
            SOCK_STREAM=real_socket.SOCK_STREAM,
            socket=lambda *a, **kw: DummySocket(),
        ),
    )
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", DummyClient)

    service = CuratorService(workspace_dir=str(tmp_path), local_model="bielik:latest")
    out = asyncio.run(service._try_local_llm_inference("test prompt"))
    assert out == '{"summary": "ok"}'
    assert captured["client_timeout"] == 45.0
    assert captured["extra_body"] == {
        "chat_template_kwargs": {"decision_mode": True, "reasoning_effort": "none"}
    }
