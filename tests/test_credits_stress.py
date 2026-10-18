# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Empirical Stress Test Suite: Provider Credits Integration
=========================================================
Adversarial challenge & boundary stress tests for Milestone M3:
- Extreme & abnormal numeric values (negative, NaN, infinity, large floats, strings, nulls)
- Partial endpoint failure matrix (/auth/key vs /credits cross-product)
- Total endpoint failure scenarios
- Static HTTP error mapping verification (CodeQL CWE-312 compliance)
- Network timeout, DNS failure, and socket error resilience
- Secret masking & taint-safety under hostile inputs
- Concurrency, cache integrity, and hermetic offline isolation
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import socket
import urllib.error
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from botex.providers import (
    BaseProvider,
    CreditsInfo,
    OpenRouterProvider,
    ProviderRegistry,
    _HTTP_ERROR_MAP,
    _safe_float,
    _sanitize_raw_payload,
)
from botex.security import mask_secrets
from botex.wallet import (
    WalletService,
    format_credits_summary,
    parse_openrouter_credits_data,
    parse_openrouter_key_data,
    parse_openrouter_payloads,
)


# ---------------------------------------------------------------------------
# Fixtures: Hermetic Socket Isolation & Clean Environment
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Ensure no live network calls can be made during stress tests."""
    orig_connect = socket.socket.connect

    def blocked_connect(self, *args: Any, **kwargs: Any) -> Any:
        import inspect

        frame = inspect.currentframe()
        try:
            caller = frame.f_back if frame else None
            # Allow asyncio internal socketpair creation on Windows ProactorEventLoop
            if caller and caller.f_code.co_name == "_fallback_socketpair":
                return orig_connect(self, *args, **kwargs)
        finally:
            del frame

        raise RuntimeError("Blocked live network socket call during stress test execution")

    with patch.object(socket.socket, "connect", blocked_connect):
        yield


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch):
    """Clear provider API keys from environment."""
    for key in ["OPENROUTER_API_KEY", "OPENROUTER_API_FREE_KEY", "NVIDIA_API_KEY"]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def clean_registry():
    """Restore ProviderRegistry._registry state."""
    orig_registry = dict(ProviderRegistry._registry)
    yield
    ProviderRegistry._registry = orig_registry


def make_mock_urlopen(
    key_payload: Optional[Dict[str, Any]] = None,
    credits_payload: Optional[Dict[str, Any]] = None,
    key_error: Optional[Exception] = None,
    credits_error: Optional[Exception] = None,
    key_raw_bytes: Optional[bytes] = None,
    credits_raw_bytes: Optional[bytes] = None,
):
    """Factory creating a mock urlopen handler for stress testing."""

    def mock_urlopen(req: Any, timeout: float = 10.0) -> Any:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "auth/key" in url:
            if key_error:
                raise key_error
            resp_mock = MagicMock()
            if key_raw_bytes is not None:
                content = key_raw_bytes
            else:
                content = json.dumps(key_payload if key_payload is not None else {}).encode("utf-8")
            resp_mock.read.return_value = content
            resp_mock.__enter__.return_value = resp_mock
            return resp_mock
        elif "credits" in url:
            if credits_error:
                raise credits_error
            resp_mock = MagicMock()
            if credits_raw_bytes is not None:
                content = credits_raw_bytes
            else:
                content = json.dumps(credits_payload if credits_payload is not None else {}).encode(
                    "utf-8"
                )
            resp_mock.read.return_value = content
            resp_mock.__enter__.return_value = resp_mock
            return resp_mock
        raise RuntimeError(f"Unexpected unmocked URL in stress test: {url}")

    return mock_urlopen


# ---------------------------------------------------------------------------
# Dimension 1: Extreme & Abnormal Credit Numbers
# ---------------------------------------------------------------------------


def test_stress_negative_values():
    """Verify negative balance, negative usage, and negative limits."""
    key_payload = {
        "data": {
            "label": "Negative Limit Key",
            "usage": -12.50,
            "limit": -50.00,
            "limit_remaining": -37.50,
            "is_free_tier": False,
        }
    }
    credits_payload = {
        "data": {
            "total_credits": -100.00,
            "total_usage": 25.00,
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen", side_effect=make_mock_urlopen(key_payload, credits_payload)
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is True
    # total_credits (-100.0) - total_usage (25.0) = -125.0
    assert info.balance_usd == -125.00
    assert info.usage_usd == 25.00
    assert info.limit_usd == -50.00
    assert info.limit_remaining_usd == -37.50

    # Ensure formatters handle negative numbers without crashing
    summary = format_credits_summary(info)
    assert "$-125.00 USD" in summary
    assert "$-50.00 USD" in summary
    compact = format_credits_summary(info, compact=True)
    assert "Balance: $-125.00 USD" in compact


def test_stress_infinity_and_nan_values():
    """Verify infinity and NaN float conversions in credit payloads."""
    key_payload = {
        "data": {
            "label": "Inf/NaN Key",
            "usage": float("inf"),
            "limit": "Infinity",
            "limit_remaining": "-Infinity",
            "is_free_tier": False,
        }
    }
    credits_payload = {
        "data": {
            "total_credits": float("inf"),
            "total_usage": 100.0,
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen", side_effect=make_mock_urlopen(key_payload, credits_payload)
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is True
    assert math.isinf(info.balance_usd)
    assert info.balance_usd > 0
    assert math.isinf(info.limit_usd)
    assert math.isinf(info.limit_remaining_usd)
    assert info.limit_remaining_usd < 0

    # Formatter must not raise ValueError
    summary = format_credits_summary(info)
    assert "inf" in summary.lower()
    compact = format_credits_summary(info, compact=True)
    assert "inf" in compact.lower()


def test_stress_nan_handling():
    """Verify NaN in balance or limits does not crash formatting or logic."""
    key_payload = {
        "data": {
            "label": "NaN Key",
            "usage": "nan",
            "limit": "NaN",
        }
    }
    credits_payload = {
        "data": {
            "total_credits": "NaN",
            "total_usage": 50.0,
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen", side_effect=make_mock_urlopen(key_payload, credits_payload)
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is True
    assert math.isnan(info.balance_usd)
    assert math.isnan(info.limit_usd)

    summary = format_credits_summary(info)
    assert "nan" in summary.lower()
    compact = format_credits_summary(info, compact=True)
    assert "nan" in compact.lower()


def test_stress_extreme_float_magnitudes():
    """Verify very large (1e308) and very small (1e-308) floats."""
    key_payload = {
        "data": {
            "label": "Extreme Magnitude Key",
            "usage": 1e-300,
            "limit": 1e308,
            "limit_remaining": 1e308,
        }
    }
    credits_payload = {
        "data": {
            "total_credits": 1e308,
            "total_usage": 1e200,
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen", side_effect=make_mock_urlopen(key_payload, credits_payload)
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is True
    assert info.limit_usd == 1e308
    assert info.balance_usd is not None

    summary = format_credits_summary(info)
    assert "Account Balance:" in summary
    assert "Key Limit:" in summary


def test_stress_string_numbers_and_scientific_notation():
    """Verify numeric strings including scientific notation and decimal strings."""
    key_payload = {
        "data": {
            "label": "String Floats Key",
            "usage": "2.5e2",  # 250.0
            "limit": "1.0e3",  # 1000.0
            "limit_remaining": "7.5e2",  # 750.0
        }
    }
    credits_payload = {
        "data": {
            "total_credits": "500.50",
            "total_usage": "100.25",
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen", side_effect=make_mock_urlopen(key_payload, credits_payload)
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is True
    assert info.balance_usd == 400.25
    assert info.limit_usd == 1000.0
    assert info.limit_remaining_usd == 750.0


def test_stress_null_and_empty_fields():
    """Verify null fields, empty dicts, and missing 'data' containers."""
    key_payload = {"data": None}
    credits_payload = {"data": {}}
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen", side_effect=make_mock_urlopen(key_payload, credits_payload)
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is True
    assert info.balance_usd is None
    assert info.usage_usd is None
    assert info.limit_usd is None
    assert info.limit_remaining_usd is None
    assert info.is_free_tier is False
    assert info.label is None

    summary = format_credits_summary(info)
    assert "Unavailable" in summary
    assert "Unlimited" in summary


def test_stress_non_numeric_and_unexpected_types():
    """Verify lists, dicts, booleans, and garbage strings in numeric positions."""
    key_payload = {
        "data": {
            "label": 12345,  # Non-string label
            "usage": ["unexpected", "list"],
            "limit": {"nested": "dict"},
            "limit_remaining": "not-a-number",
            "is_free_tier": "yes",  # truthy string
        }
    }
    credits_payload = {
        "data": {
            "total_credits": object(),
            "total_usage": None,
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen", side_effect=make_mock_urlopen(key_payload, credits_payload)
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is True
    assert info.label == "12345"
    assert info.limit_usd is None
    assert info.limit_remaining_usd is None
    assert info.balance_usd is None
    assert info.is_free_tier is True


# ---------------------------------------------------------------------------
# Dimension 2: Partial Endpoint Failure Scenarios (Cross-Product Matrix)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "http_code,expected_error_desc",
    [
        (400, "Bad Request"),
        (401, "Unauthorized (invalid or expired API key)"),
        (403, "Forbidden (insufficient permissions)"),
        (404, "Endpoint Not Found"),
        (408, "Request Timeout"),
        (429, "Too Many Requests (rate limit exceeded)"),
        (500, "Internal Server Error"),
        (502, "Bad Gateway"),
        (503, "Service Unavailable"),
        (504, "Gateway Timeout"),
        (418, "HTTP Error 418"),  # unmapped code
        (599, "HTTP Error 599"),  # unmapped code
    ],
)
def test_stress_credits_fails_key_succeeds_matrix(http_code: int, expected_error_desc: str):
    """When /credits fails with HTTP error, /auth/key data must be preserved with secondary error."""
    key_payload = {
        "data": {
            "label": "Resilient Key",
            "usage": 5.0,
            "limit": 25.0,
            "is_free_tier": False,
        }
    }
    exc = urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1/credits",
        code=http_code,
        msg="Error",
        hdrs=MagicMock(),
        fp=io.BytesIO(b"upstream failure"),
    )

    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload=key_payload, credits_error=exc),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is True
    assert info.limit_usd == 25.0
    assert info.limit_remaining_usd == 20.0
    assert info.balance_usd is None  # /credits failed
    assert info.error is not None
    assert f"credits HTTP {http_code}: {expected_error_desc}" in info.error

    # Formatter must display warning with error
    summary = format_credits_summary(info)
    assert f"credits HTTP {http_code}" in summary


@pytest.mark.parametrize(
    "http_code,expected_error_desc",
    [
        (400, "Bad Request"),
        (401, "Unauthorized (invalid or expired API key)"),
        (403, "Forbidden (insufficient permissions)"),
        (404, "Endpoint Not Found"),
        (429, "Too Many Requests (rate limit exceeded)"),
        (500, "Internal Server Error"),
        (503, "Service Unavailable"),
    ],
)
def test_stress_key_fails_credits_succeeds_matrix(http_code: int, expected_error_desc: str):
    """When /auth/key fails with HTTP error, /credits data must be preserved with secondary error."""
    credits_payload = {
        "data": {
            "total_credits": 150.0,
            "total_usage": 50.0,
        }
    }
    exc = urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1/auth/key",
        code=http_code,
        msg="Error",
        hdrs=MagicMock(),
        fp=io.BytesIO(b"upstream failure"),
    )

    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(credits_payload=credits_payload, key_error=exc),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is True
    assert info.balance_usd == 100.0
    assert info.usage_usd == 50.0
    assert info.limit_usd is None
    assert info.error is not None
    assert f"auth/key HTTP {http_code}: {expected_error_desc}" in info.error

    summary = format_credits_summary(info)
    assert "$100.00 USD" in summary
    assert f"auth/key HTTP {http_code}" in summary


def test_stress_partial_timeout_combinations():
    """Verify timeout on one endpoint while the other succeeds."""
    provider = OpenRouterProvider("openrouter")

    # Case A: /credits times out, /auth/key succeeds
    key_payload = {"data": {"label": "Key OK", "usage": 1.0, "limit": 10.0}}
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(
            key_payload=key_payload, credits_error=socket.timeout("timed out")
        ),
    ):
        info_a = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
    assert info_a.supported is True
    assert info_a.limit_usd == 10.0
    assert info_a.balance_usd is None
    assert "credits network error" in (info_a.error or "")

    # Case B: /auth/key times out, /credits succeeds
    credits_payload = {"data": {"total_credits": 75.0, "total_usage": 25.0}}
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(
            credits_payload=credits_payload, key_error=TimeoutError("timed out")
        ),
    ):
        info_b = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
    assert info_b.supported is True
    assert info_b.balance_usd == 50.0
    assert info_b.limit_usd is None
    assert "auth/key network error" in (info_b.error or "")


# ---------------------------------------------------------------------------
# Dimension 3: Both Endpoints Fail
# ---------------------------------------------------------------------------


def test_stress_both_endpoints_fail_combinations():
    """Verify supported=False and clean error combination when both fail."""
    provider = OpenRouterProvider("openrouter")

    # Both HTTP 401
    exc_401 = urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1/endpoint",
        code=401,
        msg="Unauthorized",
        hdrs=MagicMock(),
        fp=io.BytesIO(b"unauthorized"),
    )
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_error=exc_401, credits_error=exc_401),
    ):
        info = provider.get_credits(api_key="sk-or-v1-invalid-key")
    assert info.supported is False
    assert info.label == "Failed to fetch credits"
    assert "auth/key HTTP 401" in (info.error or "")
    assert "credits HTTP 401" in (info.error or "")

    # One HTTP 429, One HTTP 500
    exc_429 = urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1/auth/key",
        code=429,
        msg="Rate limited",
        hdrs=MagicMock(),
        fp=io.BytesIO(b"rate limited"),
    )
    exc_500 = urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1/credits",
        code=500,
        msg="Server error",
        hdrs=MagicMock(),
        fp=io.BytesIO(b"server error"),
    )
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_error=exc_429, credits_error=exc_500),
    ):
        info_mixed = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
    assert info_mixed.supported is False
    assert "auth/key HTTP 429: Too Many Requests" in (info_mixed.error or "")
    assert "credits HTTP 500: Internal Server Error" in (info_mixed.error or "")


# ---------------------------------------------------------------------------
# Dimension 4: Network Errors, DNS Failures, and Connection Refused
# ---------------------------------------------------------------------------


def test_stress_dns_and_connection_errors():
    """Verify URLError with gaierror (DNS resolution failure) or ConnectionRefused."""
    provider = OpenRouterProvider("openrouter")

    dns_err = urllib.error.URLError(
        reason=socket.gaierror(-3, "Temporary failure in name resolution")
    )
    conn_err = urllib.error.URLError(reason=ConnectionRefusedError(10061, "Connect call failed"))

    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_error=dns_err, credits_error=conn_err),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is False
    assert "auth/key network error" in (info.error or "")
    assert "credits network error" in (info.error or "")
    assert "Temporary failure in name resolution" in (info.error or "")


# ---------------------------------------------------------------------------
# Dimension 5: CodeQL CWE-312 Taint-Safety & Secret Scrubbing
# ---------------------------------------------------------------------------


def test_stress_secret_masking_deep_hostile_payload():
    """Verify that raw keys embedded in URLs, labels, or nested payloads are masked."""
    secret_key = "sk-or-v1-99998888777766665555444433332222"
    key_payload = {
        "data": {
            "label": f"Project key {secret_key} production",
            "usage": 10.0,
            "limit": 100.0,
            "metadata": {
                "bearer_token": secret_key,
                "nested": {
                    "api_key": secret_key,
                    "comment": f"Contains secret {secret_key}",
                },
            },
        }
    }
    credits_payload = {
        "data": {
            "total_credits": 200.0,
            "total_usage": 50.0,
            "auth_details": {
                "token": secret_key,
            },
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen", side_effect=make_mock_urlopen(key_payload, credits_payload)
    ):
        info = provider.get_credits(api_key=secret_key)

    # 1. Label must be masked
    assert secret_key not in (info.label or "")
    assert "[REDACTED]" in (info.label or "") or "sk-or-v1-***" in (info.label or "")

    # 2. Raw data dictionary must have scrubbed fields
    raw_str = json.dumps(info.raw_data)
    assert secret_key not in raw_str

    # 3. Formatted summaries must not leak the secret
    summary = format_credits_summary(info)
    assert secret_key not in summary
    compact = format_credits_summary(info, compact=True)
    assert secret_key not in compact

    # 4. WalletService cache key must not store raw secret
    service = WalletService()
    cache_key = f"openrouter:{secret_key}"
    service.invalidate("openrouter", secret_key)
    # Check that SHA-256 digest is used in cache keys
    with patch(
        "urllib.request.urlopen", side_effect=make_mock_urlopen(key_payload, credits_payload)
    ):
        service.get_credits("openrouter", {}, api_key=secret_key)
    for k in service._cache.keys():
        assert secret_key not in k


# ---------------------------------------------------------------------------
# Dimension 6: Concurrency & Async Thread Dispatch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stress_async_concurrent_queries():
    """Verify concurrent async queries through WalletService.get_credits_async."""
    service = WalletService(cache_ttl_seconds=60.0)
    key_payload = {"data": {"label": "Concurrent Key", "usage": 10.0, "limit": 100.0}}
    credits_payload = {"data": {"total_credits": 500.0, "total_usage": 50.0}}

    with patch(
        "urllib.request.urlopen", side_effect=make_mock_urlopen(key_payload, credits_payload)
    ):
        tasks = [
            service.get_credits_async("openrouter", {}, api_key=f"sk-or-v1-key{i:04d}")
            for i in range(10)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == 10
    for res in results:
        assert res.supported is True
        assert res.balance_usd == 450.0
        assert res.limit_usd == 100.0


# ---------------------------------------------------------------------------
# Dimension 7: Static HTTP Status Mapping Invariant
# ---------------------------------------------------------------------------


def test_stress_static_http_map_completeness():
    """Ensure _HTTP_ERROR_MAP contains all standard HTTP client and server error codes."""
    required_codes = [400, 401, 403, 404, 408, 429, 500, 502, 503, 504]
    for code in required_codes:
        assert code in _HTTP_ERROR_MAP
        desc = _HTTP_ERROR_MAP[code]
        assert isinstance(desc, str)
        assert len(desc) > 3


# ---------------------------------------------------------------------------
# Dimension 8: Safe Float Parsing Edge Cases
# ---------------------------------------------------------------------------


def test_stress_safe_float_primitives():
    """Verify _safe_float against diverse edge-case inputs."""
    assert _safe_float(None) is None
    assert _safe_float("") is None
    assert _safe_float("   ") is None
    assert _safe_float("abc") is None
    assert _safe_float([]) is None
    assert _safe_float({}) is None
    assert _safe_float(42) == 42.0
    assert _safe_float(3.14159) == 3.14159
    assert _safe_float("-0.0") == 0.0
    assert _safe_float("1e-5") == 1e-5
    # Infinity and NaN
    assert math.isinf(_safe_float("inf"))
    assert math.isinf(_safe_float("-inf"))
    assert math.isnan(_safe_float("nan"))


# ---------------------------------------------------------------------------
# Dimension 9: Hostile Payload & Boundary Conditions
# ---------------------------------------------------------------------------


def test_stress_binary_non_utf8_response():
    """Verify raw binary non-UTF8 response body is caught gracefully without unhandled exception."""
    raw_gzip = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\xcb\x48\xcd\xc9\xc9\x07\x00\x86\xa6\x10\x36\x05\x00\x00\x00"
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_raw_bytes=raw_gzip, credits_raw_bytes=raw_gzip),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is False
    assert info.label == "Failed to fetch credits"
    assert "invalid response" in (info.error or "")


def test_stress_crlf_in_api_key_handled_safely():
    """Verify header CRLF injection attempt in API key is caught safely."""
    provider = OpenRouterProvider("openrouter")
    # Newline in header value raises ValueError in urllib / http.client
    info = provider.get_credits(api_key="sk-or-v1-testkey\r\nInjected-Header: malicious")
    assert info.supported is False
    assert info.label == "Failed to fetch credits"


def test_stress_base_url_variations():
    """Verify base_url with trailing slashes is normalized cleanly."""
    provider = OpenRouterProvider(
        "openrouter", config={"base_url": "https://openrouter.ai/api/v1///"}
    )
    key_payload = {"data": {"label": "Normalized URL Key", "usage": 1.0, "limit": 10.0}}
    credits_payload = {"data": {"total_credits": 100.0, "total_usage": 10.0}}

    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload, credits_payload),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")

    assert info.supported is True
    assert info.balance_usd == 90.0
