# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX FastAPI Cloud Gateway & Remote MCP SSE Server
===================================================
Production-ready cloud gateway exposing BoteX harness capabilities:
1. Zero Friction Local / Strict Cloud Bearer Token authentication.
2. Remote Model Context Protocol (MCP): StreamableHTTP at /mcp/http, legacy SSE at /mcp/sse and /mcp/messages.
3. RESTful endpoints for health probes, OpenRouter wallet balance, autonomous task delegation,
   operator human-in-the-loop approvals, and workspace Memory Vault operations.
4. Real-time streaming chat over Server-Sent Events (POST /api/chat) and bidirectional WebSockets
   (WS /api/chat/ws) with a 15-second ping heartbeat and push notifications.

Invariant: Single-worker process (workers=1) for in-memory state consistency.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure repository root is on sys.path
try:
    from botex.config import PROJECT_ROOT as _PROJECT_ROOT

    _root_dir = str(_PROJECT_ROOT)
except Exception:
    _current_dir = os.path.dirname(os.path.abspath(__file__))
    _root_dir = (
        os.path.dirname(os.path.dirname(_current_dir))
        if os.path.basename(_current_dir) in ("server", "core")
        else os.path.dirname(_current_dir)
    )

if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)

from urllib.parse import parse_qs, urlparse

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from starlette.types import ASGIApp, Receive, Scope, Send

import server as botex_server
from botex.analytics import aggregate_stats, load_analytics
from botex.approval import PendingAction, TaskAbortedError, default_approval_manager
from botex.catalog import (
    CACHE_FILE_DEFAULT,
    DEFAULT_TTL_HOURS,
    get_consolidated_models,
)
from botex.chat import default_chat_manager, stream_chat_turn
from botex.config import (
    MCP_CLIENT_ORIGIN,
    WorkspaceOutOfBounds,
    analytics_setting,
    get_tasks_dir,
    resolve_provider,
    resolve_workspace,
)
from botex.core.context_guard import read_scratchpad
from botex.core.device_tokens import (
    DeviceTokenRecord,
    DeviceTokenStore,
    default_device_token_store,
)
from botex.core.tool_executor import (
    ToolExecutor,
    register_edge_executor_resolver,
)
from botex.engine import run_botex_task
from botex.events import EventKind, emitter_or_none, get_emitter
from botex.memory import (
    _get_index_db,
    _resolve_scope_dirs,
    delete_memory,
    find_graph_health,
    get_backlinks,
    list_memory_projects,
    read_memory,
    save_memory,
    search_memory,
)
from botex.plugins import PluginVaultManager
from botex.rate_limiter import get_rate_limiter
from botex.security import (
    RESERVED_ORIGIN_AGENTS,
    coerce_text,
    filter_reserved_tags,
    mask_secrets,
    sanitize_identifier,
    sanitize_origin_claim,
)
from botex.skills import delete_skill, get_skill, install_skill_async, list_skills
from botex.state import load_inflight_checkpoints
from botex.wallet import default_wallet_service

logger = logging.getLogger(__name__)

# Single-worker invariant for in-process state consistency
WORKERS_INVARIANT: int = 1
START_TIME: float = time.time()

# Shared reference to task repository in server
_TASKS: Dict[str, Dict[str, Any]] = botex_server._TASKS


def _save_task_persistent(task_id: str, entry: Dict[str, Any], workspace_dir: str = "") -> None:
    """Persist task metadata and receipt to .botex/tasks/<task_id>.json across server restarts."""
    try:
        safe_tid = sanitize_identifier(task_id)
        if not safe_tid:
            return
        tasks_dir = get_tasks_dir(workspace_dir)
        existing = next(
            (f for f in tasks_dir.iterdir() if f.is_file() and f.name == f"{safe_tid}.json"),
            None,
        )
        p = existing if existing is not None else (tasks_dir / f"{safe_tid}.json")
        clean = {k: v for k, v in entry.items() if k != "async_task"}
        p.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to persist task metadata: %s", type(exc).__name__)


def _load_tasks_persistent(workspace_dir: str = "") -> None:
    """Restore historical background tasks from disk into memory cache."""
    try:
        tasks_dir = get_tasks_dir(workspace_dir)
        for f in tasks_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                tid = data.get("task_id") or f.stem
                if tid not in _TASKS:
                    data["async_task"] = None
                    # Restart reaper: a persisted RUNNING/QUEUED entry has no
                    # live worker after a server restart — mark it failed
                    # instead of leaving an unkillable zombie in the PWA.
                    if data.get("status") in {"RUNNING", "QUEUED"}:
                        data["status"] = "FAILED"
                        data["failure_kind"] = "server_restarted"
                        data["result"] = data.get("result") or (
                            "Task interrupted by server restart."
                        )
                        data["finished_at"] = data.get("finished_at") or time.time()
                    _TASKS[tid] = data
            except Exception:
                pass
    except Exception as exc:
        logger.warning(f"Failed to load persistent tasks: {exc}")


# WAN Reliability: Idempotency Cache & SSE Event Replay Buffer
_IDEMPOTENCY_CACHE: Dict[str, Dict[str, Any]] = {}
_SSE_REPLAY_BUFFER: Dict[str, List[Dict[str, Any]]] = {}
_MAX_REPLAY_EVENTS_PER_SESSION: int = 100
_IDEMPOTENCY_TTL_SECONDS: float = 86400.0
# Remote MCP SSE: the first /mcp/messages post binds session_id to its client IP,
# so a session id lifted from logs cannot be driven from a different peer.
_SSE_SESSION_BINDINGS: Dict[str, Dict[str, Any]] = {}
_SSE_SESSION_BINDINGS_CAP: int = 10_000


def _bind_sse_session(session_id: str, client_ip: str) -> None:
    while len(_SSE_SESSION_BINDINGS) >= _SSE_SESSION_BINDINGS_CAP:
        _SSE_SESSION_BINDINGS.pop(next(iter(_SSE_SESSION_BINDINGS)), None)
    _SSE_SESSION_BINDINGS[session_id] = {"ip": client_ip, "created": time.time()}


def _cleanup_idempotency_cache() -> None:
    """Evict expired idempotency keys older than TTL."""
    now = time.time()
    expired = [
        k
        for k, v in _IDEMPOTENCY_CACHE.items()
        if now - v.get("created_at", 0) > _IDEMPOTENCY_TTL_SECONDS
    ]
    for k in expired:
        _IDEMPOTENCY_CACHE.pop(k, None)


def _record_sse_event(
    session_id: str, event_id: str, event_type: str, data: Dict[str, Any]
) -> None:
    """Store SSE event in session replay ring buffer."""
    buf = _SSE_REPLAY_BUFFER.setdefault(session_id, [])
    buf.append(
        {
            "id": event_id,
            "event": event_type,
            "data": data,
            "timestamp": time.time(),
        }
    )
    if len(buf) > _MAX_REPLAY_EVENTS_PER_SESSION:
        _SSE_REPLAY_BUFFER[session_id] = buf[-_MAX_REPLAY_EVENTS_PER_SESSION:]


def _get_sse_replay_events(session_id: str, last_event_id: Optional[str]) -> List[Dict[str, Any]]:
    """Return missed SSE events occurring after last_event_id."""
    buf = _SSE_REPLAY_BUFFER.get(session_id, [])
    if not buf or not last_event_id:
        return []
    # Find index of last_event_id
    idx = -1
    for i, ev in enumerate(buf):
        if ev["id"] == last_event_id:
            idx = i
            break
    if idx != -1 and idx + 1 < len(buf):
        return buf[idx + 1 :]
    return []


# Connected WebSockets for real-time push notifications with serialization locks
_ACTIVE_WEBSOCKETS: Dict[WebSocket, asyncio.Lock] = {}

# Per-socket task-event subscriptions (server-side filter — task telemetry is
# pushed only to clients that subscribed to that specific task_id).
_WS_TASK_SUBS: Dict[WebSocket, set] = {}


_active_chat_tasks: dict[str, asyncio.Task] = {}


def _unload_ollama_model_sync(model_name: str = "") -> None:
    """Best-effort async/sync unload of an Ollama model from VRAM."""
    import urllib.request

    try:
        cfg = botex_server.load_config().get("providers", {}).get("ollama", {})
        base_url = cfg.get("base_url", "http://127.0.0.1:11434/v1")
        clean_base = base_url.rstrip("/").removesuffix("/v1")
        endpoint = f"{clean_base}/api/generate"
        payload = json.dumps(
            {"model": model_name or "bielik-direct:latest", "keep_alive": 0}
        ).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3):
            pass
    except Exception:
        pass


async def _send_ws_json_safe(ws: WebSocket, payload: Dict[str, Any]) -> bool:
    """Send JSON payload to a WebSocket connection serialized under its mutex lock."""
    lock = _ACTIVE_WEBSOCKETS.get(ws)
    if lock is None:
        return False
    try:
        async with lock:
            await ws.send_json(payload)
        return True
    except Exception:
        _ACTIVE_WEBSOCKETS.pop(ws, None)
        return False


def _broadcast_approval_request(action: PendingAction) -> None:
    """Notify connected WebSockets when an action requires operator approval."""
    if not _ACTIVE_WEBSOCKETS:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("No active event loop to broadcast approval request")
        return

    # Zero Data Retention: mask secrets in approval preview and params (CWE-312)
    payload = {
        "type": "approval_required",
        "action": botex_server.mask_secrets_recursive(action.to_dict()),
    }
    for ws in list(_ACTIVE_WEBSOCKETS.keys()):
        loop.create_task(_send_ws_json_safe(ws, payload))


default_approval_manager.add_listener(_broadcast_approval_request)


# ---------------------------------------------------------------------------
# Pydantic Request Models
# ---------------------------------------------------------------------------


class TaskCreateRequest(BaseModel):
    task: str
    files: List[str] = Field(default_factory=list)
    workspace_dir: str = ""
    model: str = ""
    profile: str = ""
    provider: str = ""
    mode: str = ""
    max_turns: int = 0
    max_tokens: int = 0
    max_duration_s: int = 0
    budget_limit_usd: float = -1.0
    allow_destructive: bool = False
    allow_exec: bool = False
    allow_net: bool = False
    net_allowed_hosts: List[str] = Field(default_factory=list)
    net_allowed_urls: List[str] = Field(default_factory=list)
    output_path: str = ""
    verify_command: str = ""
    recipe: str = ""
    allow_non_zdr: bool = False
    allow_expensive_model: bool = False
    approval_policy: str = ""
    executor: str = "botex"
    agent_type: str = "default"


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    reason: str = ""
    remember: bool = False


class MemorySaveRequest(BaseModel):
    title: str
    content: str
    kind: str = "context"
    tags: List[str] = Field(default_factory=list)
    memory_id: Optional[str] = None
    origin: str = "cloud_api"
    scope: str = "local"
    workspace_dir: str = "."
    project: Optional[str] = None


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    workspace_dir: str = ""
    model: str = ""
    provider: str = ""
    profile: str = ""
    system_prompt: Optional[str] = None


class SaveChatToMemoryRequest(BaseModel):
    workspace_dir: str = ""
    title: Optional[str] = None
    kind: str = "context"
    scope: str = "local"
    content: Optional[str] = None
    tags: Optional[List[str]] = None


class SkillInstallRequest(BaseModel):
    name: str = ""
    content: str = ""
    source_url: str = ""
    origin_repo: str = ""
    version: str = ""
    commit_sha: str = ""
    description: str = ""
    taxonomy: Optional[List[str]] = None


class CuratorRunRequest(BaseModel):
    workspace_dir: str = "."
    scope: str = "all"
    force: bool = False
    model_override: Optional[str] = None
    notify_telegram: Optional[bool] = None


class PluginResolveRequest(BaseModel):
    name: str
    action: str
    workspace_dir: str = "."


class VisionInspectRequest(BaseModel):
    image_base64: str
    prompt_hint: str = ""
    reference_hash: Optional[int] = None
    task_id: Optional[str] = None
    workspace_dir: str = "."


class EdgeTokenIssueRequest(BaseModel):
    device_id: str
    workspace_id: str
    scopes: Optional[List[str]] = None
    ttl_s: Optional[int] = None
    label: str = ""


class EdgeDelegateRequest(BaseModel):
    device_id: str = ""
    workspace_id: str = ""
    skill: str
    sha256: str = ""
    params: Dict[str, Any] = Field(default_factory=dict)
    task: str = ""
    require_approval: Optional[bool] = None


_DELEGATE_FORBIDDEN_PARAM_KEYS = frozenset(
    {
        "command",
        "cmd",
        "shell",
        "script",
        "code",
        "eval",
        "exec",
        "argv",
        "args",
        "powershell",
        "pwsh",
        "bash",
    }
)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _find_forbidden_delegate_key(obj: Any, _depth: int = 0) -> Optional[str]:
    if _depth > 8:
        return "nesting_depth_exceeded"
    if isinstance(obj, dict):
        for k, v in obj.items():
            norm_k = unicodedata.normalize("NFKC", str(k)).strip().lower()
            if norm_k in _DELEGATE_FORBIDDEN_PARAM_KEYS:
                return norm_k
            nested = _find_forbidden_delegate_key(v, _depth + 1)
            if nested:
                return nested
    elif isinstance(obj, list):
        for item in obj:
            nested = _find_forbidden_delegate_key(item, _depth + 1)
            if nested:
                return nested
    return None


def _validate_delegate_params_safe(params: Any) -> Dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Delegation params must be a JSON object.",
        )
    forbidden = _find_forbidden_delegate_key(params)
    if forbidden:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Forbidden delegation parameter key '{forbidden}'. Raw shell/command execution is disallowed.",
        )
    try:
        encoded = json.dumps(params, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Delegation params must be JSON-serializable.",
        )
    if len(encoded) > 32768:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Delegation params exceed 32 KiB limit.",
        )
    return dict(params)


from contextlib import asynccontextmanager, suppress

# ---------------------------------------------------------------------------
# FastAPI Application & Middlewares
# ---------------------------------------------------------------------------


_WARMUP_COMPLETED = False
_MAX_TASK_CACHE_BYTES = 8 * 1024 * 1024
_EDGE_PING_INTERVAL_S = 30.0
_TRUSTED_PROXY_HOSTS = frozenset({"127.0.0.1", "::1"})
_INTERRUPTED_INFLIGHT_TASKS: List[Dict[str, Any]] = []


class _GatewayRateLimiter:
    """Sliding-window per-client HTTP/WSS limiter for the cloud gateway."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: Dict[str, List[float]] = {}

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def check_allow(self, client_key: str) -> tuple[bool, float]:
        raw_rpm = os.environ.get("BOTEX_GATEWAY_RPM", "240").strip()
        try:
            limit = int(raw_rpm)
        except ValueError:
            limit = 240
        if limit <= 0:
            return True, 0.0
        now = time.monotonic()
        cutoff = now - 60.0
        key = (client_key or "unknown")[:64]
        with self._lock:
            if len(self._hits) > 4096 and key not in self._hits:
                stale = [
                    k for k, ts_list in self._hits.items() if not ts_list or ts_list[-1] < cutoff
                ]
                for k in stale[:1024]:
                    self._hits.pop(k, None)
                if len(self._hits) > 4096:
                    self._hits.pop(next(iter(self._hits)), None)
            timestamps = [t for t in self._hits.get(key, []) if t >= cutoff]
            if len(timestamps) >= limit:
                retry_after = max(0.1, 60.0 - (now - timestamps[0]))
                self._hits[key] = timestamps
                return False, retry_after
            timestamps.append(now)
            self._hits[key] = timestamps
            return True, 0.0


_GATEWAY_RATE_LIMITER = _GatewayRateLimiter()


def _extract_client_ip_from_scope(scope: Dict[str, Any]) -> str:
    """Return peer IP, honoring X-Forwarded-For only from trusted local reverse proxy."""
    client = scope.get("client")
    peer_host = str(client[0]) if client and len(client) > 0 else ""
    if peer_host in _TRUSTED_PROXY_HOSTS:
        headers = dict(scope.get("headers") or [])
        xff = headers.get(b"x-forwarded-for", b"").decode("latin1", errors="ignore").strip()
        if xff:
            # The rightmost entry is the address our trusted proxy actually
            # observed (proxies append). The leftmost entries are client-
            # controlled: trusting them would let one remote client forge a
            # fresh rate-limit key per request ("spoofed, real" lists).
            entries = [e.strip() for e in xff.split(",") if e.strip()]
            if entries:
                return entries[-1][:64]
    return peer_host or "unknown"


class RemoteWssToolExecutor(ToolExecutor):
    """Dispatches workspace tool calls over an authenticated outbound edge WebSocket."""

    def __init__(self, session: "_EdgeSession", timeout_s: float = 120.0) -> None:
        self._session = session
        self._timeout_s = float(timeout_s)
        self._cache_lock = threading.RLock()
        self._task_caches: Dict[str, OrderedDict[str, Dict[str, Any]]] = {}
        self._task_cache_bytes: Dict[str, int] = {}

    def _cache_key(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        path = str(args.get("path") or "").strip()
        if not path:
            return None
        if tool_name == "read_file":
            return f"read_file:{path}"
        if tool_name == "read_file_lines":
            return f"read_file_lines:{path}:{args.get('start', 1)}:{args.get('end', 50)}"
        return None

    def _get_cached(self, task_id: str, key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        with self._cache_lock:
            lru = self._task_caches.get(task_id)
            if not lru or key not in lru:
                return None
            lru.move_to_end(key)
            return dict(lru[key])

    def _put_cached(self, task_id: str, key: str, sha256: str, result: Dict[str, Any]) -> None:
        try:
            size_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        except Exception:
            return
        if size_bytes <= 0 or size_bytes > _MAX_TASK_CACHE_BYTES:
            return
        with self._cache_lock:
            lru = self._task_caches.setdefault(task_id, OrderedDict())
            cur_bytes = self._task_cache_bytes.get(task_id, 0)
            old = lru.pop(key, None)
            if old is not None:
                cur_bytes -= int(old.get("size_bytes") or 0)
            while lru and (cur_bytes + size_bytes > _MAX_TASK_CACHE_BYTES):
                _, evicted = lru.popitem(last=False)
                cur_bytes -= int(evicted.get("size_bytes") or 0)
            lru[key] = {
                "sha256": sha256,
                "result": dict(result),
                "size_bytes": size_bytes,
            }
            self._task_cache_bytes[task_id] = max(0, cur_bytes) + size_bytes

    def _invalidate_path(self, task_id: str, path: str) -> None:
        clean = str(path or "").strip()
        if not clean:
            return
        with self._cache_lock:
            lru = self._task_caches.get(task_id)
            if not lru:
                return
            cur_bytes = self._task_cache_bytes.get(task_id, 0)
            drop_keys = [
                k
                for k in lru
                if k == f"read_file:{clean}" or k.startswith(f"read_file_lines:{clean}:")
            ]
            for k in drop_keys:
                old = lru.pop(k, None)
                if old is not None:
                    cur_bytes -= int(old.get("size_bytes") or 0)
            self._task_cache_bytes[task_id] = max(0, cur_bytes)

    def clear_task_cache(self, task_id: str) -> None:
        with self._cache_lock:
            self._task_caches.pop(task_id, None)
            self._task_cache_bytes.pop(task_id, None)

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        workspace_root: Path,
        task_id: str = "default",
        call_id: str = "",
    ) -> Dict[str, Any]:
        if tool_name == "read_scratchpad":
            # The scratchpad vault lives on the cloud host, not on the edge
            # device — serve it locally so engine offloads keep working.
            return read_scratchpad(str(args.get("memory_id") or ""), cwd=workspace_root)

        del workspace_root
        cache_key = self._cache_key(tool_name, args)
        cached_entry = self._get_cached(task_id, cache_key)
        cached_sha = str(cached_entry.get("sha256") or "") if cached_entry else None

        res = await self._session.dispatch_tool_call(
            tool_name=tool_name,
            args=dict(args or {}),
            task_id=task_id,
            call_id=call_id or uuid.uuid4().hex[:12],
            cached_sha256=cached_sha or None,
            timeout_s=self._timeout_s,
        )
        if not isinstance(res, dict):
            return {"ok": False, "error": "Invalid response payload from edge device."}

        if res.get("status") == "unchanged" and cached_entry is not None:
            return dict(cached_entry["result"])

        if cache_key and res.get("ok") and res.get("file_sha256"):
            self._put_cached(task_id, cache_key, str(res["file_sha256"]), res)
        elif tool_name in {"apply_patch", "create_file", "delete_file"} and res.get("ok"):
            self._invalidate_path(task_id, str(args.get("path") or ""))
        elif tool_name == "move_file" and res.get("ok"):
            self._invalidate_path(task_id, str(args.get("src_path") or ""))
            self._invalidate_path(task_id, str(args.get("dst_path") or ""))
        elif tool_name == "run_command" and res.get("ok"):
            # A successful command may mutate arbitrary files on the edge —
            # cached digests can no longer be trusted for "unchanged" replies.
            self.clear_task_cache(task_id)
        return res


class _EdgeSession:
    """Active authenticated WebSocket session to a remote edge runner."""

    def __init__(
        self,
        *,
        websocket: WebSocket,
        token_record: DeviceTokenRecord,
        device_id: str,
        workspace_id: str,
        capabilities: Dict[str, Any],
        protocol_version: str = "1.0",
        client_version: str = "",
    ) -> None:
        self.websocket = websocket
        self.token_record = token_record
        self.device_id = device_id
        self.workspace_id = workspace_id
        self.capabilities = dict(capabilities or {})
        self.protocol_version = protocol_version
        self.client_version = client_version
        self.connected_at = time.time()
        self.last_seen_at = self.connected_at
        self._send_lock = asyncio.Lock()
        self._pending: Dict[str, asyncio.Future] = {}
        self._approval_task_ids: set[str] = set()
        self.executor = RemoteWssToolExecutor(self)

    @property
    def session_key(self) -> str:
        return f"{self.device_id}:{self.workspace_id}"

    async def send_frame(self, payload: Dict[str, Any]) -> None:
        async with self._send_lock:
            await self.websocket.send_json(payload)

    async def dispatch_tool_call(
        self,
        *,
        tool_name: str,
        args: Dict[str, Any],
        task_id: str,
        call_id: str,
        cached_sha256: Optional[str] = None,
        timeout_s: float = 120.0,
    ) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[call_id] = fut
        frame: Dict[str, Any] = {
            "type": "tool_call",
            "call_id": call_id,
            "task_id": task_id,
            "tool_name": tool_name,
            "args": args,
        }
        if cached_sha256:
            frame["cached_sha256"] = cached_sha256
        try:
            await self.send_frame(frame)
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": f"Edge tool '{tool_name}' timed out after {timeout_s:.0f}s.",
            }
        except Exception as exc:
            return {"ok": False, "error": f"Edge dispatch error: {mask_secrets(str(exc))}"}
        finally:
            self._pending.pop(call_id, None)

    async def dispatch_delegation(
        self,
        *,
        skill: str,
        sha256: str,
        params: Dict[str, Any],
        task_id: str,
        call_id: str = "",
        timeout_s: float = 120.0,
    ) -> Dict[str, Any]:
        cid = call_id or uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[cid] = fut
        frame: Dict[str, Any] = {
            "type": "delegate_skill",
            "call_id": cid,
            "task_id": task_id,
            "skill": skill,
            "sha256": sha256,
            "params": params,
        }
        try:
            await self.send_frame(frame)
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": f"Remote skill '{skill}' timed out after {timeout_s:.0f}s.",
            }
        except Exception as exc:
            return {"ok": False, "error": f"Edge delegation error: {mask_secrets(str(exc))}"}
        finally:
            self._pending.pop(cid, None)

    def resolve_pending(self, call_id: str, result: Dict[str, Any]) -> None:
        fut = self._pending.get(call_id)
        if fut is not None and not fut.done():
            fut.set_result(result)

    def fail_all_pending(self, reason: str) -> None:
        for cid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result({"ok": False, "error": reason})
            self._pending.pop(cid, None)
        for tid in list(self._approval_task_ids):
            try:
                default_approval_manager.abort_task(tid)
            except Exception:
                pass
            self._approval_task_ids.discard(tid)

    def is_live(self) -> bool:
        return (
            _EDGE_SESSIONS.get(self.session_key) is self
            and (time.time() - self.last_seen_at) <= 2.0 * _EDGE_PING_INTERVAL_S
        )

    def to_public_dict(self) -> Dict[str, Any]:
        del_caps = self.capabilities.get("delegation")
        del_enabled = isinstance(del_caps, dict) and bool(del_caps.get("enabled"))
        can_delegate = bool(
            self.token_record.has_scope(f"delegate_local:{self.workspace_id}") and del_enabled
        )
        return {
            "device_id": self.device_id,
            "workspace_id": self.workspace_id,
            "jti": self.token_record.jti,
            "capabilities": dict(self.capabilities),
            "protocol_version": self.protocol_version,
            "client_version": self.client_version,
            "connected_at": self.connected_at,
            "last_seen_at": self.last_seen_at,
            "pending_calls": len(self._pending),
            "healthy": self.is_live(),
            "can_delegate": can_delegate,
        }


_EDGE_SESSIONS: Dict[str, _EdgeSession] = {}

_EDGE_TOKEN_STORE_SINGLETON: Optional[DeviceTokenStore] = None


def _edge_token_store() -> DeviceTokenStore:
    """Process-wide store: REST revocations must be visible to live WSS loops.

    A fresh store per call would leave long-lived websocket sessions pinned to
    a stale snapshot, so their periodic ``is_revoked`` heartbeat check could
    never observe a revocation written by another request's store instance.
    """
    global _EDGE_TOKEN_STORE_SINGLETON
    if _EDGE_TOKEN_STORE_SINGLETON is None:
        _EDGE_TOKEN_STORE_SINGLETON = default_device_token_store(resolve_workspace(""))
    return _EDGE_TOKEN_STORE_SINGLETON


def _resolve_edge_session(workspace_ref: str) -> Optional[_EdgeSession]:
    raw = str(workspace_ref or "").strip()
    if not raw:
        return None
    if raw.startswith("edge://"):
        body = raw[len("edge://") :].strip("/")
        if "/" in body:
            dev, ws = body.split("/", 1)
            return _EDGE_SESSIONS.get(f"{dev}:{ws}")
    if raw.startswith("edge:") and raw.count(":") >= 2:
        _, dev, ws = raw.split(":", 2)
        return _EDGE_SESSIONS.get(f"{dev}:{ws}")
    if raw in _EDGE_SESSIONS:
        return _EDGE_SESSIONS[raw]
    matches = [s for s in _EDGE_SESSIONS.values() if s.workspace_id == raw]
    if not matches:
        return None
    matches.sort(key=lambda s: s.connected_at, reverse=True)
    return matches[0]


def _resolve_active_edge_executor(workspace_ref: str) -> Optional[ToolExecutor]:
    sess = _resolve_edge_session(workspace_ref)
    return sess.executor if sess is not None else None


async def _close_sessions_for_jti(jti: str) -> int:
    closed = 0
    for key, sess in list(_EDGE_SESSIONS.items()):
        if sess.token_record.jti == jti:
            sess.fail_all_pending("Device token revoked")
            with suppress(Exception):
                await sess.websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION, reason="Token revoked"
                )
            _EDGE_SESSIONS.pop(key, None)
            closed += 1
    return closed


async def _warmup_caches() -> None:
    global _WARMUP_COMPLETED
    if _WARMUP_COMPLETED:
        return
    _WARMUP_COMPLETED = True

    # Under pytest/test harness avoid starting asynchronous background SQLite migrations
    # across threads to prevent cross-thread SQLite connection teardown races.
    in_test = bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("BOTEX_DISABLE_PROJECT_MIGRATION")
        or "pytest" in sys.modules
    )
    if not in_test:
        try:
            from botex.ops.migrate_projects import ensure_projects_migrated

            ws = resolve_workspace("")
            await asyncio.to_thread(ensure_projects_migrated, ws)
        except Exception:
            pass
    try:
        from botex.benchmarks import fetch_openrouter_benchmarks

        await asyncio.to_thread(fetch_openrouter_benchmarks)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Analytics digest (optional local-LLM, e.g. Bielik via Ollama)
# ---------------------------------------------------------------------------

_DIGEST_FILENAME = "analytics_digest.json"
_digest_lock = asyncio.Lock()


def _digest_cache_path(workspace_dir: str) -> Path:
    ws_root = Path(workspace_dir or ".").resolve()
    botex_dir = ws_root / ".botex"
    botex_dir.mkdir(parents=True, exist_ok=True)
    return botex_dir / _DIGEST_FILENAME


def _find_latest_digest(workspace_dir: str) -> Optional[Dict[str, Any]]:
    try:
        p = _digest_cache_path(workspace_dir)
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("content"):
            return None
        return {
            "id": data.get("id") or "latest_digest",
            "title": data.get("title") or "Analytics Digest",
            "timestamp": data.get("timestamp") or "",
            "content": data.get("content") or "",
        }
    except Exception:
        return None


async def _generate_analytics_digest(
    workspace_dir: str,
    provider: str = "",
    model: str = "",
    days: int = 1,
) -> Dict[str, Any]:
    """Summarize recent run telemetry with a small local model.

    The prompt carries only a pre-aggregated compact JSON (<500 tokens) —
    small ARM-class local models cannot process large payloads.
    """
    if _digest_lock.locked():
        return {"ok": False, "error": "digest already running"}
    async with _digest_lock:
        return await _run_analytics_digest(workspace_dir, provider, model, days)


# Local LLM serving stacks seen in the wild: llama-server (Bielik/Qwen on the
# VPS) listens on :8080, Ollama defaults to :11434. Re-exported from
# botex.providers.providers so catalog, chat, and digest share one probe cache.
from botex.providers.providers import (
    _LOCAL_LLM_BASE_URL_RESOLVED as _LOCAL_LLM_BASE_URL_RESOLVED,
)
from botex.providers.providers import (
    resolve_local_llm_base_url as _resolve_local_llm_base_url,
)
from botex.providers.providers import (
    resolve_model_for_openai_client as _resolve_model_for_openai_client,
)


async def _run_analytics_digest(
    workspace_dir: str,
    provider: str,
    model: str,
    days: int,
) -> Dict[str, Any]:
    from botex.providers import build_async_client, sanitize_provider_error

    cfg = botex_server.load_config()
    prov_name = (provider or analytics_setting("digest_provider") or "").strip()
    model_id = (model or analytics_setting("digest_model") or "").strip()
    if not prov_name or not model_id:
        return {"ok": False, "error": "digest provider/model not configured"}
    prov_cfg = (cfg.get("providers") or {}).get(prov_name) or {}
    prov_cfg = await asyncio.to_thread(_resolve_local_llm_base_url, prov_name, prov_cfg)

    stats = await asyncio.to_thread(aggregate_stats, load_analytics(), days)
    compact = {
        "days": days,
        "runs": stats["runs"],
        "done_pct": stats["done_pct"],
        "cost_usd": stats["cost_usd"],
        "cache_hit_pct": stats["cache_hit_pct"],
        "avg_duration_s": stats["avg_duration_s"],
        "failures": {k: v for k, v in stats["status_counts"].items() if k != "DONE"},
        "top_models": [
            {
                "model": m["model"],
                "runs": m["runs"],
                "cost": m["cost_usd"],
                "fail": m["top_failure"],
            }
            for m in stats["by_model"][:5]
        ],
        "daily_last7": [
            {"d": d["date"], "runs": d["runs"], "usd": d["cost_usd"]} for d in stats["daily"][-7:]
        ],
    }
    stats_json = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))[:1500]

    messages = [
        {
            "role": "system",
            "content": "Jesteś asystentem telemetrycznym BoteX. Odpowiadasz po polsku, maksymalnie 6 zwięzłych punktów. Bez wstępów.",
        },
        {
            "role": "user",
            "content": f"Agregat uruchomień agenta ({days}d): {stats_json}\nStreść: koszt, awarie, najdroższy model, trend.",
        },
    ]
    try:
        client = build_async_client(prov_name, prov_cfg)
        resolved_url = str(prov_cfg.get("base_url") or "")
        effective_model = (
            "bielik:latest"
            if (":8080" in resolved_url and model_id == "bielik-direct:latest")
            else model_id
        )
        if (
            prov_name in ("ollama", "bielik", "local")
            or ":8080" in resolved_url
            or ":11434" in resolved_url
        ):
            effective_model = await _resolve_model_for_openai_client(client, effective_model)
        eff_low = effective_model.lower()
        if "qwen" in eff_low:
            extra_body: Optional[Dict[str, Any]] = {
                "chat_template_kwargs": {"enable_thinking": False}
            }
        elif "bielik" in eff_low:
            extra_body = {
                "chat_template_kwargs": {"decision_mode": True, "reasoning_effort": "none"}
            }
        else:
            extra_body = None
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=effective_model,
                messages=messages,
                max_tokens=int(analytics_setting("digest_max_tokens") or 320),
                temperature=0.3,
                stream=False,
                extra_body=extra_body,
            ),
            timeout=float(analytics_setting("digest_timeout_s") or 180),
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        return {"ok": False, "error": sanitize_provider_error(exc)}
    if not text:
        return {"ok": False, "error": "empty digest"}

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    digest_payload = {
        "id": "latest_digest",
        "title": f"Analytics Digest {ts[:10]} {ts[11:16]} UTC",
        "timestamp": ts,
        "content": f"Model: {prov_name}/{model_id} | window: {days}d | generated: {ts}\n\n{text}",
        "model": f"{prov_name}/{model_id}",
        "days": days,
    }
    try:
        cache_file = _digest_cache_path(workspace_dir)
        await asyncio.to_thread(
            cache_file.write_text,
            json.dumps(digest_payload, ensure_ascii=False, indent=2),
            "utf-8",
        )
        err = None
    except Exception as exc:
        err = str(exc)
    return {
        "ok": not err,
        "text": text,
        "memory_id": None,
        "error": err,
    }


_BREAKER_PROBE_URLS = {
    # Only providers with a public, unauthenticated health endpoint —
    # a 401 probe would be scored as a failure and re-trip the breaker.
    "openrouter": "https://openrouter.ai/api/v1/models",
    "openrouter-free": "https://openrouter.ai/api/v1/models",
}
_BREAKER_PROBE_INTERVAL_S = 60.0


async def _breaker_probe_tick(breaker: Any) -> None:
    """One probe round: HALF_OPEN providers get a synthetic health ping so
    recovery does not wait for the next real request to act as the probe."""
    from botex.providers.http_pool import get_async_client

    for name, url in _BREAKER_PROBE_URLS.items():
        if breaker.get_state(name) != breaker.HALF_OPEN:
            continue
        try:
            breaker.can_execute(name)
        except Exception:
            continue
        try:
            client = get_async_client()
            resp = await client.get(url, timeout=10.0)
            if 200 <= resp.status_code < 300:
                breaker.record_success(name)
            else:
                breaker.record_failure(name)
        except Exception:
            breaker.release_probe(name)


async def _breaker_prober_loop() -> None:
    from botex.providers.retry import get_circuit_breaker

    breaker = get_circuit_breaker()
    await asyncio.sleep(_BREAKER_PROBE_INTERVAL_S)
    while True:
        try:
            await _breaker_probe_tick(breaker)
        except Exception:
            logger.exception("breaker probe tick failed")
        await asyncio.sleep(_BREAKER_PROBE_INTERVAL_S)


async def _analytics_digest_loop() -> None:
    """Periodic digest writer — runs only when analytics.digest_enabled=true."""
    interval = float(analytics_setting("digest_interval_s") or 3600)
    await asyncio.sleep(60)
    while True:
        try:
            ws = resolve_workspace("")
            res = await _generate_analytics_digest(ws)
            if not res.get("ok"):
                logger.warning("analytics digest skipped: %s", res.get("error"))
        except Exception:
            logger.exception("analytics digest tick failed")
        await asyncio.sleep(max(300.0, interval))


@asynccontextmanager
async def _base_lifespan(app: FastAPI):
    register_edge_executor_resolver(_resolve_active_edge_executor)
    _INTERRUPTED_INFLIGHT_TASKS.clear()
    try:
        checkpoints = load_inflight_checkpoints(resolve_workspace(""))
        for ckpt in checkpoints:
            _INTERRUPTED_INFLIGHT_TASKS.append(
                {
                    "task_id": ckpt.task_id,
                    "goal": coerce_text(mask_secrets(ckpt.goal))[:160],
                    "created_at": ckpt.created_at,
                    "failed_attempts": len(ckpt.failed_attempts),
                    "recovery_status": "interrupted_on_restart",
                }
            )
        if _INTERRUPTED_INFLIGHT_TASKS:
            logger.info(
                "Detected %d interrupted in-flight checkpoint(s) on startup.",
                len(_INTERRUPTED_INFLIGHT_TASKS),
            )
    except Exception:
        logger.debug("Inflight checkpoint scan skipped during startup", exc_info=True)
    app.state.interrupted_tasks = list(_INTERRUPTED_INFLIGHT_TASKS)
    # Non-blocking background warmup
    warmup_task = asyncio.create_task(_warmup_caches())
    digest_task: Optional[asyncio.Task] = None
    if analytics_setting("digest_enabled"):
        digest_task = asyncio.create_task(_analytics_digest_loop())
    prober_task = asyncio.create_task(_breaker_prober_loop())
    try:
        yield
    finally:
        register_edge_executor_resolver(None)
        if not warmup_task.done():
            try:
                # Allow a short grace period for in-flight tasks to finish safely
                await asyncio.wait_for(asyncio.shield(warmup_task), timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                warmup_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await warmup_task
        if digest_task is not None:
            digest_task.cancel()
            with suppress(asyncio.CancelledError):
                await digest_task
        prober_task.cancel()
        with suppress(asyncio.CancelledError):
            await prober_task
        try:
            from botex.providers.http_pool import close_http_pool

            await close_http_pool()
        except Exception:
            logger.exception("Failed closing http pool on shutdown")
        try:
            from botex.memory.memory import close_index_dbs

            close_index_dbs()
        except Exception:
            logger.exception("Failed closing memory index dbs on shutdown")


def _build_mcp_http_app() -> Any:
    return botex_server.mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=_mcp_transport_security(),
    )


class _MCPHttpHolder:
    """ASGI indirection so the StreamableHTTP app can be rebuilt per lifespan."""

    def __init__(self) -> None:
        self.app: Optional[ASGIApp] = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.app is None:
            resp = JSONResponse(status_code=503, content={"detail": "MCP transport not ready"})
            await resp(scope, receive, send)
            return
        if scope.get("type") == "http" and not scope.get("path"):
            scope = {**scope, "path": "/"}

        async def _send_with_no_buffer(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                hdrs = list(message.get("headers") or [])
                is_sse_resp = any(
                    k.lower() == b"content-type" and b"text/event-stream" in v.lower()
                    for k, v in hdrs
                )
                if is_sse_resp and not any(k.lower() == b"x-accel-buffering" for k, _ in hdrs):
                    hdrs.append((b"x-accel-buffering", b"no"))
                    message = {**message, "headers": hdrs}
            await send(message)

        await self.app(scope, receive, _send_with_no_buffer)


_mcp_http_holder = _MCPHttpHolder()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from botex.core.tracing import init_tracing

    init_tracing()
    # Mounted sub-apps do not receive lifespan events, and a session manager
    # can run only once, so each app lifetime builds a fresh StreamableHTTP app.
    http_app = _build_mcp_http_app()
    _mcp_http_holder.app = http_app
    try:
        async with http_app.router.lifespan_context(http_app):
            async with _base_lifespan(app):
                yield
    finally:
        _mcp_http_holder.app = None


app = FastAPI(
    title="BoteX Cloud Gateway & Remote MCP",
    description="FastAPI service for autonomous code harness, Remote MCP SSE, and interactive chat.",
    version="2.1.0",
    lifespan=lifespan,
)


@app.exception_handler(WorkspaceOutOfBounds)
async def _workspace_out_of_bounds_handler(
    request: Request,  # noqa: ARG001 - FastAPI handler signature
    exc: WorkspaceOutOfBounds,  # noqa: ARG001 - FastAPI handler signature
) -> JSONResponse:
    """Any route resolving an out-of-allowlist workspace_dir -> 403."""
    # Rejected path is request-tainted: static log text only (CodeQL).
    logging.getLogger(__name__).warning(
        "workspace containment: rejected out-of-allowlist workspace_dir"
    )
    return JSONResponse(
        status_code=403,
        content={
            "ok": False,
            "error": "WORKSPACE_OUT_OF_BOUNDS",
            "detail": "workspace_dir is outside cloud.allowed_workspaces",
        },
    )


def _is_auth_configured() -> bool:
    """Return True if either plain token or token hash is configured in environment."""
    return bool(
        os.environ.get("BOTEX_ACCESS_TOKEN_HASH", "").strip()
        or os.environ.get("BOTEX_ACCESS_TOKEN", "").strip()
    )


def _verify_access_token(candidate_token: Optional[str]) -> bool:
    """
    Verify candidate bearer token against configured token or SHA-256 hash.
    Constant-time comparison via hmac.compare_digest.
    Supports BOTEX_ACCESS_TOKEN_HASH as well as sha256:... hashes placed in BOTEX_ACCESS_TOKEN.
    """
    if not candidate_token:
        return False
    candidate_bytes = candidate_token.encode("utf-8")
    raw_hash = hashlib.sha256(candidate_bytes).hexdigest().lower()
    prefixed_hash = f"sha256:{raw_hash}"

    # 1. Direct check against configured hash in BOTEX_ACCESS_TOKEN_HASH
    expected_hash = os.environ.get("BOTEX_ACCESS_TOKEN_HASH", "").strip().strip('"').strip("'")
    if expected_hash:
        target_hash = expected_hash.lower()
        if target_hash.startswith("sha256:"):
            if hmac.compare_digest(prefixed_hash, target_hash):
                return True
        elif hmac.compare_digest(raw_hash, target_hash):
            return True

    # 2. Check BOTEX_ACCESS_TOKEN (auto-detects if sha256:... hash was placed here)
    expected_token = os.environ.get("BOTEX_ACCESS_TOKEN", "").strip().strip('"').strip("'")
    if expected_token:
        target_token = expected_token.lower()
        if target_token.startswith("sha256:"):
            if hmac.compare_digest(prefixed_hash, target_token):
                return True
        elif len(target_token) == 64 and all(c in "0123456789abcdef" for c in target_token):
            if hmac.compare_digest(raw_hash, target_token):
                return True
        # Plain token comparison (skipped when configured value is hash-shaped)
        elif hmac.compare_digest(candidate_bytes, expected_token.encode("utf-8")):
            return True

    return False


def _extract_mcp_origin_from_headers(headers: dict[bytes, bytes]) -> str:
    """Extract and sanitize '<agent>@<host>' provenance from X-Agent-Id / X-Host headers."""
    raw_agent = headers.get(b"x-agent-id", b"").decode("latin1", errors="ignore").strip()
    raw_host = headers.get(b"x-host", b"").decode("latin1", errors="ignore").strip()
    agent = re.sub(r"[^a-zA-Z0-9_.-]", "", raw_agent)[:32]
    host = re.sub(r"[^a-zA-Z0-9_.-]", "", raw_host)[:32]
    if agent and host:
        return f"{agent}@{host}"
    if agent:
        return agent
    if host:
        return f"botex@{host}"
    return ""


# Identity claims that must never be accepted from client-controlled
# headers/payloads: they freeze or impersonate privileged vault provenance.
# Canonical vocabulary lives in botex.tools.security (shared with server.py).
_RESERVED_ORIGIN_AGENTS = RESERVED_ORIGIN_AGENTS


def _normalize_origin_claim(raw: str) -> str:
    """NFKC-normalize and strictly sanitize a provenance agent claim.

    Unicode homoglyphs and full-width lookalikes are folded to their ASCII
    compatibles before the allowlist strip, so ``ｈｕｍａｎ``-style spoofing of
    reserved identity claims cannot survive normalization.
    """
    norm = unicodedata.normalize("NFKC", str(raw or "")).strip()
    return re.sub(r"[^a-zA-Z0-9_.-]", "", norm.split("@", 1)[0])[:32]


def _sanitize_mcp_client_origin(raw: str) -> str:
    """Guard reserved identity claims in client-supplied provenance."""
    if not raw:
        return ""
    return sanitize_origin_claim(raw) or "cloud_api"


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _security_allowed_origins() -> List[str]:
    """``security.allowed_origins`` normalized for exact-match comparison."""
    raw = botex_server.load_config().get("security", {}).get("allowed_origins", [])
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",") if p.strip()]
    return [str(v).strip().rstrip("/").lower() for v in raw if str(v).strip()]


def _is_allowed_origin(origin: str, host: str) -> bool:
    """Same-host/allowlist gate applied only when an Origin header is present.

    Browsers always send Origin on cross-origin mutations and WebSocket
    handshakes; headless clients (curl/agents) omit it. Allowed when the
    Origin exactly matches a ``security.allowed_origins`` entry (``["*"]``
    disables the check) or when the Origin host equals the request Host
    host — the PWA served by this gateway or a same-host dev server on a
    different port. Missing or malformed hosts fail closed.
    """
    allowed = _security_allowed_origins()
    if "*" in allowed or origin.strip().rstrip("/").lower() in allowed:
        return True
    try:
        origin_host = (urlparse(origin.strip()).hostname or "").lower()
        host_host = (urlparse(f"//{host.strip()}").hostname or "").lower()
    except ValueError:
        return False
    return bool(origin_host) and bool(host_host) and origin_host == host_host


class CloudAuthMiddleware:
    """ASGI Authentication middleware supporting Zero Friction Local and Strict Cloud modes.

    - CORS preflight (OPTIONS) requests pass through unhindered.
    - /api/health probe passes through unhindered without authentication.
    - If BOTEX_ACCESS_TOKEN / BOTEX_ACCESS_TOKEN_HASH is not configured or empty: allow local unauthenticated requests with a log note.
    - If configured: strictly verify Bearer token via constant-time comparison against token or SHA-256 hash.
    - Normalizes /mcp/messages path to /mcp/messages/ to prevent 307 Temporary Redirects.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        raw_path = scope.get("path", "")
        path = raw_path.rstrip("/")

        # Health probes and public web PWA assets bypass authentication
        if path == "/api/health" or (not path.startswith("/api") and not path.startswith("/mcp")):
            await self.app(scope, receive, send)
            return

        allowed, retry_after = _GATEWAY_RATE_LIMITER.check_allow(
            _extract_client_ip_from_scope(scope)
        )
        if not allowed:
            resp = JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded. Slow down and retry."},
                headers={"Retry-After": str(max(1, int(retry_after) + 1))},
            )
            await resp(scope, receive, send)
            return

        # Normalize /mcp/messages and /mcp/http to prevent Starlette Mount 307/404 on POST
        if path in ("/mcp/messages", "/mcp/http"):
            normalized = f"{path}/"
            scope["path"] = normalized
            if "raw_path" in scope:
                scope["raw_path"] = normalized.encode("ascii")

        # Reject payloads exceeding 25 MiB to prevent memory exhaustion / OOM on constrained VPS
        headers = dict(scope.get("headers", []))
        # X-Agent-Id/X-Host provenance is honored on MCP and REST API paths
        # alike, but reserved identity claims (human/operator/curator) are
        # rejected so headers can never spoof privileged vault origins.
        mcp_origin = ""
        if path.startswith("/mcp") or path.startswith("/api"):
            mcp_origin = _sanitize_mcp_client_origin(_extract_mcp_origin_from_headers(headers))
        content_length_raw = (
            headers.get(b"content-length", b"").decode("latin1", errors="ignore").strip()
        )
        if content_length_raw.isdigit() and int(content_length_raw) > 25 * 1024 * 1024:
            resp = JSONResponse(
                status_code=getattr(status, "HTTP_413_CONTENT_TOO_LARGE", 413),
                content={"detail": "Payload too large. Maximum request body size is 25 MiB."},
            )
            await resp(scope, receive, send)
            return

        # Browser drive-by guard: a present Origin header means a web page
        # issued this mutation; require same-host or an allowlisted origin.
        if method in _MUTATING_METHODS and path.startswith("/api") and b"origin" in headers:
            origin = headers[b"origin"].decode("latin1", errors="ignore")
            host = headers.get(b"host", b"").decode("latin1", errors="ignore")
            if not _is_allowed_origin(origin, host):
                resp = JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Forbidden: cross-origin request"},
                )
                await resp(scope, receive, send)
                return

        if not _is_auth_configured():
            # Check strict auth env or non-loopback remote origin
            client = scope.get("client")
            client_host = client[0] if client and len(client) > 0 else ""
            forwarded_for = (
                headers.get(b"x-forwarded-for", b"").decode("latin1", errors="ignore").strip()
            )

            # If BOTEX_STRICT_AUTH=1 is set, or request originates from non-loopback host -> Fail-Closed
            strict_auth = os.environ.get("BOTEX_STRICT_AUTH", "").lower() in ("1", "true", "yes")
            is_loopback = bool(
                client_host
                and client_host in ("127.0.0.1", "localhost", "::1", "testclient")
                and not forwarded_for
            )

            if strict_auth or not is_loopback:
                resp = JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "detail": "Forbidden: Remote VPS access requires BOTEX_ACCESS_TOKEN or BOTEX_ACCESS_TOKEN_HASH to be configured."
                    },
                )
                await resp(scope, receive, send)
                return

            logger.info("Zero Friction Local mode: allowing unauthenticated request to %s", path)
            tok = MCP_CLIENT_ORIGIN.set(mcp_origin) if mcp_origin else None
            try:
                await self.app(scope, receive, send)
            finally:
                if tok is not None:
                    MCP_CLIENT_ORIGIN.reset(tok)
            return

        # Extract token from the Authorization header only: query-string
        # credentials are persisted by access/proxy logs, so no route may
        # accept them.
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("latin1", errors="ignore")
        token: Optional[str] = None
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

        # Session-aware MCP authentication: if session was established on authenticated /mcp/sse
        if not token and path.startswith("/mcp/messages") and _mcp_transport is not None:
            qs = parse_qs(scope.get("query_string", b"").decode("latin1", errors="ignore"))
            session_id = qs.get("session_id", [""])[0].strip()
            if session_id:
                active_sessions = {
                    sid.hex for sid in getattr(_mcp_transport, "_read_stream_writers", {}).keys()
                }
                # Sweep bindings of closed streams: dead session ids can never
                # authenticate, so their bindings must not linger.
                for dead_sid in [s for s in _SSE_SESSION_BINDINGS if s not in active_sessions]:
                    _SSE_SESSION_BINDINGS.pop(dead_sid, None)
                if session_id in active_sessions:
                    client_ip = _extract_client_ip_from_scope(scope)
                    binding = _SSE_SESSION_BINDINGS.get(session_id)
                    if binding is None:
                        _bind_sse_session(session_id, client_ip)
                    elif binding["ip"] != client_ip:
                        resp = JSONResponse(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            content={"detail": "Unauthorized: Invalid or missing bearer token"},
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                        await resp(scope, receive, send)
                        return
                    tok = MCP_CLIENT_ORIGIN.set(mcp_origin) if mcp_origin else None
                    try:
                        await self.app(scope, receive, send)
                    finally:
                        if tok is not None:
                            MCP_CLIENT_ORIGIN.reset(tok)
                    return

        # Constant-time token verification (supports plain token and SHA-256 hash)
        is_valid = _verify_access_token(token)

        if not is_valid:
            resp = JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Unauthorized: Invalid or missing bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await resp(scope, receive, send)
            return

        tok = MCP_CLIENT_ORIGIN.set(mcp_origin) if mcp_origin else None
        try:
            await self.app(scope, receive, send)
        finally:
            if tok is not None:
                MCP_CLIENT_ORIGIN.reset(tok)


# ---------------------------------------------------------------------------
# Mount Remote MCP via SSE
# ---------------------------------------------------------------------------


def _mcp_transport_security() -> TransportSecuritySettings:
    """DNS-rebinding guard for the MCP mount.

    Without BOTEX_ACCESS_TOKEN or BOTEX_ACCESS_TOKEN_HASH (Zero Friction Local) network locality is the only barrier, so
    Host/Origin are validated against loopback names. With a token set, Bearer auth is the
    barrier and public hostnames (VPS / reverse proxy) must keep working.
    """
    if (
        os.environ.get("BOTEX_ACCESS_TOKEN", "").strip()
        or os.environ.get("BOTEX_ACCESS_TOKEN_HASH", "").strip()
    ):
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "testserver", "testserver:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )


# Expose the exact 12 existing BoteX MCP tools by wrapping server.mcp directly
_mcp_subapp = botex_server.mcp.sse_app(
    sse_path="/sse",
    message_path="/messages",
    transport_security=_mcp_transport_security(),
)


class _MCPSseErrorGuard:
    """Catch MCP SDK validation errors raised *after* the error response was sent,
    and attach ``X-Accel-Buffering: no`` on ``/mcp/sse`` responses so reverse
    proxies (nginx / Cloudflare) do not buffer SSE frames or drop idle streams.

    ``connect_sse`` emits its error_response and then raises
    ``ValueError("Request validation failed")`` (mcp sse.py:145-148). Without
    this guard the escape surfaces as an unhandled ASGI traceback (500); a
    naive catch-and-respond would double-send, since the response is already
    started at raise time — so we only log and stop.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        is_sse = scope.get("path", "").rstrip("/").endswith("/sse")

        async def _send_with_no_buffer(message: dict[str, Any]) -> None:
            if is_sse and message.get("type") == "http.response.start":
                hdrs = list(message.get("headers") or [])
                if not any(k.lower() == b"x-accel-buffering" for k, _ in hdrs):
                    hdrs.append((b"x-accel-buffering", b"no"))
                message = {**message, "headers": hdrs}
            await send(message)

        try:
            await self.app(scope, receive, _send_with_no_buffer if is_sse else send)
        except ValueError as exc:
            logger.warning("MCP SSE transport rejected request: %s", exc)


_mcp_transport = next(
    (r.app.__self__ for r in _mcp_subapp.routes if hasattr(getattr(r, "app", None), "__self__")),
    None,
)
# StreamableHTTP transport (current MCP spec) at /mcp/http; SSE stays for older clients.
# Bearer auth is enforced per request by the auth middleware (any /mcp/* path).
app.mount("/mcp/http", _mcp_http_holder)
app.mount("/mcp", _MCPSseErrorGuard(_mcp_subapp))


# ---------------------------------------------------------------------------
# Middlewares Configuration
# ---------------------------------------------------------------------------

# CORS middleware: if specific origins are configured, enable allow_credentials=True;
# otherwise default to local dev origins only with allow_credentials=False.
_cfg = botex_server.load_config()
_configured_origins: List[str] = _cfg.get("cloud", {}).get("cors_origins", [])
if _configured_origins:
    _cors_origins = _configured_origins
    _allow_credentials = "*" not in _configured_origins
else:
    # Safe default: local dev origins only (Vite dev server + gateway itself).
    _cors_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
    _allow_credentials = False

# Middleware stack in Starlette is LIFO: CORSMiddleware is outer (runs first),
# CloudAuthMiddleware is inner (runs second).
app.add_middleware(CloudAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# REST Endpoints: Health & Wallet
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def get_health() -> Dict[str, Any]:
    """Health check endpoint for container probes and load balancers (unauthenticated)."""
    now = time.time()
    return {
        "status": "healthy",
        "version": "2.1.0",
        "uptime_s": round(now - START_TIME, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/wallet")
async def get_wallet(
    provider: str = Query(default="openrouter"),
    force_refresh: bool = Query(default=False),
) -> Dict[str, Any]:
    """Unified balance, credit quota, and usage endpoint."""
    info = await default_wallet_service.get_credits_async(
        provider=provider, force_refresh=force_refresh
    )
    return {
        "supported": info.supported,
        "provider": info.provider,
        "balance_usd": info.balance_usd,
        "usage_usd": info.usage_usd,
        "spend_usd": info.spend_usd if info.spend_usd is not None else info.usage_usd,
        "limit_usd": info.limit_usd,
        "limit_remaining_usd": info.limit_remaining_usd,
        "remaining_usd": (
            info.limit_remaining_usd if info.limit_remaining_usd is not None else info.balance_usd
        ),
        "resets_at": info.resets_at,
        "free_model_daily_requests": info.free_model_daily_requests,
        "is_free_tier": info.is_free_tier,
        "label": info.label,
        "error": info.error,
        "units": getattr(info, "units", "usd") or "usd",
        "quota_windows": list(getattr(info, "quota_windows", ())) or [],
    }


@app.get("/api/key/status")
async def get_key_status(
    provider: str = Query(default="openrouter"),
    force_refresh: bool = Query(default=False),
) -> Dict[str, Any]:
    """Detailed live API key status and budget telemetry endpoint."""
    return await default_wallet_service.get_key_status_async(
        provider=provider, force_refresh=force_refresh
    )


@app.get("/api/rate_limits")
async def get_rate_limits() -> Dict[str, Any]:
    """Live provider rate limits, available bucket tokens, and cooldowns."""
    return get_rate_limiter().stats()


@app.get("/api/analytics")
async def get_analytics(
    period: str = Query(default="week"),
    workspace_dir: str = Query(default=""),
) -> Dict[str, Any]:
    """Aggregated run telemetry: totals, per-model stats, daily series, recent runs."""
    days = {"week": 7, "month": 30, "all": None}.get(period, 7)
    ws = resolve_workspace(workspace_dir)
    stats = await asyncio.to_thread(aggregate_stats, load_analytics(), days)
    digest = await asyncio.to_thread(_find_latest_digest, ws)
    return {"period": period, "days": days, **stats, "latest_digest": digest}


@app.post("/api/analytics/digest")
async def trigger_analytics_digest(
    provider: str = Query(default=""),
    model: str = Query(default=""),
    days: int = Query(default=1, ge=1, le=30),
    workspace_dir: str = Query(default=""),
) -> Dict[str, Any]:
    """Manually generate a local-LLM analytics digest and persist it to Memory."""
    ws = resolve_workspace(workspace_dir)
    return await _generate_analytics_digest(ws, provider=provider, model=model, days=days)


@app.get("/api/models")
async def get_models(
    response: Response,
    provider: Optional[str] = Query(default=None),
    force_refresh: bool = Query(default=False),
) -> List[Dict[str, Any]]:
    """Return consolidated model catalog across configured providers (openrouter, openrouter-free, nvidia, ollama, google)."""
    if provider and provider.strip():
        norm_prov, _ = resolve_provider(provider.strip())
        providers_seq = (norm_prov.lower(),)
    else:
        providers_seq = ("openrouter", "openrouter-free", "nvidia", "ollama", "google")
    cfg = botex_server.load_config() if force_refresh else _cfg
    models = await asyncio.to_thread(
        get_consolidated_models,
        providers=providers_seq,
        config=cfg,
        force_refresh=force_refresh,
    )
    # Freshness signal for the PWA: the persistent provider-metadata cache
    # (shared by the model catalog and the pricing guardrail) is the truthful
    # "last successful fetch". Stale means both the idle warmup and the lazy
    # request-time refresh failed — a real egress degradation.
    try:
        fetched_at = CACHE_FILE_DEFAULT.stat().st_mtime if CACHE_FILE_DEFAULT.exists() else 0.0
    except OSError:
        fetched_at = 0.0
    age_s = max(0.0, datetime.now(timezone.utc).timestamp() - fetched_at)
    response.headers["X-Catalog-Fetched-At"] = str(int(fetched_at))
    response.headers["X-Catalog-Age-Seconds"] = str(int(age_s))
    response.headers["X-Catalog-Stale"] = (
        "true" if fetched_at <= 0.0 or age_s > DEFAULT_TTL_HOURS * 3600.0 else "false"
    )
    return models


@app.get("/api/recipes")
async def get_recipes() -> List[Dict[str, Any]]:
    """Return available recipes with metadata."""
    from botex.recipes import list_recipes, load_recipe_metadata

    results = []
    for r in list_recipes():
        meta = load_recipe_metadata(r)
        results.append(
            {
                "name": r,
                "preferred_tier": meta.get("preferred_tier", "standard"),
                "weights": meta.get("weights", {}),
            }
        )
    return results


@app.get("/api/models/recommended")
async def get_recommended_models_endpoint(
    task_type: str = Query(default="coding"),
    limit: int = Query(default=5),
    tier: str = Query(default="auto"),
    recipe: Optional[str] = Query(default=None),
    include_free: bool = Query(default=False),
) -> List[Dict[str, Any]]:
    """Return dynamic model recommendations based on task, tier, recipe and benchmarks."""
    from botex.benchmarks import get_recommended_models

    return await asyncio.to_thread(
        get_recommended_models,
        task_type=task_type,
        limit=limit,
        tier=tier,
        recipe=recipe,
        include_free=include_free,
    )


# ---------------------------------------------------------------------------
# REST Endpoints: Tasks Management
# ---------------------------------------------------------------------------


def _update_task_progress(task_id: str, ev: Dict[str, Any]) -> None:
    """Fold one task event into the entry's live `progress` snapshot."""
    entry = _TASKS.get(task_id)
    if entry is None:
        return
    prog = entry.setdefault(
        "progress",
        {"turn": None, "max_turns": None, "cost_usd": None, "waiting_for": None},
    )
    kind, data = ev.get("kind"), ev.get("data") or {}
    if kind == EventKind.USAGE:
        prog["turn"] = data.get("turn", prog["turn"])
        prog["max_turns"] = data.get("max_turns", prog["max_turns"])
        prog["cost_usd"] = data.get("cost_usd", prog["cost_usd"])
        prog["waiting_for"] = None
    elif kind == EventKind.RETRY_WAIT:
        prog["waiting_for"] = "retry"
    elif kind == EventKind.APPROVAL_WAIT:
        prog["waiting_for"] = "approval"
    elif kind == EventKind.WAITING_FOR_INPUT:
        prog["waiting_for"] = "input"
    elif kind == EventKind.ATTEMPT_START:
        prog["waiting_for"] = "model"
    elif kind in (
        EventKind.ATTEMPT_END,
        EventKind.COMPLETION_PARSE,
        EventKind.TASK_STATE,
        EventKind.TOOL_CALL,
        EventKind.ERROR,
    ):
        prog["waiting_for"] = None


async def _task_event_pump(task_id: str, em) -> None:
    """Fan task events out to WS clients subscribed to this task, and fold
    each into the entry's live progress snapshot."""
    q = em.subscribe()
    try:
        while True:
            ev = await q.get()
            _update_task_progress(task_id, ev)
            for ws, subs in list(_WS_TASK_SUBS.items()):
                if task_id in subs:
                    asyncio.ensure_future(
                        _send_ws_json_safe(
                            ws, {"type": "task_event", "task_id": task_id, "event": ev}
                        )
                    )
    except asyncio.CancelledError:
        pass
    finally:
        em.unsubscribe(q)


_RETRY_HINT_RE = re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE)


def _extract_retry_hint_s(res: Dict[str, Any]) -> Optional[float]:
    """Pull the provider's own retry hint out of a finished task result.

    The ``Retry-After`` header dies inside the retry layer, but providers
    also embed "Please retry in Xs" in the error text that survives into
    ``res["message"]`` / ``attempts[].message``. The hint is clamped to
    300s so a malformed body cannot park the provider for an hour.
    """
    texts = [res.get("message") or ""]
    for att in res.get("attempts") or []:
        if isinstance(att, dict):
            texts.append(str(att.get("message") or att.get("error") or ""))
    best: Optional[float] = None
    for text in texts:
        m = _RETRY_HINT_RE.search(str(text))
        if m:
            val = float(m.group(1))
            best = val if best is None else max(best, val)
    return None if best is None else min(best, 300.0)


def _note_provider_result(provider_name: str, res: Dict[str, Any]) -> None:
    """Feed a finished task's failure telemetry back into the rate limiter.

    A provider_rate_limited outcome opens the provider's cooldown window so
    queued tasks wait out the upstream reset instead of stampeding the same
    limit on the very next admission.
    """
    if not provider_name or not isinstance(res, dict):
        return
    kinds = {res.get("failure_kind")}
    for att in res.get("attempts") or []:
        if isinstance(att, dict):
            kinds.add(att.get("failure_kind"))
    if "provider_rate_limited" in kinds:
        cooldown_s = get_rate_limiter().report_limited(
            provider_name, retry_after_s=_extract_retry_hint_s(res)
        )
        if cooldown_s > 0:
            logger.info(
                "provider '%s' reported rate-limited; %.1fs cooldown applied",
                provider_name,
                cooldown_s,
            )


async def _run_task_worker(task_id: str, req: TaskCreateRequest) -> None:
    """Asynchronous background worker executing run_botex_task under semaphore control."""
    entry = _TASKS[task_id]
    entry["status"] = "QUEUED"
    em = get_emitter(task_id)
    pump = asyncio.create_task(_task_event_pump(task_id, em))
    executor = (getattr(req, "executor", "") or "botex").lower()
    entry["executor"] = executor
    provider_name = ""
    edge_exec: Optional[ToolExecutor] = None
    persist_ws = req.workspace_dir
    try:
        # Provider admission throttle: wait on the provider's RPM budget
        # BEFORE taking a concurrency slot — a task parked on a provider
        # cooldown must not starve other providers' queue slots.
        provider_name, _ = resolve_provider(req.provider or "")
        limiter = get_rate_limiter()
        est_wait = limiter.wait_estimate(provider_name)
        if est_wait > 0:
            em.emit(
                EventKind.RETRY_WAIT,
                delay_s=round(est_wait, 2),
                reason="provider_rate_limit",
                provider=provider_name,
            )
        waited_s = await limiter.acquire(provider_name)
        if waited_s > 0:
            logger.info(
                "task %s waited %.2fs on provider '%s' rate budget",
                task_id,
                waited_s,
                provider_name,
            )
        # Bounded concurrency: enforce engine semaphore
        edge_exec = _resolve_active_edge_executor(req.workspace_dir)
        effective_ws = (
            resolve_workspace("") if edge_exec is not None else resolve_workspace(req.workspace_dir)
        )
        persist_ws = "" if edge_exec is not None else req.workspace_dir
        async with botex_server._TASK_SEMAPHORE:
            entry["status"] = "RUNNING"
            em.emit(EventKind.TASK_STATE, state="RUNNING", executor=executor)
            res = await run_botex_task(
                task=req.task,
                files=req.files or None,
                workspace_dir=effective_ws,
                model=req.model,
                profile=req.profile,
                provider=req.provider,
                mode=req.mode,
                max_turns=req.max_turns or None,
                max_tokens=req.max_tokens or None,
                max_duration_s=req.max_duration_s or None,
                budget_limit_usd=req.budget_limit_usd if req.budget_limit_usd >= 0 else None,
                allow_destructive=True if req.allow_destructive else None,
                allow_exec=True if req.allow_exec else None,
                allow_net=True if req.allow_net else None,
                net_allowed_hosts=req.net_allowed_hosts or None,
                net_allowed_urls=req.net_allowed_urls or None,
                output_path=req.output_path,
                verify_command=req.verify_command,
                recipe=req.recipe,
                allow_non_zdr=True if req.allow_non_zdr else None,
                allow_expensive_model=True if req.allow_expensive_model else None,
                approval_policy=req.approval_policy or None,
                progress_cb=lambda kind, **data: em.emit(kind, **data),
                tool_executor=edge_exec,
            )
            entry["result_data"] = botex_server.mask_secrets_recursive(res)
            entry["result"] = mask_secrets(res.get("summary") or res.get("message") or "")
            entry["status"] = "DONE" if res.get("ok") else res.get("status", "FAILED")
            _note_provider_result(provider_name, res)
    except asyncio.CancelledError:
        entry["status"] = "CANCELLED"
        if not entry.get("result"):
            entry["result"] = "Task cancelled by operator."
        if not entry.get("result_data"):
            entry["result_data"] = {
                "ok": False,
                "status": "CANCELLED",
                "message": "Task cancelled by operator.",
            }
        raise
    except Exception as exc:
        masked_err = mask_secrets(str(exc))
        entry["status"] = "FAILED"
        entry["result"] = f"Task execution failed: {masked_err}"
        entry["result_data"] = {"ok": False, "status": "ERROR", "error": masked_err}
    finally:
        if isinstance(edge_exec, RemoteWssToolExecutor):
            edge_exec.clear_task_cache(task_id)
        entry["finished_at"] = time.time()
        em.emit(EventKind.TASK_STATE, state=entry.get("status") or "DONE")
        await asyncio.to_thread(_save_task_persistent, task_id, entry, persist_ws)
        # Let the pump deliver the final task_state frame before teardown.
        await asyncio.sleep(0.1)
        pump.cancel()


@app.post("/api/tasks", status_code=status.HTTP_202_ACCEPTED)
async def create_task(
    req: TaskCreateRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> Dict[str, Any]:
    """Delegate autonomous code editing task in the background with WAN idempotency protection."""
    botex_server._cleanup_tasks()
    _cleanup_idempotency_cache()

    if idempotency_key:
        clean_key = sanitize_identifier(idempotency_key)
        if clean_key and clean_key in _IDEMPOTENCY_CACHE:
            cached = _IDEMPOTENCY_CACHE[clean_key]
            existing_tid = cached.get("task_id")
            if existing_tid and existing_tid in _TASKS:
                existing_entry = _TASKS[existing_tid]
                return {
                    "task_id": existing_tid,
                    "status": existing_entry.get("status", "QUEUED"),
                    "idempotent_replay": True,
                }

    # Task queue bounding
    max_queued = int(botex_server.load_config().get("engine", {}).get("max_queued_tasks", 32))
    active_count = sum(entry.get("status") in {"QUEUED", "RUNNING"} for entry in _TASKS.values())
    if active_count >= max_queued:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Task queue limit reached ({max_queued}).",
        )
    if req.task and len(req.task) > 65536:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Task prompt exceeds maximum allowed length (65536 characters).",
        )
    has_model = bool(req.model and req.model.strip())
    has_provider = bool(req.provider and req.provider.strip())
    env_prov = os.environ.get("BOTEX_PROVIDER", "").strip()
    if has_model and not has_provider and not env_prov:
        avail_provs = ", ".join((botex_server.load_config().get("providers") or {}).keys())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Explicit 'provider' is required when 'model' ('{req.model.strip()}') is specified. "
                f"Specify one of: {avail_provs}."
            ),
        )
    if has_provider:
        resolved_prov, prov_cfg = resolve_provider(req.provider)
        if not prov_cfg:
            avail_provs = ", ".join((botex_server.load_config().get("providers") or {}).keys())
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown provider '{req.provider}'. Available providers: {avail_provs}.",
            )
        req.provider = resolved_prov
    if req.max_turns and req.max_turns > 100:
        req.max_turns = 100
    if req.max_tokens and req.max_tokens > 32768:
        req.max_tokens = 32768
    if req.max_duration_s and req.max_duration_s > 7200:
        req.max_duration_s = 7200
    if req.budget_limit_usd > 25.0:
        req.budget_limit_usd = 25.0
    if req.files and len(req.files) > 200:
        req.files = req.files[:200]

    task_id = uuid.uuid4().hex[:12]
    entry = {
        "task_id": task_id,
        "task": mask_secrets(req.task),  # ZDR: scrub sensitive tokens
        # Original request params (masked) — enables retry/clone from the PWA.
        "request": botex_server.mask_secrets_recursive(req.model_dump(exclude_none=True)),
        "status": "QUEUED",
        "created_at": time.time(),
        "finished_at": None,
        "result": None,
        "result_data": None,
        "async_task": None,
    }
    _TASKS[task_id] = entry
    persist_ws = (
        "" if _resolve_active_edge_executor(req.workspace_dir) is not None else req.workspace_dir
    )
    await asyncio.to_thread(_save_task_persistent, task_id, entry, persist_ws)
    worker = asyncio.create_task(_run_task_worker(task_id, req))
    entry["async_task"] = worker
    res_dict = {"task_id": task_id, "status": "QUEUED"}
    if idempotency_key:
        clean_key = sanitize_identifier(idempotency_key)
        if clean_key:
            _IDEMPOTENCY_CACHE[clean_key] = {
                "task_id": task_id,
                "created_at": time.time(),
                "response": res_dict,
            }
    return res_dict


@app.get("/api/tasks")
async def list_tasks() -> Dict[str, Any]:
    """List all registered background tasks."""
    _load_tasks_persistent()
    tasks = []
    for tid, entry in _TASKS.items():
        tasks.append(
            {
                "task_id": tid,
                "status": entry.get("status"),
                "created_at": entry.get("created_at"),
                "finished_at": entry.get("finished_at"),
            }
        )
    return {"tasks": tasks, "count": len(tasks)}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> Dict[str, Any]:
    """Get status and structured result of a background task."""
    clean_tid = sanitize_identifier(task_id)
    if not clean_tid or clean_tid not in _TASKS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    entry = _TASKS[clean_tid]
    return {
        "task_id": clean_tid,
        "status": entry.get("status"),
        "created_at": entry.get("created_at"),
        "finished_at": entry.get("finished_at"),
        "result": entry.get("result"),
        "result_data": entry.get("result_data"),
        "task": entry.get("task"),
        "request": entry.get("request"),
        "executor": entry.get("executor"),
        "progress": entry.get("progress"),
        "session_id": entry.get("session_id") or (entry.get("result_data") or {}).get("session_id"),
        "turns": entry.get("turns") or [],
    }


@app.get("/api/tasks/{task_id}/events")
async def get_task_events(task_id: str, since: int = Query(default=0, ge=0)) -> Dict[str, Any]:
    """Gap-aware tail of the task's event buffer (WS is the push path)."""
    clean_tid = sanitize_identifier(task_id)
    if not clean_tid or clean_tid not in _TASKS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    em = emitter_or_none(clean_tid)
    if em is None:
        return {"events": [], "min_seq": 0, "dropped": 0}
    events, min_seq, dropped = em.events_since(int(since))
    return {"events": events, "min_seq": min_seq, "dropped": dropped}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> Dict[str, Any]:
    """Abort a running background task and cancel any pending operator approvals."""
    clean_tid = sanitize_identifier(task_id)
    if not clean_tid or clean_tid not in _TASKS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    entry = _TASKS[clean_tid]

    current_status = entry.get("status")
    if current_status in {"DONE", "FAILED", "CANCELLED"}:
        return {
            "ok": False,
            "task_id": clean_tid,
            "status": current_status,
            "message": f"Task already in terminal state '{current_status}'.",
        }

    # Abort pending approval actions for this task
    default_approval_manager.abort_task(clean_tid, reason="Task cancelled via API")

    # Cancel background worker task if active
    worker = entry.get("async_task")
    if worker and not worker.done():
        worker.cancel()

    req_data = entry.get("request", {})
    prov = (req_data.get("provider") or "").lower()
    mod = req_data.get("model") or ""
    if prov == "ollama" or "bielik" in mod.lower() or "ollama" in mod.lower():
        _unload_ollama_model_sync(mod)

    entry["status"] = "CANCELLED"
    entry["result"] = "Task cancelled by operator."
    entry["result_data"] = {
        "ok": False,
        "status": "CANCELLED",
        "message": "Task cancelled by operator.",
    }
    entry["finished_at"] = time.time()
    return {"ok": True, "task_id": clean_tid, "status": "CANCELLED"}


# ---------------------------------------------------------------------------
# REST Endpoints: Operator Approvals
# ---------------------------------------------------------------------------


@app.get("/api/approvals")
async def list_approvals(task_id: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """List pending actions awaiting human operator decision."""
    clean_tid = sanitize_identifier(task_id) if task_id else None
    actions = default_approval_manager.get_pending_actions(task_id=clean_tid)
    masked_actions = [botex_server.mask_secrets_recursive(a) for a in actions]
    return {"pending_actions": masked_actions, "count": len(masked_actions)}


@app.post("/api/approvals/{action_id}/decide")
async def decide_approval(action_id: str, body: ApprovalDecisionRequest) -> Dict[str, Any]:
    """Submit operator decision for a pending action."""
    clean_aid = sanitize_identifier(action_id)
    if not clean_aid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid action_id format.",
        )
    resolved = default_approval_manager.resolve(
        action_id=clean_aid,
        approved=body.approved,
        reason=body.reason,
        remember=body.remember,
    )
    if not resolved:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Action not found or already resolved",
        )
    return {
        "ok": True,
        "action_id": clean_aid,
        "approved": body.approved,
        "reason": mask_secrets(body.reason),
    }


# ---------------------------------------------------------------------------
# REST Endpoints: Memory Vault
# ---------------------------------------------------------------------------


@app.get("/api/memory/projects")
async def get_memory_projects_endpoint(
    scope: str = Query(default="all"),
    workspace_dir: str = Query(default="."),
) -> Dict[str, Any]:
    """List distinct projects and note counts across the requested memory scopes."""
    ws = resolve_workspace(workspace_dir)
    res = await asyncio.to_thread(
        list_memory_projects,
        workspace_dir=ws,
        scope=scope,
    )
    if not res.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list memory projects.",
        )
    return botex_server.mask_secrets_recursive(res)


@app.get("/api/memory")
async def get_memory(
    query: str = Query(default=""),
    tag: str = Query(default=""),
    kind: str = Query(default=""),
    scope: str = Query(default="local"),
    project: str = Query(default=""),
    workspace_dir: str = Query(default="."),
    memory_id: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Search or read entries from the Memory Vault across local, global, or all scopes."""
    ws = resolve_workspace(workspace_dir)
    if memory_id:
        clean_mid = sanitize_identifier(memory_id)
        if not clean_mid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid memory_id format.",
            )
        res = await asyncio.to_thread(
            read_memory, workspace_dir=ws, memory_id=clean_mid, scope=scope
        )
        if not res.get("ok"):
            logger.warning("Memory read failed: entry not found or inaccessible.")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Memory entry '{clean_mid}' not found.",
            )
        return botex_server.mask_secrets_recursive(res)

    res = await asyncio.to_thread(
        search_memory,
        workspace_dir=ws,
        query=query,
        tag=tag,
        kind=kind,
        scope=scope,
        project=project or None,
    )
    if not res.get("ok"):
        logger.warning("Memory search failed.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to search memory entries.",
        )
    return botex_server.mask_secrets_recursive(res)


@app.post("/api/memory")
async def post_memory(body: MemorySaveRequest) -> Dict[str, Any]:
    """Save an entry to the Memory Vault.

    Provenance is taken strictly from server-side context (X-Agent-Id /
    X-Host headers validated by CloudAuthMiddleware); the client-supplied
    ``body.origin`` is ignored — an unauthenticated claim like ``human``
    must never freeze or impersonate privileged vault origins.
    """
    ws = resolve_workspace(body.workspace_dir)
    clean_mid = sanitize_identifier(body.memory_id) if body.memory_id else None
    if body.memory_id and not clean_mid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid memory_id format.",
        )
    origin = _sanitize_mcp_client_origin(MCP_CLIENT_ORIGIN.get("")) or "cloud_api"
    if _normalize_origin_claim(body.origin).lower() in _RESERVED_ORIGIN_AGENTS:
        # Static message only (CWE-117): never echo untrusted origin text into logs.
        logger.warning(
            "Rejected reserved origin claim on POST /api/memory; using server provenance."
        )
    res = await asyncio.to_thread(
        save_memory,
        workspace_dir=ws,
        title=body.title,
        content=body.content,
        kind=body.kind,
        tags=filter_reserved_tags(body.tags),
        memory_id=clean_mid,
        origin=origin,
        scope=body.scope,
        project=body.project,
    )
    if not res.get("ok"):
        logger.warning("Memory save failed.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to save memory entry.",
        )
    return botex_server.mask_secrets_recursive(res)


@app.delete("/api/memory/{memory_id}")
async def delete_memory_endpoint(
    memory_id: str,
    scope: str = Query(default="local"),
    workspace_dir: str = Query(default="."),
) -> Dict[str, Any]:
    """Delete a memory entry by ID from local or global Memory Vault."""
    clean_mid = sanitize_identifier(memory_id)
    if not clean_mid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid memory_id format.",
        )
    ws = resolve_workspace(workspace_dir)
    res = await asyncio.to_thread(delete_memory, workspace_dir=ws, memory_id=clean_mid, scope=scope)
    if not res.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory entry '{clean_mid}' not found.",
        )
    return botex_server.mask_secrets_recursive(res)


# ---------------------------------------------------------------------------
# REST Endpoints: Server-Side Skills Catalog (Skill Vault)
# ---------------------------------------------------------------------------


@app.get("/api/skills")
async def get_skills_catalog() -> Dict[str, Any]:
    """List all installed server-side skills in the BoteX Skill Vault."""
    res = await asyncio.to_thread(list_skills)
    return botex_server.mask_secrets_recursive(res)


@app.get("/api/skills/{skill_name}")
async def get_skill_detail(skill_name: str) -> Dict[str, Any]:
    """Retrieve full metadata and SKILL.md content for an installed skill."""
    clean_name = sanitize_identifier(skill_name)
    if not clean_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid skill name format.",
        )
    res = await asyncio.to_thread(get_skill, clean_name)
    if not res.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill '{clean_name}' not found.",
        )
    return botex_server.mask_secrets_recursive(res)


@app.post("/api/skills")
async def post_install_skill(body: SkillInstallRequest) -> Dict[str, Any]:
    """Install or update a server-side skill from raw Markdown or a public URL/repo."""
    res = await install_skill_async(
        name=body.name,
        content=body.content,
        source_url=body.source_url,
        origin_repo=body.origin_repo,
        version=body.version,
        commit_sha=body.commit_sha,
        description=body.description,
        taxonomy=body.taxonomy,
    )
    if not res.get("ok"):
        audit_status = res.get("audit_status")
        if audit_status == "REJECT":
            masked_detail = botex_server.mask_secrets_recursive(
                {"error": res.get("error"), "audit": res.get("audit")}
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=masked_detail,
            )
        if audit_status == "SCANNER_ERROR":
            masked_detail = botex_server.mask_secrets_recursive(
                {
                    "error": res.get("error")
                    or "Skill security scanner failed or timed out. Installation aborted."
                }
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=masked_detail,
            )
        err_msg = coerce_text(mask_secrets(str(res.get("error") or "Failed to install skill.")))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=err_msg,
        )
    return botex_server.mask_secrets_recursive(res)


@app.delete("/api/skills/{skill_name}")
async def delete_skill_endpoint(skill_name: str) -> Dict[str, Any]:
    """Delete an installed skill from the server-side Skill Vault."""
    clean_name = sanitize_identifier(skill_name)
    if not clean_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid skill name format.",
        )
    res = await asyncio.to_thread(delete_skill, clean_name)
    if not res.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill '{clean_name}' not found.",
        )
    return botex_server.mask_secrets_recursive(res)


def _fetch_memory_graph_sync(ws: Path, scope: str) -> Dict[str, Any]:
    health = find_graph_health(workspace_dir=ws, scope=scope)

    search_dirs = _resolve_scope_dirs(ws, scope)
    nodes_map: Dict[str, Dict[str, Any]] = {}
    edges_list: List[Dict[str, Any]] = []

    for mem_dir, sc in search_dirs:
        if not mem_dir.exists() or not mem_dir.is_dir():
            continue
        conn = _get_index_db(mem_dir)
        try:
            m_rows = conn.execute(
                "SELECT id, title, kind, tags, status, compiled_truth, timestamp, project FROM memories_meta;"
            ).fetchall()
            for r in m_rows:
                mid = r["id"]
                if mid not in nodes_map:
                    raw_tags = r["tags"].split() if r["tags"] else []
                    nodes_map[mid] = {
                        "id": mid,
                        "title": r["title"] or mid,
                        "kind": r["kind"] or "context",
                        "tags": raw_tags,
                        "status": r["status"] or "active",
                        "compiled_truth": bool(r["compiled_truth"]),
                        "timestamp": r["timestamp"] or "",
                        "project": r["project"] or "",
                        "scope": sc,
                        "in_degree": 0,
                        "out_degree": 0,
                    }

            e_rows = conn.execute(
                "SELECT source_id, target_id, kind FROM memories_edges;"
            ).fetchall()
            for er in e_rows:
                edges_list.append(
                    {
                        "source": er["source_id"],
                        "target": er["target_id"],
                        "kind": er["kind"] or "wikilink",
                    }
                )
        finally:
            conn.close()

    for e in edges_list:
        s, t = e["source"], e["target"]
        if s in nodes_map:
            nodes_map[s]["out_degree"] += 1
        if t in nodes_map:
            nodes_map[t]["in_degree"] += 1

    return {
        "ok": True,
        "nodes": list(nodes_map.values()),
        "edges": edges_list,
        "health": health,
    }


@app.get("/api/memory/graph")
async def get_memory_graph(
    scope: str = Query(default="all"),
    workspace_dir: str = Query(default="."),
) -> Dict[str, Any]:
    """Retrieve full knowledge graph nodes, wikilink edges, and health integrity report."""
    ws = resolve_workspace(workspace_dir)
    res = await asyncio.to_thread(_fetch_memory_graph_sync, ws, scope)
    return botex_server.mask_secrets_recursive(res)


@app.get("/api/memory/backlinks")
async def get_memory_backlinks_endpoint(
    memory_id: str = Query(...),
    scope: str = Query(default="all"),
    workspace_dir: str = Query(default="."),
) -> Dict[str, Any]:
    """Retrieve all memory notes referencing the specified memory_id via [[wikilinks]]."""
    clean_mid = sanitize_identifier(memory_id)
    if not clean_mid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid memory_id format.",
        )
    ws = resolve_workspace(workspace_dir)
    res = await asyncio.to_thread(get_backlinks, workspace_dir=ws, memory_id=clean_mid, scope=scope)
    return botex_server.mask_secrets_recursive(res)


# ---------------------------------------------------------------------------
# Knowledge Graph Curator Endpoints (Option A, Zero Recipe Pollution)
# ---------------------------------------------------------------------------


@app.get("/api/curator/status")
async def get_curator_status_endpoint(
    workspace_dir: str = Query(default="."),
) -> Dict[str, Any]:
    """Retrieve the latest knowledge graph curator audit status and beacon health."""
    from botex.curator import get_curator_status

    ws = resolve_workspace(workspace_dir)
    res = get_curator_status(workspace_dir=ws)
    return botex_server.mask_secrets_recursive(res)


@app.post("/api/curator/run")
async def trigger_curator_run_endpoint(
    req: Optional[CuratorRunRequest] = None,
) -> Dict[str, Any]:
    """Trigger an autonomous knowledge graph curation cycle with multi-tier LLM fallback."""
    from botex.curator import run_curation_async

    body = req or CuratorRunRequest()
    ws = resolve_workspace(body.workspace_dir)
    res = await run_curation_async(
        workspace_dir=ws,
        scope=body.scope,
        force=body.force,
        model_override=body.model_override,
        notify_telegram=body.notify_telegram,
        max_batches=1,
    )
    return botex_server.mask_secrets_recursive(res)


# ---------------------------------------------------------------------------
# Chat Session Management & Memory Export
# ---------------------------------------------------------------------------


@app.post("/api/chat/abort")
async def abort_chat(
    session_id: str = Query(default=""),
    model: str = Query(default=""),
    provider: str = Query(default=""),
) -> Dict[str, Any]:
    """Abort in-flight chat generation and release Ollama VRAM if active."""
    # 1. Cancel in-flight asyncio task immediately to sever HTTP socket to Ollama
    active_task = _active_chat_tasks.get(session_id)
    if active_task and not active_task.done():
        active_task.cancel()

    # 2. If local provider, unload model from VRAM immediately
    prov_norm = provider.lower()
    if prov_norm == "ollama" or any(
        k in model.lower() for k in ("bielik", "nemo", "hermes", "qwen", "llama")
    ):
        _unload_ollama_model_sync(model)
    return {"ok": True, "session_id": session_id, "status": "ABORTED"}


@app.get("/api/chat/sessions")
async def list_chat_sessions(workspace_dir: str = Query("")) -> Dict[str, Any]:
    """List historical multi-turn chat sessions stored in memory and on disk."""
    ws = resolve_workspace(workspace_dir)
    sessions = default_chat_manager.list_sessions(workspace_dir=ws)
    return {"sessions": sessions, "count": len(sessions)}


@app.get("/api/chat/sessions/{session_id}")
async def get_chat_session(session_id: str, workspace_dir: str = Query("")) -> Dict[str, Any]:
    """Retrieve full transcript and metadata for a specific chat session."""
    clean_sid = sanitize_identifier(session_id)
    if not clean_sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid session_id format.",
        )
    ws = resolve_workspace(workspace_dir)
    sess = default_chat_manager.get_session(clean_sid, workspace_dir=ws)
    if not sess:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Chat session '{clean_sid}' not found.",
        )
    return sess.to_dict()


@app.delete("/api/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str, workspace_dir: str = Query("")) -> Dict[str, Any]:
    """Delete a chat session from memory and disk."""
    clean_sid = sanitize_identifier(session_id)
    if not clean_sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid session_id format.",
        )
    ws = resolve_workspace(workspace_dir)
    ok = default_chat_manager.delete_session(clean_sid, workspace_dir=ws)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Chat session '{clean_sid}' not found.",
        )
    return {"ok": True, "session_id": clean_sid}


@app.post("/api/chat/sessions/{session_id}/save_to_memory")
async def save_chat_session_to_memory(
    session_id: str, body: SaveChatToMemoryRequest
) -> Dict[str, Any]:
    """Export and store conversation transcript or summary into Workspace Memory Vault (.botex/memory/)."""
    clean_sid = sanitize_identifier(session_id)
    if not clean_sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid session_id format.",
        )
    ws = resolve_workspace(body.workspace_dir)
    res = default_chat_manager.save_chat_to_memory(
        session_id=clean_sid,
        workspace_dir=ws,
        title=body.title,
        kind=body.kind,
        scope=body.scope,
        content=body.content,
        tags=filter_reserved_tags(body.tags),
    )
    if not res.get("ok"):
        logger.warning("Save chat to memory failed.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to save chat transcript to memory vault.",
        )
    return botex_server.mask_secrets_recursive(res)


# ---------------------------------------------------------------------------
# Streaming Chat Endpoints: SSE & WebSocket
# ---------------------------------------------------------------------------


@app.post("/api/chat")
async def post_chat(
    body: ChatRequest,
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    """Stream conversational chat turns using Server-Sent Events with WAN replay and keepalives."""
    clean_sid = sanitize_identifier(body.session_id) if body.session_id else None
    if body.session_id and not clean_sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid session_id format.",
        )
    if body.message and len(body.message) > 32768:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Chat message exceeds maximum allowed length (32768 characters).",
        )
    if body.system_prompt and len(body.system_prompt) > 32768:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="System prompt exceeds maximum allowed length (32768 characters).",
        )
    session = default_chat_manager.get_or_create_session(
        session_id=clean_sid,
        workspace_dir=body.workspace_dir,
        model=body.model,
        provider=body.provider,
        profile=body.profile,
        system_prompt=body.system_prompt,
    )

    async def event_generator():
        curr_t = asyncio.current_task()
        if curr_t and session.session_id:
            _active_chat_tasks[session.session_id] = curr_t
        try:
            async with session.lock:
                # 1. Replay missed events if client reconnected with Last-Event-ID
                if last_event_id:
                    missed = _get_sse_replay_events(session.session_id, last_event_id)
                    for ev in missed:
                        yield ServerSentEvent(
                            data=json.dumps(ev["data"]),
                            event=ev["event"],
                            id=ev["id"],
                        )

                # 2. Stream new turn chunks
                seq_num = 0
                carry = ""
                async for chunk in stream_chat_turn(session, user_input=body.message):
                    seq_num += 1
                    ev_id = f"{session.session_id}_{int(time.time() * 1000)}_{seq_num}"
                    masked_chunk, carry = botex_server.mask_stream_chunk(chunk, carry)
                    ev_type = masked_chunk.get("type", "message")
                    _record_sse_event(session.session_id, ev_id, ev_type, masked_chunk)
                    yield ServerSentEvent(
                        data=json.dumps(masked_chunk),
                        event=ev_type,
                        id=ev_id,
                    )
        except asyncio.CancelledError:
            # Client aborted or explicitly requested abort
            pass
        finally:
            if session.session_id:
                _active_chat_tasks.pop(session.session_id, None)

    return EventSourceResponse(
        event_generator(),
        ping=15,
        ping_message_factory=lambda: ServerSentEvent(comment="keepalive"),
    )


@app.websocket("/api/chat/ws")
async def chat_websocket(websocket: WebSocket) -> None:
    """Bidirectional WebSocket chat endpoint with 15s ping heartbeat and auth support."""
    await websocket.accept()

    ws_origin = websocket.headers.get("origin", "")
    if ws_origin.strip() and not _is_allowed_origin(ws_origin, websocket.headers.get("host", "")):
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Forbidden: cross-origin request",
        )
        return

    if not _is_auth_configured():
        client_host = websocket.client.host if websocket.client else ""
        forwarded_for = websocket.headers.get("x-forwarded-for", "").strip()
        strict_auth = os.environ.get("BOTEX_STRICT_AUTH", "").lower() in ("1", "true", "yes")
        is_loopback = bool(
            client_host
            and client_host in ("127.0.0.1", "localhost", "::1", "testclient")
            and not forwarded_for
        )
        if strict_auth or not is_loopback:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Forbidden: Remote access requires BOTEX_ACCESS_TOKEN or BOTEX_ACCESS_TOKEN_HASH",
            )
            return
    else:
        # Bearer header or first auth frame only: query-string credentials are
        # persisted by access/proxy logs, so no route may accept them.
        token = ""
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

        authenticated = _verify_access_token(token)
        if not authenticated:
            # Allow auth via first message: {"type": "auth", "token": "..."}
            try:
                auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
                if (
                    isinstance(auth_msg, dict)
                    and auth_msg.get("type") == "auth"
                    and _verify_access_token(str(auth_msg.get("token", "")))
                ):
                    authenticated = True
                    await websocket.send_json({"type": "auth_ok"})
            except Exception:
                authenticated = False

        if not authenticated:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized")
            return

    ws_lock = asyncio.Lock()
    _ACTIVE_WEBSOCKETS[websocket] = ws_lock

    # Periodic 15s ping heartbeat task to keep connections alive through proxies
    async def heartbeat_loop() -> None:
        try:
            while True:
                await asyncio.sleep(15)
                await _send_ws_json_safe(websocket, {"type": "ping", "timestamp": time.time()})
        except (asyncio.CancelledError, Exception):
            pass

    heartbeat_task = asyncio.create_task(heartbeat_loop())

    try:
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue
            msg_type = data.get("type")
            if msg_type == "pong":
                continue
            elif msg_type == "ping":
                await _send_ws_json_safe(websocket, {"type": "pong", "timestamp": time.time()})
                continue
            elif msg_type == "task_subscribe":
                tid = sanitize_identifier(str(data.get("task_id") or ""))
                if tid:
                    _WS_TASK_SUBS.setdefault(websocket, set()).add(tid)
                continue
            elif msg_type == "task_unsubscribe":
                subs = _WS_TASK_SUBS.get(websocket)
                if subs:
                    subs.discard(str(data.get("task_id") or ""))
                continue
            elif msg_type == "abort" or msg_type == "cancel":
                abort_mod = data.get("model", "")
                abort_prov = (data.get("provider") or "").lower()
                if abort_prov == "ollama" or "bielik" in abort_mod.lower():
                    _unload_ollama_model_sync(abort_mod)
                await _send_ws_json_safe(
                    websocket, {"type": "done", "finish_reason": "aborted", "content": ""}
                )
                continue
            elif msg_type == "message" or "message" in data or "content" in data:
                content = data.get("content") or data.get("message") or ""
                if len(str(content)) > 32768:
                    await _send_ws_json_safe(
                        websocket,
                        {
                            "type": "error",
                            "error": "Chat message exceeds maximum allowed length (32768 characters).",
                        },
                    )
                    continue
                sys_prompt = data.get("system_prompt")
                if sys_prompt and len(str(sys_prompt)) > 32768:
                    await _send_ws_json_safe(
                        websocket,
                        {
                            "type": "error",
                            "error": "System prompt exceeds maximum allowed length (32768 characters).",
                        },
                    )
                    continue
                session = default_chat_manager.get_or_create_session(
                    session_id=data.get("session_id"),
                    workspace_dir=data.get("workspace_dir", ""),
                    model=data.get("model", ""),
                    provider=data.get("provider", ""),
                    profile=data.get("profile", ""),
                    system_prompt=data.get("system_prompt"),
                )
                async with session.lock:
                    carry = ""
                    async for chunk in stream_chat_turn(session, user_input=content):
                        masked_chunk, carry = botex_server.mask_stream_chunk(chunk, carry)
                        await _send_ws_json_safe(websocket, masked_chunk)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WebSocket session terminated: %s", type(exc).__name__)
    finally:
        heartbeat_task.cancel()
        _ACTIVE_WEBSOCKETS.pop(websocket, None)
        _WS_TASK_SUBS.pop(websocket, None)


# ---------------------------------------------------------------------------
# REST Endpoints: Creator Mode Plugin Vault & Vision Inspection
# ---------------------------------------------------------------------------


@app.get("/api/plugins")
async def list_plugins_endpoint(
    workspace_dir: str = Query(default="."),
) -> Dict[str, Any]:
    """List Creator Mode plugins across local/global vaults and the staging area."""
    ws = resolve_workspace(workspace_dir)
    vault = PluginVaultManager(Path(ws))
    local_plugins = [p.to_dict() for p in vault.load_vault_plugins(scope="local")]
    global_plugins = [p.to_dict() for p in vault.load_vault_plugins(scope="global")]
    staged_plugins = [p.to_dict() for p in vault.list_staged_plugins()]
    return botex_server.mask_secrets_recursive(
        {
            "ok": True,
            "plugins": {
                "local": local_plugins,
                "global": global_plugins,
                "staging": staged_plugins,
            },
            "index_markdown": {
                "local": vault.get_index_markdown("local"),
                "global": vault.get_index_markdown("global"),
            },
        }
    )


@app.post("/api/plugins/resolve")
async def resolve_plugin_endpoint(body: PluginResolveRequest) -> Dict[str, Any]:
    """Promote a staged plugin to the local/global vault or discard it."""
    clean_name = sanitize_identifier(body.name)
    if not clean_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid plugin name format.",
        )
    action = (body.action or "").strip().lower()
    if action not in ("local", "global", "discard"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid action — expected 'local', 'global' or 'discard'.",
        )
    ws = resolve_workspace(body.workspace_dir)
    res = PluginVaultManager(Path(ws)).resolve_staged_plugin(clean_name, action)
    if not res.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=coerce_text(mask_secrets(str(res.get("error") or "Resolve failed."))),
        )
    return botex_server.mask_secrets_recursive(res)


@app.post("/api/vision/inspect")
async def vision_inspect_endpoint(body: VisionInspectRequest) -> Dict[str, Any]:
    """Triage a visual defect screenshot through the vision pipeline."""
    try:
        from botex.vision import triage_visual_defect
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Vision pipeline is not available in this build.",
        )
    import base64
    import binascii
    import inspect as _inspect

    try:
        image_bytes = base64.b64decode(body.image_base64 or "", validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="image_base64 is not valid base64 data.",
        )
    if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Decoded image payload must be 1 byte to 20 MiB.",
        )
    ws = resolve_workspace(body.workspace_dir)
    res = triage_visual_defect(
        image_bytes=image_bytes,
        prompt_hint=body.prompt_hint,
        reference_hash=body.reference_hash,
        workspace_dir=Path(ws),
    )
    if _inspect.isawaitable(res):
        res = await res
    if isinstance(res, dict):
        clean_tid = sanitize_identifier(body.task_id) if body.task_id else ""
        if clean_tid:
            res.setdefault("task_id", clean_tid)
        return botex_server.mask_secrets_recursive(res)
    return {"ok": True, "result": coerce_text(mask_secrets(str(res)))}


# ---------------------------------------------------------------------------
# Edge Bridge: Device Tokens & Outbound Compressed WSS Tunnel
# ---------------------------------------------------------------------------


@app.post("/api/edge/tokens", status_code=status.HTTP_201_CREATED)
async def issue_edge_token(body: EdgeTokenIssueRequest) -> Dict[str, Any]:
    """Issue a scoped per-device bearer token; only the SHA-256 hash is persisted."""
    store = _edge_token_store()
    try:
        ttl_val = body.ttl_s if body.ttl_s is not None else 30 * 24 * 3600
        raw_credential, rec = store.issue_token(
            body.device_id,
            body.workspace_id,
            scopes=body.scopes,
            ttl_s=ttl_val,
            label=body.label,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=coerce_text(mask_secrets(str(exc))),
        )
    return {
        "ok": True,
        "token": raw_credential,
        "record": rec.to_public_dict(),
    }


@app.get("/api/edge/tokens")
async def list_edge_tokens(include_revoked: bool = Query(default=True)) -> Dict[str, Any]:
    """List per-device tokens without exposing hashes or secrets."""
    store = _edge_token_store()
    return {"ok": True, "tokens": store.list_tokens(include_revoked=include_revoked)}


@app.delete("/api/edge/tokens/{jti}")
async def revoke_edge_token(jti: str) -> Dict[str, Any]:
    """Revoke a device token by JTI and disconnect matching active WSS edge sessions."""
    clean_jti = sanitize_identifier(jti)
    if not clean_jti:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token jti format.",
        )
    store = _edge_token_store()
    existed = store.revoke_token(clean_jti)
    disconnected = await _close_sessions_for_jti(clean_jti)
    return {
        "ok": True,
        "jti": clean_jti,
        "revoked": True,
        "existed": existed,
        "disconnected_sessions": disconnected,
    }


@app.get("/api/edge/devices")
async def list_edge_devices() -> Dict[str, Any]:
    """Return active edge devices and any interrupted checkpoints detected at startup."""
    return {
        "ok": True,
        "devices": [sess.to_public_dict() for sess in _EDGE_SESSIONS.values()],
        "interrupted_tasks": list(_INTERRUPTED_INFLIGHT_TASKS),
    }


def _resolve_target_edge_session(device_id: str, workspace_id: str) -> Optional[_EdgeSession]:
    dev = coerce_text(mask_secrets(str(device_id or ""))).strip()
    ws = coerce_text(mask_secrets(str(workspace_id or ""))).strip()
    if dev and ws:
        return _EDGE_SESSIONS.get(f"{dev}:{ws}")
    if ws:
        return _resolve_edge_session(ws)
    if dev:
        matches = [s for s in _EDGE_SESSIONS.values() if s.device_id == dev]
        matches.sort(key=lambda s: s.last_seen_at, reverse=True)
        return matches[0] if matches else None
    return None


async def _run_edge_delegate_worker(
    task_id: str,
    sess: _EdgeSession,
    skill_name: str,
    expected_sha: str,
    params: Dict[str, Any],
    needs_approval: bool,
    human_task: str,
) -> None:
    entry = _TASKS[task_id]
    em = get_emitter(task_id)
    pump = asyncio.create_task(_task_event_pump(task_id, em))
    try:
        if needs_approval:
            entry["status"] = "RUNNING"
            em.emit(
                EventKind.TASK_STATE,
                state="WAITING",
                waiting_for="approval",
                skill=skill_name,
                device_id=sess.device_id,
            )
            masked_params = botex_server.mask_secrets_recursive(params)
            preview = (
                f"Remote Skill: {skill_name}\n"
                f"Device: {sess.device_id} ({sess.workspace_id})\n"
                f"Digest: {expected_sha[:16]}...\n"
                f"Params: {json.dumps(masked_params, ensure_ascii=False)}"
            )
            if human_task:
                preview = f"{coerce_text(mask_secrets(human_task))}\n\n{preview}"
            sess._approval_task_ids.add(task_id)
            try:
                approved = await default_approval_manager.request_approval(
                    task_id=task_id,
                    tool_name=f"edge_delegate:{skill_name}",
                    params={
                        "device_id": sess.device_id,
                        "workspace_id": sess.workspace_id,
                        "skill": skill_name,
                        "sha256": expected_sha,
                        "params": masked_params,
                    },
                    preview=preview,
                    require_explicit=True,
                    timeout=300.0,
                )
            finally:
                sess._approval_task_ids.discard(task_id)
            if not approved:
                entry["status"] = "FAILED"
                entry["result"] = "Remote skill delegation rejected or timed out."
                entry["result_data"] = {
                    "ok": False,
                    "status": "REJECTED",
                    "error": entry["result"],
                }
                return

        if not sess.is_live() or _edge_token_store().is_revoked(sess.token_record.jti):
            entry["status"] = "FAILED"
            entry["result"] = "Edge device disconnected or liveness lease expired."
            entry["result_data"] = {"ok": False, "status": "FAILED", "error": entry["result"]}
            return

        entry["status"] = "RUNNING"
        em.emit(
            EventKind.TASK_STATE,
            state="RUNNING",
            executor="edge_delegate",
            skill=skill_name,
            device_id=sess.device_id,
        )
        res = await sess.dispatch_delegation(
            skill=skill_name,
            sha256=expected_sha,
            params=params,
            task_id=task_id,
        )
        masked_res = (
            botex_server.mask_secrets_recursive(res)
            if isinstance(res, dict)
            else {"ok": False, "error": "Invalid response from edge device."}
        )
        entry["result_data"] = masked_res
        if masked_res.get("ok"):
            entry["status"] = "DONE"
            entry["result"] = coerce_text(
                mask_secrets(
                    str(
                        masked_res.get("summary")
                        or masked_res.get("output")
                        or masked_res.get("content")
                        or "Remote skill completed."
                    )
                )
            )
        else:
            entry["status"] = "FAILED"
            entry["result"] = coerce_text(
                mask_secrets(str(masked_res.get("error") or "Remote skill failed."))
            )
    except TaskAbortedError:
        entry["status"] = "CANCELLED"
        entry["result"] = "Remote delegation aborted by operator."
        entry["result_data"] = {"ok": False, "status": "CANCELLED", "error": entry["result"]}
    except asyncio.CancelledError:
        entry["status"] = "CANCELLED"
        entry["result"] = "Remote delegation cancelled."
        entry["result_data"] = {"ok": False, "status": "CANCELLED", "error": entry["result"]}
        raise
    except Exception as exc:
        masked_err = coerce_text(mask_secrets(str(exc)))
        entry["status"] = "FAILED"
        entry["result"] = f"Remote delegation failed: {masked_err}"
        entry["result_data"] = {"ok": False, "status": "FAILED", "error": masked_err}
    finally:
        entry["finished_at"] = time.time()
        em.emit(EventKind.TASK_STATE, state=entry.get("status") or "DONE")
        await asyncio.to_thread(_save_task_persistent, task_id, entry, "")
        await asyncio.sleep(0.05)
        pump.cancel()


@app.post("/api/edge/delegate", status_code=status.HTTP_202_ACCEPTED)
async def delegate_to_edge_device(body: EdgeDelegateRequest) -> Dict[str, Any]:
    """Queue a digest-pinned, scope-checked skill delegation to a live edge device."""
    botex_server._cleanup_tasks()
    max_queued = int(botex_server.load_config().get("engine", {}).get("max_queued_tasks", 32))
    active_count = sum(entry.get("status") in {"QUEUED", "RUNNING"} for entry in _TASKS.values())
    if active_count >= max_queued:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Task queue limit reached ({max_queued}).",
        )

    clean_skill = sanitize_identifier(body.skill)
    if not clean_skill:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid skill identifier.",
        )
    safe_params = _validate_delegate_params_safe(body.params)

    sess = _resolve_target_edge_session(body.device_id, body.workspace_id)
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target edge device session not connected.",
        )
    if not sess.is_live() or _edge_token_store().is_revoked(sess.token_record.jti):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Target edge device liveness lease expired or disconnected.",
        )
    if not sess.token_record.has_scope(f"delegate_local:{sess.workspace_id}"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Device token lacks 'delegate_local:{sess.workspace_id}' scope.",
        )

    del_caps = sess.capabilities.get("delegation")
    if not isinstance(del_caps, dict) or not del_caps.get("enabled"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Remote skill delegation is disabled on target device (opt-in required).",
        )

    skill_meta: Optional[Dict[str, Any]] = None
    for candidate in del_caps.get("skills") or []:
        if isinstance(candidate, dict) and candidate.get("name") == clean_skill:
            skill_meta = candidate
            break
    if skill_meta is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Skill '{clean_skill}' is not in target device's digest allowlist.",
        )

    policy_sha = str(skill_meta.get("sha256") or "").strip().lower()
    if not _SHA256_HEX_RE.match(policy_sha):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Skill '{clean_skill}' lacks a valid SHA-256 digest pin.",
        )
    req_sha = str(body.sha256 or "").strip().lower()
    # Malformed (incl. non-ASCII) digests must fail as mismatches, not crash
    # compare_digest with TypeError -> 500.
    if req_sha and (
        not _SHA256_HEX_RE.fullmatch(req_sha) or not hmac.compare_digest(req_sha, policy_sha)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Skill SHA-256 digest does not match target device allowlist.",
        )

    # Fail-closed default: require operator approval unless explicitly marked read_only=True
    is_read_only = bool(skill_meta.get("read_only") is True)
    needs_approval = (not is_read_only) or (body.require_approval is True)

    task_id = uuid.uuid4().hex[:12]
    prompt_label = coerce_text(
        mask_secrets(body.task.strip() or f"Delegate '{clean_skill}' to {sess.device_id}")
    )
    entry: Dict[str, Any] = {
        "task_id": task_id,
        "task": prompt_label,
        "executor": "edge_delegate",
        "device_id": sess.device_id,
        "workspace_id": sess.workspace_id,
        "skill": clean_skill,
        "sha256": policy_sha,
        "request": botex_server.mask_secrets_recursive(body.model_dump(exclude_none=True)),
        "status": "QUEUED",
        "created_at": time.time(),
        "finished_at": None,
        "result": None,
        "result_data": None,
        "async_task": None,
    }
    _TASKS[task_id] = entry
    await asyncio.to_thread(_save_task_persistent, task_id, entry, "")
    worker = asyncio.create_task(
        _run_edge_delegate_worker(
            task_id=task_id,
            sess=sess,
            skill_name=clean_skill,
            expected_sha=policy_sha,
            params=safe_params,
            needs_approval=needs_approval,
            human_task=body.task.strip(),
        )
    )
    entry["async_task"] = worker
    return {
        "ok": True,
        "task_id": task_id,
        "status": "QUEUED",
        "device_id": sess.device_id,
        "workspace_id": sess.workspace_id,
        "skill": clean_skill,
        "sha256": policy_sha,
        "approval_required": needs_approval,
    }


@app.websocket("/api/edge/ws")
async def edge_bridge_websocket(websocket: WebSocket) -> None:
    """Bidirectional compressed WSS channel connecting outbound ``botex edge`` runners."""
    allowed, _ = _GATEWAY_RATE_LIMITER.check_allow(_extract_client_ip_from_scope(websocket.scope))
    if not allowed:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Rate limit exceeded")
        return

    await websocket.accept()
    store = _edge_token_store()

    # Extract Bearer token from the Authorization header or the initial auth frame.
    # (No query-string fallback: tokens in URLs leak into proxy access logs.)
    auth_header = websocket.headers.get("authorization", "")
    raw_cred = ""
    if auth_header.startswith("Bearer "):
        raw_cred = auth_header[7:].strip()
    if not raw_cred:
        try:
            first_msg = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
            if isinstance(first_msg, dict) and first_msg.get("type") == "auth":
                raw_cred = str(first_msg.get("token") or "").strip()
        except Exception:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized")
            return

    rec = store.verify_token(raw_cred)
    if rec is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized")
        return

    # Require explicit handshake declaring device_id, workspace_id, and capability ceilings.
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Handshake timeout")
        return
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Expected hello frame")
        return

    hello_dev = coerce_text(mask_secrets(str(hello.get("device_id") or ""))).strip()[:96]
    hello_ws = coerce_text(mask_secrets(str(hello.get("workspace_id") or ""))).strip()[:160]
    if not hello_dev or not hello_ws:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing identity")
        return
    if rec.device_id != "*" and hello_dev != rec.device_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Device mismatch")
        return
    if not (rec.has_scope(f"task_pull:{hello_ws}") and rec.has_scope(f"result_push:{hello_ws}")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Insufficient scope")
        return

    session = _EdgeSession(
        websocket=websocket,
        token_record=rec,
        device_id=hello_dev,
        workspace_id=hello_ws,
        capabilities=dict(hello.get("capabilities") or {}),
        protocol_version=str(hello.get("protocol_version") or "1.0")[:16],
        client_version=str(hello.get("client_version") or "")[:32],
    )
    prev = _EDGE_SESSIONS.get(session.session_key)
    if prev is not None and prev is not session:
        prev.fail_all_pending("Superseded by newer edge connection")
        with suppress(Exception):
            await prev.websocket.close(code=1000, reason="Superseded")
    _EDGE_SESSIONS[session.session_key] = session

    try:
        await session.send_frame(
            {
                "type": "hello_ack",
                "device_id": session.device_id,
                "workspace_id": session.workspace_id,
                "server_version": "2.1.0",
                "min_client_version": "2.1.0",
                "heartbeat_s": int(_EDGE_PING_INTERVAL_S),
            }
        )
        while True:
            if store.is_revoked(rec.jti):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Token revoked")
                break
            try:
                frame = await asyncio.wait_for(
                    websocket.receive_json(), timeout=_EDGE_PING_INTERVAL_S
                )
            except asyncio.TimeoutError:
                # Liveness deadline: a silent-but-open connection (suspended
                # laptop, half-dead TCP) must not hold a session slot forever.
                # 1001 (not 1008) so a resuming edge reconnects instead of
                # treating this as a permanent token failure.
                if time.time() - session.last_seen_at > 2 * _EDGE_PING_INTERVAL_S:
                    await websocket.close(
                        code=status.WS_1001_GOING_AWAY, reason="Heartbeat timeout"
                    )
                    break
                if store.is_revoked(rec.jti):
                    await websocket.close(
                        code=status.WS_1008_POLICY_VIOLATION, reason="Token revoked"
                    )
                    break
                await session.send_frame({"type": "ping", "ts": time.time()})
                continue

            session.last_seen_at = time.time()
            if not isinstance(frame, dict):
                continue
            ftype = frame.get("type")
            if ftype == "ping":
                await session.send_frame({"type": "pong", "ts": frame.get("ts") or time.time()})
            elif ftype == "pong":
                continue
            elif ftype in ("tool_result", "delegate_result"):
                cid = str(frame.get("call_id") or "")
                res_obj = frame.get("result")
                if cid:
                    session.resolve_pending(
                        cid,
                        res_obj
                        if isinstance(res_obj, dict)
                        else {"ok": False, "error": "Bad payload"},
                    )
            elif ftype in ("tool_error", "delegate_error"):
                cid = str(frame.get("call_id") or "")
                err_msg = coerce_text(mask_secrets(str(frame.get("error") or "Edge tool failed")))
                if cid:
                    session.resolve_pending(cid, {"ok": False, "error": err_msg})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Edge websocket disconnected unexpectedly", exc_info=True)
    finally:
        session.fail_all_pending("Edge connection closed")
        if _EDGE_SESSIONS.get(session.session_key) is session:
            _EDGE_SESSIONS.pop(session.session_key, None)


# ---------------------------------------------------------------------------
# Static Web PWA Mounting (served when web/dist is built)
# ---------------------------------------------------------------------------
_web_dist_dir = os.path.join(_root_dir, "web", "dist")
if os.path.isdir(_web_dist_dir):
    from fastapi.staticfiles import StaticFiles
    from starlette.responses import Response

    class NoCacheStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope) -> Response:
            resp = await super().get_response(path, scope)
            # Force re-validation of entrypoints, manifest and service worker to prevent stale PWA cache
            if (
                path.endswith(".html")
                or path.endswith("sw.js")
                or path.endswith(".webmanifest")
                or path.endswith("manifest.json")
                or not path
                or path == "."
            ):
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                resp.headers["Pragma"] = "no-cache"
                resp.headers["Expires"] = "0"
            return resp

    app.mount("/", NoCacheStaticFiles(directory=_web_dist_dir, html=True), name="web_pwa")
