# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX FastAPI Cloud Gateway & Remote MCP SSE Server
===================================================
Production-ready cloud gateway exposing BoteX harness capabilities:
1. Zero Friction Local / Strict Cloud Bearer Token authentication.
2. Standard Remote Model Context Protocol (MCP) SSE transport at /mcp/sse and /mcp/messages.
3. RESTful endpoints for health probes, OpenRouter wallet balance, autonomous task delegation,
   operator human-in-the-loop approvals, and workspace Memory Vault operations.
4. Real-time streaming chat over Server-Sent Events (POST /api/chat) and bidirectional WebSockets
   (WS /api/chat/ws) with a 15-second ping heartbeat and push notifications.

Invariant: Single-worker process (workers=1) for in-memory state consistency.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
import time
import uuid
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

from urllib.parse import parse_qs

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
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
from botex.approval import PendingAction, default_approval_manager
from botex.catalog import get_consolidated_models
from botex.chat import default_chat_manager, stream_chat_turn
from botex.config import (
    WorkspaceOutOfBounds,
    analytics_setting,
    get_tasks_dir,
    resolve_provider,
    resolve_workspace,
)
from botex.engine import run_botex_task
from botex.events import EventKind, emitter_or_none, get_emitter
from botex.memory import (
    _get_index_db,
    _resolve_scope_dirs,
    delete_memory,
    find_graph_health,
    get_backlinks,
    read_memory,
    save_memory,
    search_memory,
)
from botex.plugins import PluginVaultManager
from botex.rate_limiter import get_rate_limiter
from botex.security import coerce_text, mask_secrets, sanitize_identifier
from botex.skills import delete_skill, get_skill, install_skill_async, list_skills
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


class TaskFollowupRequest(BaseModel):
    prompt: str


class MemorySaveRequest(BaseModel):
    title: str
    content: str
    kind: str = "context"
    tags: List[str] = Field(default_factory=list)
    memory_id: Optional[str] = None
    origin: str = "cloud_api"
    scope: str = "local"
    workspace_dir: str = "."


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


from contextlib import asynccontextmanager, suppress

# ---------------------------------------------------------------------------
# FastAPI Application & Middlewares
# ---------------------------------------------------------------------------


async def _warmup_caches() -> None:
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
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model_id,
                messages=messages,
                max_tokens=int(analytics_setting("digest_max_tokens") or 320),
                temperature=0.3,
                stream=False,
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
async def lifespan(app: FastAPI):
    # Non-blocking background warmup
    asyncio.create_task(_warmup_caches())
    digest_task: Optional[asyncio.Task] = None
    if analytics_setting("digest_enabled"):
        digest_task = asyncio.create_task(_analytics_digest_loop())
    yield
    if digest_task is not None:
        digest_task.cancel()
        with suppress(asyncio.CancelledError):
            await digest_task
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


class CloudAuthMiddleware:
    """ASGI Authentication middleware supporting Zero Friction Local and Strict Cloud modes.

    - CORS preflight (OPTIONS) requests pass through unhindered.
    - /api/health probe passes through unhindered without authentication.
    - If BOTEX_ACCESS_TOKEN is not configured or empty: allow local unauthenticated requests with a log note.
    - If BOTEX_ACCESS_TOKEN is set: strictly verify Bearer token via constant-time comparison.
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

        # Normalize /mcp/messages to prevent Starlette Mount 307 redirects on POST
        if path == "/mcp/messages":
            scope["path"] = "/mcp/messages/"
            if "raw_path" in scope:
                scope["raw_path"] = b"/mcp/messages/"

        # Reject payloads exceeding 25 MiB to prevent memory exhaustion / OOM on constrained VPS
        headers = dict(scope.get("headers", []))
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

        expected_token = os.environ.get("BOTEX_ACCESS_TOKEN", "").strip()
        if not expected_token:
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
                        "detail": "Forbidden: Remote VPS access requires BOTEX_ACCESS_TOKEN to be configured."
                    },
                )
                await resp(scope, receive, send)
                return

            logger.info("Zero Friction Local mode: allowing unauthenticated request to %s", path)
            await self.app(scope, receive, send)
            return

        # Extract token from Authorization header or query parameter
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("latin1", errors="ignore")
        token: Optional[str] = None
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif method == "GET" and path == "/mcp/sse":
            # Query-string token is only tolerated for the EventSource handshake (browsers
            # cannot set headers there); every other route requires the Bearer header.
            qs = parse_qs(scope.get("query_string", b"").decode("latin1", errors="ignore"))
            if "token" in qs and qs["token"]:
                token = qs["token"][0].strip()

        # Session-aware MCP authentication: if session was established on authenticated /mcp/sse
        if not token and path.startswith("/mcp/messages") and _mcp_transport is not None:
            qs = parse_qs(scope.get("query_string", b"").decode("latin1", errors="ignore"))
            session_id = qs.get("session_id", [""])[0].strip()
            if session_id:
                active_sessions = {
                    sid.hex for sid in getattr(_mcp_transport, "_read_stream_writers", {}).keys()
                }
                if session_id in active_sessions:
                    await self.app(scope, receive, send)
                    return

        # Constant-time token verification (compute compare_digest unconditionally)
        candidate_bytes = (token or "").encode("utf-8")
        expected_bytes = expected_token.encode("utf-8")
        digest_matches = hmac.compare_digest(candidate_bytes, expected_bytes)
        is_valid = bool(token) and digest_matches

        if not is_valid:
            resp = JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Unauthorized: Invalid or missing bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Mount Remote MCP via SSE
# ---------------------------------------------------------------------------


def _mcp_transport_security() -> TransportSecuritySettings:
    """DNS-rebinding guard for the MCP mount.

    Without BOTEX_ACCESS_TOKEN (Zero Friction Local) network locality is the only barrier, so
    Host/Origin are validated against loopback names. With a token set, Bearer auth is the
    barrier and public hostnames (VPS / reverse proxy) must keep working.
    """
    if os.environ.get("BOTEX_ACCESS_TOKEN", "").strip():
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
_mcp_transport = next(
    (r.app.__self__ for r in _mcp_subapp.routes if hasattr(getattr(r, "app", None), "__self__")),
    None,
)
app.mount("/mcp", _mcp_subapp)


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


@app.get("/api/delegation/devin")
async def get_devin_executor_status() -> Dict[str, Any]:
    """Availability probe for the `devin` task executor (exe + credential)."""
    from botex.delegation import devin_auth_status, list_runs

    st = devin_auth_status()
    st["runs"] = list_runs(limit=10) if st["enabled"] else []
    return st


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
    provider: Optional[str] = Query(default=None),
    force_refresh: bool = Query(default=False),
) -> List[Dict[str, Any]]:
    """Return consolidated model catalog across configured providers (openrouter, openrouter-free, nvidia, ollama, google)."""
    if provider and provider.strip():
        providers_seq = (provider.strip().lower(),)
    else:
        providers_seq = ("openrouter", "openrouter-free", "nvidia", "ollama", "google")
    cfg = botex_server.load_config() if force_refresh else _cfg
    return await asyncio.to_thread(
        get_consolidated_models,
        providers=providers_seq,
        config=cfg,
        force_refresh=force_refresh,
    )


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
    elif kind in (EventKind.ATTEMPT_START, EventKind.DEVIN_UPDATE):
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


async def _run_devin_task(
    task_id: str, req: TaskCreateRequest, session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Execute the task through the direct-ACP Devin executor."""
    from botex.delegation import delegate_run, devin_auth_status

    auth = devin_auth_status()
    if not auth["available"]:
        return {
            "ok": False,
            "status": "CONFIG_ERROR",
            "failure_kind": "capability_missing",
            "message": f"Devin executor unavailable: {auth['reason']}",
        }
    receipt = await asyncio.to_thread(
        delegate_run,
        task_id,
        req.task,
        resolve_workspace(req.workspace_dir),
        req.agent_type or "default",
        session_id=session_id or "",
    )
    ok = bool(receipt.pop("ok", False))
    receipt["ok"] = ok
    receipt["status"] = "DONE" if ok else "FAILED"
    receipt["executor"] = "devin"
    if not ok:
        receipt["failure_kind"] = "provider_error"
        receipt["summary"] = receipt.get("summary") or receipt.get("error") or ""
    return receipt


def _record_devin_turn(entry: Dict[str, Any], prompt: str, res: Dict[str, Any]) -> None:
    """Append one user->agent exchange to the task's persisted turn history."""
    turns = entry.setdefault("turns", [])
    turns.append(
        {
            "turn": len(turns) + 1,
            "prompt": mask_secrets(prompt),
            "summary": res.get("summary") or res.get("message") or "",
            "ok": bool(res.get("ok")),
            "run_id": res.get("run_id"),
            "session_id": res.get("session_id"),
            "resumed": res.get("resumed"),
            "resume_mode": res.get("resume_mode"),
            "ts": time.time(),
        }
    )


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
        cooldown_s = get_rate_limiter().report_limited(provider_name)
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
    try:
        # Provider admission throttle: wait on the provider's RPM budget
        # BEFORE taking a concurrency slot — a task parked on a provider
        # cooldown must not starve other providers' queue slots.
        if executor != "devin":
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
        async with botex_server._TASK_SEMAPHORE:
            entry["status"] = "RUNNING"
            em.emit(EventKind.TASK_STATE, state="RUNNING", executor=executor)
            if executor == "devin":
                res = await _run_devin_task(task_id, req)
            else:
                res = await run_botex_task(
                    task=req.task,
                    files=req.files or None,
                    workspace_dir=resolve_workspace(req.workspace_dir),
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
                )
            entry["result_data"] = botex_server.mask_secrets_recursive(res)
            entry["result"] = mask_secrets(res.get("summary") or res.get("message") or "")
            entry["status"] = "DONE" if res.get("ok") else res.get("status", "FAILED")
            _note_provider_result(provider_name, res)
            if executor == "devin":
                # Persist the ACP session so follow-up turns can resume it
                # (multi-turn continuation / prompt-cache reuse).
                if res.get("session_id"):
                    entry["session_id"] = res["session_id"]
                if not entry.get("turns"):
                    _record_devin_turn(entry, req.task, res)
    except asyncio.CancelledError:
        entry["status"] = "CANCELLED"
        if executor == "devin":
            try:
                from botex.delegation import cancel_run_for_task

                cancel_run_for_task(task_id)
            except Exception:
                pass
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
        entry["finished_at"] = time.time()
        em.emit(EventKind.TASK_STATE, state=entry.get("status") or "DONE")
        await asyncio.to_thread(_save_task_persistent, task_id, entry, req.workspace_dir)
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
    await asyncio.to_thread(_save_task_persistent, task_id, entry, req.workspace_dir)
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


async def _run_followup_worker(task_id: str, prompt: str, session_id: str) -> None:
    """Background worker driving a follow-up turn inside the persisted Devin session."""
    entry = _TASKS[task_id]
    em = get_emitter(task_id)
    pump = asyncio.create_task(_task_event_pump(task_id, em))
    req_data = entry.get("request") or {}
    try:
        async with botex_server._TASK_SEMAPHORE:
            entry["status"] = "RUNNING"
            em.emit(EventKind.TASK_STATE, state="RUNNING", executor="devin", followup=True)
            req_kwargs = {k: v for k, v in req_data.items() if k in TaskCreateRequest.model_fields}
            req = TaskCreateRequest(**req_kwargs)
            req.task = prompt
            res = await _run_devin_task(task_id, req, session_id=session_id)
            entry["result_data"] = botex_server.mask_secrets_recursive(res)
            entry["result"] = mask_secrets(res.get("summary") or res.get("message") or "")
            entry["status"] = "DONE" if res.get("ok") else res.get("status", "FAILED")
            if res.get("session_id"):
                entry["session_id"] = res["session_id"]
            _record_devin_turn(entry, prompt, res)
    except asyncio.CancelledError:
        entry["status"] = "CANCELLED"
        try:
            from botex.delegation import cancel_run_for_task

            cancel_run_for_task(task_id)
        except Exception:
            pass
        if not entry.get("result"):
            entry["result"] = "Follow-up cancelled by operator."
        raise
    except Exception as exc:
        masked_err = mask_secrets(str(exc))
        entry["status"] = "FAILED"
        entry["result"] = f"Follow-up execution failed: {masked_err}"
    finally:
        entry["finished_at"] = time.time()
        em.emit(EventKind.TASK_STATE, state=entry.get("status") or "DONE")
        await asyncio.to_thread(
            _save_task_persistent, task_id, entry, req_data.get("workspace_dir") or ""
        )
        # Let the pump deliver the final task_state frame before teardown.
        await asyncio.sleep(0.1)
        pump.cancel()


@app.post("/api/tasks/{task_id}/followup", status_code=status.HTTP_202_ACCEPTED)
async def create_task_followup(task_id: str, body: TaskFollowupRequest) -> Dict[str, Any]:
    """Send a follow-up turn to the Devin session persisted on the task receipt.

    Resumes the stored ACP session (or ``devin -r`` when the ACP server cannot
    ``session/load``) so multi-turn continuation reuses the prompt cache instead
    of starting a clean slate.
    """
    _load_tasks_persistent()
    clean_tid = sanitize_identifier(task_id)
    if not clean_tid or clean_tid not in _TASKS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    entry = _TASKS[clean_tid]
    if entry.get("executor") != "devin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Follow-up turns are only supported for devin-executor tasks.",
        )
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="prompt must not be empty"
        )
    if len(prompt) > 65536:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Follow-up prompt exceeds maximum allowed length (65536 characters).",
        )
    session_id = entry.get("session_id") or (entry.get("result_data") or {}).get("session_id")
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task has no resumable Devin session_id.",
        )
    if entry.get("status") in {"QUEUED", "RUNNING"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is still running - wait for it to finish before sending a follow-up.",
        )
    entry["status"] = "QUEUED"
    entry["finished_at"] = None
    worker = asyncio.create_task(_run_followup_worker(clean_tid, prompt, str(session_id)))
    entry["async_task"] = worker
    return {
        "ok": True,
        "task_id": clean_tid,
        "status": "QUEUED",
        "session_id": session_id,
    }


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

    # Devin executor: terminate the ACP run tree (cross-process safe — the
    # kill validates image name + start time before taskkill).
    if entry.get("executor") == "devin":
        try:
            from botex.delegation import kill_run, list_runs

            for r in list_runs():
                if r.get("task_id") == clean_tid and r.get("live"):
                    kill_run(r["run_id"])
        except Exception as exc:
            logger.warning(f"devin run kill for task {clean_tid} failed: {exc}")

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


@app.get("/api/memory")
async def get_memory(
    query: str = Query(default=""),
    tag: str = Query(default=""),
    kind: str = Query(default=""),
    scope: str = Query(default="local"),
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
    """Save an entry to the Memory Vault."""
    ws = resolve_workspace(body.workspace_dir)
    clean_mid = sanitize_identifier(body.memory_id) if body.memory_id else None
    if body.memory_id and not clean_mid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid memory_id format.",
        )
    res = await asyncio.to_thread(
        save_memory,
        workspace_dir=ws,
        title=body.title,
        content=body.content,
        kind=body.kind,
        tags=body.tags,
        memory_id=clean_mid,
        origin=body.origin,
        scope=body.scope,
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
                "SELECT id, title, kind, tags, status, compiled_truth, timestamp FROM memories_meta;"
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
        tags=body.tags,
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

    expected_token = os.environ.get("BOTEX_ACCESS_TOKEN", "").strip()
    if not expected_token:
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
                reason="Forbidden: Remote access requires BOTEX_ACCESS_TOKEN",
            )
            return
    elif expected_token:
        # Check token in query parameters or Authorization header
        token = websocket.query_params.get("token", "").strip()
        if not token:
            auth_header = websocket.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()

        authenticated = False
        if token and hmac.compare_digest(token.encode("utf-8"), expected_token.encode("utf-8")):
            authenticated = True
        else:
            # Allow auth via first message: {"type": "auth", "token": "..."}
            try:
                auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
                if (
                    isinstance(auth_msg, dict)
                    and auth_msg.get("type") == "auth"
                    and hmac.compare_digest(
                        str(auth_msg.get("token", "")).encode("utf-8"),
                        expected_token.encode("utf-8"),
                    )
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
# Static Web PWA Mounting (served when web/dist is built)
# ---------------------------------------------------------------------------
_web_dist_dir = os.path.join(_root_dir, "web", "dist")
if os.path.isdir(_web_dist_dir):
    from fastapi.staticfiles import StaticFiles
    from starlette.responses import Response

    class NoCacheStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope) -> Response:
            resp = await super().get_response(path, scope)
            # Force re-validation of entrypoints and service worker to prevent stale PWA cache
            if path.endswith(".html") or path.endswith("sw.js") or not path or path == ".":
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                resp.headers["Pragma"] = "no-cache"
                resp.headers["Expires"] = "0"
            return resp

    app.mount("/", NoCacheStaticFiles(directory=_web_dist_dir, html=True), name="web_pwa")
