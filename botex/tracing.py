# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Internal OpenTelemetry tracing layer for the BoteX harness.

Provides lightweight, secret-safe span creation for BoteX operations
(MCP tools, engine loops, provider requests, rate limiter waits).
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import Any, Dict, Iterator, Optional

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:
    trace = None  # type: ignore
    Status = None  # type: ignore
    StatusCode = None  # type: ignore
    _OTEL_AVAILABLE = False

try:
    from .security import mask_secrets
except (ImportError, ValueError):
    from security import mask_secrets


_TRACER_NAME = "botex.harness"


def get_tracer():
    if _OTEL_AVAILABLE and trace is not None:
        return trace.get_tracer(_TRACER_NAME)
    return None


def get_current_trace_id() -> Optional[str]:
    """Return the current W3C trace_id hex string if an active span exists."""
    if not _OTEL_AVAILABLE or trace is None:
        return None
    span = trace.get_current_span()
    if span is None:
        return None
    ctx = span.get_span_context()
    if not ctx or not ctx.is_valid:
        return None
    return f"{ctx.trace_id:032x}"


# Span attribute values are capped so an oversized string cannot push a
# whole document into a tracing backend — ids, slugs and counts only.
_MAX_ATTR_VALUE_LEN = 256


def _sanitize_attributes(attrs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not attrs:
        return {}
    clean = {}
    for k, v in attrs.items():
        # OTel rejects None attribute values — drop them instead of
        # stringifying into a "None" taint-carrying value.
        if v is None:
            continue
        if isinstance(v, str):
            clean[k] = mask_secrets(v)[:_MAX_ATTR_VALUE_LEN]
        elif isinstance(v, (int, float, bool)):
            clean[k] = v
        elif isinstance(v, (list, tuple)):
            clean[k] = [
                mask_secrets(x)[:_MAX_ATTR_VALUE_LEN] if isinstance(x, str) else x for x in v[:20]
            ]
        else:
            clean[k] = mask_secrets(str(v))[:_MAX_ATTR_VALUE_LEN]
    return clean


class _NullSpanContext:
    """Stand-in for an invalid OTel SpanContext (trace_id/span_id = 0)."""

    trace_id = 0
    span_id = 0
    is_valid = False
    is_remote = False
    trace_flags = 0
    trace_state: Dict[str, str] = {}


class NullSpan:
    """Fallback no-op span when OpenTelemetry is unavailable.

    Mirrors the OTel Span API surface used by BoteX call sites so they
    never branch on SDK availability (parity with NonRecordingSpan).
    """

    def __init__(self, name: str, attributes: Optional[Dict[str, Any]] = None):
        self.name = name
        self.attributes = attributes or {}
        self.start_time = time.time()
        self.end_time = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: Any, description: Optional[str] = None) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        pass

    def update_name(self, name: str) -> None:
        self.name = name

    def is_recording(self) -> bool:
        return False

    def get_span_context(self) -> "_NullSpanContext":
        return _NullSpanContext()

    def end(self) -> None:
        self.end_time = time.time()


@contextlib.contextmanager
def start_span(
    name: str,
    attributes: Optional[Dict[str, Any]] = None,
) -> Iterator[Any]:
    """Start an OpenTelemetry span, or a safe NullSpan fallback."""
    clean_attrs = _sanitize_attributes(attributes)
    tracer = get_tracer()
    if tracer is not None:
        with tracer.start_as_current_span(name, attributes=clean_attrs) as span:
            yield span
    else:
        span = NullSpan(name, clean_attrs)
        try:
            yield span
        finally:
            span.end()


# Exporter kinds resolve through a module-constant allowlist — a malformed
# config value must never smuggle an arbitrary string into SDK wiring
# (CodeQL CWE-020 invariant for normalized parameters).
_VALID_EXPORTER_KINDS: Dict[str, str] = {
    "otlp": "otlp",
    "console": "console",
    "none": "none",
}

_DEFAULT_TRACING_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "exporter": "none",
    "endpoint": "",
    "sample_ratio": 1.0,
}

_TRACING_INITIALIZED = False


def resolve_tracing_config(raw: Any) -> Dict[str, Any]:
    """Merge a ``tracing`` config block over the defaults.

    ``raw`` may be a dict or a bare bool (``tracing: false``). The
    exporter name resolves through a module-constant allowlist; the
    sample ratio clamps into [0.0, 1.0].
    """
    cfg = dict(_DEFAULT_TRACING_CONFIG)
    if isinstance(raw, dict):
        for key in cfg:
            if key in raw and raw[key] is not None:
                cfg[key] = raw[key]
    elif raw is not None:
        cfg["enabled"] = bool(raw)
    try:
        ratio = float(cfg["sample_ratio"])
    except (TypeError, ValueError):
        ratio = 1.0
    return {
        "enabled": bool(cfg["enabled"]),
        "exporter": _VALID_EXPORTER_KINDS.get(str(cfg["exporter"]).strip().lower(), "none"),
        "endpoint": str(cfg["endpoint"] or "").strip(),
        "sample_ratio": min(max(ratio, 0.0), 1.0),
    }


def init_tracing(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Bootstrap the OpenTelemetry SDK TracerProvider from config.

    ``cfg`` is the merged app config (its ``tracing`` section carries
    ``{enabled, exporter, endpoint, sample_ratio}``); None loads it.
    Returns True when a real SDK provider was installed. Stays a safe
    no-op (False) when disabled, when ``OTEL_SDK_DISABLED`` is set, when
    the SDK packages are absent, or on any setup failure — the NullSpan
    path keeps everything working. Idempotent: later calls reuse the
    provider installed by the first.
    """
    global _TRACING_INITIALIZED
    if _TRACING_INITIALIZED:
        return True
    if not _OTEL_AVAILABLE or trace is None:
        return False
    if str(os.environ.get("OTEL_SDK_DISABLED", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    if cfg is None:
        try:
            from .config import load_config

            cfg = load_config()
        except Exception:
            try:
                from config import load_config

                cfg = load_config()
            except Exception:
                cfg = {}
    tcfg = resolve_tracing_config((cfg or {}).get("tracing"))
    if not tcfg["enabled"] or tcfg["exporter"] == "none":
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    except ImportError:
        return False
    try:
        if tcfg["exporter"] == "otlp":
            endpoint = (
                tcfg["endpoint"] or str(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")).strip()
            )
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter_obj = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        else:
            exporter_obj = ConsoleSpanExporter()
        provider = TracerProvider(
            resource=Resource.create({"service.name": _TRACER_NAME}),
            sampler=TraceIdRatioBased(tcfg["sample_ratio"]),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter_obj))
        trace.set_tracer_provider(provider)
        _TRACING_INITIALIZED = True
        return True
    except Exception:
        return False
