# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Downward capability adapters, epoch checkpointing, and the verify terminal hook.

Translates canonical ``WorkOrder`` objects
into per-harness dispatch specs for ``botex`` / ``zcode`` / ``devin`` — never
widening ``mode_request`` — while the ``antigravity_ls`` family stays excluded
until a read-status API exists in L0. ``EpochCheckpointManager`` snapshots WIP
every ``min_turns``–``max_turns`` file-mutating turns (or after a green
intermediate test) and refuses to push ``swarm/wip/<order>/e<epoch>`` unless the
remote epoch still matches the local one. ``finalize_verified_work_order`` is
the ``no_self_report_done`` terminal hook: only an external
``verify_gate``/``acceptance_review`` result with ``ok is True`` marks an order
``DONE`` and refreshes the bot brief.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Mapping, Sequence
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import urlsplit

try:
    from botex.tools.security import coerce_text, mask_secrets, sanitize_identifier
except (ImportError, ValueError):
    from security import coerce_text, mask_secrets, sanitize_identifier

_LOG = logging.getLogger("botex.edge.adapters")

__all__ = [
    "ANTIGRAVITY_EXCLUSION_REASON",
    "DEFAULT_DISCOVERY_CLI_ALLOWLIST",
    "HARNESS_MODE_MAP",
    "RESTRICTIVENESS_RANK",
    "SUPPORTED_HARNESSES",
    "EpochCheckpointManager",
    "HarnessInventoryService",
    "HarnessLimitsCollector",
    "build_anti_spiral_reset",
    "build_harness_argv",
    "check_harness_eligible",
    "discover_local_harnesses",
    "finalize_verified_work_order",
    "normalize_limits_payload",
    "normalize_mode_request",
    "paid_ceiling_violations",
    "translate_work_order",
]

# Canonical capability ladder. Mapping is strictly downward-or-equal: a target
# mode whose rank exceeds the requested rank would widen privileges and is
# always rejected before dispatch.
RESTRICTIVENESS_RANK: Dict[str, int] = {
    "readonly": 0,
    "plan": 0,
    "edit": 1,
    "fs_write": 1,
    "destructive": 2,
    "full": 3,
    "yolo": 3,
}

SUPPORTED_HARNESSES = frozenset({"botex", "zcode", "devin"})

ANTIGRAVITY_EXCLUSION_REASON = "missing read-status API in L0"

# Requested canonical mode -> granted canonical mode, per target harness.
# Entries absent here fail closed (BLOCKED) rather than silently degrade.
# ``fs_write`` is deliberately unmapped everywhere: no harness exposes a safe
# file-system-write-only equivalent yet.
HARNESS_MODE_MAP: Dict[str, Dict[str, str]] = {
    "botex": {
        "readonly": "readonly",
        "plan": "readonly",
        "edit": "edit",
        "destructive": "destructive",
        "full": "full",
        "yolo": "full",
    },
    "zcode": {
        "readonly": "plan",
        "plan": "plan",
        "edit": "edit",
    },
    "devin": {
        "readonly": "readonly",
        "plan": "plan",
        "edit": "edit",
        "destructive": "destructive",
        "full": "full",
        "yolo": "full",
    },
}

# Preferred outbound transport per harness. ``acp`` = Agent Client Protocol
# (JSON-RPC over stdio); ``mcp_or_cli`` = BoteX MCP/CLI entry points. ACP-capable
# harnesses keep ``stdout`` as the documented fallback channel.
HARNESS_TRANSPORT: Dict[str, str] = {
    "botex": "mcp_or_cli",
    "zcode": "acp",
    "devin": "acp",
}

_MODE_BLOCK_REASON = "unmapped_or_widened_mode"
_MAX_NOTE_CHARS = 1000
_EPOCH_REF_RE = re.compile(r"/e(\d+)\s*$")


# ---------------------------------------------------------------------------
# Small duck-typed helpers (WorkOrder may be a dataclass or a plain dict)
# ---------------------------------------------------------------------------


def _order_field(order: Any, name: str, default: Any = None) -> Any:
    if order is None:
        return default
    if isinstance(order, Mapping):
        return order.get(name, default)
    return getattr(order, name, default)


def _coerce_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _masked(text: Any, limit: int = _MAX_NOTE_CHARS) -> str:
    return coerce_text(mask_secrets(str(text or ""))).strip()[:limit]


def _verify_kind(order: Any) -> str:
    verify = _order_field(order, "verify", None)
    if verify is None:
        return "none"
    if isinstance(verify, str):
        return verify.strip().lower() or "none"
    if isinstance(verify, Mapping):
        return str(verify.get("kind") or "command").strip().lower() or "command"
    kind = getattr(verify, "kind", None)
    if kind is None:
        return "none"
    return str(kind).strip().lower() or "none"


def _verify_field(order: Any, name: str, default: Any = "") -> Any:
    verify = _order_field(order, "verify", None)
    if verify is None:
        return default
    if isinstance(verify, Mapping):
        return verify.get(name, default)
    return getattr(verify, name, default)


def _order_id(order: Any) -> str:
    return str(_order_field(order, "order_id", "") or "").strip()


def _clean_order_id(raw: str) -> Optional[str]:
    clean = sanitize_identifier(str(raw or "").strip())
    if not clean or clean != str(raw or "").strip():
        return None
    return clean


def _normalize_harness_name(name: Any) -> str:
    return str(name or "").strip().lower().replace("-", "_").replace(" ", "_")


def _is_excluded_harness(norm: str) -> bool:
    # The whole antigravity* family is excluded from automated failover until
    # the L0 read-status/cascade-health API exists.
    return (
        norm == "antigravity"
        or norm.startswith("antigravity_")
        or norm.replace("_", "").startswith("antigravityls")
    )


# ---------------------------------------------------------------------------
# Bounded local harness discovery (Plan 042)
# ---------------------------------------------------------------------------

# CLI presence is resolved off-thread via shutil.which against bare,
# allowlisted executable names only; ACP endpoints get loopback-restricted
# TCP reachability probes. Records never carry filesystem paths or endpoint
# hosts, keeping inventory frames safe to forward to BoteX Cloud.
DEFAULT_DISCOVERY_CLI_ALLOWLIST: Dict[str, str] = {
    "aider": "aider",
    "botex": "botex",
    "claude": "claude",
    "codex": "codex",
    "devin": "devin",
    "zcode": "zcode",
}

_ACP_DEFAULT_PORTS: Dict[str, Optional[int]] = {"http": 80, "https": 443, "tcp": None}
# Grace on top of the caller's per-probe budget so the outer bound only fires
# on genuine overruns (thread scheduling, loop starvation), never racing the
# individually wait_for-bounded probes.
_DISCOVERY_OUTER_GRACE_S = 0.25


def _is_bare_cli_name(name: str) -> bool:
    # shutil.which() treats separator-containing names as literal paths, so
    # only bare names may enter the probe set.
    return (
        bool(name)
        and "/" not in name
        and "\\" not in name
        and ":" not in name
        and name not in (".", "..")
    )


def _resolve_cli_availability(cli_names: List[str]) -> Dict[str, bool]:
    # Executed inside asyncio.to_thread; only booleans come back so resolved
    # executable paths never leave the probe thread.
    return {name: (shutil.which(name) is not None) for name in cli_names}


def _parse_loopback_endpoint(endpoint: str) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """Parse an ACP endpoint into ``(scheme, host, port)``; ``host`` is None
    for non-loopback or otherwise non-probeable targets."""
    raw = coerce_text(endpoint).strip()
    if not raw:
        return None, None, None
    parsed = urlsplit(raw if "://" in raw else f"tcp://{raw}")
    scheme = (parsed.scheme or "tcp").lower()
    if scheme not in _ACP_DEFAULT_PORTS:
        return scheme, None, None
    try:
        port = parsed.port
    except ValueError:
        return scheme, None, None
    host = parsed.hostname or ""
    if not host:
        return scheme, None, None
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            # Non-literal hosts are refused rather than resolved: a DNS lookup
            # could block and would turn the edge bridge into a network scanner.
            loopback = False
    if not loopback:
        return scheme, None, None
    resolved_port = port if port is not None else _ACP_DEFAULT_PORTS[scheme]
    return scheme, host, resolved_port


async def _probe_acp_endpoint(endpoint: str, timeout_s: float) -> tuple[str, Dict[str, Any]]:
    """Loopback-only TCP reachability probe; never reads endpoint responses."""
    scheme, host, port = _parse_loopback_endpoint(endpoint)
    capabilities: Dict[str, Any] = {"probe": "tcp"}
    if scheme is not None:
        capabilities["scheme"] = scheme
    if scheme is None:
        capabilities["reason"] = "empty-endpoint"
        return "degraded", capabilities
    if host is None:
        capabilities["reason"] = "unsupported-or-non-loopback-endpoint"
        return "degraded", capabilities
    if not port or port <= 0:
        capabilities["reason"] = "missing-or-invalid-port"
        return "degraded", capabilities
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)), timeout=timeout_s
        )
    except TimeoutError:
        capabilities["reason"] = "connect-timeout"
        return "degraded", capabilities
    except (ConnectionRefusedError, OSError):
        return "offline", capabilities
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=timeout_s)
    except (TimeoutError, OSError):
        pass
    return "online", capabilities


_MAX_LIMITS_COLLECT_TIMEOUT_S = 1.5
_MAX_PROBE_STDOUT_BYTES = 65536
_DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
_STDLIB_URLOPEN = urllib.request.urlopen
_STDLIB_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec

# Static enum maps decouple untrusted probe output from persisted enum fields.
_VALID_LIMIT_UNITS_MAP: Dict[str, str] = {
    "requests": "requests",
    "request": "requests",
    "req": "requests",
    "tokens": "tokens",
    "token": "tokens",
    "tok": "tokens",
    "credits": "credits",
    "credit": "credits",
    "acu": "credits",
    "models_loaded": "models_loaded",
    "models": "models_loaded",
    "unknown": "unknown",
}

_VALID_LIMIT_CONFIDENCE_MAP: Dict[str, str] = {
    "exact": "exact",
    "estimated": "estimated",
    "estimate": "estimated",
    "unknown": "unknown",
}

DEFAULT_LIMITS_CLI_PROBES: Dict[str, tuple[str, ...]] = {
    "devin": ("devin", "status", "--json"),
    "zcode": ("zcode", "status", "--json"),
}
_DEFAULT_LIMITS_HARNESS_IDS: tuple[str, ...] = ("devin", "zcode", "ollama")

_SENSITIVE_ENV_PREFIXES: tuple[str, ...] = ("BOTEX_", "SWARM_")
_SENSITIVE_ENV_SUFFIXES: tuple[str, ...] = (
    "_API_KEY",
    "_API_TOKEN",
    "_ACCESS_TOKEN",
    "_AUTH_TOKEN",
    "_BEARER_TOKEN",
    "_SECRET",
    "_SECRET_KEY",
    "_PRIVATE_KEY",
    "_PASSWORD",
    "_PASSWD",
    "_TOKEN",
    "_CREDENTIALS",
    "_CREDENTIALS_DIR",
)
_SENSITIVE_ENV_EXACT = frozenset(
    {
        "API_KEY",
        "ACCESS_TOKEN",
        "AUTH_TOKEN",
        "SECRET",
        "SECRET_KEY",
        "PRIVATE_KEY",
        "PASSWORD",
        "OPENAI_KEY",
        "ANTHROPIC_KEY",
        "GEMINI_KEY",
        "HF_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITLAB_TOKEN",
        "NPM_TOKEN",
        "PYPI_TOKEN",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONSTARTUP",
        "NODE_OPTIONS",
    }
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise urllib.error.HTTPError(req.full_url, code, "redirects disallowed", headers, fp)


_LOOPBACK_HTTP_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirectHandler(),
)


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _coerce_optional_window_s(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        val = int(value)
    except (TypeError, ValueError):
        return None
    if val < 0:
        return None
    return val


def _coerce_optional_str(value: Any, limit: int = 64) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    clean = _masked(value, limit)
    return clean if clean else None


def _default_unknown_limits(
    *,
    source: str = "unprobed",
    collected_at: Optional[str] = None,
) -> Dict[str, Any]:
    ts = _coerce_optional_str(collected_at, 64) or datetime.now(timezone.utc).isoformat()
    clean_source = _coerce_optional_str(source, 64) or "unknown"
    return {
        "unit": "unknown",
        "used": None,
        "remaining": None,
        "limit": None,
        "reset_at": None,
        "window_s": None,
        "source": clean_source,
        "confidence": "unknown",
        "collected_at": ts,
    }


def normalize_limits_payload(
    payload: Optional[Mapping[str, Any]] = None,
    *,
    default_source: str = "unknown",
    collected_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize a raw quota/limits mapping into the canonical 9-key telemetry schema."""
    if payload is None or not isinstance(payload, Mapping):
        return _default_unknown_limits(source=default_source, collected_at=collected_at)
    raw_unit = str(payload.get("unit") or "").strip().lower()
    unit = _VALID_LIMIT_UNITS_MAP.get(raw_unit, "unknown")
    raw_conf = str(payload.get("confidence") or "").strip().lower()
    confidence = _VALID_LIMIT_CONFIDENCE_MAP.get(raw_conf, "unknown")
    source = (
        _coerce_optional_str(payload.get("source"), 64)
        or _coerce_optional_str(default_source, 64)
        or "unknown"
    )
    ts = (
        _coerce_optional_str(payload.get("collected_at"), 64)
        or _coerce_optional_str(collected_at, 64)
        or datetime.now(timezone.utc).isoformat()
    )
    return {
        "unit": unit,
        "used": _coerce_optional_float(payload.get("used")),
        "remaining": _coerce_optional_float(payload.get("remaining")),
        "limit": _coerce_optional_float(payload.get("limit")),
        "reset_at": _coerce_optional_str(payload.get("reset_at"), 64),
        "window_s": _coerce_optional_window_s(payload.get("window_s")),
        "source": source,
        "confidence": confidence,
        "collected_at": ts,
    }


def _scrub_probe_env(base_env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    src = os.environ if base_env is None else base_env
    clean: Dict[str, str] = {}
    for raw_key, raw_val in src.items():
        key = str(raw_key)
        upper = key.upper()
        if (
            upper.startswith(_SENSITIVE_ENV_PREFIXES)
            or upper.endswith(_SENSITIVE_ENV_SUFFIXES)
            or upper in _SENSITIVE_ENV_EXACT
        ):
            continue
        clean[key] = str(raw_val)
    clean["NO_COLOR"] = "1"
    clean["TERM"] = "dumb"
    return clean


def _validate_ollama_loopback_base_url(ollama_base_url: str) -> Optional[str]:
    raw = coerce_text(ollama_base_url).strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"http://{raw}"
    parsed = urlsplit(candidate)
    if parsed.username is not None or parsed.password is not None:
        return None
    scheme, host, port = _parse_loopback_endpoint(candidate)
    if scheme not in ("http", "https") or host is None or port is None or port <= 0 or port > 65535:
        return None
    clean_scheme = "https" if scheme == "https" else "http"
    host_fmt = f"[{host}]" if ":" in host else host
    return f"{clean_scheme}://{host_fmt}:{int(port)}"


def _http_get_loopback_json(url: str, timeout_s: float) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    req_timeout = max(0.01, float(timeout_s))
    if urllib.request.urlopen is _STDLIB_URLOPEN:
        ctx = _LOOPBACK_HTTP_OPENER.open(req, timeout=req_timeout)
    else:
        ctx = urllib.request.urlopen(req, timeout=req_timeout)
    with ctx as resp:
        raw_bytes = resp.read(_MAX_PROBE_STDOUT_BYTES)
    if isinstance(raw_bytes, bytes):
        text = raw_bytes.decode("utf-8", errors="replace")
    else:
        text = str(raw_bytes or "")
    return json.loads(text)


def _probe_ollama_sync(
    clean_base_url: str,
    timeout_s: float,
    collected_at: str,
    http_get_fn: Callable[[str, float], Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    deadline = time.monotonic() + max(0.02, float(timeout_s))
    models_loaded: Optional[int] = None
    version = ""
    reachable = False

    rem_ps = deadline - time.monotonic()
    if rem_ps > 0:
        try:
            ps_data = http_get_fn(f"{clean_base_url}/api/ps", rem_ps)
            if isinstance(ps_data, Mapping):
                reachable = True
                models = ps_data.get("models")
                if isinstance(models, list):
                    models_loaded = len(models)
        except Exception:
            pass

    rem_ver = deadline - time.monotonic()
    if rem_ver > 0:
        try:
            ver_data = http_get_fn(f"{clean_base_url}/api/version", rem_ver)
            if isinstance(ver_data, Mapping):
                reachable = True
                raw_ver = ver_data.get("version")
                if isinstance(raw_ver, str) and raw_ver.strip():
                    version = _masked(raw_ver.strip(), 32)
        except Exception:
            pass

    source = f"ollama/{version}" if version else ("ollama" if reachable else "ollama_unreachable")
    meta: Dict[str, Any] = {
        "reachable": reachable,
        "status": "online" if reachable else "offline",
        "models_loaded": models_loaded,
        "version": version,
    }
    if models_loaded is not None:
        limits = normalize_limits_payload(
            {
                "unit": "models_loaded",
                "used": float(models_loaded),
                "remaining": None,
                "limit": None,
                "reset_at": None,
                "window_s": None,
                "source": source,
                "confidence": "exact",
                "collected_at": collected_at,
            },
            default_source="ollama",
            collected_at=collected_at,
        )
        return limits, meta

    return _default_unknown_limits(source=source, collected_at=collected_at), meta


def _parse_cli_limits_stdout(
    harness_id: str,
    stdout_bytes: bytes,
    collected_at: str,
) -> Dict[str, Any]:
    default_source = f"{harness_id}_cli"
    text = (stdout_bytes or b"")[:_MAX_PROBE_STDOUT_BYTES].decode("utf-8", errors="replace").strip()
    if not text:
        return _default_unknown_limits(source=default_source, collected_at=collected_at)
    try:
        parsed = json.loads(text)
    except ValueError:
        return _default_unknown_limits(source=default_source, collected_at=collected_at)
    if not isinstance(parsed, Mapping):
        return _default_unknown_limits(source=default_source, collected_at=collected_at)

    sub: Mapping[str, Any] = parsed
    for nested_key in ("limits", "quota", "rate_limit", "usage"):
        candidate = parsed.get(nested_key)
        if isinstance(candidate, Mapping):
            sub = candidate
            break

    def _first_float(keys: Sequence[str]) -> Optional[float]:
        for k in keys:
            val = _coerce_optional_float(sub.get(k))
            if val is not None:
                return val
        return None

    used = _first_float(("used", "credits_used", "acu_used", "tokens_used", "requests_used"))
    remaining = _first_float(
        (
            "remaining",
            "credits_remaining",
            "acu_remaining",
            "tokens_remaining",
            "requests_remaining",
        )
    )
    limit = _first_float(
        ("limit", "total", "max", "credits_limit", "acu_limit", "tokens_limit", "requests_limit")
    )
    if remaining is None and limit is not None and used is not None:
        remaining = max(0.0, limit - used)
    if used is None and limit is not None and remaining is not None:
        used = max(0.0, limit - remaining)

    raw_unit = str(sub.get("unit") or "").strip().lower()
    if not raw_unit:
        if any(
            k in sub
            for k in (
                "credits_used",
                "credits_remaining",
                "credits_limit",
                "acu_used",
                "acu_remaining",
                "acu_limit",
            )
        ):
            raw_unit = "credits"
        elif any(k in sub for k in ("tokens_used", "tokens_remaining", "tokens_limit")):
            raw_unit = "tokens"
        elif any(k in sub for k in ("requests_used", "requests_remaining", "requests_limit")):
            raw_unit = "requests"

    has_numeric = used is not None or remaining is not None or limit is not None
    raw_conf = str(sub.get("confidence") or "").strip().lower()
    if not raw_conf:
        if has_numeric:
            raw_conf = "estimated" if sub.get("estimated") is True else "exact"
        else:
            raw_conf = "unknown"

    reset_at: Optional[str] = None
    for k in ("reset_at", "resets_at", "reset", "period_end"):
        cand_reset = _coerce_optional_str(sub.get(k), 64)
        if cand_reset is not None:
            reset_at = cand_reset
            break

    window_s: Optional[int] = None
    for k in ("window_s", "window_seconds", "period_s"):
        cand_win = _coerce_optional_window_s(sub.get(k))
        if cand_win is not None:
            window_s = cand_win
            break

    return normalize_limits_payload(
        {
            "unit": raw_unit,
            "used": used,
            "remaining": remaining,
            "limit": limit,
            "reset_at": reset_at,
            "window_s": window_s,
            "source": sub.get("source") or parsed.get("source") or default_source,
            "confidence": raw_conf,
            "collected_at": sub.get("collected_at") or collected_at,
        },
        default_source=default_source,
        collected_at=collected_at,
    )


async def _cleanup_probe_process(proc: Any, job_guard: Any = None) -> None:
    is_real_proc = isinstance(proc, asyncio.subprocess.Process)
    if job_guard is not None:
        with suppress(Exception):
            job_guard.kill_all()
    if is_real_proc and sys.platform != "win32":
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, bool) and isinstance(pid, int) and pid > 0:
            try:
                from botex.edge.host_os_driver import kill_posix_process_group

                kill_posix_process_group(pid)
            except Exception:
                pass
    wait_fn = getattr(proc, "wait", None)
    try:
        with suppress(ProcessLookupError, OSError, Exception):
            proc.terminate()
        if callable(wait_fn) and getattr(proc, "returncode", None) is None:
            with suppress(TimeoutError, OSError, Exception):
                res = wait_fn()
                if inspect.isawaitable(res):
                    await asyncio.wait_for(res, timeout=0.01)
    finally:
        if getattr(proc, "returncode", None) is None:
            with suppress(ProcessLookupError, OSError, Exception):
                proc.kill()
    if callable(wait_fn):
        with suppress(TimeoutError, OSError, Exception):
            res2 = wait_fn()
            if inspect.isawaitable(res2):
                await asyncio.wait_for(res2, timeout=0.02)


class HarnessLimitsCollector:
    """Bounded non-blocking quota/headroom collector for local harnesses."""

    def __init__(
        self,
        *,
        cli_probes: Optional[Mapping[str, Sequence[str]]] = None,
        http_get_json: Optional[Callable[[str, float], Any]] = None,
    ) -> None:
        probes_src = DEFAULT_LIMITS_CLI_PROBES if cli_probes is None else cli_probes
        self._cli_probes: Dict[str, tuple[str, ...]] = {
            _normalize_harness_name(hid): tuple(coerce_text(arg) for arg in argv)
            for hid, argv in probes_src.items()
            if argv
        }
        self._http_get_json = http_get_json or _http_get_loopback_json

    @staticmethod
    def _clamp_budget(timeout_s: Any) -> float:
        if isinstance(timeout_s, bool):
            return _MAX_LIMITS_COLLECT_TIMEOUT_S
        try:
            val = float(timeout_s)
        except (TypeError, ValueError):
            return _MAX_LIMITS_COLLECT_TIMEOUT_S
        if not math.isfinite(val) or val <= 0:
            return _MAX_LIMITS_COLLECT_TIMEOUT_S
        return min(val, _MAX_LIMITS_COLLECT_TIMEOUT_S)

    async def _probe_cli_harness(
        self,
        harness_id: str,
        probe_timeout: float,
        collected_at: str,
    ) -> Dict[str, Any]:
        argv = self._cli_probes.get(harness_id)
        if not argv:
            return _default_unknown_limits(source="unprobed", collected_at=collected_at)
        cli_bin = argv[0]
        if not _is_bare_cli_name(cli_bin):
            return _default_unknown_limits(source="invalid_cli_probe", collected_at=collected_at)

        is_stdlib_exec = asyncio.create_subprocess_exec is _STDLIB_CREATE_SUBPROCESS_EXEC
        exe_target = cli_bin
        spawn_kwargs: Dict[str, Any] = {}
        if is_stdlib_exec:
            try:
                resolved = await asyncio.wait_for(
                    asyncio.to_thread(shutil.which, cli_bin),
                    timeout=max(0.01, probe_timeout * 0.5),
                )
            except (TimeoutError, Exception):
                resolved = None
            if resolved is None:
                return _default_unknown_limits(source="cli_unavailable", collected_at=collected_at)
            exe_target = resolved
            if sys.platform == "win32":
                spawn_kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NO_WINDOW", 0
                ) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                spawn_kwargs["start_new_session"] = True

        scrubbed_env = _scrub_probe_env()
        proc: Any = None
        job_guard: Any = None
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    exe_target,
                    *argv[1:],
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=scrubbed_env,
                    **spawn_kwargs,
                ),
                timeout=max(0.01, probe_timeout),
            )
            if isinstance(proc, asyncio.subprocess.Process) and sys.platform == "win32":
                pid = getattr(proc, "pid", None)
                if not isinstance(pid, bool) and isinstance(pid, int) and pid > 0:
                    try:
                        from botex.edge.host_os_driver import WindowsJobGuard

                        guard = WindowsJobGuard()
                        guard.assign(pid)
                        job_guard = guard
                    except Exception:
                        job_guard = None

            stdout_bytes, _stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=max(0.01, probe_timeout),
            )
        except TimeoutError:
            if proc is not None:
                await _cleanup_probe_process(proc, job_guard)
            return _default_unknown_limits(source="timeout", collected_at=collected_at)
        except asyncio.CancelledError:
            if proc is not None:
                await _cleanup_probe_process(proc, job_guard)
            raise
        except (FileNotFoundError, PermissionError, OSError):
            if proc is not None:
                await _cleanup_probe_process(proc, job_guard)
            return _default_unknown_limits(source="cli_unavailable", collected_at=collected_at)
        except Exception:
            if proc is not None:
                await _cleanup_probe_process(proc, job_guard)
            return _default_unknown_limits(source="probe_error", collected_at=collected_at)
        finally:
            if job_guard is not None:
                with suppress(Exception):
                    job_guard.kill_all()

        rc = getattr(proc, "returncode", 0)
        if rc not in (0, None):
            parsed = _parse_cli_limits_stdout(harness_id, stdout_bytes or b"", collected_at)
            if parsed.get("confidence") != "unknown":
                return parsed
            return _default_unknown_limits(
                source=f"{harness_id}_cli_error", collected_at=collected_at
            )
        return _parse_cli_limits_stdout(harness_id, stdout_bytes or b"", collected_at)

    async def _probe_ollama(
        self,
        ollama_base_url: str,
        probe_timeout: float,
        collected_at: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        clean_base = _validate_ollama_loopback_base_url(ollama_base_url)
        if clean_base is None:
            return (
                _default_unknown_limits(source="non_loopback_rejected", collected_at=collected_at),
                {
                    "reachable": False,
                    "status": "degraded",
                    "models_loaded": None,
                    "version": "",
                    "reason": "unsupported-or-non-loopback-endpoint",
                },
            )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _probe_ollama_sync,
                    clean_base,
                    probe_timeout,
                    collected_at,
                    self._http_get_json,
                ),
                timeout=max(0.01, probe_timeout),
            )
        except TimeoutError:
            return (
                _default_unknown_limits(source="timeout", collected_at=collected_at),
                {
                    "reachable": False,
                    "status": "degraded",
                    "models_loaded": None,
                    "version": "",
                    "reason": "connect-timeout",
                },
            )
        except Exception:
            return (
                _default_unknown_limits(source="ollama_unreachable", collected_at=collected_at),
                {
                    "reachable": False,
                    "status": "offline",
                    "models_loaded": None,
                    "version": "",
                },
            )

    async def _collect_with_meta(
        self,
        harness_ids: Optional[Sequence[str]] = None,
        *,
        timeout_s: float = _MAX_LIMITS_COLLECT_TIMEOUT_S,
        ollama_base_url: str = _DEFAULT_OLLAMA_BASE_URL,
        collected_at: Optional[str] = None,
    ) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        ts = _coerce_optional_str(collected_at, 64) or datetime.now(timezone.utc).isoformat()
        budget_s = self._clamp_budget(timeout_s)
        # Reserve cleanup headroom inside budget_s so subprocess termination completes before the outer ceiling.
        probe_timeout = (
            max(0.01, min(budget_s * 0.85, budget_s - 0.05))
            if budget_s > 0.08
            else max(0.01, budget_s * 0.7)
        )

        raw_targets = _DEFAULT_LIMITS_HARNESS_IDS if harness_ids is None else harness_ids
        targets: List[str] = []
        seen: set[str] = set()
        for raw_id in raw_targets:
            norm = _normalize_harness_name(raw_id)
            clean_id = sanitize_identifier(norm) or norm
            if clean_id and clean_id not in seen:
                seen.add(clean_id)
                targets.append(clean_id)

        out: Dict[str, Dict[str, Any]] = {
            hid: _default_unknown_limits(source="timeout", collected_at=ts) for hid in targets
        }
        ollama_meta: Dict[str, Any] = {
            "reachable": False,
            "status": "offline",
            "models_loaded": None,
            "version": "",
        }
        if not targets:
            return {}, ollama_meta

        async def _run_one(hid: str) -> None:
            nonlocal ollama_meta
            if hid == "ollama":
                limits, meta = await self._probe_ollama(ollama_base_url, probe_timeout, ts)
                out[hid] = limits
                ollama_meta = meta
            elif hid in self._cli_probes:
                out[hid] = await self._probe_cli_harness(hid, probe_timeout, ts)
            else:
                out[hid] = _default_unknown_limits(source="unprobed", collected_at=ts)

        try:
            await asyncio.wait_for(
                asyncio.gather(*(_run_one(hid) for hid in targets)),
                timeout=budget_s,
            )
        except (TimeoutError, Exception):
            pass
        return out, ollama_meta

    async def collect(
        self,
        harness_ids: Optional[Sequence[str]] = None,
        *,
        timeout_s: float = _MAX_LIMITS_COLLECT_TIMEOUT_S,
        ollama_base_url: str = _DEFAULT_OLLAMA_BASE_URL,
    ) -> Dict[str, Dict[str, Any]]:
        """Collect normalized ``limits`` per harness within ``<= 1.5s``; never raises."""
        try:
            limits_map, _meta = await self._collect_with_meta(
                harness_ids,
                timeout_s=timeout_s,
                ollama_base_url=ollama_base_url,
            )
            return limits_map
        except Exception:
            ts = datetime.now(timezone.utc).isoformat()
            raw_targets = (
                _DEFAULT_LIMITS_HARNESS_IDS if harness_ids is None else (harness_ids or ())
            )
            fallback: Dict[str, Dict[str, Any]] = {}
            for raw_id in raw_targets:
                norm = _normalize_harness_name(raw_id)
                clean_id = sanitize_identifier(norm) or norm
                if clean_id:
                    fallback[clean_id] = _default_unknown_limits(
                        source="probe_error", collected_at=ts
                    )
            return fallback


async def _gather_discovery_probes(
    cli_names: List[str],
    endpoint_map: Dict[str, str],
    timeout_s: float,
) -> tuple[Dict[str, bool], List[tuple[str, Dict[str, Any]]]]:
    cli_available, *endpoint_results = await asyncio.gather(
        asyncio.wait_for(
            asyncio.to_thread(_resolve_cli_availability, cli_names), timeout=timeout_s
        ),
        *(_probe_acp_endpoint(endpoint, timeout_s) for endpoint in endpoint_map.values()),
    )
    return cli_available, endpoint_results


async def discover_local_harnesses(
    *,
    acp_endpoints: Optional[Dict[str, str]] = None,
    allowlisted_clis: Optional[Dict[str, str]] = None,
    timeout_s: float = 1.5,
    ollama_base_url: Optional[str] = _DEFAULT_OLLAMA_BASE_URL,
    include_ollama: Optional[bool] = None,
    limits_collector: Optional[HarnessLimitsCollector] = None,
) -> List[Dict[str, Any]]:
    """Probe local harness availability and limits without blocking the event loop.

    Returns one record per probe target with the keys
    ``harness_id``/``transport``/``status``/``last_seen``/``capabilities``/``limits``;
    the whole pass is bounded by ``timeout_s`` (plus a small scheduling
    grace) and records never contain filesystem paths or endpoint hosts.
    """
    started_mono = time.monotonic()
    cli_map: Dict[str, str] = dict(
        DEFAULT_DISCOVERY_CLI_ALLOWLIST if allowlisted_clis is None else allowlisted_clis
    )
    endpoint_map: Dict[str, str] = {}
    for raw_id, endpoint in (acp_endpoints or {}).items():
        endpoint_map[_normalize_harness_name(raw_id)] = coerce_text(endpoint)
    try:
        timeout = float(timeout_s)
    except (TypeError, ValueError):
        timeout = 1.5
    if isinstance(timeout_s, bool) or math.isnan(timeout) or timeout <= 0:
        timeout = 1.5

    def _harness_id(raw: Any) -> str:
        norm = _normalize_harness_name(raw)
        return sanitize_identifier(norm) or norm

    collector = limits_collector or HarnessLimitsCollector()
    custom_collector = limits_collector is not None

    cli_available: Dict[str, bool] = {}
    endpoint_results: List[tuple[str, Dict[str, Any]]] = []
    if cli_map or endpoint_map:
        probe_names = sorted(
            {
                name
                for name in (coerce_text(value) for value in cli_map.values())
                if _is_bare_cli_name(name)
            }
        )
        try:
            cli_available, endpoint_results = await asyncio.wait_for(
                _gather_discovery_probes(probe_names, endpoint_map, timeout),
                timeout=timeout + _DISCOVERY_OUTER_GRACE_S,
            )
        except TimeoutError:
            cli_available, endpoint_results = {}, []

    observed_at = datetime.now(timezone.utc).isoformat()
    records: List[Dict[str, Any]] = []
    online_ids: set[str] = set()
    all_record_ids: List[str] = []

    for raw_id, cli_name in cli_map.items():
        harness_id = _harness_id(raw_id)
        if not harness_id:
            continue
        cli_name_clean = coerce_text(cli_name)
        found: Optional[bool] = cli_available.get(cli_name_clean)
        if not _is_bare_cli_name(cli_name_clean):
            found = None
        status = "online" if found else ("offline" if found is False else "degraded")
        if status == "online":
            online_ids.add(harness_id)
        all_record_ids.append(harness_id)
        records.append(
            {
                "harness_id": harness_id,
                "transport": "cli",
                "status": status,
                "last_seen": observed_at,
                "capabilities": {
                    "available": found is True,
                    "probe": "which",
                    "cli": cli_name_clean,
                },
            }
        )

    for idx, (raw_id, _endpoint) in enumerate(endpoint_map.items()):
        harness_id = _harness_id(raw_id)
        if not harness_id:
            continue
        if idx < len(endpoint_results):
            status, probe_capabilities = endpoint_results[idx]
        else:
            status, probe_capabilities = "degraded", {"probe": "tcp"}
        if status == "online":
            online_ids.add(harness_id)
        all_record_ids.append(harness_id)
        capabilities = dict(probe_capabilities)
        capabilities["available"] = status == "online"
        records.append(
            {
                "harness_id": harness_id,
                "transport": "acp",
                "status": status,
                "last_seen": observed_at,
                "capabilities": capabilities,
            }
        )

    ollama_queried = include_ollama is True or "ollama" in all_record_ids
    should_probe_ollama = include_ollama is not False and bool(
        coerce_text(ollama_base_url or "").strip()
    )

    limits_targets: List[str] = []
    for hid in all_record_ids:
        if hid == "ollama":
            continue
        if hid in online_ids and (hid in collector._cli_probes or custom_collector):
            limits_targets.append(hid)
    if should_probe_ollama or ollama_queried:
        limits_targets.append("ollama")

    limits_map: Dict[str, Dict[str, Any]] = {}
    ollama_meta: Dict[str, Any] = {
        "reachable": False,
        "status": "offline",
        "models_loaded": None,
        "version": "",
    }
    if limits_targets:
        elapsed = time.monotonic() - started_mono
        rem_budget = max(0.05, min(_MAX_LIMITS_COLLECT_TIMEOUT_S, timeout - elapsed))
        limits_map, ollama_meta = await collector._collect_with_meta(
            limits_targets,
            timeout_s=rem_budget,
            ollama_base_url=coerce_text(ollama_base_url or ""),
            collected_at=observed_at,
        )

    has_ollama_record = False
    for record in records:
        hid = record["harness_id"]
        if hid == "ollama":
            has_ollama_record = True
            if ollama_meta.get("models_loaded") is not None:
                record["capabilities"]["models_loaded"] = int(ollama_meta["models_loaded"])
            if ollama_meta.get("version"):
                record["capabilities"]["version"] = str(ollama_meta["version"])
        if hid in limits_map:
            record["limits"] = normalize_limits_payload(
                limits_map[hid], default_source="unprobed", collected_at=observed_at
            )
        else:
            fallback_src = "cli_unavailable" if record["status"] == "offline" else "unprobed"
            record["limits"] = _default_unknown_limits(
                source=fallback_src, collected_at=observed_at
            )

    if not has_ollama_record and (bool(ollama_meta.get("reachable")) or include_ollama is True):
        ollama_caps: Dict[str, Any] = {
            "available": bool(ollama_meta.get("reachable")),
            "probe": "http",
        }
        if ollama_meta.get("models_loaded") is not None:
            ollama_caps["models_loaded"] = int(ollama_meta["models_loaded"])
        if ollama_meta.get("version"):
            ollama_caps["version"] = str(ollama_meta["version"])
        if ollama_meta.get("reason"):
            ollama_caps["reason"] = str(ollama_meta["reason"])
        ollama_limits = normalize_limits_payload(
            limits_map.get("ollama"),
            default_source="ollama",
            collected_at=observed_at,
        )
        records.append(
            {
                "harness_id": "ollama",
                "transport": "http",
                "status": str(
                    ollama_meta.get("status")
                    or ("online" if ollama_meta.get("reachable") else "offline")
                ),
                "last_seen": observed_at,
                "capabilities": ollama_caps,
                "limits": ollama_limits,
            }
        )

    records.sort(key=lambda record: (record["harness_id"], record["transport"]))
    return records


class HarnessInventoryService:
    """Async background harness-inventory cache shared across workspace heartbeats."""

    def __init__(
        self,
        refresh_interval_s: float = 25.0,
        ttl_s: float = 30.0,
        discover_fn: Optional[Callable[..., Awaitable[List[Dict[str, Any]]]]] = None,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        if isinstance(refresh_interval_s, bool):
            interval = 25.0
        else:
            try:
                interval = float(refresh_interval_s)
            except (TypeError, ValueError):
                interval = 25.0
        if not math.isfinite(interval) or interval <= 0:
            interval = 25.0

        if isinstance(ttl_s, bool):
            ttl = 30.0
        else:
            try:
                ttl = float(ttl_s)
            except (TypeError, ValueError):
                ttl = 30.0
        if not math.isfinite(ttl) or ttl < 0:
            ttl = 30.0

        self._refresh_interval_s = max(0.01, interval)
        self._ttl_s = ttl
        self._discover_fn = discover_fn or discover_local_harnesses
        self._time_fn = time_fn or time.monotonic
        self._snapshot: List[Dict[str, Any]] = []
        self._last_refresh_mono: Optional[float] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._refresh_lock: Optional[asyncio.Lock] = None

    async def refresh_once(self) -> List[Dict[str, Any]]:
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        async with self._refresh_lock:
            try:
                res = self._discover_fn()
                if inspect.isawaitable(res):
                    res = await res
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.debug("harness inventory refresh failed (%s)", type(exc).__name__)
                return self.get_cached_snapshot()

            if isinstance(res, list):
                now_iso = datetime.now(timezone.utc).isoformat()
                cleaned: List[Dict[str, Any]] = []
                for item in res:
                    if not isinstance(item, Mapping):
                        continue
                    rec = copy.deepcopy(dict(item))
                    rec["limits"] = normalize_limits_payload(
                        rec.get("limits"),
                        default_source="inventory",
                        collected_at=str(rec.get("last_seen") or now_iso),
                    )
                    cleaned.append(rec)
                self._snapshot = cleaned
                self._last_refresh_mono = float(self._time_fn())
            return self.get_cached_snapshot()

    async def _run_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._refresh_interval_s)
            except TimeoutError:
                continue

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        self._task = asyncio.create_task(self._run_loop(stop_event), name="botex-harness-inventory")
        await asyncio.sleep(0)

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def get_cached_snapshot(self) -> List[Dict[str, Any]]:
        """Return a deep copy of the cached snapshot without awaiting I/O."""
        if not self._snapshot:
            return []
        out = copy.deepcopy(self._snapshot)
        is_stale = self._last_refresh_mono is None or (
            (float(self._time_fn()) - self._last_refresh_mono) > self._ttl_s
        )
        if is_stale:
            for record in out:
                limits = normalize_limits_payload(
                    record.get("limits"),
                    default_source="stale_cache",
                    collected_at=str(record.get("last_seen") or ""),
                )
                limits["confidence"] = "unknown"
                limits["source"] = "stale_cache"
                record["limits"] = limits
        return out


# ---------------------------------------------------------------------------
# Harness eligibility & downward mode mapping
# ---------------------------------------------------------------------------


def check_harness_eligible(target_harness: str) -> Dict[str, Any]:
    """Return eligibility for ``target_harness``; excluded/unknown -> BLOCKED."""
    norm = _normalize_harness_name(target_harness)
    if _is_excluded_harness(norm):
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": ANTIGRAVITY_EXCLUSION_REASON,
            "harness": norm,
        }
    if norm not in SUPPORTED_HARNESSES:
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": "unsupported_harness",
            "harness": norm,
        }
    transport = HARNESS_TRANSPORT.get(norm, "")
    out: Dict[str, Any] = {
        "ok": True,
        "status": "eligible",
        "harness": norm,
        "transport": transport,
    }
    if transport == "acp":
        out["transport_fallback"] = "stdout"
    return out


def _mode_block(harness: str, mode_request: str, detail: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "status": "BLOCKED",
        "reason": _MODE_BLOCK_REASON,
        "harness": harness,
        "mode_request": mode_request,
        "detail": detail,
    }


def normalize_mode_request(
    raw: Any, *, default: str = "readonly"
) -> tuple[str, Optional[Dict[str, Any]]]:
    """Normalize a requested mode, substituting ``default`` only for omitted/empty input."""
    if raw is None:
        req = str(default or "").strip().lower()
    else:
        req = str(raw).strip().lower()
        if not req:
            req = str(default or "").strip().lower()
    if req not in RESTRICTIVENESS_RANK:
        return "", _mode_block("", req, "mode_request_not_in_restrictiveness_rank")
    return req, None


def _resolve_mode(
    harness: str, mode_request: Any
) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Map ``mode_request`` to a harness mode, strictly downward in rank."""
    req = str(mode_request or "").strip().lower()
    req_rank = RESTRICTIVENESS_RANK.get(req)
    if req_rank is None:
        return None, _mode_block(harness, req, "mode_request_not_in_restrictiveness_rank")
    target = HARNESS_MODE_MAP.get(harness, {}).get(req)
    tgt_rank = RESTRICTIVENESS_RANK.get(target) if target else None
    if target is None:
        return None, _mode_block(harness, req, "no_safe_downward_mapping")
    if tgt_rank is None or tgt_rank > req_rank:
        return None, _mode_block(harness, req, "target_mode_would_widen_restrictiveness")
    return target, None


_BOTEX_DEFAULT_PROVIDER = "openrouter-free"
# Preference order when the spec carries no sub-provider and no explicit
# operator choice exists: free-tier providers first, never a silent paid hop.
_BOTEX_FREE_PROVIDER_PREF = (
    "openrouter-free",
    "nvidia",
    "google",
    "cloudflare",
    "groq",
    "ollama",
)


def _botex_dispatch_provider(sub_provider: str) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Explicit provider for a ``botex`` dispatch spec — sub-specifier wins
    (alias-resolved and validated), else a configured free provider."""
    sub = (sub_provider or "").strip().lower()
    try:
        from botex.server.config import load_config, resolve_provider

        provs = load_config().get("providers") or {}
    except Exception:
        provs = {}

        def resolve_provider(name):  # type: ignore[no-redef]
            return name, {}

    if sub:
        canon, pcfg = resolve_provider(sub)
        if not pcfg:
            return None, {
                "ok": False,
                "status": "BLOCKED",
                "reason": "unknown_provider",
                "provider": sub,
            }
        return canon, None
    for name in _BOTEX_FREE_PROVIDER_PREF:
        if name in provs:
            return name, None
    return _BOTEX_DEFAULT_PROVIDER, None


def translate_work_order(
    order: Any,
    target_harness: str,
    *,
    checkpoint_ref: str = "",
    blind_alley_note: str = "",
    mode_default: str = "edit",
) -> Dict[str, Any]:
    """Translate ``order`` into a fresh-epoch dispatch spec for ``target_harness``.

    Never emits ``--resume``: every hop runs clean in a new epoch worktree
    ``e<N+1>`` on ``swarm/wip/<order_id>/e<N+1>``. ``blind_alley_note`` carries
    the concise anti-spiral context (secret-masked) into the next hop.

    ``target_harness`` may carry a sub-provider specifier (``botex:nvidia``);
    for ``botex`` targets the result always names an explicit ``provider``.
    ``mode_default`` is used only when ``mode_request`` is omitted or empty.
    """
    target_name, _, sub_provider = str(target_harness or "").partition(":")
    elig = check_harness_eligible(target_name)
    if not elig.get("ok"):
        return dict(elig)
    harness = str(elig["harness"])

    if order is None:
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": "missing_order",
            "harness": harness,
        }

    raw_oid = _order_id(order)
    order_id = _clean_order_id(raw_oid)
    if order_id is None:
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": "invalid_order_id",
            "harness": harness,
            "order_id": raw_oid,
        }

    cur_status = str(_order_field(order, "status", "") or "").strip().upper()
    raw_epoch = max(0, _coerce_int(_order_field(order, "epoch", 0), 0))
    if cur_status == "HANDOFF" and raw_epoch > 0:
        base_epoch = raw_epoch - 1
        new_epoch = raw_epoch
    else:
        base_epoch = raw_epoch
        new_epoch = raw_epoch + 1
    branch = f"swarm/wip/{order_id}/e{new_epoch}"

    mode_request, mode_err = normalize_mode_request(
        _order_field(order, "mode_request", None), default=mode_default
    )
    if mode_err is not None:
        mode_err["harness"] = harness
        return mode_err
    mapped_mode, mode_err = _resolve_mode(harness, mode_request)
    if mode_err is not None:
        return mode_err

    masked_checkpoint = _masked(checkpoint_ref)
    masked_note = _masked(blind_alley_note)
    anti_spiral = bool(masked_checkpoint or masked_note)

    base_ref = str(_order_field(order, "base_ref", "HEAD") or "HEAD").strip() or "HEAD"
    scope_raw = _order_field(order, "scope", ()) or ()
    if isinstance(scope_raw, str):
        scope_list = [scope_raw]
    elif isinstance(scope_raw, (list, tuple)):
        scope_list = [str(s) for s in scope_raw]
    else:
        scope_list = []
    goal = _masked(_order_field(order, "goal", ""))
    verify_kind = _verify_kind(order)
    verify_command = _masked(_verify_field(order, "command", ""), limit=300)

    prompt_lines = [goal or f"Execute WorkOrder {order_id}."]
    if base_ref != "HEAD":
        prompt_lines.append(f"Base ref: {base_ref}")
    if scope_list:
        prompt_lines.append("Scope: " + ", ".join(scope_list))
    prompt_lines.append(f"WorkOrder {order_id} — fresh epoch e{new_epoch} on branch {branch}.")
    if masked_checkpoint:
        prompt_lines.append(f"Anti-spiral checkpoint ref: {masked_checkpoint}")
    if masked_note:
        prompt_lines.append(f"Anti-spiral blind-alley note: {masked_note}")
    if verify_kind != "none" and verify_command:
        prompt_lines.append(f"Required verification: {verify_command}")

    result: Dict[str, Any] = {
        "ok": True,
        "status": "READY",
        "harness": harness,
        "transport": HARNESS_TRANSPORT.get(harness, ""),
        "mode": mapped_mode,
        "mode_request": mode_request,
        "order_id": order_id,
        "epoch": new_epoch,
        "base_epoch": base_epoch,
        "branch": branch,
        "worktree": f"{order_id}_e{new_epoch}",
        "base_ref": base_ref,
        "scope": scope_list,
        "goal": goal,
        "verify": {"kind": verify_kind, "command": verify_command},
        "prompt": "\n".join(prompt_lines),
        "resume": False,
        "fresh_run": True,
        "fresh_epoch": True,
        "checkpoint_ref": masked_checkpoint,
        "blind_alley_note": masked_note,
        "anti_spiral_reset": anti_spiral,
    }
    if harness == "botex":
        provider, prov_err = _botex_dispatch_provider(sub_provider)
        if prov_err is not None:
            prov_err["harness"] = harness
            prov_err["order_id"] = order_id
            return prov_err
        result["provider"] = provider
    if result["transport"] == "acp":
        result["transport_fallback"] = "stdout"
        result["acp"] = {"protocol": "agent-client-protocol", "framing": "json-rpc/stdio"}
    return result


_DEVIN_PERMISSION_MODE_MAP: Dict[str, str] = {
    "readonly": "auto",
    "plan": "auto",
    "edit": "accept-edits",
    "destructive": "dangerous",
    "full": "dangerous",
}


def build_harness_argv(spec: dict, *, cli: str) -> List[str]:
    """Build the verified non-interactive CLI argv for a translated dispatch spec.

    Raw ACP server argv cannot carry the WorkOrder prompt as an argument. ACP
    harnesses therefore use the stdout fallback shape advertised by
    ``translate_work_order``; protocol-driven ACP startup belongs to a dedicated
    client path outside this argv builder.
    """
    if not isinstance(spec, Mapping):
        raise ValueError("invalid_spec")
    harness = _normalize_harness_name(spec.get("harness"))
    if harness not in SUPPORTED_HARNESSES:
        raise ValueError("unsupported_harness")
    executable = str(cli or "").strip()
    if not executable:
        raise ValueError("missing_cli")
    prompt = str(spec.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("missing_prompt")
    mode = str(spec.get("mode") or "").strip().lower()
    mapped_modes = {str(m) for m in HARNESS_MODE_MAP.get(harness, {}).values()}
    if mode not in mapped_modes:
        raise ValueError("unsupported_mode")

    if harness == "botex":
        argv = [executable, "run", "--mode", mode]
        provider = str(spec.get("provider") or "").strip()
        if provider:
            argv.extend(["--provider", provider])
        # Flags before "--" so a dash-leading prompt (Markdown bullet list)
        # is never parsed as an unknown CLI option.
        argv.extend(["--", prompt])
        return argv
    if harness == "zcode":
        # --prompt=<value> keeps dash-leading prompts out of the flag parser.
        return [executable, f"--prompt={prompt}", "--mode", mode, "--json", "--no-color"]

    permission_mode = _DEVIN_PERMISSION_MODE_MAP.get(mode)
    if permission_mode is None:
        raise ValueError("unsupported_mode")
    return [
        executable,
        "--process-model",
        "in-process",
        "--print",
        "--permission-mode",
        permission_mode,
        "--",
        prompt,
    ]


_PAID_FLAG_KEYS = ("allow_expensive", "allow_expensive_model", "paid", "allow_paid")
_PAID_QUOTA_KEYS = ("acu", "credits", "budget_usd", "max_cost_usd")
_HANDOFF_QUOTA_KEYS = ("acu", "credits", "budget_usd")
_PROVIDER_ALLOWLIST_KEYS = (
    "allowed_providers",
    "provider_allowlist",
    "provider_allowlists",
    "providers",
    "paid_allowlist",
)


def _ordered_payload_mappings(order: Any, body: Any) -> List[Mapping[str, Any]]:
    candidates = [order, body]
    if isinstance(body, Mapping):
        candidates.append(body.get("order"))
    out: List[Mapping[str, Any]] = []
    seen: set[int] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        out.append(candidate)
    return out


def _is_free_botex_provider(provider: Any) -> bool:
    clean = str(provider or "").strip().lower()
    if not clean:
        return False
    if clean in _BOTEX_FREE_PROVIDER_PREF:
        return True
    resolved, err = _botex_dispatch_provider(clean)
    if err is not None or not resolved:
        return False
    return str(resolved).strip().lower() in _BOTEX_FREE_PROVIDER_PREF


def _positive_cost(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        fval = float(value)
    except (TypeError, ValueError):
        return bool(str(value).strip())
    if not math.isfinite(fval) or fval < 0.0:
        # NaN compares False against 0.0; negative/NaN quotas fail closed as paid.
        return True
    return fval > 0.0


def _policy_value(policy: Any, name: str, default: Any = None) -> Any:
    if isinstance(policy, Mapping):
        return policy.get(name, default)
    return getattr(policy, name, default)


def _policy_value_present(policy: Any, name: str) -> bool:
    if isinstance(policy, Mapping):
        return name in policy and policy.get(name) is not None
    return getattr(policy, name, None) is not None


def _iter_text_entries(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        # Allowlists given as {"provider": True} must not silently skip validation.
        return [str(k) for k in value.keys() if str(k or "").strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value if str(item or "").strip()]
    return []


def _allowlist_entry_is_paid(entry: Any) -> bool:
    token = str(entry or "").strip().lower()
    if not token:
        return False
    if token.startswith("botex:"):
        token = token.split(":", 1)[1]
    token = token.lstrip("/").split("/", 1)[0].strip()
    if not token:
        # Malformed provider references ("/openai") fail closed as paid.
        return True
    return not _is_free_botex_provider(token)


def paid_ceiling_violations(order: dict, body: dict) -> List[str]:
    """Return static reason strings for any paid-capability signal in an order payload."""
    violations: List[str] = []

    def add(reason: str) -> None:
        if reason not in violations:
            violations.append(reason)

    mappings = _ordered_payload_mappings(order, body)
    for key in _PAID_FLAG_KEYS:
        if any(key in payload and bool(payload.get(key)) for payload in mappings):
            add(f"paid_key.{key}")
    for key in _PAID_QUOTA_KEYS:
        if any(key in payload and payload.get(key) is not None for payload in mappings):
            add(f"paid_key.{key}")
    for payload in mappings:
        provider = payload.get("provider")
        if provider is not None and str(provider).strip() and not _is_free_botex_provider(provider):
            add("paid_key.provider")
            break

    for key in ("target_harness", "harness"):
        for payload in mappings:
            raw_target = str(payload.get(key) or "")
            target_name, sep, sub_provider = raw_target.partition(":")
            if (
                sep
                and sub_provider.strip()
                and _normalize_harness_name(target_name) == "botex"
                and not _is_free_botex_provider(sub_provider)
            ):
                add("target_harness.paid_provider")

    policies: List[Any] = []
    seen_policies: set[int] = set()
    for payload in mappings:
        if "handoff_policy" not in payload:
            continue
        policy = payload.get("handoff_policy")
        if policy is None or id(policy) in seen_policies:
            continue
        seen_policies.add(id(policy))
        policies.append(policy)

    for policy in policies:
        if _positive_cost(_policy_value(policy, "max_cost_usd")):
            add("handoff_policy.max_cost_usd")
        for key in _HANDOFF_QUOTA_KEYS:
            if _policy_value_present(policy, key):
                add(f"handoff_policy.{key}")
        if bool(_policy_value(policy, "allow_paid", False)):
            add("handoff_policy.allow_paid")
        for key in _PROVIDER_ALLOWLIST_KEYS:
            entries = _iter_text_entries(_policy_value(policy, key))
            if not entries:
                continue
            if key == "paid_allowlist" or any(_allowlist_entry_is_paid(entry) for entry in entries):
                add("handoff_policy.provider_allowlist")
                break
    return violations


# ---------------------------------------------------------------------------
# Anti-Spiral Reset
# ---------------------------------------------------------------------------


def _epoch_from_ref(ref: Any) -> Optional[int]:
    match = _EPOCH_REF_RE.search(str(ref or ""))
    return int(match.group(1)) if match else None


def _reset_context(order_id: str, epoch: int, branch: str, ref: str, note: str) -> str:
    parts = [
        f"Anti-Spiral Reset for WorkOrder {order_id}: previous hops spiralled; "
        f"restart clean from the last good checkpoint on epoch e{epoch} ({branch})."
    ]
    if ref:
        parts.append(f"Checkpoint ref: {ref}")
    if note:
        parts.append(f"Blind-alley note (do not repeat): {note}")
    parts.append("Do not resume prior session state; --resume across harnesses is forbidden.")
    return "\n".join(parts)


def build_anti_spiral_reset(
    order: Any,
    *,
    last_checkpoint_ref: str = "",
    blind_alley_note: str = "",
    epoch: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a clean next-hop context from ``last_checkpoint_ref`` + masked note."""
    order_id = ""
    base_epoch: Optional[int] = None
    if order is not None:
        if isinstance(order, str):
            order_id = order.strip()
        else:
            order_id = _order_id(order)
            raw_epoch = _order_field(order, "epoch", None)
            if raw_epoch is not None:
                base_epoch = _coerce_int(raw_epoch, 0)
    clean_oid = _clean_order_id(order_id)
    if clean_oid is None:
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": "invalid_order_id",
            "order_id": order_id,
        }
    if epoch is not None:
        base_epoch = max(0, _coerce_int(epoch, 0))
    if base_epoch is None:
        base_epoch = _epoch_from_ref(last_checkpoint_ref)
    if base_epoch is None:
        base_epoch = 0
    new_epoch = base_epoch + 1
    ref = _masked(last_checkpoint_ref)
    note = _masked(blind_alley_note)
    branch = f"swarm/wip/{clean_oid}/e{new_epoch}"
    return {
        "ok": True,
        "status": "anti_spiral_reset",
        "order_id": clean_oid,
        "epoch": new_epoch,
        "branch": branch,
        "worktree": f"{clean_oid}_e{new_epoch}",
        "checkpoint_ref": ref,
        "blind_alley_note": note,
        "resume": False,
        "fresh_run": True,
        "handoff_context": _reset_context(clean_oid, new_epoch, branch, ref, note),
    }


# ---------------------------------------------------------------------------
# Epoch Checkpoint Manager
# ---------------------------------------------------------------------------


def _run_git(
    repo_dir: Union[str, Path],
    args: list[str],
    *,
    timeout_s: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo_dir), *args]
    env = dict(os.environ)
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    env["SSH_ASKPASS"] = "true"
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            cmd, 124, stdout="", stderr=coerce_text(mask_secrets(str(exc)))
        )


def _invoke_epoch_fn(
    fn: Callable[..., Any],
    *,
    order_id: str,
    epoch: int,
    remote: str,
    branch: str,
    repo_dir: str,
) -> Any:
    """Call ``fetch_remote_epoch_fn`` tolerantly: kwargs subset or no-arg."""
    kwargs = {
        "order_id": order_id,
        "epoch": epoch,
        "remote": remote,
        "branch": branch,
        "repo_dir": repo_dir,
    }
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(**kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**kwargs)
    accepted = {
        p.name
        for p in params.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    if not accepted:
        return fn()
    return fn(**{k: v for k, v in kwargs.items() if k in accepted})


def _coerce_remote_epoch(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Mapping):
        for key in ("remote_epoch", "epoch", "fencing_epoch"):
            if key in value:
                return _coerce_remote_epoch(value.get(key))
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else None


def _push_result_ok(pres: Any) -> tuple[bool, str]:
    if pres is None or pres is True:
        return True, ""
    if isinstance(pres, Mapping):
        if pres.get("ok") is False:
            return False, _masked(pres.get("error") or pres.get("stderr") or "push_failed", 300)
        rc = pres.get("returncode", pres.get("rc"))
        if rc not in (None, 0):
            return False, _masked(pres.get("stderr") or pres.get("error") or "push_failed", 300)
        return True, ""
    rc = getattr(pres, "returncode", None)
    if rc is None or rc == 0:
        return True, ""
    return False, _masked(getattr(pres, "stderr", "") or "push_failed", 300)


class EpochCheckpointManager:
    """Auto-checkpoint WIP with pre-push ``remote_epoch == local_epoch`` fencing.

    ``record_turn`` counts file-mutating turns (and green intermediate tests);
    ``should_checkpoint`` fires once the count reaches the configured interval
    inside ``[min_turns, max_turns]`` or a green test is pending.
    ``checkpoint_and_push`` verifies the remote epoch (via
    ``fetch_remote_epoch_fn`` callback, or ``git ls-remote`` on the swarm
    remote when a repo is known) before pushing
    ``swarm/wip/<order_id>/e<epoch>`` with LF normalization flags. A mismatch
    aborts with ``status="stale_epoch"`` — the push never happens.
    """

    def __init__(
        self,
        order: Any = None,
        *,
        order_id: str = "",
        epoch: Optional[int] = None,
        origin_repo: Optional[Union[str, Path]] = None,
        worktree_root: Optional[Union[str, Path]] = None,
        swarm_remote: str = "",
        min_turns: int = 5,
        max_turns: int = 8,
        interval: Optional[int] = None,
        fetch_remote_epoch_fn: Optional[Callable[..., Any]] = None,
        push_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        resolved_id = str(order_id or "").strip()
        resolved_epoch = epoch
        if order is not None and not isinstance(order, str):
            resolved_id = resolved_id or _order_id(order)
            if resolved_epoch is None:
                resolved_epoch = _order_field(order, "epoch", 0)
        elif isinstance(order, str) and not resolved_id:
            resolved_id = order.strip()
        clean_id = _clean_order_id(resolved_id)
        if clean_id is None:
            raise ValueError(f"EpochCheckpointManager requires a valid order_id: {resolved_id!r}")
        self._order_id = clean_id
        self._epoch = max(0, _coerce_int(resolved_epoch, 0))

        self._repo_dir = Path(origin_repo).expanduser() if origin_repo is not None else None
        self._worktree_root = (
            Path(worktree_root).expanduser() if worktree_root is not None else None
        )
        self._swarm_remote = (
            str(swarm_remote or "").strip()
            or os.environ.get("BOTEX_SWARM_REMOTE", "").strip()
            or "swarm"
        )

        self._min_turns = int(min_turns)
        self._max_turns = int(max_turns)
        if self._min_turns < 1 or self._max_turns < self._min_turns:
            raise ValueError(
                f"invalid turn bounds: min_turns={self._min_turns} max_turns={self._max_turns}"
            )
        self._interval = (
            self._min_turns
            if interval is None
            else min(max(int(interval), self._min_turns), self._max_turns)
        )

        self._fetch_remote_epoch_fn = fetch_remote_epoch_fn
        self._push_fn = push_fn
        self._mutating_turns = 0
        self._green_pending = False
        self._total_turns = 0
        self._last_checkpoint_ref = ""

    # -- introspection ------------------------------------------------------

    @property
    def order_id(self) -> str:
        return self._order_id

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def interval(self) -> int:
        return self._interval

    @property
    def mutating_turns(self) -> int:
        return self._mutating_turns

    @property
    def total_turns(self) -> int:
        return self._total_turns

    @property
    def last_checkpoint_ref(self) -> str:
        return self._last_checkpoint_ref

    @property
    def branch(self) -> str:
        return f"swarm/wip/{self._order_id}/e{self._epoch}"

    # -- turn accounting ----------------------------------------------------

    def record_turn(
        self,
        *,
        mutated_files: bool = False,
        green_test: bool = False,
    ) -> Dict[str, Any]:
        """Record one executed turn; returns the current checkpoint snapshot."""
        self._total_turns += 1
        if mutated_files:
            self._mutating_turns += 1
        if green_test:
            self._green_pending = True
        return {
            "total_turns": self._total_turns,
            "mutating_turns": self._mutating_turns,
            "green_pending": self._green_pending,
            "should_checkpoint": self.should_checkpoint(),
        }

    def should_checkpoint(self) -> bool:
        """True when the mutating-turn interval elapsed or a green test is pending."""
        return bool(self._green_pending or self._mutating_turns >= self._interval)

    def mark_checkpointed(self, ref: str = "") -> None:
        """Reset turn counters after a successful checkpoint/push."""
        self._mutating_turns = 0
        self._green_pending = False
        if ref:
            self._last_checkpoint_ref = str(ref)

    # -- epoch resolution ---------------------------------------------------

    def _git_remote_epoch(
        self, repo_dir: Path, remote: str, order_id: str, local_epoch: int
    ) -> Optional[int]:
        """Highest ``e<N>`` epoch branch for this order on ``remote`` (None on error).

        No matching remote refs yields ``local_epoch`` — an empty remote cannot
        conflict with the local epoch. Production pushes should still inject
        ``fetch_remote_epoch_fn`` bound to the VPS epoch record.
        """
        res = _run_git(
            repo_dir,
            ["ls-remote", remote, f"refs/heads/swarm/wip/{order_id}/e*"],
            timeout_s=60.0,
        )
        if res.returncode != 0:
            return None
        prefix = f"swarm/wip/{order_id}/e"
        epochs = [
            int(m.group(1))
            for line in res.stdout.splitlines()
            if prefix in line and (m := re.search(r"/e(\d+)\s*$", line.strip()))
        ]
        return max(epochs) if epochs else local_epoch

    def _resolve_remote_epoch(
        self,
        *,
        repo_dir: Optional[Path],
        remote: str,
        branch: str,
        order_id: str,
        epoch: int,
        fetch_remote_epoch_fn: Optional[Callable[..., Any]],
    ) -> tuple[Optional[int], str]:
        fn = (
            fetch_remote_epoch_fn
            if fetch_remote_epoch_fn is not None
            else self._fetch_remote_epoch_fn
        )
        if fn is not None:
            raw = _invoke_epoch_fn(
                fn,
                order_id=order_id,
                epoch=epoch,
                remote=remote,
                branch=branch,
                repo_dir=str(repo_dir or ""),
            )
            epoch = _coerce_remote_epoch(raw)
            return epoch, "callback"
        if repo_dir is not None:
            return self._git_remote_epoch(repo_dir, remote, order_id, epoch), "git_ls_remote"
        return None, "none"

    # -- push ----------------------------------------------------------------

    def checkpoint_and_push(
        self,
        *,
        repo_dir: Optional[Union[str, Path]] = None,
        remote: str = "",
        order_id: str = "",
        epoch: Optional[int] = None,
        branch: str = "",
        commit: bool = True,
        commit_message: str = "",
        dry_run: bool = False,
        fetch_remote_epoch_fn: Optional[Callable[..., Any]] = None,
        push_fn: Optional[Callable[..., Any]] = None,
        timeout_s: float = 120.0,
    ) -> Dict[str, Any]:
        """Commit WIP (optional) and push ``branch`` after the epoch fence check.

        Returns ``status="stale_epoch"`` without pushing whenever the remote
        epoch disagrees with the local one, or ``status="epoch_check_failed"``
        when no epoch source is available — fail-stop either way.
        """
        oid = _clean_order_id(order_id) if str(order_id or "").strip() else self._order_id
        if oid is None:
            return {
                "ok": False,
                "status": "BLOCKED",
                "reason": "invalid_order_id",
                "order_id": str(order_id or ""),
            }
        ep = self._epoch if epoch is None else max(0, _coerce_int(epoch, self._epoch))
        rem = str(remote or "").strip() or self._swarm_remote
        br = str(branch or "").strip() or f"swarm/wip/{oid}/e{ep}"
        repo = Path(repo_dir).expanduser() if repo_dir is not None else self._repo_dir

        base = {
            "order_id": oid,
            "epoch": ep,
            "local_epoch": ep,
            "branch": br,
            "remote": rem,
        }
        try:
            remote_epoch, epoch_source = self._resolve_remote_epoch(
                repo_dir=repo,
                remote=rem,
                branch=br,
                order_id=oid,
                epoch=ep,
                fetch_remote_epoch_fn=fetch_remote_epoch_fn,
            )
        except Exception as exc:
            return {
                "ok": False,
                "status": "epoch_check_failed",
                "error": _masked(str(exc), 300),
                "epoch_source": "error",
                **base,
            }
        base["epoch_source"] = epoch_source
        if remote_epoch is None:
            return {
                "ok": False,
                "status": "epoch_check_failed",
                "error": "remote_epoch_unavailable",
                **base,
            }
        remote_epoch = int(remote_epoch)
        if remote_epoch != ep:
            return {
                "ok": False,
                "status": "stale_epoch",
                "error": "EPOCH_MISMATCH",
                "remote_epoch": remote_epoch,
                **base,
            }
        if dry_run:
            return {"ok": True, "status": "dry_run", "remote_epoch": remote_epoch, **base}

        committed = ""
        if commit and repo is not None and repo.exists():
            _run_git(repo, ["add", "-A"], timeout_s=timeout_s)
            status_res = _run_git(repo, ["status", "--porcelain"], timeout_s=timeout_s)
            if status_res.returncode == 0 and status_res.stdout.strip():
                msg = _masked(commit_message or f"swarm checkpoint {oid} e{ep}", 200)
                commit_res = _run_git(
                    repo,
                    [
                        "-c",
                        "user.name=botex-edge",
                        "-c",
                        "user.email=botex-edge@localhost",
                        "commit",
                        "--allow-empty",
                        "-m",
                        msg,
                    ],
                    timeout_s=timeout_s,
                )
                if commit_res.returncode != 0:
                    return {
                        "ok": False,
                        "status": "commit_failed",
                        "error": _masked(commit_res.stderr or commit_res.stdout, 300),
                        **base,
                    }
                committed = (commit_res.stdout.strip().splitlines() or [""])[0]

        pfn = push_fn if push_fn is not None else self._push_fn
        if pfn is not None:
            try:
                pres = pfn(
                    repo_dir=str(repo or ""),
                    remote=rem,
                    branch=br,
                    order_id=oid,
                    epoch=ep,
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "status": "push_failed",
                    "error": _masked(str(exc), 300),
                    **base,
                }
            push_ok, push_err = _push_result_ok(pres)
            if not push_ok:
                return {
                    "ok": False,
                    "status": "push_failed",
                    "error": push_err,
                    **base,
                }
        else:
            if repo is None:
                return {
                    "ok": False,
                    "status": "push_failed",
                    "error": "repo_dir_required",
                    **base,
                }
            push_res = _run_git(
                repo,
                [
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "core.eol=lf",
                    "push",
                    rem,
                    f"HEAD:refs/heads/{br}",
                ],
                timeout_s=timeout_s,
            )
            if push_res.returncode != 0:
                return {
                    "ok": False,
                    "status": "push_failed",
                    "error": _masked(push_res.stderr or push_res.stdout, 300),
                    **base,
                }

        self.mark_checkpointed(br)
        return {
            "ok": True,
            "status": "pushed",
            "remote_epoch": remote_epoch,
            "commit": committed,
            "pushed_ref": f"{rem}/{br}",
            **base,
        }

    # -- anti-spiral ---------------------------------------------------------

    def build_anti_spiral_reset(
        self,
        order: Any = None,
        *,
        last_checkpoint_ref: str = "",
        blind_alley_note: str = "",
    ) -> Dict[str, Any]:
        """Clean next-hop context from the last checkpoint + masked note."""
        return build_anti_spiral_reset(
            self._order_id if order is None else order,
            last_checkpoint_ref=last_checkpoint_ref or self._last_checkpoint_ref,
            blind_alley_note=blind_alley_note,
            epoch=self._epoch,
        )


# ---------------------------------------------------------------------------
# Terminal verify hook (no_self_report_done)
# ---------------------------------------------------------------------------


def _set_order_status(order: Any, status: str) -> bool:
    try:
        if isinstance(order, dict):
            order["status"] = status
        else:
            order.status = status
        return True
    except Exception:
        return False


_TERMINAL_ORDER_STATUSES = frozenset({"DONE", "FAILED", "BLOCKED", "NEEDS_HUMAN", "FENCED"})


def _transition_order_status(
    order: Any,
    status: str,
    *,
    via_verify_gate: bool = False,
    reason: str = "",
) -> Optional[Dict[str, Any]]:
    """Route ``order`` through the canonical supervisor state machine when possible.

    Returns the transition result dict for real ``WorkOrder`` instances, or
    ``None`` for dict/duck-typed orders and when ``botex.core.work_orders`` is
    not importable yet — adapters must work regardless of import order.
    """
    try:
        from botex.core.work_orders import WorkOrder, WorkOrderSupervisor
    except Exception:
        return None
    if not isinstance(order, WorkOrder):
        return None
    try:
        return WorkOrderSupervisor().transition(
            order, status, reason=reason, via_verify_gate=via_verify_gate
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": str(getattr(order, "status", "") or ""),
            "reason": _masked(str(exc), 200),
        }


def _apply_order_status(
    order: Any,
    status: str,
    *,
    via_verify_gate: bool = False,
    reason: str = "",
) -> tuple[bool, str]:
    """Set ``order.status`` — canonical supervisor transition for ``WorkOrder``, direct for dicts."""
    tr = _transition_order_status(order, status, via_verify_gate=via_verify_gate, reason=reason)
    if tr is not None:
        if tr.get("ok"):
            return True, "supervisor"
        return False, "rejected"
    if _set_order_status(order, status):
        return True, "direct"
    return False, "failed"


def _brief_result(res: Any) -> Dict[str, Any]:
    if res is None:
        return {"ok": True}
    if isinstance(res, Mapping):
        out: Dict[str, Any] = {"ok": bool(res.get("ok", True))}
        for key in ("status", "error", "reason"):
            if res.get(key):
                out[key] = str(res.get(key))
        return out
    return {"ok": True}


def _invoke_refresh(fn: Callable[..., Any], slug: str) -> Dict[str, Any]:
    res = fn(slug)
    if not inspect.isawaitable(res):
        return _brief_result(res)
    try:
        asyncio.get_running_loop()
    except RuntimeError:

        async def _wait(awaitable: Any) -> Any:
            return await awaitable

        return _brief_result(asyncio.run(_wait(res)))
    # Inside a running loop: schedule the coroutine; the DONE transition must
    # not block on Cloud I/O.
    task = asyncio.ensure_future(res)
    return {"ok": True, "scheduled": True, "task": task.get_name()}


def finalize_verified_work_order(
    order: Any,
    verify_result: Optional[Mapping[str, Any]] = None,
    *,
    bot_slug: str = "",
    refresh_brief_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Finalize ``order`` after external verification; ``DONE`` only on ``ok is True``.

    ``verify.kind == "none"`` can never auto-complete — the order lands in
    ``NEEDS_HUMAN`` regardless of the result payload. On success the order is
    marked ``DONE`` and the bot brief refresh is invoked (``refresh_brief_fn``
    or ``botex.ops.curator.brief_curator.refresh_bot_brief`` when ``bot_slug``
    is provided). Brief-refresh failures never revoke ``DONE``.
    """
    if order is None:
        return {"ok": False, "status": "BLOCKED", "reason": "missing_order"}
    order_id = _order_id(order)
    cur_status = str(_order_field(order, "status", "") or "").strip().upper()
    if cur_status in _TERMINAL_ORDER_STATUSES:
        return {
            "ok": cur_status == "DONE",
            "status": cur_status,
            "reason": "order_terminal",
            "order_id": order_id,
        }
    kind = _verify_kind(order)

    try:
        from botex.core.work_orders import WorkOrder, WorkOrderSupervisor
    except Exception:
        WorkOrder = None  # type: ignore[assignment]

    if WorkOrder is not None and isinstance(order, WorkOrder):
        sup_out = WorkOrderSupervisor().apply_verify_result(order, verify_result)
        if not sup_out.get("ok") or order.status != "DONE":
            return {
                "ok": False,
                "status": order.status,
                "reason": _masked(sup_out.get("reason") or "", 300),
                "order_id": order_id,
                "verify_kind": kind,
            }
        status_set, status_via = True, "WorkOrderSupervisor.apply_verify_result"
    else:
        if kind == "none":
            status_set, status_via = _apply_order_status(
                order, "NEEDS_HUMAN", reason="verify_kind_none"
            )
            return {
                "ok": False,
                "status": "NEEDS_HUMAN",
                "reason": "verify_kind_none",
                "order_id": order_id,
                "verify_kind": kind,
                "status_set": status_set,
                "status_via": status_via,
            }

        res = verify_result if isinstance(verify_result, Mapping) else {}
        if res.get("ok") is not True:
            return {
                "ok": False,
                "status": str(res.get("status") or res.get("verdict") or "VERIFY_FAILED"),
                "reason": _masked(res.get("reason") or "", 300),
                "order_id": order_id,
                "verify_kind": kind,
            }

        status_set, status_via = _apply_order_status(
            order, "DONE", via_verify_gate=True, reason="external_verify_passed"
        )

    brief: Dict[str, Any] = {"skipped": True}
    target_fn = refresh_brief_fn
    if target_fn is None and str(bot_slug or "").strip():
        try:
            from botex.ops.curator.brief_curator import refresh_bot_brief as target_fn
        except Exception as exc:
            target_fn = None
            brief = {
                "ok": False,
                "error": _masked(f"brief_curator_import_failed: {exc}", 300),
            }
    if target_fn is not None:
        try:
            brief = _invoke_refresh(target_fn, str(bot_slug or "").strip())
        except Exception as exc:
            brief = {"ok": False, "error": _masked(str(exc), 300)}

    return {
        "ok": True,
        "status": "DONE",
        "order_id": order_id,
        "verify_kind": kind,
        "status_set": status_set,
        "status_via": status_via,
        "brief_refresh": brief,
    }
