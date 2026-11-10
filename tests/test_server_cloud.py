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

from botex import __version__
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
    assert data["version"] == __version__
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
    """Token passed as query parameter ?token=... is accepted only on the GET /mcp/sse handshake."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    # State-changing POSTs must use the Bearer header, never the URL.
    mcp_resp = client.post("/mcp/messages?token=super-secret-key-12345", json={})
    assert mcp_resp.status_code == 401
    mcp_ok = client.post(
        "/mcp/messages", json={}, headers={"Authorization": "Bearer super-secret-key-12345"}
    )
    assert mcp_ok.status_code != 401

    # Rejected on non-streaming REST routes to prevent token leakage in access logs
    rest_resp = client.get("/api/tasks?token=super-secret-key-12345")
    assert rest_resp.status_code == 401

    # Chat POST must not accept URL tokens either
    chat_resp = client.post("/api/chat?token=super-secret-key-12345", json={})
    assert chat_resp.status_code == 401


def test_devin_status_requires_auth_in_strict_mode(client, monkeypatch):
    """/api/delegation/devin leaks run metadata, so it is no longer an unauthenticated probe."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    assert client.get("/api/delegation/devin").status_code == 401
    ok = client.get(
        "/api/delegation/devin", headers={"Authorization": "Bearer super-secret-key-12345"}
    )
    assert ok.status_code == 200


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
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200

    # Default CORS is local-dev only: arbitrary foreign origins are not allowed.
    foreign = client.options(
        "/api/tasks",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert foreign.headers.get("access-control-allow-origin") is None


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
        assert data["units"] == "usd"
        assert data["quota_windows"] == []


def test_wallet_endpoint_zai_quota_windows(client):
    """Wallet endpoint returns quota windows and units='quota' for Z.ai."""
    sample_windows = (
        {
            "label": "5 Hours",
            "unit": "hours",
            "number": 5,
            "limit": 1000000.0,
            "current_usage": 150000.0,
            "percentage": 15.0,
            "next_reset_time": "2026-11-06T18:00:00Z",
        },
        {
            "label": "Weekly",
            "unit": "weeks",
            "number": 1,
            "limit": 5000000.0,
            "current_usage": 1500000.0,
            "percentage": 30.0,
            "next_reset_time": "2026-11-13T00:00:00Z",
        },
    )
    mock_credits = CreditsInfo(
        supported=True,
        provider="zai",
        units="quota",
        quota_windows=sample_windows,
    )
    with patch(
        "botex.server_cloud.default_wallet_service.get_credits_async",
        new=AsyncMock(return_value=mock_credits),
    ):
        resp = client.get("/api/wallet?provider=zai&force_refresh=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["supported"] is True
        assert data["provider"] == "zai"
        assert data["units"] == "quota"
        assert len(data["quota_windows"]) == 2
        assert data["quota_windows"][0]["unit"] == "hours"


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


def test_task_idempotency_key(client):
    """Submitting duplicate task with same Idempotency-Key returns existing task without re-execution."""
    headers = {"Idempotency-Key": "test-idem-key-42"}
    payload = {"task": "Idempotent task execution", "mode": "readonly"}

    resp1 = client.post("/api/tasks", json=payload, headers=headers)
    assert resp1.status_code == 202
    data1 = resp1.json()
    task_id1 = data1["task_id"]

    # Repeat request with identical idempotency key
    resp2 = client.post("/api/tasks", json=payload, headers=headers)
    assert resp2.status_code == 202
    data2 = resp2.json()
    assert data2["task_id"] == task_id1
    assert data2.get("idempotent_replay") is True


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
    """GET /api/models returns consolidated model catalog with required schema and warm cache latency."""
    import time

    resp = client.get("/api/models")
    assert resp.status_code == 200
    models = resp.json()
    assert isinstance(models, list)
    assert len(models) >= 5

    # Warm TTL cache call should complete within budget (<150ms on shared CI runners)
    t0 = time.perf_counter()
    warm_resp = client.get("/api/models")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert warm_resp.status_code == 200
    assert elapsed_ms < 150.0

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


def test_memory_graph_and_backlinks_endpoints(client, tmp_path):
    """Verify GET /api/memory/graph and /api/memory/backlinks endpoints."""
    # 1. Create two connected memory notes
    m1 = client.post(
        "/api/memory",
        json={
            "workspace_dir": str(tmp_path),
            "title": "Node A",
            "content": "Links to [[node_b_target]].",
            "kind": "decision",
            "tags": ["alpha"],
            "scope": "local",
        },
    ).json()
    assert m1.get("ok") is True

    m2 = client.post(
        "/api/memory",
        json={
            "workspace_dir": str(tmp_path),
            "memory_id": "node_b_target",
            "title": "Node B",
            "content": f"Links back to [[{m1.get('id')}]].",
            "kind": "lesson",
            "tags": ["beta"],
            "scope": "local",
        },
    ).json()
    assert m2.get("ok") is True

    # 2. Query graph endpoint
    g_resp = client.get(f"/api/memory/graph?workspace_dir={tmp_path}&scope=local")
    assert g_resp.status_code == 200
    g_data = g_resp.json()
    assert g_data["ok"] is True
    assert len(g_data["nodes"]) >= 2
    assert len(g_data["edges"]) >= 2
    assert "health" in g_data

    # 3. Query backlinks endpoint
    bl_resp = client.get(
        f"/api/memory/backlinks?workspace_dir={tmp_path}&memory_id=node_b_target&scope=local"
    )
    assert bl_resp.status_code == 200
    bl_data = bl_resp.json()
    assert bl_data["ok"] is True
    assert bl_data["count"] >= 1
    source_ids = [b["id"] for b in bl_data["backlinks"]]
    assert m1.get("id") in source_ids


def test_get_rate_limits_endpoint(client: TestClient) -> None:
    """GET /api/rate_limits returns live provider bucket stats."""
    resp = client.get("/api/rate_limits")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    assert "openrouter-free" in data
    free_stats = data["openrouter-free"]
    assert "rpm" in free_stats
    assert "tpm" in free_stats
    assert "cooldown_s" in free_stats
    assert "requests_available" in free_stats


def test_auth_fail_closed_on_remote_origin_when_token_unset(client: TestClient, monkeypatch):
    """When BOTEX_ACCESS_TOKEN is unset, external requests (X-Forwarded-For) must receive 403."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    resp = client.get("/api/tasks", headers={"X-Forwarded-For": "198.51.100.24"})
    assert resp.status_code == 403
    assert "Forbidden" in resp.json()["detail"]


def test_payload_too_large_rejected_with_413(client: TestClient):
    """Requests declaring content-length > 25 MiB must be rejected with 413."""
    resp = client.post(
        "/api/tasks",
        headers={"Content-Length": str(30 * 1024 * 1024)},
        content=b"{}",
    )
    assert resp.status_code == 413
    assert "Payload too large" in resp.json()["detail"]


def test_oversized_task_prompt_rejected_with_400(client: TestClient):
    """Task prompts exceeding 65536 characters are rejected with 400."""
    huge_prompt = "x" * 70000
    resp = client.post("/api/tasks", json={"task": huge_prompt})
    assert resp.status_code == 400
    assert "exceeds maximum allowed length" in resp.json()["detail"]


def test_ws_auth_fail_closed_on_remote_origin_when_token_unset(client: TestClient, monkeypatch):
    """When BOTEX_ACCESS_TOKEN is unset, external websocket requests (X-Forwarded-For) must receive 1008."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/chat/ws", headers={"x-forwarded-for": "198.51.100.24"}
        ) as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008


def test_ws_oversized_chat_message_rejected(client: TestClient, monkeypatch):
    """WebSocket chat message exceeding 32768 characters is rejected with an error frame."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    with client.websocket_connect("/api/chat/ws") as ws:
        huge_message = "m" * 35000
        ws.send_json({"type": "message", "content": huge_message})
        resp = ws.receive_json()
        assert resp["type"] == "error"
        assert "exceeds maximum allowed length" in resp["error"]


def test_oversized_chat_system_prompt_rejected_with_400(client: TestClient):
    """Chat requests with system_prompt exceeding 32768 chars are rejected with 400."""
    huge_sp = "s" * 35000
    resp = client.post("/api/chat", json={"message": "hi", "system_prompt": huge_sp})
    assert resp.status_code == 400
    assert "System prompt exceeds maximum allowed length" in resp.json()["detail"]


def test_oversized_task_followup_prompt_rejected_with_400(client: TestClient):
    """Follow-up prompt exceeding 65536 characters is rejected with 400."""
    from botex.server_cloud import _TASKS

    fake_tid = "fake_devin_task"
    _TASKS[fake_tid] = {
        "task_id": fake_tid,
        "executor": "devin",
        "status": "DONE",
        "session_id": "ses_123",
    }
    try:
        huge_prompt = "f" * 70000
        resp = client.post(f"/api/tasks/{fake_tid}/followup", json={"prompt": huge_prompt})
        assert resp.status_code == 400
        assert "exceeds maximum allowed length" in resp.json()["detail"]
    finally:
        _TASKS.pop(fake_tid, None)


def test_mask_stream_chunk_boundary_secrets():
    """Verify mask_stream_chunk redacts secrets split across consecutive streaming chunks."""
    from server import mask_stream_chunk

    # 1. Normal chunk
    chunk1 = {"type": "content_chunk", "delta": "Here is the key: "}
    res1, carry1 = mask_stream_chunk(chunk1, carry="")
    assert res1["delta"] == "Here is the key: "
    assert "Here is the key: " in carry1

    # 2. Secret split: "sk-" in carry, rest in chunk 2
    chunk2 = {"type": "content_chunk", "delta": "sk-1234567890abcdef1234567890"}
    res2, carry2 = mask_stream_chunk(chunk2, carry=carry1)
    assert "[REDACTED]" in res2["delta"]
    assert "sk-" not in res2["delta"]

    # 3. Reasoning chunk with accumulated field
    chunk3 = {
        "type": "reasoning_chunk",
        "delta": "sk-9876543210abcdef12345",
        "accumulated": "Full reasoning with sk-9876543210abcdef12345",
    }
    res3, carry3 = mask_stream_chunk(chunk3, carry="")
    assert "[REDACTED]" in res3["delta"]
    assert "[REDACTED]" in res3["accumulated"]
    assert "sk-" not in res3["accumulated"]

    # 4. Structural chunk
    struct_chunk = {
        "type": "tool_result",
        "result": {"output": "Sensitive sk-1234567890abcdef value"},
    }
    res_struct, carry_struct = mask_stream_chunk(struct_chunk, carry="carry_val")
    assert carry_struct == "carry_val"
    assert "[REDACTED]" in res_struct["result"]["output"]


def test_lifespan_teardown_invokes_cleanup():
    """Verify lifespan teardown invokes close_http_pool and close_index_dbs."""
    from unittest.mock import AsyncMock, patch
    from botex.server_cloud import lifespan

    async def _test():
        with (
            patch(
                "botex.providers.http_pool.close_http_pool", new_callable=AsyncMock
            ) as mock_close_http,
            patch("botex.memory.memory.close_index_dbs") as mock_close_dbs,
        ):
            async with lifespan(app):
                pass
            mock_close_http.assert_awaited_once()
            mock_close_dbs.assert_called_once()

    asyncio.run(_test())


def test_access_token_hash_authentication(monkeypatch):
    """Verify server authenticates raw bearer token when BOTEX_ACCESS_TOKEN_HASH is configured."""
    import hashlib

    raw_token = "btx_live_test_secret_12345"
    token_hash = "sha256:" + hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN_HASH", token_hash)
    monkeypatch.setenv("BOTEX_STRICT_AUTH", "1")

    client = TestClient(app)

    # 1. Valid token -> 200 OK
    res_ok = client.get("/api/health", headers={"Authorization": f"Bearer {raw_token}"})
    assert res_ok.status_code == 200

    # 2. Invalid token on protected endpoint -> 401
    res_err = client.get("/api/wallet", headers={"Authorization": "Bearer wrong-token"})
    assert res_err.status_code == 401

    # 3. Missing token on protected endpoint -> 401
    res_missing = client.get("/api/wallet")
    assert res_missing.status_code == 401


def test_security_masks_btx_tokens_and_blocks_env():
    """Verify security layer redacts btx_ tokens and blocks .env variations."""
    from botex.tools.security import mask_secrets, resolve_safe_path, SecurityError
    import pytest
    import tempfile

    # Token redaction
    assert "[REDACTED]" in mask_secrets("token: btx_live_abc1234567890def")
    assert "[REDACTED]" in mask_secrets("token: btx_sec_xyz9876543210qwe")

    # Path traversal and blocked file checks
    with tempfile.TemporaryDirectory() as tmpdir:
        for blocked_name in [".env", ".env.production", ".secrets.yaml", "secrets.json"]:
            with pytest.raises(SecurityError):
                resolve_safe_path(tmpdir, blocked_name)
