# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for Two-tier QoS Control Lane, Lease TTL Failover, and --edge-daemon Loopback IPC."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import botex.server.server_cloud as server_cloud
from botex.core.device_tokens import DeviceTokenRecord
from botex.core.swarm_store import (
    close_swarm_store_dbs,
    get_swarm_store_conn,
    register_bot,
    upsert_host_lease,
)
from botex.edge.runner import (
    CONTROL_LANE_FRAME_TYPES,
    LOOPBACK_CONTROL_ALLOWLIST,
    MAX_ENROLLED_WORKSPACES,
    QOS_PRIORITY_CONTROL,
    QOS_PRIORITY_DATA,
    TYPED_CONTROL_FRAME_TYPES,
    EdgeActiveOrdersStore,
    EdgeCapabilities,
    EdgeHostDaemon,
    EdgeOutboxBuffer,
    EdgeRunner,
    EdgeWorkspaceAgent,
    attach_or_spawn_edge_daemon,
    frame_qos_priority,
    probe_edge_daemon_loopback,
)


class _DummyWebSocket:
    def __init__(self) -> None:
        self.sent_frames: List[Dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.sent_frames.append(json.loads(raw))

    async def send_json(self, payload: Dict[str, Any]) -> None:
        self.sent_frames.append(dict(payload))


def test_qos_control_lane_preempts_data_plane(tmp_path: Path) -> None:
    """Control Lane frames (fence_epoch, ping/pong, heartbeat, brief_sync) must transmit
    before queued Data Plane frames (tool_result, logs, chunks) in both EdgeRunner and _EdgeSession."""
    assert frame_qos_priority({"type": "fence_epoch"}) == QOS_PRIORITY_CONTROL
    assert frame_qos_priority({"type": "heartbeat"}) == QOS_PRIORITY_CONTROL
    assert frame_qos_priority({"type": "ping"}) == QOS_PRIORITY_CONTROL
    assert frame_qos_priority({"type": "pong"}) == QOS_PRIORITY_CONTROL
    assert frame_qos_priority({"type": "brief_sync"}) == QOS_PRIORITY_CONTROL
    assert frame_qos_priority({"type": "tool_result"}) == QOS_PRIORITY_DATA
    assert frame_qos_priority({"type": "tool_call"}) == QOS_PRIORITY_DATA

    async def _exercise() -> None:
        # 1. EdgeRunner outbox priority preemption
        runner = EdgeRunner(
            "ws://127.0.0.1:9/api/edge/ws",
            token="btxd_test.secret",
            device_id="host-x",
            workspace_id="ws-1",
            workspace_root=tmp_path,
        )
        ws_runner = _DummyWebSocket()

        # Enqueue 3 bulky data plane frames followed by 2 control lane frames
        for idx in range(3):
            runner.enqueue_frame(
                {
                    "type": "tool_result",
                    "call_id": f"data-{idx}",
                    "result": {"stdout": "X" * 4096},
                }
            )
        runner.enqueue_frame({"type": "fence_epoch", "order_id": "ord-1", "fencing_epoch": 3})
        runner.enqueue_frame({"type": "pong", "ts": 100.0})

        flushed = await runner.flush_outbox(ws_runner)
        assert [f["type"] for f in flushed] == [
            "fence_epoch",
            "pong",
            "tool_result",
            "tool_result",
            "tool_result",
        ]
        assert [f.get("call_id") for f in flushed[2:]] == ["data-0", "data-1", "data-2"]

        # 2. _EdgeSession outbox priority preemption on the Cloud Gateway side
        dummy_tok = DeviceTokenRecord(
            jti="jti-qos",
            device_id="host-x",
            workspace_id="ws-1",
            scopes=["task_pull:ws-1", "result_push:ws-1"],
            digest_sha256="h",
        )
        ws_cloud = _DummyWebSocket()
        sess = server_cloud._EdgeSession(
            websocket=ws_cloud,  # type: ignore[arg-type]
            token_record=dummy_tok,
            device_id="host-x",
            workspace_id="ws-1",
            capabilities={},
        )
        sess.enqueue_frame({"type": "tool_call", "call_id": "c-1", "tool_name": "read_file"})
        sess.enqueue_frame({"type": "tool_call", "call_id": "c-2", "tool_name": "list_dir"})
        sess.enqueue_frame({"type": "fence_epoch", "order_id": "ord-1", "fencing_epoch": 4})
        sess.enqueue_frame({"type": "ping", "ts": 200.0})

        cloud_flushed = await sess.flush_outbox()
        assert [f["type"] for f in cloud_flushed] == [
            "fence_epoch",
            "ping",
            "tool_call",
            "tool_call",
        ]
        assert [f.get("call_id") for f in cloud_flushed[2:]] == ["c-1", "c-2"]

    asyncio.run(_exercise())


def test_lease_ttl_expiry_bumps_epoch_and_failovers_host_x_to_y(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Host X's lease exceeds TTL=10min without heartbeat, fencing_epoch bumps e2 -> e3,
    WorkOrder fails over from last checkpoint to healthy Host Y, and Host X receives fence_epoch on reconnect."""
    db_file = tmp_path / "swarm_store.db"
    monkeypatch.setenv("BOTEX_SWARM_STORE_DB", str(db_file))
    close_swarm_store_dbs()
    server_cloud._PENDING_HOST_FENCES.clear()

    try:
        reg = register_bot("night-coder", "harness", "Overnight coding worker", db_path=db_file)
        assert reg["ok"] is True

        # Acquire lease on Host X at epoch 2 with TTL=600s
        lease_res = upsert_host_lease(
            "night-coder",
            "host-x",
            "repo/core",
            "running order-77",
            fencing_token=2,
            ttl_s=600,
            db_path=db_file,
        )
        assert lease_res["ok"] is True
        assert lease_res["fencing_token"] == 2

        conn = get_swarm_store_conn(db_file)
        conn.execute(
            "INSERT INTO work_orders (order_id, bot_slug, epoch, status, payload_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?);",
            (
                "order-77",
                "night-coder",
                2,
                "RUNNING",
                json.dumps({"last_checkpoint_ref": "swarm/wip/order-77/e2"}),
                "2026-11-19T00:00:00+00:00",
                "2026-11-19T00:00:00+00:00",
            ),
        )
        conn.commit()

        # Simulate >10 minutes elapsed without heartbeat from Host X while Host Y is healthy
        future_now = time.time() + 605.0

        async def _run_failover_and_reconnect() -> None:
            failovers = await server_cloud.sweep_expired_host_leases(
                now=future_now,
                candidate_hosts=["host-y"],
                db_path=db_file,
            )
            assert len(failovers) == 1
            ev = failovers[0]
            assert ev["type"] == "fence_epoch"
            assert ev["order_id"] == "order-77"
            assert ev["stale_host"] == "host-x"
            assert ev["target_host"] == "host-y"
            assert ev["stale_epoch"] == 2
            assert ev["fencing_epoch"] == 3
            assert ev["checkpoint_ref"] == "swarm/wip/order-77/e2"

            # Verify DB state updated to e3 and assigned to host-y
            row = conn.execute(
                "SELECT epoch, status, payload_json FROM work_orders WHERE order_id = 'order-77';"
            ).fetchone()
            assert int(row["epoch"]) == 3
            assert row["status"] == "HANDOFF"
            payload = json.loads(row["payload_json"])
            assert payload["assigned_host"] == "host-y"
            assert payload["stale_host"] == "host-x"
            assert payload["resume_from_checkpoint"] == "swarm/wip/order-77/e2"

            lrow = conn.execute(
                "SELECT host_id, fencing_token FROM host_leases WHERE bot_slug = 'night-coder';"
            ).fetchone()
            assert lrow["host_id"] == "host-y"
            assert int(lrow["fencing_token"]) == 3

            # Now Host X reconnects — queued fence_epoch must be delivered on its Control Lane
            dummy_tok = DeviceTokenRecord(
                jti="jti-x",
                device_id="host-x",
                workspace_id="ws-1",
                scopes=["task_pull:ws-1", "result_push:ws-1"],
                digest_sha256="h",
            )
            ws_x = _DummyWebSocket()
            sess_x = server_cloud._EdgeSession(
                websocket=ws_x,  # type: ignore[arg-type]
                token_record=dummy_tok,
                device_id="host-x",
                workspace_id="ws-1",
                capabilities={},
            )
            delivered = await server_cloud.deliver_pending_host_fences(sess_x)
            assert len(delivered) == 1
            assert ws_x.sent_frames[0]["type"] == "fence_epoch"
            assert ws_x.sent_frames[0]["fencing_epoch"] == 3
            assert ws_x.sent_frames[0]["order_id"] == "order-77"

        asyncio.run(_run_failover_and_reconnect())
    finally:
        close_swarm_store_dbs()
        server_cloud._PENDING_HOST_FENCES.clear()


def test_pre_push_epoch_mismatch_aborts_stale_push(tmp_path: Path) -> None:
    """Auto-checkpoint every 5-8 file-mutating turns must check remote_epoch == local_epoch
    and abort immediately on mismatch without pushing stale work."""
    runner = EdgeRunner(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="host-x",
        workspace_id="ws-1",
        workspace_root=tmp_path,
    )
    pushed_calls: List[tuple] = []

    def fake_push(wt: Path, remote: str, branch: str) -> Dict[str, Any]:
        pushed_calls.append((wt, remote, branch))
        return {"ok": True}

    # Record 5 file-mutating turns -> checkpoint becomes due on turn 5
    for _turn_idx in range(1, 5):
        assert runner.record_order_turn("order-88", mutated_files=True) is False
    assert runner.record_order_turn("order-88", mutated_files=True) is True

    # Remote epoch bumped to 3 while local is 2 -> pre-push check MUST abort
    stale_res = runner.auto_checkpoint_epoch(
        "order-88",
        local_epoch=2,
        fetch_remote_epoch_fn=lambda _oid: 3,
        push_fn=fake_push,
    )
    assert stale_res["ok"] is False
    assert stale_res["status"] == "stale_epoch"
    assert stale_res["error"] == "EPOCH_MISMATCH"
    assert stale_res["local_epoch"] == 2
    assert stale_res["remote_epoch"] == 3
    assert pushed_calls == []

    # Matching epoch (3 == 3) succeeds and resets turn counter
    ok_res = runner.auto_checkpoint_epoch(
        "order-88",
        local_epoch=3,
        fetch_remote_epoch_fn=lambda _oid: 3,
        push_fn=fake_push,
    )
    assert ok_res["ok"] is True
    assert ok_res["status"] == "pushed"
    assert ok_res["branch"] == "swarm/wip/order-88/e3"
    assert len(pushed_calls) == 1
    assert runner.record_order_turn("order-88", mutated_files=False) is False


def test_edge_daemon_attach_or_spawn_and_loopback_ipc_auth(tmp_path: Path) -> None:
    """--edge-daemon must bind 127.0.0.1 loopback IPC, reject bad tokens/cross-origin requests,
    and attach to an already-running daemon instead of spawning a duplicate."""
    state_file = tmp_path / "edge_daemon.json"
    lock_file = tmp_path / "edge_daemon.lock"

    runner = EdgeRunner(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="dev-loopback",
        workspace_id="ws-loopback",
        workspace_root=tmp_path,
    )

    async def _exercise_loopback() -> None:
        info = await runner.start_loopback_ipc(
            port=0,
            loopback_token="local-secret-tok-123",
            state_file=state_file,
        )
        try:
            assert info["ok"] is True
            assert info["host"] == "127.0.0.1"
            port = int(info["port"])
            assert port > 0
            assert state_file.is_file()

            # 1. Valid token probe succeeds
            probe_ok = await asyncio.to_thread(
                probe_edge_daemon_loopback, port, "local-secret-tok-123"
            )
            assert probe_ok["ok"] is True
            assert probe_ok["device_id"] == "dev-loopback"
            assert probe_ok["workspace_id"] == "ws-loopback"

            # 2. Invalid token is rejected with 401
            probe_bad = await asyncio.to_thread(probe_edge_daemon_loopback, port, "wrong-token")
            assert probe_bad["ok"] is False
            assert probe_bad.get("status_code") == 401

            # 3. Cross-origin header is rejected with 403
            probe_cross = await asyncio.to_thread(
                probe_edge_daemon_loopback,
                port,
                "local-secret-tok-123",
                origin="https://evil.example.com",
            )
            assert probe_cross["ok"] is False
            assert probe_cross.get("status_code") == 403

            # 4. attach_or_spawn_edge_daemon attaches to the healthy running daemon
            attach_res = await asyncio.to_thread(
                attach_or_spawn_edge_daemon,
                state_file=state_file,
                lock_path=lock_file,
            )
            assert attach_res["ok"] is True
            assert attach_res["mode"] == "attached"
            assert attach_res["port"] == port
            assert attach_res["device_id"] == "dev-loopback"
        finally:
            await runner.stop_loopback_ipc()

    asyncio.run(_exercise_loopback())

    # 5. After daemon stops, attach_or_spawn_edge_daemon acquires lock and enters "spawned" mode
    spawn_res = attach_or_spawn_edge_daemon(
        state_file=state_file,
        lock_path=lock_file,
        loopback_port=0,
    )
    try:
        assert spawn_res["ok"] is True
        assert spawn_res["mode"] == "spawned"
        assert spawn_res["lock"].locked is True
    finally:
        spawn_res["lock"].release()


def test_loopback_header_dos_guard_and_outbox_drain(tmp_path: Path) -> None:
    """Loopback IPC rejects oversized header floods with 413, and outbox drain fails pending futures."""
    runner = EdgeRunner(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="dev-dos",
        workspace_id="ws-dos",
        workspace_root=tmp_path,
    )

    async def _exercise() -> None:
        info = await runner.start_loopback_ipc(
            port=0,
            loopback_token="tok-dos",
            state_file=tmp_path / "edge_daemon.json",
        )
        try:
            port = int(info["port"])
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                flood = ["GET /status HTTP/1.1", f"Host: 127.0.0.1:{port}"]
                for i in range(70):
                    flood.append(f"X-Pad-{i}: val")
                flood.append("\r\n")
                writer.write("\r\n".join(flood).encode("ascii"))
                await writer.drain()
                raw_resp = await asyncio.wait_for(reader.read(4096), timeout=2.0)
                assert b"413 Payload Too Large" in raw_resp
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await runner.stop_loopback_ipc()

        # Verify _drain_outbox fails pending futures instead of hanging callers
        fut = runner.enqueue_frame({"type": "tool_result", "call_id": "pending-1"})
        assert not fut.done()
        drained = runner._drain_outbox(ConnectionError("closed"))
        assert drained == 1
        assert fut.done()
        with pytest.raises(ConnectionError):
            fut.result()

    asyncio.run(_exercise())


def test_sweep_skips_fenced_and_needs_human_orders_without_epoch_inflation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEEDS_HUMAN and FENCED orders must not be re-fenced or re-handed off on repeated sweeps."""
    db_file = tmp_path / "swarm_store.db"
    monkeypatch.setenv("BOTEX_SWARM_STORE_DB", str(db_file))
    close_swarm_store_dbs()
    server_cloud._PENDING_HOST_FENCES.clear()

    try:
        assert register_bot("human-bot", "harness", "Human gated", db_path=db_file)["ok"] is True
        assert (
            upsert_host_lease(
                "human-bot",
                "host-x",
                "repo/human",
                "waiting",
                fencing_token=1,
                ttl_s=600,
                db_path=db_file,
            )["ok"]
            is True
        )
        conn = get_swarm_store_conn(db_file)
        conn.execute(
            "INSERT INTO work_orders (order_id, bot_slug, epoch, status, payload_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?);",
            (
                "ord-human",
                "human-bot",
                1,
                "NEEDS_HUMAN",
                "{}",
                "2026-11-19T00:00:00+00:00",
                "2026-11-19T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO work_orders (order_id, bot_slug, epoch, status, payload_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?);",
            (
                "ord-fenced",
                "human-bot",
                2,
                "FENCED",
                "{}",
                "2026-11-19T00:00:00+00:00",
                "2026-11-19T00:00:00+00:00",
            ),
        )
        conn.commit()

        future_now = time.time() + 700.0
        first = asyncio.run(
            server_cloud.sweep_expired_host_leases(
                now=future_now,
                candidate_hosts=[],
                db_path=db_file,
            )
        )
        assert len(first) == 1
        second = asyncio.run(
            server_cloud.sweep_expired_host_leases(
                now=future_now + 60.0,
                candidate_hosts=[],
                db_path=db_file,
            )
        )
        assert second == []

        row_h = conn.execute(
            "SELECT epoch, status FROM work_orders WHERE order_id = 'ord-human';"
        ).fetchone()
        row_f = conn.execute(
            "SELECT epoch, status FROM work_orders WHERE order_id = 'ord-fenced';"
        ).fetchone()
        assert (int(row_h["epoch"]), row_h["status"]) == (1, "NEEDS_HUMAN")
        assert (int(row_f["epoch"]), row_f["status"]) == (2, "FENCED")
    finally:
        close_swarm_store_dbs()
        server_cloud._PENDING_HOST_FENCES.clear()


def test_sweep_skips_healthy_host_orders_for_same_bot_slug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-2: Expired lease on host-x must never fence a healthy RUNNING order on host-y for same bot."""
    db_file = tmp_path / "swarm_store.db"
    monkeypatch.setenv("BOTEX_SWARM_STORE_DB", str(db_file))
    close_swarm_store_dbs()
    server_cloud._PENDING_HOST_FENCES.clear()

    try:
        assert register_bot("night-coder", "harness", "Coder", db_path=db_file)["ok"] is True
        # host-x lease expired, host-y lease active
        assert (
            upsert_host_lease(
                "night-coder",
                "host-x",
                "repo/a",
                "stale",
                fencing_token=1,
                ttl_s=10,
                db_path=db_file,
            )["ok"]
            is True
        )
        assert (
            upsert_host_lease(
                "night-coder",
                "host-y",
                "repo/b",
                "healthy",
                fencing_token=1,
                ttl_s=600,
                db_path=db_file,
            )["ok"]
            is True
        )
        conn = get_swarm_store_conn(db_file)
        conn.execute(
            "INSERT INTO work_orders (order_id, bot_slug, epoch, status, payload_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?);",
            (
                "ord-healthy-y",
                "night-coder",
                1,
                "RUNNING",
                json.dumps({"assigned_host": "host-y", "scope_path": "repo/b"}),
                "2026-11-19T00:00:00+00:00",
                "2026-11-19T00:00:00+00:00",
            ),
        )
        conn.commit()

        # Only host-x (ttl=10) is expired at now + 30; host-y (ttl=600) is still active.
        asyncio.run(
            server_cloud.sweep_expired_host_leases(
                now=time.time() + 30.0,
                candidate_hosts=["host-y"],
                db_path=db_file,
            )
        )
        row_y = conn.execute(
            "SELECT epoch, status FROM work_orders WHERE order_id = 'ord-healthy-y';"
        ).fetchone()
        assert int(row_y["epoch"]) == 1
        assert row_y["status"] == "RUNNING"
    finally:
        close_swarm_store_dbs()
        server_cloud._PENDING_HOST_FENCES.clear()


def test_auto_checkpoint_epoch_stages_and_commits_new_file_before_push(tmp_path: Path) -> None:
    """P1-3: EdgeRunner.auto_checkpoint_epoch must stage+commit new working-dir files before push."""
    import subprocess

    remote_bare = tmp_path / "remote.git"
    work_repo = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote_bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(work_repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work_repo), "config", "user.email", "bot@example.invalid"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "config", "user.name", "Bot Test"],
        check=True,
        capture_output=True,
    )
    (work_repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work_repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work_repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "remote", "add", "swarm", str(remote_bare)],
        check=True,
        capture_output=True,
    )

    runner = EdgeRunner(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="host-x",
        workspace_id="ws-git",
        workspace_root=work_repo,
    )
    # Modify/create a new file without manually committing it
    (work_repo / "new_feature.py").write_text("VALUE = 42\n", encoding="utf-8")
    for _ in range(5):
        runner.record_order_turn("order-git", mutated_files=True)

    res = runner.auto_checkpoint_epoch(
        "order-git",
        local_epoch=1,
        remote_epoch=1,
        swarm_remote="swarm",
    )
    assert res["ok"] is True, res
    tree = subprocess.run(
        ["git", "-C", str(remote_bare), "ls-tree", "-r", "--name-only", "swarm/wip/order-git/e1"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "new_feature.py" in tree.stdout.splitlines()


def test_loopback_control_allowlist_and_body_size_guard(tmp_path: Path) -> None:
    """P2-2: POST /control rejects disallowed frame types (400) and bodies > 65536 bytes (413)."""
    runner = EdgeRunner(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="dev-ctl",
        workspace_id="ws-ctl",
        workspace_root=tmp_path,
    )

    async def _exercise() -> None:
        info = await runner.start_loopback_ipc(
            port=0,
            loopback_token="tok-ctl",
            state_file=tmp_path / "edge_daemon.json",
        )
        try:
            port = int(info["port"])

            # 1. Oversized Content-Length -> 413 without reading truncated JSON
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                req = (
                    f"POST /control HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    f"X-Loopback-Token: tok-ctl\r\n"
                    f"Content-Length: 70000\r\n\r\n"
                )
                writer.write(req.encode("ascii"))
                await writer.drain()
                raw = await asyncio.wait_for(reader.read(4096), timeout=2.0)
                assert b"413 Payload Too Large" in raw
            finally:
                writer.close()
                await writer.wait_closed()

            # 2. Disallowed control frame type -> 400 Bad Request
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
            try:
                body = json.dumps({"frame": {"type": "arbitrary_exec"}}).encode("utf-8")
                req2 = (
                    f"POST /control HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    f"X-Loopback-Token: tok-ctl\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode("ascii") + body
                writer2.write(req2)
                await writer2.drain()
                raw2 = await asyncio.wait_for(reader2.read(4096), timeout=2.0)
                assert b"400 Bad Request" in raw2
            finally:
                writer2.close()
                await writer2.wait_closed()
        finally:
            await runner.stop_loopback_ipc()

    asyncio.run(_exercise())


def test_hello_and_heartbeat_include_harness_inventory_and_last_seen(tmp_path: Path) -> None:
    """Plan 042: hello and heartbeat frames include discovered harnesses and last_seen."""
    fake_inventory = [
        {
            "harness_id": "devin",
            "transport": "acp",
            "status": "online",
            "last_seen": "2026-11-23T14:00:00+00:00",
            "capabilities": {"available": True},
        }
    ]

    async def _fake_discover() -> list[dict]:
        return list(fake_inventory)

    runner = EdgeRunner(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="dev-inv",
        workspace_id="ws-inv",
        workspace_root=tmp_path,
        discover_harnesses_fn=_fake_discover,
    )

    async def _run() -> None:
        hello = await runner.build_hello_frame()
        assert hello["type"] == "hello"
        assert hello["harnesses"] == fake_inventory
        assert isinstance(hello["last_seen"], str) and hello["last_seen"]

        hb = await runner.build_heartbeat_frame()
        assert hb["type"] == "heartbeat"
        assert hb["device_id"] == "dev-inv"
        assert hb["workspace_id"] == "ws-inv"
        assert hb["harnesses"] == fake_inventory
        assert isinstance(hb["last_seen"], str) and hb["last_seen"]

    asyncio.run(_run())


def test_typed_control_frames_verify_fencing_token_and_return_ack_nack(tmp_path: Path) -> None:
    """Plan 042: harness_pause/resume/cancel verify fencing_token and return ack/nack."""
    calls: list[tuple[str, int]] = []

    class _FakeDriver:
        def pause_process_tree(self, pid: int, *, job_guard=None) -> dict:
            calls.append(("pause", pid))
            return {
                "ok": True,
                "action": "pause",
                "pid": pid,
                "affected_pids": [pid],
                "detail": "ok",
            }

        def resume_process_tree(self, pid: int, *, job_guard=None) -> dict:
            calls.append(("resume", pid))
            return {
                "ok": True,
                "action": "resume",
                "pid": pid,
                "affected_pids": [pid],
                "detail": "ok",
            }

        def cancel_process_tree(self, pid: int, *, job_guard=None) -> dict:
            calls.append(("cancel", pid))
            return {"ok": True, "action": "cancel", "pid": pid, "detail": "ok"}

    runner = EdgeRunner(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="dev-ctrl",
        workspace_id="ws-ctrl",
        workspace_root=tmp_path,
        os_driver=_FakeDriver(),
    )
    runner.register_active_order("ord-42", pid=4242, fencing_token="tok-epoch-2", fencing_epoch=2)

    async def _run() -> None:
        bad_type = await runner.handle_control_frame(
            {"type": "shell_exec", "order_id": "ord-42", "fencing_token": "tok-epoch-2"}
        )
        assert bad_type["type"] == "control_nack"
        assert bad_type["reason"] == "unsupported_control_frame"

        bad_tok = await runner.handle_control_frame(
            {"type": "harness_pause", "order_id": "ord-42", "fencing_token": "wrong"}
        )
        assert bad_tok["type"] == "control_nack"
        assert bad_tok["reason"] == "fencing_token_mismatch"

        stale_ep = await runner.handle_control_frame(
            {
                "type": "harness_pause",
                "order_id": "ord-42",
                "fencing_token": "tok-epoch-2",
                "fencing_epoch": 1,
            }
        )
        assert stale_ep["type"] == "control_nack"
        assert stale_ep["reason"] == "stale_fencing_epoch"

        ack_p = await runner.handle_control_frame(
            {
                "type": "harness_pause",
                "request_id": "r1",
                "order_id": "ord-42",
                "fencing_token": "tok-epoch-2",
                "fencing_epoch": 2,
            }
        )
        assert ack_p["type"] == "control_ack"
        assert ack_p["ok"] is True
        assert ack_p["request_id"] == "r1"

        ack_r = await runner.handle_control_frame(
            {"type": "harness_resume", "order_id": "ord-42", "fencing_token": "tok-epoch-2"}
        )
        assert ack_r["type"] == "control_ack"

        ack_c = await runner.handle_control_frame(
            {"type": "harness_cancel_task", "order_id": "ord-42", "fencing_token": "tok-epoch-2"}
        )
        assert ack_c["type"] == "control_ack"
        assert calls == [("pause", 4242), ("resume", 4242), ("cancel", 4242)]

        # After cancel, order is unregistered -> nack
        after_c = await runner.handle_control_frame(
            {"type": "harness_pause", "order_id": "ord-42", "fencing_token": "tok-epoch-2"}
        )
        assert after_c["type"] == "control_nack"
        assert after_c["reason"] == "order_not_active"

    asyncio.run(_run())


def test_loopback_control_accepts_typed_harness_frames_with_fencing_token(tmp_path: Path) -> None:
    """Plan 042: loopback POST /control allows typed harness_* frames only with fencing_token."""

    class _FakeDriver:
        def pause_process_tree(self, pid: int, *, job_guard=None) -> dict:
            return {
                "ok": True,
                "action": "pause",
                "pid": pid,
                "affected_pids": [pid],
                "detail": "ok",
            }

    runner = EdgeRunner(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="dev-lb",
        workspace_id="ws-lb",
        workspace_root=tmp_path,
        os_driver=_FakeDriver(),
    )
    runner.register_active_order("ord-lb", pid=5150, fencing_token="tok-lb", fencing_epoch=1)

    async def _post(port: int, payload: dict) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            body = json.dumps(payload).encode("utf-8")
            req = (
                f"POST /control HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"X-Loopback-Token: tok-ctl\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode("ascii") + body
            writer.write(req)
            await writer.drain()
            return await asyncio.wait_for(reader.read(4096), timeout=2.0)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _exercise() -> None:
        info = await runner.start_loopback_ipc(
            port=0,
            loopback_token="tok-ctl",
            state_file=tmp_path / "edge_daemon.json",
        )
        try:
            port = int(info["port"])
            missing_tok = await _post(
                port, {"frame": {"type": "harness_pause", "order_id": "ord-lb"}}
            )
            assert b"400 Bad Request" in missing_tok
            assert b"missing_fencing_token_or_order_id" in missing_tok

            ok_resp = await _post(
                port,
                {
                    "frame": {
                        "type": "harness_pause",
                        "order_id": "ord-lb",
                        "fencing_token": "tok-lb",
                    },
                    "local_exec": True,
                },
            )
            assert b"200 OK" in ok_resp
            assert b"control_ack" in ok_resp
        finally:
            await runner.stop_loopback_ipc()

    asyncio.run(_exercise())


# ---------------------------------------------------------------------------
# Plan 051 — EdgeHostDaemon, dispatch_work_order, durable persistence, F3/F5/F7
# ---------------------------------------------------------------------------


_WS_A = "ws_aaaabbbb0001"
_WS_B = "ws_ccccddee0002"


def _ws_entry(
    ws_id: str,
    root: Path,
    *,
    state: str = "enrolled",
    local_admin: bool = False,
) -> Dict[str, Any]:
    return {
        "workspace_id": ws_id,
        "root": str(root),
        "label": ws_id,
        "state": state,
        "added_at": "2026-11-24T00:00:00+00:00",
        "source": "manual",
        "local_admin": local_admin,
        "caps_overrides": {},
    }


def _write_registry(path: Path, entries: List[Dict[str, Any]]) -> None:
    from botex.edge.workspaces import save_workspaces_registry

    save_workspaces_registry(
        {"version": 1, "device_id": "host_test01", "workspaces": entries}, path=path
    )


class _FakeInventoryService:
    def __init__(self, snapshot: List[Dict[str, Any]]) -> None:
        self._snapshot = snapshot
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def get_cached_snapshot(self) -> List[Dict[str, Any]]:
        return list(self._snapshot)


class _FakeHostDriver:
    def __init__(self, pre_ok: bool = True) -> None:
        self._pre_ok = pre_ok
        self.prepared: List[tuple] = []

    def preflight_runtime(self, cli_name: str, *a: Any, **k: Any) -> Dict[str, Any]:
        if self._pre_ok:
            return {"ok": True, "status": "allowed", "cli": cli_name}
        return {"ok": False, "status": "infra_blocked_runtime", "reason": "blocked"}

    def prepare_epoch_worktree(self, origin_repo: Any, order_id: str, epoch: int, **k: Any) -> Path:
        self.prepared.append((origin_repo, order_id, epoch))
        return Path(origin_repo)


def _spec_ok(harness: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "status": "READY",
        "harness": harness,
        "order_id": "ord-9",
        "epoch": 1,
        "branch": "swarm/wip/ord-9/e1",
        "worktree": "ord-9_e1",
        "mode": "edit",
        "prompt": "run it",
        "base_ref": "HEAD",
    }


def test_dispatch_frame_constants_and_harness_spawn_cap() -> None:
    """Plan 051 §4.1: dispatch_work_order joins all typed/control sets;
    work_order_event is Control Lane; EdgeCapabilities.harness_spawn round-trips."""
    assert "dispatch_work_order" in TYPED_CONTROL_FRAME_TYPES
    assert "dispatch_work_order" not in LOOPBACK_CONTROL_ALLOWLIST
    assert "dispatch_work_order" in CONTROL_LANE_FRAME_TYPES
    assert "work_order_event" in CONTROL_LANE_FRAME_TYPES
    assert frame_qos_priority({"type": "dispatch_work_order"}) == QOS_PRIORITY_CONTROL
    assert frame_qos_priority({"type": "work_order_event"}) == QOS_PRIORITY_CONTROL

    caps = EdgeCapabilities()
    assert caps.harness_spawn is False
    assert caps.to_dict()["harness_spawn"] is False
    loaded = EdgeCapabilities.from_dict({"harness_spawn": True})
    assert loaded.harness_spawn is True
    assert loaded.to_dict()["harness_spawn"] is True


def test_dispatch_work_order_pipeline_persistence_and_env_scrub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dispatch_work_order: enrolled re-verify -> translate -> preflight -> spawn,
    durable active_orders.json + offline edge_outbox.jsonl, F6 env scrub."""
    ws_root = tmp_path / "ws_a"
    ws_root.mkdir()
    (ws_root / ".git").mkdir()
    cand_root = tmp_path / "ws_cand"
    cand_root.mkdir()
    ws_file = tmp_path / "host_workspaces.json"
    swarm_home = tmp_path / "swarm"
    _write_registry(
        ws_file,
        [
            _ws_entry(_WS_A, ws_root, state="enrolled"),
            _ws_entry(_WS_B, cand_root, state="candidate"),
        ],
    )

    spawned: Dict[str, Any] = {}

    def fake_spawn(argv: List[str], cwd: Path, env: Dict[str, str]) -> Dict[str, Any]:
        spawned.update({"argv": list(argv), "cwd": str(cwd), "env": dict(env)})
        return {"pid": 98765, "job_guard": None}

    agent = EdgeWorkspaceAgent(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="host_test01",
        workspace=_ws_entry(_WS_A, ws_root),
        workspaces_file=ws_file,
        credentials_dir=tmp_path / "creds",
        swarm_home=swarm_home,
        caps=EdgeCapabilities(harness_spawn=True),
        daemon_salt="boot-salt",
        daemon_gen_id="gen-1",
        resolve_token_fn=lambda *a, **k: ("tok-abc", "test"),
        check_eligible_fn=lambda h: {"ok": True, "harness": h, "transport": "acp"},
        translate_order_fn=lambda order, h: _spec_ok(h),
        spawn_fn=fake_spawn,
        host_driver=_FakeHostDriver(),
    )
    monkeypatch.setattr("shutil.which", lambda cli: f"/fake/bin/{cli}")

    async def _run() -> None:
        import os

        os.environ["SWARM_TEST_SECRET"] = "1"
        os.environ["BOTEX_EDGE_TOKEN"] = "leak-me-not"
        try:
            reply = await agent.handle_control_frame(
                {
                    "type": "dispatch_work_order",
                    "request_id": "r-1",
                    "order_id": "ord-9",
                    "fencing_token": "ft-1",
                    "fencing_epoch": 2,
                    "order": {"order_id": "ord-9", "epoch": 1, "goal": "x"},
                    "target_harness": "zcode",
                }
            )
            assert reply["type"] == "control_ack"
            assert reply["ok"] is True
            assert reply["request_id"] == "r-1"
            assert reply["order_id"] == "ord-9"
            assert reply["pid"] == 98765
            assert reply["spec_echo"]["harness"] == "zcode"
            assert spawned["argv"] == [
                "/fake/bin/zcode",
                "--prompt",
                "run it",
                "--mode",
                "edit",
                "--json",
                "--no-color",
            ]
            assert "SWARM_TEST_SECRET" not in spawned["env"]
            assert "BOTEX_EDGE_TOKEN" not in spawned["env"]
            assert "leak-me-not" not in spawned["env"].values()

            # durable record keyed by order_id
            rec = EdgeActiveOrdersStore(swarm_home / "active_orders.json").get("ord-9")
            assert rec is not None
            assert rec["pid"] == 98765
            assert rec["workspace_id"] == _WS_A
            assert rec["fencing_token"] == "ft-1"
            assert rec["fencing_epoch"] == 2
            assert rec["daemon_gen_id"] == "gen-1"
            assert rec["last_seq"] == 1

            # offline -> started event buffered to edge_outbox.jsonl
            events = EdgeOutboxBuffer(swarm_home / "edge_outbox.jsonl").pending()
            assert any(
                e.get("type") == "work_order_event"
                and e.get("order_id") == "ord-9"
                and e.get("workspace_id") == _WS_A
                and e.get("seq") == 1
                and e.get("kind") == "started"
                for e in events
            )

            # candidate workspace is never dispatched
            reply_c = await agent.handle_control_frame(
                {
                    "type": "dispatch_work_order",
                    "order_id": "ord-10",
                    "workspace_id": _WS_B,
                    "fencing_token": "ft-x",
                    "order": {"order_id": "ord-10"},
                }
            )
            assert reply_c["type"] == "control_nack"
            assert reply_c["reason"] == "workspace_not_enrolled"

            # harness_spawn ceiling off -> clean nack, no spawn
            agent.caps.harness_spawn = False
            reply_d = await agent.handle_control_frame(
                {
                    "type": "dispatch_work_order",
                    "order_id": "ord-11",
                    "fencing_token": "ft-x",
                    "order": {"order_id": "ord-11"},
                }
            )
            assert reply_d["type"] == "control_nack"
            assert reply_d["reason"] == "harness_spawn_disabled"
        finally:
            os.environ.pop("SWARM_TEST_SECRET", None)
            os.environ.pop("BOTEX_EDGE_TOKEN", None)

    asyncio.run(_run())


def test_dispatch_work_order_nacks_for_single_workspace_runner(tmp_path: Path) -> None:
    """A plain single-workspace EdgeRunner without harness_spawn nacks cleanly."""
    runner = EdgeRunner(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="dev-single",
        workspace_id="ws-single",
        workspace_root=tmp_path,
    )

    async def _run() -> None:
        reply = await runner.handle_control_frame(
            {
                "type": "dispatch_work_order",
                "order_id": "ord-1",
                "fencing_token": "ft",
                "order": {"order_id": "ord-1"},
            }
        )
        assert reply["type"] == "control_nack"
        assert reply["reason"] == "harness_spawn_disabled"

    asyncio.run(_run())


def test_f5_stale_pid_refusal_marks_order_stale(tmp_path: Path) -> None:
    """F5: control frames refuse signaling when live create-time mismatches."""
    import os

    class _FakeDriver:
        def __init__(self) -> None:
            self.calls: List[str] = []

        def pause_process_tree(self, pid: int, *, job_guard=None) -> Dict[str, Any]:
            self.calls.append("pause")
            return {"ok": True, "action": "pause", "pid": pid}

    driver = _FakeDriver()
    runner = EdgeRunner(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="dev-f5",
        workspace_id="ws-f5",
        workspace_root=tmp_path,
        os_driver=driver,
    )
    runner.register_active_order(
        "ord-stale",
        pid=os.getpid(),
        pid_create_time=1.0,  # recorded in 1970 -> live ctime cannot match
        fencing_token="ft-stale",
        fencing_epoch=1,
    )
    runner.register_active_order(
        "ord-legacy",
        pid=424242,  # never existed -> pid_create_time capture returns None
        fencing_token="ft-legacy",
        fencing_epoch=1,
    )

    async def _run() -> None:
        stale = await runner.handle_control_frame(
            {"type": "harness_pause", "order_id": "ord-stale", "fencing_token": "ft-stale"}
        )
        assert stale["type"] == "control_nack"
        assert stale["reason"] == "stale_pid"
        assert runner._active_order_controls["ord-stale"]["stale"] is True
        assert driver.calls == []

        legacy = await runner.handle_control_frame(
            {
                "type": "harness_pause",
                "order_id": "ord-legacy",
                "fencing_token": "ft-legacy",
            }
        )
        # no recorded create time -> check skipped; driver decides the outcome
        assert legacy["type"] == "control_ack"
        assert driver.calls == ["pause"]

    asyncio.run(_run())


def test_edge_workspace_agent_manifest_jitter_and_outbox_replay(tmp_path: Path) -> None:
    """Agents emit zero-path workspaces_manifest, salted jitter, and replay the
    durable outbox in (order_id, seq) order after hello_ack."""
    ws_root = tmp_path / "ws_a"
    ws_root.mkdir()
    (ws_root / ".git").mkdir()
    ws_file = tmp_path / "host_workspaces.json"
    swarm_home = tmp_path / "swarm"
    swarm_home.mkdir()
    _write_registry(
        ws_file,
        [
            _ws_entry(_WS_A, ws_root),
            _ws_entry(_WS_B, ws_root, state="disabled"),
        ],
    )
    snapshot = [
        {
            "harness_id": "zcode",
            "status": "online",
            "limits": {"unit": "requests", "confidence": "exact"},
        }
    ]
    svc = _FakeInventoryService(snapshot)
    agent = EdgeWorkspaceAgent(
        "ws://127.0.0.1:9/api/edge/ws",
        token="btxd_test.secret",
        device_id="host_test01",
        workspace=_ws_entry(_WS_A, ws_root),
        workspaces_file=ws_file,
        swarm_home=swarm_home,
        daemon_salt="boot-salt",
        inventory_service=svc,
    )

    # seed durable state: store last_seq=3, outbox seq 4-5 for this ws + 1 other ws
    store = EdgeActiveOrdersStore(swarm_home / "active_orders.json")
    store.upsert(
        {
            "order_id": "ord-a",
            "workspace_id": _WS_A,
            "pid": 111,
            "pid_create_time": None,
            "daemon_gen_id": "g0",
            "fencing_token": "ft",
            "fencing_epoch": 1,
            "last_seq": 3,
        }
    )
    buf = EdgeOutboxBuffer(swarm_home / "edge_outbox.jsonl")
    buf.append(
        {
            "type": "work_order_event",
            "order_id": "ord-a",
            "workspace_id": _WS_A,
            "seq": 5,
            "kind": "progress",
            "ts": "t",
            "data": {},
        }
    )
    buf.append(
        {
            "type": "work_order_event",
            "order_id": "ord-a",
            "workspace_id": _WS_A,
            "seq": 4,
            "kind": "started",
            "ts": "t",
            "data": {},
        }
    )
    buf.append(
        {
            "type": "work_order_event",
            "order_id": "ord-b",
            "workspace_id": _WS_B,
            "seq": 1,
            "kind": "started",
            "ts": "t",
            "data": {},
        }
    )
    agent._seed_order_seqs()
    assert agent._order_seqs["ord-a"] == 5

    async def _run() -> None:
        hello = await agent.build_hello_frame()
        assert hello["workspaces_manifest"]
        assert all("root" not in m for m in hello["workspaces_manifest"])
        assert {m["workspace_id"] for m in hello["workspaces_manifest"]} == {
            _WS_A,
            _WS_B,
        }
        assert hello["harnesses"] == snapshot

        hb = await agent.build_heartbeat_frame()
        assert hb["harnesses"] == snapshot
        assert "workspaces_manifest" in hb

        import hashlib

        expect = (
            int.from_bytes(hashlib.sha256(f"{_WS_A}:boot-salt".encode()).digest()[:4], "big") % 10
        )
        assert agent._extra_reconnect_delay_s() == float(expect)

        ws = _DummyWebSocket()
        await agent._post_hello_ack(ws)
        seqs = [f.get("seq") for f in ws.sent_frames]
        kinds = [f.get("kind") for f in ws.sent_frames]
        assert seqs == [4, 5]
        assert kinds == ["started", "progress"]
        remaining = buf.pending()
        assert len(remaining) == 1
        assert remaining[0]["workspace_id"] == _WS_B

    asyncio.run(_run())


def test_edge_host_daemon_supervises_only_enrolled_and_drains(tmp_path: Path) -> None:
    """Daemon spawns <=8 enrolled agents, never candidate/disabled, and drains."""
    ws_file = tmp_path / "host_workspaces.json"
    swarm_home = tmp_path / "swarm"
    entries = []
    for i in range(MAX_ENROLLED_WORKSPACES + 2):
        root = tmp_path / f"ws_{i}"
        root.mkdir()
        entries.append(_ws_entry(f"ws_{i:012x}", root))
    cand = tmp_path / "cand"
    cand.mkdir()
    disabled = tmp_path / "dis"
    disabled.mkdir()
    entries.append(_ws_entry(_WS_B, cand, state="candidate"))
    entries.append(_ws_entry("ws_ffff0000eeee", disabled, state="disabled"))
    _write_registry(ws_file, entries)

    svc = _FakeInventoryService([])
    daemon = EdgeHostDaemon(
        "ws://127.0.0.1:9/api/edge/ws",
        workspaces_file=ws_file,
        swarm_home=swarm_home,
        state_file=swarm_home / "edge_daemon.json",
        lock_path=swarm_home / "edge_daemon.lock",
        inventory_service=svc,
        resolve_token_fn=lambda *a, **k: ("tok", "test"),
    )

    async def _run() -> None:
        res = await daemon.start()
        try:
            assert res["ok"] is True
            assert res["mode"] == "spawned"
            assert len(daemon.agents) == MAX_ENROLLED_WORKSPACES
            assert _WS_B not in daemon._ws_agent_index
            assert "ws_ffff0000eeee" not in daemon._ws_agent_index
            assert svc.started is True
            assert daemon._lock is not None and daemon._lock.locked is True
            assert all(a.daemon_gen_id == daemon.daemon_gen_id for a in daemon.agents)
        finally:
            await daemon.stop(drain_timeout_s=1.0)
        assert daemon._draining is True
        assert svc.stopped is False  # injected service is not owned by the daemon
        assert daemon._lock is None

    asyncio.run(_run())


async def _loopback_request(
    port: int,
    method: str,
    path: str,
    *,
    token: str = "",
    host: str = "",
    body: Optional[Dict[str, Any]] = None,
) -> bytes:
    """Fire one raw HTTP request at a loopback IPC endpoint; returns raw bytes."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        payload = json.dumps(body).encode("utf-8") if body is not None else b""
        lines = [f"{method} {path} HTTP/1.1"]
        if host:
            lines.append(f"Host: {host}")
        if token:
            lines.append(f"X-Loopback-Token: {token}")
        if payload:
            lines.append(f"Content-Length: {len(payload)}")
        lines.append("\r\n")
        writer.write("\r\n".join(lines).encode("ascii") + payload)
        await writer.drain()
        return await asyncio.wait_for(reader.read(65536), timeout=3.0)
    finally:
        writer.close()
        await writer.wait_closed()


def test_edge_host_daemon_loopback_ipc_host_token_and_routes(tmp_path: Path) -> None:
    """F3/F7: Host pinning, constant-time token on every route, /workspaces root
    stripping, /harnesses limits, and the $0/branch-only /orders ceiling."""
    ws_root = tmp_path / "ws_a"
    ws_root.mkdir()
    (ws_root / ".git").mkdir()
    admin_root = tmp_path / "ws_admin"
    admin_root.mkdir()
    ws_file = tmp_path / "host_workspaces.json"
    swarm_home = tmp_path / "swarm"
    _write_registry(
        ws_file,
        [
            _ws_entry(_WS_A, ws_root),
            _ws_entry("ws_adbeff00beef", admin_root, local_admin=True),
        ],
    )
    svc = _FakeInventoryService(
        [{"harness_id": "zcode", "limits": {"unit": "requests", "confidence": "exact"}}]
    )
    daemon = EdgeHostDaemon(
        "ws://127.0.0.1:9/api/edge/ws",
        workspaces_file=ws_file,
        swarm_home=swarm_home,
        state_file=swarm_home / "edge_daemon.json",
        lock_path=swarm_home / "edge_daemon.lock",
        loopback_token="lb-tok-123",
        inventory_service=svc,
        resolve_token_fn=lambda *a, **k: ("tok", "test"),
    )

    async def _run() -> None:
        res = await daemon.start()
        assert res["ok"] is True
        port = int(res["loopback_port"])
        good_host = f"127.0.0.1:{port}"
        try:
            # 1. missing Host -> 403
            assert b"403 Forbidden" in await _loopback_request(
                port, "GET", "/workspaces", token="lb-tok-123"
            )
            # 2. foreign Host -> 403 (DNS rebinding)
            assert b"403 Forbidden" in await _loopback_request(
                port, "GET", "/workspaces", token="lb-tok-123", host="evil.example.com:80"
            )
            # 3. valid Host, missing token -> 401
            assert b"401 Unauthorized" in await _loopback_request(
                port, "GET", "/workspaces", host=good_host
            )
            # 4. valid Host+token -> 200, roots stripped
            raw = await _loopback_request(
                port, "GET", "/workspaces", token="lb-tok-123", host=good_host
            )
            assert b"200 OK" in raw
            body = json.loads(raw.split(b"\r\n\r\n", 1)[1])
            assert body["ok"] is True
            assert len(body["workspaces"]) == 2
            assert all("root" not in ws for ws in body["workspaces"])
            # 5. include_paths=1 -> roots returned
            raw2 = await _loopback_request(
                port,
                "GET",
                "/workspaces?include_paths=1",
                token="lb-tok-123",
                host=good_host,
            )
            body2 = json.loads(raw2.split(b"\r\n\r\n", 1)[1])
            assert any("root" in ws for ws in body2["workspaces"])
            # 6. /harnesses returns cached limits
            raw3 = await _loopback_request(
                port, "GET", "/harnesses", token="lb-tok-123", host=good_host
            )
            body3 = json.loads(raw3.split(b"\r\n\r\n", 1)[1])
            assert body3["harnesses"][0]["limits"]["confidence"] == "exact"
            # 7. /orders mode=edit on non-admin -> 403 ceiling
            raw4 = await _loopback_request(
                port,
                "POST",
                "/orders",
                token="lb-tok-123",
                host=good_host,
                body={"workspace_id": _WS_A, "order_id": "ord-x", "mode": "edit"},
            )
            assert b"403 Forbidden" in raw4
            assert b"mode_exceeds_ceiling" in raw4
            # 8. /orders explicit provider on non-admin -> 403 paid ceiling
            raw5 = await _loopback_request(
                port,
                "POST",
                "/orders",
                token="lb-tok-123",
                host=good_host,
                body={
                    "workspace_id": _WS_A,
                    "order_id": "ord-x",
                    "mode": "plan",
                    "provider": "openrouter",
                },
            )
            assert b"403 Forbidden" in raw5
            assert b"paid_ceiling_exceeded" in raw5
            # 9. /orders mode=plan non-admin passes ceiling -> offline Cloud fails closed (503)
            raw6_off = await _loopback_request(
                port,
                "POST",
                "/orders",
                token="lb-tok-123",
                host=good_host,
                body={
                    "workspace_id": _WS_A,
                    "order_id": "ord-y",
                    "mode": "plan",
                    "order": {"order_id": "ord-y", "mode_request": "plan"},
                },
            )
            assert b"503" in raw6_off.split(b"\r\n", 1)[0]
            assert b"registration_unavailable" in raw6_off

            # 9b. With Cloud registration acked, mode=plan passes ceiling -> dispatch nacks
            async def _ack_reg(payload, **_kw):
                return {"ok": True, "order_id": payload.get("order_id"), "status": "QUEUED"}

            for a in daemon.agents:
                a.register_work_order_with_cloud = _ack_reg
            raw6 = await _loopback_request(
                port,
                "POST",
                "/orders",
                token="lb-tok-123",
                host=good_host,
                body={
                    "workspace_id": _WS_A,
                    "order_id": "ord-y",
                    "mode": "plan",
                    "order": {"order_id": "ord-y", "mode_request": "plan"},
                },
            )
            assert b"409" in raw6.split(b"\r\n", 1)[0]
            assert b"harness_spawn_disabled" in raw6
            # 10. admin workspace still hits dispatch path (ceiling lifted)
            raw7 = await _loopback_request(
                port,
                "POST",
                "/orders",
                token="lb-tok-123",
                host=good_host,
                body={
                    "workspace_id": "ws_adbeff00beef",
                    "order_id": "ord-z",
                    "mode": "edit",
                    "provider": "openrouter",
                },
            )
            assert b"403 Forbidden" not in raw7.split(b"\r\n", 1)[0]
        finally:
            await daemon.stop(drain_timeout_s=1.0)

    asyncio.run(_run())


def test_loopback_control_rejects_dispatch_and_orders_enforces_ceilings_and_idem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timezone

    from botex.edge.credentials import save_edge_token
    from botex.edge.runner import EdgeHostDaemon
    from botex.edge.workspaces import save_workspaces_registry

    ws_root = tmp_path / "ws_ceil"
    ws_root.mkdir()
    ws_file = tmp_path / "workspaces.json"
    save_workspaces_registry(
        {
            "device_id": "dev-ceil",
            "workspaces": [
                {
                    "workspace_id": "ws_ceil",
                    "root": str(ws_root),
                    "state": "enrolled",
                    "local_admin": False,
                }
            ],
        },
        path=ws_file,
    )
    cred_dir = tmp_path / "creds"
    save_edge_token("dev-ceil", "ws_ceil", "btxd_1111222233334444.secret", base_dir=cred_dir)

    async def _run() -> None:
        daemon = EdgeHostDaemon(
            "ws://127.0.0.1:9/api/edge/ws",
            workspaces_file=ws_file,
            credentials_dir=cred_dir,
            swarm_home=tmp_path / "swarm_home",
            loopback_token="tok-ceil",
            single_instance=False,
        )
        await daemon.start()
        port = daemon._loopback_port_bound or 0
        host = f"127.0.0.1:{port}"
        try:

            async def _ack_reg(payload, **_kw):
                return {"ok": True, "order_id": payload.get("order_id"), "status": "QUEUED"}

            for a in daemon.agents:
                a.register_work_order_with_cloud = _ack_reg

            # 1. /control rejects dispatch_work_order bypass
            r_ctrl = await _loopback_request(
                port,
                "POST",
                "/control",
                token="tok-ceil",
                host=host,
                body={
                    "local_exec": True,
                    "frame": {
                        "type": "dispatch_work_order",
                        "order_id": "ord-bypass",
                        "fencing_token": "tok",
                    },
                },
            )
            assert b"400 Bad Request" in r_ctrl
            assert b"forbidden_control_frame_type" in r_ctrl

            # 2. botex:<paid> and handoff_policy.max_cost_usd blocked
            r_paid = await _loopback_request(
                port,
                "POST",
                "/orders",
                token="tok-ceil",
                host=host,
                body={
                    "workspace_id": "ws_ceil",
                    "order_id": "ord-p1",
                    "mode": "readonly",
                    "target_harness": "botex:openai",
                },
            )
            assert b"403 Forbidden" in r_paid
            assert b"paid_ceiling_exceeded" in r_paid

            # 3. Confirmed edit passes ceiling when confirmation is fresh
            now_iso = datetime.now(timezone.utc).isoformat()
            r_conf = await _loopback_request(
                port,
                "POST",
                "/orders",
                token="tok-ceil",
                host=host,
                body={
                    "workspace_id": "ws_ceil",
                    "order_id": "ord-conf-1",
                    "mode": "edit",
                    "confirmation": {
                        "confirmed_by": "kubai@win11",
                        "confirmed_at": now_iso,
                        "ceiling_ack": True,
                    },
                },
            )
            assert b"403 Forbidden" not in r_conf
            assert b"harness_spawn_disabled" in r_conf

            # 4. Idempotency-Key duplicate returns 200 duplicate=True; conflicting order_id -> 409
            for a in daemon.agents:
                a.handle_control_frame = lambda frame: asyncio.sleep(
                    0, result={"ok": True, "order_id": frame["order_id"], "pid": 123}
                )
            raw_body = json.dumps(
                {"workspace_id": "ws_ceil", "order_id": "ord-idem-1", "mode": "readonly"}
            ).encode("utf-8")

            async def _post_idem(payload_bytes: bytes) -> bytes:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                req = (
                    f"POST /orders HTTP/1.1\r\nHost: {host}\r\n"
                    f"X-Loopback-Token: tok-ceil\r\n"
                    f"Idempotency-Key: idem-k1\r\n"
                    f"Content-Length: {len(payload_bytes)}\r\nConnection: close\r\n\r\n"
                ).encode("ascii") + payload_bytes
                writer.write(req)
                await writer.drain()
                resp = await reader.read()
                writer.close()
                return resp

            first = await _post_idem(raw_body)
            assert b"200 OK" in first
            second = await _post_idem(raw_body)
            assert b"200 OK" in second
            assert b'"duplicate": true' in second
            conflict_body = json.dumps(
                {"workspace_id": "ws_ceil", "order_id": "ord-idem-other", "mode": "readonly"}
            ).encode("utf-8")
            conflict = await _post_idem(conflict_body)
            assert b"409" in conflict.split(b"\r\n", 1)[0]
            assert b"idem_key_conflict" in conflict
        finally:
            await daemon.stop(drain_timeout_s=1.0)

    asyncio.run(_run())
