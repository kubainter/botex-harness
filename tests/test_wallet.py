# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Hermetic Offline Unit Tests for BoteX Wallet & Credits Service
==============================================================
Validates CreditsInfo data model, OpenRouterProvider credit queries,
partial failure resilience, secret masking (CWE-312), WalletService dispatch,
TTL caching, and formatted summaries.

All tests are 100% offline; any attempt to make a live network socket call
aborts immediately via block_live_sockets fixture.
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
import urllib.error
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from botex.providers import (
    BaseProvider,
    CreditsInfo,
    GenericOpenAIProvider,
    NvidiaProvider,
    OpenRouterProvider,
    ProviderRegistry,
    _sanitize_raw_payload,
    sanitize_provider_error,
)
from botex.security import mask_secrets
from botex.wallet import (
    WalletService,
    format_credits_summary,
    get_credits,
    parse_openrouter_credits_data,
    parse_openrouter_key_data,
    parse_openrouter_payloads,
)


# ---------------------------------------------------------------------------
# Fixtures: Hermetic Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Ensure no live network calls can be made during wallet tests (TEST-WAL-00)."""
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

        raise RuntimeError("Blocked live network socket call during unit test execution")

    with patch.object(socket.socket, "connect", blocked_connect):
        yield


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch):
    """Clear all provider API keys from environment to guarantee hermetic behavior."""
    keys_to_clear = [
        "OPENROUTER_API_KEY",
        "OPENROUTER_API_FREE_KEY",
        "NVIDIA_API_KEY",
        "NVAPI_KEY",
        "OPENAI_API_KEY",
        "CUSTOM_KEY",
    ]
    for key in keys_to_clear:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def clean_registry():
    """Restore ProviderRegistry._registry state after tests that mutate registrations."""
    orig_registry = dict(ProviderRegistry._registry)
    yield
    ProviderRegistry._registry = orig_registry


def make_mock_urlopen(
    key_payload: Optional[Dict[str, Any]] = None,
    credits_payload: Optional[Dict[str, Any]] = None,
    key_error: Optional[Exception] = None,
    credits_error: Optional[Exception] = None,
):
    """Factory creating a mock urlopen handler simulating endpoint responses."""

    def mock_urlopen(req: Any, timeout: float = 10.0) -> Any:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "auth/key" in url:
            if key_error:
                raise key_error
            resp_mock = MagicMock()
            content = json.dumps(key_payload or {}).encode("utf-8")
            resp_mock.read.return_value = content
            resp_mock.__enter__.return_value = resp_mock
            return resp_mock
        elif "credits" in url:
            if credits_error:
                raise credits_error
            resp_mock = MagicMock()
            content = json.dumps(credits_payload or {}).encode("utf-8")
            resp_mock.read.return_value = content
            resp_mock.__enter__.return_value = resp_mock
            return resp_mock
        raise RuntimeError(f"Unexpected unmocked URL in test: {url}")

    return mock_urlopen


# ---------------------------------------------------------------------------
# Group 0: Offline Socket Isolation Verification
# ---------------------------------------------------------------------------


def test_block_live_sockets_intercepts_network():
    """TEST-WAL-00: Verify that block_live_sockets intercepts socket connection attempts."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(
        RuntimeError, match="Blocked live network socket call during unit test execution"
    ):
        s.connect(("127.0.0.1", 80))


# ---------------------------------------------------------------------------
# Group 1: OpenRouter /api/v1/auth/key Endpoint & Key Metadata
# ---------------------------------------------------------------------------


def test_openrouter_paid_key_usage_and_limit():
    """TEST-WAL-01: Verify parsing of label, usage, limit, and computed limit remaining."""
    key_payload = {
        "data": {
            "label": "Production Agent Key",
            "usage": 14.50,
            "limit": 50.00,
            "is_free_tier": False,
        }
    }
    credits_payload = {
        "data": {
            "total_credits": 100.00,
            "total_usage": 35.00,
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload=key_payload, credits_payload=credits_payload),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
        assert info.supported is True
        assert info.provider == "openrouter"
        assert info.label == "Production Agent Key"
        assert info.limit_usd == 50.00
        assert info.limit_remaining_usd == 35.50
        assert info.is_free_tier is False


def test_openrouter_free_tier_key():
    """TEST-WAL-02: Verify is_free_tier flag mapping and unlimited / None limit handling."""
    key_payload = {
        "data": {
            "label": "Free Tier Key",
            "usage": 0.00,
            "limit": None,
            "is_free_tier": True,
        }
    }
    credits_payload = {"data": {}}
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload=key_payload, credits_payload=credits_payload),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
        assert info.is_free_tier is True
        assert info.limit_usd is None
        assert info.limit_remaining_usd is None
        assert info.label == "Free Tier Key"


def test_openrouter_unlimited_paid_key():
    """TEST-WAL-03: Verify handling of paid keys with no spending ceiling."""
    key_payload = {
        "data": {
            "label": "Corporate Unlimited",
            "usage": 128.40,
            "limit": None,
            "is_free_tier": False,
        }
    }
    credits_payload = {
        "data": {
            "total_credits": 500.00,
            "total_usage": 128.40,
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload=key_payload, credits_payload=credits_payload),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
        assert info.limit_usd is None
        assert info.limit_remaining_usd is None
        assert info.is_free_tier is False
        assert info.label == "Corporate Unlimited"
        assert info.balance_usd == 371.60


def test_openrouter_over_limit_key_exhaustion():
    """TEST-WAL-04: Verify that limit_remaining_usd is bounded to non-negative 0.0."""
    key_payload = {
        "data": {
            "label": "Exhausted Key",
            "usage": 105.00,
            "limit": 100.00,
            "is_free_tier": False,
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload=key_payload, credits_payload={"data": {}}),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
        assert info.limit_usd == 100.00
        assert info.limit_remaining_usd == 0.0


# ---------------------------------------------------------------------------
# Group 2: OpenRouter /api/v1/credits Endpoint & Composite Balance
# ---------------------------------------------------------------------------


def test_openrouter_full_composite_calculation():
    """TEST-WAL-05: Verify arithmetic combining /api/v1/auth/key and /api/v1/credits."""
    key_payload = {
        "data": {
            "label": "Dev Key",
            "usage": 5.00,
            "limit": 20.00,
            "is_free_tier": False,
        }
    }
    credits_payload = {
        "data": {
            "total_credits": 50.00,
            "total_usage": 12.25,
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload=key_payload, credits_payload=credits_payload),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
        assert info.balance_usd == 37.75
        assert info.usage_usd == 12.25
        assert info.limit_usd == 20.00
        assert info.limit_remaining_usd == 15.00
        assert info.label == "Dev Key"
        assert info.raw_data["key"]["label"] == "Dev Key"
        assert info.raw_data["credits"]["total_credits"] == 50.00


def test_openrouter_zero_balance_account_exhaustion():
    """TEST-WAL-06: Verify balance calculation when total_credits equals total_usage."""
    credits_payload = {
        "data": {
            "total_credits": 25.00,
            "total_usage": 25.00,
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload={"data": {}}, credits_payload=credits_payload),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
        assert info.balance_usd == 0.0
        assert info.usage_usd == 25.00


def test_openrouter_string_encoded_floats():
    """TEST-WAL-07: Verify robust float conversion when API returns numeric strings."""
    key_payload = {
        "data": {
            "usage": "2.50",
            "limit": "10.00",
        }
    }
    credits_payload = {
        "data": {
            "total_credits": "15.00",
            "total_usage": "5.00",
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload=key_payload, credits_payload=credits_payload),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
        assert info.balance_usd == 10.00
        assert info.limit_usd == 10.00
        assert info.limit_remaining_usd == 7.50


def test_openrouter_empty_response_data():
    """TEST-WAL-08: Verify behavior when both endpoints return empty data payloads."""
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload={"data": {}}, credits_payload={"data": {}}),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
        assert info.supported is True
        assert info.balance_usd is None
        assert info.usage_usd is None
        assert info.limit_usd is None
        assert info.limit_remaining_usd is None
        assert info.label is None


# ---------------------------------------------------------------------------
# Group 3: Partial Endpoint Failures & Fallback Resilience
# ---------------------------------------------------------------------------


def test_openrouter_credits_fails_fallback_to_key():
    """TEST-WAL-09: Verify retention of key limit when /api/v1/credits raises HTTP 404."""
    key_payload = {
        "data": {
            "label": "Key A",
            "limit": 30.00,
            "usage": 10.00,
        }
    }
    cred_err = urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1/credits",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=io.BytesIO(b'{"error": "Endpoint not found"}'),
    )
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload=key_payload, credits_error=cred_err),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
        assert info.supported is True
        assert info.balance_usd is None
        assert info.usage_usd is None
        assert info.limit_usd == 30.00
        assert info.limit_remaining_usd == 20.00
        assert info.label == "Key A"
        assert info.error is not None
        assert "404" in str(info.error)


def test_openrouter_key_fails_fallback_to_credits():
    """TEST-WAL-10: Verify retention of account balance when /api/v1/auth/key raises HTTP 500."""
    key_err = urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1/auth/key",
        code=500,
        msg="Internal Server Error",
        hdrs={},
        fp=io.BytesIO(b'{"error": "Server error"}'),
    )
    credits_payload = {
        "data": {
            "total_credits": 100.00,
            "total_usage": 20.00,
        }
    }
    provider = OpenRouterProvider("openrouter")
    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_error=key_err, credits_payload=credits_payload),
    ):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
        assert info.supported is True
        assert info.balance_usd == 80.00
        assert info.usage_usd == 20.00
        assert info.limit_usd is None
        assert info.limit_remaining_usd is None
        assert info.label is None
        assert info.error is not None
        assert "500" in str(info.error)


def test_openrouter_both_endpoints_fail():
    """TEST-WAL-11: Verify graceful error representation when both endpoints fail."""
    err = urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1",
        code=500,
        msg="Internal Server Error",
        hdrs={},
        fp=io.BytesIO(b'{"error": "Down"}'),
    )
    provider = OpenRouterProvider("openrouter")
    with patch("urllib.request.urlopen", side_effect=err):
        info = provider.get_credits(api_key="sk-or-v1-testkey1234567890")
        assert info.supported is False
        assert info.balance_usd is None
        assert info.limit_usd is None
        assert info.error is not None


# ---------------------------------------------------------------------------
# Group 4: Unsupported Providers & Default Fallbacks
# ---------------------------------------------------------------------------


def test_nvidia_credits_unsupported():
    """TEST-WAL-12: Verify that NvidiaProvider reports supported=False without network calls."""
    provider = NvidiaProvider("nvidia")
    mock_urlopen = MagicMock()
    with patch("urllib.request.urlopen", mock_urlopen):
        info = provider.get_credits()
        assert info.supported is False
        assert info.provider == "nvidia"
        assert info.label == "NVIDIA NIM (No Credits API)"
        assert info.balance_usd is None
        assert mock_urlopen.call_count == 0


def test_generic_openai_credits_unsupported():
    """TEST-WAL-13: Verify that GenericOpenAIProvider reports supported=False without network calls."""
    provider = GenericOpenAIProvider("custom")
    mock_urlopen = MagicMock()
    with patch("urllib.request.urlopen", mock_urlopen):
        info = provider.get_credits()
        assert info.supported is False
        assert info.provider == "custom"
        assert info.balance_usd is None
        assert mock_urlopen.call_count == 0


def test_base_provider_default_credits_unsupported():
    """TEST-WAL-14: Verify BaseProvider.get_credits default implementation."""

    class MinimalProvider(BaseProvider):
        pass

    provider = MinimalProvider("minimal")
    info = provider.get_credits()
    assert info.supported is False
    assert info.provider == "minimal"


# ---------------------------------------------------------------------------
# Group 5: Missing & Unconfigured API Keys
# ---------------------------------------------------------------------------


def test_openrouter_missing_api_key():
    """TEST-WAL-15: Verify missing API key returns supported=True with Missing API key label."""
    provider = OpenRouterProvider("openrouter")
    mock_urlopen = MagicMock()
    with patch("urllib.request.urlopen", mock_urlopen):
        info = provider.get_credits(api_key=None)
        assert info.supported is True
        assert info.provider == "openrouter"
        assert info.label == "Missing API key"
        assert info.balance_usd is None
        assert mock_urlopen.call_count == 0


def test_openrouter_free_tier_key_name_fallback(monkeypatch: pytest.MonkeyPatch):
    """TEST-WAL-16: Verify openrouter_free adapter checks OPENROUTER_API_FREE_KEY."""
    monkeypatch.setenv("OPENROUTER_API_FREE_KEY", "sk-or-v1-freekey")
    provider = OpenRouterProvider("openrouter_free")
    assert provider.resolve_api_key() == "sk-or-v1-freekey"

    captured_headers: Dict[str, str] = {}

    def capture_auth_urlopen(req: Any, timeout: float = 10.0) -> Any:
        nonlocal captured_headers
        captured_headers["Authorization"] = req.get_header("Authorization")
        resp = MagicMock()
        resp.read.return_value = b'{"data": {}}'
        resp.__enter__.return_value = resp
        return resp

    with patch("urllib.request.urlopen", side_effect=capture_auth_urlopen):
        provider.get_credits()
        assert captured_headers.get("Authorization") == "Bearer sk-or-v1-freekey"


# ---------------------------------------------------------------------------
# Group 6: Upstream Network & HTTP Error Handling
# ---------------------------------------------------------------------------


def test_openrouter_http_401_unauthorized():
    """TEST-WAL-17: Verify handling of invalid / revoked API key (HTTP 401)."""
    err = urllib.error.HTTPError(
        url="",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"error": "Invalid API key"}'),
    )
    provider = OpenRouterProvider("openrouter")
    with patch("urllib.request.urlopen", side_effect=err):
        info = provider.get_credits(api_key="invalid-key")
        assert info.balance_usd is None
        assert info.error is not None
        assert "401" in str(info.error)


def test_openrouter_http_429_rate_limit():
    """TEST-WAL-18: Verify handling of upstream rate limiting (HTTP 429)."""
    err = urllib.error.HTTPError(
        url="",
        code=429,
        msg="Rate Limit Exceeded",
        hdrs={},
        fp=io.BytesIO(b'{"error": "Too Many Requests"}'),
    )
    provider = OpenRouterProvider("openrouter")
    with patch("urllib.request.urlopen", side_effect=err):
        info = provider.get_credits(api_key="valid-key")
        assert info.error is not None
        assert "429" in str(info.error)


def test_openrouter_http_500_server_error():
    """TEST-WAL-19: Verify handling of upstream server errors (HTTP 502 Bad Gateway)."""
    err = urllib.error.HTTPError(
        url="",
        code=502,
        msg="Bad Gateway",
        hdrs={},
        fp=io.BytesIO(b"Bad Gateway"),
    )
    provider = OpenRouterProvider("openrouter")
    with patch("urllib.request.urlopen", side_effect=err):
        info = provider.get_credits(api_key="valid-key")
        assert info.error is not None
        assert "502" in str(info.error)


def test_openrouter_network_timeout():
    """TEST-WAL-20: Verify handling of network connection / read timeouts."""
    err = urllib.error.URLError("Connection timed out")
    provider = OpenRouterProvider("openrouter")
    with patch("urllib.request.urlopen", side_effect=err):
        info = provider.get_credits(api_key="valid-key")
        assert info.error is not None
        assert "network" in str(info.error).lower() or "timed out" in str(info.error).lower()


def test_openrouter_malformed_non_json_body():
    """TEST-WAL-21: Verify handling of HTML / truncated response bodies."""
    resp = MagicMock()
    resp.read.return_value = b"<html><head><title>504 Gateway Time-out</title></head></html>"
    resp.__enter__.return_value = resp
    provider = OpenRouterProvider("openrouter")
    with patch("urllib.request.urlopen", return_value=resp):
        info = provider.get_credits(api_key="valid-key")
        assert info.balance_usd is None
        assert info.limit_usd is None


# ---------------------------------------------------------------------------
# Group 7: Secret Masking & CodeQL CWE-312 Taint-Safety
# ---------------------------------------------------------------------------


def test_secret_masking_in_error_messages():
    """TEST-WAL-22: Verify API keys in exceptions are redacted via sanitize_provider_error."""
    raw_key = "sk-or-v1-secret9876543210abcdef9876543210"
    exc = Exception(f"Failed to query balance with bearer token {raw_key}")
    sanitized = sanitize_provider_error(exc)
    assert raw_key not in sanitized
    assert "[REDACTED]" in sanitized


def test_secret_masking_in_authorization_headers():
    """TEST-WAL-23: Verify Authorization Bearer headers are masked via mask_secrets."""
    raw_key = "sk-or-v1-secret9876543210abcdef9876543210"
    headers = {"Authorization": f"Bearer {raw_key}"}
    masked = mask_secrets(str(headers))
    assert raw_key not in masked
    assert "[REDACTED]" in masked


def test_secret_masking_in_credits_info_serialization():
    """TEST-WAL-24: Verify CreditsInfo str, repr, and raw_data never contain cleartext API keys."""
    raw_key = "sk-or-v1-secret9876543210abcdef9876543210"
    dirty_raw_data = {
        "key": {"label": f"Key for {raw_key}", "api_key": raw_key},
        "credits": {"token": raw_key, "total_credits": 25.0},
    }
    clean_raw_data = {
        "key": _sanitize_raw_payload(dirty_raw_data["key"]),
        "credits": _sanitize_raw_payload(dirty_raw_data["credits"]),
    }
    info = CreditsInfo(
        supported=True,
        provider="openrouter",
        label=mask_secrets(f"My key {raw_key}"),
        raw_data=clean_raw_data,
    )
    assert raw_key not in str(info)
    assert raw_key not in repr(info)
    assert raw_key not in json.dumps(info.raw_data)


# ---------------------------------------------------------------------------
# Group 8: WalletService Provider Dispatch & Caching
# ---------------------------------------------------------------------------


def test_wallet_service_provider_dispatch():
    """TEST-WAL-25: Verify WalletService correctly dispatches to registered providers."""
    service = WalletService()
    key_payload = {"data": {"label": "Dispatch Test", "usage": 1.0, "limit": 10.0}}
    cred_payload = {"data": {"total_credits": 20.0, "total_usage": 5.0}}

    with patch(
        "urllib.request.urlopen",
        side_effect=make_mock_urlopen(key_payload=key_payload, credits_payload=cred_payload),
    ):
        info_or = service.get_credits("openrouter", api_key="sk-test-key")
        assert info_or.supported is True
        assert info_or.provider == "openrouter"

    info_nv = service.get_credits("nvidia")
    assert info_nv.supported is False
    assert info_nv.provider == "nvidia"

    info_gen = service.get_credits("generic")
    assert info_gen.supported is False
    assert info_gen.provider == "generic"


def test_wallet_service_unknown_provider():
    """TEST-WAL-26: Verify querying unregistered provider returns supported=False cleanly."""
    service = WalletService()
    info = service.get_credits("unknown_provider_xyz")
    assert info.supported is False
    assert info.provider == "unknown_provider_xyz"
    assert "Unknown" in str(info.label) or "not registered" in str(info.error)


def test_wallet_service_ttl_caching_and_force_refresh():
    """TEST-WAL-27: Verify in-memory TTL caching and force_refresh cache bypass."""
    service = WalletService(cache_ttl_seconds=60.0)
    mock_urlopen = MagicMock()
    resp_mock = MagicMock()
    resp_mock.read.return_value = b'{"data": {"label": "Cached", "usage": 1.0, "limit": 10.0}}'
    resp_mock.__enter__.return_value = resp_mock
    mock_urlopen.return_value = resp_mock

    with patch("urllib.request.urlopen", mock_urlopen):
        # 1st call -> query upstream (2 endpoints: /auth/key and /credits)
        info1 = service.get_credits("openrouter", api_key="sk-test-cache-key")
        assert mock_urlopen.call_count == 2
        assert info1.supported is True

        # 2nd call within TTL -> cache hit (no new network requests)
        info2 = service.get_credits("openrouter", api_key="sk-test-cache-key")
        assert mock_urlopen.call_count == 2
        assert info2 is info1

        # 3rd call with force_refresh=True -> bypasses cache (makes 2 new calls)
        info3 = service.get_credits("openrouter", api_key="sk-test-cache-key", force_refresh=True)
        assert mock_urlopen.call_count == 4
        assert info3 is not None


# ---------------------------------------------------------------------------
# Group 9: WalletService Human-Readable Summaries
# ---------------------------------------------------------------------------


def test_wallet_service_format_summary_standard():
    """TEST-WAL-28: Verify format_credits_summary for standard supported account."""
    info = CreditsInfo(
        supported=True,
        provider="openrouter",
        balance_usd=37.75,
        usage_usd=12.25,
        limit_usd=20.00,
        limit_remaining_usd=15.00,
        label="Dev Key",
    )
    summary = WalletService.format_credits_summary(info)
    assert "openrouter" in summary.lower()
    assert "37.75" in summary
    assert "12.25" in summary
    assert "20.00" in summary
    assert "15.00" in summary
    assert "Dev Key" in summary

    compact = format_credits_summary(info, compact=True)
    assert "37.75" in compact
    assert "openrouter" in compact


def test_wallet_service_format_summary_free_tier():
    """TEST-WAL-29: Verify format_credits_summary for free-tier account."""
    info = CreditsInfo(
        supported=True,
        provider="openrouter",
        is_free_tier=True,
        label="Free Bot",
    )
    summary = WalletService.format_credits_summary(info)
    assert "free" in summary.lower()
    assert "Free Bot" in summary
    assert "None" not in summary

    compact = format_credits_summary(info, compact=True)
    assert "Free" in compact
    assert "Free Bot" in compact


def test_wallet_service_format_summary_unsupported_and_missing():
    """TEST-WAL-30: Verify formatting for unsupported providers and missing API keys."""
    # Case A: Unsupported provider
    info_unsupp = CreditsInfo(
        supported=False,
        provider="nvidia",
        label="NVIDIA NIM (No Credits API)",
    )
    summary_unsupp = WalletService.format_credits_summary(info_unsupp)
    assert "not supported" in summary_unsupp.lower() or "no credits api" in summary_unsupp.lower()

    # Case B: Missing API key
    info_missing = CreditsInfo(
        supported=True,
        provider="openrouter",
        label="Missing API key",
    )
    summary_missing = WalletService.format_credits_summary(info_missing)
    assert (
        "missing api key" in summary_missing.lower() or "not configured" in summary_missing.lower()
    )


def test_format_credits_summary_masks_unsupported_label_secrets():
    """TEST-WAL-31: Verify format_credits_summary masks secrets in info.label for unsupported providers."""
    raw_key = "sk-or-v1-supersecret1234567890abcdef1234567890abcdef"
    info = CreditsInfo(
        supported=False,
        provider="custom_provider",
        label=f"Failed key {raw_key}",
    )

    # Standard multi-line formatting
    std_summary = format_credits_summary(info, compact=False)
    assert raw_key not in std_summary
    assert "[REDACTED]" in std_summary
    assert "Credits API not supported (Failed key [REDACTED])" in std_summary

    # Compact single-line formatting
    compact_summary = format_credits_summary(info, compact=True)
    assert raw_key not in compact_summary
    assert "[REDACTED]" in compact_summary
    assert "[custom_provider] Credits API not supported (Failed key [REDACTED])" in compact_summary

    # Static facade verification
    facade_summary = WalletService.format_credits_summary(info, compact=False)
    assert raw_key not in facade_summary
    assert "[REDACTED]" in facade_summary


def test_format_credits_summary_masks_provider_and_error_secrets():
    """TEST-WAL-32: Verify format_credits_summary masks secrets in info.provider and info.error."""
    prov_key = "sk-or-v1-providersecret1234567890abcdef12345678"
    err_key = "sk-or-v1-errorsecret1234567890abcdef1234567890"
    info = CreditsInfo(
        supported=False,
        provider=f"test-{prov_key}",
        label="No Credits API",
        error=f"Rejected token {err_key}",
    )

    std_summary = format_credits_summary(info, compact=False)
    assert prov_key not in std_summary
    assert err_key not in std_summary
    assert "[REDACTED]" in std_summary

    compact_summary = format_credits_summary(info, compact=True)
    assert prov_key not in compact_summary
    assert "[REDACTED]" in compact_summary


# ---------------------------------------------------------------------------
# Additional Verification: Async, Direct Providers & Parsing Helpers
# ---------------------------------------------------------------------------


def test_wallet_service_async():
    """Verify get_credits_async functions properly on a worker thread."""
    service = WalletService()
    info = asyncio.run(service.get_credits_async("nvidia"))
    assert info.supported is False
    assert info.provider == "nvidia"


def test_wallet_service_direct_provider_instance():
    """Verify WalletService accepts direct BaseProvider instance."""
    service = WalletService()
    prov = NvidiaProvider("nvidia")
    info = service.get_credits(provider=prov)
    assert info.supported is False
    assert info.provider == "nvidia"


def test_wallet_service_clear_cache_and_invalidate():
    """Verify clear_cache and invalidate methods on WalletService."""
    service = WalletService()
    service._cache["test:default"] = (100.0, CreditsInfo(supported=True, provider="test"))
    assert len(service._cache) == 1
    service.invalidate("test")
    assert len(service._cache) == 0

    service._cache["test:default"] = (100.0, CreditsInfo(supported=True, provider="test"))
    service.clear_cache()
    assert len(service._cache) == 0


def test_parse_helpers_isolated():
    """Verify isolated operation of parse_openrouter helpers."""
    key_tuple = parse_openrouter_key_data({"data": {"usage": 5.0, "limit": 20.0, "label": "Key"}})
    assert key_tuple.usage == 5.0
    assert key_tuple.limit == 20.0
    assert key_tuple.limit_remaining == 15.0
    assert key_tuple.label == "Key"

    cred_tuple = parse_openrouter_credits_data(
        {"data": {"total_credits": 50.0, "total_usage": 10.0}}
    )
    assert cred_tuple.total_credits == 50.0
    assert cred_tuple.total_usage == 10.0
    assert cred_tuple.balance == 40.0

    comb_info = parse_openrouter_payloads(
        key_payload={"data": {"limit": 10.0}},
        credits_payload={"data": {"total_credits": 20.0, "total_usage": 5.0}},
    )
    assert comb_info.supported is True
    assert comb_info.balance_usd == 15.0
    assert comb_info.limit_usd == 10.0

    # Test top-level get_credits convenience function
    top_info = get_credits("nvidia")
    assert top_info.supported is False


# ---------------------------------------------------------------------------
# M3 R3 Regression Tests: Secret Masking & Cache Key Sanitization
# ---------------------------------------------------------------------------


def test_build_cache_key_secret_sanitization():
    """TEST-WAL-33: Verify WalletService._build_cache_key sanitizes uppercase, lowercase, and prefixed secrets."""
    service = WalletService()
    secret_inputs = [
        ("Bearer 1234567890abcdef1234567890abcdef", "1234567890abcdef1234567890abcdef"),
        ("bearer 1234567890abcdef1234567890abcdef", "1234567890abcdef1234567890abcdef"),
        ("BEARER 1234567890abcdef1234567890abcdef", "1234567890abcdef1234567890abcdef"),
        ("AKIAIOSFODNN7EXAMPLE12", "AKIAIOSFODNN7EXAMPLE12"),
        ("AIzaSyD-1234567890abcdefghijklmnopqrstuv", "AIzaSyD-1234567890abcdefghijklmnopqrstuv"),
        ("sk-or-v1-abcdef1234567890abcdef1234567890", "sk-or-v1-abcdef1234567890abcdef1234567890"),
        ("nvapi-abcdef1234567890abcdef1234567890", "nvapi-abcdef1234567890abcdef1234567890"),
    ]
    for raw_secret, token_val in secret_inputs:
        key = service._build_cache_key(raw_secret)
        assert "[redacted]" in key, (
            f"Expected [redacted] in cache key for '{raw_secret}', got '{key}'"
        )
        assert token_val.lower() not in key.lower() or "[redacted]" in key

    # Standard benign provider names normalize cleanly to lowercase
    assert service._build_cache_key("OpenRouter") == "openrouter:default"
    assert service._build_cache_key("nvidia") == "nvidia:default"
    assert service._build_cache_key("  generic  ") == "generic:default"


def test_wallet_service_get_credits_masks_lowercase_bearer():
    """TEST-WAL-34: Verify WalletService.get_credits masks lowercase bearer tokens in fallback and exceptions."""
    service = WalletService()
    raw_token = "token1234567890abcdef1234567890abcdef"
    bearer_provider = f"bearer {raw_token}"

    # 1. Unknown / unregistered provider fallback
    info_fallback = service.get_credits(provider=bearer_provider)
    assert info_fallback.supported is False
    assert raw_token not in str(info_fallback.provider)
    assert "[REDACTED]" in str(info_fallback.provider)

    # 2. Upstream provider exception containing lowercase bearer token
    class FailingBearerProvider(BaseProvider):
        def __init__(self) -> None:
            super().__init__(name="failing_bearer")

        def resolve_api_key(self, **kwargs) -> Optional[str]:
            return "dummy_key"

        def get_client(self, **kwargs) -> Any:
            pass

        def normalize_response(self, response: Any) -> Any:
            pass

        def sanitize_error(self, exc: Exception, max_chars: int = 1024) -> str:
            return str(exc)

        def fetch_catalog(self, **kwargs) -> list:
            return []

        def get_credits(self, **kwargs) -> CreditsInfo:
            raise RuntimeError(f"HTTP 401 Unauthorized: bearer {raw_token}")

    info_err = service.get_credits(provider=FailingBearerProvider())
    assert info_err.supported is False
    assert raw_token not in str(info_err.error)
    assert "[REDACTED]" in str(info_err.error)


def test_format_credits_summary_masks_lowercase_bearer_all_branches():
    """TEST-WAL-35: Verify format_credits_summary masks lowercase bearer tokens across all account status branches."""
    raw_token = "sec1234567890abcdef1234567890"
    bearer_token = f"bearer {raw_token}"

    # Branch A: Unsupported provider
    info_unsupp = CreditsInfo(
        supported=False,
        provider=bearer_token,
        label=f"Invalid {bearer_token}",
        error=f"Rejected {bearer_token}",
    )
    s_unsupp = format_credits_summary(info_unsupp, compact=False)
    s_unsupp_c = format_credits_summary(info_unsupp, compact=True)
    assert raw_token not in s_unsupp and "[REDACTED]" in s_unsupp
    assert raw_token not in s_unsupp_c and "[REDACTED]" in s_unsupp_c

    # Branch B: Free tier account
    info_free = CreditsInfo(
        supported=True,
        provider="openrouter",
        is_free_tier=True,
        label=f"Free key {bearer_token}",
    )
    s_free = format_credits_summary(info_free, compact=False)
    s_free_c = format_credits_summary(info_free, compact=True)
    assert raw_token not in s_free and "[REDACTED]" in s_free
    assert raw_token not in s_free_c and "[REDACTED]" in s_free_c

    # Branch C: Standard paid account
    info_paid = CreditsInfo(
        supported=True,
        provider=bearer_token,
        label=f"Paid key {bearer_token}",
        balance_usd=50.0,
        usage_usd=10.0,
        limit_usd=100.0,
        limit_remaining_usd=90.0,
        error=f"Warning {bearer_token}",
    )
    s_paid = format_credits_summary(info_paid, compact=False)
    s_paid_c = format_credits_summary(info_paid, compact=True)
    assert raw_token not in s_paid and "[REDACTED]" in s_paid
    assert raw_token not in s_paid_c and "[REDACTED]" in s_paid_c

    # Static facade verification
    s_facade = WalletService.format_credits_summary(info_paid, compact=False)
    assert raw_token not in s_facade and "[REDACTED]" in s_facade
