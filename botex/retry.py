# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Retry Layer
=================
Single owner of transient-failure backoff for provider calls. When the
custom layer is active the SDK client is built with ``max_retries=0`` —
one layer owns the retry policy so hidden SDK retries can never multiply
waits or billed requests.

Retryable means *unbilled* rejections only: HTTP 429 and connection
errors (provider classes may widen the rule via ``is_retryable`` — e.g.
Google's 503 "model is overloaded"). Arbitrary 5xx and timeouts are
never retried: the request may already have executed server-side, and a
retry would double-bill it.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import random
import re
import time
from typing import Any, Awaitable, Callable, Dict, Optional

try:
    from .security import mask_secrets
except (ImportError, ValueError):
    from security import mask_secrets

# Applied when the provider config carries no ``retry`` block. Enabled by
# default: every OpenAI-compatible provider shares this layer (the SDK's
# own retries are then disabled at client build time).
_DEFAULT_RETRY: Dict[str, Any] = {
    "enabled": True,
    "max_waits": 2,
    "max_wait_s": 60.0,
    "base_delay_s": 1.0,
    "jitter_s": 1.5,
}

_RETRY_IN_RE = re.compile(r"\bretry\s+in\s+(\d+(?:\.\d+)?)\s*s\b", re.IGNORECASE)
_DELAY_FIELD_RE = re.compile(
    r'"(?:retryDelay|quotaResetDelay)"\s*:\s*"?\s*(\d+(?:\.\d+)?)\s*s?"', re.IGNORECASE
)
# Daily-quota detection needs BOTH a per-day token and a quota context —
# "daily" alone appears in unrelated messages and must not trip the
# immediate-fallback path.
_DAILY_TOKEN_RE = re.compile(r"per[\s_-]?day|daily", re.IGNORECASE)
_QUOTA_CTX_RE = re.compile(r"quota|limit|exceed", re.IGNORECASE)


def resolve_retry_config(raw: Any) -> Dict[str, Any]:
    """Merge a ``providers.<name>.retry`` config block over the defaults.

    ``raw`` may be a dict, a bare bool (``retry: false`` disables the
    layer entirely — SDK retries then stay on, keeping a single owner),
    or None (defaults, layer enabled)."""
    cfg = dict(_DEFAULT_RETRY)
    if isinstance(raw, dict):
        for key in cfg:
            if key in raw and raw[key] is not None:
                cfg[key] = raw[key]
    elif raw is not None:
        cfg["enabled"] = bool(raw)
    return {
        "enabled": bool(cfg["enabled"]),
        "max_waits": max(0, round(cfg["max_waits"])),
        "max_wait_s": max(0.0, float(cfg["max_wait_s"])),
        "base_delay_s": max(0.0, float(cfg["base_delay_s"])),
        "jitter_s": max(0.0, float(cfg["jitter_s"])),
    }


def status_code_of(exc: BaseException) -> Optional[int]:
    """Extract an HTTP status code from an SDK or duck-typed exception."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _exc_text(exc: BaseException) -> str:
    """Error text plus a serialized body for pattern scanning."""
    body = getattr(exc, "body", None)
    try:
        body_text = json.dumps(body, default=str) if body else ""
    except (TypeError, ValueError):
        body_text = str(body)
    return f"{exc} {body_text}"


def _header_delay_s(exc: BaseException) -> Optional[float]:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    # A past date is not a delay hint — defer to exponential backoff
    # rather than returning 0.0 and hot-looping the retry.
    delay = dt.timestamp() - time.time()
    return delay if delay > 0 else None


def parse_retry_delay(exc: BaseException) -> Optional[float]:
    """Best-effort server-provided wait hint in seconds.

    Order: ``Retry-After`` header (seconds or HTTP-date), then body/text
    fields — Google reports ``retryDelay``/``quotaResetDelay`` in the
    error payload and "Please retry in Xs" in the message rather than a
    header. ``None`` means the caller should use its own backoff.
    """
    header_delay = _header_delay_s(exc)
    if header_delay is not None:
        return header_delay
    text = _exc_text(exc)
    match = _DELAY_FIELD_RE.search(text) or _RETRY_IN_RE.search(text)
    if match:
        try:
            return max(0.0, float(match.group(1)))
        except (TypeError, ValueError):
            return None
    return None


def is_daily_quota(exc: BaseException) -> bool:
    """True when the failure is a daily-quota exhaustion.

    Daily limits reset in hours, not seconds — waiting is always wrong,
    so the caller must fall back immediately instead of sleeping.
    """
    text = _exc_text(exc)
    return bool(_DAILY_TOKEN_RE.search(text) and _QUOTA_CTX_RE.search(text))


async def call_with_backoff(
    provider: Any,
    factory: Callable[[], Awaitable[Any]],
    provider_cfg: Optional[Dict[str, Any]] = None,
    *,
    retry_cfg: Optional[Dict[str, Any]] = None,
    deadline_ts: Optional[float] = None,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    on_wait: Optional[Callable[[float, BaseException], None]] = None,
) -> Any:
    """Call ``factory()`` retrying transient unbilled failures.

    ``factory`` is re-invoked per attempt; it must be idempotent. The
    caller owns what goes inside — for streaming, create() plus the full
    chunk collection belongs in one unit so mid-stream failures retry.

    Daily-quota errors re-raise immediately: the retry horizon is hours
    and the correct reaction is fallback, not waiting. Non-retryable
    errors (5xx other than a provider-blessed overload, timeouts, auth)
    pass straight through.
    """
    cfg = retry_cfg or resolve_retry_config((provider_cfg or {}).get("retry"))
    if not cfg["enabled"]:
        return await factory()
    sleep_fn = sleep or asyncio.sleep
    waits = 0
    while True:
        try:
            return await factory()
        except Exception as exc:
            if waits >= cfg["max_waits"] or not provider.is_retryable(exc) or is_daily_quota(exc):
                raise
            delay = provider.retry_after_s(exc)
            if delay is None:
                delay = cfg["base_delay_s"] * (2**waits) + random.uniform(0.0, cfg["jitter_s"])
            delay = min(delay, cfg["max_wait_s"])
            if deadline_ts is not None:
                # Spend whatever budget remains instead of aborting
                # outright — a partial wait still buys one more attempt.
                remaining = deadline_ts - time.time()
                if remaining <= 0:
                    raise
                delay = min(delay, remaining)
            if on_wait is not None:
                on_wait(delay, exc)
            await sleep_fn(delay)
            waits += 1


def describe_retry_error(exc: BaseException, max_chars: int = 160) -> str:
    """Bounded, secret-masked error text for retry wait logs."""
    text = mask_secrets(str(exc))
    return text if len(text) <= max_chars else text[:max_chars] + "..."
