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

from openai import APIConnectionError, APITimeoutError

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


# Applied when neither ``providers.<name>.circuit_breaker`` nor
# ``engine.circuit_breaker`` declares a block. Enabled by default — the
# breaker only short-circuits providers already failing server-side, so
# an absent config fails open (normal traffic) rather than silently
# disabling protection.
_DEFAULT_CIRCUIT_BREAKER: Dict[str, Any] = {
    "enabled": True,
    "failure_threshold": 3,
    "recovery_timeout_s": 60.0,
}


def resolve_circuit_breaker_config(raw: Any) -> Dict[str, Any]:
    """Merge a ``circuit_breaker`` config block over the defaults.

    ``raw`` may be a dict, a bare bool (``circuit_breaker: false``
    disables the breaker for that provider/run), or None (defaults,
    breaker active). Malformed numeric values clamp back to defaults.
    """
    cfg = dict(_DEFAULT_CIRCUIT_BREAKER)
    if isinstance(raw, dict):
        for key in cfg:
            if key in raw and raw[key] is not None:
                cfg[key] = raw[key]
    elif raw is not None:
        cfg["enabled"] = bool(raw)
    try:
        threshold = int(float(cfg["failure_threshold"]))
    except (TypeError, ValueError):
        threshold = int(_DEFAULT_CIRCUIT_BREAKER["failure_threshold"])
    try:
        timeout = float(cfg["recovery_timeout_s"])
    except (TypeError, ValueError):
        timeout = float(_DEFAULT_CIRCUIT_BREAKER["recovery_timeout_s"])
    return {
        "enabled": bool(cfg["enabled"]),
        "failure_threshold": max(1, threshold),
        "recovery_timeout_s": max(1.0, timeout),
    }


def _is_server_side_failure(provider: Any, exc: BaseException) -> bool:
    """Whether ``exc`` is a provider-health failure worth counting.

    Only failures that say something about the provider trip the
    breaker: retryable rejections (429/connection/provider-blessed
    overloads), arbitrary 5xx, and connection errors. Auth/policy
    (401/403/404) and local request timeouts never count — a breaker
    must not mask config errors, and APITimeoutError may still be
    executing server-side (it subclasses APIConnectionError, so it is
    checked first).
    """
    if isinstance(exc, APITimeoutError):
        return False
    try:
        if provider.is_retryable(exc):
            return True
    except Exception:
        pass
    status = status_code_of(exc)
    if isinstance(status, int) and status >= 500:
        return True
    return isinstance(exc, APIConnectionError)


def _note_breaker_outcome(
    breaker: Optional["ProviderCircuitBreaker"],
    breaker_name: str,
    provider: Any,
    exc: BaseException,
) -> None:
    """Record one failed logical request — or free a claimed HALF_OPEN probe.

    A probe ending in a non-counted failure still frees the slot;
    otherwise the provider would stay blocked behind a probe that
    never reported.
    """
    if breaker is None:
        return
    if _is_server_side_failure(provider, exc):
        breaker.record_failure(breaker_name, exc)
    else:
        breaker.release_probe(breaker_name)


def _note_breaker_success(breaker: Optional["ProviderCircuitBreaker"], breaker_name: str) -> None:
    if breaker is not None:
        breaker.record_success(breaker_name)


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
    circuit_breaker: Optional["ProviderCircuitBreaker"] = None,
    circuit_breaker_cfg: Any = None,
    provider_name: str = "",
) -> Any:
    """Call ``factory()`` retrying transient unbilled failures.

    ``factory`` is re-invoked per attempt; it must be idempotent. The
    caller owns what goes inside — for streaming, create() plus the full
    chunk collection belongs in one unit so mid-stream failures retry.

    Daily-quota errors re-raise immediately: the retry horizon is hours
    and the correct reaction is fallback, not waiting. Non-retryable
    errors (5xx other than a provider-blessed overload, timeouts, auth)
    pass straight through.

    When the circuit breaker is enabled, a provider in OPEN state raises
    ``CircuitBreakerOpenError`` before ``factory()`` ever runs — a dead
    provider fails fast instead of burning request budget. Each logical
    invocation records at most one failure (on the final raise), so a
    single retried request can never trip the breaker alone.
    ``circuit_breaker_cfg`` is the raw ``circuit_breaker`` block
    (provider override or engine default); ``provider_name`` names the
    breaker bucket when the adapter lacks ``.name``.
    """
    cfg = retry_cfg or resolve_retry_config((provider_cfg or {}).get("retry"))
    cb_cfg = resolve_circuit_breaker_config(
        circuit_breaker_cfg
        if circuit_breaker_cfg is not None
        else (provider_cfg or {}).get("circuit_breaker")
    )
    breaker: Optional[ProviderCircuitBreaker] = None
    breaker_name = ""
    if cb_cfg["enabled"]:
        breaker = (
            circuit_breaker
            if circuit_breaker is not None
            else get_circuit_breaker(
                failure_threshold=cb_cfg["failure_threshold"],
                recovery_timeout_s=cb_cfg["recovery_timeout_s"],
            )
        )
        breaker_name = str(getattr(provider, "name", "") or provider_name or "default")
        # Fail-fast before the first attempt — a tripped provider never
        # burns rate-limiter wait budget on a request doomed upstream.
        breaker.can_execute(breaker_name)
    if not cfg["enabled"]:
        try:
            result = await factory()
        except Exception as exc:
            _note_breaker_outcome(breaker, breaker_name, provider, exc)
            raise
        _note_breaker_success(breaker, breaker_name)
        return result
    sleep_fn = sleep or asyncio.sleep
    waits = 0
    while True:
        try:
            result = await factory()
        except Exception as exc:
            if waits >= cfg["max_waits"] or not provider.is_retryable(exc) or is_daily_quota(exc):
                _note_breaker_outcome(breaker, breaker_name, provider, exc)
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
                    _note_breaker_outcome(breaker, breaker_name, provider, exc)
                    raise
                delay = min(delay, remaining)
            if on_wait is not None:
                on_wait(delay, exc)
            await sleep_fn(delay)
            waits += 1
        else:
            _note_breaker_success(breaker, breaker_name)
            return result


def describe_retry_error(exc: BaseException, max_chars: int = 160) -> str:
    """Bounded, secret-masked error text for retry wait logs."""
    text = mask_secrets(str(exc))
    return text if len(text) <= max_chars else text[:max_chars] + "..."


class CircuitBreakerOpenError(RuntimeError):
    """Raised when a provider's circuit breaker is in the OPEN state."""

    def __init__(self, provider_name: str, recovery_remaining_s: float):
        super().__init__(
            f"Circuit breaker for provider '{provider_name}' is OPEN. "
            f"Recovery probing in {recovery_remaining_s:.1f}s."
        )
        self.provider_name = provider_name
        self.recovery_remaining_s = recovery_remaining_s


class ProviderCircuitBreaker:
    """Stateful circuit breaker guarding failing providers across runs.

    States:
      - CLOSED: Normal operation; failures are counted.
      - OPEN: Tripped after consecutive server-side/network failures;
              fail-fast without network timeouts.
      - HALF_OPEN: Recovery probe window; single trial allowed.
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_s: float = 60.0,
    ):
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout_s = max(1.0, recovery_timeout_s)
        self._states: Dict[str, str] = {}
        self._consecutive_failures: Dict[str, int] = {}
        self._tripped_at: Dict[str, float] = {}
        # Providers with a recovery probe currently in flight — HALF_OPEN
        # admits exactly one trial so concurrent callers cannot stampede
        # a recovering provider and instantly re-trip it.
        self._probe_in_flight: Dict[str, bool] = {}

    def get_state(self, provider_name: str, now: Optional[float] = None) -> str:
        name = str(provider_name or "default").lower().strip()
        state = self._states.get(name, self.CLOSED)
        if state == self.OPEN:
            current_time = now if now is not None else time.time()
            tripped = self._tripped_at.get(name, 0.0)
            if (current_time - tripped) >= self.recovery_timeout_s:
                self._states[name] = self.HALF_OPEN
                return self.HALF_OPEN
        return state

    def can_execute(self, provider_name: str) -> bool:
        name = str(provider_name or "default").lower().strip()
        state = self.get_state(provider_name)
        if state == self.OPEN:
            tripped = self._tripped_at.get(name, 0.0)
            remaining = max(0.0, self.recovery_timeout_s - (time.time() - tripped))
            raise CircuitBreakerOpenError(name, remaining)
        if state == self.HALF_OPEN:
            if self._probe_in_flight.get(name):
                raise CircuitBreakerOpenError(name, 0.0)
            self._probe_in_flight[name] = True
        return True

    def release_probe(self, provider_name: str) -> None:
        """Free a claimed HALF_OPEN probe slot without recording an outcome.

        A probe ending in a non-counted failure (auth error, local
        timeout) still frees the slot — otherwise the provider stays
        blocked behind a probe that never reported.
        """
        name = str(provider_name or "default").lower().strip()
        self._probe_in_flight.pop(name, None)

    def record_success(self, provider_name: str) -> None:
        name = str(provider_name or "default").lower().strip()
        self._states[name] = self.CLOSED
        self._consecutive_failures[name] = 0
        self._tripped_at.pop(name, None)
        self._probe_in_flight.pop(name, None)

    def record_failure(self, provider_name: str, exc: Optional[BaseException] = None) -> None:
        name = str(provider_name or "default").lower().strip()
        self._probe_in_flight.pop(name, None)
        current_state = self.get_state(provider_name)

        # If in HALF_OPEN, any failure immediately trips back to OPEN
        if current_state == self.HALF_OPEN:
            self._states[name] = self.OPEN
            self._tripped_at[name] = time.time()
            return

        fails = self._consecutive_failures.get(name, 0) + 1
        self._consecutive_failures[name] = fails
        if fails >= self.failure_threshold:
            self._states[name] = self.OPEN
            self._tripped_at[name] = time.time()

    def reset(self, provider_name: Optional[str] = None) -> None:
        if provider_name is not None:
            name = str(provider_name).lower().strip()
            self._states.pop(name, None)
            self._consecutive_failures.pop(name, None)
            self._tripped_at.pop(name, None)
            self._probe_in_flight.pop(name, None)
        else:
            self._states.clear()
            self._consecutive_failures.clear()
            self._tripped_at.clear()
            self._probe_in_flight.clear()


_CIRCUIT_BREAKER_INSTANCE: Optional[ProviderCircuitBreaker] = None


def get_circuit_breaker(
    failure_threshold: Optional[int] = None,
    recovery_timeout_s: Optional[float] = None,
) -> ProviderCircuitBreaker:
    """Process-wide breaker, lazily built once.

    Optional tuning applies only on first construction — the singleton
    is the single owner of provider state, so per-call overrides are
    ignored afterwards (mirrors ``get_rate_limiter``: config is read
    once at first use).
    """
    global _CIRCUIT_BREAKER_INSTANCE
    if _CIRCUIT_BREAKER_INSTANCE is None:
        _CIRCUIT_BREAKER_INSTANCE = ProviderCircuitBreaker(
            failure_threshold=failure_threshold or 3,
            recovery_timeout_s=recovery_timeout_s or 60.0,
        )
    return _CIRCUIT_BREAKER_INSTANCE


def reset_circuit_breaker() -> None:
    """Drop the shared breaker (test isolation / config reload boundary)."""
    global _CIRCUIT_BREAKER_INSTANCE
    _CIRCUIT_BREAKER_INSTANCE = None
