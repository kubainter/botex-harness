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

Phase 3 adds live Safe PWA → Device X delegation: ``POST /api/edge/delegate``
against a real ``EdgeRunner`` (real ``EdgeDelegationHandler``) — read-only
round-trips on the edge filesystem, the operator approval gate for mutating
skills (approve via REST → remote handler executes), and fail-closed
rejections (raw command params, bad/non-ASCII digests, unadvertised skills,
opt-in disabled).

The edge client runs on the main thread's event loop; the cloud-side executor
must run on the uvicorn server's loop (its WebSocket lives there), hence
``run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
import uvicorn

from botex.core.device_tokens import DeviceTokenStore
from botex.edge import (
    EdgeCapabilities,
    EdgeDelegationHandler,
    EdgeDelegationPolicy,
    EdgeRunner,
    EdgeSkillSpec,
)
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


# ---------------------------------------------------------------------------
# Phase 3: live Safe PWA -> Device X skill delegation over the real gateway
# ---------------------------------------------------------------------------

_RO_SHA = hashlib.sha256(b"integration-readonly-skill-v1").hexdigest()
_MUT_SHA = hashlib.sha256(b"integration-mutating-skill-v1").hexdigest()
_FINAL_STATES = {"DONE", "FAILED", "CANCELLED"}


def _delegation_policy() -> EdgeDelegationPolicy:
    return EdgeDelegationPolicy(
        enabled=True,
        allowed_skills={
            "workspace-summary": EdgeSkillSpec(
                name="workspace-summary", sha256=_RO_SHA, read_only=True
            ),
            "skill-read": EdgeSkillSpec(
                name="skill-read",
                sha256=_RO_SHA,
                read_only=True,
                params_schema={"allowed_params": {"path": "str"}, "required": ["path"]},
            ),
            "apply-refactor": EdgeSkillSpec(
                name="apply-refactor",
                sha256=_MUT_SHA,
                read_only=False,
                params_schema={"allowed_params": {"target": "str"}, "required": ["target"]},
            ),
        },
    )


def _refactor_handler(spec, params: dict, root: Path) -> dict:
    (root / "refactor-marker.txt").write_text(
        f"refactored:{params.get('target')}", encoding="utf-8"
    )
    return {"ok": True, "summary": f"Refactored {params.get('target')} on the edge device."}


async def _wait_session(device_id: str, workspace_id: str, timeout_s: float = 15.0) -> None:
    key = f"{device_id}:{workspace_id}"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if key in server_cloud._EDGE_SESSIONS:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"edge session {key!r} never registered on the gateway")


async def _wait_task_final(client: httpx.AsyncClient, task_id: str, timeout_s: float = 20.0):
    deadline = time.monotonic() + timeout_s
    data: dict = {}
    while time.monotonic() < deadline:
        data = (await client.get(f"/api/tasks/{task_id}")).json()
        if data.get("status") in _FINAL_STATES:
            return data
        await asyncio.sleep(0.05)
    return data


async def _wait_pending_action(client: httpx.AsyncClient, task_id: str, timeout_s: float = 15.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        actions = (await client.get("/api/approvals", params={"task_id": task_id})).json().get(
            "pending_actions"
        ) or []
        if actions:
            return actions[0]
        await asyncio.sleep(0.05)
    raise AssertionError(f"no pending approval action appeared for task {task_id}")


@asynccontextmanager
async def _live_delegation_stack(live_gateway, handler: EdgeDelegationHandler, token: str):
    runner = EdgeRunner(
        f"ws://127.0.0.1:{live_gateway['port']}/api/edge/ws",
        token=token,
        device_id="dev-del",
        workspace_id="ws-del",
        workspace_root=live_gateway["ws_dir"],
        caps=EdgeCapabilities(),
        delegation_handler=handler,
    )
    runner_task = asyncio.get_running_loop().create_task(runner.run_forever())
    try:
        await _wait_session("dev-del", "ws-del")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{live_gateway['port']}",
            timeout=20.0,
            trust_env=False,
        ) as client:
            yield client
    finally:
        runner.stop()
        runner_task.cancel()
        with suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(runner_task, 5.0)


def _issue_delegate_token(live_gateway) -> str:
    raw, _rec = live_gateway["store"].issue_token(
        "dev-del",
        "ws-del",
        scopes=["task_pull:ws-del", "result_push:ws-del", "delegate_local:ws-del"],
    )
    return raw


def test_cloud_edge_delegation_readonly_roundtrip(live_gateway):
    edge_root: Path = live_gateway["ws_dir"]
    (edge_root / "notes.md").write_text("edge-side notes", encoding="utf-8")
    (edge_root / "pinned-skill.md").write_bytes(b"pinned-skill-body-v1")
    policy = _delegation_policy()
    # Pin skill-read to the real on-disk bytes so the edge verifies the digest.
    policy.allowed_skills["skill-read"].sha256 = hashlib.sha256(b"pinned-skill-body-v1").hexdigest()
    handler = EdgeDelegationHandler(policy, edge_root)
    token = _issue_delegate_token(live_gateway)

    async def main() -> None:
        async with _live_delegation_stack(live_gateway, handler, token) as client:
            devices = (await client.get("/api/edge/devices")).json()["devices"]
            mine = [d for d in devices if d["device_id"] == "dev-del"]
            assert mine and mine[0]["healthy"] is True and mine[0]["can_delegate"] is True

            res = await client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "dev-del",
                    "workspace_id": "ws-del",
                    "skill": "workspace-summary",
                },
            )
            assert res.status_code == 202
            body = res.json()
            assert body["approval_required"] is False
            done = await _wait_task_final(client, body["task_id"])
            assert done["status"] == "DONE", done
            assert done["result_data"]["ok"] is True
            assert "notes.md" in [e["path"] for e in done["result_data"]["entries"]]

            # Digest-pinned file read executed on the edge filesystem.
            res2 = await client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "dev-del",
                    "workspace_id": "ws-del",
                    "skill": "skill-read",
                    "params": {"path": "pinned-skill.md"},
                },
            )
            assert res2.status_code == 202
            done2 = await _wait_task_final(client, res2.json()["task_id"])
            assert done2["status"] == "DONE", done2
            assert "pinned-skill-body-v1" in str(done2["result_data"].get("content"))

    asyncio.run(main())


def test_cloud_edge_delegation_approval_gate_then_remote_execution(live_gateway):
    edge_root: Path = live_gateway["ws_dir"]
    policy = EdgeDelegationPolicy(
        enabled=True,
        allowed_skills={
            "apply-refactor": EdgeSkillSpec(
                name="apply-refactor",
                sha256=_MUT_SHA,
                read_only=False,
                params_schema={"allowed_params": {"target": "str"}, "required": ["target"]},
            ),
        },
    )
    handler = EdgeDelegationHandler(policy, edge_root)
    handler.register_handler("apply-refactor", _refactor_handler)
    token = _issue_delegate_token(live_gateway)

    async def main() -> None:
        async with _live_delegation_stack(live_gateway, handler, token) as client:
            res = await client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "dev-del",
                    "workspace_id": "ws-del",
                    "skill": "apply-refactor",
                    "sha256": _MUT_SHA,
                    "params": {"target": "src"},
                    "task": "Integration refactor probe",
                },
            )
            assert res.status_code == 202
            body = res.json()
            assert body["approval_required"] is True

            action = await _wait_pending_action(client, body["task_id"])
            assert action["tool_name"] == "edge_delegate:apply-refactor"
            assert action["preview"]

            dec = await client.post(
                f"/api/approvals/{action['action_id']}/decide", json={"approved": True}
            )
            assert dec.status_code == 200

            done = await _wait_task_final(client, body["task_id"])
            assert done["status"] == "DONE", done
            assert "Refactored src" in (done["result"] or "")
            # Proof of real remote execution: the edge handler wrote the file.
            assert (edge_root / "refactor-marker.txt").read_text(encoding="utf-8") == (
                "refactored:src"
            )

    asyncio.run(main())


def test_cloud_edge_delegation_rejects_commands_and_bad_digests(live_gateway):
    handler = EdgeDelegationHandler(_delegation_policy(), live_gateway["ws_dir"])
    token = _issue_delegate_token(live_gateway)

    async def main() -> None:
        async with _live_delegation_stack(live_gateway, handler, token) as client:
            # Raw shell/command smuggling — plain and NFKC-folded keys.
            for bad_params in ({"command": "whoami"}, {"note": {"ｃｍｄ": "x"}}):
                res = await client.post(
                    "/api/edge/delegate",
                    json={
                        "device_id": "dev-del",
                        "workspace_id": "ws-del",
                        "skill": "workspace-summary",
                        "params": bad_params,
                    },
                )
                assert res.status_code == 400, bad_params
                assert "Forbidden delegation parameter key" in res.json()["detail"]

            # Wrong digest pin -> 409
            res = await client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "dev-del",
                    "workspace_id": "ws-del",
                    "skill": "workspace-summary",
                    "sha256": "0" * 64,
                },
            )
            assert res.status_code == 409

            # Non-ASCII digests must fail as a clean mismatch, never a 500.
            res = await client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "dev-del",
                    "workspace_id": "ws-del",
                    "skill": "workspace-summary",
                    "sha256": "ą" * 64,
                },
            )
            assert res.status_code == 409

            # Skill outside the advertised digest allowlist -> 403
            res = await client.post(
                "/api/edge/delegate",
                json={"device_id": "dev-del", "workspace_id": "ws-del", "skill": "not-pinned"},
            )
            assert res.status_code == 403

    asyncio.run(main())


def test_cloud_edge_delegation_optin_disabled_rejected(live_gateway):
    handler = EdgeDelegationHandler(EdgeDelegationPolicy(), live_gateway["ws_dir"])  # opt-in off
    token = _issue_delegate_token(live_gateway)

    async def main() -> None:
        async with _live_delegation_stack(live_gateway, handler, token) as client:
            devices = (await client.get("/api/edge/devices")).json()["devices"]
            mine = [d for d in devices if d["device_id"] == "dev-del"]
            assert mine and mine[0]["can_delegate"] is False

            res = await client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "dev-del",
                    "workspace_id": "ws-del",
                    "skill": "workspace-summary",
                },
            )
            assert res.status_code == 403
            assert "disabled" in res.json()["detail"].lower()

    asyncio.run(main())
