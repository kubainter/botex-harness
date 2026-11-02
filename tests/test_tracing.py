# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for botex/tracing.py."""

from botex.tracing import (
    NullSpan,
    _sanitize_attributes,
    get_current_trace_id,
    init_tracing,
    resolve_tracing_config,
    start_span,
)


def test_sanitize_attributes_masks_secrets():
    attrs = {
        "user_key": "sk-or-v1-abcdef1234567890abcdef1234567890",
        "task_name": "Refactor auth",
        "step_count": 5,
        "is_ok": True,
    }
    clean = _sanitize_attributes(attrs)
    assert clean["step_count"] == 5
    assert clean["is_ok"] is True
    assert "abcdef" not in clean["user_key"]


def test_sanitize_attributes_drops_none_and_caps():
    attrs = {
        "none_value": None,
        "long_string": "word " * 80,
        "big_list": [f"item{i}" for i in range(40)],
        "mixed_list": ["a", 1, None],
        "obj": object(),
    }
    clean = _sanitize_attributes(attrs)
    # OTel rejects None attribute values — the key is dropped entirely.
    assert "none_value" not in clean
    assert len(clean["long_string"]) == 256
    assert len(clean["big_list"]) == 20
    assert clean["mixed_list"] == ["a", 1, None]
    assert isinstance(clean["obj"], str)


def test_sanitize_attributes_empty_and_none():
    assert _sanitize_attributes(None) == {}
    assert _sanitize_attributes({}) == {}


def test_start_span_context_manager():
    with start_span("botex.test_span", {"model": "z-ai/glm-5.3-flash"}) as span:
        assert span is not None
        if hasattr(span, "set_attribute"):
            span.set_attribute("status", "DONE")


def test_null_span_api_parity():
    """NullSpan mirrors the OTel Span surface BoteX call sites use."""
    span = NullSpan("botex.test", {"k": "v"})
    span.set_attribute("x", 1)
    assert span.attributes["x"] == 1
    span.set_status("OK")
    span.record_exception(ValueError("boom"))
    span.add_event("checkpoint", {"a": 1})
    span.update_name("botex.renamed")
    assert span.name == "botex.renamed"
    assert span.is_recording() is False
    ctx = span.get_span_context()
    assert ctx.is_valid is False
    assert ctx.trace_id == 0
    assert span.end_time is None
    span.end()
    assert span.end_time is not None


def test_null_span_ends_via_context_manager():
    with start_span("botex.null_ctx") as span:
        pass
    if isinstance(span, NullSpan):
        assert span.end_time is not None


def test_resolve_tracing_config_defaults():
    cfg = resolve_tracing_config(None)
    assert cfg == {
        "enabled": False,
        "exporter": "none",
        "endpoint": "",
        "sample_ratio": 1.0,
    }


def test_resolve_tracing_config_allowlist_and_clamps():
    cfg = resolve_tracing_config(
        {
            "enabled": True,
            "exporter": "OTLP",
            "endpoint": " http://collector:4318 ",
            "sample_ratio": 4.5,
        }
    )
    assert cfg["enabled"] is True
    assert cfg["exporter"] == "otlp"
    assert cfg["endpoint"] == "http://collector:4318"
    assert cfg["sample_ratio"] == 1.0
    # A malformed exporter never reaches SDK wiring — it resolves to "none".
    assert resolve_tracing_config({"exporter": "gRPC-evil"})["exporter"] == "none"
    # Bare-bool form and negative ratio clamp.
    assert resolve_tracing_config(False)["enabled"] is False
    assert resolve_tracing_config({"sample_ratio": -1.0})["sample_ratio"] == 0.0


def test_init_tracing_disabled_returns_false():
    assert init_tracing({"tracing": {"enabled": False}}) is False
    assert init_tracing({"tracing": {"enabled": True, "exporter": "none"}}) is False


def test_init_tracing_respects_otel_sdk_disabled(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "1")
    assert init_tracing({"tracing": {"enabled": True, "exporter": "console"}}) is False


def test_emit_stamps_trace_id():
    """TaskEventEmitter stamps a correlation id — honest null, never fabricated."""
    from botex.events import TaskEventEmitter

    em = TaskEventEmitter("task-trace-test")
    em.emit("test_kind", payload="p")
    ev = em.tail(1)[0]
    assert "trace_id" in ev["data"]
    # No SDK span is active in unit tests — the stamp is a real None.
    assert ev["data"]["trace_id"] == get_current_trace_id()

    em.emit("test_kind2", trace_id="preset-trace")
    assert em.tail(1)[0]["data"]["trace_id"] == "preset-trace"
