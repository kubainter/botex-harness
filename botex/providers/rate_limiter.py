# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Provider Rate Limiter
===========================
Asynchronous per-provider token-bucket throttling for the cloud task queue.

Each provider configured in the ``rate_limits`` config section gets up to
two token buckets -- a request bucket (RPM) and a token bucket (TPM).
Queue workers ``acquire`` request capacity BEFORE taking an engine
concurrency slot, so a task parked on a provider cooldown never starves
other providers' queue slots.

Reserve-and-wait scheduling: ``acquire`` books its tokens immediately
(allowing the bucket to run a bounded deficit) and sleeps for the debt
payoff. Consecutive waiters therefore reserve consecutive slices of
future capacity, which yields deterministic FIFO ordering without locks --
every critical section below is synchronous code, hence atomic under
asyncio cooperative scheduling.

A provider-side 429 opens a cooldown window (``report_limited``): the
server-provided ``Retry-After`` hint wins over the configured
``cooldown_s`` fallback, and subsequent ``acquire`` calls sleep the
window out instead of stampeding the same upstream limit.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

try:
    from botex.server.config import load_config
except (ImportError, ValueError):
    from config import load_config


def _norm_name(provider: Any) -> str:
    """Normalize a provider name for bucket lookup (lowercased slug)."""
    return str(provider or "").strip().lower()


def _num(value: Any) -> Optional[float]:
    """Coerce a JSON scalar to float; bools and junk degrade to None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _pos(value: Any, default: float = 0.0) -> float:
    """Non-negative float coercion with a fallback for missing/junk input."""
    parsed = _num(value)
    if parsed is None or parsed < 0.0:
        return default
    return parsed


class TokenBucket:
    """One async token bucket with reserve-and-wait FIFO scheduling.

    ``capacity`` is the burst ceiling and ``rate_per_s`` the steady-state
    refill. The balance may go negative while waiters are queued -- the
    deficit is already-booked capacity being refilled. A bucket with
    ``rate_per_s <= 0`` never refills; it admits requests while the
    balance lasts and then stops waiting entirely (degenerate config --
    throttling disabled).
    """

    __slots__ = ("_capacity", "_rate", "_tokens", "_updated", "_clock", "_sleep")

    def __init__(
        self,
        capacity: float,
        rate_per_s: float,
        *,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self._capacity = max(0.0, float(capacity))
        self._rate = max(0.0, float(rate_per_s))
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._tokens = self._capacity
        self._updated = self._clock()

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def rate(self) -> float:
        return self._rate

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0.0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._updated = now

    @property
    def available(self) -> float:
        """Projected balance right now (negative while debt is queued)."""
        now = self._clock()
        projected = self._tokens + max(0.0, now - self._updated) * self._rate
        return min(self._capacity, projected)

    def wait_for(self, amount: float = 1.0) -> float:
        """Estimated seconds until ``amount`` tokens are available."""
        if self._rate <= 0.0:
            return 0.0
        return max(0.0, (float(amount) - self.available) / self._rate)

    def charge(self, amount: float) -> None:
        """Deduct tokens synchronously (post-hoc usage accounting).

        May push the bucket into debt -- later ``acquire`` calls wait the
        deficit off. Contains no await, so it stays atomic for asyncio
        callers between their own awaits.
        """
        self._refill()
        self._tokens -= max(0.0, float(amount))

    async def acquire(self, amount: float = 1.0) -> float:
        """Book ``amount`` tokens, sleeping off the deficit. Returns wait seconds."""
        amount = float(amount)
        if amount <= 0.0:
            return 0.0
        start = self._clock()
        # Synchronous critical section -- atomic under cooperative asyncio.
        self._refill()
        self._tokens -= amount
        deficit = -self._tokens if self._tokens < 0.0 else 0.0
        wait_s = deficit / self._rate if self._rate > 0.0 else 0.0
        if wait_s > 0.0:
            try:
                await self._sleep(wait_s)
            except asyncio.CancelledError:
                # The reservation never reached the wire -- refund it so a
                # cancelled waiter does not burn provider budget.
                self._refill()
                self._tokens = min(self._capacity, self._tokens + amount)
                raise
        return self._clock() - start


@dataclass(frozen=True)
class ProviderLimit:
    """Normalized per-provider throttle limits from the ``rate_limits`` section."""

    rpm: float = 0.0
    tpm: float = 0.0
    burst_multiplier: float = 1.0
    cooldown_s: float = 0.0


def parse_rate_limits(raw: Any) -> Dict[str, ProviderLimit]:
    """Normalize the ``rate_limits`` config section into ProviderLimit entries."""
    limits: Dict[str, ProviderLimit] = {}
    if not isinstance(raw, dict):
        return limits
    for name, entry in raw.items():
        pname = _norm_name(name)
        if not pname or not isinstance(entry, dict):
            continue
        limits[pname] = ProviderLimit(
            rpm=_pos(entry.get("rpm")),
            tpm=_pos(entry.get("tpm")),
            burst_multiplier=_pos(entry.get("burst_multiplier"), 1.0),
            cooldown_s=_pos(entry.get("cooldown_s")),
        )
    return limits


class ProviderRateLimiter:
    """Async per-provider token-bucket limiter with post-429 cooldowns.

    Built from the ``rate_limits`` config section::

        {"<provider>": {"rpm": 20, "tpm": 100000,
                        "burst_multiplier": 1.2, "cooldown_s": 60.0}}

    ``rpm`` / ``tpm`` set each bucket's refill rate (per minute); bucket
    capacity is the limit scaled by ``burst_multiplier``. Providers with
    no entry are unthrottled; providers with an entry but no ``rpm`` are
    request-unlimited and may still carry a ``tpm`` token budget.
    """

    def __init__(
        self,
        limits: Optional[Dict[str, Any]] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._limits = parse_rate_limits(limits)
        self._req_buckets: Dict[str, TokenBucket] = {}
        self._tok_buckets: Dict[str, TokenBucket] = {}
        self._cooldown_until: Dict[str, float] = {}

    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]] = None) -> "ProviderRateLimiter":
        """Build a limiter from a merged config dict (defaults to load_config())."""
        merged = cfg if cfg is not None else load_config()
        section = (merged or {}).get("rate_limits")
        return cls(section if isinstance(section, dict) else None)

    # -- internals -----------------------------------------------------------

    def _req_bucket(self, name: str) -> Optional[TokenBucket]:
        bucket = self._req_buckets.get(name)
        if bucket is None:
            lim = self._limits.get(name)
            if lim is None or lim.rpm <= 0.0:
                return None
            bucket = TokenBucket(
                capacity=lim.rpm * lim.burst_multiplier,
                rate_per_s=lim.rpm / 60.0,
                clock=self._clock,
                sleep=self._sleep,
            )
            self._req_buckets[name] = bucket
        return bucket

    def _tok_bucket(self, name: str) -> Optional[TokenBucket]:
        bucket = self._tok_buckets.get(name)
        if bucket is None:
            lim = self._limits.get(name)
            if lim is None or lim.tpm <= 0.0:
                return None
            bucket = TokenBucket(
                capacity=lim.tpm * lim.burst_multiplier,
                rate_per_s=lim.tpm / 60.0,
                clock=self._clock,
                sleep=self._sleep,
            )
            self._tok_buckets[name] = bucket
        return bucket

    # -- public API ----------------------------------------------------------

    def providers(self) -> List[str]:
        """Names of providers with configured limits."""
        return sorted(self._limits.keys())

    def is_throttled(self, provider: str) -> bool:
        """True when the provider has a configured limit entry."""
        return _norm_name(provider) in self._limits

    def cooldown_remaining(self, provider: str) -> float:
        """Seconds left in the provider's post-429 cooldown window."""
        return max(0.0, self._cooldown_until.get(_norm_name(provider), 0.0) - self._clock())

    def wait_estimate(self, provider: str) -> float:
        """Approximate seconds until the next request may start on this provider."""
        name = _norm_name(provider)
        if not name or name not in self._limits:
            return 0.0
        wait = self.cooldown_remaining(name)
        bucket = self._req_buckets.get(name)
        if bucket is not None:
            wait = max(wait, bucket.wait_for(1.0))
        return wait

    async def acquire(
        self, provider: str = "", *, requests: float = 1.0, est_tokens: float = 0.0
    ) -> float:
        """Wait until the provider's budget permits a request.

        Charges ``requests`` (>=1) against the RPM bucket and
        ``est_tokens`` against the TPM bucket when the caller knows its
        request size. Returns total seconds waited -- 0.0 when the
        provider is not throttled.
        """
        name = _norm_name(provider)
        if not name or name not in self._limits:
            return 0.0
        waited = 0.0
        remaining = self.cooldown_remaining(name)
        if remaining > 0.0:
            await self._sleep(remaining)
            waited += remaining
        bucket = self._req_bucket(name)
        if bucket is not None:
            waited += await bucket.acquire(max(1.0, _pos(requests, 1.0)))
        bucket = self._tok_bucket(name)
        if bucket is not None:
            est = _pos(est_tokens)
            if est > 0.0:
                waited += await bucket.acquire(est)
        return waited

    def report_limited(self, provider: str, retry_after_s: Optional[float] = None) -> float:
        """Register an upstream 429 and open a provider-wide cooldown.

        The server-provided ``retry_after_s`` hint wins over the
        configured ``cooldown_s`` fallback. Cooldowns are monotonic -- a
        later, shorter hint cannot shrink an open window. Returns the
        cooldown applied (0.0 when the provider is not throttled).
        """
        name = _norm_name(provider)
        lim = self._limits.get(name) if name else None
        if lim is None:
            return 0.0
        wait = _pos(retry_after_s) if _num(retry_after_s) is not None else lim.cooldown_s
        if wait <= 0.0:
            return 0.0
        until = self._clock() + wait
        if until > self._cooldown_until.get(name, 0.0):
            self._cooldown_until[name] = until
        return wait

    def record_tokens(self, provider: str, tokens: float) -> None:
        """Retroactively charge measured token usage to the provider's TPM bucket."""
        amount = _pos(tokens)
        if amount <= 0.0:
            return
        bucket = self._tok_bucket(_norm_name(provider))
        if bucket is not None:
            bucket.charge(amount)

    def stats(self) -> Dict[str, Any]:
        """Snapshot of configured limits and live bucket levels for diagnostics."""
        out: Dict[str, Any] = {}
        for name, lim in self._limits.items():
            req = self._req_buckets.get(name)
            tok = self._tok_buckets.get(name)
            out[name] = {
                "rpm": lim.rpm,
                "tpm": lim.tpm,
                "burst_multiplier": lim.burst_multiplier,
                "requests_available": round(req.available, 3) if req is not None else None,
                "tokens_available": round(tok.available, 3) if tok is not None else None,
                "cooldown_s": round(self.cooldown_remaining(name), 3),
                "wait_s": round(self.wait_estimate(name), 3),
            }
        return out


_shared_limiter: Optional[ProviderRateLimiter] = None
_shared_guard = threading.Lock()


def get_rate_limiter() -> ProviderRateLimiter:
    """Process-wide limiter, lazily built once from the ``rate_limits`` config.

    Mirrors ``server._TASK_SEMAPHORE``: config is read once at first use --
    changing ``rate_limits`` takes effect on the next worker process.
    """
    global _shared_limiter
    if _shared_limiter is None:
        with _shared_guard:
            if _shared_limiter is None:
                _shared_limiter = ProviderRateLimiter.from_config()
    return _shared_limiter


def reset_rate_limiter() -> None:
    """Drop the shared limiter (test isolation / config reload boundary)."""
    global _shared_limiter
    with _shared_guard:
        _shared_limiter = None
