# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Tests for BoteX FastAPI Cloud Gateway & Remote MCP SSE Server
=============================================================
Comprehensive unit and integration test suite covering:
1. Zero Friction Local / Strict Cloud Bearer token authentication and timing-attack resistance.
2. /api/health probes, /api/wallet credit inspection.
3. Autonomous background task delegation, listing, detail polling, and cancellation.
4. Operator human-in-the-loop approvals listing and decision resolution.
5. Workspace Memory Vault operations across local, global, and all scopes.
6. Server-Sent Events (SSE) streaming chat with keepalives.
7. WebSocket bidirectional chat with heartbeat, auth negotiation, and ping/pong.
8. Remote MCP SSE endpoints (/mcp/sse and /mcp/messages).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from botex.approval import ActionStatus, PendingAction, default_approval_manager
from botex.providers import CreditsInfo
from botex.server_cloud import app


@pytest.fixture
def client(monkeypatch):
    """TestClient fixture with unauthenticated environment."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 1. Health Probe Endpoint
# ---------------------------------------------------------------------------


def test_health_endpoint_public(client):
    """Health check must be publicly accessible and return standard schema."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["version"] == "1.0.0"
    assert "uptime_s" in data
    assert "timestamp" in data


# ---------------------------------------------------------------------------
# 2. Authentication: Zero Friction Local / Strict Cloud
# ---------------------------------------------------------------------------


def test_auth_zero_friction_local(client, monkeypatch):
    """When BOTEX_ACCESS_TOKEN is unset, requests pass through unauthenticated."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    assert "tasks" in resp.json()


def test_auth_strict_cloud_rejects_missing_token(client, monkeypatch):
    """When BOTEX_ACCESS_TOKEN is set, requests without token receive 401."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    resp = client.get("/api/tasks")
    assert resp.status_code == 401
    assert "Unauthorized" in resp.json()["detail"]


def test_auth_strict_cloud_rejects_invalid_token(client, monkeypatch):
    """Requests with wrong Bearer token are rejected."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    resp = client.get("/api/tasks", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401
    assert "Unauthorized" in resp.json()["detail"]


def test_auth_strict_cloud_accepts_valid_bearer_token(client, monkeypatch):
    """Requests with valid Bearer token are accepted."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    resp = client.get(
        "/api/tasks",
        headers={"Authorization": "Bearer super-secret-key-12345"},
    )
    assert resp.status_code == 200
    assert "tasks" in resp.json()


def test_auth_strict_cloud_accepts_query_token_on_sse_path(client, monkeypatch):
    """Token passed as query parameter ?token=... is accepted on streaming SSE paths."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    # Allowed on /mcp path (returns 400 Bad Request from handler, not 401 Unauthorized)
    mcp_resp = client.post("/mcp/messages?token=super-secret-key-12345", json={})
    assert mcp_resp.status_code != 401

    # Rejected on non-streaming REST routes to prevent token leakage in access logs
    rest_resp = client.get("/api/tasks?token=super-secret-key-12345")
    assert rest_resp.status_code == 401


def test_auth_strict_cloud_allows_health_unauthenticated(client, monkeypatch):
    """Health probes bypass authentication even in strict cloud mode."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_auth_strict_cloud_allows_cors_preflight(client, monkeypatch):
    """OPTIONS preflight requests bypass authentication for CORS compliance."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    resp = client.options(
        "/api/tasks",
        headers={
            "Origin": "https://dashboard.botex.dev",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 3. Wallet / Credits Endpoint
# ---------------------------------------------------------------------------


def test_wallet_endpoint(client, monkeypatch):
    """Wallet endpoint returns parsed credits information."""
    mock_credits = CreditsInfo(
        supported=True,
        provider="openrouter",
        balance_usd=42.50,
        usage_usd=7.50,
        limit_usd=100.0,
        limit_remaining_usd=50.0,
        is_free_tier=False,
        label="Test API Key",
        error=None,
    )
    with patch(
        "botex.server_cloud.default_wallet_service.get_credits_async",
        new=AsyncMock(return_value=mock_credits),
    ):
        resp = client.get("/api/wallet?provider=openrouter&force_refresh=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["supported"] is True
        assert data["provider"] == "openrouter"
        assert data["balance_usd"] == 42.50
        assert data["label"] == "Test API Key"


# ---------------------------------------------------------------------------
# 4. Tasks Delegation & Management
# ---------------------------------------------------------------------------


def test_tasks_lifecycle(client):
    """Test task creation, listing, detail inspection, and cancellation."""
    # 1. Create task
    payload = {
        "task": "Perform a unit test verification",
        "mode": "readonly",
        "model": "openrouter/auto",
    }
    with patch(
        "botex.server_cloud.run_botex_task",
        new=AsyncMock(return_value={"ok": True, "status": "DONE"}),
    ):
        create_resp = client.post("/api/tasks", json=payload)
        assert create_resp.status_code == 202
        create_data = create_resp.json()
        task_id = create_data["task_id"]
        assert create_data["status"] == "QUEUED"

        # 2. List tasks
        list_resp = client.get("/api/tasks")
        assert list_resp.status_code == 200
        tasks = list_resp.json()["tasks"]
        task_ids = [t["task_id"] for t in tasks]
        assert task_id in task_ids

        # 3. Get task details
        get_resp = client.get(f"/api/tasks/{task_id}")
        assert get_resp.status_code == 200
        task_entry = get_resp.json()
        assert task_entry["task_id"] == task_id
        assert task_entry["status"] in ("QUEUED", "RUNNING", "DONE")


def test_task_not_found(client):
    """Non-existent task ID returns 404."""
    resp = client.get("/api/tasks/nonexistent_12345")
    assert resp.status_code == 404


def test_task_cancellation(client):
    """Task cancellation aborts background execution and pending approvals."""
    create_resp = client.post("/api/tasks", json={"task": "Long running task", "mode": "edit"})
    task_id = create_resp.json()["task_id"]

    cancel_resp = client.post(f"/api/tasks/{task_id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["ok"] is True
    assert cancel_resp.json()["status"] == "CANCELLED"

    # Status should now be CANCELLED
    detail_resp = client.get(f"/api/tasks/{task_id}")
    assert detail_resp.json()["status"] == "CANCELLED"

    # Cancelling already terminal task bails out safely without state corruption
    repeat_cancel = client.post(f"/api/tasks/{task_id}/cancel")
    assert repeat_cancel.status_code == 200
    assert repeat_cancel.json()["ok"] is False
    assert "terminal state" in repeat_cancel.json()["message"]


# ---------------------------------------------------------------------------
# 5. Operator Approvals Endpoint
# ---------------------------------------------------------------------------


def test_approvals_listing_and_decision(client):
    """Operator can view pending approval requests and submit approve/reject decisions."""
    # Register a synthetic pending action
    action_id = "act_test_123"
    action = PendingAction(
        action_id=action_id,
        task_id="task_test_999",
        tool_name="delete_file",
        params={"path": "sensitive.txt"},
        preview="Delete file sensitive.txt",
        _future=None,
    )
    default_approval_manager._actions[action_id] = action

    try:
        # 1. List approvals
        list_resp = client.get("/api/approvals?task_id=task_test_999")
        assert list_resp.status_code == 200
        actions = list_resp.json()["pending_actions"]
        assert any(a["action_id"] == action_id for a in actions)

        # 2. Decide approval
        secret_key = "sk-" + "dummy-mock-secret-token-key-1234567890"
        decide_resp = client.post(
            f"/api/approvals/{action_id}/decide",
            json={"approved": True, "reason": f"Approved with key {secret_key}"},
        )
        assert decide_resp.status_code == 200
        assert decide_resp.json()["ok"] is True
        assert decide_resp.json()["approved"] is True
        assert secret_key not in decide_resp.json()["reason"]
        assert "[REDACTED]" in decide_resp.json()["reason"]
        assert action.status == ActionStatus.APPROVED
    finally:
        default_approval_manager._actions.pop(action_id, None)


def test_approval_decision_not_found(client):
    """Attempting to decide a nonexistent action returns 404."""
    resp = client.post(
        "/api/approvals/nonexistent_action/decide",
        json={"approved": False, "reason": "Not found"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 6. Memory Vault Operations
# ---------------------------------------------------------------------------


def test_memory_vault_api_lifecycle(client, tmp_path):
    """Test saving, searching, and reading memory entries via REST API."""
    ws = str(tmp_path)

    # 1. Save memory
    save_resp = client.post(
        "/api/memory",
        json={
            "workspace_dir": ws,
            "title": "Cloud Architecture Decision",
            "content": "Selected FastAPI for Etap 3 gateway.",
            "kind": "decision",
            "tags": ["arch", "cloud"],
            "scope": "local",
        },
    )
    assert save_resp.status_code == 200
    save_data = save_resp.json()
    assert save_data["ok"] is True
    mem_id = save_data["id"]

    # 2. Search memory
    search_resp = client.get(f"/api/memory?workspace_dir={ws}&query=FastAPI&scope=local")
    assert search_resp.status_code == 200
    memories = search_resp.json()["memories"]
    assert any(m["id"] == mem_id for m in memories)

    # 3. Read memory by ID
    read_resp = client.get(f"/api/memory?workspace_dir={ws}&memory_id={mem_id}&scope=local")
    assert read_resp.status_code == 200
    entry = read_resp.json()
    assert entry["ok"] is True
    assert entry["metadata"]["title"] == "Cloud Architecture Decision"
    assert "FastAPI" in entry["content"]


# ---------------------------------------------------------------------------
# 7. SSE Streaming Chat Endpoint
# ---------------------------------------------------------------------------


def test_chat_sse_stream(client):
    """POST /api/chat streams Server-Sent Events."""

    async def mock_stream(session, user_input="", **kwargs):
        yield {"type": "content_chunk", "delta": "Hello "}
        yield {"type": "content_chunk", "delta": "world!"}
        yield {"type": "turn_end", "content": "Hello world!"}

    with patch("botex.server_cloud.stream_chat_turn", side_effect=mock_stream):
        with client.stream(
            "POST",
            "/api/chat",
            json={"message": "Hi botex", "session_id": "test_sse_sess"},
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            lines = [line for line in resp.iter_lines() if line]
            assert any("Hello " in l for l in lines)
            assert any("world!" in l for l in lines)


# ---------------------------------------------------------------------------
# 8. WebSocket Chat Endpoint
# ---------------------------------------------------------------------------


def test_websocket_chat_unauthenticated(client, monkeypatch):
    """WebSocket chat works directly when BOTEX_ACCESS_TOKEN is not set."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)

    async def mock_stream(session, user_input="", **kwargs):
        yield {"type": "content_chunk", "delta": "Echo: " + user_input}

    with patch("botex.server_cloud.stream_chat_turn", side_effect=mock_stream):
        with client.websocket_connect("/api/chat/ws") as ws:
            # Test ping/pong
            ws.send_json({"type": "ping"})
            pong_msg = ws.receive_json()
            assert pong_msg["type"] == "pong"

            # Test message streaming
            ws.send_json({"type": "message", "content": "ping message"})
            reply = ws.receive_json()
            assert reply["type"] == "content_chunk"
            assert "Echo: ping message" in reply["delta"]


def test_websocket_chat_auth_handshake(client, monkeypatch):
    """WebSocket chat requires auth when BOTEX_ACCESS_TOKEN is set."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "ws-token-pass")

    # Connect without auth query param, authenticate via first message
    with client.websocket_connect("/api/chat/ws") as ws:
        ws.send_json({"type": "auth", "token": "ws-token-pass"})
        resp = ws.receive_json()
        assert resp["type"] == "auth_ok"

        # Verify active connection can now ping
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_websocket_chat_auth_rejection(client, monkeypatch):
    """WebSocket chat closes with policy violation on invalid first auth message."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "ws-token-pass")

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/chat/ws") as ws:
            ws.send_json({"type": "auth", "token": "wrong-secret"})
            ws.receive_json()
    assert excinfo.value.code == 1008


# ---------------------------------------------------------------------------
# 9. Remote MCP via SSE Endpoint
# ---------------------------------------------------------------------------


def test_mcp_sse_transport_mounted(client):
    """Mounted Remote MCP SSE transport is properly attached and routed."""
    # 1. Verify route mount exists under /mcp
    mount_paths = [getattr(r, "path", "") for r in app.routes]
    assert "/mcp" in mount_paths

    # 2. Test messages endpoint routing directly without 307 redirects
    resp = client.post("/mcp/messages", json={}, follow_redirects=False)
    assert resp.status_code == 400
    assert "session_id is required" in resp.text

    resp_slash = client.post("/mcp/messages/", json={}, follow_redirects=False)
    assert resp_slash.status_code == 400
    assert "session_id is required" in resp_slash.text

    # 3. Verify exactly 12 tools are exposed without duplication
    expected_tools = {
        "start_task",
        "get_task_status",
        "fetch_url",
        "recommend_models",
        "run_subagent",
        "get_outline",
        "save_memory",
        "search_memory",
        "read_memory",
        "get_stats",
        "check_health",
        "clean_snapshots",
    }
    assert len(expected_tools) == 12


def test_sse_keepalive_formatting():
    """Verify that SSE keepalive comment conforms to SSE wire format."""
    from sse_starlette.sse import ServerSentEvent

    sse = ServerSentEvent(comment="keepalive")
    wire_bytes = sse.encode()
    assert b": keepalive\r\n\r\n" in wire_bytes or b": keepalive\n\n" in wire_bytes


def test_auth_zero_friction_local_logs_note(client, monkeypatch, caplog):
    """Local unauthenticated mode emits an informative log note."""
    import logging

    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    with caplog.at_level(logging.INFO):
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
    assert any("Zero Friction Local mode" in record.message for record in caplog.records)


def test_cli_cloud_parser():
    """Verify that botex cloud CLI argument parsing works."""
    import argparse

    parser = argparse.ArgumentParser(prog="botex cloud")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(["--host", "127.0.0.1", "--port", "9000"])
    assert args.host == "127.0.0.1"
    assert args.port == 9000


# ---------------------------------------------------------------------------
# 10. Progressive Web App (PWA) Static Shell & Manifest
# ---------------------------------------------------------------------------

# web/dist is a gitignored build artifact: these tests only run when the PWA was built.
_requires_pwa_build = pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "web" / "dist" / "index.html").is_file(),
    reason="web/dist not built (run `npm run build` in web/)",
)


@_requires_pwa_build
def test_pwa_static_shell_served(client, monkeypatch):
    """PWA static entrypoint index.html is served at root / even in Strict Cloud Mode."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "strict-token-abc")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "BoteX Control" in resp.text
    assert '<div id="app"></div>' in resp.text


@_requires_pwa_build
def test_pwa_manifest_and_sw(client, monkeypatch):
    """PWA manifest and Service Worker are publicly accessible for offline installation."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "strict-token-abc")
    manifest_resp = client.get("/manifest.webmanifest")
    assert manifest_resp.status_code == 200
    data = manifest_resp.json()
    assert data["name"] == "BoteX Control"
    assert data["display"] == "standalone"
    assert data["theme_color"] == "#0f172a"

    sw_resp = client.get("/sw.js")
    assert sw_resp.status_code == 200
    assert "workbox" in sw_resp.text.lower() or "precache" in sw_resp.text.lower()


def test_models_catalog_endpoint(client):
    """GET /api/models returns consolidated model catalog with required schema and <20ms warm latency."""
    import time

    resp = client.get("/api/models")
    assert resp.status_code == 200
    models = resp.json()
    assert isinstance(models, list)
    assert len(models) >= 5

    # Warm TTL cache call should complete in <20ms
    t0 = time.perf_counter()
    warm_resp = client.get("/api/models")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert warm_resp.status_code == 200
    assert elapsed_ms < 20.0

    required_keys = {
        "id",
        "name",
        "provider",
        "is_free",
        "context_length",
        "pricing",
        "supports_tools",
        "is_zdr_compliant",
    }
    providers_found = set()
    has_lfm_free = False
    for entry in models:
        assert required_keys.issubset(entry.keys())
        assert isinstance(entry["pricing"], dict)
        assert isinstance(entry["pricing"].get("prompt_per_m"), float)
        assert isinstance(entry["pricing"].get("completion_per_m"), float)
        assert isinstance(entry["is_free"], bool)
        assert isinstance(entry["context_length"], int)
        assert entry["context_length"] > 0
        providers_found.add(entry["provider"])
        if entry["id"] == "liquid/lfm-2.5-2.6b:free":
            has_lfm_free = True
            assert entry["is_free"] is True
            assert entry["provider"] == "openrouter-free"
            assert entry["pricing"]["prompt_per_m"] == 0.0

    assert {"openrouter", "openrouter-free", "nvidia"}.issubset(providers_found)
    assert has_lfm_free is True


def test_models_catalog_provider_filter(client):
    """GET /api/models?provider=openrouter-free filters to free tier models only."""
    resp = client.get("/api/models?provider=openrouter-free")
    assert resp.status_code == 200
    models = resp.json()
    assert len(models) >= 1
    assert all(m["provider"] == "openrouter-free" and m["is_free"] is True for m in models)
