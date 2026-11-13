# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for Faza 3 Safe PWA -> Device X delegation endpoints & approval/lease gates."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from botex.approval import default_approval_manager
from botex.core.device_tokens import DeviceTokenStore
from botex.server import server_cloud
from botex.server.server_cloud import (
    _EDGE_PING_INTERVAL_S,
    _EDGE_SESSIONS,
    _GATEWAY_RATE_LIMITER,
    _TASKS,
    app,
)


_SAMPLE_DIGEST = hashlib.sha256(b"approved-skill-v1").hexdigest()


@pytest.fixture(autouse=True)
def _isolated_delegation_cloud(monkeypatch, tmp_path: Path):
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


def _connect_ws_and_hello(
    client: TestClient,
    token: str,
    *,
    device_id: str = "win11-dev",
    workspace_id: str = "harness",
    delegation_caps: dict | None = None,
):
    ws_cm = client.websocket_connect("/api/edge/ws", headers={"Authorization": f"Bearer {token}"})
    ws = ws_cm.__enter__()
    caps = {"allow_exec": False, "allow_destructive": False, "max_output_bytes": 65536}
    if delegation_caps is not None:
        caps["delegation"] = delegation_caps
    ws.send_json(
        {
            "type": "hello",
            "device_id": device_id,
            "workspace_id": workspace_id,
            "capabilities": caps,
            "protocol_version": "1.0",
            "client_version": "2.1.0",
        }
    )
    ack = ws.receive_json()
    assert ack["type"] == "hello_ack"
    return ws_cm, ws


def _wait_task_done(client: TestClient, task_id: str, timeout_s: float = 5.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = client.get(f"/api/tasks/{task_id}").json()
        if data.get("status") in {"DONE", "FAILED", "CANCELLED"}:
            return data
        time.sleep(0.05)
    return client.get(f"/api/tasks/{task_id}").json()


def test_devices_endpoint_reports_health_and_delegate_capability(_isolated_delegation_cloud):
    _, store = _isolated_delegation_cloud
    tok_ro, _ = store.issue_token(
        "win11-ro",
        "ws-ro",
        scopes=["task_pull:ws-ro", "result_push:ws-ro"],
    )
    tok_del, _ = store.issue_token(
        "win11-del",
        "ws-del",
        scopes=["task_pull:ws-del", "result_push:ws-del", "delegate_local:ws-del"],
    )

    with TestClient(app) as client:
        cm1, _ = _connect_ws_and_hello(
            client,
            tok_ro,
            device_id="win11-ro",
            workspace_id="ws-ro",
            delegation_caps={
                "enabled": True,
                "skills": [
                    {"name": "workspace-summary", "sha256": _SAMPLE_DIGEST, "read_only": True}
                ],
            },
        )
        cm2, _ = _connect_ws_and_hello(
            client,
            tok_del,
            device_id="win11-del",
            workspace_id="ws-del",
            delegation_caps={
                "enabled": True,
                "skills": [
                    {"name": "workspace-summary", "sha256": _SAMPLE_DIGEST, "read_only": True}
                ],
            },
        )
        try:
            res = client.get("/api/edge/devices")
            assert res.status_code == 200
            by_dev = {d["device_id"]: d for d in res.json()["devices"]}
            assert by_dev["win11-ro"]["healthy"] is True
            assert by_dev["win11-ro"]["can_delegate"] is False
            assert by_dev["win11-del"]["healthy"] is True
            assert by_dev["win11-del"]["can_delegate"] is True
            # Never expose raw secret or digest store fields
            assert "digest_sha256" not in str(by_dev)
        finally:
            cm2.__exit__(None, None, None)
            cm1.__exit__(None, None, None)


def test_delegate_rejects_missing_scope_disabled_opt_in_and_digest_mismatch(
    _isolated_delegation_cloud,
):
    _, store = _isolated_delegation_cloud
    tok_no_scope, _ = store.issue_token(
        "dev-noscope", "ws1", scopes=["task_pull:ws1", "result_push:ws1"]
    )
    tok_scoped, _ = store.issue_token(
        "dev-scoped",
        "ws2",
        scopes=["task_pull:ws2", "result_push:ws2", "delegate_local:ws2"],
    )

    with TestClient(app) as client:
        # Offline device -> 404
        r_404 = client.post(
            "/api/edge/delegate",
            json={"device_id": "missing", "workspace_id": "ws", "skill": "workspace-summary"},
        )
        assert r_404.status_code == 404

        # Token missing delegate_local -> 403
        cm1, _ = _connect_ws_and_hello(
            client,
            tok_no_scope,
            device_id="dev-noscope",
            workspace_id="ws1",
            delegation_caps={
                "enabled": True,
                "skills": [
                    {"name": "workspace-summary", "sha256": _SAMPLE_DIGEST, "read_only": True}
                ],
            },
        )
        try:
            r_scope = client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "dev-noscope",
                    "workspace_id": "ws1",
                    "skill": "workspace-summary",
                },
            )
            assert r_scope.status_code == 403
            assert "delegate_local" in r_scope.json()["detail"]
        finally:
            cm1.__exit__(None, None, None)

        # Device opt-in disabled -> 403
        cm2, _ = _connect_ws_and_hello(
            client,
            tok_scoped,
            device_id="dev-scoped",
            workspace_id="ws2",
            delegation_caps={"enabled": False, "skills": []},
        )
        try:
            r_dis = client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "dev-scoped",
                    "workspace_id": "ws2",
                    "skill": "workspace-summary",
                },
            )
            assert r_dis.status_code == 403
        finally:
            cm2.__exit__(None, None, None)

        # Unallowlisted skill & SHA-256 mismatch & expired lease
        cm3, _ = _connect_ws_and_hello(
            client,
            tok_scoped,
            device_id="dev-scoped",
            workspace_id="ws2",
            delegation_caps={
                "enabled": True,
                "skills": [
                    {"name": "workspace-summary", "sha256": _SAMPLE_DIGEST, "read_only": True}
                ],
            },
        )
        try:
            r_unk = client.post(
                "/api/edge/delegate",
                json={"device_id": "dev-scoped", "workspace_id": "ws2", "skill": "arbitrary-skill"},
            )
            assert r_unk.status_code == 403

            r_sha = client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "dev-scoped",
                    "workspace_id": "ws2",
                    "skill": "workspace-summary",
                    "sha256": "0" * 64,
                },
            )
            assert r_sha.status_code == 409

            # Stale liveness lease -> 409
            sess = _EDGE_SESSIONS["dev-scoped:ws2"]
            sess.last_seen_at = time.time() - (2.0 * _EDGE_PING_INTERVAL_S + 5.0)
            r_stale = client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "dev-scoped",
                    "workspace_id": "ws2",
                    "skill": "workspace-summary",
                },
            )
            assert r_stale.status_code == 409
        finally:
            cm3.__exit__(None, None, None)


def test_delegate_rejects_raw_command_params_recursively(_isolated_delegation_cloud):
    with TestClient(app) as client:
        for bad_params in [
            {"command": "rm -rf /"},
            {"nested": [{"c\u212a": "x", "SHELL": "pwsh"}]},  # noqa: F841
            {
                "nested": {"\uff43\uff4f\uff4d\uff4d\uff41\uff4e\uff44": "whoami"}
            },  # NFKC fullwidth "command"
            {"deep": {"argv": ["bash", "-c", "id"]}},
        ]:
            res = client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "dev",
                    "workspace_id": "ws",
                    "skill": "workspace-summary",
                    "params": bad_params,
                },
            )
            assert res.status_code == 400
            assert "Forbidden delegation parameter key" in res.json()["detail"]


def test_delegate_readonly_roundtrip_and_approval_gate_on_mutation(_isolated_delegation_cloud):
    _, store = _isolated_delegation_cloud
    tok, _ = store.issue_token(
        "win11-main",
        "proj",
        scopes=["task_pull:proj", "result_push:proj", "delegate_local:proj"],
    )

    with TestClient(app) as client:
        cm, ws = _connect_ws_and_hello(
            client,
            tok,
            device_id="win11-main",
            workspace_id="proj",
            delegation_caps={
                "enabled": True,
                "skills": [
                    {"name": "workspace-summary", "sha256": _SAMPLE_DIGEST, "read_only": True},
                    {"name": "apply-refactor", "sha256": _SAMPLE_DIGEST, "read_only": False},
                ],
            },
        )
        try:

            def _edge_responder():
                for _ in range(2):
                    frame = ws.receive_json()
                    if frame.get("type") == "delegate_skill":
                        ws.send_json(
                            {
                                "type": "delegate_result",
                                "call_id": frame["call_id"],
                                "result": {
                                    "ok": True,
                                    "summary": f"Skill {frame['skill']} ran cleanly with token sk-secret-1234567890abcdef",
                                },
                            }
                        )

            t = threading.Thread(target=_edge_responder, daemon=True)
            t.start()

            # 1) Read-only skill runs without blocking for approval
            r_ro = client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "win11-main",
                    "workspace_id": "proj",
                    "skill": "workspace-summary",
                    "sha256": _SAMPLE_DIGEST,
                    "params": {"path": "docs"},
                },
            )
            assert r_ro.status_code == 202
            ro_data = r_ro.json()
            assert ro_data["approval_required"] is False
            done_ro = _wait_task_done(client, ro_data["task_id"])
            assert done_ro["status"] == "DONE"
            assert "sk-secret" not in (done_ro["result"] or "")

            # 2) Non-readonly skill queues operator ApprovalManager gate
            r_mut = client.post(
                "/api/edge/delegate",
                json={
                    "device_id": "win11-main",
                    "workspace_id": "proj",
                    "skill": "apply-refactor",
                    "sha256": _SAMPLE_DIGEST,
                    "params": {"target": "src"},
                    "task": "Safe refactor check",
                },
            )
            assert r_mut.status_code == 202
            mut_tid = r_mut.json()["task_id"]
            assert r_mut.json()["approval_required"] is True

            # Wait briefly for PendingAction registration
            pending = []
            for _ in range(40):
                pending = client.get("/api/approvals", params={"task_id": mut_tid}).json()[
                    "pending_actions"
                ]
                if pending:
                    break
                time.sleep(0.05)
            assert len(pending) == 1
            action_id = pending[0]["action_id"]

            dec = client.post(
                f"/api/approvals/{action_id}/decide",
                json={"approved": True, "reason": "Verified by operator"},
            )
            assert dec.status_code == 200

            done_mut = _wait_task_done(client, mut_tid)
            assert done_mut["status"] == "DONE"
            t.join(timeout=2.0)
        finally:
            cm.__exit__(None, None, None)


def test_delegate_aborts_immediately_on_wss_drop_during_approval(_isolated_delegation_cloud):
    _, store = _isolated_delegation_cloud
    tok, _ = store.issue_token(
        "win11-drop",
        "proj",
        scopes=["task_pull:proj", "result_push:proj", "delegate_local:proj"],
    )

    with TestClient(app) as client:
        cm, _ = _connect_ws_and_hello(
            client,
            tok,
            device_id="win11-drop",
            workspace_id="proj",
            delegation_caps={
                "enabled": True,
                "skills": [
                    {"name": "apply-refactor", "sha256": _SAMPLE_DIGEST, "read_only": False},
                ],
            },
        )
        r_mut = client.post(
            "/api/edge/delegate",
            json={
                "device_id": "win11-drop",
                "workspace_id": "proj",
                "skill": "apply-refactor",
                "sha256": _SAMPLE_DIGEST,
                "params": {},
            },
        )
        assert r_mut.status_code == 202
        tid = r_mut.json()["task_id"]

        for _ in range(40):
            if default_approval_manager.get_pending_actions(task_id=tid):
                break
            time.sleep(0.05)
        assert len(default_approval_manager.get_pending_actions(task_id=tid)) == 1

        # Dropping the WSS connection must immediately abort pending approvals and task
        cm.__exit__(None, None, None)
        final = _wait_task_done(client, tid)
        assert final["status"] == "CANCELLED"
        assert _TASKS[tid]["status"] == "CANCELLED"
        assert default_approval_manager.get_pending_actions(task_id=tid) == []
