# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Plan 057 Phase-A characterization of the Google OpenAI-compatible wire."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI, BadRequestError, InternalServerError, RateLimitError

from botex.core.contracts import classify_api_error
from botex.providers.providers import (
    GoogleAIStudioProvider,
    OpenRouterProvider,
    normalize_api_error_shape,
    normalize_response,
    normalize_streaming_chunks,
)
from botex.providers.retry import parse_retry_delay

GOOGLE_MODEL = "gemini-2.5-flash"
GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENROUTER_ONLY_KEYS = frozenset(
    {"provider", "models", "transforms", "usage", "reasoning", "plugins"}
)


def _google_config_with_openrouter_contamination() -> dict[str, Any]:
    return {
        "base_url": GOOGLE_BASE_URL,
        "api_key_env": "GEMINI_API_KEY",
        "stream": True,
        "zdr": {"enabled": False},
        "retry": {"enabled": True, "max_waits": 2, "max_wait_s": 60.0, "jitter_s": 1.5},
        "capabilities": {GOOGLE_MODEL: {"supports_tools": True}},
        "models": {"default": GOOGLE_MODEL},
        "extra_body": {
            "reasoning_effort": "low",
            "google": {"thinking_config": {"thinking_budget": 128}},
            "provider": {"data_collection": "deny", "require_parameters": True},
            "models": [GOOGLE_MODEL, "gemini-2.5-flash-lite"],
            "transforms": ["middle-out"],
            "usage": {"include": True},
            "reasoning": {"max_tokens": 3000},
            "plugins": [{"id": "context-compression", "enabled": True}],
        },
    }


def _tool_declaration() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a workspace file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }


def _sse_body(events: list[dict[str, Any]]) -> bytes:
    frames = [f"data: {json.dumps(event, separators=(',', ':'))}" for event in events]
    frames.append("data: [DONE]")
    return ("\n\n".join(frames) + "\n\n").encode()


def _stream_events_normal() -> list[dict[str, Any]]:
    return [
        {
            "id": "chatcmpl-google-stream",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": GOOGLE_MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "STATUS: DONE"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-google-stream",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": GOOGLE_MODEL,
            "choices": [
                {"index": 0, "delta": {"content": "\nwire captured"}, "finish_reason": None}
            ],
        },
        {
            "id": "chatcmpl-google-stream",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": GOOGLE_MODEL,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chatcmpl-google-stream",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": GOOGLE_MODEL,
            "choices": [],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 4,
                "total_tokens": 15,
                "completion_tokens_details": {"reasoning_tokens": 1},
                "prompt_tokens_details": {"cached_tokens": 3},
            },
        },
    ]


def _stream_event_usage_only() -> dict[str, Any]:
    return {
        "id": "chatcmpl-google-stream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": GOOGLE_MODEL,
        "choices": [],
        "usage": {"prompt_tokens": 11, "completion_tokens": 0, "total_tokens": 11},
    }


async def _collect_sse_chunks(events: list[dict[str, Any]]) -> list[Any]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=_sse_body(events),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = AsyncOpenAI(
            base_url=GOOGLE_BASE_URL,
            api_key="unit-test-key",
            http_client=http_client,
            max_retries=0,
        )
        try:
            stream = await client.chat.completions.create(
                model=GOOGLE_MODEL,
                messages=[{"role": "user", "content": "stream probe"}],
                stream=True,
                stream_options={"include_usage": True},
            )
            return [chunk async for chunk in stream]
        finally:
            await client.close()


@pytest.mark.anyio
async def test_engine_google_create_kwargs_are_openai_compatible(tmp_path, monkeypatch):
    import botex.engine as engine

    final_chunk = SimpleNamespace(
        id="chunk-1",
        model=GOOGLE_MODEL,
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content="STATUS: DONE\nwire captured",
                    reasoning_content=None,
                    reasoning=None,
                    tool_calls=None,
                ),
                finish_reason="stop",
                index=0,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=1,
            completion_tokens_details=None,
            prompt_tokens_details=None,
        ),
    )

    class AsyncChunks:
        def __aiter__(self):
            async def generate():
                yield final_chunk

            return generate()

    class RawResponse:
        headers: dict[str, str] = {}

        def parse(self):
            return AsyncChunks()

    class StreamingContext:
        async def __aenter__(self):
            return RawResponse()

        async def __aexit__(self, *args):
            return False

    streaming_calls: list[dict[str, Any]] = []
    factory_calls: list[dict[str, Any]] = []

    async def forbidden_plain_create(**kwargs):
        raise AssertionError(f"streaming engine path used plain create: {kwargs!r}")

    streaming_api = SimpleNamespace(
        create=lambda **kwargs: streaming_calls.append(kwargs) or StreamingContext()
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=forbidden_plain_create,
                with_streaming_response=streaming_api,
                with_raw_response=SimpleNamespace(create=forbidden_plain_create),
            )
        )
    )

    def fake_openai_factory(**kwargs):
        factory_calls.append(kwargs)
        return fake_client

    provider_cfg = _google_config_with_openrouter_contamination()
    monkeypatch.setattr(engine, "AsyncOpenAI", fake_openai_factory)
    monkeypatch.setattr(engine, "resolve_provider", lambda provider="": ("google", provider_cfg))
    monkeypatch.setattr(engine, "resolve_model_candidates", lambda *args, **kwargs: [GOOGLE_MODEL])
    monkeypatch.setattr(engine, "resolve_model", lambda *args, **kwargs: GOOGLE_MODEL)
    monkeypatch.setattr(engine, "check_model_price", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(engine, "check_model_tools", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(engine, "check_budget_limit", lambda *args, **kwargs: (False, 0.0))
    monkeypatch.setattr(engine, "log_run", lambda **kwargs: {"cost_usd": 0.0})
    monkeypatch.setattr(engine, "print_run_summary", lambda *args, **kwargs: None)

    result = await engine.run_botex_task(
        task="capture the Google wire shape",
        workspace_dir=str(tmp_path),
        model=GOOGLE_MODEL,
        api_key="unit-test-key",
        provider="google",
        mode="readonly",
        max_turns=1,
    )

    assert result["status"] == "DONE", result
    assert len(factory_calls) == 1
    assert factory_calls[0]["base_url"] == GOOGLE_BASE_URL
    assert factory_calls[0]["max_retries"] == 0

    assert len(streaming_calls) == 1, streaming_calls
    create_kwargs = streaming_calls[0]
    assert create_kwargs["model"] == GOOGLE_MODEL
    assert create_kwargs["stream"] is True
    assert create_kwargs["stream_options"] == {"include_usage": True}
    assert create_kwargs["tools"] and create_kwargs["tool_choice"] == "auto"
    assert isinstance(create_kwargs["max_tokens"], int) and create_kwargs["max_tokens"] > 0
    assert isinstance(create_kwargs["temperature"], float)
    assert "response_format" not in create_kwargs

    extra_body = create_kwargs["extra_body"]
    assert extra_body["reasoning_effort"] == "low"
    assert extra_body["google"] == {"thinking_config": {"thinking_budget": 128}}
    assert OPENROUTER_ONLY_KEYS.isdisjoint(extra_body)


@pytest.mark.anyio
async def test_openai_sdk_serializes_google_extra_body_as_top_level_fields():
    provider = GoogleAIStudioProvider("google", _google_config_with_openrouter_contamination())
    captured: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(
            200,
            content=_sse_body(_stream_events_normal()),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = provider.get_client(
            api_key="unit-test-key",
            client_factory=AsyncOpenAI,
            http_client=http_client,
        )
        try:
            extra_body = provider.get_extra_body(model=GOOGLE_MODEL)
            # Engine mirrors the official stream_options kwarg into extra_body;
            # serialize the same shape it emits.
            extra_body["stream_options"] = {"include_usage": True}
            stream = await client.chat.completions.create(
                model=GOOGLE_MODEL,
                messages=[{"role": "user", "content": "serialize this"}],
                tools=[_tool_declaration()],
                tool_choice="auto",
                max_tokens=321,
                temperature=0.2,
                stream=True,
                stream_options={"include_usage": True},
                extra_body=extra_body,
                timeout=7.0,
            )
            chunks = [chunk async for chunk in stream]
        finally:
            await client.close()

    assert client.max_retries == 0
    assert chunks, "MockTransport SSE stream did not parse"
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert request.url.path == "/v1beta/openai/chat/completions"
    assert request.headers["authorization"] == "Bearer unit-test-key"

    body = json.loads(request.content.decode())
    assert body["model"] == GOOGLE_MODEL
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["tools"] == [_tool_declaration()]
    assert body["tool_choice"] == "auto"
    assert body["max_tokens"] == 321
    assert body["temperature"] == pytest.approx(0.2)
    assert "response_format" not in body

    assert body["reasoning_effort"] == "low"
    assert body["google"] == {"thinking_config": {"thinking_budget": 128}}
    assert "extra_body" not in body
    assert "timeout" not in body
    assert OPENROUTER_ONLY_KEYS.isdisjoint(body)


def test_google_extra_body_stripping_is_scoped_to_google_adapter():
    contaminated = _google_config_with_openrouter_contamination()["extra_body"]
    google_extra = GoogleAIStudioProvider(
        "google", _google_config_with_openrouter_contamination()
    ).get_extra_body(model=GOOGLE_MODEL)

    assert google_extra == {
        "reasoning_effort": "low",
        "google": {"thinking_config": {"thinking_budget": 128}},
    }
    assert OPENROUTER_ONLY_KEYS.isdisjoint(google_extra)

    openrouter_extra = OpenRouterProvider(
        "openrouter",
        {
            "extra_body": dict(contaminated),
            "context_compression": False,
            "zdr": {"enabled": True},
        },
    ).get_extra_body(
        model="google/gemini-2.5-flash",
        has_tools=True,
        pricing_cfg={"enabled": False},
    )
    for key in OPENROUTER_ONLY_KEYS:
        assert key in openrouter_extra


@pytest.mark.parametrize(
    ("payload", "finish_reason", "raw_finish_reason", "content"),
    [
        pytest.param(
            {
                "id": "chatcmpl-valid",
                "model": GOOGLE_MODEL,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "valid answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
            "completed",
            "stop",
            "valid answer",
            id="valid",
        ),
        pytest.param(
            {"id": "chatcmpl-empty", "model": GOOGLE_MODEL, "choices": []},
            "provider_error",
            "no_choices",
            "",
            id="choices-empty",
        ),
        pytest.param(
            {"id": "chatcmpl-missing", "model": GOOGLE_MODEL},
            "provider_error",
            "no_choices",
            "",
            id="choices-missing",
        ),
        pytest.param(
            [{"error": {"code": 200, "message": "off-spec top-level list"}}],
            "provider_error",
            "no_choices",
            "",
            id="top-level-list",
        ),
    ],
)
def test_normalize_response_matrix(payload, finish_reason, raw_finish_reason, content):
    normalized = normalize_response(payload)

    assert normalized.finish_reason == finish_reason
    assert normalized.raw_finish_reason == raw_finish_reason
    assert normalized.content == content
    if finish_reason == "completed":
        assert normalized.usage.prompt_tokens == 5
        assert normalized.usage.completion_tokens == 2
        assert normalized.model == GOOGLE_MODEL


@pytest.mark.anyio
async def test_streaming_aggregation_normal_stream_with_terminal_usage_chunk():
    chunks = await _collect_sse_chunks(_stream_events_normal())
    assert len(chunks) == len(_stream_events_normal())

    normalized = normalize_streaming_chunks(chunks, raw_model=GOOGLE_MODEL)

    assert normalized.finish_reason == "completed"
    assert normalized.raw_finish_reason == "stop"
    assert normalized.content == "STATUS: DONE\nwire captured"
    assert normalized.usage.prompt_tokens == 11
    assert normalized.usage.completion_tokens == 4
    assert normalized.usage.reasoning_tokens == 1
    assert normalized.usage.cached_tokens == 3
    assert normalized.model == GOOGLE_MODEL


@pytest.mark.anyio
async def test_streaming_aggregation_empty_stream_is_provider_error():
    chunks = await _collect_sse_chunks([])
    assert chunks == []

    normalized = normalize_streaming_chunks(chunks, raw_model=GOOGLE_MODEL)

    assert normalized.finish_reason == "provider_error"
    assert normalized.raw_finish_reason == "no_choices"
    assert normalized.content == ""


@pytest.mark.anyio
async def test_streaming_aggregation_usage_only_empty_choices_stream_is_provider_error():
    chunks = await _collect_sse_chunks([_stream_event_usage_only()])
    assert len(chunks) == 1
    assert chunks[0].choices == []

    normalized = normalize_streaming_chunks(chunks, raw_model=GOOGLE_MODEL)

    assert normalized.finish_reason == "provider_error"
    assert normalized.raw_finish_reason == "no_choices"
    assert normalized.content == ""
    assert normalized.usage.prompt_tokens == 11


def _google_error(status: int, message: str, retry_delay_s: float | None = None) -> dict[str, Any]:
    status_name = {
        400: "INVALID_ARGUMENT",
        429: "RESOURCE_EXHAUSTED",
        503: "UNAVAILABLE",
    }[status]
    error: dict[str, Any] = {"code": status, "message": message, "status": status_name}
    if retry_delay_s is not None:
        error["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": f"{retry_delay_s:g}s",
            }
        ]
    return error


async def _capture_mocked_error(status: int, payload: Any) -> BaseException:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=payload, headers={"content-type": "application/json"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = AsyncOpenAI(
            base_url=GOOGLE_BASE_URL,
            api_key="unit-test-key",
            http_client=http_client,
            max_retries=0,
        )
        try:
            await client.chat.completions.create(
                model=GOOGLE_MODEL,
                messages=[{"role": "user", "content": "trigger error"}],
            )
        except Exception as exc:
            return exc
        finally:
            await client.close()
    raise AssertionError("MockTransport error response did not raise")


def _error_case(shape: str, status: int) -> tuple[str, int, Any, dict[str, Any]]:
    retry_delay = {429: 4.5, 503: 2.0}.get(status)
    message = {
        400: "Request is invalid.",
        429: "Quota exceeded. Please retry in 4.5s.",
        503: "The model is overloaded. Please retry in 2s.",
    }[status]
    error = _google_error(status, message, retry_delay)
    payload: Any = {"error": error} if shape == "dict" else [{"error": error}]
    expected_type = {
        400: BadRequestError,
        429: RateLimitError,
        503: InternalServerError,
    }[status]
    return shape, status, payload, {"error": error, "type": expected_type, "delay": retry_delay}


_ERROR_CASES = [
    pytest.param(*_error_case("dict", 429), id="dict-429-control"),
    pytest.param(*_error_case("list", 429), id="list-429"),
    pytest.param(*_error_case("list", 400), id="list-400"),
    pytest.param(*_error_case("list", 503), id="list-503-overload"),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("shape", "status", "payload", "expected"), _ERROR_CASES)
async def test_google_shim_error_operational_classification(shape, status, payload, expected):
    exc = await _capture_mocked_error(status, payload)
    provider = GoogleAIStudioProvider("google", {})

    assert type(exc) is expected["type"]
    assert exc.status_code == status
    assert provider.is_retryable(exc) is (status in (429, 503))
    if expected["delay"] is None:
        assert parse_retry_delay(exc) is None
    else:
        assert parse_retry_delay(exc) == pytest.approx(expected["delay"])
    expected_kind = {
        400: "provider_error",
        429: "provider_rate_limited",
        503: "provider_unavailable",
    }[status]
    assert classify_api_error(exc) == expected_kind


@pytest.mark.anyio
@pytest.mark.parametrize(("shape", "status", "payload", "expected"), _ERROR_CASES)
async def test_google_shim_error_body_and_string_are_openai_compatible(
    shape, status, payload, expected
):
    exc = normalize_api_error_shape(await _capture_mocked_error(status, payload))
    expected_error = expected["error"]
    expected_text_prefix = f"Error code: {status} - "

    failures = []
    if exc.body != expected_error:
        failures.append(f"body={exc.body!r}")
    if not str(exc).startswith(expected_text_prefix):
        failures.append(f"str={str(exc)!r}")
    if expected_error["message"] not in str(exc):
        failures.append(f"message missing from str={str(exc)!r}")
    assert not failures, "; ".join(failures)
