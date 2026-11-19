# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for Plan 038 Step 3: Two-tier QoS Control Lane, Lease TTL Failover, and --edge-daemon Loopback IPC."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

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
    QOS_PRIORITY_CONTROL,
    QOS_PRIORITY_DATA,
    EdgeRunner,
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
    monkeypatch.setenv("BOTEX_SWARM_DB_PATH", str(db_file))
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
    assert ok_res["status"] == "checkpoint_pushed"
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
    monkeypatch.setenv("BOTEX_SWARM_DB_PATH", str(db_file))
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
