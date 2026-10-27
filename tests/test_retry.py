# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Offline tests for the shared retry layer (botex/retry.py).

Covers: retry-config resolution, server-delay parsing (headers + body),
daily-quota bypass, provider is_retryable rules (incl. the Google 503
overload widening and the APITimeoutError-is-a-APIConnectionError trap),
call_with_backoff wait accounting, SDK max_retries=0 wiring, and the
provider_rate_limited classification feeding the fallback chain.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from botex.contracts import FALLBACKABLE_KINDS, classify_api_error, should_fallback
from botex.providers import (
    GenericOpenAIProvider,
    GoogleAIStudioProvider,
    OpenRouterProvider,
)
from botex.retry import (
    call_with_backoff,
    is_daily_quota,
    parse_retry_delay,
    resolve_retry_config,
)


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://example.invalid/v1/chat/completions")


def _resp(status: int, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, request=_req())


def _rate_limit(body: Any = None, headers: Optional[Dict[str, str]] = None) -> RateLimitError:
    return RateLimitError("rate limited", response=_resp(429, headers), body=body)


def _status_err(code: int, message: str = "") -> APIStatusError:
    return APIStatusError(message or f"HTTP {code}", response=_resp(code), body=None)


def _conn_err() -> APIConnectionError:
    return APIConnectionError(message="connection reset", request=_req())


class _FakeProvider:
    """Minimal duck-typed provider for call_with_backoff unit tests."""

    def __init__(self, retryable: bool = True, delay: Optional[float] = None) -> None:
        self._retryable = retryable
        self._delay = delay

    def is_retryable(self, exc: BaseException) -> bool:
        return self._retryable

    def retry_after_s(self, exc: BaseException) -> Optional[float]:
        return self._delay


class _FakeSleep:
    def __init__(self) -> None:
        self.sleeps: List[float] = []

    async def __call__(self, delay: float) -> None:
        self.sleeps.append(delay)


# --- retry config resolution -------------------------------------------------


def test_retry_config_defaults_enabled():
    cfg = resolve_retry_config(None)
    assert cfg["enabled"] is True
    assert cfg["max_waits"] == 2
    assert cfg["max_wait_s"] == 60.0


def test_retry_config_bare_bool_disables():
    """retry: false (bare bool, not a dict) must disable the layer —
    otherwise get_client and call_with_backoff disagree and BOTH the SDK
    retries and the custom layer end up active (retry multiplication)."""
    assert resolve_retry_config(False)["enabled"] is False
    assert resolve_retry_config(True)["enabled"] is True
    assert resolve_retry_config("off")["enabled"] is True  # non-empty string truthy


def test_retry_config_merge_and_clamp():
    cfg = resolve_retry_config({"enabled": False, "max_waits": 5, "max_wait_s": -3})
    assert cfg["enabled"] is False
    assert cfg["max_waits"] == 5
    assert cfg["max_wait_s"] == 0.0
    # Unknown keys ignored; missing keys keep defaults
    cfg2 = resolve_retry_config({"max_waits": 0, "bogus": 9})
    assert cfg2["max_waits"] == 0
    assert cfg2["enabled"] is True
    assert "bogus" not in cfg2


# --- server-provided delay parsing --------------------------------------------


def test_parse_retry_after_header_seconds():
    exc = _rate_limit(headers={"Retry-After": "12"})
    assert parse_retry_delay(exc) == 12.0


def test_parse_retry_after_header_http_date():
    future = httpx.Headers({"Retry-After": "Wed, 01 Jan 2098 00:00:00 GMT"})
    exc = _rate_limit(headers=dict(future))
    delay = parse_retry_delay(exc)
    assert delay is not None and delay > 1000


def test_parse_body_retry_delay_google_shape():
    exc = _rate_limit(
        body={
            "error": {
                "code": 429,
                "details": [{"@type": "google.rpc.RetryInfo", "retryDelay": "20s"}],
            }
        }
    )
    assert parse_retry_delay(exc) == 20.0


def test_parse_body_quota_reset_delay():
    exc = _rate_limit(body={"error": {"quotaResetDelay": "35.5s"}})
    assert parse_retry_delay(exc) == 35.5


def test_parse_retry_in_text():
    exc = RateLimitError("Quota exceeded. Please retry in 7.25s.", response=_resp(429), body=None)
    assert parse_retry_delay(exc) == 7.25


def test_parse_no_hint_returns_none():
    exc = _rate_limit(body={"error": {"message": "slow down"}})
    assert parse_retry_delay(exc) is None


def test_header_beats_body():
    exc = _rate_limit(headers={"Retry-After": "5"}, body={"retryDelay": "99s"})
    assert parse_retry_delay(exc) == 5.0


# --- daily quota detection ----------------------------------------------------


def test_is_daily_quota_quota_id():
    exc = _rate_limit(
        body={
            "error": {
                "details": [
                    {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]}
                ]
            }
        }
    )
    assert is_daily_quota(exc) is True


def test_is_daily_quota_message_text():
    exc = _rate_limit(body={"error": {"message": "You exceeded your current quota per day"}})
    assert is_daily_quota(exc) is True


def test_is_daily_quota_false_for_plain_429():
    assert is_daily_quota(_rate_limit(body={"error": {"message": "rpm limit"}})) is False


def test_is_daily_quota_requires_quota_context():
    """\"daily\" as a bare adjective must not trip immediate fallback —
    only daily tokens appearing alongside quota/limit language count."""
    exc = APIStatusError(
        "upstream error",
        response=_resp(503),
        body={"error": {"message": "daily sync job failed, retry later"}},
    )
    assert is_daily_quota(exc) is False
    exc2 = _rate_limit(body={"error": {"message": "daily quota limit reached"}})
    assert is_daily_quota(exc2) is True


# --- provider is_retryable hooks ----------------------------------------------


def test_base_is_retryable_matrix():
    provider = OpenRouterProvider("openrouter")
    assert provider.is_retryable(_rate_limit()) is True
    assert provider.is_retryable(_conn_err()) is True
    # APITimeoutError subclasses APIConnectionError — must stay non-retryable
    assert provider.is_retryable(APITimeoutError(request=_req())) is False
    # Arbitrary 5xx never retried
    assert provider.is_retryable(_status_err(500)) is False
    assert provider.is_retryable(_status_err(503)) is False
    # Duck-typed 429
    e = Exception("429")
    e.status_code = 429
    assert provider.is_retryable(e) is True


def test_google_is_retryable_503_overload():
    provider = GoogleAIStudioProvider("google")
    overload = APIStatusError(
        "The model is overloaded. Please try again later.",
        response=_resp(503),
        body=None,
    )
    assert provider.is_retryable(overload) is True
    # A plain 503 without the overload marker stays non-retryable
    assert provider.is_retryable(_status_err(503, "internal error")) is False
    assert provider.is_retryable(_status_err(500)) is False
    assert provider.is_retryable(_rate_limit()) is True


def test_generic_provider_does_not_retry_503():
    provider = GenericOpenAIProvider("generic")
    overload = APIStatusError("model is overloaded", response=_resp(503), body=None)
    assert provider.is_retryable(overload) is False


def test_retry_after_s_delegates_to_parser():
    provider = OpenRouterProvider("openrouter")
    exc = _rate_limit(headers={"Retry-After": "9"})
    assert provider.retry_after_s(exc) == 9.0


# --- call_with_backoff ---------------------------------------------------------


def test_backoff_success_first_try():
    async def _run() -> Any:
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        sleeps = _FakeSleep()
        result = await call_with_backoff(_FakeProvider(), factory, {"retry": {}}, sleep=sleeps)
        return result, calls, sleeps.sleeps

    result, calls, sleeps = asyncio.run(_run())
    assert result == "ok" and calls == 1 and sleeps == []


def test_backoff_429_then_success_uses_server_delay():
    async def _run() -> Any:
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _rate_limit()
            return "ok"

        sleeps = _FakeSleep()
        result = await call_with_backoff(
            _FakeProvider(delay=4.0), factory, {"retry": {}}, sleep=sleeps
        )
        return result, calls, sleeps.sleeps

    result, calls, sleeps = asyncio.run(_run())
    assert result == "ok" and calls == 2 and sleeps == [4.0]


def test_backoff_exhausts_max_waits():
    async def _run() -> Any:
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            raise _rate_limit()

        sleeps = _FakeSleep()
        try:
            await call_with_backoff(
                _FakeProvider(delay=1.0),
                factory,
                {"retry": {"max_waits": 2}},
                sleep=sleeps,
            )
            return calls, sleeps.sleeps, None
        except RateLimitError as exc:
            return calls, sleeps.sleeps, exc

    calls, sleeps, exc = asyncio.run(_run())
    assert calls == 3  # 1 initial + 2 waits
    assert sleeps == [1.0, 1.0]
    assert isinstance(exc, RateLimitError)


def test_backoff_daily_quota_skips_wait():
    async def _run() -> Any:
        async def factory() -> str:
            raise _rate_limit(body={"error": {"message": "Quota exceeded per day for this model"}})

        sleeps = _FakeSleep()
        try:
            await call_with_backoff(_FakeProvider(delay=0.01), factory, {}, sleep=sleeps)
        except RateLimitError:
            return sleeps.sleeps
        return None

    sleeps = asyncio.run(_run())
    assert sleeps == []


def test_backoff_non_retryable_passthrough():
    async def _run() -> Any:
        async def factory() -> str:
            raise _status_err(500)

        sleeps = _FakeSleep()
        try:
            await call_with_backoff(_FakeProvider(retryable=False), factory, {}, sleep=sleeps)
        except APIStatusError:
            return sleeps.sleeps
        return None

    sleeps = asyncio.run(_run())
    assert sleeps == []


def test_backoff_deadline_blocks_wait():
    async def _run() -> Any:
        async def factory() -> str:
            raise _rate_limit()

        sleeps = _FakeSleep()
        try:
            await call_with_backoff(
                _FakeProvider(delay=500.0),
                factory,
                {"retry": {"max_wait_s": 600.0}},
                deadline_ts=0.0,  # already in the past
                sleep=sleeps,
            )
        except RateLimitError:
            return sleeps.sleeps
        return None

    sleeps = asyncio.run(_run())
    assert sleeps == []


def test_backoff_disabled_single_attempt():
    async def _run() -> Any:
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            raise _rate_limit()

        sleeps = _FakeSleep()
        try:
            await call_with_backoff(
                _FakeProvider(delay=0.01),
                factory,
                {"retry": {"enabled": False}},
                sleep=sleeps,
            )
        except RateLimitError:
            return calls, sleeps.sleeps
        return None

    result = asyncio.run(_run())
    assert result == (1, [])


def test_backoff_stream_unit_retries_mid_stream():
    """The factory wraps create()+chunk collection: a failure raised while
    consuming chunks must still retry the whole unit."""

    async def _run() -> Any:
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                # Simulate a stream that dies partway through collection
                raise _rate_limit()
            return "collected"

        sleeps = _FakeSleep()
        result = await call_with_backoff(
            _FakeProvider(delay=0.5), factory, {"retry": {}}, sleep=sleeps
        )
        return result, calls

    result, calls = asyncio.run(_run())
    assert result == "collected" and calls == 2


def test_backoff_on_wait_callback_invoked():
    async def _run() -> Any:
        seen: List[Any] = []

        async def factory() -> str:
            raise _rate_limit()

        async def _s(_d: float) -> None:
            return None

        try:
            await call_with_backoff(
                _FakeProvider(delay=2.0),
                factory,
                {"retry": {"max_waits": 1}},
                sleep=_s,
                on_wait=lambda d, e: seen.append(d),
            )
        except RateLimitError:
            pass
        return seen

    assert asyncio.run(_run()) == [2.0]


# --- SDK max_retries wiring ----------------------------------------------------


class _KwargClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_get_client_disables_sdk_retries_when_custom_layer_on():
    provider = OpenRouterProvider("openrouter")
    client = provider.get_client(
        api_key="k",
        client_factory=lambda **kw: _KwargClient(**kw),
        provider_cfg={"retry": {"enabled": True}},
    )
    assert client.kwargs["max_retries"] == 0


def test_get_client_keeps_sdk_retries_when_custom_layer_off():
    provider = OpenRouterProvider("openrouter")
    client = provider.get_client(
        api_key="k",
        client_factory=lambda **kw: _KwargClient(**kw),
        provider_cfg={"retry": {"enabled": False}},
    )
    assert "max_retries" not in client.kwargs


def test_get_client_bare_bool_retry_off_keeps_sdk_retries():
    provider = OpenRouterProvider("openrouter")
    client = provider.get_client(
        api_key="k",
        client_factory=lambda **kw: _KwargClient(**kw),
        provider_cfg={"retry": False},
    )
    assert "max_retries" not in client.kwargs


def test_get_client_default_retry_on_disables_sdk_retries():
    provider = GoogleAIStudioProvider("google")
    client = provider.get_client(
        api_key="k",
        client_factory=lambda **kw: _KwargClient(**kw),
        provider_cfg={},
    )
    assert client.kwargs["max_retries"] == 0


# --- classification / fallback contract ----------------------------------------


def test_429_classifies_as_provider_rate_limited():
    exc = _rate_limit()
    kind = classify_api_error(exc)
    assert kind == "provider_rate_limited"
    assert "provider_rate_limited" in FALLBACKABLE_KINDS
    assert should_fallback({"status": "API_ERROR", "failure_kind": kind}) is True


def test_duck_typed_429_classifies_too():
    e = Exception("rate limit")
    e.status_code = 429
    assert classify_api_error(e) == "provider_rate_limited"


def test_5xx_still_provider_unavailable():
    assert classify_api_error(_status_err(500)) == "provider_unavailable"
    assert classify_api_error(_status_err(503)) == "provider_unavailable"
