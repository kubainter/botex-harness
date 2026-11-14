# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Tests for the async token-bucket ProviderRateLimiter
====================================================
1. TokenBucket refill / reserve-and-wait / cancellation semantics.
2. ProviderRateLimiter per-provider gating, TPM charging, post-429 cooldowns.
3. Config parsing hardening (bool/negative/junk rate_limits entries).
4. FastAPI background task queue integration (admission gate + cooldown feedback).
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from botex.rate_limiter import (
    ProviderRateLimiter,
    TokenBucket,
    get_rate_limiter,
    parse_rate_limits,
    reset_rate_limiter,
)
from botex.server_cloud import _note_provider_result, app


class FakeClock:
    """Deterministic monotonic clock advanced only by the fake sleep."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))


def _limiter(
    limits: Optional[dict] = None,
    clock: Optional[FakeClock] = None,
) -> tuple[ProviderRateLimiter, FakeClock]:
    clk = clock or FakeClock()
    return (
        ProviderRateLimiter(
            limits
            or {"prov": {"rpm": 60, "tpm": 6000, "burst_multiplier": 1.0, "cooldown_s": 30.0}},
            clock=clk,
            sleep=clk.sleep,
        ),
        clk,
    )


# ---------------------------------------------------------------------------
# 1. Config parsing
# ---------------------------------------------------------------------------


def test_parse_rate_limits_normalizes_entries():
    raw = {
        "OpenRouter-Free": {"rpm": 20, "tpm": "100000", "burst_multiplier": 1.2, "cooldown_s": 60},
        "empty": {},
        "junk": "not-a-dict",
    }
    limits = parse_rate_limits(raw)
    assert set(limits.keys()) == {"openrouter-free", "empty"}
    free = limits["openrouter-free"]
    assert free.rpm == 20.0
    assert free.tpm == 100000.0
    assert free.burst_multiplier == 1.2
    assert free.cooldown_s == 60.0
    assert limits["empty"].rpm == 0.0
    assert limits["empty"].burst_multiplier == 1.0


def test_parse_rate_limits_rejects_bools_and_negatives():
    limits = parse_rate_limits(
        {
            "p": {
                "rpm": True,
                "tpm": -5,
                "burst_multiplier": "nope",
                "cooldown_s": None,
            }
        }
    )
    p = limits["p"]
    # bools and negative/junk values degrade to safe defaults
    assert p.rpm == 0.0
    assert p.tpm == 0.0
    assert p.burst_multiplier == 1.0
    assert p.cooldown_s == 0.0


def test_parse_rate_limits_non_dict_input():
    assert parse_rate_limits(None) == {}
    assert parse_rate_limits("x") == {}
    assert parse_rate_limits([1, 2]) == {}


def test_from_config_reads_rate_limits_section():
    limiter = ProviderRateLimiter.from_config(
        {
            "rate_limits": {
                "nvidia": {"rpm": 40, "tpm": 150000, "burst_multiplier": 1.5, "cooldown_s": 30.0}
            }
        }
    )
    assert limiter.is_throttled("NVIDIA")
    assert not limiter.is_throttled("google")
    assert limiter.providers() == ["nvidia"]


def test_from_config_missing_section_is_empty():
    assert ProviderRateLimiter.from_config({}).providers() == []
    # None falls back to the merged repo config (rate_limits committed there).
    assert ProviderRateLimiter.from_config(None).providers() == [
        "google",
        "nvidia",
        "openrouter",
        "openrouter-free",
    ]


# ---------------------------------------------------------------------------
# 2. TokenBucket
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bucket_admits_burst_then_paces():
    clk = FakeClock()
    bucket = TokenBucket(capacity=2.0, rate_per_s=1.0, clock=clk, sleep=clk.sleep)
    assert await bucket.acquire() == 0.0
    assert await bucket.acquire() == 0.0
    waited = await bucket.acquire()
    assert waited == pytest.approx(1.0)
    assert clk.t == pytest.approx(1.0)


@pytest.mark.anyio
async def test_bucket_refills_over_time():
    clk = FakeClock()
    bucket = TokenBucket(capacity=1.0, rate_per_s=2.0, clock=clk, sleep=clk.sleep)
    await bucket.acquire()
    clk.t += 0.5  # half a token refilled
    assert await bucket.acquire(0.5) == 0.0
    assert bucket.available == pytest.approx(0.5)


@pytest.mark.anyio
async def test_bucket_acquire_zero_amount_is_free():
    bucket = TokenBucket(capacity=1.0, rate_per_s=1.0)
    assert await bucket.acquire(0.0) == 0.0
    assert await bucket.acquire(-5.0) == 0.0
    assert bucket.available == pytest.approx(1.0)


@pytest.mark.anyio
async def test_bucket_fifo_reservation_slices():
    """Concurrent waiters reserve consecutive slices of future capacity."""
    clk = FakeClock()
    waits: List[float] = []

    async def hang(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.Event().wait()  # never resolves -- cancelled by the test

    bucket = TokenBucket(capacity=1.0, rate_per_s=1.0, clock=clk, sleep=hang)
    await bucket.acquire()  # drain the only token
    t1 = asyncio.create_task(bucket.acquire())
    t2 = asyncio.create_task(bucket.acquire())
    await asyncio.sleep(0)  # let both register their reservations
    assert waits == pytest.approx([1.0, 2.0])
    t1.cancel()
    t2.cancel()
    for task in (t1, t2):
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_bucket_cancel_refunds_reservation():
    clk = FakeClock()
    waits: List[float] = []

    async def hang(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.Event().wait()

    bucket = TokenBucket(capacity=1.0, rate_per_s=1.0, clock=clk, sleep=hang)
    await bucket.acquire()  # tokens: 1 -> 0
    pending = asyncio.create_task(bucket.acquire())  # reserves -> -1, hangs
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    # Reservation refunded: balance returns to 0, not -1.
    assert bucket.available == pytest.approx(0.0)
    assert waits == pytest.approx([1.0])


@pytest.mark.anyio
async def test_bucket_charge_pushes_debt_and_paces_next_acquire():
    clk = FakeClock()
    bucket = TokenBucket(capacity=10.0, rate_per_s=10.0, clock=clk, sleep=clk.sleep)
    bucket.charge(15.0)  # over-spend: 10 -> -5
    assert bucket.available == pytest.approx(-5.0)
    assert bucket.wait_for(1.0) == pytest.approx(0.6)
    waited = await bucket.acquire()
    assert waited == pytest.approx(0.6)


@pytest.mark.anyio
async def test_bucket_zero_rate_never_blocks():
    clk = FakeClock()
    bucket = TokenBucket(capacity=1.0, rate_per_s=0.0, clock=clk, sleep=clk.sleep)
    assert await bucket.acquire() == 0.0
    assert await bucket.acquire() == 0.0
    assert bucket.wait_for(1.0) == 0.0


@pytest.mark.anyio
async def test_bucket_real_sleep_paces_acquires():
    """Sanity check against the real clock: ~20ms/token."""
    bucket = TokenBucket(capacity=1.0, rate_per_s=50.0)
    await bucket.acquire()
    waited = await bucket.acquire()
    assert waited >= 0.015


# ---------------------------------------------------------------------------
# 3. ProviderRateLimiter
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_acquire_unconfigured_provider_passes_through():
    limiter, _ = _limiter()
    assert await limiter.acquire("unknown-provider") == 0.0
    assert await limiter.acquire("") == 0.0


@pytest.mark.anyio
async def test_acquire_rpm_gating():
    limiter, clk = _limiter({"prov": {"rpm": 1.0, "burst_multiplier": 1.0}})
    assert await limiter.acquire("prov") == 0.0
    waited = await limiter.acquire("prov")
    assert waited == pytest.approx(60.0)  # 1 request/minute
    assert clk.t == pytest.approx(60.0)


@pytest.mark.anyio
async def test_acquire_burst_multiplier_scales_capacity():
    limiter, _ = _limiter({"prov": {"rpm": 10.0, "burst_multiplier": 2.0}})
    # capacity = 20 requests; 20 acquires free, 21st paced
    for _ in range(20):
        assert await limiter.acquire("prov") == 0.0
    assert await limiter.acquire("prov") == pytest.approx(6.0)


@pytest.mark.anyio
async def test_acquire_tpm_est_tokens():
    limiter, _ = _limiter({"prov": {"tpm": 100.0}})
    assert await limiter.acquire("prov", est_tokens=100.0) == 0.0
    waited = await limiter.acquire("prov", est_tokens=60.0)
    # 60 tokens at 100/minute -> 36s
    assert waited == pytest.approx(36.0)


@pytest.mark.anyio
async def test_acquire_skips_tpm_when_estimate_zero():
    limiter, _ = _limiter({"prov": {"tpm": 50.0}})
    # est_tokens=0 drains nothing from the token bucket
    assert await limiter.acquire("prov", est_tokens=0.0) == 0.0
    stats = limiter.stats()
    assert stats["prov"]["tokens_available"] == pytest.approx(50.0)


@pytest.mark.anyio
async def test_report_limited_cooldown_blocks_acquires():
    limiter, clk = _limiter({"prov": {"rpm": 60.0, "cooldown_s": 30.0}})
    applied = limiter.report_limited("prov")
    assert applied == pytest.approx(30.0)
    assert limiter.cooldown_remaining("prov") == pytest.approx(30.0)
    waited = await limiter.acquire("prov")
    assert waited == pytest.approx(30.0)
    assert clk.t == pytest.approx(30.0)


@pytest.mark.anyio
async def test_report_limited_prefers_retry_after_hint():
    limiter, clk = _limiter({"prov": {"rpm": 60.0, "cooldown_s": 30.0}})
    applied = limiter.report_limited("prov", retry_after_s=45.0)
    assert applied == pytest.approx(45.0)
    waited = await limiter.acquire("prov")
    assert waited == pytest.approx(45.0)
    assert clk.t == pytest.approx(45.0)


def test_report_limited_monotonic_window():
    limiter, _ = _limiter({"prov": {"rpm": 60.0, "cooldown_s": 30.0}})
    limiter.report_limited("prov", retry_after_s=60.0)
    limiter.report_limited("prov", retry_after_s=10.0)
    assert limiter.cooldown_remaining("prov") == pytest.approx(60.0)


def test_report_limited_unknown_provider_is_noop():
    limiter, _ = _limiter()
    assert limiter.report_limited("unknown") == 0.0
    assert limiter.report_limited("") == 0.0


def test_wait_estimate_reflects_cooldown_and_deficit():
    limiter, _ = _limiter({"prov": {"rpm": 60.0, "cooldown_s": 30.0}})
    assert limiter.wait_estimate("prov") == 0.0
    limiter.report_limited("prov")
    assert limiter.wait_estimate("prov") == pytest.approx(30.0)
    limiter._cooldown_until.clear()
    bucket = limiter._req_bucket("prov")
    assert bucket is not None
    bucket.charge(65.0)  # 60 -> -5 debt; one more request waits 6s at 1/s
    assert limiter.wait_estimate("prov") == pytest.approx(6.0)


@pytest.mark.anyio
async def test_record_tokens_drains_tpm_bucket():
    limiter, _ = _limiter({"prov": {"tpm": 100.0}})
    limiter.record_tokens("prov", 70.0)
    stats = limiter.stats()
    assert stats["prov"]["tokens_available"] == pytest.approx(30.0)
    waited = await limiter.acquire("prov", est_tokens=60.0)
    # deficit 30 tokens at 100/minute -> 18s
    assert waited == pytest.approx(18.0)


def test_record_tokens_ignores_invalid_amounts():
    limiter, _ = _limiter({"prov": {"tpm": 100.0}})
    limiter.record_tokens("prov", 0.0)
    limiter.record_tokens("prov", -5)
    limiter.record_tokens("prov", "junk")
    # Bucket never created -- nothing was ever charged.
    assert limiter.stats()["prov"]["tokens_available"] is None


def test_stats_snapshot_shape():
    limiter, _ = _limiter({"prov": {"rpm": 60.0, "tpm": 6000.0, "cooldown_s": 30.0}})
    limiter._req_bucket("prov")
    limiter._tok_bucket("prov")
    stats = limiter.stats()
    assert stats["prov"]["rpm"] == 60.0
    assert stats["prov"]["requests_available"] == pytest.approx(60.0)
    assert stats["prov"]["tokens_available"] == pytest.approx(6000.0)
    assert stats["prov"]["cooldown_s"] == 0.0


# ---------------------------------------------------------------------------
# 4. Shared singleton
# ---------------------------------------------------------------------------


def test_get_rate_limiter_singleton_and_reset():
    reset_rate_limiter()
    try:
        first = get_rate_limiter()
        second = get_rate_limiter()
        assert first is second
        # Built from the repo's committed rate_limits section.
        assert first.is_throttled("openrouter-free")
    finally:
        reset_rate_limiter()
    assert get_rate_limiter() is not first


# ---------------------------------------------------------------------------
# 5. FastAPI task queue integration
# ---------------------------------------------------------------------------


def _wait_terminal(client: TestClient, task_id: str, timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    status_value = ""
    while time.time() < deadline:
        status_value = client.get(f"/api/tasks/{task_id}").json().get("status") or ""
        if status_value not in ("QUEUED", "RUNNING"):
            return status_value
        time.sleep(0.05)
    return status_value


def _mock_limiter() -> Mock:
    lim = Mock()
    lim.wait_estimate = Mock(return_value=0.0)
    lim.acquire = AsyncMock(return_value=0.0)
    lim.report_limited = Mock(return_value=0.0)
    return lim


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    with TestClient(app) as test_client:
        yield test_client


def test_worker_acquires_provider_budget_before_run(client):
    """The worker gates queue admission on the resolved provider's bucket."""
    lim = _mock_limiter()
    calls: List[str] = []
    lim.acquire = AsyncMock(side_effect=lambda p: calls.append(p) or 0.0)

    with (
        patch("botex.server_cloud.get_rate_limiter", return_value=lim),
        patch(
            "botex.server_cloud.run_botex_task",
            new=AsyncMock(return_value={"ok": True, "status": "DONE"}),
        ),
    ):
        resp = client.post(
            "/api/tasks",
            json={"task": "t", "mode": "readonly", "provider": "openrouter-free"},
        )
        assert resp.status_code == 202
        assert _wait_terminal(client, resp.json()["task_id"]) == "DONE"
    assert calls == ["openrouter-free"]


def test_worker_emits_wait_event_and_logs_delay(client):
    """A non-zero rate-limit wait surfaces as a retry_wait task event."""
    lim = _mock_limiter()
    lim.wait_estimate = Mock(return_value=2.5)
    lim.acquire = AsyncMock(return_value=2.5)

    with (
        patch("botex.server_cloud.get_rate_limiter", return_value=lim),
        patch(
            "botex.server_cloud.run_botex_task",
            new=AsyncMock(return_value={"ok": True, "status": "DONE"}),
        ),
    ):
        resp = client.post(
            "/api/tasks",
            json={"task": "t", "mode": "readonly", "provider": "openrouter-free"},
        )
        task_id = resp.json()["task_id"]
        assert _wait_terminal(client, task_id) == "DONE"
        events = client.get(f"/api/tasks/{task_id}/events").json()["events"]
    lim.acquire.assert_awaited_once_with("openrouter-free")
    wait_events = [e for e in events if e.get("kind") == "retry_wait"]
    assert wait_events, "expected a retry_wait event while parked on the provider bucket"
    assert wait_events[0]["data"]["reason"] == "provider_rate_limit"
    assert wait_events[0]["data"]["provider"] == "openrouter-free"
    assert wait_events[0]["data"]["delay_s"] == pytest.approx(2.5)


def test_worker_rate_limited_result_opens_cooldown(client):
    """A provider_rate_limited task outcome feeds back into the limiter."""
    lim = _mock_limiter()
    failed = {
        "ok": False,
        "status": "API_ERROR",
        "failure_kind": "provider_rate_limited",
        "attempts": [{"model": "m", "failure_kind": "provider_rate_limited"}],
    }
    with (
        patch("botex.server_cloud.get_rate_limiter", return_value=lim),
        patch("botex.server_cloud.run_botex_task", new=AsyncMock(return_value=failed)),
    ):
        resp = client.post(
            "/api/tasks",
            json={"task": "t", "mode": "readonly", "provider": "openrouter-free"},
        )
        assert _wait_terminal(client, resp.json()["task_id"]) == "API_ERROR"
    lim.report_limited.assert_called_once_with("openrouter-free", retry_after_s=None)


def test_worker_devin_executor_bypasses_limiter(client):
    """The devin executor never touches the provider rate limiter."""
    lim = _mock_limiter()
    with (
        patch("botex.server_cloud.get_rate_limiter", return_value=lim),
        patch(
            "botex.server_cloud._run_devin_task",
            new=AsyncMock(return_value={"ok": True, "status": "DONE", "summary": "s"}),
        ),
    ):
        resp = client.post(
            "/api/tasks",
            json={"task": "t", "mode": "readonly", "executor": "devin"},
        )
        assert _wait_terminal(client, resp.json()["task_id"]) == "DONE"
    lim.acquire.assert_not_called()
    lim.wait_estimate.assert_not_called()


def test_worker_successful_result_no_cooldown(client):
    """A clean run must not open a cooldown window."""
    lim = _mock_limiter()
    with (
        patch("botex.server_cloud.get_rate_limiter", return_value=lim),
        patch(
            "botex.server_cloud.run_botex_task",
            new=AsyncMock(return_value={"ok": True, "status": "DONE"}),
        ),
    ):
        resp = client.post("/api/tasks", json={"task": "t", "mode": "readonly"})
        assert _wait_terminal(client, resp.json()["task_id"]) == "DONE"
    lim.report_limited.assert_not_called()


# ---------------------------------------------------------------------------
# 6. _note_provider_result feedback helper
# ---------------------------------------------------------------------------


def test_note_provider_result_detects_attempt_level_rate_limit():
    lim = _mock_limiter()
    with patch("botex.server_cloud.get_rate_limiter", return_value=lim):
        _note_provider_result(
            "prov", {"failure_kind": None, "attempts": [{"failure_kind": "provider_rate_limited"}]}
        )
    lim.report_limited.assert_called_once_with("prov", retry_after_s=None)


def test_note_provider_result_ignores_other_failures():
    lim = _mock_limiter()
    with patch("botex.server_cloud.get_rate_limiter", return_value=lim):
        _note_provider_result("prov", {"failure_kind": "provider_error"})
        _note_provider_result("prov", {"failure_kind": None, "attempts": []})
        _note_provider_result("prov", "not-a-dict")
        _note_provider_result("", {"failure_kind": "provider_rate_limited"})
    lim.report_limited.assert_not_called()


def test_note_provider_result_forwards_retry_hint():
    """The provider's own 'retry in Xs' text reaches report_limited."""
    lim = _mock_limiter()
    res = {
        "failure_kind": "provider_rate_limited",
        "message": "Model call error m: 429 quota exceeded. Please retry in 59.9s",
        "attempts": [{"failure_kind": "provider_rate_limited"}],
    }
    with patch("botex.server_cloud.get_rate_limiter", return_value=lim):
        _note_provider_result("google", res)
    lim.report_limited.assert_called_once_with("google", retry_after_s=pytest.approx(59.9))


def test_note_provider_result_hint_scans_attempt_errors():
    """Attempt-level message/error text is scanned for a hint as well."""
    lim = _mock_limiter()
    res = {
        "failure_kind": None,
        "attempts": [
            {"failure_kind": "provider_rate_limited", "error": "rate limited, retry in 12s"}
        ],
    }
    with patch("botex.server_cloud.get_rate_limiter", return_value=lim):
        _note_provider_result("prov", res)
    lim.report_limited.assert_called_once_with("prov", retry_after_s=pytest.approx(12.0))


def test_note_provider_result_hint_opens_stats_cooldown(client):
    """The hint-driven cooldown surfaces through /api/rate_limits stats."""
    limiter, _ = _limiter({"google": {"rpm": 60.0, "cooldown_s": 30.0}})
    res = {
        "failure_kind": "provider_rate_limited",
        "message": "429 quota exceeded. Please retry in 59.9s",
        "attempts": [{"failure_kind": "provider_rate_limited"}],
    }
    with patch("botex.server_cloud.get_rate_limiter", return_value=limiter):
        _note_provider_result("google", res)
        data = client.get("/api/rate_limits").json()
    assert data["google"]["cooldown_s"] == pytest.approx(59.9)


def test_note_provider_result_hint_free_falls_back_to_config():
    """A hint-free 429 opens the configured cooldown_s fallback."""
    limiter, _ = _limiter({"prov": {"rpm": 60.0, "cooldown_s": 30.0}})
    res = {"failure_kind": "provider_rate_limited", "message": "429 rate limit exceeded"}
    with patch("botex.server_cloud.get_rate_limiter", return_value=limiter):
        _note_provider_result("prov", res)
    assert limiter.cooldown_remaining("prov") == pytest.approx(30.0)


def test_note_provider_result_hint_clamped_at_ceiling():
    """A malformed 'retry in 3600s' body cannot park the provider for an hour."""
    limiter, _ = _limiter({"prov": {"rpm": 60.0, "cooldown_s": 30.0}})
    res = {"failure_kind": "provider_rate_limited", "message": "429 — retry in 3600s"}
    with patch("botex.server_cloud.get_rate_limiter", return_value=limiter):
        _note_provider_result("prov", res)
    assert limiter.cooldown_remaining("prov") == pytest.approx(300.0)
