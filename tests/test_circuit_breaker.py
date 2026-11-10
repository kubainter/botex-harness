# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for ProviderCircuitBreaker in botex/retry.py."""

import time

import pytest
from botex.contracts import classify_api_error
from botex.retry import (
    CircuitBreakerOpenError,
    ProviderCircuitBreaker,
    call_with_backoff,
    get_circuit_breaker,
    reset_circuit_breaker,
    resolve_circuit_breaker_config,
)


def test_circuit_breaker_transitions():
    cb = ProviderCircuitBreaker(failure_threshold=3, recovery_timeout_s=10.0)
    provider = "test_provider"

    # Initial state is CLOSED
    assert cb.get_state(provider) == ProviderCircuitBreaker.CLOSED
    assert cb.can_execute(provider) is True

    # 1st failure -> still CLOSED
    cb.record_failure(provider)
    assert cb.get_state(provider) == ProviderCircuitBreaker.CLOSED

    # 2nd failure -> still CLOSED
    cb.record_failure(provider)
    assert cb.get_state(provider) == ProviderCircuitBreaker.CLOSED

    # 3rd failure -> trips to OPEN
    cb.record_failure(provider)
    assert cb.get_state(provider) == ProviderCircuitBreaker.OPEN

    # When OPEN, can_execute raises CircuitBreakerOpenError
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        cb.can_execute(provider)
    assert "test_provider" in str(exc_info.value)
    assert classify_api_error(exc_info.value) == "provider_unavailable"

    # Simulate time passing beyond recovery_timeout_s
    simulated_now = cb._tripped_at[provider] + 11.0
    assert cb.get_state(provider, now=simulated_now) == ProviderCircuitBreaker.HALF_OPEN

    # In HALF_OPEN, a failure immediately re-trips to OPEN
    cb.record_failure(provider)
    assert cb.get_state(provider) == ProviderCircuitBreaker.OPEN

    # Simulate recovery again
    simulated_now = cb._tripped_at[provider] + 11.0
    assert cb.get_state(provider, now=simulated_now) == ProviderCircuitBreaker.HALF_OPEN

    # In HALF_OPEN, success restores to CLOSED
    cb.record_success(provider)
    assert cb.get_state(provider) == ProviderCircuitBreaker.CLOSED
    assert cb.can_execute(provider) is True


def test_circuit_breaker_reset():
    cb = ProviderCircuitBreaker(failure_threshold=1, recovery_timeout_s=60.0)
    cb.record_failure("prov_a")
    assert cb.get_state("prov_a") == ProviderCircuitBreaker.OPEN

    cb.reset("prov_a")
    assert cb.get_state("prov_a") == ProviderCircuitBreaker.CLOSED


def test_get_circuit_breaker_singleton():
    cb1 = get_circuit_breaker()
    cb2 = get_circuit_breaker()
    assert cb1 is cb2


def test_reset_circuit_breaker_drops_state():
    cb = get_circuit_breaker(failure_threshold=1)
    cb.record_failure("leaky_provider")
    assert cb.get_state("leaky_provider") == ProviderCircuitBreaker.OPEN

    reset_circuit_breaker()
    fresh = get_circuit_breaker()
    assert fresh is not cb
    assert fresh.get_state("leaky_provider") == ProviderCircuitBreaker.CLOSED


def test_half_open_single_probe_gating():
    cb = ProviderCircuitBreaker(failure_threshold=1, recovery_timeout_s=10.0)
    cb.record_failure("prov")
    assert cb.get_state("prov") == ProviderCircuitBreaker.OPEN

    # Age the trip past recovery_timeout_s.
    cb._tripped_at["prov"] = time.time() - 11.0
    assert cb.get_state("prov") == ProviderCircuitBreaker.HALF_OPEN

    # First caller claims the single probe slot; concurrent callers fail fast.
    assert cb.can_execute("prov") is True
    with pytest.raises(CircuitBreakerOpenError):
        cb.can_execute("prov")

    # A probe that never reports an outcome still frees the slot.
    cb.release_probe("prov")
    assert cb.can_execute("prov") is True
    # Probe failure re-trips OPEN and frees the slot for the next window.
    cb.record_failure("prov")
    assert cb.get_state("prov") == ProviderCircuitBreaker.OPEN


def test_resolve_circuit_breaker_config():
    assert resolve_circuit_breaker_config(None) == {
        "enabled": True,
        "failure_threshold": 3,
        "recovery_timeout_s": 60.0,
    }
    assert resolve_circuit_breaker_config(False)["enabled"] is False
    cfg = resolve_circuit_breaker_config({"failure_threshold": 0, "recovery_timeout_s": -5.0})
    assert cfg["failure_threshold"] == 1
    assert cfg["recovery_timeout_s"] == 1.0
    cfg = resolve_circuit_breaker_config({"failure_threshold": "bad", "recovery_timeout_s": None})
    assert cfg["failure_threshold"] == 3
    assert cfg["recovery_timeout_s"] == 60.0


class _HTTPError(Exception):
    pass


class _FakeProvider:
    name = "testprov"

    def __init__(self, retryable=False):
        self._retryable = retryable

    def is_retryable(self, exc):
        return self._retryable

    def retry_after_s(self, exc):
        return 0.001


def _err(status):
    exc = _HTTPError(f"http {status}")
    exc.status_code = status
    return exc


@pytest.mark.anyio
async def test_call_with_backoff_open_circuit_skips_factory():
    cb = ProviderCircuitBreaker(failure_threshold=1, recovery_timeout_s=60.0)
    cb.record_failure("testprov")
    called = []

    async def factory():
        called.append(1)
        return "unreachable"

    with pytest.raises(CircuitBreakerOpenError):
        await call_with_backoff(
            _FakeProvider(),
            factory,
            None,
            circuit_breaker=cb,
            provider_name="testprov",
        )
    assert called == []


@pytest.mark.anyio
async def test_call_with_backoff_records_one_failure_per_invocation():
    cb = ProviderCircuitBreaker(failure_threshold=2, recovery_timeout_s=60.0)
    calls = []

    async def factory():
        calls.append(1)
        raise _err(429)

    with pytest.raises(_HTTPError):
        await call_with_backoff(
            _FakeProvider(retryable=True),
            factory,
            {"retry": {"max_waits": 2, "base_delay_s": 0.001, "jitter_s": 0.0}},
            circuit_breaker=cb,
            provider_name="testprov",
        )
    # Three internal attempts, but only one logical request failed.
    assert len(calls) == 3
    assert cb._consecutive_failures["testprov"] == 1
    assert cb.get_state("testprov") == ProviderCircuitBreaker.CLOSED

    # A second logical failure reaches the threshold and trips the breaker.
    with pytest.raises(_HTTPError):
        await call_with_backoff(
            _FakeProvider(retryable=True),
            factory,
            {"retry": {"max_waits": 0}},
            circuit_breaker=cb,
            provider_name="testprov",
        )
    assert cb.get_state("testprov") == ProviderCircuitBreaker.OPEN


@pytest.mark.anyio
async def test_call_with_backoff_auth_and_timeout_not_counted():
    cb = ProviderCircuitBreaker(failure_threshold=1, recovery_timeout_s=60.0)

    async def auth_fail():
        raise _err(401)

    with pytest.raises(_HTTPError):
        await call_with_backoff(
            _FakeProvider(retryable=False),
            auth_fail,
            None,
            circuit_breaker=cb,
            provider_name="testprov",
        )
    assert cb._consecutive_failures.get("testprov", 0) == 0
    assert cb.get_state("testprov") == ProviderCircuitBreaker.CLOSED

    import httpx2 as httpx
    from openai import APITimeoutError

    async def timeout_fail():
        raise APITimeoutError(request=httpx.Request("POST", "http://x"))

    with pytest.raises(APITimeoutError):
        await call_with_backoff(
            _FakeProvider(retryable=False),
            timeout_fail,
            None,
            circuit_breaker=cb,
            provider_name="testprov",
        )
    # Local request timeouts say nothing about provider health.
    assert cb._consecutive_failures.get("testprov", 0) == 0
    assert cb.get_state("testprov") == ProviderCircuitBreaker.CLOSED


@pytest.mark.anyio
async def test_call_with_backoff_server_side_failures_counted():
    from openai import APIConnectionError
    import httpx2 as httpx

    cb = ProviderCircuitBreaker(failure_threshold=3, recovery_timeout_s=60.0)

    async def conn_fail():
        raise APIConnectionError(request=httpx.Request("POST", "http://x"))

    for expected in (1, 2):
        with pytest.raises(APIConnectionError):
            await call_with_backoff(
                _FakeProvider(retryable=False),
                conn_fail,
                None,
                circuit_breaker=cb,
                provider_name="testprov",
            )
        assert cb._consecutive_failures["testprov"] == expected

    # Non-retryable 5xx counts as well.
    async def server_fail():
        raise _err(503)

    with pytest.raises(_HTTPError):
        await call_with_backoff(
            _FakeProvider(retryable=False),
            server_fail,
            None,
            circuit_breaker=cb,
            provider_name="testprov",
        )
    assert cb.get_state("testprov") == ProviderCircuitBreaker.OPEN


@pytest.mark.anyio
async def test_call_with_backoff_success_resets_failures():
    cb = ProviderCircuitBreaker(failure_threshold=3, recovery_timeout_s=60.0)
    cb.record_failure("testprov")
    cb.record_failure("testprov")

    async def factory():
        return "ok"

    result = await call_with_backoff(
        _FakeProvider(),
        factory,
        None,
        circuit_breaker=cb,
        provider_name="testprov",
    )
    assert result == "ok"
    assert cb._consecutive_failures["testprov"] == 0
    assert cb.get_state("testprov") == ProviderCircuitBreaker.CLOSED


@pytest.mark.anyio
async def test_call_with_backoff_breaker_disabled():
    cb = ProviderCircuitBreaker(failure_threshold=1, recovery_timeout_s=60.0)
    cb.record_failure("testprov")

    async def factory():
        return "ok"

    # circuit_breaker_cfg=False disables the gate entirely for this call.
    result = await call_with_backoff(
        _FakeProvider(),
        factory,
        None,
        circuit_breaker=cb,
        circuit_breaker_cfg=False,
        provider_name="testprov",
    )
    assert result == "ok"
