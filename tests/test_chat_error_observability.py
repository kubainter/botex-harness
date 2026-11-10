# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Chat provider-error observability tests (P1/P2 of the 403-incident remediation).

Guards the server-side sanitized error logging and the 401/403 policy-rejection
classification — the incident diagnostic gap: provider failures used to reach
only the client event stream, leaving server artifacts empty.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from botex.server.chat import _provider_error_payload


class _FakeProviderError(Exception):
    def __init__(self, status_code: int, body: str = "boom") -> None:
        super().__init__(f"Error code: {status_code} - {body}")
        self.status_code = status_code
        self.body = body


def _session() -> Any:
    return SimpleNamespace(session_id="sess-123")


def test_policy_rejection_403_classification(caplog: Any) -> None:
    with caplog.at_level("WARNING", logger="botex.server.chat"):
        payload = _provider_error_payload(
            _session(),
            "openrouter",
            "google/gemma-4-31b-it:free",
            _FakeProviderError(403, "moderation_blocked"),
            phase="request",
        )
    assert payload["error_class"] == "policy_rejection"
    assert payload["type"] == "error"
    assert payload["session_id"] == "sess-123"
    assert "Not retrying" in payload["error"]
    assert "moderation_blocked" in payload["error"]  # sanitized body retained for the operator
    assert any(
        "chat provider error" in rec.message and "status=403" in rec.message
        for rec in caplog.records
    ), "server-side provider error log line missing"
    assert any("sess-123" in rec.getMessage() for rec in caplog.records)


def test_policy_rejection_401_classification() -> None:
    payload = _provider_error_payload(
        _session(),
        "openrouter",
        "m",
        _FakeProviderError(401, "No auth credentials"),
        phase="request",
    )
    assert payload["error_class"] == "policy_rejection"
    assert "401" in payload["error"]
    assert "API key" in payload["error"]


def test_transient_provider_error_stays_generic() -> None:
    payload = _provider_error_payload(
        _session(),
        "openrouter",
        "m",
        _FakeProviderError(500, "upstream oops"),
        phase="stream",
    )
    assert payload["error_class"] == "provider_error"
    assert payload["error"].startswith("API Error:")
    assert "upstream oops" in payload["error"]


def test_exception_without_status_is_generic() -> None:
    payload = _provider_error_payload(
        _session(),
        "ollama",
        "m",
        RuntimeError("connection refused"),
        phase="request",
    )
    assert payload["error_class"] == "provider_error"
    assert payload["error"].startswith("API Error:")


def test_log_line_carries_session_model_status(caplog: Any) -> None:
    with caplog.at_level("WARNING", logger="botex.server.chat"):
        _provider_error_payload(
            _session(),
            "nvidia",
            "nvidia/nemotron-3-ultra",
            _FakeProviderError(429, "rl"),
            phase="tool_retry",
        )
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "provider=nvidia" in joined
    assert "model=nvidia/nemotron-3-ultra" in joined
    assert "status=429" in joined
    assert "phase=tool_retry" in joined
