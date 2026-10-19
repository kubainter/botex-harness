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
from typing import Any, Dict, List, Optional

# Ensure repository root is on sys.path
_current_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir = os.path.dirname(_current_dir)
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)

from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from starlette.types import ASGIApp, Receive, Scope, Send

import server as botex_server
from botex.approval import PendingAction, default_approval_manager
from botex.catalog import get_consolidated_models
from botex.chat import default_chat_manager, stream_chat_turn
from botex.config import resolve_workspace
from botex.engine import run_botex_task
from botex.memory import read_memory, save_memory, search_memory
from botex.security import mask_secrets, sanitize_identifier, sever_taint_numeric
from botex.wallet import default_wallet_service

logger = logging.getLogger(__name__)

# Single-worker invariant for in-process state consistency
WORKERS_INVARIANT: int = 1
START_TIME: float = time.time()

# Shared reference to task repository in server
_TASKS: Dict[str, Dict[str, Any]] = botex_server._TASKS

# Connected WebSockets for real-time push notifications with serialization locks
_ACTIVE_WEBSOCKETS: Dict[WebSocket, asyncio.Lock] = {}


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
    approval_policy: str = ""


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


from contextlib import asynccontextmanager

# ---------------------------------------------------------------------------
# FastAPI Application & Middlewares
# ---------------------------------------------------------------------------


async def _warmup_caches() -> None:
    try:
        from botex.benchmarks import fetch_openrouter_benchmarks

        await asyncio.to_thread(fetch_openrouter_benchmarks)
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Non-blocking background warmup
    asyncio.create_task(_warmup_caches())
    yield


app = FastAPI(
    title="BoteX Cloud Gateway & Remote MCP",
    description="FastAPI service for autonomous code harness, Remote MCP SSE, and interactive chat.",
    version="1.0.0",
    lifespan=lifespan,
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

        expected_token = os.environ.get("BOTEX_ACCESS_TOKEN", "").strip()
        if not expected_token:
            logger.info("Zero Friction Local mode: allowing unauthenticated request to %s", path)
            await self.app(scope, receive, send)
            return

        # Extract token from Authorization header or query parameter
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("latin1", errors="ignore")
        token: Optional[str] = None
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif path.startswith("/mcp") or path.startswith("/api/chat"):
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

# Expose the exact 12 existing BoteX MCP tools by wrapping server.mcp directly
_mcp_subapp = botex_server.mcp.sse_app(
    sse_path="/sse",
    message_path="/messages",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
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
# otherwise default to allow_origins=["*"] with allow_credentials=False for security compliance.
_cfg = botex_server.load_config()
_configured_origins: List[str] = _cfg.get("cloud", {}).get("cors_origins", [])
if _configured_origins:
    _cors_origins = _configured_origins
    _allow_credentials = True
else:
    _cors_origins = ["*"]
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
        "version": "1.0.0",
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


@app.get("/api/models")
async def get_models(
    provider: Optional[str] = Query(default=None),
    force_refresh: bool = Query(default=False),
) -> List[Dict[str, Any]]:
    """Return consolidated model catalog across configured providers (openrouter, openrouter-free, nvidia)."""
    if provider and provider.strip():
        providers_seq = (provider.strip().lower(),)
    else:
        providers_seq = ("openrouter", "openrouter-free", "nvidia")
    cfg = botex_server.load_config() if force_refresh else _cfg
    return get_consolidated_models(
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


async def _run_task_worker(task_id: str, req: TaskCreateRequest) -> None:
    """Asynchronous background worker executing run_botex_task under semaphore control."""
    entry = _TASKS[task_id]
    entry["status"] = "QUEUED"
    try:
        # Bounded concurrency: enforce engine semaphore
        async with botex_server._TASK_SEMAPHORE:
            entry["status"] = "RUNNING"
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
                approval_policy=req.approval_policy or None,
            )
            entry["result_data"] = botex_server.mask_secrets_recursive(res)
            entry["result"] = mask_secrets(res.get("summary") or res.get("message") or "")
            entry["status"] = "DONE" if res.get("ok") else res.get("status", "FAILED")
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
        entry["finished_at"] = time.time()


@app.post("/api/tasks", status_code=status.HTTP_202_ACCEPTED)
async def create_task(req: TaskCreateRequest) -> Dict[str, Any]:
    """Delegate autonomous code editing task in the background."""
    botex_server._cleanup_tasks()

    # Task queue bounding
    max_queued = int(botex_server.load_config().get("engine", {}).get("max_queued_tasks", 32))
    active_count = sum(entry.get("status") in {"QUEUED", "RUNNING"} for entry in _TASKS.values())
    if active_count >= max_queued:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Task queue limit reached ({max_queued}).",
        )

    task_id = uuid.uuid4().hex[:12]
    entry = {
        "task_id": task_id,
        "task": mask_secrets(req.task),  # ZDR: scrub sensitive tokens
        "status": "QUEUED",
        "created_at": time.time(),
        "finished_at": None,
        "result": None,
        "result_data": None,
        "async_task": None,
    }
    _TASKS[task_id] = entry
    worker = asyncio.create_task(_run_task_worker(task_id, req))
    entry["async_task"] = worker
    return {"task_id": task_id, "status": "QUEUED"}


@app.get("/api/tasks")
async def list_tasks() -> Dict[str, Any]:
    """List all registered background tasks."""
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
    }


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
        res = read_memory(workspace_dir=ws, memory_id=clean_mid, scope=scope)
        if not res.get("ok"):
            logger.warning(
                "Memory read failed for ID '%s': %s",
                clean_mid,
                sever_taint_numeric(mask_secrets(str(res.get("error") or ""))),
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Memory entry '{clean_mid}' not found.",
            )
        return botex_server.mask_secrets_recursive(res)

    res = search_memory(
        workspace_dir=ws,
        query=query,
        tag=tag,
        kind=kind,
        scope=scope,
    )
    if not res.get("ok"):
        logger.warning(
            "Memory search failed: %s",
            sever_taint_numeric(mask_secrets(str(res.get("error") or ""))),
        )
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
    res = save_memory(
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
        logger.warning(
            "Memory save failed: %s",
            sever_taint_numeric(mask_secrets(str(res.get("error") or ""))),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to save memory entry.",
        )
    return botex_server.mask_secrets_recursive(res)


# ---------------------------------------------------------------------------
# Chat Session Management & Memory Export
# ---------------------------------------------------------------------------


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
        logger.warning(
            "Save chat to memory failed for '%s': %s",
            clean_sid,
            sever_taint_numeric(mask_secrets(str(res.get("error") or ""))),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to save chat transcript to memory vault.",
        )
    return botex_server.mask_secrets_recursive(res)


# ---------------------------------------------------------------------------
# Streaming Chat Endpoints: SSE & WebSocket
# ---------------------------------------------------------------------------


@app.post("/api/chat")
async def post_chat(body: ChatRequest) -> EventSourceResponse:
    """Stream conversational chat turns using Server-Sent Events with periodic keepalives."""
    clean_sid = sanitize_identifier(body.session_id) if body.session_id else None
    if body.session_id and not clean_sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid session_id format.",
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
        async with session.lock:
            async for chunk in stream_chat_turn(session, user_input=body.message):
                masked_chunk = botex_server.mask_secrets_recursive(chunk)
                yield ServerSentEvent(
                    data=json.dumps(masked_chunk),
                    event=masked_chunk.get("type", "message"),
                )

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
    if expected_token:
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
            elif msg_type == "message" or "message" in data or "content" in data:
                content = data.get("content") or data.get("message") or ""
                session = default_chat_manager.get_or_create_session(
                    session_id=data.get("session_id"),
                    workspace_dir=data.get("workspace_dir", ""),
                    model=data.get("model", ""),
                    provider=data.get("provider", ""),
                    profile=data.get("profile", ""),
                    system_prompt=data.get("system_prompt"),
                )
                async with session.lock:
                    async for chunk in stream_chat_turn(session, user_input=content):
                        masked_chunk = botex_server.mask_secrets_recursive(chunk)
                        await _send_ws_json_safe(websocket, masked_chunk)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        clean_err = sever_taint_numeric(mask_secrets(str(exc)))
        logger.warning("WebSocket session terminated: %s", clean_err)
    finally:
        heartbeat_task.cancel()
        _ACTIVE_WEBSOCKETS.pop(websocket, None)


# ---------------------------------------------------------------------------
# Static Web PWA Mounting (served when web/dist is built)
# ---------------------------------------------------------------------------
_web_dist_dir = os.path.join(_root_dir, "web", "dist")
if os.path.isdir(_web_dist_dir):
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_web_dist_dir, html=True), name="web_pwa")
