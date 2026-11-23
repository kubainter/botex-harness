# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for botex/mcp_bridge — the multiplexing stdio<->SSE client bridge."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from botex.mcp_bridge.client import McpBridgeClient, _SseSlot


def _session_mock(tools=None):
    sess = MagicMock()
    sess.initialize = AsyncMock()
    sess.call_tool = AsyncMock(
        return_value=SimpleNamespace(content=[SimpleNamespace(text="ok")], isError=False)
    )
    sess.list_tools = AsyncMock(
        return_value=SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="check_health",
                    description="h",
                    input_schema={"type": "object"},
                )
            ]
            if tools is None
            else tools
        )
    )
    sess.list_resources = AsyncMock(return_value=SimpleNamespace(resources=[]))
    return sess


def _client(monkeypatch, pool_size=3, http_reads="0"):
    monkeypatch.setenv("BOTEX_MCP_HTTP_READS", http_reads)
    return McpBridgeClient(
        pool_size=pool_size,
        cloud_url="https://cloud.test",
        access_token="tok",
        agent_id="devin",
        host_id="host1",
    )


def test_sse_slot_headers_and_lazy_connect():
    """Each slot builds its SSE client with auth/provenance headers."""
    captured = {}

    class _FakeSse:
        def __init__(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers

        async def __aenter__(self):
            return ("rs", "ws")

        async def __aexit__(self, *a):
            return False

    sess = _session_mock()

    async def _run():
        with (
            patch("botex.mcp_bridge.client.sse_client", _FakeSse),
            patch("botex.mcp_bridge.client.ClientSession") as CS,
        ):
            CS.return_value.__aenter__ = AsyncMock(return_value=sess)
            CS.return_value.__aexit__ = AsyncMock(return_value=False)
            slot = _SseSlot(0, "https://cloud.test/mcp/sse", {"Authorization": "Bearer tok"})
            got = await slot.acquire()
            assert got is sess
            await slot.stop()

    asyncio.run(_run())
    assert captured["url"] == "https://cloud.test/mcp/sse"
    assert captured["headers"]["Authorization"] == "Bearer tok"


def test_tools_list_cached_and_headers_forwarded(monkeypatch):
    """tools/list answers from priming cache; headers carry Bearer + provenance."""
    c = _client(monkeypatch)
    slot_session = _session_mock()
    slot = MagicMock(spec=_SseSlot)
    slot.in_flight = 0
    slot.acquire = AsyncMock(return_value=slot_session)
    slot._teardown = AsyncMock()
    c._slots = [slot]
    c._tools_cache = [{"name": "cached_tool"}]
    c._tools_cached_at = time.time()

    tools = asyncio.run(c.list_tools())
    assert tools == [{"name": "cached_tool"}]
    assert c.headers["Authorization"] == "Bearer tok"
    assert c.headers["X-Agent-Id"] == "devin"
    assert c.headers["X-Host"] == "host1"


def test_tools_list_changed_invalidates_cache(monkeypatch):
    c = _client(monkeypatch)
    c._tools_cache = [{"name": "old"}]
    c._tools_cached_at = time.time()
    note = MagicMock()
    note.model_dump_json = lambda: '{"root": {"method": "notifications/tools/list_changed"}}'
    c._on_server_notification(note)
    assert c._tools_cache is None


def test_concurrent_calls_use_least_loaded_slot(monkeypatch):
    """Two in-flight calls must land on DIFFERENT slots (the core regression)."""
    c = _client(monkeypatch, pool_size=2)
    used = []
    blockers = []

    for i in range(2):
        sess = _session_mock()
        slot = MagicMock(spec=_SseSlot)
        slot.in_flight = 0
        slot.idx = i
        slot.acquire = AsyncMock(return_value=sess)
        slot._teardown = AsyncMock()

        async def _call(name, args, _sess=sess, _slot=slot):
            used.append(_slot.idx)
            ev = asyncio.Event()
            blockers.append(ev)
            await asyncio.wait_for(ev.wait(), timeout=5)
            return SimpleNamespace(content=[SimpleNamespace(text="done")], isError=False)

        sess.call_tool = _call
        c._slots.append(slot)

    async def _run():
        monkeypatch.setenv("BOTEX_MCP_HTTP_READS", "0")
        t1 = asyncio.create_task(c.call_tool("run_subagent", {"a": 1}))
        t2 = asyncio.create_task(c.call_tool("get_stats", {"b": 2}))
        await asyncio.sleep(0.2)
        # Both calls must already be dispatched on their own sessions.
        assert len(used) == 2
        for ev in blockers:
            ev.set()
        await asyncio.gather(t1, t2)

    asyncio.run(_run())
    assert used == [0, 1], f"calls serialized on one slot: {used}"


def test_http_read_gating(monkeypatch):
    """Read-class tools use /mcp/http only when BOTEX_MCP_HTTP_READS=1."""
    c = _client(monkeypatch, http_reads="1")
    called = {"http": 0, "sse": 0}
    c._call_http = AsyncMock(
        side_effect=lambda n, a: (
            called.__setitem__("http", called["http"] + 1)
            or SimpleNamespace(content=[SimpleNamespace(text="h")], isError=False)
        )
    )
    slot = MagicMock(spec=_SseSlot)
    slot.in_flight = 0
    slot.acquire = AsyncMock(return_value=_session_mock())
    slot._teardown = AsyncMock()
    c._slots = [slot]

    text, err = asyncio.run(c.call_tool("check_health", {}))
    assert called["http"] == 1
    c2 = _client(monkeypatch, http_reads="0")
    slot2 = MagicMock(spec=_SseSlot)
    slot2.in_flight = 0
    slot2.acquire = AsyncMock(return_value=_session_mock())
    slot2._teardown = AsyncMock()
    c2._slots = [slot2]
    text2, _ = asyncio.run(c2.call_tool("check_health", {}))
    assert "ok" in text2


def test_missing_token_fails_fast(capsys):
    """__main__.main exits non-zero with clear stderr when env is missing."""
    import importlib
    import os
    import sys

    mod = importlib.import_module("botex.mcp_bridge.__main__")
    old_env = os.environ.copy()
    try:
        os.environ.pop("BOTEX_CLOUD_URL", None)
        os.environ.pop("BOTEX_ACCESS_TOKEN", None)
        with pytest.raises(SystemExit) as exc:
            mod.main([])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "BOTEX_CLOUD_URL" in err
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_dispatch_writes_single_framed_line(monkeypatch):
    """_dispatch must emit exactly one complete JSON line per request."""
    import json as _json

    from botex.mcp_bridge import __main__ as m

    written = []
    monkeypatch.setattr(m, "_write", lambda msg: written.append(msg))
    bridge = MagicMock()

    async def _call(name, args):
        await asyncio.sleep(0)
        return "pong", False

    bridge.call_tool = _call
    asyncio.run(
        m._dispatch(
            bridge,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "ping_tool", "arguments": {}},
            },
        )
    )
    assert len(written) == 1
    resp = written[0]
    assert resp["id"] == 7 and resp["result"]["content"][0]["text"] == "pong"
    _json.dumps(resp)  # serializable -> one framed NDJSON line downstream


def test_eof_drains_inflight_dispatch(monkeypatch):
    """Real _run(): a request followed by stdin EOF must still get its
    response — the drain grace lets the in-flight call finish."""
    import io
    import json as _json
    import sys as _sys

    from botex.mcp_bridge import __main__ as m

    written = []
    monkeypatch.setattr(m, "_write", lambda msg: written.append(msg))
    req = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "slow_tool", "arguments": {}},
    }
    monkeypatch.setattr(_sys, "stdin", io.StringIO(_json.dumps(req) + "\n"))
    monkeypatch.setenv("BOTEX_MCP_DRAIN_S", "5")

    import importlib

    importlib.reload(m)  # pick up DRAIN_TIMEOUT_S env
    monkeypatch.setattr(m, "_write", lambda msg: written.append(msg))
    try:
        bridge = MagicMock()
        bridge.on_client_notification = None
        bridge.stop = AsyncMock()

        async def _slow_call(name, args):
            await asyncio.sleep(0.15)
            return "drained", False

        bridge.call_tool = _slow_call
        asyncio.run(m._run(bridge))
        assert written and written[0]["result"]["content"][0]["text"] == "drained"
    finally:
        monkeypatch.delenv("BOTEX_MCP_DRAIN_S", raising=False)
        importlib.reload(m)


def test_env_file_loading_via_botex_mcp_env(tmp_path, monkeypatch):
    """BOTEX_MCP_ENV points at a local .env — secrets stay off the registry."""
    from botex.mcp_bridge import __main__ as m

    env_file = tmp_path / "bridge.env"
    env_file.write_text("BOTEX_ACCESS_TOKEN=tok-123\nBOTEX_CLOUD_URL=https://c.test\n")
    monkeypatch.setenv("BOTEX_MCP_ENV", str(env_file))
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BOTEX_CLOUD_URL", raising=False)
    monkeypatch.delenv("BOTEX_DISABLE_DOTENV", raising=False)
    import os

    m._load_env_file()
    assert os.environ["BOTEX_ACCESS_TOKEN"] == "tok-123"
    assert os.environ["BOTEX_CLOUD_URL"] == "https://c.test"


def test_env_file_respects_disable_flag(tmp_path, monkeypatch):
    from botex.mcp_bridge import __main__ as m

    env_file = tmp_path / "bridge.env"
    env_file.write_text("BOTEX_ACCESS_TOKEN=tok-123\n")
    monkeypatch.setenv("BOTEX_MCP_ENV", str(env_file))
    monkeypatch.setenv("BOTEX_DISABLE_DOTENV", "1")
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    import os

    m._load_env_file()
    assert "BOTEX_ACCESS_TOKEN" not in os.environ


def test_notifications_emit_no_stdout_response(monkeypatch):
    """JSON-RPC notifications (no "id") must never get a response line."""
    from botex.mcp_bridge import __main__ as m

    written = []
    monkeypatch.setattr(m, "_write", lambda msg: written.append(msg))
    bridge = MagicMock()
    for req in (
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 1},
        },
        {"jsonrpc": "2.0", "method": "bogus/notification"},
    ):
        asyncio.run(m._dispatch(bridge, req))
    assert written == [], f"notification leaked a response: {written}"


def test_list_changed_relay_dedupes_across_pool(monkeypatch):
    """N SSE slots each deliver tools/list_changed — client sees ONE relay."""
    c = _client(monkeypatch)
    relayed = []
    c.on_client_notification = relayed.append
    c._tools_cache = [{"name": "old"}]
    note = MagicMock()
    note.model_dump_json = lambda: '{"root": {"method": "notifications/tools/list_changed"}}'
    for _ in range(3):  # three slots deliver the same broadcast
        c._on_server_notification(note)
    assert c._tools_cache is None
    assert len(relayed) == 1, f"broadcast relayed per-slot: {relayed}"
    assert relayed[0]["method"] == "notifications/tools/list_changed"
    # A distinct notification still passes through untouched.
    note2 = MagicMock()
    note2.model_dump_json = lambda: '{"method": "notifications/progress", "params": {}}'
    c._on_server_notification(note2)
    assert len(relayed) == 2


def test_mutating_call_no_retry_after_send(monkeypatch):
    """A TimeoutError on a mutating call must NOT be retried (no double writes)."""
    c = _client(monkeypatch)
    calls = []

    async def _timeout(name, args):
        calls.append(name)
        raise TimeoutError("slow")

    sess = _session_mock()
    sess.call_tool = _timeout
    for _ in range(2):
        slot = MagicMock(spec=_SseSlot)
        slot.in_flight = 0
        slot.acquire = AsyncMock(return_value=sess)
        slot._teardown = AsyncMock()
        c._slots.append(slot)
    c._call_timeout = 0.05

    text, is_err = asyncio.run(c.call_tool("start_task", {"task": "x"}))
    assert is_err is True
    assert len(calls) == 1, f"mutating call retried: {calls}"


def test_run_startup_answers_initialize_before_bridge_start_finishes(monkeypatch):
    """Slow Cloud SSE handshake must not block stdin `initialize` response."""
    import io
    import json as _json
    import sys as _sys

    from botex.mcp_bridge import __main__ as m

    written = []
    monkeypatch.setattr(m, "_write", lambda msg: written.append(msg))
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    }
    monkeypatch.setattr(_sys, "stdin", io.StringIO(_json.dumps(init_req) + "\n"))

    started = asyncio.Event()
    bridge = MagicMock()
    bridge.on_client_notification = None
    bridge.stop = AsyncMock()

    async def _slow_start():
        started.set()
        await asyncio.sleep(10)

    bridge.start = _slow_start

    t0 = time.monotonic()
    asyncio.run(m._run_startup(bridge))
    elapsed = time.monotonic() - t0

    assert started.is_set()
    assert elapsed < 2.0, f"initialize blocked on bridge.start(): {elapsed:.2f}s"
    assert len(written) == 1
    assert written[0]["id"] == 1
    assert written[0]["result"]["serverInfo"]["name"] == "botex-mcp-bridge"


def test_pick_slot_initializes_slots_lazily(monkeypatch):
    """_pick_slot must work even if called before start() has populated _slots."""
    c = _client(monkeypatch, pool_size=2)
    assert c._slots == []
    slot = c._pick_slot()
    assert slot.idx == 0
    assert len(c._slots) == 2
