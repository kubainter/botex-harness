# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Unit tests for ZaiGLMProvider adapter (botex/providers/adapters/zai.py).
Covers initialization, registry resolution, payload sanitization, finish_reason
re-mapping, live quota JSON parsing, error handling, and failover classification.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from openai import RateLimitError

from botex.providers.adapters.zai import ProviderQuotaExceededError, ZaiGLMProvider
from botex.providers.providers import ProviderRegistry


def _mock_http_response(status: int = 200, payload: dict | None = None) -> MagicMock:
    """Helper creating a mock response context manager for urllib.request.urlopen."""
    body_bytes = json.dumps(payload or {}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body_bytes
    mock_resp.status = status
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None
    return mock_resp


class TestZaiGLMProviderInit:
    """Provider instantiation, config loading, and registry resolution."""

    def test_default_attributes(self):
        prov = ZaiGLMProvider()
        assert prov.name == "zai"
        assert prov.base_url == "https://api.z.ai/api/coding/paas/v4"
        assert prov.api_key_env == "ZAI_API_KEY"
        assert prov.quota_url == "https://api.z.ai/api/monitor/usage/quota/limit"

    def test_custom_attributes(self):
        prov = ZaiGLMProvider(
            name="zai-custom",
            config={
                "base_url": "https://custom.z.ai/v4",
                "quota_url": "https://custom.z.ai/quota",
                "api_key_env": "CUSTOM_ZAI_KEY",
            },
        )
        assert prov.name == "zai-custom"
        assert prov.base_url == "https://custom.z.ai/v4"
        assert prov.quota_url == "https://custom.z.ai/quota"
        assert prov.api_key_env == "CUSTOM_ZAI_KEY"

    def test_provider_registry_resolution(self):
        """Verify zai and glm are not registered in ProviderRegistry per ToS compliance."""
        assert "zai" not in ProviderRegistry.list_providers()
        assert "glm" not in ProviderRegistry.list_providers()
        with pytest.raises(ValueError):
            ProviderRegistry.get("zai")
        with pytest.raises(ValueError):
            ProviderRegistry.get("glm")


class TestZaiPayloadSanitization:
    """Cleansing of OpenRouter-only parameters from extra_body and request payloads."""

    def test_cleanses_openrouter_extra_body(self):
        prov = ZaiGLMProvider(
            config={
                "extra_body": {
                    "transforms": ["middle-out"],
                    "route": "fallback",
                    "provider": {"data_collection": "deny"},
                    "data_collection": "allow",
                    "valid_z_ai_option": 123,
                }
            }
        )
        cleaned = prov.get_extra_body()
        assert "transforms" not in cleaned
        assert "route" not in cleaned
        assert "provider" not in cleaned
        assert "data_collection" not in cleaned
        assert cleaned.get("valid_z_ai_option") == 123

    def test_prepare_request_payload_sanitization(self):
        prov = ZaiGLMProvider()
        payload = prov.prepare_request_payload(
            model="glm-5.3",
            messages=[{"role": "user", "content": "hello"}],
            tools=[{"type": "function", "function": {"name": "test"}}],
            max_tokens=2048,
            temperature=0.1,
            transforms=["strip-me"],
            route="fast",
            custom_arg="keep-me",
        )
        assert payload["model"] == "glm-5.3"
        assert payload["max_tokens"] == 2048
        assert payload["temperature"] == 0.1
        assert "transforms" not in payload
        assert "route" not in payload
        assert payload.get("custom_arg") == "keep-me"
        assert payload["tool_choice"] == "auto"


class TestZaiFinishReasonNormalization:
    """Normalization of proprietary Z.ai finish reasons (sensitive, model_context_window_exceeded)."""

    def test_normalizes_sensitive_to_refusal(self):
        prov = ZaiGLMProvider()
        raw_resp = {
            "id": "chat-123",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "I cannot fulfill this request."},
                    "finish_reason": "sensitive",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        norm = prov.normalize_response(raw_resp)
        assert norm.finish_reason == "refusal"
        assert norm.raw_finish_reason == "sensitive"
        assert norm.content == "I cannot fulfill this request."

    def test_normalizes_context_window_exceeded_to_length(self):
        prov = ZaiGLMProvider()
        raw_resp = {
            "id": "chat-124",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Truncated output..."},
                    "finish_reason": "model_context_window_exceeded",
                }
            ],
            "usage": {"prompt_tokens": 128000, "completion_tokens": 4096},
        }
        norm = prov.normalize_response(raw_resp)
        assert norm.finish_reason == "length"
        assert norm.raw_finish_reason == "model_context_window_exceeded"

    def test_standard_finish_reasons_preserved(self):
        prov = ZaiGLMProvider()
        raw_stop = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Done."},
                    "finish_reason": "stop",
                }
            ],
        }
        norm = prov.normalize_response(raw_stop)
        assert norm.finish_reason == "completed"
        assert norm.raw_finish_reason == "stop"


class TestZaiLiveQuotaParsing:
    """Live Quota API parsing for 5-hour and weekly windows, edge cases, and errors."""

    def test_missing_api_key_returns_unsupported_error(self):
        prov = ZaiGLMProvider()
        credits = prov.get_credits(api_key="")
        assert credits.supported is True
        assert credits.error == "No API key configured"
        assert credits.label == "Missing API key"

    @patch("urllib.request.urlopen")
    def test_parses_5h_and_weekly_limits_successfully(self, mock_urlopen):
        mock_payload = {
            "code": 200,
            "msg": "success",
            "data": {
                "limits": [
                    {
                        "unit": 3,
                        "number": 5,
                        "limit": 1000000,
                        "current_usage": 150000,
                        "percentage": 15.0,
                        "next_reset_time": "2026-11-06T18:00:00Z",
                    },
                    {
                        "unit": 6,
                        "number": 1,
                        "limit": 10000000,
                        "current_usage": 2500000,
                        "percentage": 25.0,
                        "next_reset_time": "2026-11-10T00:00:00Z",
                    },
                ]
            },
        }
        mock_urlopen.return_value = _mock_http_response(200, mock_payload)

        prov = ZaiGLMProvider()
        cred = prov.get_credits(api_key="test-key-123")

        assert cred.supported is True
        assert cred.provider == "zai"
        assert cred.units == "quota"
        assert len(cred.quota_windows) == 2
        assert cred.limit_usd == 1000000.0
        assert cred.usage_usd == 150000.0
        assert cred.limit_remaining_usd == 850000.0
        assert cred.resets_at == "2026-11-06T18:00:00Z"
        assert cred.label == "GLM Quota 15.0%"
        assert cred.error is None
        assert prov.check_quota(cred) is True

    @patch("urllib.request.urlopen")
    def test_quota_exceeded_detection_and_failover_exception(self, mock_urlopen):
        mock_payload = {
            "data": {
                "limits": [
                    {
                        "unit": 3,
                        "number": 5,
                        "limit": 50000,
                        "current_usage": 50000,
                        "percentage": 100.0,
                        "next_reset_time": "2026-11-06T18:00:00Z",
                    }
                ]
            }
        }
        mock_urlopen.return_value = _mock_http_response(200, mock_payload)

        prov = ZaiGLMProvider()
        cred = prov.get_credits(api_key="test-key-123")

        assert cred.error is not None
        assert "quota exceeded" in cred.error.lower()

        # check_quota should raise ProviderQuotaExceededError
        with pytest.raises(ProviderQuotaExceededError) as exc_info:
            prov.check_quota(cred)
        assert "quota exceeded" in str(exc_info.value).lower()

        # get_credits with raise_on_exceeded=True
        mock_urlopen.return_value = _mock_http_response(200, mock_payload)
        with pytest.raises(ProviderQuotaExceededError):
            prov.get_credits(api_key="test-key-123", raise_on_exceeded=True)

    @patch("urllib.request.urlopen")
    def test_http_401_unauthorized_handled(self, mock_urlopen):
        err = urllib.error.HTTPError(
            url="https://api.z.ai/api/monitor/usage/quota/limit",
            code=401,
            msg="Unauthorized",
            hdrs=MagicMock(),
            fp=io.BytesIO(b'{"error": "Invalid token"}'),
        )
        mock_urlopen.side_effect = err

        prov = ZaiGLMProvider()
        cred = prov.get_credits(api_key="invalid-key")
        assert cred.supported is True
        assert "HTTP 401 Unauthorized" in (cred.error or "")

    @patch("urllib.request.urlopen")
    def test_http_429_rate_limit_handled(self, mock_urlopen):
        err = urllib.error.HTTPError(
            url="https://api.z.ai/api/monitor/usage/quota/limit",
            code=429,
            msg="Too Many Requests",
            hdrs=MagicMock(),
            fp=io.BytesIO(b'{"error": "Rate limited"}'),
        )
        mock_urlopen.side_effect = err

        prov = ZaiGLMProvider()
        cred = prov.get_credits(api_key="rate-limited-key")
        assert cred.supported is True
        assert "HTTP 429" in (cred.error or "")

        # When raise_on_exceeded=True, should raise ProviderQuotaExceededError
        mock_urlopen.side_effect = err
        with pytest.raises(ProviderQuotaExceededError):
            prov.get_credits(api_key="rate-limited-key", raise_on_exceeded=True)


class TestZaiRetryAndFailoverClassification:
    """Verifies that transient errors and quota exhaustion are recognized as retryable / failover."""

    def test_is_retryable_classification(self):
        prov = ZaiGLMProvider()

        # ProviderQuotaExceededError is retryable/failover
        quota_err = ProviderQuotaExceededError("5h limit reached")
        assert prov.is_retryable(quota_err) is True

        # RateLimitError is retryable
        rate_err = RateLimitError("Rate limit exceeded", response=MagicMock(), body=None)
        assert prov.is_retryable(rate_err) is True

        # Non-transient errors are not retryable
        val_err = ValueError("Invalid prompt")
        assert prov.is_retryable(val_err) is False

    def test_rate_limit_exceptions_tuple(self):
        prov = ZaiGLMProvider()
        assert ProviderQuotaExceededError in prov.rate_limit_exceptions
        assert RateLimitError in prov.rate_limit_exceptions


class TestZaiQuotaWindowNormalization:
    """Unit tests for quota window normalization, unit mapping, and offline fetch."""

    def test_window_units_map_constants(self):
        from botex.providers.adapters.zai import _WINDOW_UNITS_MAP

        assert _WINDOW_UNITS_MAP[3] == "hours"
        assert _WINDOW_UNITS_MAP[5] == "months"
        assert _WINDOW_UNITS_MAP[6] == "weeks"

    def test_normalize_quota_window_legacy_and_modern_shapes(self):
        from botex.providers.adapters.zai import _normalize_quota_window

        # Legacy shape: snake_case, ISO reset string
        legacy_item = {
            "unit": 3,
            "number": 5,
            "limit": 1000000,
            "current_usage": 150000,
            "percentage": 15.0,
            "next_reset_time": "2026-11-06T18:00:00Z",
        }
        norm_legacy = _normalize_quota_window(legacy_item)
        assert norm_legacy["unit"] == "hours"
        assert norm_legacy["number"] == 5
        assert norm_legacy["limit"] == 1000000.0
        assert norm_legacy["current_usage"] == 150000.0
        assert norm_legacy["percentage"] == 15.0
        assert norm_legacy["next_reset_time"] == "2026-11-06T18:00:00Z"

        # Modern shape: camelCase, ms epoch timestamp
        modern_item = {
            "unit": 3,
            "number": 5,
            "type": "CREDIT_LIMIT",
            "limit": 1000000,
            "currentUsage": 150000,
            "remaining": 850000,
            "percentage": 15.0,
            "nextResetTime": 1794074400000,
        }
        norm_modern = _normalize_quota_window(modern_item)
        assert norm_modern["unit"] == "hours"
        assert norm_modern["number"] == 5
        assert norm_modern["type"] == "credits"
        assert norm_modern["limit"] == 1000000.0
        assert norm_modern["current_usage"] == 150000.0
        assert norm_modern["remaining"] == 850000.0
        assert norm_modern["percentage"] == 15.0
        assert norm_modern["next_reset_time"] is not None
        assert "2026-" in norm_modern["next_reset_time"]

    def test_fetch_catalog_without_key_returns_empty_list(self):
        prov = ZaiGLMProvider()
        catalog = prov.fetch_catalog(api_key="")
        assert catalog == []
