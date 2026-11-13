# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for Faza 2 Cloud Edge Gateway: /api/edge/ws, tokens, LRU cache, rate limiting, Caddy."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from botex.core.device_tokens import DeviceTokenStore
from botex.core.tool_executor import resolve_edge_executor
from botex.server import server_cloud
from botex.server.server_cloud import (
    _EDGE_SESSIONS,
    _GATEWAY_RATE_LIMITER,
    _MAX_TASK_CACHE_BYTES,
    RemoteWssToolExecutor,
    _extract_client_ip_from_scope,
    app,
)
from botex.state import InFlightTaskState, save_inflight_checkpoint


@pytest.fixture(autouse=True)
def _isolated_cloud_state(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN_HASH", raising=False)
    monkeypatch.delenv("BOTEX_STRICT_AUTH", raising=False)
    monkeypatch.setenv("BOTEX_GATEWAY_RPM", "240")
    _GATEWAY_RATE_LIMITER.reset()
    _EDGE_SESSIONS.clear()
    monkeypatch.setattr(
        "botex.ops.migrate_projects.ensure_projects_migrated",
        lambda *args, **kwargs: {"ok": True, "roots": [], "total_updated": 0},
    )
    ws_dir = tmp_path / "cloud_ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    store = DeviceTokenStore(ws_dir / ".botex" / "device_tokens.json")
    monkeypatch.setattr(server_cloud, "_edge_token_store", lambda: store)
    monkeypatch.setattr(server_cloud, "resolve_workspace", lambda value="": str(ws_dir))
    yield ws_dir, store
    _EDGE_SESSIONS.clear()
    _GATEWAY_RATE_LIMITER.reset()


def test_edge_token_lifecycle_rest_endpoints(_isolated_cloud_state):
    with TestClient(app) as client:
        res = client.post(
            "/api/edge/tokens",
            json={"device_id": "win11-dev", "workspace_id": "harness"},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["ok"] is True
        raw_token = data["token"]
        jti = data["record"]["jti"]
        assert raw_token.startswith("btxd_")

        listed = client.get("/api/edge/tokens").json()
        assert len(listed["tokens"]) == 1
        assert listed["tokens"][0]["jti"] == jti
        assert raw_token not in str(listed)
        assert "digest_sha256" not in str(listed)

        revoked = client.delete(f"/api/edge/tokens/{jti}").json()
        assert revoked["ok"] is True
        assert revoked["revoked"] is True
        assert revoked["existed"] is True


def test_edge_ws_auth_rejects_bad_token_and_scope(_isolated_cloud_state):
    _, store = _isolated_cloud_state
    raw_token, _ = store.issue_token(
        "dev-a",
        "repo-a",
        scopes=["task_pull:repo-a", "result_push:repo-a"],
    )

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/api/edge/ws",
                headers={"Authorization": "Bearer invalid-token"},
            ) as ws:
                ws.receive_json()

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/api/edge/ws",
                headers={"Authorization": f"Bearer {raw_token}"},
            ) as ws:
                # Wrong workspace for scoped token
                ws.send_json(
                    {
                        "type": "hello",
                        "device_id": "dev-a",
                        "workspace_id": "repo-other",
                    }
                )
                ws.receive_json()


def test_edge_ws_handshake_ping_and_revocation(_isolated_cloud_state):
    with TestClient(app) as client:
        tok_res = client.post(
            "/api/edge/tokens",
            json={"device_id": "box1", "workspace_id": "ws1"},
        ).json()
        raw_token = tok_res["token"]
        jti = tok_res["record"]["jti"]

        with client.websocket_connect(
            "/api/edge/ws",
            headers={"Authorization": f"Bearer {raw_token}"},
        ) as ws:
            ws.send_json(
                {
                    "type": "hello",
                    "device_id": "box1",
                    "workspace_id": "ws1",
                    "capabilities": {"allow_exec": False, "max_output_bytes": 32768},
                    "protocol_version": "1.0",
                    "client_version": "2.1.0",
                }
            )
            ack = ws.receive_json()
            assert ack["type"] == "hello_ack"
            assert ack["device_id"] == "box1"
            assert ack["workspace_id"] == "ws1"

            ws.send_json({"type": "ping", "ts": 123.0})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

            devices = client.get("/api/edge/devices").json()["devices"]
            assert len(devices) == 1
            assert devices[0]["device_id"] == "box1"
            assert resolve_edge_executor("edge://box1/ws1") is not None

            # Revoking token actively closes WS on next check/heartbeat
            rev = client.delete(f"/api/edge/tokens/{jti}").json()
            assert rev["disconnected_sessions"] >= 1


@pytest.mark.anyio
async def test_remote_wss_executor_8mib_lru_cache_and_unchanged_reconstruct(tmp_path: Path):
    calls: list[dict] = []

    class _DummySession:
        async def dispatch_tool_call(
            self,
            *,
            tool_name: str,
            args: dict,
            task_id: str,
            call_id: str,
            cached_sha256: str | None = None,
            timeout_s: float = 120.0,
        ):
            calls.append(
                {
                    "tool_name": tool_name,
                    "args": args,
                    "task_id": task_id,
                    "call_id": call_id,
                    "cached_sha256": cached_sha256,
                }
            )
            if tool_name == "read_file" and cached_sha256 == "sha-1":
                return {"ok": True, "status": "unchanged", "sha256": "sha-1"}
            if tool_name == "read_file":
                return {
                    "ok": True,
                    "path": args["path"],
                    "file_sha256": "sha-1",
                    "content": "hello world",
                }
            if tool_name == "apply_patch":
                return {"ok": True, "path": args["path"], "lines_added": 1}
            return {"ok": True}

    ex = RemoteWssToolExecutor(_DummySession())  # type: ignore[arg-type]
    r1 = await ex.execute("read_file", {"path": "a.py"}, tmp_path, task_id="t1")
    assert r1["content"] == "hello world"
    assert calls[0]["cached_sha256"] is None

    # Second read passes cached_sha256 and reconstructs cached payload cleanly
    r2 = await ex.execute("read_file", {"path": "a.py"}, tmp_path, task_id="t1")
    assert r2["content"] == "hello world"
    assert calls[1]["cached_sha256"] == "sha-1"

    # Mutating path invalidates cached entry
    await ex.execute("apply_patch", {"path": "a.py"}, tmp_path, task_id="t1")
    await ex.execute("read_file", {"path": "a.py"}, tmp_path, task_id="t1")
    assert calls[3]["cached_sha256"] is None

    # Verify strict 8 MiB per-task LRU ceiling
    big_chunk = "A" * (3 * 1024 * 1024)
    for idx in range(4):
        ex._put_cached(
            "t-lru",
            f"read_file:f{idx}.txt",
            f"sha-{idx}",
            {"ok": True, "file_sha256": f"sha-{idx}", "content": big_chunk},
        )
    assert ex._task_cache_bytes["t-lru"] <= _MAX_TASK_CACHE_BYTES
    assert "read_file:f0.txt" not in ex._task_caches["t-lru"]


def test_rate_limiter_and_proxy_xff_trust(monkeypatch):
    monkeypatch.setenv("BOTEX_GATEWAY_RPM", "3")
    _GATEWAY_RATE_LIMITER.reset()

    with TestClient(app) as client:
        # /api/health bypasses rate limit
        for _ in range(5):
            assert client.get("/api/health").status_code == 200

        # Non-health /api/ routes enforce 3 RPM limit
        assert client.get("/api/edge/tokens").status_code == 200
        assert client.get("/api/edge/tokens").status_code == 200
        assert client.get("/api/edge/tokens").status_code == 200
        blocked = client.get("/api/edge/tokens")
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers

    # Trusted proxy header honored ONLY when peer IP is loopback
    assert (
        _extract_client_ip_from_scope(
            {"client": ("127.0.0.1", 50000), "headers": [(b"x-forwarded-for", b"203.0.113.9")]}
        )
        == "203.0.113.9"
    )
    assert (
        _extract_client_ip_from_scope(
            {"client": ("198.51.100.7", 50000), "headers": [(b"x-forwarded-for", b"203.0.113.9")]}
        )
        == "198.51.100.7"
    )
    # Spoofed leftmost entries must not forge fresh rate-limit keys: only the
    # rightmost entry (appended by our trusted proxy) identifies the client.
    assert (
        _extract_client_ip_from_scope(
            {
                "client": ("127.0.0.1", 50000),
                "headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8, 203.0.113.9")],
            }
        )
        == "203.0.113.9"
    )
    assert (
        _extract_client_ip_from_scope(
            {"client": ("::1", 50000), "headers": [(b"x-forwarded-for", b"1.2.3.4, 203.0.113.9")]}
        )
        == "203.0.113.9"
    )


def test_startup_lifespan_detects_interrupted_inflight_checkpoints(_isolated_cloud_state):
    ws_dir, _ = _isolated_cloud_state
    state = InFlightTaskState(task_id="interrupted-1", goal="Finish refactor")
    save_inflight_checkpoint(state, ws_dir)

    with TestClient(app) as client:
        assert len(app.state.interrupted_tasks) == 1
        assert app.state.interrupted_tasks[0]["task_id"] == "interrupted-1"
        devices_res = client.get("/api/edge/devices").json()
        assert len(devices_res["interrupted_tasks"]) == 1


def test_mcp_backwards_compat_with_token(_isolated_cloud_state, monkeypatch):
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "master-secret-token")
    with TestClient(app) as client:
        unauth = client.get("/mcp/sse")
        assert unauth.status_code == 401
        http_res = client.post(
            "/mcp/http",
            headers={"Authorization": "Bearer master-secret-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        # Reaches MCP handler (never blocked by edge changes)
        assert http_res.status_code != 401


def test_oracle_vps_deploy_caddy_and_service_hardening():
    repo_root = Path(__file__).resolve().parent.parent
    caddy = (repo_root / "deploy" / "oracle-vps" / "Caddyfile.template").read_text(encoding="utf-8")
    assert "write 3600s" in caddy
    assert "max_size 25MB" in caddy
    assert "flush_interval -1" in caddy
    assert "/api/edge/ws" in caddy
    assert "127.0.0.1:8000" in caddy

    service = (repo_root / "deploy" / "oracle-vps" / "botex-cloud.service").read_text(
        encoding="utf-8"
    )
    assert "--host 127.0.0.1" in service
    assert "--proxy-headers" in service
    assert "--forwarded-allow-ips 127.0.0.1" in service
    assert "--ws websockets" in service

    install_sh = (repo_root / "deploy" / "oracle-vps" / "install.sh").read_text(encoding="utf-8")
    assert "ufw allow 8000/tcp" not in install_sh
    assert "ufw allow 443/tcp" in install_sh
    assert "ufw delete allow 8000/tcp" in install_sh


# ---------------------------------------------------------------------------
# Worker C regressions: query-token removal, store singleton, scratchpad,
# run_command cache invalidation
# ---------------------------------------------------------------------------


def test_ws_query_param_token_not_accepted(_isolated_cloud_state):
    """Tokens must ride the Authorization header or first auth frame — never
    the query string, which leaks into proxy access logs."""
    _, store = _isolated_cloud_state
    raw_token, _ = store.issue_token("dev-q", "ws-q")

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/edge/ws?token={raw_token}") as ws:
                # A hello frame is not an auth frame; without a Bearer header
                # the credential stays empty and the socket closes 1008.
                ws.send_json({"type": "hello", "device_id": "dev-q", "workspace_id": "ws-q"})
                ws.receive_json()


def test_edge_token_store_singleton_shares_revocation(monkeypatch, tmp_path):
    """REST revocations must be visible to the store instance a live WSS
    loop polls — a per-call store would pin a stale snapshot forever."""
    monkeypatch.setattr(server_cloud, "_EDGE_TOKEN_STORE_SINGLETON", None)
    monkeypatch.setattr(server_cloud, "resolve_workspace", lambda value="": str(tmp_path))

    first = server_cloud._edge_token_store()
    second = server_cloud._edge_token_store()
    assert first is second

    raw_token, rec = first.issue_token("dev-s", "ws-s")
    # Simulate a later REST request revoking via its own handle
    assert server_cloud._edge_token_store().revoke_token(rec.jti) is True
    assert first.is_revoked(rec.jti) is True
    assert first.verify_token(raw_token) is None


def test_remote_executor_serves_read_scratchpad_locally(tmp_path):
    """read_scratchpad is a cloud-host vault; it must never be dispatched to
    the edge (which would reject it as an unsupported tool)."""

    class _BoomSession:
        async def dispatch_tool_call(self, **kwargs):
            raise AssertionError("read_scratchpad must not reach the edge wire")

    ex = RemoteWssToolExecutor(_BoomSession())  # type: ignore[arg-type]
    res = asyncio.run(ex.execute("read_scratchpad", {"memory_id": "missing"}, tmp_path))
    assert res["ok"] is False
    assert "not found" in res["error"] or "Invalid" in res["error"]


def test_run_command_success_invalidates_read_cache(tmp_path):
    """A successful command may mutate files on the edge; the next read must
    not trust the pre-command cached digest."""
    calls: list[tuple[str, str | None]] = []

    class _DummySession:
        async def dispatch_tool_call(
            self,
            *,
            tool_name: str,
            args: dict,
            task_id: str,
            call_id: str,
            cached_sha256: str | None = None,
            timeout_s: float = 120.0,
        ):
            calls.append((tool_name, cached_sha256))
            if tool_name == "read_file":
                if cached_sha256 == "sha-x":
                    return {"ok": True, "status": "unchanged", "sha256": "sha-x"}
                return {
                    "ok": True,
                    "path": args["path"],
                    "file_sha256": "sha-x",
                    "content": "data",
                }
            return {"ok": True, "stdout": ""}

    ex = RemoteWssToolExecutor(_DummySession())  # type: ignore[arg-type]
    r1 = asyncio.run(ex.execute("read_file", {"path": "f.py"}, tmp_path, task_id="t"))
    assert r1["ok"] is True
    r2 = asyncio.run(ex.execute("read_file", {"path": "f.py"}, tmp_path, task_id="t"))
    assert r2 == r1  # digest shortcut reconstructs the cached payload
    assert calls[1] == ("read_file", "sha-x")

    r3 = asyncio.run(ex.execute("run_command", {"command": "python fmt.py"}, tmp_path, task_id="t"))
    assert r3["ok"] is True
    r4 = asyncio.run(ex.execute("read_file", {"path": "f.py"}, tmp_path, task_id="t"))
    assert r4["ok"] is True
    assert calls[3] == ("read_file", None)  # cache dropped: fresh digest sent
