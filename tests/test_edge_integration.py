# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Worker C live A+B integration test: the real cloud gateway
(``server_cloud.app`` served by uvicorn on loopback) bridged against the real
edge client (``EdgeRunner.run_once`` over a genuine WebSocket), driven through
``RemoteWssToolExecutor``.

Covers the full Phase 2 wire: header auth → hello/hello_ack handshake →
tool_call/tool_result round-trips → cached_sha256 "unchanged" reconstruction →
digest mismatch re-read → mutating apply_patch → concurrent read batching →
EDGE_NEVER_TOOLS rejection over the wire → live JTI revocation (close 1008)
→ session cleanup.

The edge client runs on the main thread's event loop; the cloud-side executor
must run on the uvicorn server's loop (its WebSocket lives there), hence
``run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import uvicorn

from botex.core.device_tokens import DeviceTokenStore
from botex.edge import EdgeCapabilities, EdgeRunner
from botex.server import server_cloud
from botex.server.server_cloud import RemoteWssToolExecutor


class _LoopCapture:
    """ASGI wrapper recording the server's event loop for cross-thread work."""

    def __init__(self, app) -> None:
        self._app = app
        self.loop: asyncio.AbstractEventLoop | None = None

    async def __call__(self, scope, receive, send):
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        await self._app(scope, receive, send)


@pytest.fixture()
def live_gateway(monkeypatch, tmp_path):
    server_cloud._EDGE_SESSIONS.clear()
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN_HASH", raising=False)
    monkeypatch.delenv("BOTEX_STRICT_AUTH", raising=False)
    monkeypatch.setattr(
        "botex.ops.migrate_projects.ensure_projects_migrated",
        lambda *args, **kwargs: {"ok": True, "roots": [], "total_updated": 0},
    )
    ws_dir = tmp_path / "edge_ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    store = DeviceTokenStore(tmp_path / ".botex" / "device_tokens.json")
    monkeypatch.setattr(server_cloud, "_edge_token_store", lambda: store)
    monkeypatch.setattr(server_cloud, "resolve_workspace", lambda value="": str(tmp_path))

    wrapper = _LoopCapture(server_cloud.app)
    config = uvicorn.Config(wrapper, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15.0
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn gateway did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    assert wrapper.loop is not None
    try:
        yield {
            "port": port,
            "store": store,
            "ws_dir": ws_dir,
            "server_loop": wrapper.loop,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        server_cloud._EDGE_SESSIONS.clear()


def test_cloud_edge_live_roundtrip_and_revocation(live_gateway):
    edge_ws: Path = live_gateway["ws_dir"]
    (edge_ws / "hello.txt").write_text("v1 content\n", encoding="utf-8")
    raw_token, rec = live_gateway["store"].issue_token("dev-int", "ws-int")

    async def client_side() -> str:
        runner = EdgeRunner(
            f"ws://127.0.0.1:{live_gateway['port']}/api/edge/ws",
            token=raw_token,
            device_id="dev-int",
            workspace_id="ws-int",
            workspace_root=edge_ws,
            caps=EdgeCapabilities(),
        )
        status = await runner.run_once()
        return status

    async def cloud_side() -> bool:
        sess = None
        for _ in range(300):  # up to 15s for the edge handshake
            sess = server_cloud._EDGE_SESSIONS.get("dev-int:ws-int")
            if sess is not None:
                break
            await asyncio.sleep(0.05)
        assert sess is not None, "edge session never registered"

        ex = RemoteWssToolExecutor(sess, timeout_s=20.0)
        root = edge_ws

        # 1. Fresh read populates the digest cache
        r1 = await ex.execute("read_file", {"path": "hello.txt"}, root, task_id="int-1")
        assert r1["ok"] is True, r1
        assert "v1 content" in r1["content"]
        assert r1["file_sha256"]

        # 2. Second read exercises the wire-level "unchanged" shortcut
        r2 = await ex.execute("read_file", {"path": "hello.txt"}, root, task_id="int-1")
        assert r2 == r1

        # 3. Edge-side mutation → digest mismatch → full re-read with new sha
        (edge_ws / "hello.txt").write_text("v2 content\n", encoding="utf-8")
        r3 = await ex.execute("read_file", {"path": "hello.txt"}, root, task_id="int-1")
        assert r3["ok"] is True and "v2 content" in r3["content"]
        assert r3["file_sha256"] != r1["file_sha256"]

        # 4. Mutating tool over the wire (fuzzy patch engine on the edge)
        r4 = await ex.execute(
            "apply_patch",
            {"path": "hello.txt", "search_block": "v2 content", "replace_block": "v3 content"},
            root,
            task_id="int-1",
        )
        assert r4["ok"] is True, r4
        assert "v3 content" in (edge_ws / "hello.txt").read_text(encoding="utf-8")

        # 5. Concurrent read-only batch over the real socket
        batch = await ex.execute_batch(
            [
                {"tool_name": "list_dir", "args": {"path": "."}},
                {"tool_name": "read_file", "args": {"path": "hello.txt"}},
            ],
            root,
            task_id="int-1",
        )
        assert len(batch) == 2 and all(b["ok"] for b in batch), batch

        # 6. EDGE_NEVER_TOOLS rejected over the wire, fail-closed
        net = await ex.execute("read_url", {"url": "https://example.com"}, root, task_id="int-1")
        assert net["ok"] is False and "not available" in net["error"]

        # 7. Live revocation closes the socket with 1008 and drops the session
        rev = await server_cloud.revoke_edge_token(rec.jti)
        assert rev["ok"] is True and rev["disconnected_sessions"] == 1
        return True

    async def main():
        client_task = asyncio.get_running_loop().create_task(client_side())
        cloud_fut = asyncio.run_coroutine_threadsafe(cloud_side(), live_gateway["server_loop"])
        while not cloud_fut.done():
            await asyncio.sleep(0.05)
        assert cloud_fut.result(timeout=5.0) is True
        status = await asyncio.wait_for(client_task, timeout=20.0)
        return status

    assert asyncio.run(main()) == "unauthorized"

    # Session registry drained after the 1008 close (handler finally-block)
    deadline = time.time() + 5.0
    while server_cloud._EDGE_SESSIONS and time.time() < deadline:
        time.sleep(0.05)
    assert not server_cloud._EDGE_SESSIONS


def test_cloud_edge_device_mismatch_closes_1008(live_gateway):
    raw_token, _rec = live_gateway["store"].issue_token("dev-int-2", "ws-int-2")

    async def client_side() -> str:
        runner = EdgeRunner(
            f"ws://127.0.0.1:{live_gateway['port']}/api/edge/ws",
            token=raw_token,
            device_id="dev-IMPERSONATED",
            workspace_id="ws-int-2",
            workspace_root=live_gateway["ws_dir"],
            caps=EdgeCapabilities(),
        )
        return await runner.run_once()

    # The gateway rejects the hello (device_id must match the token record)
    # before any session is registered — no cloud-side coordination needed.
    assert asyncio.run(asyncio.wait_for(client_side(), timeout=20.0)) == "unauthorized"
    assert not server_cloud._EDGE_SESSIONS


def test_cloud_edge_bad_token_rejected_before_hello(live_gateway):
    async def client_side() -> str:
        runner = EdgeRunner(
            f"ws://127.0.0.1:{live_gateway['port']}/api/edge/ws",
            token="btxd_deadbeef.not-a-real-secret",
            device_id="dev-int-3",
            workspace_id="ws-int-3",
            workspace_root=live_gateway["ws_dir"],
            caps=EdgeCapabilities(),
        )
        return await runner.run_once()

    assert asyncio.run(client_side()) == "unauthorized"
