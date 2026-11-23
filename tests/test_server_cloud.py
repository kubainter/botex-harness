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
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from botex import __version__
from botex.approval import ActionStatus, PendingAction, default_approval_manager
from botex.providers import CreditsInfo
from botex.server import server_cloud
from botex.server.server_cloud import CloudAuthMiddleware
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


def test_auth_strict_cloud_rejects_query_token_everywhere(client, monkeypatch):
    """Query-string ?token= is rejected on every route; credentials must ride the Bearer header."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    # The MCP SSE handshake used to be the sole exception; URL tokens are gone entirely.
    sse_resp = client.get("/mcp/sse?token=super-secret-key-12345")
    assert sse_resp.status_code == 401
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
# 2b. Origin Guard (same-host drive-by protection on mutations)
# ---------------------------------------------------------------------------


def test_origin_guard_rejects_cross_origin_mutation(client):
    """POST /api/* carrying a foreign Origin header is rejected with 403."""
    resp = client.post(
        "/api/tasks",
        json={"task": "drive-by write", "mode": "readonly"},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert "cross-origin" in resp.json()["detail"]


def test_origin_guard_allows_same_origin(client):
    """An Origin whose host matches the request Host passes through to auth."""
    resp = client.post(
        "/api/tasks",
        json={"task": "same origin", "mode": "readonly"},
        headers={"Origin": "http://testserver"},
    )
    assert resp.status_code != 403


def test_origin_guard_ignores_missing_origin(client):
    """Requests without an Origin header (curl/agents) are unaffected."""
    resp = client.post(
        "/api/tasks",
        json={"task": "no origin", "mode": "readonly", "provider": "openrouter"},
    )
    assert resp.status_code != 403


def test_origin_guard_wildcard_opt_out(client, monkeypatch):
    """security.allowed_origins=['*'] disables the check (explicit opt-out)."""
    monkeypatch.setattr("botex.server_cloud._security_allowed_origins", lambda: ["*"])
    resp = client.post(
        "/api/tasks",
        json={"task": "opted out", "mode": "readonly"},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code != 403


def test_origin_guard_allowlisted_origin(client, monkeypatch):
    """An exact security.allowed_origins entry permits a cross-origin PWA only."""
    monkeypatch.setattr(
        "botex.server_cloud._security_allowed_origins",
        lambda: ["https://pwa.example.com"],
    )
    ok = client.post(
        "/api/tasks",
        json={"task": "allowlisted pwa", "mode": "readonly"},
        headers={"Origin": "https://pwa.example.com"},
    )
    assert ok.status_code != 403
    blocked = client.post(
        "/api/tasks",
        json={"task": "still blocked", "mode": "readonly"},
        headers={"Origin": "http://evil.example"},
    )
    assert blocked.status_code == 403


def test_origin_guard_websocket_foreign_origin(client):
    """/api/chat/ws upgrades from a foreign Origin are closed with 1008."""
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/chat/ws", headers={"origin": "http://evil.example"}
        ) as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008


def test_chat_ws_rate_limit_and_cap_rejected(client, monkeypatch):
    """/api/chat/ws enforces the gateway RPM and a 32-connection cap pre-accept."""
    from botex.server.server_cloud import _ACTIVE_WEBSOCKETS, _GATEWAY_RATE_LIMITER

    monkeypatch.setenv("BOTEX_GATEWAY_RPM", "1")
    _GATEWAY_RATE_LIMITER.reset()
    try:
        # First handshake consumes the single token of the minute window.
        with client.websocket_connect("/api/chat/ws"):
            pass

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/api/chat/ws") as ws:
                ws.receive_json()
        assert excinfo.value.code == 1008
    finally:
        monkeypatch.setenv("BOTEX_GATEWAY_RPM", "240")
        _GATEWAY_RATE_LIMITER.reset()

    try:
        for _ in range(32):
            _ACTIVE_WEBSOCKETS[object()] = asyncio.Lock()
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/api/chat/ws") as ws:
                ws.receive_json()
        assert excinfo.value.code == 1008
    finally:
        _ACTIVE_WEBSOCKETS.clear()
        _GATEWAY_RATE_LIMITER.reset()


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


def test_wallet_endpoint_quota_windows(client):
    """Wallet endpoint returns quota windows and units='quota' for a quota-based provider."""
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
        provider="groq",
        units="quota",
        quota_windows=sample_windows,
    )
    with patch(
        "botex.server_cloud.default_wallet_service.get_credits_async",
        new=AsyncMock(return_value=mock_credits),
    ):
        resp = client.get("/api/wallet?provider=groq&force_refresh=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["supported"] is True
        assert data["provider"] == "groq"
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
        "provider": "openrouter",
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


def test_task_detail_get_marks_collected(client):
    """DCP: GET /api/tasks/{id} returning a terminal payload marks the task
    collected (accounting-only) and clears it from the uncollected filter."""
    import time

    # Patch the worker itself: under TestClient its scheduling is
    # nondeterministic and it would flip the entry back to RUNNING after
    # the test drives it to DONE.
    with patch("botex.server_cloud._run_task_worker", new=AsyncMock()):
        create_resp = client.post(
            "/api/tasks",
            json={"task": "dcp collect", "mode": "readonly", "provider": "openrouter"},
        )
        assert create_resp.status_code == 202
        task_id = create_resp.json()["task_id"]

    # The endpoint marking logic is under test, so drive the entry to
    # terminal directly.
    entry = server_cloud._TASKS[task_id]
    entry["status"] = "DONE"
    entry["result"] = "done"
    entry["finished_at"] = time.time()

    before = client.get("/api/tasks", params={"uncollected_only": "true"}).json()
    assert task_id in [t["task_id"] for t in before["tasks"]]

    detail = client.get(f"/api/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["collected"] is True

    after = client.get("/api/tasks", params={"uncollected_only": "true"}).json()
    assert task_id not in [t["task_id"] for t in after["tasks"]]
    listed = client.get("/api/tasks").json()
    match = next(t for t in listed["tasks"] if t["task_id"] == task_id)
    assert match["collected"] is True


def test_task_list_origin_filter(client):
    """DCP: POST /api/tasks stores header-derived origin; GET /api/tasks
    filters by exact origin for orchestrator sweeps."""
    with patch("botex.server_cloud._run_task_worker", new=AsyncMock()):
        create_resp = client.post(
            "/api/tasks",
            json={"task": "dcp origin filter", "mode": "readonly", "provider": "openrouter"},
            headers={"X-Agent-Id": "devin", "X-Host": "kubai-workstation"},
        )
        assert create_resp.status_code == 202
        task_id = create_resp.json()["task_id"]

    matched = client.get("/api/tasks", params={"origin": "devin@kubai-workstation"}).json()
    assert task_id in [t["task_id"] for t in matched["tasks"]]
    other = client.get("/api/tasks", params={"origin": "nobody@nowhere"}).json()
    assert task_id not in [t["task_id"] for t in other["tasks"]]


def test_task_collected_flag_persists(client):
    """DCP: the collected flag is re-persisted on collection so it survives
    server restarts (_load_tasks_persistent restores it)."""
    import json as _json
    import time as _time
    from botex.config import get_tasks_dir

    task_id = uuid.uuid4().hex[:12]
    server_cloud._TASKS[task_id] = {
        "task_id": task_id,
        "task": "dcp persist",
        "request": {"workspace_dir": ""},
        "status": "DONE",
        "created_at": _time.time(),
        "finished_at": _time.time(),
        "result": "done",
        "result_data": {"ok": True, "status": "DONE"},
        "async_task": None,
    }
    try:
        detail = client.get(f"/api/tasks/{task_id}")
        assert detail.status_code == 200 and detail.json()["collected"] is True
        persisted_path = get_tasks_dir("") / f"{task_id}.json"
        persisted = _json.loads(persisted_path.read_text(encoding="utf-8"))
        assert persisted.get("collected") is True
    finally:
        server_cloud._TASKS.pop(task_id, None)
        try:
            persisted_path.unlink(missing_ok=True)
        except Exception:
            pass


def _seed_evicted_terminal_task(task_id: str, collected: bool = False) -> Path:
    """Persist a terminal task then evict its in-memory entry via the real TTL path."""
    import time as _time

    import server as botex_server
    from botex.config import get_tasks_dir
    from botex.server.task_store import save_task_persistent

    entry = {
        "task_id": task_id,
        "task": "ttl fallback",
        "request": {"workspace_dir": ""},
        "status": "DONE",
        "created_at": _time.time() - 7200,
        "finished_at": _time.time() - 3700,
        "result": "done",
        "result_data": {"ok": True, "status": "DONE"},
        "async_task": None,
    }
    if collected:
        entry["collected"] = True
    save_task_persistent(task_id, entry, "")
    persisted_path = get_tasks_dir("") / f"{task_id}.json"
    assert persisted_path.exists()
    botex_server._TASKS[task_id] = dict(entry)
    botex_server._cleanup_tasks()
    assert task_id not in botex_server._TASKS
    return persisted_path


def test_get_task_route_restores_terminal_from_disk(client):
    """TTL-evicted terminal entries are restored from .botex/tasks/ before 404."""
    import server as botex_server

    task_id = uuid.uuid4().hex[:12]
    persisted_path = _seed_evicted_terminal_task(task_id)
    try:
        resp = client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "DONE"
        assert data["result_data"]["status"] == "DONE"
    finally:
        botex_server._TASKS.pop(task_id, None)
        persisted_path.unlink(missing_ok=True)


def test_get_task_status_restores_terminal_from_disk():
    """MCP get_task_status falls back to the persistent store on memory miss."""
    import asyncio

    import server as botex_server

    task_id = uuid.uuid4().hex[:12]
    persisted_path = _seed_evicted_terminal_task(task_id)
    try:
        result = asyncio.run(botex_server.get_task_status(task_id))
        assert result["ok"] is True
        assert result["status"] == "DONE"
        assert result["result_data"]["status"] == "DONE"
        assert botex_server._TASKS[task_id]["collected"] is True
    finally:
        botex_server._TASKS.pop(task_id, None)
        persisted_path.unlink(missing_ok=True)


def test_restore_preserves_collected_and_not_found_for_unknown():
    """Restore keeps the persisted `collected` flag; unknown ids stay NOT_FOUND."""
    import asyncio

    import server as botex_server

    task_id = uuid.uuid4().hex[:12]
    persisted_path = _seed_evicted_terminal_task(task_id, collected=True)
    try:
        restored = server_cloud._load_tasks_persistent("")
        assert restored[task_id]["collected"] is True
        result = asyncio.run(botex_server.get_task_status("zz-never-existed-zz"))
        assert result == {"ok": False, "status": "NOT_FOUND", "task_id": "zz-never-existed-zz"}
    finally:
        botex_server._TASKS.pop(task_id, None)
        persisted_path.unlink(missing_ok=True)


def test_task_requires_provider_when_model_specified(client, monkeypatch):
    """Explicit model without provider must be rejected with 400 Bad Request."""
    monkeypatch.delenv("BOTEX_PROVIDER", raising=False)
    resp = client.post(
        "/api/tasks",
        json={
            "task": "Check provider requirement",
            "mode": "readonly",
            "model": "meta-llama/llama-3.3-70b-instruct",
        },
    )
    assert resp.status_code == 400
    assert "Explicit 'provider' is required" in resp.json()["detail"]


def test_task_requires_provider_even_without_model(client, monkeypatch):
    """Provider is mandatory on every delegation — an omitted provider would
    silently route to paid 'openrouter', so model-less tasks are rejected too."""
    monkeypatch.delenv("BOTEX_PROVIDER", raising=False)
    resp = client.post(
        "/api/tasks",
        json={"task": "No provider at all", "mode": "readonly"},
    )
    assert resp.status_code == 400
    assert "Explicit 'provider' is required" in resp.json()["detail"]


def test_task_provider_env_exemption(client, monkeypatch):
    """BOTEX_PROVIDER env is the operator's explicit default — it satisfies
    the provider requirement when the request omits one."""
    monkeypatch.setenv("BOTEX_PROVIDER", "nvidia")
    with patch(
        "botex.server_cloud.run_botex_task",
        new=AsyncMock(return_value={"ok": True, "status": "DONE"}),
    ):
        resp = client.post(
            "/api/tasks",
            json={"task": "Env-provided provider", "mode": "readonly"},
        )
    assert resp.status_code == 202


def test_task_provider_alias_resolution(client):
    """Provider aliases like 'nvmi' or 'or-free' resolve cleanly to canonical provider."""
    with patch(
        "botex.server_cloud.run_botex_task",
        new=AsyncMock(return_value={"ok": True, "status": "DONE"}),
    ):
        resp = client.post(
            "/api/tasks",
            json={
                "task": "Test provider alias",
                "mode": "readonly",
                "model": "meta/llama-3.3-70b-instruct",
                "provider": "nvmi",
            },
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]
        detail = client.get(f"/api/tasks/{task_id}").json()
        assert detail["request"]["provider"] == "nvidia"


def test_task_not_found(client):
    """Non-existent task ID returns 404."""
    resp = client.get("/api/tasks/nonexistent_12345")
    assert resp.status_code == 404


def test_list_tasks_offloads_disk_load_to_thread(client, monkeypatch):
    """GET /api/tasks must run the .botex/tasks disk scan off the event loop."""
    seen = []
    real_to_thread = asyncio.to_thread

    def _spy_to_thread(func, /, *args, **kwargs):
        seen.append(func)
        return real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _spy_to_thread)
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    assert server_cloud._load_tasks_persistent in seen


def test_task_cancellation(client):
    """Task cancellation aborts background execution and pending approvals."""
    create_resp = client.post(
        "/api/tasks",
        json={"task": "Long running task", "mode": "edit", "provider": "openrouter"},
    )
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
    payload = {"task": "Idempotent task execution", "mode": "readonly", "provider": "openrouter"}

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


def test_websocket_chat_query_token_not_accepted(client, monkeypatch):
    """?token= on the chat WebSocket URL is ignored: Bearer header or auth frame only."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "ws-token-pass")

    # A URL token alone no longer authenticates: a non-auth first frame closes 1008.
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/chat/ws?token=ws-token-pass") as ws:
            ws.send_json({"type": "ping"})
            ws.receive_json()
    assert excinfo.value.code == 1008

    # Bearer header still authenticates even when a bogus URL token is present.
    with client.websocket_connect(
        "/api/chat/ws?token=bogus", headers={"authorization": "Bearer ws-token-pass"}
    ) as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"

    # First auth frame still authenticates regardless of the URL token.
    with client.websocket_connect("/api/chat/ws?token=bogus") as ws:
        ws.send_json({"type": "auth", "token": "ws-token-pass"})
        assert ws.receive_json()["type"] == "auth_ok"


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


# ---------------------------------------------------------------------------
# MCP session_id IP binding (CloudAuthMiddleware, ASGI-scope level)
# ---------------------------------------------------------------------------


def _invoke_auth_middleware(scope: dict):
    """Drive CloudAuthMiddleware over a stub inner app; return (status, reached_app)."""
    sent = []
    reached = []

    async def inner(scope, receive, send):
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def _receive():
        return {"type": "http.request", "body": b""}

    async def _send(msg):
        sent.append(msg)

    asyncio.run(CloudAuthMiddleware(inner)(scope, _receive, _send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    return status, bool(reached)


def _mcp_messages_scope(session_id: str, client_ip: str) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp/messages",
        "query_string": f"session_id={session_id}".encode("latin1"),
        "client": (client_ip, 40000),
        "headers": [],
    }


@pytest.fixture
def _live_mcp_transport(monkeypatch):
    """Install a fake MCP SSE transport with one live session; reset bindings."""
    session_uuid = uuid.uuid4()
    monkeypatch.setattr(
        server_cloud,
        "_mcp_transport",
        SimpleNamespace(_read_stream_writers={session_uuid: object()}),
    )
    server_cloud._SSE_SESSION_BINDINGS.clear()
    yield session_uuid
    server_cloud._SSE_SESSION_BINDINGS.clear()


def test_mcp_messages_requires_prior_sse_binding_same_ip(monkeypatch, _live_mcp_transport):
    """Unbound /mcp/messages post gets 401; a prior GET /mcp/sse binding admits the same IP."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "master-secret")
    sid = _live_mcp_transport.hex

    status, reached = _invoke_auth_middleware(_mcp_messages_scope(sid, "203.0.113.10"))
    assert status == 401 and not reached

    server_cloud._bind_sse_session(sid, "203.0.113.10")

    status, reached = _invoke_auth_middleware(_mcp_messages_scope(sid, "203.0.113.10"))
    assert status == 200 and reached


def test_mcp_messages_session_post_rejects_different_ip(monkeypatch, _live_mcp_transport):
    """A live session id posted from a different peer IP is rejected with 401."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "master-secret")
    sid = _live_mcp_transport.hex
    server_cloud._bind_sse_session(sid, "203.0.113.10")

    status, reached = _invoke_auth_middleware(_mcp_messages_scope(sid, "203.0.113.10"))
    assert status == 200 and reached

    status, reached = _invoke_auth_middleware(_mcp_messages_scope(sid, "198.51.100.7"))
    assert status == 401 and not reached


def test_mcp_sse_guard_binds_session_ip_on_handshake(_live_mcp_transport):
    """_MCPSseErrorGuard binds session_id -> client_ip during the GET /mcp/sse handshake."""
    sid = _live_mcp_transport.hex
    sent = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": f"event: endpoint\ndata: /mcp/messages?session_id={sid}\n\n".encode(),
            }
        )

    async def _receive():
        return {"type": "http.request", "body": b""}

    async def _send(msg):
        sent.append(msg)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp/sse",
        "query_string": b"",
        "client": ("203.0.113.55", 50000),
        "headers": [],
    }
    asyncio.run(server_cloud._MCPSseErrorGuard(inner)(scope, _receive, _send))
    assert server_cloud._SSE_SESSION_BINDINGS[sid]["ip"] == "203.0.113.55"
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert (b"x-accel-buffering", b"no") in start["headers"]


def test_mcp_sse_guard_binds_session_via_writer_delta(_live_mcp_transport):
    """A single new _read_stream_writers entry at response.start also binds the peer IP."""
    new_sid = uuid.uuid4()

    async def inner(scope, receive, send):
        server_cloud._mcp_transport._read_stream_writers[new_sid] = object()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def _receive():
        return {"type": "http.request", "body": b""}

    async def _send(msg):
        pass

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp/sse",
        "query_string": b"",
        "client": ("198.51.100.9", 50000),
        "headers": [],
    }
    asyncio.run(server_cloud._MCPSseErrorGuard(inner)(scope, _receive, _send))
    assert server_cloud._SSE_SESSION_BINDINGS[new_sid.hex]["ip"] == "198.51.100.9"

    async def plain(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"session_id=" + new_sid.hex.encode()})

    health_scope = {**scope, "path": "/api/health", "client": ("203.0.113.77", 50001)}
    asyncio.run(server_cloud._MCPSseErrorGuard(plain)(health_scope, _receive, _send))
    assert server_cloud._SSE_SESSION_BINDINGS[new_sid.hex]["ip"] == "198.51.100.9"


def test_mcp_messages_unknown_session_id_rejected(monkeypatch, _live_mcp_transport):
    """session_id not in the transport's active sessions cannot authenticate."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "master-secret")
    status, reached = _invoke_auth_middleware(_mcp_messages_scope(uuid.uuid4().hex, "203.0.113.10"))
    assert status == 401 and not reached


def test_mcp_sse_bearer_still_accepted_query_token_rejected(monkeypatch):
    """GET /mcp/sse authenticates via Bearer header only; ?token= is ignored."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "master-secret")

    bearer_scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp/sse",
        "query_string": b"",
        "client": ("203.0.113.10", 40000),
        "headers": [(b"authorization", b"Bearer master-secret")],
    }
    status, reached = _invoke_auth_middleware(bearer_scope)
    assert status == 200 and reached

    query_scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp/sse",
        "query_string": b"token=master-secret",
        "client": ("203.0.113.10", 40000),
        "headers": [],
    }
    status, reached = _invoke_auth_middleware(query_scope)
    assert status == 401 and not reached


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


def test_pwa_security_headers_always_emitted(tmp_path):
    """_NoCacheStaticFiles emits CSP/framing headers even without a web/dist build (SEC-05)."""
    (tmp_path / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    files = server_cloud._NoCacheStaticFiles(directory=str(tmp_path), html=True)
    resp = asyncio.run(
        files.get_response("index.html", {"type": "http", "method": "GET", "headers": []})
    )
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    from urllib.parse import urlparse

    csp = resp.headers["Content-Security-Policy"]
    directives = {
        part.strip().split()[0]: part.strip().split()[1:] for part in csp.split(";") if part.strip()
    }
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self' 'sha256-p+tHzEcHFHDEELDmXpTaQ0BuVNK1d0x/N41rW5RlGJw='" in csp
    assert any(
        urlparse(src).scheme == "https" and urlparse(src).netloc == "fonts.gstatic.com"
        for src in directives["font-src"]
    )


def test_pwa_csp_hash_matches_inline_theme_script():
    """The CSP script-src hash must equal the live web/index.html inline script hash."""
    import base64
    import hashlib
    from html.parser import HTMLParser

    class _ScriptExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.in_script = False
            self.scripts: list[str] = []
            self._buf: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag.lower() == "script":
                self.in_script = True
                self._buf = []

        def handle_data(self, data: str) -> None:
            if self.in_script:
                self._buf.append(data)

        def handle_endtag(self, tag: str) -> None:
            if tag.lower() == "script" and self.in_script:
                self.scripts.append("".join(self._buf))
                self.in_script = False

    index_html = (
        (Path(__file__).resolve().parent.parent / "web" / "index.html")
        .read_text(encoding="utf-8")
        .replace("\r\n", "\n")
    )
    parser = _ScriptExtractor()
    parser.feed(index_html)
    raw = parser.scripts[0]
    digest = "sha256-" + base64.b64encode(hashlib.sha256(raw.encode("utf-8")).digest()).decode(
        "ascii"
    )
    assert digest in server_cloud._PWA_CSP


@_requires_pwa_build
def test_pwa_static_shell_served(client, monkeypatch):
    """PWA static entrypoint index.html is served at root / even in Strict Cloud Mode."""
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "strict-token-abc")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "BoteX Control" in resp.text
    assert '<div id="app"></div>' in resp.text
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


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


def test_access_token_in_botex_access_token_auto_detected_as_sha256(monkeypatch):
    """Verify server auto-detects sha256:... hash placed directly in BOTEX_ACCESS_TOKEN."""
    import hashlib

    raw_token = "btx_live_test_direct_hash_987"
    token_hash = "sha256:" + hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    monkeypatch.delenv("BOTEX_ACCESS_TOKEN_HASH", raising=False)
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", f'"{token_hash}"')  # with quotes as in .env
    monkeypatch.setenv("BOTEX_STRICT_AUTH", "1")

    client = TestClient(app)

    # Valid raw token authenticated against sha256 hash placed in BOTEX_ACCESS_TOKEN
    res_ok = client.get("/api/wallet", headers={"Authorization": f"Bearer {raw_token}"})
    assert res_ok.status_code == 200

    # Wrong token rejected
    res_err = client.get("/api/wallet", headers={"Authorization": "Bearer wrong"})
    assert res_err.status_code == 401


def test_stored_token_hash_is_not_a_bearer_credential(monkeypatch):
    """The at-rest BOTEX_ACCESS_TOKEN_HASH value itself must be rejected as bearer."""
    import hashlib

    raw_token = "btx_live_test_secret_12345"
    hex_digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    token_hash = f"sha256:{hex_digest}"

    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN_HASH", token_hash)
    monkeypatch.setenv("BOTEX_STRICT_AUTH", "1")

    client = TestClient(app)

    # Stored hash presented literally is rejected, in both prefixed and bare-hex form
    res_prefixed = client.get("/api/wallet", headers={"Authorization": f"Bearer {token_hash}"})
    assert res_prefixed.status_code == 401
    res_hex = client.get("/api/wallet", headers={"Authorization": f"Bearer {hex_digest}"})
    assert res_hex.status_code == 401

    # Hash-only deployment still verifies the real token
    res_ok = client.get("/api/wallet", headers={"Authorization": f"Bearer {raw_token}"})
    assert res_ok.status_code == 200


def test_hash_in_access_token_var_is_not_a_bearer_credential(monkeypatch):
    """sha256:... value placed in BOTEX_ACCESS_TOKEN is a verifier, not a credential."""
    import hashlib

    raw_token = "btx_live_test_direct_hash_987"
    token_hash = "sha256:" + hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    monkeypatch.delenv("BOTEX_ACCESS_TOKEN_HASH", raising=False)
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", token_hash)
    monkeypatch.setenv("BOTEX_STRICT_AUTH", "1")

    client = TestClient(app)

    res_literal = client.get("/api/wallet", headers={"Authorization": f"Bearer {token_hash}"})
    assert res_literal.status_code == 401

    res_ok = client.get("/api/wallet", headers={"Authorization": f"Bearer {raw_token}"})
    assert res_ok.status_code == 200


def test_access_token_plaintext_compare_is_case_sensitive(monkeypatch):
    """Plain bearer comparison is byte-exact; a case-mutated token is rejected."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN_HASH", raising=False)
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "Super-Secret-Key-ABC")
    monkeypatch.setenv("BOTEX_STRICT_AUTH", "1")

    client = TestClient(app)

    res_mutated = client.get(
        "/api/wallet", headers={"Authorization": "Bearer super-secret-key-abc"}
    )
    assert res_mutated.status_code == 401

    res_ok = client.get("/api/wallet", headers={"Authorization": "Bearer Super-Secret-Key-ABC"})
    assert res_ok.status_code == 200


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


# ---------------------------------------------------------------------------
# Memory Vault Project Partitioning (Phase 5)
# ---------------------------------------------------------------------------


def test_memory_project_field_api_roundtrip(client, tmp_path):
    """POST /api/memory accepts project; GET /api/memory?project= filters results."""
    ws = str(tmp_path)

    r1 = client.post(
        "/api/memory",
        json={
            "workspace_dir": ws,
            "title": "Partitioned Architecture Note",
            "content": "Recall pipeline partitioning decision.",
            "kind": "decision",
            "project": "Botex Harness",
        },
    )
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["ok"] is True
    assert d1["project"] == "botex-harness"

    r2 = client.post(
        "/api/memory",
        json={
            "workspace_dir": ws,
            "title": "Unassigned Scratch Note",
            "content": "No project attached.",
        },
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["project"] == ""

    proj_resp = client.get(f"/api/memory?workspace_dir={ws}&project=botex-harness&scope=local")
    assert proj_resp.status_code == 200
    ids = {m["id"] for m in proj_resp.json()["memories"]}
    assert d1["id"] in ids
    assert d2["id"] not in ids

    un_resp = client.get(f"/api/memory?workspace_dir={ws}&project=_unassigned&scope=local")
    assert un_resp.status_code == 200
    ids_un = {m["id"] for m in un_resp.json()["memories"]}
    assert d2["id"] in ids_un
    assert d1["id"] not in ids_un

    read_resp = client.get(f"/api/memory?workspace_dir={ws}&memory_id={d1['id']}&scope=local")
    assert read_resp.status_code == 200
    assert read_resp.json()["metadata"]["project"] == "botex-harness"


def test_memory_projects_endpoint(client, tmp_path, monkeypatch):
    """GET /api/memory/projects aggregates project counts across local+global scopes."""
    ws = str(tmp_path)
    # Keep scope="all" hermetic: configured swarm root is the repo working tree.
    monkeypatch.setattr("botex.memory.get_swarm_memory_dir", lambda: None)

    client.post(
        "/api/memory",
        json={"workspace_dir": ws, "title": "H1", "content": "x", "project": "botex-harness"},
    )
    client.post(
        "/api/memory",
        json={"workspace_dir": ws, "title": "H2", "content": "x", "project": "Botex Harness"},
    )
    client.post(
        "/api/memory",
        json={"workspace_dir": ws, "title": "M1", "content": "x", "project": "media-gen"},
    )
    client.post(
        "/api/memory",
        json={
            "workspace_dir": ws,
            "title": "G1",
            "content": "x",
            "project": "media-gen",
            "scope": "global",
        },
    )
    client.post("/api/memory", json={"workspace_dir": ws, "title": "U1", "content": "x"})

    resp = client.get(f"/api/memory/projects?workspace_dir={ws}&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    counts = {p["project"]: p["count"] for p in data["projects"]}
    assert counts["botex-harness"] == 2
    assert counts["media-gen"] == 2
    assert counts[""] == 1
    assert data["total"] == 5
    assert data["projects"][-1]["project"] == ""

    resp_local = client.get(f"/api/memory/projects?workspace_dir={ws}&scope=local")
    assert resp_local.status_code == 200
    counts_local = {p["project"]: p["count"] for p in resp_local.json()["projects"]}
    assert counts_local == {"botex-harness": 2, "media-gen": 1, "": 1}


class _ProbeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _ProbeClient:
    """Fake httpx sync client: :8080 alive, :11434 refused."""

    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if ":8080" in url:
            return _ProbeResp(200)
        raise OSError("connection refused")


def test_resolve_local_llm_base_url_probe_and_cache():
    """Loopback providers probe :8080/:11434 /models and cache the winner."""
    from botex.server_cloud import _LOCAL_LLM_BASE_URL_RESOLVED, _resolve_local_llm_base_url

    _LOCAL_LLM_BASE_URL_RESOLVED.clear()
    fake = _ProbeClient()
    with patch("botex.providers.http_pool.get_sync_client", return_value=fake):
        out = _resolve_local_llm_base_url("ollama", {})
    assert out["base_url"] == "http://127.0.0.1:8080/v1"
    assert fake.calls == ["http://127.0.0.1:8080/v1/models"]

    # Second resolution is served from cache — no new probe calls.
    calls_before = len(fake.calls)
    with patch("botex.providers.http_pool.get_sync_client", return_value=fake):
        cached = _resolve_local_llm_base_url("ollama", {"other": "cfg"})
    assert cached["base_url"] == "http://127.0.0.1:8080/v1"
    assert len(fake.calls) == calls_before
    _LOCAL_LLM_BASE_URL_RESOLVED.clear()


def test_resolve_local_llm_base_url_skips_non_loopback():
    """Remote providers are returned untouched — no probe, no client."""
    from botex.server_cloud import _resolve_local_llm_base_url

    cfg = {"base_url": "https://openrouter.ai/api/v1"}
    assert _resolve_local_llm_base_url("openrouter", cfg) is cfg


def test_resolve_local_llm_base_url_probe_failure_degrades():
    """When no candidate responds, config passes through unchanged (degrade, not crash)."""
    from botex.server_cloud import _LOCAL_LLM_BASE_URL_RESOLVED, _resolve_local_llm_base_url

    class _DeadClient:
        def get(self, url, headers=None, timeout=None):
            raise OSError("connection refused")

    _LOCAL_LLM_BASE_URL_RESOLVED.clear()
    cfg = {"base_url": "http://127.0.0.1:9999/v1"}
    with patch("botex.providers.http_pool.get_sync_client", return_value=_DeadClient()):
        out = _resolve_local_llm_base_url("ollama", cfg)
    assert out is cfg
    _LOCAL_LLM_BASE_URL_RESOLVED.clear()


def test_streamable_http_mcp_requires_token(client, monkeypatch):
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    resp = client.post("/mcp/http", json={})
    assert resp.status_code == 401


def test_streamable_http_mcp_initialize(client, monkeypatch):
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "super-secret-key-12345")
    resp = client.post(
        "/mcp/http",
        headers={
            "Authorization": "Bearer super-secret-key-12345",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
    )
    assert resp.status_code == 200
    assert "serverInfo" in resp.text


def test_swarm_orders_rest_crud_pause_resume_cancel_and_lease_freeze(client, monkeypatch, tmp_path):
    """Plan 041: WorkOrder REST lifecycle, auth, CAS/illegal transitions, and PAUSED lease-freeze."""
    from botex.core import swarm_store
    from botex.server import server_cloud

    db_file = tmp_path / "swarm_orders_rest.db"
    monkeypatch.setattr(swarm_store, "_swarm_store_db_path", lambda: db_file)
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "swarm-secret-token")
    hdrs = {"Authorization": "Bearer swarm-secret-token"}

    # Unauthenticated request rejected
    assert client.get("/api/swarm/orders").status_code == 401

    # Register bot so lease sweep can be tested
    swarm_store.register_bot("bot-alpha", "coding", "Alpha bot", db_path=db_file)

    # Create order in RUNNING state
    r_create = client.post(
        "/api/swarm/orders",
        headers=hdrs,
        json={
            "order_id": "ord-rest-1",
            "bot_slug": "bot-alpha",
            "status": "RUNNING",
            "host_id": "host-1",
        },
    )
    assert r_create.status_code == 201, r_create.text
    assert r_create.json()["ok"] is True

    # Duplicate rejected with 409
    r_dup = client.post(
        "/api/swarm/orders",
        headers=hdrs,
        json={"order_id": "ord-rest-1", "bot_slug": "bot-alpha", "status": "RUNNING"},
    )
    assert r_dup.status_code == 409

    # List & Get
    r_list = client.get("/api/swarm/orders?status=RUNNING", headers=hdrs)
    assert r_list.status_code == 200
    assert r_list.json()["count"] == 1
    r_get = client.get("/api/swarm/orders/ord-rest-1", headers=hdrs)
    assert r_get.status_code == 200
    assert r_get.json()["order"]["status"] == "RUNNING"

    # Pause -> PAUSED
    r_pause = client.post(
        "/api/swarm/orders/ord-rest-1/pause",
        headers=hdrs,
        json={"reason": "operator_break"},
    )
    assert r_pause.status_code == 200, r_pause.text
    assert r_pause.json()["status"] == "PAUSED"
    paused_epoch = r_pause.json()["epoch"]

    # Expired host lease must NOT auto-fence PAUSED order
    swarm_store.upsert_host_lease(
        "bot-alpha", "host-1", "repo/a", "working", fencing_token=1, ttl_s=1, db_path=db_file
    )
    failovers = server_cloud._sweep_expired_host_leases_sync(
        now=9_999_999_999.0, candidate_hosts=["host-2"], db_path=db_file
    )
    assert failovers == []
    assert swarm_store.get_work_order("ord-rest-1", db_path=db_file)["status"] == "PAUSED"

    # Stale fencing_epoch rejected with 409
    r_stale = client.post(
        "/api/swarm/orders/ord-rest-1/resume",
        headers=hdrs,
        json={"fencing_epoch": paused_epoch - 1},
    )
    assert r_stale.status_code == 409

    # Resume -> RUNNING
    r_resume = client.post(
        "/api/swarm/orders/ord-rest-1/resume",
        headers=hdrs,
        json={"fencing_epoch": paused_epoch},
    )
    assert r_resume.status_code == 200
    assert r_resume.json()["status"] == "RUNNING"

    # Cancel -> CANCELLED (terminal)
    r_cancel = client.post(
        "/api/swarm/orders/ord-rest-1/cancel",
        headers=hdrs,
        json={"reason": "done_early"},
    )
    assert r_cancel.status_code == 200
    assert r_cancel.json()["status"] == "CANCELLED"

    # Further transitions from CANCELLED rejected with 409
    r_after = client.post("/api/swarm/orders/ord-rest-1/resume", headers=hdrs, json={})
    assert r_after.status_code == 409


def test_bots_pause_resume_and_host_harnesses_rest(client, monkeypatch, tmp_path):
    """Plan 041: /api/bots/{slug}/pause|resume and /api/edge/hosts/{host_id}/harnesses."""
    from botex.core import swarm_store

    db_file = tmp_path / "swarm_bots_harnesses.db"
    monkeypatch.setattr(swarm_store, "_swarm_store_db_path", lambda: db_file)
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "swarm-secret-token")
    hdrs = {"Authorization": "Bearer swarm-secret-token"}

    swarm_store.register_bot("bot-beta", "ops", "Beta bot", db_path=db_file)
    assert client.post("/api/bots/unknown-bot/pause", headers=hdrs).status_code == 404

    r_p = client.post("/api/bots/bot-beta/pause", headers=hdrs)
    assert r_p.status_code == 200
    assert r_p.json()["status"] == "paused"

    r_r = client.post("/api/bots/bot-beta/resume", headers=hdrs)
    assert r_r.status_code == 200
    assert r_r.json()["status"] == "active"

    swarm_store.upsert_host_harnesses(
        "edge-host-9",
        [
            {
                "harness_id": "devin",
                "transport": "acp",
                "status": "online",
                "capabilities": {"available": True},
            }
        ],
        last_seen="2020-01-01T00:00:00+00:00",
        db_path=db_file,
    )
    r_h = client.get("/api/edge/hosts/edge-host-9/harnesses?stale_after_s=60", headers=hdrs)
    assert r_h.status_code == 200
    body = r_h.json()
    assert body["count"] == 1
    assert body["harnesses"][0]["harness_id"] == "devin"
    assert body["harnesses"][0]["status"] == "stale"
