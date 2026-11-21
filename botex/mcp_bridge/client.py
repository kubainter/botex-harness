# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Multiplexing MCP client bridge core (stdio JSON-RPC <-> Cloud SSE/HTTP).

A pool of N independent SSE ClientSessions lets concurrent tool calls
proceed in parallel — one long-running call (run_subagent, get_task_status
long-poll) never blocks a lightweight one. Read-class tools optionally use
the stateless /mcp/http endpoint so they skip the SSE pool entirely.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

DEFAULT_POOL_SIZE = 3
RECONNECT_INITIAL_BACKOFF_S = 1.0
RECONNECT_MAX_BACKOFF_S = 30.0
TOOLS_TTL_S = 300.0
LIST_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 600.0
CONNECT_RETRIES = 3

# Read-only tools may retry after the request was sent; mutating calls retry
# ONCE and only on a transport-level error (at-least-once semantics).
IDEMPOTENT_TOOLS = {
    "check_health",
    "get_stats",
    "get_task_status",
    "recommend_models",
    "search_memory",
    "recall_memory",
    "reflect_memory",
    "read_memory",
    "read_scratchpad",
    "get_backlinks",
    "find_graph_health",
    "get_outline",
    "fetch_url",
    "list_tasks",
}

# Read-class tools eligible for the stateless /mcp/http fast path.
HTTP_READ_TOOLS = {"check_health", "recall_memory", "get_task_status", "list_tasks"}


def _env_num(name: str, default: float, cast=float) -> float:
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


def _is_transport_error(e: BaseException) -> bool:
    names = {c.__name__ for c in type(e).__mro__}
    return bool(
        names
        & {
            "ConnectError",
            "ClosedResourceError",
            "BrokenResourceError",
            "ConnectionError",
            "ReadError",
            "RemoteProtocolError",
            "EndOfStream",
            "ConnectTimeout",
        }
    )


def _tool_dict(t: Any) -> Dict[str, Any]:
    schema = getattr(t, "input_schema", getattr(t, "inputSchema", None))
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump()
    elif schema is None:
        schema = {"type": "object", "properties": {}}
    return {
        "name": t.name,
        "description": getattr(t, "description", "") or "",
        "inputSchema": schema,
    }


class _SseSlot:
    """Owns one ClientSession over one SSE connection via a single owner task.

    The context managers are entered and exited inside the owner task so
    anyio cancel scopes never leak across tasks. `acquire()` lazily
    (re)connects with exponential backoff; `mark_dead()` tears the slot down
    so the next acquire rebuilds a fresh session.
    """

    def __init__(self, idx: int, url: str, headers: Dict[str, str], on_server_notification=None):
        self.idx = idx
        self.url = url
        self.headers = headers
        self.on_server_notification = on_server_notification
        self.in_flight = 0
        self._session: Optional[ClientSession] = None
        self._owner_task: Optional[asyncio.Task] = None
        self._close_evt: Optional[asyncio.Event] = None
        self._lock = asyncio.Lock()
        self._backoff = RECONNECT_INITIAL_BACKOFF_S

    async def _owner(self, ready: "asyncio.Future[ClientSession]") -> None:
        close_evt = asyncio.Event()
        try:
            async with sse_client(self.url, headers=self.headers) as (rs, ws):
                async with ClientSession(
                    rs, ws, message_handler=self._handle_server_message
                ) as sess:
                    await sess.initialize()
                    self._close_evt = close_evt
                    self._session = sess
                    if not ready.done():
                        ready.set_result(sess)
                    await close_evt.wait()
        except BaseException as e:  # noqa: BLE001
            if not ready.done():
                ready.set_exception(e)
        finally:
            self._session = None
            self._close_evt = None

    async def _handle_server_message(self, message: Any) -> None:
        # Forward server notifications for cache invalidation + client relay.
        if self.on_server_notification is None:
            return
        try:
            self.on_server_notification(message)
        except Exception:
            logger.debug("server notification handler failed", exc_info=True)

    async def _teardown(self) -> None:
        task, self._owner_task = self._owner_task, None
        evt = self._close_evt
        if evt is not None:
            evt.set()
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except BaseException:
                task.cancel()

    async def acquire(self) -> ClientSession:
        """Return a live session, (re)connecting lazily with backoff."""
        async with self._lock:
            if (
                self._session is not None
                and self._owner_task is not None
                and not self._owner_task.done()
            ):
                return self._session
            await self._teardown()
            last: Optional[BaseException] = None
            for _attempt in range(max(1, CONNECT_RETRIES)):
                ready: "asyncio.Future[ClientSession]" = asyncio.get_running_loop().create_future()
                self._owner_task = asyncio.create_task(self._owner(ready))
                try:
                    self._backoff = RECONNECT_INITIAL_BACKOFF_S
                    return await ready
                except BaseException as e:  # noqa: BLE001
                    last = e
                    await self._teardown()
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, RECONNECT_MAX_BACKOFF_S)
            raise ConnectionError(f"SSE slot {self.idx} unreachable: {last}")

    async def stop(self) -> None:
        await self._teardown()


class McpBridgeClient:
    def __init__(
        self,
        pool_size: int = DEFAULT_POOL_SIZE,
        cloud_url: str = "",
        access_token: str = "",
        agent_id: str = "",
        host_id: str = "",
    ):
        self.pool_size = max(1, int(pool_size))
        self.cloud_url = (cloud_url or "").rstrip("/")
        self.access_token = access_token
        self.agent_id = agent_id
        self.host_id = host_id
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Agent-Id": self.agent_id,
            "X-Host": self.host_id,
        }
        self.http_reads_enabled = os.environ.get("BOTEX_MCP_HTTP_READS", "1") == "1"
        self._call_timeout = _env_num("BOTEX_CALL_TIMEOUT_S", CALL_TIMEOUT_S)
        self._list_timeout = _env_num("BOTEX_LIST_TIMEOUT_S", LIST_TIMEOUT_S)
        self._tools_ttl = _env_num("BOTEX_TOOLS_TTL_S", TOOLS_TTL_S)
        self._slots: List[_SseSlot] = []
        self._tools_cache: Optional[List[Dict[str, Any]]] = None
        self._tools_cached_at = 0.0
        self._resources_cache: Optional[List[Dict[str, Any]]] = None
        self._resources_cached_at = 0.0
        self.on_client_notification = None  # callable(dict) set by the stdio loop
        self._last_list_changed_relay: Dict[str, float] = {}

    def _on_server_notification(self, message: Any) -> None:
        try:
            raw = (
                message.model_dump_json()
                if hasattr(message, "model_dump_json")
                else json.dumps(message)
            )
            data = json.loads(raw)
            method = (data.get("root") or data).get("method") or data.get("method")
            if method == "notifications/tools/list_changed":
                self._tools_cache = None
            if not method or not str(method).startswith("notifications/"):
                return
            # Server broadcasts reach every pooled SSE session — relay each
            # */list_changed to the client once per window, not N times.
            if str(method).endswith("/list_changed"):
                now = time.monotonic()
                if now - self._last_list_changed_relay.get(method, 0.0) < 0.5:
                    return
                self._last_list_changed_relay[method] = now
            if self.on_client_notification:
                self.on_client_notification(data.get("root") or data)
        except Exception:
            logger.debug("failed to relay server notification", exc_info=True)

    async def start(self) -> None:
        self._slots = [
            _SseSlot(
                i, f"{self.cloud_url}/mcp/sse", dict(self.headers), self._on_server_notification
            )
            for i in range(self.pool_size)
        ]
        # Connect eagerly so a bad token fails at startup, not on first call.
        await self._slots[0].acquire()
        await self._prime_cache()

    async def stop(self) -> None:
        await asyncio.gather(*(s.stop() for s in self._slots), return_exceptions=True)

    async def _prime_cache(self) -> None:
        try:
            self._tools_cache = await self._fetch_tools_list()
            self._tools_cached_at = time.time()
        except Exception as e:
            logger.warning("tools/list priming failed: %s", type(e).__name__)
        try:

            async def _res(sess):
                res = await sess.list_resources()
                return [_tool_dict(r) for r in res.resources]

            self._resources_cache = await self._with_slot(_res, self._list_timeout, True)
            self._resources_cached_at = time.time()
        except Exception:
            self._resources_cache = []

    def _pick_slot(self) -> _SseSlot:
        return min(self._slots, key=lambda s: s.in_flight)

    async def _with_slot(self, fn, timeout: float, retry_after_send: bool):
        """Run fn(session) on the least-loaded slot; transport errors tear the
        slot down so the next attempt rebuilds a fresh session."""
        last: Optional[BaseException] = None
        for attempt in range(2):
            slot = self._pick_slot()
            slot.in_flight += 1
            try:
                sess = await slot.acquire()
                return await asyncio.wait_for(fn(sess), timeout=timeout)
            except BaseException as e:  # noqa: BLE001
                last = e
                await slot._teardown()
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                if retry_after_send:
                    continue
                if _is_transport_error(e) and attempt == 0:
                    continue
                break
            finally:
                slot.in_flight -= 1
        raise last  # type: ignore[misc]

    async def _fetch_tools_list(self) -> List[Dict[str, Any]]:
        async def _fetch(sess):
            res = await sess.list_tools()
            return [_tool_dict(t) for t in res.tools]

        return await self._with_slot(_fetch, self._list_timeout, True)

    async def list_tools(self) -> List[Dict[str, Any]]:
        if (
            self._tools_cache is not None
            and (time.time() - self._tools_cached_at) < self._tools_ttl
        ):
            return self._tools_cache
        try:
            self._tools_cache = await self._fetch_tools_list()
            self._tools_cached_at = time.time()
            return self._tools_cache
        except Exception as e:
            if self._tools_cache is not None:
                return self._tools_cache  # serve stale schema rather than nothing
            return [
                {
                    "name": "check_health",
                    "description": f"Connection error: {e}",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

    async def list_resources(self) -> List[Dict[str, Any]]:
        if (
            self._resources_cache is not None
            and (time.time() - self._resources_cached_at) < self._tools_ttl
        ):
            return self._resources_cache
        try:

            async def _res(sess):
                res = await sess.list_resources()
                return [_tool_dict(r) for r in res.resources]

            self._resources_cache = await self._with_slot(_res, self._list_timeout, True)
            self._resources_cached_at = time.time()
        except Exception:
            if self._resources_cache is None:
                self._resources_cache = []
        return self._resources_cache

    async def _call_http(self, name: str, arguments: Dict[str, Any]):
        """Stateless /mcp/http read: one fresh streamable-http exchange per call."""
        async with streamable_http_client(
            f"{self.cloud_url}/mcp/http", headers=dict(self.headers)
        ) as (rs, ws, _get_sid):
            async with ClientSession(rs, ws) as sess:
                await sess.initialize()
                res = await asyncio.wait_for(
                    sess.call_tool(name, arguments), timeout=self._call_timeout
                )
                return res

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> tuple[str, bool]:
        """Returns (text, is_error) — transport failures surface as error text,
        matching the legacy bridge contract."""
        try:
            if self.http_reads_enabled and name in HTTP_READ_TOOLS:
                try:
                    res = await self._call_http(name, arguments)
                except Exception:
                    res = None  # fast-path failure -> fall back to SSE pool
            else:
                res = None
            if res is None:

                async def _call(sess):
                    return await sess.call_tool(name, arguments)

                res = await self._with_slot(_call, self._call_timeout, name in IDEMPOTENT_TOOLS)
            texts = [
                item.text if hasattr(item, "text") else str(item)
                for item in (getattr(res, "content", None) or [res])
            ]
            return "\n".join(texts), bool(getattr(res, "isError", False))
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            return f"[BoteX Cloud Error] Failed to execute '{name}': {type(e).__name__}: {e}", True
