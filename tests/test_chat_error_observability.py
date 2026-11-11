# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Chat provider-error observability tests (P1/P2 of the 403-incident remediation).

Guards the server-side taint-safe error logging (CWE-532: only static
allowlisted error codes reach the log sink — never the raw exception text)
and the 401/403 policy-rejection classification.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from botex.server.chat import _provider_error_payload


class _FakeProviderError(Exception):
    """Mimics an OpenAI SDK APIStatusError: status_code + structured JSON body."""

    def __init__(self, status_code: int, body: Any = "boom") -> None:
        super().__init__(f"Error code: {status_code} - {body}")
        self.status_code = status_code
        self.body = body


def _session() -> Any:
    return SimpleNamespace(session_id="sess-123")


def test_policy_rejection_403_classification(caplog: Any) -> None:
    exc = _FakeProviderError(
        403, {"error": {"code": "moderation_blocked", "message": "flagged by moderation"}}
    )
    with caplog.at_level("WARNING", logger="botex.server.chat"):
        payload = _provider_error_payload(
            _session(),
            "openrouter",
            "google/gemma-4-31b-it:free",
            exc,
            phase="request",
        )
    assert payload["error_class"] == "policy_rejection"
    assert payload["type"] == "error"
    assert payload["session_id"] == "sess-123"
    assert "Not retrying" in payload["error"]
    assert "moderation_blocked" in payload["error"]  # sanitized body retained for the operator
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert any("chat provider error" in m and "status=403" in m for m in joined.splitlines()), (
        "server-side provider error log line missing"
    )
    # Taint-safe logging: the allowlisted static code IS logged...
    assert "code=moderation_blocked" in joined
    # ...but the raw exception text never reaches the log sink (CWE-532).
    assert "Error code: 403" not in joined
    assert "flagged by moderation" not in joined


def test_unclassified_provider_code_is_generic_in_log(caplog: Any) -> None:
    exc = _FakeProviderError(
        400, {"error": {"code": "weird_new_code", "message": "something novel"}}
    )
    with caplog.at_level("WARNING", logger="botex.server.chat"):
        payload = _provider_error_payload(_session(), "openrouter", "m", exc, phase="request")
    assert payload["error_class"] == "provider_error"
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "code=unclassified_provider_error" in joined
    assert "weird_new_code" not in joined  # unknown strings never echoed to logs


def test_policy_rejection_401_classification() -> None:
    payload = _provider_error_payload(
        _session(),
        "openrouter",
        "m",
        _FakeProviderError(401, {"error": {"code": "invalid_key"}}),
        phase="request",
    )
    assert payload["error_class"] == "auth_failure"
    assert "401" in payload["error"]
    assert "API key" in payload["error"]


def test_transient_provider_error_stays_generic() -> None:
    payload = _provider_error_payload(
        _session(),
        "openrouter",
        "m",
        _FakeProviderError(500, {"error": {"message": "upstream oops"}}),
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
            _FakeProviderError(429, {"error": {"code": "rate_limit_exceeded"}}),
            phase="tool_retry",
        )
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "provider=nvidia" in joined
    assert "model=nvidia/nemotron-3-ultra" in joined
    assert "status=429" in joined
    assert "phase=tool_retry" in joined
    assert "code=rate_limit_exceeded" in joined


def test_client_init_phase_preserves_prefix() -> None:
    payload = _provider_error_payload(
        _session(),
        "ollama",
        "m",
        RuntimeError("bad base_url"),
        phase="client_init",
        message_prefix="Client Init Error: ",
    )
    assert payload["error_class"] == "provider_error"
    assert payload["error"].startswith("Client Init Error: ")
