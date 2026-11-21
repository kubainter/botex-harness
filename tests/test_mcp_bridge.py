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
        c._slots = c._slots + [slot] if False else [*c._slots, slot]
    c._slots = c._slots[-2:]

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
