# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Edge Runner
=================
Thin outbound WSS bridge: the edge dials out to the Cloud Gateway
(``wss://…/api/edge/ws``) and executes workspace tool calls on its own
filesystem — no inbound listener, no file sync, no provider keys.

Security posture (fail-closed):
- ``edge_caps`` is the local ceiling: cloud frames can narrow it, never widen it.
- Every path argument is jailed inside ``workspace_root`` via
  ``resolve_safe_path`` (``..`` traversal and symlink escapes are rejected
  before any tool runs).
- All outbound strings pass ``mask_secrets``/``coerce_text`` before
  ``json.dumps``; the bearer token never reaches logs or result payloads.
- Duplicate ``call_id`` deliveries after reconnect are answered from the
  idempotency ring so mutations never execute twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import random
import re
import socket
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from botex import __version__
from botex.tools.exec_tools import run_command
from botex.tools.file_tools import (
    create_file,
    delete_file,
    get_file_outline,
    list_dir,
    move_file,
    read_file,
    read_file_lines,
)
from botex.tools.patch_engine import apply_patch
from botex.tools.security import (
    SecurityError,
    coerce_text,
    mask_secrets,
    resolve_safe_path,
)

try:
    from botex.server.config import load_config as _load_config
except (ImportError, ValueError):  # standalone edge installs may lack server cfg
    _load_config = None

_LOG = logging.getLogger("botex.edge")

PROTOCOL_VERSION = "1.0"

DEFAULT_ALLOWED_TOOLS = frozenset(
    {
        "get_file_outline",
        "read_file_lines",
        "read_file",
        "list_dir",
        "apply_patch",
        "create_file",
    }
)

# Tools the runner can dispatch at all — the allowed_tools ceiling can only
# select a subset of these.
EDGE_TOOL_NAMES = frozenset(
    {
        "get_file_outline",
        "read_file_lines",
        "read_file",
        "list_dir",
        "apply_patch",
        "create_file",
        "delete_file",
        "move_file",
        "run_command",
    }
)

# Network tools are never executed on the edge, even if a crafted
# edge_caps.json whitelists them — the edge only has an outbound control
# channel, so a fetched page would become an uncontrolled exfil path.
EDGE_NEVER_TOOLS = frozenset({"read_url", "fetch_url"})

_DESTRUCTIVE_TOOLS = frozenset({"delete_file", "move_file"})
_DIGEST_TOOLS = frozenset({"read_file", "read_file_lines"})
_PATH_ARG_KEYS = ("path", "src_path", "dst_path")
_TRUNCATED_MARKER = "\n[truncated on edge]"

_IDEM_MAXLEN = 256
_IDEM_TTL_S = 600.0


def _version_tuple(value: Any) -> Tuple[int, ...]:
    parts = [int(p) for p in re.findall(r"\d+", str(value))[:4]]
    return tuple(parts)


def _connect_headers_kwargs(connect_fn: Any, headers: Dict[str, str]) -> Dict[str, Any]:
    """websockets>=14 renamed ``extra_headers`` to ``additional_headers``;
    the declared floor is websockets>=12, so pick whichever kwarg exists."""
    try:
        params = inspect.signature(connect_fn).parameters
    except (TypeError, ValueError):
        params = {}
    key = "additional_headers" if "additional_headers" in params else "extra_headers"
    return {key: headers}


def _err(message: str) -> Dict[str, Any]:
    return {"ok": False, "error": message}


def _mask_recursive(value: Any) -> Any:
    if isinstance(value, str):
        return coerce_text(mask_secrets(value))
    if isinstance(value, dict):
        return {k: _mask_recursive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_recursive(v) for v in value]
    return value


@dataclass
class EdgeCapabilities:
    """Local capability ceiling advertised to and enforced against the cloud."""

    allow_exec: bool = False
    allow_destructive: bool = False
    max_output_bytes: int = 65536
    allowed_tools: frozenset = field(default_factory=lambda: DEFAULT_ALLOWED_TOOLS)
    exec_allowlist: List[str] = field(default_factory=list)
    exec_deny_args: List[str] = field(default_factory=list)
    exec_timeout_s: int = 120

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allow_exec": bool(self.allow_exec),
            "allow_destructive": bool(self.allow_destructive),
            "max_output_bytes": int(self.max_output_bytes),
            "allowed_tools": sorted(self.allowed_tools),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EdgeCapabilities":
        cfg_exec = (_load_config() or {}).get("exec", {}) if _load_config else {}
        caps = cls()
        if not isinstance(data, dict):
            return caps
        caps.allow_exec = bool(data.get("allow_exec", caps.allow_exec))
        caps.allow_destructive = bool(data.get("allow_destructive", caps.allow_destructive))
        try:
            caps.max_output_bytes = max(1, int(data.get("max_output_bytes", caps.max_output_bytes)))
        except (TypeError, ValueError):
            pass
        tools = data.get("allowed_tools")
        if isinstance(tools, (list, tuple, set)):
            caps.allowed_tools = frozenset(str(t) for t in tools) & EDGE_TOOL_NAMES
        caps.exec_allowlist = [
            str(x) for x in (data.get("exec_allowlist", cfg_exec.get("allowlist", [])) or [])
        ]
        caps.exec_deny_args = [
            str(x) for x in (data.get("exec_deny_args", cfg_exec.get("deny_args", [])) or [])
        ]
        try:
            caps.exec_timeout_s = max(
                1, int(data.get("exec_timeout_s", cfg_exec.get("timeout_s", caps.exec_timeout_s)))
            )
        except (TypeError, ValueError):
            pass
        return caps

    @classmethod
    def load(
        cls,
        workspace_root: Optional[str | Path] = None,
        explicit: Optional["EdgeCapabilities | Dict[str, Any]"] = None,
    ) -> "EdgeCapabilities":
        """Resolve the capability ceiling: explicit object > workspace
        ``.botex/edge_caps.json`` > ``~/.botex/edge_caps.json`` > defaults."""
        if isinstance(explicit, EdgeCapabilities):
            return explicit
        if isinstance(explicit, dict):
            return cls.from_dict(explicit)
        candidates: List[Path] = []
        if workspace_root is not None:
            candidates.append(Path(workspace_root) / ".botex" / "edge_caps.json")
        candidates.append(Path.home() / ".botex" / "edge_caps.json")
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return cls.from_dict(json.loads(candidate.read_text(encoding="utf-8")))
            except Exception as exc:
                _LOG.warning(
                    "ignoring unreadable %s: %s",
                    candidate.name,
                    mask_secrets(str(exc))[:120],
                )
        return cls()


class _IdemCache:
    """Bounded FIFO idempotency ring keyed by ``(task_id, call_id)``."""

    def __init__(self, maxlen: int = _IDEM_MAXLEN, ttl_s: float = _IDEM_TTL_S) -> None:
        self._maxlen = int(maxlen)
        self._ttl_s = float(ttl_s)
        self._items: "OrderedDict[Tuple[str, str], Tuple[float, Dict[str, Any]]]" = OrderedDict()

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._ttl_s
        for key in list(self._items):
            if self._items[key][0] < cutoff:
                del self._items[key]
            else:
                break  # FIFO insertion order — the rest are newer

    def get(self, key: Tuple[str, str]) -> Optional[Dict[str, Any]]:
        self._prune()
        entry = self._items.get(key)
        return entry[1] if entry is not None else None

    def put(self, key: Tuple[str, str], value: Dict[str, Any]) -> None:
        self._prune()
        self._items[key] = (time.monotonic(), value)
        while len(self._items) > self._maxlen:
            self._items.popitem(last=False)


class EdgeRunner:
    """Outbound WSS client executing cloud tool calls inside ROOT_JAIL."""

    def __init__(
        self,
        server_url: str,
        *,
        token: str = "",
        device_id: str = "",
        workspace_id: str = "",
        workspace_root: str | Path = ".",
        caps: Optional[EdgeCapabilities | Dict[str, Any]] = None,
        ping_interval_s: float = 30.0,
        ping_timeout_s: float = 15.0,
        max_backoff_s: float = 30.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.server_url = str(server_url)
        self._token = token or os.environ.get("BOTEX_EDGE_TOKEN", "")
        self.workspace_root = Path(os.path.realpath(str(workspace_root)))
        self.device_id = str(device_id) or socket.gethostname()
        self.workspace_id = str(workspace_id) or self.workspace_root.name
        self.caps = (
            caps
            if isinstance(caps, EdgeCapabilities)
            else EdgeCapabilities.load(self.workspace_root, caps)
        )
        self.ping_interval_s = float(ping_interval_s)
        self.ping_timeout_s = float(ping_timeout_s)
        self.max_backoff_s = float(max_backoff_s)
        self._log = logger or _LOG
        self._idem = _IdemCache()
        self._stop = asyncio.Event()
        self._pending: set[asyncio.Task] = set()
        self._ws: Any = None
        # Rebound per connection in run_once(): asyncio primitives must not
        # cross event-loop boundaries when run_once is re-invoked.
        self._send_lock = asyncio.Lock()
        self.hello_ack: Optional[Dict[str, Any]] = None
        self._session_established = False

    # -- wire frames ---------------------------------------------------------

    def _hello_frame(self) -> Dict[str, Any]:
        return {
            "type": "hello",
            "device_id": self.device_id,
            "workspace_id": self.workspace_id,
            "capabilities": self.caps.to_dict(),
            "protocol_version": PROTOCOL_VERSION,
            "client_version": __version__,
        }

    def _handle_hello_ack(self, frame: Dict[str, Any]) -> None:
        self.hello_ack = frame
        self._session_established = True
        # Advisory only — the edge never auto-installs remote instructions.
        for key, log_fn in (
            ("min_client_version", self._log.warning),
            ("latest_client_version", self._log.info),
        ):
            required = _version_tuple(frame.get(key))
            if required and required > _version_tuple(__version__):
                log_fn(
                    "edge client %s is behind %s %s — update botex-harness on this device",
                    __version__,
                    key,
                    frame.get(key),
                )

    # -- tool dispatch (ROOT_JAIL + caps ceiling) ----------------------------

    async def handle_tool_call(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and execute one ``tool_call`` frame; return the result dict."""
        if not isinstance(tool_call, dict):
            return _err("malformed tool_call frame")
        task_id = str(tool_call.get("task_id") or "")
        call_id = str(tool_call.get("call_id") or "")
        key = (task_id, call_id) if call_id else None
        if key is not None:
            hit = self._idem.get(key)
            if hit is not None:
                return hit
        result = await self._execute_frame(tool_call, task_id)
        if key is not None:
            self._idem.put(key, result)
        return result

    async def _execute_frame(self, tool_call: Dict[str, Any], task_id: str) -> Dict[str, Any]:
        try:
            tool_name = str(tool_call.get("tool_name") or "")
            args = tool_call.get("args")
            if not isinstance(args, dict):
                args = {}
            cached_sha256 = tool_call.get("cached_sha256") or args.get("cached_sha256")
            if not tool_name:
                return _err("malformed tool_call: missing tool_name")
            result = await self._dispatch(tool_name, args, task_id, cached_sha256)
            return self._sanitize_result(result)
        except Exception as exc:
            return _err(
                f"edge internal error: {type(exc).__name__}: {mask_secrets(str(exc))[:200]}"
            )

    async def _dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        task_id: str,
        cached_sha256: Any,
    ) -> Dict[str, Any]:
        if tool_name in EDGE_NEVER_TOOLS:
            return _err(f"tool '{tool_name}' is not available on the edge")
        if tool_name not in EDGE_TOOL_NAMES:
            return _err(f"unsupported tool '{tool_name}' on the edge")
        if tool_name not in self.caps.allowed_tools:
            return _err(f"tool '{tool_name}' exceeds edge capabilities")
        if tool_name == "run_command" and not self.caps.allow_exec:
            return _err("run_command exceeds edge capabilities (allow_exec=false)")
        if tool_name in _DESTRUCTIVE_TOOLS and not self.caps.allow_destructive:
            return _err(f"{tool_name} exceeds edge capabilities (allow_destructive=false)")

        for arg_key in _PATH_ARG_KEYS:
            if args.get(arg_key) is not None:
                try:
                    resolve_safe_path(self.workspace_root, str(args[arg_key]))
                except SecurityError as se:
                    return _err(str(se))

        if cached_sha256 and tool_name in _DIGEST_TOOLS:
            unchanged = self._digest_shortcut(str(args.get("path") or ""), str(cached_sha256))
            if unchanged is not None:
                return unchanged

        root = self.workspace_root
        if tool_name == "get_file_outline":
            return await asyncio.to_thread(get_file_outline, str(args.get("path", "")), root)
        if tool_name == "read_file_lines":
            return await asyncio.to_thread(
                read_file_lines,
                str(args.get("path", "")),
                int(args.get("start", 1)),
                int(args.get("end", 50)),
                root,
            )
        if tool_name == "read_file":
            return await asyncio.to_thread(read_file, str(args.get("path", "")), root)
        if tool_name == "list_dir":
            return await asyncio.to_thread(list_dir, str(args.get("path", ".")), root)
        if tool_name == "apply_patch":
            return await asyncio.to_thread(
                apply_patch,
                str(args.get("path", "")),
                str(args.get("search_block", "")),
                str(args.get("replace_block", "")),
                root,
                task_id,
                args.get("base_sha256"),
            )
        if tool_name == "create_file":
            return await asyncio.to_thread(
                create_file, str(args.get("path", "")), str(args.get("content", "")), root, task_id
            )
        if tool_name == "delete_file":
            return await asyncio.to_thread(delete_file, str(args.get("path", "")), root, task_id)
        if tool_name == "move_file":
            return await asyncio.to_thread(
                move_file,
                str(args.get("src_path", "")),
                str(args.get("dst_path", "")),
                root,
                task_id,
            )
        # run_command — allow_exec verified above; policy comes from edge caps.
        timeout_arg = args.get("timeout_s")
        out_arg = args.get("max_output_bytes")
        try:
            timeout_s = int(timeout_arg) if timeout_arg else self.caps.exec_timeout_s
        except (TypeError, ValueError):
            timeout_s = self.caps.exec_timeout_s
        try:
            max_out = int(out_arg) if out_arg else self.caps.max_output_bytes
        except (TypeError, ValueError):
            max_out = self.caps.max_output_bytes
        return await run_command(
            str(args.get("command", "")),
            root,
            allowlist=self.caps.exec_allowlist,
            deny_args=self.caps.exec_deny_args,
            timeout_s=max(1, min(timeout_s, self.caps.exec_timeout_s)),
            max_output_bytes=max(1, min(max_out, self.caps.max_output_bytes)),
        )

    def _digest_shortcut(self, path: str, cached_sha256: str) -> Optional[Dict[str, Any]]:
        """Return the ``unchanged`` reply when the on-disk digest still matches."""
        try:
            safe_path = resolve_safe_path(self.workspace_root, path)
        except SecurityError as se:
            return _err(str(se))
        if not safe_path.is_file():
            return None  # fall through to the real read for the proper error
        try:
            current = hashlib.sha256(safe_path.read_bytes()).hexdigest()
        except OSError:
            return None
        if current != cached_sha256:
            return None
        return {"ok": True, "status": "unchanged", "path": path, "sha256": current}

    def _sanitize_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Mask every outbound string, then bound stdout/stderr to the cap."""
        masked = _mask_recursive(result if isinstance(result, dict) else {"result": result})
        budget = int(self.caps.max_output_bytes)
        marker_bytes = _TRUNCATED_MARKER.encode("utf-8")
        truncated = False
        for key in ("stdout", "stderr"):
            value = masked.get(key)
            if isinstance(value, str) and len(value.encode("utf-8")) > budget:
                keep = max(0, budget - len(marker_bytes))
                masked[key] = (
                    value.encode("utf-8")[:keep].decode("utf-8", errors="ignore")
                    + _TRUNCATED_MARKER
                )
                truncated = True
        if truncated:
            masked["truncated"] = True
        return masked

    # -- websocket session ---------------------------------------------------

    async def _send_json(self, ws: Any, frame: Dict[str, Any]) -> None:
        # Serialize sends: concurrent tool_call replies must never interleave.
        async with self._send_lock:
            await ws.send(json.dumps(frame, ensure_ascii=False))

    async def _tool_call_task(self, ws: Any, frame: Dict[str, Any]) -> None:
        call_id = str(frame.get("call_id") or "")
        try:
            result = await self.handle_tool_call(frame)
            reply = {"type": "tool_result", "call_id": call_id, "result": result}
        except Exception as exc:
            reply = {
                "type": "tool_error",
                "call_id": call_id,
                "error": f"{type(exc).__name__}: {mask_secrets(str(exc))[:200]}",
            }
        try:
            await self._send_json(ws, reply)
        except Exception:
            # The socket is gone; the cloud will redeliver the call_id after
            # reconnect and the idem ring answers replays without re-executing.
            self._log.debug("tool_result for call_id=%s lost on a dead socket", call_id)

    async def _serve(self, ws: Any) -> None:
        await self._send_json(ws, self._hello_frame())
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(frame, dict):
                continue
            ftype = frame.get("type")
            if ftype == "hello_ack":
                self._handle_hello_ack(frame)
            elif ftype == "ping":
                await self._send_json(ws, {"type": "pong", "ts": frame.get("ts")})
            elif ftype == "tool_call":
                task = asyncio.create_task(self._tool_call_task(ws, frame))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)

    async def run_once(self) -> str:
        """Run a single connect→serve session.

        Returns ``"unauthorized"`` when the gateway closes with 1008
        (bad/revoked token — do not retry), otherwise ``"closed"``.
        """
        import websockets
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        self._session_established = False
        self._send_lock = asyncio.Lock()
        auth_kwargs = _connect_headers_kwargs(
            websockets.connect, {"Authorization": f"Bearer {self._token}"}
        )
        try:
            async with websockets.connect(
                self.server_url,
                compression="deflate",
                ping_interval=self.ping_interval_s,
                ping_timeout=self.ping_timeout_s,
                **auth_kwargs,
            ) as ws:
                self._ws = ws
                try:
                    await self._serve(ws)
                finally:
                    for task in list(self._pending):
                        task.cancel()
                    self._ws = None
            return "closed"
        except ConnectionClosed as exc:
            rcvd = getattr(exc, "rcvd", None)
            sent = getattr(exc, "sent", None)
            code = getattr(rcvd, "code", None) or getattr(sent, "code", None)
            if code == 1008:
                return "unauthorized"
            return "closed"
        except InvalidStatus as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) in (401, 403):
                return "unauthorized"
            raise

    async def run_forever(self) -> None:
        """Reconnect loop with exponential backoff + jitter (1s..max_backoff_s).

        Stops on ``stop()`` and permanently on close code 1008 — a revoked or
        wrong token will never succeed, so retrying is pointless.
        """
        backoff = 1.0
        while not self._stop.is_set():
            try:
                status = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning("edge connection failed: %s", mask_secrets(str(exc))[:200])
                status = "error"
            if status == "unauthorized":
                self._log.error(
                    "edge authorization failed or token revoked (close 1008) — stopping"
                )
                return
            if self._session_established:
                backoff = 1.0  # a full handshake happened; failure was transient
            delay = min(backoff + random.uniform(0.0, backoff), self.max_backoff_s)
            backoff = min(backoff * 2.0, self.max_backoff_s)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        """Ask the reconnect loop to end; best-effort close of the socket."""
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                asyncio.ensure_future(ws.close())
            except RuntimeError:
                pass
