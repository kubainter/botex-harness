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
- ``delegate_skill`` frames run only operator-pinned skills from the opt-in
  ``delegation`` section of ``edge_caps.json`` (disabled by default).
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
import secrets
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from botex import __version__
from botex.edge.delegation import EdgeDelegationHandler, EdgeDelegationPolicy
from botex.edge.host_os_driver import (
    SingleInstanceLock,
    SingleInstanceLockError,
    default_swarm_home,
    fence_local_epoch,
)
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
    sanitize_identifier,
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

QOS_PRIORITY_CONTROL = 0
QOS_PRIORITY_DATA = 1

CONTROL_LANE_FRAME_TYPES = frozenset(
    {
        "fence_epoch",
        "fence_ack",
        "heartbeat",
        "ping",
        "pong",
        "brief_sync",
        "hello",
        "hello_ack",
    }
)


def frame_qos_priority(frame: Dict[str, Any]) -> int:
    """Return QoS lane priority: 0 for Control Lane, 1 for Data Plane."""
    ftype = str((frame or {}).get("type") or "").strip().lower()
    if ftype in CONTROL_LANE_FRAME_TYPES:
        return QOS_PRIORITY_CONTROL
    return QOS_PRIORITY_DATA


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


def get_local_bot_briefs_dir(briefs_dir: Optional[Path] = None) -> Path:
    """Resolve the local fast-cache dir for tethered-bot briefs.

    Precedence: explicit ``briefs_dir`` > ``BOTEX_SWARM_BRIEFS_DIR`` env >
    ``~/.ai/.swarm/bot_briefs``. Local filesystem only — never network.
    """
    if briefs_dir is not None:
        return Path(briefs_dir)
    env_dir = os.environ.get("BOTEX_SWARM_BRIEFS_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return Path.home().resolve() / ".ai" / ".swarm" / "bot_briefs"


def _brief_meta_field(value: Any) -> str:
    """Single-line, secret-masked scalar safe for the brief frontmatter."""
    flat = " ".join(coerce_text(value).split())
    return coerce_text(mask_secrets(flat))


def write_local_bot_brief(
    slug: str,
    *,
    namespace: str = "",
    role_desc: str = "",
    etag: str = "",
    fencing_epoch: int = 0,
    content: str = "",
    updated_at: str = "",
    briefs_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Atomically persist one bot brief as ``<slug>.md`` in the fast-cache.

    ``slug`` must survive ``sanitize_identifier`` unchanged — empty or
    traversal-bearing slugs (``..``, ``/``, ``\\``) return ``None`` without
    touching the filesystem. Stale writes (lower ``fencing_epoch`` than disk)
    and identical-ETag no-ops are skipped before touching disk.
    """
    cleaned = str(slug or "").strip()
    safe_slug = sanitize_identifier(cleaned)
    if not safe_slug or safe_slug != cleaned:
        return None
    try:
        epoch = int(fencing_epoch)
    except (TypeError, ValueError):
        epoch = 0
    clean_ns = _brief_meta_field(namespace)
    clean_role = _brief_meta_field(role_desc)[:500]
    clean_etag = _brief_meta_field(etag)
    clean_updated = _brief_meta_field(updated_at)
    target_dir = get_local_bot_briefs_dir(briefs_dir)
    target_path = target_dir / f"{safe_slug}.md"
    if target_path.is_file():
        try:
            existing = _parse_local_bot_brief(
                target_path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            existing = None
        if existing is not None:
            existing_epoch = int(existing.get("fencing_epoch") or 0)
            if existing_epoch > epoch:
                return target_path
            if (
                clean_etag
                and existing.get("etag") == clean_etag
                and existing_epoch >= epoch
                and existing.get("role_desc", "") == clean_role
            ):
                return target_path
    document = (
        "---\n"
        f"slug: {safe_slug}\n"
        f"namespace: {clean_ns}\n"
        f"role_desc: {clean_role}\n"
        f"etag: {clean_etag}\n"
        f"fencing_epoch: {epoch}\n"
        f"updated_at: {clean_updated}\n"
        "---\n\n"
        f"{coerce_text(mask_secrets(coerce_text(content)))}"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    fd_handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=target_dir,
        prefix=f".{safe_slug}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(fd_handle.name)
    try:
        with fd_handle:
            fd_handle.write(document)
            fd_handle.flush()
        os.replace(tmp_path, target_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target_path


def _parse_local_bot_brief(text: str) -> Optional[Dict[str, Any]]:
    """Parse one cached brief document; ``None`` for corrupt/empty files."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    meta: Dict[str, str] = {}
    close_idx = -1
    for idx in range(1, len(lines)):
        line = lines[idx].rstrip("\r").strip()
        if line == "---":
            close_idx = idx
            break
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    if close_idx < 0:
        return None
    slug = meta.get("slug", "").strip()
    if not slug or slug != sanitize_identifier(slug):
        return None
    body_start = close_idx + 1
    if body_start < len(lines) and not lines[body_start].strip():
        body_start += 1
    try:
        epoch = int(str(meta.get("fencing_epoch", "")).strip() or 0)
    except (TypeError, ValueError):
        epoch = 0
    return {
        "slug": slug,
        "namespace": meta.get("namespace", "").strip(),
        "role_desc": meta.get("role_desc", "").strip(),
        "etag": meta.get("etag", "").strip(),
        "fencing_epoch": epoch,
        "updated_at": meta.get("updated_at", "").strip(),
        "content": "\n".join(lines[body_start:]),
    }


def read_local_bot_briefs(
    namespace: str = "", briefs_dir: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """Read every cached brief; local FS only (0 ms, offline-safe).

    Corrupt or empty ``*.md`` files are skipped silently. When ``namespace``
    is non-empty, only entries whose frontmatter namespace matches exactly
    are returned.
    """
    target_dir = get_local_bot_briefs_dir(briefs_dir)
    want_ns = str(namespace or "")
    entries: List[Dict[str, Any]] = []
    try:
        paths = sorted(target_dir.glob("*.md"))
    except OSError:
        return entries
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        entry = _parse_local_bot_brief(text)
        if entry is None:
            continue
        if want_ns and entry.get("namespace") != want_ns:
            continue
        entries.append(entry)
    return entries


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
                    "ignoring unreadable %s (%s)",
                    candidate.name,
                    type(exc).__name__,
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
        delegation_policy: Optional[EdgeDelegationPolicy] = None,
        delegation_handler: Optional[EdgeDelegationHandler] = None,
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
        if delegation_policy is not None:
            self._delegation_policy = delegation_policy
        elif delegation_handler is not None:
            self._delegation_policy = delegation_handler.policy
        else:
            self._delegation_policy = EdgeDelegationPolicy.load(self.workspace_root)
        self._delegation_handler = delegation_handler or EdgeDelegationHandler(
            self._delegation_policy,
            self.workspace_root,
            max_output_bytes=self.caps.max_output_bytes,
        )
        self._idem = _IdemCache()
        self._stop = asyncio.Event()
        self._pending: set[asyncio.Task] = set()
        self._ws: Any = None
        # Rebound per connection in run_once(): asyncio primitives must not
        # cross event-loop boundaries when run_once is re-invoked.
        self._send_lock = asyncio.Lock()
        self._outbox: Optional[asyncio.PriorityQueue] = None
        self._sender_task: Optional[asyncio.Task] = None
        self._outbox_seq = 0
        self.hello_ack: Optional[Dict[str, Any]] = None
        self._session_established = False
        self.fenced_epochs: List[Dict[str, Any]] = []
        self._checkpoint_managers: Dict[str, Any] = {}
        self._checkpoint_min_turns = 5
        self._checkpoint_max_turns = 8
        self._loopback_server: Optional[asyncio.AbstractServer] = None
        self._loopback_port: Optional[int] = None
        self._loopback_token: str = ""

    # -- wire frames ---------------------------------------------------------

    def _hello_frame(self) -> Dict[str, Any]:
        capabilities = self.caps.to_dict()
        capabilities["delegation"] = self._delegation_policy.to_public_dict()
        return {
            "type": "hello",
            "device_id": self.device_id,
            "workspace_id": self.workspace_id,
            "capabilities": capabilities,
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

    # -- websocket session & QoS priority queue ------------------------------

    async def _qos_sender_loop(self, ws: Any, outbox: asyncio.PriorityQueue) -> None:
        """Drain outbound frames from ``outbox`` in priority order (0=Control, 1=Data)."""
        while True:
            _prio, _seq, frame, fut = await outbox.get()
            try:
                async with self._send_lock:
                    await ws.send(json.dumps(frame, ensure_ascii=False))
                if fut is not None and not fut.done():
                    fut.set_result(True)
            except Exception as exc:
                if fut is not None and not fut.done():
                    fut.set_exception(exc)
            finally:
                outbox.task_done()

    def enqueue_frame(
        self,
        frame: Dict[str, Any],
        *,
        priority: Optional[int] = None,
    ) -> asyncio.Future:
        """Queue ``frame`` on the two-tier QoS PriorityQueue and return its send future."""
        loop = asyncio.get_running_loop()
        if self._outbox is None:
            self._outbox = asyncio.PriorityQueue()
        prio = int(priority) if priority is not None else frame_qos_priority(frame)
        self._outbox_seq += 1
        fut: asyncio.Future = loop.create_future()
        self._outbox.put_nowait((prio, self._outbox_seq, frame, fut))
        return fut

    async def flush_outbox(self, ws: Any) -> List[Dict[str, Any]]:
        """Drain all currently queued frames in strict QoS priority order."""
        sent: List[Dict[str, Any]] = []
        if self._outbox is None:
            return sent
        while not self._outbox.empty():
            _prio, _seq, frame, fut = self._outbox.get_nowait()
            try:
                async with self._send_lock:
                    await ws.send(json.dumps(frame, ensure_ascii=False))
                sent.append(frame)
                if fut is not None and not fut.done():
                    fut.set_result(True)
            except Exception as exc:
                if fut is not None and not fut.done():
                    fut.set_exception(exc)
                raise
            finally:
                self._outbox.task_done()
        return sent

    def _drain_outbox(self, exc: BaseException) -> int:
        """Fail all un-sent futures in ``_outbox`` on socket teardown so callers never hang."""
        drained = 0
        if self._outbox is None:
            return drained
        while not self._outbox.empty():
            try:
                _prio, _seq, _frame, fut = self._outbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if fut is not None and not fut.done():
                fut.set_exception(exc)
            self._outbox.task_done()
            drained += 1
        return drained

    async def _send_json(
        self,
        ws: Any,
        frame: Dict[str, Any],
        *,
        priority: Optional[int] = None,
    ) -> None:
        if (
            self._outbox is not None
            and self._sender_task is not None
            and not self._sender_task.done()
        ):
            fut = self.enqueue_frame(frame, priority=priority)
            await fut
            return
        async with self._send_lock:
            await ws.send(json.dumps(frame, ensure_ascii=False))

    def _get_checkpoint_manager(
        self,
        order_id: str,
        *,
        local_epoch: int = 0,
        wt_path: Optional[Path] = None,
        swarm_remote: str = "swarm",
    ) -> Any:
        from botex.edge.adapters import EpochCheckpointManager

        clean_id = sanitize_identifier(str(order_id or "")) or str(order_id or "")
        mgr = self._checkpoint_managers.get(clean_id)
        if mgr is None:
            mgr = EpochCheckpointManager(
                order_id=clean_id,
                epoch=int(local_epoch),
                origin_repo=Path(wt_path) if wt_path is not None else self.workspace_root,
                swarm_remote=swarm_remote,
                interval=self._checkpoint_min_turns,
            )
            self._checkpoint_managers[clean_id] = mgr
        return mgr

    def record_order_turn(
        self,
        order_id: str,
        *,
        mutated_files: bool = False,
        green_test: bool = False,
    ) -> bool:
        """Track file-mutating turns for ``order_id`` via ``EpochCheckpointManager``."""
        mgr = self._get_checkpoint_manager(order_id)
        snap = mgr.record_turn(mutated_files=mutated_files, green_test=green_test)
        return bool(snap.get("should_checkpoint"))

    def auto_checkpoint_epoch(
        self,
        order_id: str,
        local_epoch: int,
        *,
        remote_epoch: Optional[int] = None,
        fetch_remote_epoch_fn: Optional[Callable[..., Any]] = None,
        wt_path: Optional[Path] = None,
        swarm_remote: str = "swarm",
        push_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        commit: bool = True,
        commit_message: str = "",
    ) -> Dict[str, Any]:
        """Delegate stage+commit+epoch-fenced push to ``EpochCheckpointManager``."""
        clean_id = sanitize_identifier(str(order_id or ""))
        if not clean_id:
            return {"ok": False, "status": "invalid_order_id", "error": "INVALID_ORDER_ID"}
        clean_remote = sanitize_identifier(swarm_remote)
        if push_fn is None and (not clean_remote or clean_remote.lower() == "origin"):
            return {
                "ok": False,
                "status": "forbidden_remote",
                "error": "Auto-push to origin is forbidden; use explicit swarm remote.",
            }
        mgr = self._get_checkpoint_manager(
            clean_id,
            local_epoch=int(local_epoch),
            wt_path=wt_path,
            swarm_remote=swarm_remote,
        )
        epoch_cb: Callable[..., Any]
        if remote_epoch is not None:
            fixed_remote = int(remote_epoch)
            epoch_cb = lambda **_kw: fixed_remote  # noqa: E731
        elif fetch_remote_epoch_fn is not None:
            user_cb = fetch_remote_epoch_fn

            def epoch_cb(order_id: str = clean_id, **_kw: Any) -> Any:
                try:
                    return user_cb(order_id=order_id)
                except TypeError:
                    return user_cb(order_id)
        else:
            fixed_local = int(local_epoch)
            epoch_cb = lambda **_kw: fixed_local  # noqa: E731

        wrapped_push_fn: Optional[Callable[..., Any]] = None
        if push_fn is not None:
            user_push = push_fn

            def wrapped_push_fn(**kw: Any) -> Dict[str, Any]:
                try:
                    return user_push(**kw)
                except TypeError:
                    return user_push(
                        Path(kw.get("repo_dir") or self.workspace_root),
                        str(kw.get("remote") or swarm_remote),
                        str(kw.get("branch") or ""),
                    )

        target_wt = Path(wt_path) if wt_path is not None else self.workspace_root
        return mgr.checkpoint_and_push(
            repo_dir=target_wt,
            remote=swarm_remote,
            order_id=clean_id,
            epoch=int(local_epoch),
            commit=commit,
            commit_message=commit_message,
            fetch_remote_epoch_fn=epoch_cb,
            push_fn=wrapped_push_fn,
        )

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

    async def _delegate_skill_task(self, ws: Any, frame: Dict[str, Any]) -> None:
        call_id = str(frame.get("call_id") or "")
        task_id = str(frame.get("task_id") or "")
        key = (task_id, f"del:{call_id}") if call_id else None
        reply = self._idem.get(key) if key is not None else None
        if reply is None:
            try:
                result = await self._delegation_handler.handle(
                    str(frame.get("skill") or ""),
                    str(frame.get("sha256") or ""),
                    frame.get("params"),
                )
                reply = {"type": "delegate_result", "call_id": call_id, "result": result}
            except Exception as exc:
                reply = {
                    "type": "delegate_error",
                    "call_id": call_id,
                    "error": f"{type(exc).__name__}: {mask_secrets(str(exc))[:200]}",
                }
            if key is not None:
                self._idem.put(key, reply)
        try:
            await self._send_json(ws, reply)
        except Exception:
            self._log.debug("delegate reply for call_id=%s lost on a dead socket", call_id)

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
                await self._send_json(
                    ws,
                    {"type": "pong", "ts": frame.get("ts")},
                    priority=QOS_PRIORITY_CONTROL,
                )
            elif ftype == "fence_epoch":
                order_id = str(frame.get("order_id") or "").strip()
                fenced_epoch = int(frame.get("fencing_epoch") or frame.get("epoch") or 0)
                stale_epoch = int(frame.get("stale_epoch") or max(0, fenced_epoch - 1))
                entry: Dict[str, Any] = {
                    "order_id": order_id,
                    "stale_epoch": stale_epoch,
                    "fencing_epoch": fenced_epoch,
                    "reason": str(frame.get("reason") or "remote_fence"),
                }
                if order_id and (self.workspace_root / ".git").exists():
                    try:
                        fence_res = await asyncio.to_thread(
                            fence_local_epoch,
                            self.workspace_root,
                            order_id,
                            stale_epoch,
                        )
                        entry["fence_result"] = fence_res
                    except Exception as exc:
                        entry["fence_error"] = type(exc).__name__
                self.fenced_epochs.append(entry)
                await self._send_json(
                    ws,
                    {
                        "type": "fence_ack",
                        "order_id": order_id,
                        "fencing_epoch": fenced_epoch,
                    },
                    priority=QOS_PRIORITY_CONTROL,
                )
            elif ftype == "tool_call":
                task = asyncio.create_task(self._tool_call_task(ws, frame))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
            elif ftype == "delegate_skill":
                task = asyncio.create_task(self._delegate_skill_task(ws, frame))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
            elif ftype == "brief_sync":
                bots = frame.get("bots")
                if isinstance(bots, list):
                    for bot in bots:
                        if not isinstance(bot, dict):
                            continue
                        try:
                            raw_slug = str(bot.get("slug") or "").strip()
                            bot_status = str(bot.get("status") or "active").strip().lower()
                            if bot_status != "active":
                                safe_slug = sanitize_identifier(raw_slug)
                                if safe_slug and safe_slug == raw_slug:
                                    (get_local_bot_briefs_dir() / f"{safe_slug}.md").unlink(
                                        missing_ok=True
                                    )
                                continue
                            write_local_bot_brief(
                                raw_slug,
                                namespace=str(bot.get("namespace") or ""),
                                role_desc=str(bot.get("role_desc") or ""),
                                etag=str(bot.get("etag") or ""),
                                fencing_epoch=bot.get("fencing_epoch", 0),
                                content=str(bot.get("content") or ""),
                                updated_at=str(bot.get("updated_at") or ""),
                            )
                        except Exception as exc:
                            self._log.warning("brief_sync entry rejected (%s)", type(exc).__name__)

    async def run_once(self) -> str:
        """Run a single connect→serve session.

        Returns ``"unauthorized"`` when the gateway closes with 1008
        (bad/revoked token — do not retry), otherwise ``"closed"``.
        """
        import websockets
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        self._session_established = False
        self._send_lock = asyncio.Lock()
        self._outbox = asyncio.PriorityQueue()
        self._outbox_seq = 0
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
                self._sender_task = asyncio.create_task(self._qos_sender_loop(ws, self._outbox))
                try:
                    await self._serve(ws)
                finally:
                    if self._sender_task is not None:
                        self._sender_task.cancel()
                        try:
                            await self._sender_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        self._sender_task = None
                    self._drain_outbox(ConnectionError("edge websocket closed"))
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
                self._log.warning("edge connection failed (%s)", type(exc).__name__)
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
        """Ask the reconnect loop to end; best-effort close of the socket and loopback IPC."""
        self._stop.set()
        if self._loopback_server is not None:
            try:
                self._loopback_server.close()
            except Exception:
                pass
        ws = self._ws
        if ws is not None:
            try:
                self._close_task = asyncio.ensure_future(ws.close())
            except RuntimeError:
                pass

    # -- 127.0.0.1 loopback IPC server (Phase 2b / Phase 3 botex-ears hook) ---

    async def start_loopback_ipc(
        self,
        *,
        port: int = 0,
        loopback_token: str = "",
        state_file: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Bind a local-only ``127.0.0.1`` IPC server with token + origin auth."""
        if not self._loopback_token:
            self._loopback_token = loopback_token or secrets.token_urlsafe(32)
        elif loopback_token:
            self._loopback_token = loopback_token

        server = await asyncio.start_server(
            self._handle_loopback_client,
            host="127.0.0.1",
            port=int(port),
        )
        self._loopback_server = server
        bound_port = int(server.sockets[0].getsockname()[1]) if server.sockets else int(port)
        self._loopback_port = bound_port

        target_state = (
            Path(state_file)
            if state_file is not None
            else (default_swarm_home() / "edge_daemon.json")
        )
        try:
            target_state.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "pid": os.getpid(),
                "host": "127.0.0.1",
                "port": bound_port,
                "loopback_token": self._loopback_token,
                "device_id": self.device_id,
                "workspace_id": self.workspace_id,
                "started_at": time.time(),
            }
            raw_json = json.dumps(payload, indent=2)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=target_state.parent,
                prefix=".edge_daemon_",
                suffix=".tmp",
                delete=False,
            ) as tmp_f:
                tmp_f.write(raw_json)
                tmp_path = Path(tmp_f.name)
            if os.name != "nt":
                try:
                    os.chmod(tmp_path, 0o600)
                except OSError:
                    pass
            os.replace(tmp_path, target_state)
        except OSError:
            pass

        return {
            "ok": True,
            "host": "127.0.0.1",
            "port": bound_port,
            "loopback_token": self._loopback_token,
            "state_file": str(target_state),
        }

    async def stop_loopback_ipc(self) -> None:
        if self._loopback_server is not None:
            self._loopback_server.close()
            try:
                await self._loopback_server.wait_closed()
            except Exception:
                pass
            self._loopback_server = None
            self._loopback_port = None

    async def _handle_loopback_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            peer = writer.get_extra_info("peername")
            peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else ""
            if peer_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                await self._write_loopback_http(
                    writer,
                    403,
                    {"ok": False, "error": "forbidden_non_loopback"},
                )
                return

            max_line_bytes = 8192
            max_headers = 64
            first_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not first_line:
                return
            if len(first_line) > max_line_bytes:
                await self._write_loopback_http(
                    writer,
                    413,
                    {"ok": False, "error": "request_line_too_long"},
                )
                return
            line_str = first_line.decode("utf-8", errors="replace").strip()

            # Support raw JSON-line IPC as well as minimal HTTP/1.1 requests.
            if line_str.startswith("{"):
                await self._handle_loopback_json_line(line_str, writer)
                return

            parts = line_str.split()
            method = parts[0].upper() if parts else "GET"
            path = parts[1] if len(parts) > 1 else "/status"
            headers: Dict[str, str] = {}
            header_count = 0
            while True:
                hline = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if len(hline) > max_line_bytes:
                    await self._write_loopback_http(
                        writer,
                        413,
                        {"ok": False, "error": "header_line_too_long"},
                    )
                    return
                if not hline or hline in (b"\r\n", b"\n"):
                    break
                header_count += 1
                if header_count > max_headers:
                    await self._write_loopback_http(
                        writer,
                        413,
                        {"ok": False, "error": "too_many_headers"},
                    )
                    return
                hstr = hline.decode("utf-8", errors="replace").strip()
                if ":" in hstr:
                    hk, hv = hstr.split(":", 1)
                    headers[hk.strip().lower()] = hv.strip()

            origin = headers.get("origin", "").strip()
            if origin:
                parsed_orig = urllib.parse.urlparse(origin)
                if parsed_orig.hostname not in ("127.0.0.1", "localhost"):
                    await self._write_loopback_http(
                        writer,
                        403,
                        {"ok": False, "error": "forbidden_origin"},
                    )
                    return

            provided_tok = headers.get("x-loopback-token", "").strip()
            if not provided_tok:
                auth_hdr = headers.get("authorization", "").strip()
                if auth_hdr.lower().startswith("bearer "):
                    provided_tok = auth_hdr[7:].strip()

            if not self._loopback_token or not secrets.compare_digest(
                provided_tok, self._loopback_token
            ):
                await self._write_loopback_http(
                    writer,
                    401,
                    {"ok": False, "error": "unauthorized_loopback_token"},
                )
                return

            content_len = int(headers.get("content-length") or 0)
            if content_len > 65536:
                await self._write_loopback_http(
                    writer,
                    413,
                    {"ok": False, "error": "body_too_large"},
                )
                return
            body_obj: Dict[str, Any] = {}
            if content_len > 0:
                raw_body = await asyncio.wait_for(
                    reader.readexactly(content_len),
                    timeout=5.0,
                )
                try:
                    parsed_body = json.loads(raw_body.decode("utf-8", errors="replace"))
                    if isinstance(parsed_body, dict):
                        body_obj = parsed_body
                except json.JSONDecodeError:
                    pass

            if path.startswith("/status") or (method == "GET" and path == "/"):
                await self._write_loopback_http(
                    writer,
                    200,
                    {
                        "ok": True,
                        "status": "running",
                        "pid": os.getpid(),
                        "device_id": self.device_id,
                        "workspace_id": self.workspace_id,
                        "session_established": self._session_established,
                        "loopback_port": self._loopback_port,
                    },
                )
                return

            if method == "POST" and path.startswith("/control"):
                frame = body_obj.get("frame")
                allowed_control = frozenset({"lease_heartbeat", "fence_ack", "ping"})
                ftype = (
                    str(frame.get("type") or "").strip().lower() if isinstance(frame, dict) else ""
                )
                if not isinstance(frame, dict) or ftype not in allowed_control:
                    await self._write_loopback_http(
                        writer,
                        400,
                        {"ok": False, "error": "forbidden_control_frame_type"},
                    )
                    return
                if self._ws is not None:
                    await self._send_json(
                        self._ws,
                        frame,
                        priority=QOS_PRIORITY_CONTROL,
                    )
                    await self._write_loopback_http(writer, 200, {"ok": True, "dispatched": True})
                    return
                await self._write_loopback_http(
                    writer,
                    200,
                    {"ok": True, "dispatched": False, "reason": "no_active_ws"},
                )
                return

            await self._write_loopback_http(writer, 404, {"ok": False, "error": "not_found"})
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_loopback_json_line(
        self,
        line_str: str,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            msg = json.loads(line_str)
        except json.JSONDecodeError:
            writer.write(b'{"ok": false, "error": "invalid_json"}\n')
            await writer.drain()
            return
        if not isinstance(msg, dict):
            writer.write(b'{"ok": false, "error": "invalid_payload"}\n')
            await writer.drain()
            return
        tok = str(msg.get("token") or msg.get("loopback_token") or "")
        if not self._loopback_token or not secrets.compare_digest(tok, self._loopback_token):
            writer.write(b'{"ok": false, "error": "unauthorized_loopback_token"}\n')
            await writer.drain()
            return
        action = str(msg.get("action") or "status").lower()
        if action == "status":
            resp = {
                "ok": True,
                "status": "running",
                "pid": os.getpid(),
                "device_id": self.device_id,
                "workspace_id": self.workspace_id,
                "loopback_port": self._loopback_port,
            }
        else:
            resp = {"ok": True, "action": action}
        writer.write((json.dumps(resp) + "\n").encode("utf-8"))
        await writer.drain()

    async def _write_loopback_http(
        self,
        writer: asyncio.StreamWriter,
        status_code: int,
        payload: Dict[str, Any],
    ) -> None:
        reasons = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            413: "Payload Too Large",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        reason = reasons.get(status_code, "OK")
        header = (
            f"HTTP/1.1 {status_code} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(header + body)
        await writer.drain()


async def async_probe_edge_daemon_loopback(
    port: int,
    loopback_token: str,
    *,
    origin: str = "",
    timeout_s: float = 1.5,
) -> Dict[str, Any]:
    """Async probe of ``127.0.0.1:<port>/status`` using ``asyncio.open_connection``."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", int(port)),
            timeout=timeout_s,
        )
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    try:
        hdrs = [
            "GET /status HTTP/1.1",
            f"Host: 127.0.0.1:{int(port)}",
            f"X-Loopback-Token: {loopback_token}",
        ]
        if origin:
            hdrs.append(f"Origin: {origin}")
        hdrs.append("Connection: close\r\n\r\n")
        writer.write("\r\n".join(hdrs).encode("utf-8"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(65536), timeout=timeout_s)
        text = raw.decode("utf-8", errors="replace")
        head, _, body = text.partition("\r\n\r\n")
        status_line = head.splitlines()[0] if head else ""
        parts = status_line.split()
        status_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 500
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {}
        if status_code != 200:
            return {
                "ok": False,
                "status_code": status_code,
                "error": f"HTTP {status_code}",
                **(parsed if isinstance(parsed, dict) else {}),
            }
        if isinstance(parsed, dict):
            parsed.setdefault("status_code", 200)
            return parsed
        return {"ok": False, "status_code": status_code, "error": "invalid_response"}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def probe_edge_daemon_loopback(
    port: int,
    loopback_token: str,
    *,
    origin: str = "",
    timeout_s: float = 1.5,
) -> Dict[str, Any]:
    """Probe a running local ``127.0.0.1:<port>`` edge daemon via token-authenticated status."""
    return asyncio.run(
        async_probe_edge_daemon_loopback(
            port,
            loopback_token,
            origin=origin,
            timeout_s=timeout_s,
        )
    )


def attach_or_spawn_edge_daemon(
    *,
    state_file: Optional[Path] = None,
    lock_path: Optional[Path] = None,
    loopback_port: int = 0,
    spawn_fn: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Attach to an existing local ``--edge-daemon`` if healthy, or acquire lock to spawn."""
    swarm_home = default_swarm_home()
    st_path = Path(state_file) if state_file is not None else (swarm_home / "edge_daemon.json")
    lk_path = Path(lock_path) if lock_path is not None else (swarm_home / "edge_daemon.lock")

    if st_path.is_file():
        try:
            state = json.loads(st_path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                existing_port = int(state.get("port") or 0)
                existing_tok = str(state.get("loopback_token") or "")
                if existing_port > 0 and existing_tok:
                    probe = probe_edge_daemon_loopback(existing_port, existing_tok)
                    if probe.get("ok"):
                        return {
                            "ok": True,
                            "mode": "attached",
                            "pid": probe.get("pid") or state.get("pid"),
                            "port": existing_port,
                            "device_id": probe.get("device_id") or state.get("device_id"),
                            "workspace_id": probe.get("workspace_id") or state.get("workspace_id"),
                        }
        except Exception:
            pass

    lock = SingleInstanceLock(lk_path)
    try:
        lock.acquire()
    except SingleInstanceLockError:
        for _attempt in range(3):
            time.sleep(0.15)
            if st_path.is_file():
                try:
                    state = json.loads(st_path.read_text(encoding="utf-8"))
                    if isinstance(state, dict):
                        existing_port = int(state.get("port") or 0)
                        existing_tok = str(state.get("loopback_token") or "")
                        if existing_port > 0 and existing_tok:
                            probe = probe_edge_daemon_loopback(existing_port, existing_tok)
                            if probe.get("ok"):
                                return {
                                    "ok": True,
                                    "mode": "attached",
                                    "pid": probe.get("pid") or state.get("pid"),
                                    "port": existing_port,
                                    "device_id": probe.get("device_id") or state.get("device_id"),
                                    "workspace_id": probe.get("workspace_id")
                                    or state.get("workspace_id"),
                                }
                except Exception:
                    pass
        return {
            "ok": True,
            "mode": "attached",
            "reason": "lock_held_by_active_daemon",
            "lock_path": str(lk_path),
        }

    if spawn_fn is not None:
        try:
            res = spawn_fn()
            return {
                "ok": True,
                "mode": "spawned",
                "lock": lock,
                "loopback_port": int(loopback_port),
                **(res if isinstance(res, dict) else {}),
            }
        except Exception:
            lock.release()
            raise

    return {
        "ok": True,
        "mode": "spawned",
        "lock": lock,
        "state_file": str(st_path),
        "lock_path": str(lk_path),
        "loopback_port": int(loopback_port),
    }
