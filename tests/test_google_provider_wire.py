# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Characterization tests for Google AI Studio OpenAI-shim wire behavior."""

from __future__ import annotations

import json

import httpx2
import pytest
from openai import AsyncOpenAI, BadRequestError, RateLimitError

from botex.core.contracts import classify_api_error
from botex.providers.providers import (
    GoogleAIStudioProvider,
    normalize_response,
    normalize_streaming_chunks,
)
from botex.providers.retry import parse_retry_delay


def test_normalize_response_valid_choice_has_no_shape_diag():
    resp = {
        "id": "chatcmpl-1",
        "model": "gemini-2.5-flash",
        "choices": [
            {
                "message": {"role": "assistant", "content": "pong"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    out = normalize_response(resp)
    assert out.finish_reason == "completed"
    assert out.content == "pong"
    assert out.response_shape is None


@pytest.mark.parametrize("payload", [{"choices": []}, {"id": "missing-choices"}])
def test_normalize_response_empty_or_missing_choices_has_safe_shape_diag(payload):
    out = normalize_response({**payload, "model": "gemini-2.5-flash"})
    assert out.finish_reason == "provider_error"
    assert out.raw_finish_reason == "no_choices"
    assert out.response_shape is not None
    assert out.response_shape["response_type"] == "dict"
    assert "top_level_keys" in out.response_shape
    assert "content" not in out.response_shape


def test_normalize_response_list_body_has_safe_shape_diag():
    out = normalize_response([{"error": {"message": "shim error"}}])
    assert out.finish_reason == "provider_error"
    assert out.raw_finish_reason == "no_choices"
    assert out.response_shape == {"response_type": "list", "choices_type": "missing"}


def test_streaming_empty_and_usage_only_empty_choices_are_not_no_choices():
    empty = normalize_streaming_chunks([], raw_model="gemini-2.5-flash")
    assert empty.finish_reason == "unknown"
    assert empty.raw_finish_reason == "unknown"
    assert empty.content == ""

    usage_only = normalize_streaming_chunks(
        [{"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 0}}],
        raw_model="gemini-2.5-flash",
    )
    assert usage_only.finish_reason == "unknown"
    assert usage_only.raw_finish_reason == "unknown"
    assert usage_only.usage.prompt_tokens == 3


def test_streaming_ignores_choiceless_chunks_and_preserves_later_content():
    out = normalize_streaming_chunks(
        [
            {"choices": []},
            {
                "choices": [
                    {
                        "delta": {"content": "po"},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"content": "ng"},
                        "finish_reason": "stop",
                    }
                ]
            },
        ],
        raw_model="gemini-2.5-flash",
    )
    assert out.content == "pong"
    assert out.finish_reason == "completed"


@pytest.mark.anyio
async def test_google_extra_body_is_stripped_and_merged_top_level_on_wire():
    provider = GoogleAIStudioProvider(
        name="google",
        config={
            "extra_body": {
                "reasoning_effort": "low",
                "provider": {"require_parameters": True},
                "models": ["fallback"],
                "transforms": ["middle-out"],
                "usage": {"include": True},
                "reasoning": {"effort": "low"},
                "plugins": ["x"],
            }
        },
    )
    extra = provider.get_extra_body()
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "gemini-2.5-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    client = AsyncOpenAI(
        api_key="dummy",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        max_retries=0,
        http_client=http_client,
    )
    try:
        await client.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "ping"}],
            extra_body=extra,
            timeout=3,
        )
    finally:
        await client.close()
        await http_client.aclose()

    payload = captured["payload"]
    assert payload["reasoning_effort"] == "low"
    assert "extra_body" not in payload
    assert "timeout" not in payload
    for forbidden in ("provider", "models", "transforms", "usage", "reasoning", "plugins"):
        assert forbidden not in payload


async def _raise_for_shim_body(status: int, body):
    def handler(request):
        return httpx2.Response(status, json=body)

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    client = AsyncOpenAI(
        api_key="dummy",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        max_retries=0,
        http_client=http_client,
    )
    try:
        await client.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "ping"}],
        )
    finally:
        await client.close()
        await http_client.aclose()


@pytest.mark.anyio
async def test_list_shaped_429_surfaces_rate_limit_class_and_retry_delay():
    body = [
        {
            "error": {
                "message": "quota exceeded",
                "code": "rate_limit_exceeded",
                "status": "RESOURCE_EXHAUSTED",
                "retryDelay": "37s",
            }
        }
    ]
    with pytest.raises(RateLimitError) as err:
        await _raise_for_shim_body(429, body)
    exc = err.value
    assert exc.status_code == 429
    assert isinstance(exc.body, list)
    assert classify_api_error(exc) == "provider_rate_limited"
    assert parse_retry_delay(exc) == pytest.approx(37.0)
    assert "rate_limit_exceeded" in str(exc) or "quota exceeded" in str(exc)


@pytest.mark.anyio
async def test_list_shaped_400_surfaces_bad_request_class():
    body = [{"error": {"message": "invalid argument", "code": "invalid_argument"}}]
    with pytest.raises(BadRequestError) as err:
        await _raise_for_shim_body(400, body)
    assert err.value.status_code == 400
    assert isinstance(err.value.body, list)


@pytest.mark.anyio
async def test_list_shaped_503_overloaded_remains_google_retryable():
    body = [{"error": {"message": "model overloaded", "status": "UNAVAILABLE"}}]
    with pytest.raises(Exception) as err:
        await _raise_for_shim_body(503, body)
    provider = GoogleAIStudioProvider(name="google", config={})
    assert getattr(err.value, "status_code", None) == 503
    assert provider.is_retryable(err.value) is True
