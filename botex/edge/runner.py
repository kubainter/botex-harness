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
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from botex import __version__
from botex.edge.delegation import EdgeDelegationHandler, EdgeDelegationPolicy
from botex.edge.host_os_driver import (
    SingleInstanceLock,
    SingleInstanceLockError,
    cancel_process_tree,
    default_swarm_home,
    fence_local_epoch,
    pdeathsig_preexec,
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

TYPED_CONTROL_FRAME_TYPES = frozenset(
    {
        "harness_pause",
        "harness_resume",
        "harness_cancel_task",
        "dispatch_work_order",
    }
)
CLOUD_CONTROL_FRAME_TYPES = TYPED_CONTROL_FRAME_TYPES

LOOPBACK_CONTROL_ALLOWLIST = frozenset(
    {
        "lease_heartbeat",
        "fence_ack",
        "ping",
        "harness_pause",
        "harness_resume",
        "harness_cancel_task",
    }
)

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
        "control_ack",
        "control_nack",
        "work_order_event",
        *TYPED_CONTROL_FRAME_TYPES,
    }
)


MAX_ENROLLED_WORKSPACES = 8

_DISPATCH_WORK_ORDER_TYPE = "dispatch_work_order"
_WORK_ORDER_EVENT_TYPE = "work_order_event"
_OUTBOX_MAX_LINES = 10_000
_PID_CTIME_TOLERANCE_S = 0.01
_OUTPUT_TAIL_MAX_LINES = 200
_OUTPUT_TAIL_MAX_BYTES = 16_384
_EDIT_CONFIRM_MAX_AGE_S = 900.0
_EDIT_CONFIRM_ALLOWED_MODES = frozenset({"edit", "destructive"})
_RESERVED_CONFIRM_ACTORS = frozenset({"human", "human-curated", "operator", "curator"})

# F6: secret-bearing env vars never reach spawned harness children; the
# SWARM_* namespace is reserved for supervisor-only state.
_SCRUB_ENV_EXACT = frozenset(
    {
        "BOTEX_EDGE_TOKEN",
        "BOTEX_ACCESS_TOKEN",
        "BOTEX_CREDENTIALS_DIR",
    }
)
_SCRUB_ENV_PREFIXES = ("SWARM_",)

# Loopback POST /orders ceiling for non-local_admin workspaces (F7).
_ORDERS_ALLOWED_MODES = frozenset({"readonly", "plan"})
_ORDERS_PAID_KEYS = frozenset({"allow_expensive", "paid", "provider"})
# Bound on per-key asyncio.Lock entries for concurrent /orders idempotency.
_ORDERS_IDEM_LOCK_MAX = 256

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


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


# ---------------------------------------------------------------------------
# Durable edge state (F5 active orders + offline work_order_event outbox)
# ---------------------------------------------------------------------------


def _atomic_write_json_0600(path: Path, payload: Any) -> None:
    """Atomically write ``payload`` as JSON with owner-only permissions (0600)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    if os.name != "nt":
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        from botex.edge.credentials import _tighten_acl_windows

        _tighten_acl_windows(target)
    except Exception:
        pass


class EdgeActiveOrdersStore:
    """Durable ``active_orders.json`` keyed by ``order_id`` (F5).

    Every mutation re-reads the file first so several workspace agents of one
    daemon can share the same store without losing each other's records.
    """

    def __init__(self, path: Optional[Path]) -> None:
        self.path = Path(path) if path is not None else None
        self.idem_path = (
            self.path.with_name(f"{self.path.stem}_idem.json") if self.path is not None else None
        )

    def _read(self) -> Dict[str, Any]:
        if self.path is None or not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        orders = data.get("orders", data)
        return orders if isinstance(orders, dict) else {}

    def _write(self, orders: Dict[str, Any]) -> None:
        if self.path is None:
            return
        try:
            _atomic_write_json_0600(self.path, orders)
        except OSError as exc:
            _LOG.debug("active_orders persist failed (%s)", type(exc).__name__)

    def get(self, order_id: str) -> Optional[Dict[str, Any]]:
        rec = self._read().get(str(order_id))
        return dict(rec) if isinstance(rec, dict) else None

    def upsert(self, record: Dict[str, Any]) -> None:
        order_id = str(record.get("order_id") or "").strip()
        if not order_id:
            return
        orders = self._read()
        orders[order_id] = dict(record)
        self._write(orders)

    def update_fields(self, order_id: str, **fields: Any) -> None:
        key = str(order_id or "").strip()
        if not key:
            return
        orders = self._read()
        rec = orders.get(key)
        if not isinstance(rec, dict):
            return
        rec.update(fields)
        orders[key] = rec
        self._write(orders)

    def remove(self, order_id: str) -> None:
        key = str(order_id or "").strip()
        orders = self._read()
        if key in orders:
            del orders[key]
            self._write(orders)

    def list_orders(self) -> Dict[str, Any]:
        return {k: dict(v) for k, v in self._read().items() if isinstance(v, dict)}

    def _read_idem(self) -> Dict[str, Any]:
        if self.idem_path is None or not self.idem_path.is_file():
            return {}
        try:
            data = json.loads(self.idem_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get_idempotency(self, idem_key: str) -> Optional[Dict[str, Any]]:
        key = str(idem_key or "").strip()
        if not key:
            return None
        rec = self._read_idem().get(key)
        return dict(rec) if isinstance(rec, dict) else None

    def set_idempotency(self, idem_key: str, order_id: str, response: Dict[str, Any]) -> None:
        key = str(idem_key or "").strip()
        if not key or self.idem_path is None:
            return
        records = self._read_idem()
        records[key] = {"order_id": str(order_id or ""), "response": dict(response)}
        if len(records) > 1000:
            oldest = list(records.keys())[: len(records) - 1000]
            for old_k in oldest:
                records.pop(old_k, None)
        try:
            _atomic_write_json_0600(self.idem_path, records)
        except OSError as exc:
            _LOG.debug("active_orders_idem persist failed (%s)", type(exc).__name__)

    def last_seqs(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for oid, rec in self._read().items():
            if not isinstance(rec, dict):
                continue
            try:
                out[str(oid)] = int(rec.get("last_seq") or 0)
            except (TypeError, ValueError):
                continue
        return out


def _read_bounded_masked_tail(
    *handles: Any,
    max_lines: int = _OUTPUT_TAIL_MAX_LINES,
    max_bytes: int = _OUTPUT_TAIL_MAX_BYTES,
) -> str:
    """Seek-bounded read + secret-masking of child ``TemporaryFile`` handles."""
    chunks: List[str] = []
    seek_window = max(1024, int(max_bytes) * 2)
    for fh in handles:
        if fh is None:
            continue
        try:
            fh.flush()
            fh.seek(0, os.SEEK_END)
            size = int(fh.tell())
            fh.seek(max(0, size - seek_window), os.SEEK_SET)
            raw = fh.read()
        except Exception:
            raw = b""
        finally:
            try:
                fh.close()
            except Exception:
                pass
        if isinstance(raw, bytes):
            if len(raw) > max_bytes:
                raw = raw[-max_bytes:]
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw or "")
        if text:
            chunks.append(text)
    combined = "\n".join(chunks).strip()
    if not combined:
        return ""
    lines = combined.splitlines()[-max_lines:]
    raw_lines = "\n".join(lines).encode("utf-8", errors="replace")
    if len(raw_lines) > max_bytes:
        raw_lines = raw_lines[-max_bytes:]
    return str(mask_secrets(raw_lines.decode("utf-8", errors="replace")))


def _cleanup_order_worktree(real_root: Path, wt_path: Path) -> None:
    """Best-effort removal of a finished order worktree under ``swarm/wip``."""
    try:
        resolved_wt = wt_path.resolve()
        resolved_root = real_root.resolve()
        if resolved_wt == resolved_root or not resolved_wt.exists():
            return
    except OSError:
        return
    try:
        from botex.edge.host_os_driver import _rmtree_with_retries, _run_git

        if (resolved_root / ".git").exists():
            _run_git(
                resolved_root, ["worktree", "remove", "--force", str(resolved_wt)], check=False
            )
        if resolved_wt.exists():
            _rmtree_with_retries(resolved_wt)
    except Exception:
        pass


def _worktree_is_dirty(wt_path: Path) -> bool:
    """True when a worktree still holds uncommitted work; fails closed on errors.

    Losing unverified agent output is irreversible, so any ``git status``
    failure (missing repo, git unavailable, spawn error) is treated as dirty.
    """
    try:
        resolved = wt_path.resolve()
        if not resolved.exists():
            return False
        from botex.edge.host_os_driver import _run_git

        res = _run_git(resolved, ["status", "--porcelain"], check=False, timeout_s=30.0)
        if res.returncode != 0:
            return True
        return bool(str(res.stdout or "").strip())
    except Exception:
        return True


def _reconcile_orphan_worktrees(
    workspaces: List[Dict[str, Any]],
    active_orders: Dict[str, Any],
) -> int:
    """Prune ``swarm/wip/<order_id>/e<N>`` git worktrees that have no active order record."""
    removed = 0
    try:
        from botex.edge.host_os_driver import _list_git_worktrees, _rmtree_with_retries, _run_git
    except ImportError:
        return 0
    active_ids = {str(oid).strip() for oid in (active_orders or {}) if str(oid).strip()}
    for ws in workspaces:
        root_str = str(ws.get("root") or "").strip()
        if not root_str:
            continue
        real_root = Path(root_str)
        if not (real_root / ".git").exists():
            continue
        try:
            resolved_root = real_root.resolve()
        except OSError:
            continue
        pruned_any = False
        for entry in _list_git_worktrees(resolved_root):
            branch_ref = str(entry.get("branch") or "")
            if not branch_ref.startswith("refs/heads/swarm/wip/"):
                continue
            parts = branch_ref.split("/")
            order_id = parts[4] if len(parts) >= 5 else ""
            if not order_id or order_id in active_ids:
                continue
            wt_raw = str(entry.get("worktree") or "").strip()
            if not wt_raw:
                continue
            wt_path = Path(wt_raw).resolve()
            if wt_path == resolved_root:
                continue
            if _worktree_is_dirty(wt_path):
                # Uncommitted agent work is irrecoverable once the worktree is
                # gone — leave dirty orphans for Supervisor / checkpointing.
                _LOG.warning(
                    "reconcile: keeping dirty orphan worktree %s (uncommitted changes)",
                    wt_path,
                )
                continue
            _run_git(resolved_root, ["worktree", "remove", "--force", str(wt_path)], check=False)
            if wt_path.exists():
                _rmtree_with_retries(wt_path)
            removed += 1
            pruned_any = True
        if pruned_any:
            _run_git(resolved_root, ["worktree", "prune"], check=False)
    return removed


def _validate_edit_confirmation(confirmation: Any) -> Tuple[bool, str]:
    """Verify explicit human confirmation envelope for non-local_admin edit orders."""
    if not isinstance(confirmation, dict):
        return False, "missing_confirmation"
    actor = str(confirmation.get("confirmed_by") or "").strip()
    if not actor or actor.lower() in _RESERVED_CONFIRM_ACTORS:
        return False, "invalid_confirmed_by"
    if confirmation.get("ceiling_ack") is not True:
        return False, "missing_ceiling_ack"
    raw_ts = str(confirmation.get("confirmed_at") or "").strip()
    if not raw_ts:
        return False, "missing_confirmed_at"
    try:
        dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - dt).total_seconds()
    except (TypeError, ValueError):
        return False, "invalid_confirmed_at"
    if age_s < -60.0 or age_s > _EDIT_CONFIRM_MAX_AGE_S:
        return False, "stale_confirmation"
    return True, ""


class EdgeOutboxBuffer:
    """Bounded JSONL ring of offline ``work_order_event`` frames (10k lines)."""

    MAX_LINES = _OUTBOX_MAX_LINES

    def __init__(self, path: Optional[Path], max_lines: int = _OUTBOX_MAX_LINES) -> None:
        self.path = Path(path) if path is not None else None
        self.max_lines = max(1, int(max_lines))

    def pending(self) -> List[Dict[str, Any]]:
        if self.path is None or not self.path.is_file():
            return []
        out: List[Dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        for line in lines[-self.max_lines :]:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict):
                out.append(frame)
        return out

    def append(self, frame: Dict[str, Any]) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(frame, ensure_ascii=False)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._enforce_bound()
        except OSError as exc:
            _LOG.debug("edge_outbox append failed (%s)", type(exc).__name__)

    def _enforce_bound(self) -> None:
        try:
            if self.path is None:
                return
            with open(self.path, "rb") as fh:
                data = fh.readlines()
            if len(data) <= self.max_lines:
                return
            keep = data[-self.max_lines :]
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".edge_outbox.", suffix=".tmp"
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.writelines(keep)
                if os.name != "nt":
                    try:
                        os.chmod(tmp_path, 0o600)
                    except OSError:
                        pass
                os.replace(tmp_path, self.path)
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass

    def rewrite(self, frames: List[Dict[str, Any]]) -> None:
        if self.path is None:
            return
        lines = [json.dumps(f, ensure_ascii=False) for f in frames if isinstance(f, dict)]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".edge_outbox.", suffix=".tmp"
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    for line in lines[-self.max_lines :]:
                        fh.write(line + "\n")
                if os.name != "nt":
                    try:
                        os.chmod(tmp_path, 0o600)
                    except OSError:
                        pass
                os.replace(tmp_path, self.path)
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError as exc:
            _LOG.debug("edge_outbox rewrite failed (%s)", type(exc).__name__)

    def clear(self) -> None:
        self.rewrite([])

    def drop_event(self, order_id: str, seq: int) -> int:
        """Drop ``work_order_event`` frames matching ``(order_id, seq)``; returns count."""
        if self.path is None or not self.path.is_file():
            return 0
        frames = self.pending()
        kept: List[Dict[str, Any]] = []
        dropped = 0
        for frame in frames:
            same_key = (
                frame.get("type") == _WORK_ORDER_EVENT_TYPE
                and str(frame.get("order_id") or "") == str(order_id)
                and str(frame.get("seq") or "") == str(seq)
            )
            if same_key:
                dropped += 1
            else:
                kept.append(frame)
        if dropped:
            self.rewrite(kept)
        return dropped

    def last_seqs(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for frame in self.pending():
            if frame.get("type") != _WORK_ORDER_EVENT_TYPE:
                continue
            oid = str(frame.get("order_id") or "")
            try:
                seq = int(frame.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            if oid:
                out[oid] = max(out.get(oid, 0), seq)
        return out


# ---------------------------------------------------------------------------
# F5: OS process create-time (PID-reuse protection)
# ---------------------------------------------------------------------------


def _procfs_pid_create_time(pid: int) -> Optional[float]:
    """Linux ``/proc`` create time: boot epoch + ``starttime``/``SC_CLK_TCK``."""
    try:
        stat_text = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
        rparen = stat_text.rfind(")")
        if rparen < 0:
            return None
        fields = stat_text[rparen + 2 :].split()
        start_ticks = int(fields[19])  # stat field 22 (starttime), 0-based after comm
        hz = float(os.sysconf("SC_CLK_TCK"))
        boot_epoch = 0.0
        with open("/proc/stat", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("btime "):
                    boot_epoch = float(line.split()[1])
                    break
        if boot_epoch <= 0 or hz <= 0:
            return None
        return boot_epoch + (start_ticks / hz)
    except Exception:
        return None


def _win_pid_create_time(pid: int) -> Optional[float]:
    """Win32 ``GetProcessTimes`` create time converted to epoch seconds."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:

            class _FileTime(ctypes.Structure):
                _fields_ = [
                    ("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD),
                ]

            times = (_FileTime * 4)()
            ok = k32.GetProcessTimes(
                handle,
                ctypes.byref(times[0]),
                ctypes.byref(times[1]),
                ctypes.byref(times[2]),
                ctypes.byref(times[3]),
            )
            if not ok:
                return None
            filetime = (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)
            return (filetime - 116444736000000000) / 10_000_000.0
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def _pid_create_time(pid: Any) -> Optional[float]:
    """Best-effort OS create time for ``pid``; ``None`` when unknown or gone."""
    if isinstance(pid, bool):
        return None
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0:
        return None
    try:
        import psutil  # optional dependency — precise cross-platform source

        return float(psutil.Process(pid_int).create_time())
    except Exception:
        pass
    if sys.platform == "win32":
        return _win_pid_create_time(pid_int)
    return _procfs_pid_create_time(pid_int)


def _scrubbed_child_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Child env minus edge credentials and the whole ``SWARM_*`` namespace (F6)."""
    env = dict(base_env if base_env is not None else os.environ)
    for key in list(env):
        if key in _SCRUB_ENV_EXACT or any(key.startswith(prefix) for prefix in _SCRUB_ENV_PREFIXES):
            del env[key]
    return env


async def _write_loopback_http_response(
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


def _loopback_host_allowed(host_hdr: str, port: int) -> bool:
    """F3: only the exact bound loopback Host values pass (DNS-rebinding guard)."""
    if not host_hdr:
        return False
    normalized = host_hdr.strip().lower()
    return normalized in {f"127.0.0.1:{int(port)}", f"localhost:{int(port)}"}


@dataclass
class EdgeCapabilities:
    """Local capability ceiling advertised to and enforced against the cloud."""

    allow_exec: bool = False
    allow_destructive: bool = False
    harness_spawn: bool = False
    max_output_bytes: int = 65536
    allowed_tools: frozenset = field(default_factory=lambda: DEFAULT_ALLOWED_TOOLS)
    exec_allowlist: List[str] = field(default_factory=list)
    exec_deny_args: List[str] = field(default_factory=list)
    exec_timeout_s: int = 120

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allow_exec": bool(self.allow_exec),
            "allow_destructive": bool(self.allow_destructive),
            "harness_spawn": bool(self.harness_spawn),
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
        caps.harness_spawn = bool(data.get("harness_spawn", caps.harness_spawn))
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
        heartbeat_interval_s: float = 0.0,
        discover_harnesses_fn: Optional[Callable[[], Any]] = None,
        os_driver: Optional[Any] = None,
        workspaces_file: Optional[str | Path] = None,
        credentials_dir: Optional[str | Path] = None,
        swarm_home: Optional[str | Path] = None,
        active_orders_path: Optional[str | Path] = None,
        outbox_path: Optional[str | Path] = None,
        daemon_salt: str = "",
        daemon_gen_id: str = "",
        inventory_service: Optional[Any] = None,
        host_driver: Optional[Any] = None,
        spawn_fn: Optional[Callable[..., Any]] = None,
        translate_order_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        check_eligible_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        resolve_token_fn: Optional[Callable[..., Any]] = None,
        prepare_worktree_fn: Optional[Callable[..., Any]] = None,
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
        self.heartbeat_interval_s = max(0.0, float(heartbeat_interval_s))
        self._discover_harnesses_fn = discover_harnesses_fn
        self._os_driver = os_driver
        self._active_order_controls: Dict[str, Dict[str, Any]] = {}
        self._control_lock: Optional[asyncio.Lock] = None
        # Multi-workspace daemon plumbing (inert for single-workspace mode).
        self._workspaces_file = Path(workspaces_file) if workspaces_file is not None else None
        self._credentials_dir = Path(credentials_dir) if credentials_dir is not None else None
        self._swarm_home = Path(swarm_home) if swarm_home is not None else None
        self._active_orders_path = (
            Path(active_orders_path) if active_orders_path is not None else None
        )
        self._outbox_path = Path(outbox_path) if outbox_path is not None else None
        self._daemon_salt = str(daemon_salt or "")
        self.daemon_gen_id = str(daemon_gen_id or "")
        self._inventory_service = inventory_service
        self._host_driver = host_driver
        self._spawn_fn = spawn_fn
        self._translate_order_fn = translate_order_fn
        self._check_eligible_fn = check_eligible_fn
        self._resolve_token_fn = resolve_token_fn
        self._prepare_worktree_fn = prepare_worktree_fn
        self._store = (
            EdgeActiveOrdersStore(self._active_orders_path)
            if self._active_orders_path is not None
            else None
        )
        self._outbox_buffer = (
            EdgeOutboxBuffer(self._outbox_path) if self._outbox_path is not None else None
        )
        self._order_seqs: Dict[str, int] = {}
        self._order_procs: Dict[str, Any] = {}
        self._pending_cloud_acks: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._last_argv_error: str = ""
        self._draining = False

    # -- active order control registry & harness discovery -------------------

    def register_active_order(
        self,
        order_id: str,
        *,
        pid: int = 0,
        job_guard: Any = None,
        fencing_token: str = "",
        fencing_epoch: int = 0,
        pid_create_time: Optional[float] = None,
        workspace_id: str = "",
        worktree_path: Optional[Path] = None,
        real_root: Optional[Path] = None,
    ) -> None:
        key = str(order_id or "").strip()
        if not key:
            return
        pid_int = int(pid or 0)
        # F5: capture the OS create time once so later signals can detect PID
        # reuse; ``None`` stays ``None`` (legacy callers, gone/unsupported).
        if pid_int > 0 and pid_create_time is None:
            pid_create_time = _pid_create_time(pid_int)
        entry = {
            "pid": pid_int,
            "pid_create_time": pid_create_time,
            "job_guard": job_guard,
            "fencing_token": str(fencing_token or ""),
            "fencing_epoch": int(fencing_epoch or 0),
            "workspace_id": str(workspace_id or self.workspace_id),
            "worktree_path": str(worktree_path) if worktree_path is not None else "",
            "real_root": str(real_root) if real_root is not None else "",
            "stale": False,
        }
        self._active_order_controls[key] = entry
        if self._store is not None:
            self._store.upsert(
                {
                    "order_id": key,
                    "workspace_id": entry["workspace_id"],
                    "pid": pid_int,
                    "pid_create_time": pid_create_time,
                    "daemon_gen_id": self.daemon_gen_id,
                    "fencing_token": entry["fencing_token"],
                    "fencing_epoch": entry["fencing_epoch"],
                    "last_seq": int(self._order_seqs.get(key, 0)),
                }
            )

    def unregister_active_order(self, order_id: str) -> None:
        key = str(order_id or "").strip()
        if key:
            self._active_order_controls.pop(key, None)
            self._order_procs.pop(key, None)
            if self._store is not None:
                self._store.remove(key)

    def _mark_order_stale(self, order_id: str, entry: Dict[str, Any]) -> None:
        entry["stale"] = True
        if self._store is not None:
            self._store.update_fields(order_id, stale=True)

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _collect_harness_inventory(self) -> List[Dict[str, Any]]:
        fn = self._discover_harnesses_fn
        if fn is None:
            try:
                from botex.edge.adapters import discover_local_harnesses

                fn = discover_local_harnesses
            except Exception:
                return []
        try:
            res = fn()
            if inspect.isawaitable(res):
                res = await asyncio.wait_for(res, timeout=3.0)
            if isinstance(res, list):
                return [item for item in res if isinstance(item, dict)]
        except Exception as exc:
            self._log.debug("harness discovery skipped (%s)", type(exc).__name__)
        return []

    async def build_hello_frame(self) -> Dict[str, Any]:
        frame = self._hello_frame()
        frame["last_seen"] = self._utc_now_iso()
        frame["harnesses"] = await self._collect_harness_inventory()
        return frame

    async def build_heartbeat_frame(self) -> Dict[str, Any]:
        return {
            "type": "heartbeat",
            "device_id": self.device_id,
            "workspace_id": self.workspace_id,
            "last_seen": self._utc_now_iso(),
            "harnesses": await self._collect_harness_inventory(),
        }

    async def handle_control_frame(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        """Validate fencing token and execute an allowlisted typed control frame."""
        if self._control_lock is None:
            self._control_lock = asyncio.Lock()
        async with self._control_lock:
            return await self._handle_control_frame_locked(frame)

    async def _handle_control_frame_locked(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        ftype = str((frame or {}).get("type") or "").strip().lower()
        req_id = str((frame or {}).get("request_id") or "").strip()
        order_id = str((frame or {}).get("order_id") or "").strip()

        def _nack(reason: str, **extra: Any) -> Dict[str, Any]:
            out: Dict[str, Any] = {
                "type": "control_nack",
                "request_id": req_id,
                "order_id": order_id,
                "action": ftype,
                "ok": False,
                "reason": reason,
            }
            out.update(extra)
            return out

        if ftype not in TYPED_CONTROL_FRAME_TYPES:
            return _nack("unsupported_control_frame")
        if not order_id:
            return _nack("missing_order_id")

        provided_token = str(frame.get("fencing_token") or "").strip()
        if not provided_token:
            return _nack("missing_fencing_token")

        if ftype == _DISPATCH_WORK_ORDER_TYPE:
            return await self._handle_dispatch_work_order(frame, _nack)

        entry = self._active_order_controls.get(order_id)
        if entry is None:
            return _nack("order_not_active")

        expected_token = str(entry.get("fencing_token") or "")
        if not expected_token or not secrets.compare_digest(provided_token, expected_token):
            return _nack("fencing_token_mismatch")

        if "fencing_epoch" in frame and frame.get("fencing_epoch") is not None:
            try:
                provided_epoch = int(frame["fencing_epoch"])
            except (TypeError, ValueError):
                return _nack("invalid_fencing_epoch")
            if provided_epoch < int(entry.get("fencing_epoch") or 0):
                return _nack("stale_fencing_epoch")

        pid = int(entry.get("pid") or 0)
        # F5: refuse to signal a reused/gone PID — the recorded create time must
        # still match the live process before pause/resume/cancel goes out.
        stored_ct = entry.get("pid_create_time")
        if stored_ct is not None and pid > 0:
            live_ct = _pid_create_time(pid)
            try:
                ctime_ok = live_ct is not None and (
                    abs(live_ct - float(stored_ct)) <= _PID_CTIME_TOLERANCE_S
                )
            except (TypeError, ValueError):
                ctime_ok = False
            if not ctime_ok:
                self._mark_order_stale(order_id, entry)
                return _nack("stale_pid")

        job_guard = entry.get("job_guard")

        driver = self._os_driver
        if driver is None:
            from botex.edge import host_os_driver as driver

        method_map = {
            "harness_pause": "pause_process_tree",
            "harness_resume": "resume_process_tree",
            "harness_cancel_task": "cancel_process_tree",
        }
        helper = getattr(driver, method_map[ftype], None)
        if helper is None:
            return _nack("os_driver_method_unavailable")

        try:
            res = await asyncio.to_thread(helper, pid, job_guard=job_guard)
        except Exception as exc:
            return _nack("os_driver_error", error=type(exc).__name__)

        if not isinstance(res, dict) or not res.get("ok"):
            return _nack(
                str((res or {}).get("detail") or "control_action_failed")
                if isinstance(res, dict)
                else "control_action_failed",
                result=res if isinstance(res, dict) else {},
            )

        if ftype == "harness_cancel_task":
            self.unregister_active_order(order_id)

        return {
            "type": "control_ack",
            "request_id": req_id,
            "order_id": order_id,
            "action": ftype,
            "fencing_token_verified": True,
            "ok": True,
            "result": res,
        }

    # -- downward work-order dispatch ----------------------------------------

    async def _handle_dispatch_work_order(
        self,
        frame: Dict[str, Any],
        _nack: Callable[..., Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Re-verify, preflight, and spawn a work order for an enrolled workspace."""
        req_id = str((frame or {}).get("request_id") or "").strip()
        order_id = str((frame or {}).get("order_id") or "").strip()
        if self._draining:
            return _nack("draining")
        if not bool(getattr(self.caps, "harness_spawn", False)):
            return _nack("harness_spawn_disabled")

        workspace_id = str(frame.get("workspace_id") or self.workspace_id).strip()
        fencing_token = str(frame.get("fencing_token") or "").strip()
        try:
            fencing_epoch = int(frame.get("fencing_epoch") or 0)
        except (TypeError, ValueError):
            fencing_epoch = 0

        try:
            from botex.edge.workspaces import (
                get_workspace,
                validate_workspace_root_realpath,
            )
        except ImportError:
            return _nack("workspaces_module_unavailable")

        # Re-verified immediately before spawn: enrolled state + token + root.
        ws_entry = get_workspace(workspace_id, path=self._workspaces_file)
        if ws_entry is None or str(ws_entry.get("state") or "") != "enrolled":
            return _nack("workspace_not_enrolled")

        resolve_fn = self._resolve_token_fn
        if resolve_fn is None:
            try:
                from botex.edge.credentials import resolve_edge_token as resolve_fn
            except ImportError:
                resolve_fn = None
        token = ""
        if resolve_fn is not None:
            try:
                resolved = resolve_fn(self.device_id, workspace_id, base_dir=self._credentials_dir)
                token = str(resolved[0] if isinstance(resolved, (list, tuple)) else resolved)
            except Exception:
                token = ""
        if not token:
            return _nack("missing_edge_token")

        try:
            real_root = validate_workspace_root_realpath(ws_entry.get("root") or "")
        except Exception:
            return _nack("invalid_workspace_root")

        order = frame.get("order") if isinstance(frame.get("order"), dict) else {}
        target_harness = (
            str(
                frame.get("target_harness")
                or frame.get("harness")
                or order.get("target_harness")
                or "botex"
            ).strip()
            or "botex"
        )

        eligible_fn = self._check_eligible_fn
        if eligible_fn is None:
            from botex.edge.adapters import check_harness_eligible as eligible_fn
        try:
            elig = eligible_fn(target_harness)
        except Exception as exc:
            return _nack("harness_ineligible", error=type(exc).__name__)
        if not (isinstance(elig, dict) and elig.get("ok")):
            detail = (
                str(elig.get("reason") or "ineligible") if isinstance(elig, dict) else "ineligible"
            )
            return _nack("harness_ineligible", detail=detail)

        translate_fn = self._translate_order_fn
        if translate_fn is None:
            from botex.edge.adapters import translate_work_order as translate_fn
        try:
            if frame.get("mode_default") is not None and self._translate_order_fn is None:
                spec = translate_fn(
                    order, target_harness, mode_default=str(frame.get("mode_default"))
                )
            else:
                spec = translate_fn(order, target_harness)
        except Exception as exc:
            return _nack("translate_failed", error=type(exc).__name__)
        if not (isinstance(spec, dict) and spec.get("ok")):
            detail = (
                str(spec.get("reason") or "translate_failed")
                if isinstance(spec, dict)
                else "translate_failed"
            )
            return _nack("translate_failed", detail=detail)

        driver = self._host_driver
        if driver is None:
            from botex.edge.host_os_driver import HostOSDriver

            driver = self._host_driver = HostOSDriver()
        try:
            pre = await asyncio.to_thread(
                driver.preflight_runtime, str(spec.get("harness") or target_harness)
            )
        except Exception as exc:
            return _nack("preflight_error", error=type(exc).__name__)
        if not (isinstance(pre, dict) and pre.get("ok")):
            detail = (
                str(pre.get("reason") or "runtime_blocked")
                if isinstance(pre, dict)
                else "runtime_blocked"
            )
            return _nack("runtime_blocked", detail=detail)

        spec_mode = str(spec.get("mode") or "").strip().lower()
        req_mode = str(spec.get("mode_request") or "").strip().lower()
        has_git = (Path(real_root) / ".git").exists()
        is_read_only = spec_mode in {"readonly", "plan"} or req_mode in {"readonly", "plan"}
        # Fail closed for every mutating mode (edit / destructive / full / yolo):
        # without git there is no epoch worktree, no diff review, no rollback.
        if not is_read_only and not has_git:
            return _nack("non_git_edit_unsupported")

        wt_path = Path(real_root)
        try:
            epoch = int(spec.get("epoch") or fencing_epoch or 0)
        except (TypeError, ValueError):
            epoch = 0
        if not is_read_only and has_git and epoch > 0:
            prep_fn = self._prepare_worktree_fn or driver.prepare_epoch_worktree
            try:
                prepared = await asyncio.to_thread(
                    prep_fn,
                    Path(real_root),
                    order_id,
                    epoch,
                    base_ref=str(spec.get("base_ref") or "HEAD"),
                    swarm_remote="swarm",
                )
                if prepared:
                    wt_path = Path(prepared)
            except Exception as exc:
                return _nack("worktree_failed", error=type(exc).__name__)

        argv = self._build_spawn_argv(spec, order, token)
        if not argv:
            return _nack(self._last_argv_error or "invalid_spawn_argv")
        env = _scrubbed_child_env()

        guard: Any = None
        proc: Any = None
        pid = 0
        try:
            if self._spawn_fn is not None:
                spawned = self._spawn_fn(argv=argv, cwd=wt_path, env=env)
                proc, pid, guard = self._coerce_spawn_result(spawned)
            else:
                proc, guard = self._spawn_child(argv, wt_path, env)
                pid = int(proc.pid) if proc is not None else 0
        except Exception as exc:
            if guard is not None:
                try:
                    guard.kill_all()
                except Exception:
                    pass
            return _nack("spawn_failed", error=type(exc).__name__)
        if pid <= 0:
            return _nack("spawn_failed")

        self.register_active_order(
            order_id,
            pid=pid,
            job_guard=guard,
            fencing_token=fencing_token,
            fencing_epoch=fencing_epoch,
            workspace_id=workspace_id,
            worktree_path=wt_path if wt_path != Path(real_root) else None,
            real_root=Path(real_root),
        )
        if proc is not None:
            self._order_procs[order_id] = proc
            watcher = asyncio.create_task(self._watch_order_process(order_id, proc))
            self._pending.add(watcher)
            watcher.add_done_callback(self._pending.discard)

        self.emit_work_order_event(
            order_id,
            "started",
            {
                "pid": pid,
                "harness": str(spec.get("harness") or target_harness),
                "branch": str(spec.get("branch") or ""),
                "worktree": str(spec.get("worktree") or ""),
            },
        )
        return {
            "type": "control_ack",
            "request_id": req_id,
            "order_id": order_id,
            "ok": True,
            "pid": pid,
            "spec_echo": _mask_recursive(spec),
        }

    def _build_spawn_argv(
        self,
        spec: Dict[str, Any],
        order: Dict[str, Any],
        token: str,
    ) -> List[str]:
        self._last_argv_error = ""
        harness = str((spec or {}).get("harness") or "").strip() or "botex"
        raw: Optional[List[Any]] = None
        for cand in (
            spec.get("argv") if isinstance(spec, dict) else None,
            order.get("argv") if isinstance(order, dict) else None,
        ):
            if isinstance(cand, (list, tuple)) and cand:
                raw = list(cand)
                break
        if raw is None:
            try:
                from botex.edge.adapters import (
                    DEFAULT_DISCOVERY_CLI_ALLOWLIST,
                    build_harness_argv,
                )

                cli = DEFAULT_DISCOVERY_CLI_ALLOWLIST.get(harness, harness)
                if (
                    self._translate_order_fn is not None
                    and not str(spec.get("prompt") or "").strip()
                ):
                    raw = [cli]
                else:
                    resolved_cli = shutil.which(str(cli))
                    if not resolved_cli:
                        self._last_argv_error = "cli_not_found"
                        return []
                    raw = build_harness_argv(spec, cli=resolved_cli)
            except ValueError:
                raw = None
            except Exception:
                raw = None
        if not raw:
            self._last_argv_error = "invalid_spawn_argv"
            return []
        argv = [str(a) for a in raw if str(a).strip()]
        # F6: credential material must never reach argv.
        if token:
            argv = [a for a in argv if token not in a]
        return argv

    @staticmethod
    def _coerce_spawn_result(spawned: Any) -> Tuple[Any, int, Any]:
        """Normalize ``_spawn_fn`` returns to ``(proc, pid, job_guard)``."""
        if isinstance(spawned, dict):
            proc = spawned.get("proc")
            pid = int(spawned.get("pid") or getattr(proc, "pid", 0) or 0)
            return proc, pid, spawned.get("job_guard")
        if spawned is None:
            return None, 0, None
        pid = int(getattr(spawned, "pid", 0) or (spawned if isinstance(spawned, int) else 0))
        proc = spawned if hasattr(spawned, "wait") else None
        return proc, pid, None

    def _spawn_child(
        self,
        argv: List[str],
        cwd: Path,
        env: Dict[str, str],
        *,
        capture: bool = True,
    ) -> Tuple[Any, Any]:
        """Spawn the harness child in a new POSIX session / Win32 process group."""
        stdout_fh = tempfile.TemporaryFile(mode="w+b") if capture else None
        stderr_fh = tempfile.TemporaryFile(mode="w+b") if capture else None
        kwargs: Dict[str, Any] = {
            "cwd": str(cwd),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": stdout_fh if stdout_fh is not None else subprocess.DEVNULL,
            "stderr": stderr_fh if stderr_fh is not None else subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
            preexec = pdeathsig_preexec()
            if preexec is not None:
                kwargs["preexec_fn"] = preexec
        try:
            proc = subprocess.Popen(argv, **kwargs)
        except Exception:
            for fh in (stdout_fh, stderr_fh):
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
            raise
        if capture:
            try:
                proc._botex_capture_fhs = (stdout_fh, stderr_fh)  # type: ignore[attr-defined]
            except Exception:
                pass
        guard = None
        try:
            from botex.edge.host_os_driver import WindowsJobGuard

            candidate = WindowsJobGuard()
            try:
                candidate.assign(int(proc.pid))
                guard = candidate
            except Exception:
                try:
                    candidate.kill_all()
                except Exception:
                    pass
        except Exception:
            guard = None
        return proc, guard

    async def _watch_order_process(self, order_id: str, proc: Any) -> None:
        try:
            rc = await asyncio.to_thread(proc.wait)
        except Exception:
            rc = None
        capture_fhs = getattr(proc, "_botex_capture_fhs", ())
        summary = ""
        if capture_fhs:
            summary = await asyncio.to_thread(_read_bounded_masked_tail, *capture_fhs)
            if self._store is not None and summary:
                self._store.update_fields(order_id, output_summary=summary)
        entry = self._active_order_controls.get(order_id) or {}
        wt_str = str(entry.get("worktree_path") or "").strip()
        root_str = str(entry.get("real_root") or "").strip()
        term_status = str(entry.get("terminal_status") or "").strip().upper()
        if wt_str and root_str:
            dirty = await asyncio.to_thread(_worktree_is_dirty, Path(wt_str))
            # Destroy the worktree only for explicitly verified terminal
            # statuses or when nothing uncommitted remains; dirty state after a
            # failed run stays for Supervisor checkpointing / recovery.
            if term_status in {"DONE", "CANCELLED"} or not dirty:
                await asyncio.to_thread(_cleanup_order_worktree, Path(root_str), Path(wt_str))
        if self._stop.is_set() or self._draining:
            return
        if order_id in self._active_order_controls:
            ev_data: Dict[str, Any] = {"returncode": rc}
            if summary:
                ev_data["summary"] = summary
            self.emit_work_order_event(order_id, "exited", ev_data)
            self.unregister_active_order(order_id)

    async def register_work_order_with_cloud(
        self,
        payload: Dict[str, Any],
        *,
        timeout_s: float = 10.0,
    ) -> Dict[str, Any]:
        """Register a loopback-originated WorkOrder with Cloud before local dispatch."""
        if self._ws is None:
            return {"ok": False, "error": "registration_unavailable"}
        req_id = str(payload.get("request_id") or f"reg-{secrets.token_hex(4)}").strip()
        frame = {
            "type": "register_work_order",
            "request_id": req_id,
            "order_id": str(payload.get("order_id") or "").strip(),
            "workspace_id": str(payload.get("workspace_id") or self.workspace_id).strip(),
            "goal": str(payload.get("goal") or "").strip(),
            "mode_request": str(payload.get("mode_request") or "readonly").strip(),
        }
        if payload.get("target_harness"):
            frame["target_harness"] = str(payload["target_harness"]).strip()
        if isinstance(payload.get("handoff_policy"), dict):
            frame["handoff_policy"] = payload["handoff_policy"]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending_cloud_acks[req_id] = fut
        try:
            await self._send_json(self._ws, frame, priority=QOS_PRIORITY_CONTROL)
            reply = await asyncio.wait_for(fut, timeout=max(0.1, float(timeout_s)))
            return (
                dict(reply)
                if isinstance(reply, dict)
                else {"ok": False, "error": "registration_unavailable"}
            )
        except Exception:
            return {"ok": False, "error": "registration_unavailable"}
        finally:
            self._pending_cloud_acks.pop(req_id, None)

    # -- work_order_event sequence & durable outbox --------------------------

    def _next_order_seq(self, order_id: str) -> int:
        seq = int(self._order_seqs.get(order_id, 0)) + 1
        self._order_seqs[order_id] = seq
        if self._store is not None:
            self._store.update_fields(order_id, last_seq=seq)
        return seq

    def emit_work_order_event(
        self,
        order_id: str,
        kind: str,
        data: Any = None,
    ) -> Dict[str, Any]:
        """Emit a masked ``work_order_event``; buffer to disk while offline."""
        seq = self._next_order_seq(order_id)
        payload = (
            data if isinstance(data, (dict, list)) else ({} if data is None else {"value": data})
        )
        frame: Dict[str, Any] = {
            "type": _WORK_ORDER_EVENT_TYPE,
            "order_id": str(order_id),
            "workspace_id": self.workspace_id,
            "seq": seq,
            "kind": str(kind or ""),
            "ts": self._utc_now_iso(),
            "data": _mask_recursive(payload),
        }
        if self._ws is not None and self._session_established:
            try:
                fut = self.enqueue_frame(frame, priority=QOS_PRIORITY_CONTROL)
                fut.add_done_callback(lambda f: f.cancelled() or f.exception())
                return frame
            except Exception:
                pass
        if self._outbox_buffer is not None:
            self._outbox_buffer.append(frame)
        return frame

    async def _post_hello_ack(self, ws: Any) -> None:
        """Hook run after ``hello_ack``; agents replay the durable outbox here."""

    def _extra_reconnect_delay_s(self) -> float:
        """Hook for per-agent salted reconnect jitter; base has none."""
        return 0.0

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

    async def _heartbeat_loop(self, ws: Any) -> None:
        while not self._stop.is_set() and self.heartbeat_interval_s > 0:
            await asyncio.sleep(self.heartbeat_interval_s)
            if self._stop.is_set():
                break
            hb = await self.build_heartbeat_frame()
            await self._send_json(ws, hb, priority=QOS_PRIORITY_CONTROL)

    async def _serve(self, ws: Any) -> None:
        await self._send_json(ws, await self.build_hello_frame(), priority=QOS_PRIORITY_CONTROL)
        hb_task: Optional[asyncio.Task] = None
        if self.heartbeat_interval_s > 0:
            hb_task = asyncio.create_task(self._heartbeat_loop(ws))
        try:
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
                    await self._post_hello_ack(ws)
                elif ftype == "ping":
                    await self._send_json(
                        ws,
                        {"type": "pong", "ts": frame.get("ts")},
                        priority=QOS_PRIORITY_CONTROL,
                    )
                elif ftype in TYPED_CONTROL_FRAME_TYPES:
                    reply = await self.handle_control_frame(frame)
                    await self._send_json(ws, reply, priority=QOS_PRIORITY_CONTROL)
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
                                self._log.warning(
                                    "brief_sync entry rejected (%s)", type(exc).__name__
                                )
                elif ftype in ("control_ack", "control_nack"):
                    req_id = str(frame.get("request_id") or "").strip()
                    fut = self._pending_cloud_acks.pop(req_id, None) if req_id else None
                    if fut is not None and not fut.done():
                        fut.set_result(frame)
                    if (
                        ftype == "control_nack"
                        and str(frame.get("action") or "") == _WORK_ORDER_EVENT_TYPE
                        and str(frame.get("error") or frame.get("reason") or "") == "unknown_order"
                    ):
                        # Cloud is the lifecycle authority: a rejected event will
                        # never be accepted on replay, so drop it from the
                        # durable outbox instead of resending it forever.
                        try:
                            nack_seq = int(frame.get("seq") or 0)
                        except (TypeError, ValueError):
                            nack_seq = 0
                        nack_oid = str(frame.get("order_id") or "").strip()
                        if nack_oid and self._outbox_buffer is not None:
                            dropped = self._outbox_buffer.drop_event(nack_oid, nack_seq)
                            if dropped:
                                self._log.warning(
                                    "cloud rejected work_order_event order=%s seq=%s "
                                    "as unknown_order — dropped %d stale outbox frame(s)",
                                    nack_oid,
                                    nack_seq,
                                    dropped,
                                )
        finally:
            for req_id, fut in list(self._pending_cloud_acks.items()):
                self._pending_cloud_acks.pop(req_id, None)
                if not fut.done():
                    fut.set_result({"ok": False, "error": "registration_unavailable"})
            if hb_task is not None:
                hb_task.cancel()
                try:
                    await hb_task
                except (asyncio.CancelledError, Exception):
                    pass

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
            delay = (
                min(backoff + random.uniform(0.0, backoff), self.max_backoff_s)
                + self._extra_reconnect_delay_s()
            )
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

            # F3: DNS-rebinding guard — Host must be exactly the bound loopback.
            if not _loopback_host_allowed(headers.get("host", ""), int(self._loopback_port or 0)):
                await self._write_loopback_http(
                    writer,
                    403,
                    {"ok": False, "error": "forbidden_host_header"},
                )
                return

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
                ftype = (
                    str(frame.get("type") or "").strip().lower() if isinstance(frame, dict) else ""
                )
                if not isinstance(frame, dict) or ftype not in LOOPBACK_CONTROL_ALLOWLIST:
                    await self._write_loopback_http(
                        writer,
                        400,
                        {"ok": False, "error": "forbidden_control_frame_type"},
                    )
                    return
                if ftype in TYPED_CONTROL_FRAME_TYPES:
                    if (
                        not str(frame.get("fencing_token") or "").strip()
                        or not str(frame.get("order_id") or "").strip()
                    ):
                        await self._write_loopback_http(
                            writer,
                            400,
                            {"ok": False, "error": "missing_fencing_token_or_order_id"},
                        )
                        return
                    if body_obj.get("local_exec"):
                        ctrl_reply = await self.handle_control_frame(frame)
                        status_code = 200 if ctrl_reply.get("ok") else 409
                        await self._write_loopback_http(writer, status_code, ctrl_reply)
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
        await _write_loopback_http_response(writer, status_code, payload)


class EdgeWorkspaceAgent(EdgeRunner):
    """One enrolled workspace supervised by :class:`EdgeHostDaemon`.

    Adds registry context, the shared harness-inventory cache, salted
    reconnect jitter, and durable order/outbox persistence on top of the
    single-workspace ``EdgeRunner`` (whose API stays fully intact).
    """

    def __init__(
        self,
        server_url: str,
        *,
        workspace: Optional[Dict[str, Any]] = None,
        daemon_salt: str = "",
        daemon_gen_id: str = "",
        inventory_service: Optional[Any] = None,
        **runner_kwargs: Any,
    ) -> None:
        ws = dict(workspace or {})
        runner_kwargs.setdefault("workspace_root", ws.get("root") or ".")
        runner_kwargs.setdefault("workspace_id", str(ws.get("workspace_id") or ""))
        runner_kwargs.setdefault("daemon_salt", daemon_salt)
        runner_kwargs.setdefault("daemon_gen_id", daemon_gen_id)
        runner_kwargs.setdefault("inventory_service", inventory_service)
        super().__init__(server_url, **runner_kwargs)
        self.workspace = ws
        swarm_home = self._swarm_home or default_swarm_home()
        if self._active_orders_path is None:
            self._active_orders_path = swarm_home / "active_orders.json"
        if self._outbox_path is None:
            self._outbox_path = swarm_home / "edge_outbox.jsonl"
        self._store = EdgeActiveOrdersStore(self._active_orders_path)
        self._outbox_buffer = EdgeOutboxBuffer(self._outbox_path)
        self._seed_order_seqs()

    def _seed_order_seqs(self) -> None:
        """Boot-seed ``last_seq`` per order so ``(order_id, seq)`` never resets."""
        seed: Dict[str, int] = {}
        if self._store is not None:
            seed.update(self._store.last_seqs())
        if self._outbox_buffer is not None:
            for oid, seq in self._outbox_buffer.last_seqs().items():
                seed[oid] = max(seed.get(oid, 0), seq)
        for oid, seq in seed.items():
            self._order_seqs[oid] = max(self._order_seqs.get(oid, 0), int(seq))

    async def _collect_harness_inventory(self) -> List[Dict[str, Any]]:
        svc = self._inventory_service
        if svc is not None:
            try:
                snap = svc.get_cached_snapshot()
                return list(snap) if isinstance(snap, list) else []
            except Exception:
                return []
        return await super()._collect_harness_inventory()

    def _workspaces_manifest(self) -> List[Dict[str, Any]]:
        try:
            from botex.edge.workspaces import (
                load_workspaces_registry,
                to_wire_manifest,
            )

            return to_wire_manifest(load_workspaces_registry(self._workspaces_file))
        except Exception:
            return []

    async def build_hello_frame(self) -> Dict[str, Any]:
        frame = await super().build_hello_frame()
        frame["workspaces_manifest"] = self._workspaces_manifest()
        return frame

    async def build_heartbeat_frame(self) -> Dict[str, Any]:
        frame = await super().build_heartbeat_frame()
        frame["workspaces_manifest"] = self._workspaces_manifest()
        return frame

    def _extra_reconnect_delay_s(self) -> float:
        """Deterministic per-boot salted jitter ``sha256(ws:salt) % 10`` seconds."""
        if not self._daemon_salt:
            return 0.0
        digest = hashlib.sha256(f"{self.workspace_id}:{self._daemon_salt}".encode("utf-8")).digest()
        return float(int.from_bytes(digest[:4], "big") % 10)

    async def _post_hello_ack(self, ws: Any) -> None:
        """Replay durable outbox events in ``(order_id, seq)`` order on reconnect."""
        buf = self._outbox_buffer
        if buf is None:
            return
        pending = buf.pending()
        if not pending:
            return

        def _seq(frame: Dict[str, Any]) -> Tuple[str, int]:
            try:
                return (str(frame.get("order_id") or ""), int(frame.get("seq") or 0))
            except (TypeError, ValueError):
                return (str(frame.get("order_id") or ""), 0)

        mine = sorted(
            (f for f in pending if str(f.get("workspace_id") or "") == self.workspace_id),
            key=_seq,
        )
        others = [f for f in pending if str(f.get("workspace_id") or "") != self.workspace_id]
        if not mine:
            return
        unsent: List[Dict[str, Any]] = []
        sent_any = False
        for idx, frame in enumerate(mine):
            try:
                await self._send_json(ws, frame, priority=QOS_PRIORITY_CONTROL)
                sent_any = True
            except Exception:
                unsent = mine[idx:]
                break
        if sent_any or not unsent:
            buf.rewrite(others + unsent)

    def stop(self) -> None:
        self._draining = True
        super().stop()


class EdgeHostDaemon:
    """Multi-workspace edge host daemon (Plan 051).

    Supervises up to ``MAX_ENROLLED_WORKSPACES`` enrolled workspaces from
    ``host_workspaces.json`` — one WSS session each — behind one shared
    ``HarnessInventoryService``, one ``SingleInstanceLock``, and one hardened
    ``127.0.0.1`` loopback IPC endpoint. ``candidate``/``disabled`` workspaces
    never get a session.
    """

    def __init__(
        self,
        server_url: str,
        *,
        workspaces_file: Optional[str | Path] = None,
        credentials_dir: Optional[str | Path] = None,
        loopback_port: int = 0,
        loopback_token: str = "",
        allow_exec: bool = False,
        allow_destructive: bool = False,
        harness_spawn: bool = False,
        heartbeat_interval_s: float = 30.0,
        ping_interval_s: float = 30.0,
        ping_timeout_s: float = 15.0,
        max_backoff_s: float = 30.0,
        swarm_home: Optional[str | Path] = None,
        state_file: Optional[str | Path] = None,
        lock_path: Optional[str | Path] = None,
        inventory_service: Optional[Any] = None,
        discover_harnesses_fn: Optional[Callable[[], Any]] = None,
        os_driver: Optional[Any] = None,
        host_driver: Optional[Any] = None,
        resolve_token_fn: Optional[Callable[..., Any]] = None,
        logger: Optional[logging.Logger] = None,
        agent_factory: Optional[Callable[..., EdgeWorkspaceAgent]] = None,
        single_instance: bool = True,
        **agent_kwargs: Any,
    ) -> None:
        self.server_url = str(server_url)
        self._workspaces_file = Path(workspaces_file) if workspaces_file is not None else None
        self._credentials_dir = Path(credentials_dir) if credentials_dir is not None else None
        self._loopback_port = int(loopback_port or 0)
        self._loopback_token = str(loopback_token or "")
        self._allow_exec = bool(allow_exec)
        self._allow_destructive = bool(allow_destructive)
        self._harness_spawn = bool(harness_spawn)
        self.heartbeat_interval_s = max(0.0, float(heartbeat_interval_s))
        self.ping_interval_s = float(ping_interval_s)
        self.ping_timeout_s = float(ping_timeout_s)
        self.max_backoff_s = float(max_backoff_s)
        self._swarm_home = Path(swarm_home) if swarm_home is not None else default_swarm_home()
        self._state_file = (
            Path(state_file) if state_file is not None else self._swarm_home / "edge_daemon.json"
        )
        self._lock_path = (
            Path(lock_path) if lock_path is not None else self._swarm_home / "edge_daemon.lock"
        )
        self._inventory_service = inventory_service
        self._owns_inventory = inventory_service is None
        self._discover_harnesses_fn = discover_harnesses_fn
        self._os_driver = os_driver
        self._host_driver = host_driver
        self._resolve_token_fn = resolve_token_fn
        self._log = logger or _LOG
        self._agent_factory = agent_factory
        self._single_instance = bool(single_instance)
        self._agent_kwargs = dict(agent_kwargs)

        self.daemon_salt = secrets.token_hex(16)
        self.daemon_gen_id = secrets.token_hex(8)
        self.device_id = ""
        self.agents: List[EdgeWorkspaceAgent] = []
        self._ws_agent_index: Dict[str, EdgeWorkspaceAgent] = {}
        self._agent_tasks: List[asyncio.Task] = []
        self._lock: Optional[SingleInstanceLock] = None
        self._draining = False
        self._started = False
        self._stopped: Optional[asyncio.Event] = None
        self._loopback_server: Optional[asyncio.AbstractServer] = None
        self._loopback_port_bound: Optional[int] = None
        self._orders_idem_locks: Dict[str, asyncio.Lock] = {}

    # -- registry ------------------------------------------------------------

    def load_registry(self) -> Dict[str, Any]:
        from botex.edge.workspaces import load_workspaces_registry

        return load_workspaces_registry(self._workspaces_file)

    def enrolled_workspaces(
        self, registry: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        reg = registry if registry is not None else self.load_registry()
        out: List[Dict[str, Any]] = []
        for ws in reg.get("workspaces") or []:
            if isinstance(ws, dict) and str(ws.get("state") or "") == "enrolled":
                out.append(dict(ws))
        return out[:MAX_ENROLLED_WORKSPACES]

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> Dict[str, Any]:
        if self._started:
            return {
                "ok": True,
                "mode": "running",
                "agents": [a.workspace_id for a in self.agents],
            }
        if self._stopped is None:
            self._stopped = asyncio.Event()
        if self._single_instance:
            try:
                attach = await asyncio.to_thread(
                    attach_or_spawn_edge_daemon,
                    state_file=self._state_file,
                    lock_path=self._lock_path,
                    loopback_port=self._loopback_port,
                )
            except Exception as exc:
                return {"ok": False, "mode": "error", "error": type(exc).__name__}
            if not attach.get("ok"):
                return {
                    "ok": False,
                    "mode": "error",
                    "error": str(attach.get("error") or attach.get("mode") or "lock_contention"),
                    "lock_path": attach.get("lock_path", ""),
                }
            if attach.get("mode") == "attached":
                return {
                    "ok": True,
                    "mode": "attached",
                    "pid": attach.get("pid"),
                    "port": attach.get("port"),
                }
            lock_obj = attach.get("lock")
            if isinstance(lock_obj, SingleInstanceLock):
                self._lock = lock_obj

        reg = self.load_registry()
        self.device_id = str(reg.get("device_id") or socket.gethostname())
        enrolled_ws = self.enrolled_workspaces(reg)
        for ws in enrolled_ws:
            agent = self._make_agent(ws)
            if agent is not None:
                self.agents.append(agent)
                self._ws_agent_index[agent.workspace_id] = agent
        await self._reconcile_startup_state(enrolled_ws)
        await self._ensure_inventory_started()
        loopback_info = await self._start_loopback_ipc(port=self._loopback_port)
        self._started = True
        return {
            "ok": True,
            "mode": "spawned",
            "device_id": self.device_id,
            "agents": [a.workspace_id for a in self.agents],
            "loopback_port": self._loopback_port_bound,
            "loopback_token": loopback_info.get("loopback_token"),
        }

    def _make_agent(self, ws: Dict[str, Any]) -> Optional[EdgeWorkspaceAgent]:
        ws_id = str(ws.get("workspace_id") or "").strip()
        if not ws_id:
            return None
        token = ""
        try:
            resolve_fn = self._resolve_token_fn
            if resolve_fn is None:
                from botex.edge.credentials import resolve_edge_token as resolve_fn

            resolved = resolve_fn(self.device_id, ws_id, base_dir=self._credentials_dir)
            token = str(resolved[0] if isinstance(resolved, (list, tuple)) else resolved)
        except Exception as exc:
            self._log.warning(
                "workspace %s skipped: edge token unavailable (%s)",
                ws_id,
                type(exc).__name__,
            )
            return None
        if not token:
            self._log.warning("workspace %s skipped: no edge token", ws_id)
            return None

        root = ws.get("root") or "."
        caps = EdgeCapabilities.load(root)
        caps.allow_exec = caps.allow_exec or self._allow_exec
        caps.allow_destructive = caps.allow_destructive or self._allow_destructive
        caps.harness_spawn = caps.harness_spawn or self._harness_spawn
        overrides = ws.get("caps_overrides")
        if isinstance(overrides, dict):
            for key in ("allow_exec", "allow_destructive", "harness_spawn"):
                if key in overrides:
                    setattr(caps, key, bool(overrides[key]))

        factory = self._agent_factory or EdgeWorkspaceAgent
        try:
            return factory(
                self.server_url,
                token=token,
                device_id=self.device_id,
                workspace=ws,
                workspaces_file=self._workspaces_file,
                credentials_dir=self._credentials_dir,
                swarm_home=self._swarm_home,
                caps=caps,
                ping_interval_s=self.ping_interval_s,
                ping_timeout_s=self.ping_timeout_s,
                max_backoff_s=self.max_backoff_s,
                heartbeat_interval_s=self.heartbeat_interval_s,
                daemon_salt=self.daemon_salt,
                daemon_gen_id=self.daemon_gen_id,
                inventory_service=self._inventory_service,
                discover_harnesses_fn=self._discover_harnesses_fn,
                os_driver=self._os_driver,
                host_driver=self._host_driver,
                resolve_token_fn=self._resolve_token_fn,
                logger=self._log,
                **self._agent_kwargs,
            )
        except Exception as exc:
            self._log.warning("workspace %s agent init failed (%s)", ws_id, type(exc).__name__)
            return None

    async def _reconcile_startup_state(self, enrolled_ws: List[Dict[str, Any]]) -> None:
        """Reconcile stale active_orders.json PIDs and orphan wip worktrees on startup."""
        store = EdgeActiveOrdersStore(self._swarm_home / "active_orders.json")
        stored = store.list_orders()
        for oid, rec in list(stored.items()):
            try:
                pid = int(rec.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            stored_ct = rec.get("pid_create_time")
            ws_id = str(rec.get("workspace_id") or "").strip()
            alive = False
            if pid > 0:
                live_ct = _pid_create_time(pid)
                if live_ct is not None:
                    if stored_ct is None:
                        alive = True
                    else:
                        try:
                            alive = abs(live_ct - float(stored_ct)) <= _PID_CTIME_TOLERANCE_S
                        except (TypeError, ValueError):
                            alive = False
            if not alive:
                agent = self._ws_agent_index.get(ws_id)
                if agent is not None:
                    try:
                        agent.emit_work_order_event(oid, "reconciled", {"returncode": None})
                    except Exception:
                        pass
                store.remove(oid)
        remaining = store.list_orders()
        if enrolled_ws:
            await asyncio.to_thread(_reconcile_orphan_worktrees, enrolled_ws, remaining)

    async def _ensure_inventory_started(self) -> None:
        if self._inventory_service is None:
            try:
                from botex.edge.adapters import HarnessInventoryService

                self._inventory_service = HarnessInventoryService(
                    discover_fn=self._discover_harnesses_fn
                )
                self._owns_inventory = True
            except Exception:
                self._inventory_service = None
                return
        try:
            await self._inventory_service.start()
        except Exception:
            pass

    async def run_forever(self) -> None:
        if not self._started:
            res = await self.start()
            if not res.get("ok") or res.get("mode") == "attached":
                return
        self._agent_tasks = [
            asyncio.create_task(agent.run_forever(), name=f"botex-edge-agent:{agent.workspace_id}")
            for agent in self.agents
        ]
        try:
            if self._agent_tasks:
                await asyncio.gather(*self._agent_tasks)
            elif self._stopped is not None:
                await self._stopped.wait()
        finally:
            await self.stop()

    async def stop(self, drain_timeout_s: float = 10.0) -> None:
        """Graceful drain: refuse new dispatches, close orders, stop services."""
        self._draining = True
        for agent in self.agents:
            agent._draining = True
            agent.stop()
        tasks = [t for t in self._agent_tasks if not t.done()]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=max(0.0, float(drain_timeout_s)))
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for agent in self.agents:
            for order_id in list(agent._active_order_controls):
                entry = agent._active_order_controls.get(order_id) or {}
                try:
                    pid_val = int(entry.get("pid") or 0)
                except (TypeError, ValueError):
                    pid_val = 0
                stored_ct = entry.get("pid_create_time")
                if pid_val > 0 and stored_ct is not None:
                    live_ct = _pid_create_time(pid_val)
                    try:
                        same_proc = live_ct is not None and (
                            abs(live_ct - float(stored_ct)) <= _PID_CTIME_TOLERANCE_S
                        )
                    except (TypeError, ValueError):
                        same_proc = False
                    if not same_proc:
                        pid_val = 0
                guard = entry.get("job_guard")
                if pid_val > 0 or guard is not None:
                    try:
                        await asyncio.to_thread(cancel_process_tree, pid_val, job_guard=guard)
                    except Exception:
                        pass
                try:
                    agent.emit_work_order_event(order_id, "fenced", {"reason": "daemon_drain"})
                except Exception:
                    pass
                agent.unregister_active_order(order_id)
        svc = self._inventory_service
        if svc is not None and self._owns_inventory:
            try:
                await svc.stop()
            except Exception:
                pass
        await self._stop_loopback_ipc()
        if self._lock is not None:
            try:
                self._lock.release()
            except Exception:
                pass
            self._lock = None
        self._started = False
        if self._stopped is not None:
            self._stopped.set()

    # -- hardened 127.0.0.1 loopback IPC (F3, F7) -----------------------------

    async def _start_loopback_ipc(self, *, port: int = 0) -> Dict[str, Any]:
        if not self._loopback_token:
            self._loopback_token = secrets.token_urlsafe(32)
        server = await asyncio.start_server(
            self._handle_loopback_client,
            host="127.0.0.1",
            port=int(port),
        )
        self._loopback_server = server
        bound = int(server.sockets[0].getsockname()[1]) if server.sockets else int(port)
        self._loopback_port_bound = bound
        try:
            _atomic_write_json_0600(
                self._state_file,
                {
                    "pid": os.getpid(),
                    "host": "127.0.0.1",
                    "port": bound,
                    "loopback_token": self._loopback_token,
                    "device_id": self.device_id,
                    "workspaces": [a.workspace_id for a in self.agents],
                    "daemon_gen_id": self.daemon_gen_id,
                    "started_at": time.time(),
                },
            )
        except OSError:
            pass
        return {
            "ok": True,
            "host": "127.0.0.1",
            "port": bound,
            "loopback_token": self._loopback_token,
            "state_file": str(self._state_file),
        }

    async def _stop_loopback_ipc(self) -> None:
        if self._loopback_server is not None:
            self._loopback_server.close()
            try:
                await self._loopback_server.wait_closed()
            except Exception:
                pass
            self._loopback_server = None
            self._loopback_port_bound = None

    def _agent_for_frame(self, frame: Dict[str, Any]) -> Optional[EdgeWorkspaceAgent]:
        ws_id = str(frame.get("workspace_id") or "").strip()
        if ws_id:
            return self._ws_agent_index.get(ws_id)
        order_id = str(frame.get("order_id") or "").strip()
        if order_id:
            for agent in self.agents:
                if order_id in agent._active_order_controls:
                    return agent
        return None

    def _workspaces_payload(self, include_paths: bool) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for ws in self.load_registry().get("workspaces") or []:
            if not isinstance(ws, dict):
                continue
            entry: Dict[str, Any] = {
                "workspace_id": ws.get("workspace_id", ""),
                "label": ws.get("label", ""),
                "state": ws.get("state", ""),
                "local_admin": bool(ws.get("local_admin")),
            }
            if include_paths:
                entry["root"] = ws.get("root", "")
            out.append(entry)
        return out

    def _harnesses_payload(self) -> List[Dict[str, Any]]:
        svc = self._inventory_service
        if svc is None:
            return []
        try:
            snap = svc.get_cached_snapshot()
            return list(snap) if isinstance(snap, list) else []
        except Exception:
            return []

    async def _handle_loopback_order(
        self,
        writer: asyncio.StreamWriter,
        body_obj: Dict[str, Any],
        *,
        idem_key: str = "",
    ) -> None:
        effective_idem = str(
            idem_key or body_obj.get("idempotency_key") or body_obj.get("request_id") or ""
        ).strip()
        # Serialize same-key POST /orders replays: two concurrent requests must
        # not both miss the idempotency cache and race the Cloud registration
        # (409 order_exists) — the loser gets the cached 200 duplicate instead.
        idem_lock = (
            self._orders_idem_locks.setdefault(effective_idem, asyncio.Lock())
            if effective_idem
            else None
        )
        if idem_lock is not None:
            await idem_lock.acquire()
        try:
            await self._handle_loopback_order_locked(
                writer, body_obj, effective_idem=effective_idem
            )
        finally:
            if idem_lock is not None:
                idem_lock.release()
                if len(self._orders_idem_locks) > _ORDERS_IDEM_LOCK_MAX:
                    for stale_key in [
                        k
                        for k in self._orders_idem_locks
                        if k != effective_idem and not self._orders_idem_locks[k].locked()
                    ]:
                        self._orders_idem_locks.pop(stale_key, None)

    async def _handle_loopback_order_locked(
        self,
        writer: asyncio.StreamWriter,
        body_obj: Dict[str, Any],
        *,
        effective_idem: str = "",
    ) -> None:
        try:
            from botex.edge.adapters import normalize_mode_request, paid_ceiling_violations
            from botex.edge.workspaces import get_workspace
        except ImportError:
            await _write_loopback_http_response(
                writer, 500, {"ok": False, "error": "workspaces_module_unavailable"}
            )
            return
        order = body_obj.get("order") if isinstance(body_obj.get("order"), dict) else dict(body_obj)
        order_id = str(order.get("order_id") or body_obj.get("order_id") or "").strip()
        orders_store = EdgeActiveOrdersStore(self._swarm_home / "active_orders.json")
        if effective_idem:
            existing_idem = orders_store.get_idempotency(effective_idem)
            if existing_idem is not None:
                prev_oid = str(existing_idem.get("order_id") or "")
                if order_id and prev_oid and prev_oid != order_id:
                    await _write_loopback_http_response(
                        writer, 409, {"ok": False, "error": "idem_key_conflict"}
                    )
                    return
                cached_resp = dict(
                    existing_idem.get("response") or {"ok": True, "order_id": prev_oid}
                )
                cached_resp["duplicate"] = True
                await _write_loopback_http_response(writer, 200, cached_resp)
                return

        target_ws = str(body_obj.get("workspace_id") or order.get("workspace_id") or "").strip()
        if not target_ws:
            await _write_loopback_http_response(
                writer, 400, {"ok": False, "error": "missing_workspace_id"}
            )
            return
        ws_entry = get_workspace(target_ws, path=self._workspaces_file)
        if ws_entry is None:
            await _write_loopback_http_response(
                writer, 404, {"ok": False, "error": "workspace_not_found"}
            )
            return
        raw_mode = order.get("mode_request")
        if raw_mode is None:
            raw_mode = order.get("mode")
        if raw_mode is None:
            raw_mode = body_obj.get("mode")
        mode, mode_err = normalize_mode_request(raw_mode, default="readonly")
        confirmed_edit_meta: Optional[Dict[str, Any]] = None
        # F7: non-local_admin workspaces stay at the $0 / branch-only ceiling.
        if not bool(ws_entry.get("local_admin")):
            if mode_err is not None:
                await _write_loopback_http_response(
                    writer, 403, {"ok": False, "error": "mode_exceeds_ceiling"}
                )
                return
            violations = paid_ceiling_violations(order, body_obj)
            if violations:
                await _write_loopback_http_response(
                    writer,
                    403,
                    {"ok": False, "error": "paid_ceiling_exceeded", "violations": violations},
                )
                return
            if mode not in _ORDERS_ALLOWED_MODES:
                conf = body_obj.get("confirmation") or order.get("confirmation")
                if mode in _EDIT_CONFIRM_ALLOWED_MODES and conf is not None:
                    conf_ok, conf_err = _validate_edit_confirmation(conf)
                    if not conf_ok:
                        await _write_loopback_http_response(
                            writer,
                            403,
                            {"ok": False, "error": "mode_exceeds_ceiling", "detail": conf_err},
                        )
                        return
                    confirmed_edit_meta = {
                        "confirmed_by": str(conf.get("confirmed_by") or "").strip(),
                        "confirmed_at": str(conf.get("confirmed_at") or "").strip(),
                    }
                else:
                    await _write_loopback_http_response(
                        writer, 403, {"ok": False, "error": "mode_exceeds_ceiling"}
                    )
                    return
        if not order_id:
            await _write_loopback_http_response(
                writer, 400, {"ok": False, "error": "missing_order_id"}
            )
            return
        if self._draining:
            await _write_loopback_http_response(writer, 409, {"ok": False, "error": "draining"})
            return
        agent = self._ws_agent_index.get(target_ws)
        if agent is None:
            await _write_loopback_http_response(
                writer, 409, {"ok": False, "error": "no_active_agent"}
            )
            return
        target_harness = str(
            body_obj.get("target_harness") or order.get("target_harness") or "botex"
        )
        goal = str(order.get("goal") or order.get("prompt") or body_obj.get("goal") or "").strip()
        if not goal:
            goal = f"Loopback order {order_id}"
        reg_payload: Dict[str, Any] = {
            "order_id": order_id,
            "workspace_id": target_ws,
            "goal": goal,
            "mode_request": mode or "readonly",
            "target_harness": target_harness,
        }
        if isinstance(order.get("handoff_policy"), dict):
            reg_payload["handoff_policy"] = order["handoff_policy"]
        reg_res = await agent.register_work_order_with_cloud(reg_payload)
        if not reg_res.get("ok"):
            err_code = str(reg_res.get("error") or "registration_unavailable")
            http_status = (
                503
                if err_code == "registration_unavailable"
                else (403 if err_code == "insufficient_scope" else 409)
            )
            await _write_loopback_http_response(
                writer, http_status, {"ok": False, "error": err_code}
            )
            return
        try:
            fencing_epoch = int(body_obj.get("fencing_epoch") or order.get("epoch") or 0)
        except (TypeError, ValueError):
            fencing_epoch = 0
        if bool(ws_entry.get("local_admin")):
            dispatch_order = dict(order)
        else:
            # F7 ceiling: non-local_admin dispatches are rebuilt from an explicit
            # field allowlist, so raw ``argv``/``command`` payloads can never
            # reach the spawn path — ``build_harness_argv`` stays the only source.
            dispatch_order = {
                key: order[key]
                for key in (
                    "order_id",
                    "goal",
                    "prompt",
                    "mode_request",
                    "epoch",
                    "workspace_id",
                    "target_harness",
                    "handoff_policy",
                )
                if key in order
            }
        # translate_work_order requires the order identity inside the payload,
        # including the {"order": {"goal": ...}} loopback body shape.
        dispatch_order["order_id"] = order_id
        if "mode_request" not in dispatch_order or not dispatch_order.get("mode_request"):
            dispatch_order["mode_request"] = mode or "readonly"
        frame = {
            "type": _DISPATCH_WORK_ORDER_TYPE,
            "request_id": str(body_obj.get("request_id") or f"lb-{secrets.token_hex(4)}"),
            "order_id": order_id,
            "workspace_id": target_ws,
            "fencing_token": str(body_obj.get("fencing_token") or "") or secrets.token_urlsafe(24),
            "fencing_epoch": fencing_epoch,
            "mode_default": "readonly",
            "order": dispatch_order,
            "target_harness": target_harness,
        }
        if confirmed_edit_meta is not None:
            agent.emit_work_order_event(
                order_id, "confirmed_edit", _mask_recursive(confirmed_edit_meta)
            )
        reply = await agent.handle_control_frame(frame)
        status = 200 if reply.get("ok") else 409
        if status == 200 and effective_idem:
            orders_store.set_idempotency(effective_idem, order_id, reply)
        await _write_loopback_http_response(writer, status, reply)

    async def _handle_loopback_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            peer = writer.get_extra_info("peername")
            peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else ""
            if peer_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                await _write_loopback_http_response(
                    writer, 403, {"ok": False, "error": "forbidden_non_loopback"}
                )
                return

            max_line_bytes = 8192
            max_headers = 64
            first_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not first_line:
                return
            if len(first_line) > max_line_bytes:
                await _write_loopback_http_response(
                    writer, 413, {"ok": False, "error": "request_line_too_long"}
                )
                return
            line_str = first_line.decode("utf-8", errors="replace").strip()

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
                    await _write_loopback_http_response(
                        writer, 413, {"ok": False, "error": "header_line_too_long"}
                    )
                    return
                if not hline or hline in (b"\r\n", b"\n"):
                    break
                header_count += 1
                if header_count > max_headers:
                    await _write_loopback_http_response(
                        writer, 413, {"ok": False, "error": "too_many_headers"}
                    )
                    return
                hstr = hline.decode("utf-8", errors="replace").strip()
                if ":" in hstr:
                    hk, hv = hstr.split(":", 1)
                    headers[hk.strip().lower()] = hv.strip()

            # F3: DNS-rebinding guard — Host must be exactly the bound loopback.
            if not _loopback_host_allowed(
                headers.get("host", ""), int(self._loopback_port_bound or 0)
            ):
                await _write_loopback_http_response(
                    writer, 403, {"ok": False, "error": "forbidden_host_header"}
                )
                return

            origin = headers.get("origin", "").strip()
            if origin:
                parsed_orig = urllib.parse.urlparse(origin)
                if parsed_orig.hostname not in ("127.0.0.1", "localhost"):
                    await _write_loopback_http_response(
                        writer, 403, {"ok": False, "error": "forbidden_origin"}
                    )
                    return

            provided_tok = headers.get("x-loopback-token", "").strip()
            if not provided_tok:
                auth_hdr = headers.get("authorization", "").strip()
                if auth_hdr.lower().startswith("bearer "):
                    provided_tok = auth_hdr[7:].strip()

            # F7: constant-time token check on ALL loopback routes.
            if not self._loopback_token or not secrets.compare_digest(
                provided_tok, self._loopback_token
            ):
                await _write_loopback_http_response(
                    writer, 401, {"ok": False, "error": "unauthorized_loopback_token"}
                )
                return

            content_len = int(headers.get("content-length") or 0)
            if content_len > 65536:
                await _write_loopback_http_response(
                    writer, 413, {"ok": False, "error": "body_too_large"}
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

            path_only, _, query_str = path.partition("?")
            query = urllib.parse.parse_qs(query_str)

            if path_only.startswith("/status") or (method == "GET" and path_only == "/"):
                await _write_loopback_http_response(
                    writer,
                    200,
                    {
                        "ok": True,
                        "status": "running",
                        "pid": os.getpid(),
                        "device_id": self.device_id,
                        "workspaces": [a.workspace_id for a in self.agents],
                        "draining": self._draining,
                        "loopback_port": self._loopback_port_bound,
                    },
                )
                return

            if method == "GET" and path_only.startswith("/workspaces"):
                include_paths = query.get("include_paths") == ["1"]
                await _write_loopback_http_response(
                    writer,
                    200,
                    {
                        "ok": True,
                        "workspaces": self._workspaces_payload(include_paths),
                    },
                )
                return

            if method == "GET" and path_only.startswith("/harnesses"):
                await _write_loopback_http_response(
                    writer,
                    200,
                    {"ok": True, "harnesses": self._harnesses_payload()},
                )
                return

            if method == "POST" and path_only.startswith("/orders"):
                await self._handle_loopback_order(
                    writer,
                    body_obj,
                    idem_key=headers.get("idempotency-key", "").strip(),
                )
                return

            if method == "POST" and path_only.startswith("/control"):
                frame = body_obj.get("frame")
                ftype = (
                    str(frame.get("type") or "").strip().lower() if isinstance(frame, dict) else ""
                )
                if not isinstance(frame, dict) or ftype not in LOOPBACK_CONTROL_ALLOWLIST:
                    await _write_loopback_http_response(
                        writer,
                        400,
                        {"ok": False, "error": "forbidden_control_frame_type"},
                    )
                    return
                if ftype in TYPED_CONTROL_FRAME_TYPES and (
                    not str(frame.get("fencing_token") or "").strip()
                    or not str(frame.get("order_id") or "").strip()
                ):
                    await _write_loopback_http_response(
                        writer,
                        400,
                        {"ok": False, "error": "missing_fencing_token_or_order_id"},
                    )
                    return
                target_agent = self._agent_for_frame(frame)
                if body_obj.get("local_exec"):
                    if target_agent is None:
                        await _write_loopback_http_response(
                            writer, 409, {"ok": False, "error": "no_active_agent"}
                        )
                        return
                    ctrl_reply = await target_agent.handle_control_frame(frame)
                    status_code = 200 if ctrl_reply.get("ok") else 409
                    await _write_loopback_http_response(writer, status_code, ctrl_reply)
                    return
                if target_agent is not None and target_agent._ws is not None:
                    await target_agent._send_json(
                        target_agent._ws,
                        frame,
                        priority=QOS_PRIORITY_CONTROL,
                    )
                    await _write_loopback_http_response(
                        writer, 200, {"ok": True, "dispatched": True}
                    )
                    return
                await _write_loopback_http_response(
                    writer,
                    200,
                    {"ok": True, "dispatched": False, "reason": "no_active_ws"},
                )
                return

            await _write_loopback_http_response(writer, 404, {"ok": False, "error": "not_found"})
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
                "workspaces": [a.workspace_id for a in self.agents],
                "loopback_port": self._loopback_port_bound,
            }
        else:
            resp = {"ok": True, "action": action}
        writer.write((json.dumps(resp) + "\n").encode("utf-8"))
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


def _probe_loopback_state_file(st_path: Path) -> Optional[Dict[str, Any]]:
    if not st_path.is_file():
        return None
    try:
        state = json.loads(st_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return None
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
    return None


def attach_or_spawn_edge_daemon(
    *,
    state_file: Optional[Path] = None,
    lock_path: Optional[Path] = None,
    loopback_port: int = 0,
    spawn_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    health_timeout_s: float = 5.0,
) -> Dict[str, Any]:
    """Attach to an existing local ``--edge-daemon`` if healthy, or acquire lock to spawn."""
    swarm_home = default_swarm_home()
    st_path = Path(state_file) if state_file is not None else (swarm_home / "edge_daemon.json")
    lk_path = Path(lock_path) if lock_path is not None else (swarm_home / "edge_daemon.lock")

    attached = _probe_loopback_state_file(st_path)
    if attached is not None:
        return attached

    lock = SingleInstanceLock(lk_path)
    try:
        acquired = bool(lock.acquire())
    except SingleInstanceLockError:
        acquired = False
    if not acquired:
        for _attempt in range(3):
            time.sleep(0.15)
            attached = _probe_loopback_state_file(st_path)
            if attached is not None:
                return attached
        return {
            "ok": False,
            "mode": "lock_contention",
            "error": "lock_held_by_active_daemon",
            "lock_path": str(lk_path),
        }

    if spawn_fn is not None:
        # Release the singleton lock before invoking the external daemon spawner so the
        # child daemon can acquire it itself during startup without false contention.
        lock.release()
        try:
            res = spawn_fn()
        except Exception:
            raise
        deadline = time.monotonic() + max(0.1, float(health_timeout_s))
        while time.monotonic() < deadline:
            probed = _probe_loopback_state_file(st_path)
            if probed is not None:
                extra = {
                    k: v
                    for k, v in (res.items() if isinstance(res, dict) else ())
                    if k not in ("ok", "mode", "lock")
                }
                return {
                    **extra,
                    "ok": True,
                    "mode": "spawned",
                    "loopback_port": int(probed.get("port") or loopback_port),
                }
            time.sleep(0.1)
        try:
            spawned_pid = int(res.get("pid") or 0) if isinstance(res, dict) else 0
        except (TypeError, ValueError):
            spawned_pid = 0
        if spawned_pid > 0:
            try:
                cancel_process_tree(spawned_pid)
            except Exception:
                pass
        return {
            "ok": False,
            "mode": "spawn_failed_health",
            "error": "spawn_failed_health",
            "lock_path": str(lk_path),
        }

    return {
        "ok": True,
        "mode": "spawned",
        "lock": lock,
        "state_file": str(st_path),
        "lock_path": str(lk_path),
        "loopback_port": int(loopback_port),
    }
