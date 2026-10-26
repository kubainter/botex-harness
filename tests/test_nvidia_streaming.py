# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Unit tests for NVIDIA streaming chunk accumulation, reasoning, and recommend_models.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from botex.benchmarks import get_recommended_models
from botex.providers import (
    NvidiaProvider,
    normalize_streaming_chunks,
)


class DummyChoiceDelta:
    def __init__(self, content=None, reasoning_content=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class DummyChoice:
    def __init__(self, delta=None, finish_reason=None, index=0):
        self.delta = delta
        self.finish_reason = finish_reason
        self.index = index


class DummyUsage:
    def __init__(self, prompt_tokens=10, completion_tokens=25, total_tokens=35):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class DummyChunk:
    def __init__(self, choices=None, usage=None, id="chunk-1", model="nvidia/nemotron-test"):
        self.choices = choices or []
        self.usage = usage
        self.id = id
        self.model = model


def test_normalize_streaming_chunks_content_and_reasoning():
    chunks = [
        DummyChunk(
            choices=[
                DummyChoice(
                    delta=DummyChoiceDelta(reasoning_content="Let me think about "),
                    finish_reason=None,
                )
            ]
        ),
        DummyChunk(
            choices=[
                DummyChoice(
                    delta=DummyChoiceDelta(reasoning_content="the answer."),
                    finish_reason=None,
                )
            ]
        ),
        DummyChunk(
            choices=[
                DummyChoice(
                    delta=DummyChoiceDelta(content="The answer is 42."),
                    finish_reason="stop",
                )
            ],
            usage=DummyUsage(prompt_tokens=15, completion_tokens=30, total_tokens=45),
        ),
    ]

    resp = normalize_streaming_chunks(chunks, raw_model="nvidia/nemotron-test")
    assert resp.content == "The answer is 42."
    assert resp.usage.reasoning_tokens > 0
    assert resp.finish_reason == "completed"
    assert resp.usage.prompt_tokens == 15
    assert resp.usage.completion_tokens == 30


def test_normalize_streaming_chunks_reasoning_only():
    # When content is empty but model produced reasoning
    chunks = [
        DummyChunk(
            choices=[
                DummyChoice(
                    delta=DummyChoiceDelta(reasoning_content="Thinking process only."),
                    finish_reason="stop",
                )
            ]
        )
    ]
    resp = normalize_streaming_chunks(chunks)
    assert resp.content == "Thinking process only."
    assert resp.finish_reason == "completed"


def test_normalize_streaming_chunks_tool_calls():
    # Simulate fragmented tool call streaming
    tc_delta_1 = MagicMock()
    tc_delta_1.index = 0
    tc_delta_1.id = "call_abc123"
    tc_delta_1.type = "function"
    tc_delta_1.function.name = "read_file"
    tc_delta_1.function.arguments = '{"path": '

    tc_delta_2 = MagicMock()
    tc_delta_2.index = 0
    tc_delta_2.id = None
    tc_delta_2.type = None
    tc_delta_2.function.name = None
    tc_delta_2.function.arguments = '"test.py"}'

    chunks = [
        DummyChunk(choices=[DummyChoice(delta=DummyChoiceDelta(tool_calls=[tc_delta_1]))]),
        DummyChunk(
            choices=[
                DummyChoice(
                    delta=DummyChoiceDelta(tool_calls=[tc_delta_2]),
                    finish_reason="tool_calls",
                )
            ]
        ),
    ]

    resp = normalize_streaming_chunks(chunks)
    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert call.id == "call_abc123"
    assert call.name == "read_file"
    assert json.loads(call.arguments) == {"path": "test.py"}
    assert resp.finish_reason == "tool_call"


def test_nvidia_provider_thinking_in_extra_body():
    provider = NvidiaProvider()
    payload = provider.prepare_request_payload(
        model="nvidia/nemotron-3.5-super-vl-120b-a12b",
        messages=[{"role": "user", "content": "Hello"}],
    )
    assert "extra_body" in payload
    assert payload["extra_body"].get("chat_template_kwargs", {}).get("enable_thinking") is True
    assert payload.get("top_p") == 0.95
    assert payload.get("temperature") == 1.0


def test_benchmarks_nvidia_recommendation():
    # Mocking urllib.request.urlopen returning models payload for NVIDIA NIM
    mock_models_response = {
        "data": [
            {
                "id": "nvidia/nemotron-3.5-super-vl-120b-a12b",
                "object": "model",
                "created": 1700000000,
                "owned_by": "nvidia",
            },
            {
                "id": "meta/llama-3.1-70b-instruct",
                "object": "model",
                "created": 1700000000,
                "owned_by": "nvidia",
            },
            {
                "id": "nvidia/riva-translate-4b-instruct-v2",
                "object": "model",
                "created": 1700000000,
                "owned_by": "nvidia",
            },
        ]
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_models_response).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        recs = get_recommended_models(
            task_type="coding",
            provider="nvidia",
            require_tools=True,
            limit=5,
        )

    assert len(recs) >= 1
    # Check that nemotron was inferred with 131072 context length and tools capability
    nemotron = next((r for r in recs if "nemotron" in r["id"]), None)
    assert nemotron is not None
    assert nemotron["context_length"] == 131072
    assert "tools" in nemotron["capabilities"]
    assert nemotron["pricing"]["prompt_1M"] == 0.0
