# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""E2E tests for tethered-bot visibility."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import botex  # noqa: F401  # legacy alias graft must be installed first
import server as botex_server
import botex.server.server_cloud as server_cloud
from botex.core import swarm_store
from botex.core.device_tokens import DeviceTokenStore
from botex.edge.runner import write_local_bot_brief
from botex.server import config as server_config
from botex.server.server_cloud import _EDGE_SESSIONS, _GATEWAY_RATE_LIMITER, app


@pytest.fixture(autouse=True)
def _isolated_swarm_env(monkeypatch, tmp_path: Path):
    """Pin Cloud Soul + edge fast-cache into tmp_path; reset ETag dedup.

    Zero host files are touched: BOTEX_SWARM_STORE_DB and
    BOTEX_SWARM_BRIEFS_DIR redirect every store/cache access into the
    pytest tmp tree, and swarm DB connections are closed on teardown.
    """
    db = tmp_path / "swarm_store.db"
    briefs_dir = tmp_path / "bot_briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BOTEX_SWARM_STORE_DB", str(db))
    monkeypatch.setenv("BOTEX_SWARM_BRIEFS_DIR", str(briefs_dir))
    server_config.load_config(reload=True)
    botex_server._reset_seen_brief_etags()
    yield {"db": db, "briefs_dir": briefs_dir}
    botex_server._reset_seen_brief_etags()
    monkeypatch.delenv("BOTEX_SWARM_STORE_DB", raising=False)
    monkeypatch.delenv("BOTEX_SWARM_BRIEFS_DIR", raising=False)
    swarm_store.close_swarm_store_dbs()
    server_config.load_config(reload=True)


@pytest.fixture()
def _cloud_state(monkeypatch, tmp_path: Path):
    """Edge-gateway isolation mirroring tests/test_edge_ws_gateway.py."""
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


def _register_and_brief(slug: str, namespace: str, content: str) -> dict:
    res = swarm_store.register_bot(slug, namespace, role_desc="sentinel bot")
    assert res["ok"] is True
    brief = swarm_store.upsert_bot_brief(slug, content)
    assert brief["ok"] is True
    return brief


def test_edge_ws_hello_ack_triggers_brief_sync_preload(_isolated_swarm_env, _cloud_state):
    """brief_sync rides the wire immediately after hello_ack; the edge then
    persists each payload as <slug>.md via an atomic tmp+rename write."""
    _, store = _cloud_state
    brief = _register_and_brief("guardian-1", "swarm", "Check fences before writing.")
    raw_token, _ = store.issue_token("edge-a", "ws1", scopes=["task_pull:ws1", "result_push:ws1"])

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/edge/ws", headers={"Authorization": f"Bearer {raw_token}"}
        ) as ws:
            ws.send_json(
                {
                    "type": "hello",
                    "device_id": "edge-a",
                    "workspace_id": "ws1",
                    "capabilities": {"allow_exec": False, "max_output_bytes": 8192},
                    "protocol_version": "1.0",
                    "client_version": "2.1.0",
                }
            )
            ack = ws.receive_json()
            assert ack["type"] == "hello_ack"
            assert ack["device_id"] == "edge-a"
            frame = ws.receive_json()
            assert frame["type"] == "brief_sync"
            bots = frame["bots"]
            assert [b["slug"] for b in bots] == ["guardian-1"]
            payload = bots[0]
            assert payload["etag"] == brief["etag"]
            assert payload["namespace"] == "swarm"

    briefs_dir = _isolated_swarm_env["briefs_dir"]
    path = write_local_bot_brief(
        payload["slug"],
        namespace=payload["namespace"],
        etag=payload["etag"],
        fencing_epoch=payload["fencing_epoch"],
        content=payload["content"],
        updated_at=payload["updated_at"],
        briefs_dir=briefs_dir,
    )
    assert path == briefs_dir / "guardian-1.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert f"etag: {brief['etag']}" in text
    assert "Check fences" in text
    # Atomic-write invariant: no orphan tmp artifacts survive os.replace.
    assert not list(briefs_dir.glob("*.tmp"))


def test_bot_report_activity_rejects_stale_fencing_token(_isolated_swarm_env):
    res = swarm_store.register_bot("bot-f", "ns", role_desc="worker")
    assert res["ok"] is True

    r1 = asyncio.run(
        botex_server.bot_report_activity("bot-f", "scope-a", "started work", fencing_token=2)
    )
    assert r1["ok"] is True
    assert r1["status"] == "active"
    assert r1["fencing_token"] == 2

    r2 = asyncio.run(
        botex_server.bot_report_activity("bot-f", "scope-a", "stale write attempt", fencing_token=1)
    )
    assert r2["ok"] is False
    assert r2["status"] == "stale_epoch"
    assert r2["current_fencing_token"] == 2

    leases = swarm_store.get_active_leases(bot_slug="bot-f")
    assert len(leases) == 1
    assert leases[0]["fencing_token"] == 2
    assert leases[0]["summary"] == "started work"


def test_recall_memory_piggybacks_guardians_and_dedups_by_etag(
    _isolated_swarm_env, monkeypatch, tmp_path: Path
):
    brief = _register_and_brief("bot-r", "swarm", "brief v1")
    etag1 = brief["etag"]
    monkeypatch.setattr(
        botex_server,
        "_gather_recall_context",
        lambda **kwargs: {"ok": True, "nodes": []},
    )
    monkeypatch.setattr(botex_server, "resolve_workspace", lambda value="": str(tmp_path))

    p1 = asyncio.run(botex_server._recall_memory_impl(workspace_dir=str(tmp_path), query="q"))
    assert p1["ok"] is True
    assert [g["slug"] for g in p1["swarm_guardians"]] == ["bot-r"]
    assert p1["active_brief"]["slug"] == "bot-r"
    assert p1["active_brief"]["etag"] == etag1
    assert p1["active_brief"]["content"] == "brief v1"

    saved = asyncio.run(
        botex_server.save_memory(
            title="note one", content="body", workspace_dir=str(tmp_path), scope="local"
        )
    )
    assert saved["ok"] is True
    m1 = asyncio.run(
        botex_server.read_memory(memory_id=saved["id"], workspace_dir=str(tmp_path), scope="local")
    )
    assert m1["ok"] is True
    # Guardians stay visible even though the unchanged brief is deduplicated.
    assert [g["slug"] for g in m1["swarm_guardians"]] == ["bot-r"]
    assert "active_brief" not in m1

    brief2 = swarm_store.upsert_bot_brief("bot-r", "brief v2")
    assert brief2["ok"] is True and brief2["etag"] != etag1
    m2 = asyncio.run(
        botex_server.read_memory(memory_id=saved["id"], workspace_dir=str(tmp_path), scope="local")
    )
    assert m2["ok"] is True
    assert m2["active_brief"]["etag"] == brief2["etag"]
    assert m2["active_brief"]["content"] == "brief v2"

    # Same etag already emitted in this session -> omitted on the next call.
    p2 = asyncio.run(botex_server._recall_memory_impl(workspace_dir=str(tmp_path), query="q"))
    assert p2["ok"] is True
    assert [g["slug"] for g in p2["swarm_guardians"]] == ["bot-r"]
    assert "active_brief" not in p2


def test_local_visibility_offline_without_cloud(_isolated_swarm_env, monkeypatch, tmp_path: Path):
    """Empty/absent Cloud Soul store: tools fall back to the local fast-cache
    (read_local_bot_briefs) with zero HTTP involved."""
    briefs_dir = _isolated_swarm_env["briefs_dir"]
    written = write_local_bot_brief(
        "ghost-1",
        namespace="edge",
        role_desc="Edge specialist",
        etag="etag-ghost",
        fencing_epoch=3,
        content="cached ghost brief",
        briefs_dir=briefs_dir,
    )
    assert written is not None

    res = asyncio.run(botex_server.swarm_list_bots())
    assert res["ok"] is True
    assert res["count"] == 1
    bot = res["bots"][0]
    assert bot["slug"] == "ghost-1"
    assert bot["source"] == "local_cache"
    assert bot["role_desc"] == "Edge specialist"
    assert bot["etag"] == "etag-ghost"
    assert bot["fencing_epoch"] == 3
    assert bot["active_leases"] == []

    monkeypatch.setattr(
        botex_server,
        "_gather_recall_context",
        lambda **kwargs: {"ok": True, "nodes": []},
    )
    p = asyncio.run(
        botex_server._recall_memory_impl(workspace_dir=str(tmp_path), query="q", project="edge")
    )
    assert p["ok"] is True
    assert [g["slug"] for g in p["swarm_guardians"]] == ["ghost-1"]
    assert p["active_brief"]["slug"] == "ghost-1"
    assert p["active_brief"]["etag"] == "etag-ghost"
    assert p["active_brief"]["content"] == "cached ghost brief"


def test_bot_consult_filters_active_notes_only(_isolated_swarm_env, monkeypatch, tmp_path: Path):
    brief = _register_and_brief("sage-1", "swarm", "sage brief content")
    nodes = [
        {"id": "n1", "status": "candidate", "content": "draft", "score": 10},
        {"id": "n2", "status": "active", "content": "live note", "score": 9},
        {"id": "n3", "status": "conflict_pending", "content": "conflict", "score": 8},
        {"id": "n4", "status": "deprecated", "content": "old", "score": 7},
        {"id": "n5", "status": "superseded", "content": "replaced", "score": 6},
        {"id": "n6", "content": "missing status defaults to active", "score": 5},
    ]
    monkeypatch.setattr(
        botex_server,
        "_gather_recall_context",
        lambda **kwargs: {"ok": True, "nodes": nodes},
    )
    monkeypatch.setattr(botex_server, "resolve_workspace", lambda value="": str(tmp_path))

    captured: dict = {}

    async def _fake_synth(query, node_list):
        captured["query"] = query
        captured["nodes"] = list(node_list)
        return {"answer": "synthesized", "sources": ["n2"], "confidence": "HIGH"}

    import botex.ops.curator as curator_pkg

    monkeypatch.setattr(curator_pkg, "synthesize_recall", _fake_synth)

    missing = asyncio.run(botex_server.bot_consult("no-such-bot", "q"))
    assert missing == {"ok": False, "error": "BOT_NOT_FOUND"}

    res = asyncio.run(botex_server.bot_consult("sage-1", "what is the plan?"))
    assert res["ok"] is True
    assert res["bot_id"] == "sage-1"
    assert res["namespace"] == "swarm"
    assert res["ttl_hops"] == 1
    assert res["answer"] == "synthesized"
    assert res["context_nodes"] == 2
    assert {n["id"] for n in captured["nodes"]} == {"n2", "n6"}
    assert captured["query"] == "what is the plan?"
    assert res["active_brief"]["etag"] == brief["etag"]
    assert res["active_brief"]["content"] == "sage brief content"


def test_cli_bots_sync_handles_304_and_200(_isolated_swarm_env, _cloud_state):
    """Injected TestClient exercises the real /api/bots + /brief endpoints:
    first sync writes (200), second sends If-None-Match and skips on 304."""
    _register_and_brief("syncbot", "swarm", "sync me")
    briefs_dir = _isolated_swarm_env["briefs_dir"]

    with TestClient(app) as client:
        r1 = botex_server.sync_bot_briefs(
            base_url="http://testserver", client=client, briefs_dir=briefs_dir
        )
        assert r1["ok"] is True
        assert r1["bots"] == 1
        assert r1["updated"] == 1
        assert r1["skipped_304"] == 0

        cached = briefs_dir / "syncbot.md"
        assert cached.is_file()
        assert "sync me" in cached.read_text(encoding="utf-8")
        first_mtime = cached.stat().st_mtime_ns

        r2 = botex_server.sync_bot_briefs(
            base_url="http://testserver", client=client, briefs_dir=briefs_dir
        )
        assert r2["ok"] is True
        assert r2["updated"] == 0
        assert r2["skipped_304"] == 1
        # 304 path must not rewrite the local file.
        assert cached.stat().st_mtime_ns == first_mtime

    # Offline / unreachable gateway: on_startup must degrade gracefully.
    offline = botex_server.sync_bot_briefs(
        base_url="http://127.0.0.1:1", on_startup=True, briefs_dir=briefs_dir
    )
    assert offline["ok"] is False
    assert offline["on_startup"] is True

    with pytest.raises(SystemExit) as exc:
        botex_server._cli_bots(["sync", "--on-startup"])
    assert exc.value.code == 0


def test_seen_brief_etags_bounded_lru_and_steward_dot_origin(monkeypatch, _isolated_swarm_env):
    from botex.tools.security import sanitize_origin_claim

    assert sanitize_origin_claim("steward.ops@host-a") == ""
    assert sanitize_origin_claim("steward:ops@host-a") == ""

    _register_and_brief("lrubot", "swarm", "lru brief")
    monkeypatch.setattr(botex_server, "_SEEN_BRIEF_ETAGS_MAX", 2)

    p1 = botex_server._attach_swarm_visibility_piggyback(
        {"ok": True}, namespace="swarm", session_key="sess-1"
    )
    p2 = botex_server._attach_swarm_visibility_piggyback(
        {"ok": True}, namespace="swarm", session_key="sess-2"
    )
    p3 = botex_server._attach_swarm_visibility_piggyback(
        {"ok": True}, namespace="swarm", session_key="sess-3"
    )
    assert "active_brief" in p1 and "active_brief" in p2 and "active_brief" in p3
    assert len(botex_server._SEEN_BRIEF_ETAGS) == 2

    # sess-3 is still cached (no active_brief); sess-1 was evicted (re-injected)
    p3_again = botex_server._attach_swarm_visibility_piggyback(
        {"ok": True}, namespace="swarm", session_key="sess-3"
    )
    p1_evicted = botex_server._attach_swarm_visibility_piggyback(
        {"ok": True}, namespace="swarm", session_key="sess-1"
    )
    assert "active_brief" not in p3_again
    assert "active_brief" in p1_evicted


def test_concurrent_upsert_brief_and_lease_monotonicity(_isolated_swarm_env):
    from concurrent.futures import ThreadPoolExecutor

    swarm_store.register_bot("conc-bot", "swarm", role_desc="concurrent check")

    def _bump_brief(i: int):
        return swarm_store.upsert_bot_brief("conc-bot", f"brief body rev {i}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_bump_brief, range(12)))
    assert all(r["ok"] for r in results)
    epochs = sorted(r["fencing_epoch"] for r in results)
    assert epochs == list(range(1, 13))

    swarm_store.upsert_host_lease("conc-bot", "host-hi", "repo-scope", "fence 10", fencing_token=10)

    def _race_lease(tok: int):
        return swarm_store.upsert_host_lease(
            "conc-bot", f"host-{tok}", "repo-scope", f"fence {tok}", fencing_token=tok
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        lease_res = list(pool.map(_race_lease, [1, 2, 3, 15, 4, 5]))
    ok_tokens = [r["fencing_token"] for r in lease_res if r["ok"]]
    assert all(tok >= 10 for tok in ok_tokens)
    active = swarm_store.get_active_leases(bot_slug="conc-bot")
    assert len(active) == 1
    assert active[0]["fencing_token"] == 15


def test_piggyback_empty_namespace_skips_foreign_domain_and_respects_ttl(
    monkeypatch, _isolated_swarm_env
):
    _register_and_brief("alpha-marketing", "marketing", "marketing brief")
    p_foreign = botex_server._attach_swarm_visibility_piggyback(
        {"ok": True}, namespace="", session_key="sess-ns"
    )
    assert "active_brief" not in p_foreign

    _register_and_brief("zeta-swarm", "swarm", "global swarm brief")
    p_global = botex_server._attach_swarm_visibility_piggyback(
        {"ok": True}, namespace="", session_key="sess-ns"
    )
    assert p_global["active_brief"]["slug"] == "zeta-swarm"

    # Immediate repeat deduped; expired TTL re-injects.
    p_dedup = botex_server._attach_swarm_visibility_piggyback(
        {"ok": True}, namespace="", session_key="sess-ns"
    )
    assert "active_brief" not in p_dedup
    monkeypatch.setattr(botex_server, "_SEEN_BRIEF_ETAGS_TTL_S", -1.0)
    p_expired = botex_server._attach_swarm_visibility_piggyback(
        {"ok": True}, namespace="", session_key="sess-ns"
    )
    assert p_expired["active_brief"]["slug"] == "zeta-swarm"


def test_sync_bot_briefs_prunes_disabled_bot_cache(_isolated_swarm_env, _cloud_state):
    _register_and_brief("pb2", "swarm", "active brief")
    briefs_dir = _isolated_swarm_env["briefs_dir"]
    with TestClient(app) as client:
        r1 = botex_server.sync_bot_briefs(
            base_url="http://testserver", client=client, briefs_dir=briefs_dir
        )
        assert r1["ok"] and (briefs_dir / "pb2.md").is_file()

        swarm_store.register_bot("pb2", "swarm", status="disabled")
        r2 = botex_server.sync_bot_briefs(
            base_url="http://testserver", client=client, briefs_dir=briefs_dir
        )
        assert r2["ok"] and r2["removed"] == 1
        assert not (briefs_dir / "pb2.md").exists()

        consult_disabled = asyncio.run(botex_server.bot_consult("pb2", "q"))
        assert consult_disabled["ok"] is False
        assert consult_disabled["error"] == "BOT_INACTIVE"

        lease_disabled = asyncio.run(
            botex_server.bot_report_activity("pb2", "/repo", "work", fencing_token=1)
        )
        assert lease_disabled["ok"] is False
        assert lease_disabled["status"] == "bot_inactive"
