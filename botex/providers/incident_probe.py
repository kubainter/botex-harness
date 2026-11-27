# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Provider Incident Probe — Redacted Wire-Shape Records
============================================================
Schema-versioned, secret-free incident records for provider control-plane
operations (catalog discovery) and data-plane probes. Records capture only
wire-shape metadata — method, URL template, query keys, header names,
payload shape, status, content type, top-level response keys, bounded
masked error fields, counts, and durations. Header values, query values,
userinfo, account ids, prompt/patch content, raw response bodies, and
exception free-text are never persisted (CodeQL CWE-312 / CWE-020).
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from botex.tools.security import sanitize_error_text
except (ImportError, ValueError):
    from security import sanitize_error_text

SCHEMA_VERSION: int = 1
DEFAULT_INCIDENT_PATH: Path = Path(".botex/provider_incidents.jsonl")

# Canonical operation names — a static literal map keeps dynamic values out of
# persisted records (CodeQL CWE-020): only these literals may leave the module.
VALID_OPERATIONS: Dict[str, str] = {
    "catalog_models_openai": "catalog_models_openai",
    "catalog_models_native": "catalog_models_native",
    "chat_completion": "chat_completion",
    "catalog_skipped_unconfigured": "catalog_skipped_unconfigured",
    "catalog_discovery_unsupported": "catalog_discovery_unsupported",
}

_VALID_METHODS: Dict[str, str] = {
    "GET": "GET",
    "POST": "POST",
    "PUT": "PUT",
    "PATCH": "PATCH",
    "DELETE": "DELETE",
    "HEAD": "HEAD",
    "OPTIONS": "OPTIONS",
}

_AUTH_HEADER_NAMES = frozenset({"authorization", "x-api-key", "api-key", "x-goog-api-key"})
_REQUEST_ID_HEADERS = ("x-request-id", "cf-ray", "request-id", "openai-request-id")
_ACCOUNT_SEGMENT_RE = re.compile(r"(?i)(accounts[/\\])[^/\s'\")\]&?]+")
_ERROR_FIELD_MAX = 160

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_INCIDENT_PATH",
    "VALID_OPERATIONS",
    "normalize_operation",
    "redact_url_template",
    "redact_headers",
    "summarize_payload",
    "summarize_response",
    "build_incident_record",
    "append_incident_record",
    "emit_incident_safe",
]


def normalize_operation(raw: str) -> str:
    """Canonicalize an operation name through the static literal map."""
    return VALID_OPERATIONS.get(str(raw or "").strip().lower(), "unknown")


def redact_url_template(url: str) -> Tuple[str, List[str]]:
    """Return ``(url_template, query_keys)`` with all secret surfaces removed.

    Userinfo, query values, and fragments are stripped entirely; the path
    segment after ``/accounts/`` is replaced with ``<account_id>``. Only
    query *key names* survive, never their values.
    """
    raw = str(url or "")
    try:
        parts = urllib.parse.urlsplit(raw)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        segments = parts.path.split("/")
        for i, segment in enumerate(segments):
            if segment.lower() == "accounts" and i + 1 < len(segments):
                segments[i + 1] = "<account_id>"
        path = "/".join(segments)
        keys = {str(k) for k, _v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)}
        return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", "")), sorted(keys)
    except Exception:
        # Last-resort fallback: drop query/fragment wholesale so a malformed
        # URL can never smuggle secret-bearing query values into the record.
        base = raw.split("#", 1)[0].split("?", 1)[0].rsplit("@", 1)[-1]
        return _ACCOUNT_SEGMENT_RE.sub(r"\1<account_id>", base), []


def redact_headers(headers: Optional[Mapping[str, Any]]) -> Tuple[List[str], bool]:
    """Return ``(sorted_lowercase_names, auth_present)`` — never header values."""
    if not isinstance(headers, Mapping):
        return [], False
    names = sorted({str(k).strip().lower() for k in headers.keys()})
    auth_present = any(
        str(k).strip().lower() in _AUTH_HEADER_NAMES and v is not None and str(v).strip() != ""
        for k, v in headers.items()
    )
    return names, auth_present


def summarize_payload(payload: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Reduce a request payload to its wire shape — keys and counts only."""
    if not isinstance(payload, Mapping):
        return {"stream": False, "tool_count": 0, "payload_keys": []}
    tools = payload.get("tools")
    return {
        "stream": bool(payload.get("stream")),
        "tool_count": len(tools) if isinstance(tools, (list, tuple)) else 0,
        "payload_keys": sorted(str(k) for k in payload.keys()),
    }


def _nonempty(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _bounded_error_field(value: Any, max_chars: int) -> Optional[str]:
    if value is None or isinstance(value, (Mapping, list, tuple)):
        return None
    text = _ACCOUNT_SEGMENT_RE.sub(r"\1<account_id>", str(value))
    cleaned = sanitize_error_text(text, max_len=max_chars)
    return cleaned or None


def _error_source(body: Any) -> Optional[Mapping[str, Any]]:
    """Locate the provider error object across OpenAI/Cloudflare body shapes."""
    if not isinstance(body, Mapping):
        return None
    err = body.get("error")
    if isinstance(err, Mapping):
        return err
    if isinstance(err, str):
        return {"message": err}
    errors = body.get("errors")
    if isinstance(errors, list):
        first = next((e for e in errors if isinstance(e, Mapping)), None)
        if first is not None:
            return first
    if any(isinstance(body.get(k), (str, int, float)) for k in ("message", "code", "type")):
        return {
            "message": body.get("message"),
            "code": body.get("code"),
            "type": body.get("type"),
        }
    return None


def summarize_response(
    status: Optional[int],
    headers: Optional[Mapping[str, Any]],
    body: Any,
    *,
    max_error_chars: int = 256,
) -> Dict[str, Any]:
    """Reduce a response to status/content-type/top-level keys, bounded masked
    error fields, counts, and request-id presence — never raw bodies or IDs."""
    hdr_map: Dict[str, Any] = {}
    if isinstance(headers, Mapping):
        for k, v in headers.items():
            hdr_map[str(k).strip().lower()] = v

    content_type: Optional[str] = None
    raw_ct = hdr_map.get("content-type")
    if raw_ct is not None:
        content_type = str(raw_ct).split(";", 1)[0].strip().lower() or None

    err = _error_source(body)
    try:
        status_val = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_val = None

    result_count: Optional[int] = None
    choices_count: Optional[int] = None
    request_id_present = any(_nonempty(hdr_map.get(h)) for h in _REQUEST_ID_HEADERS)
    if isinstance(body, Mapping):
        if isinstance(body.get("choices"), list):
            choices_count = len(body["choices"])
        for key in ("result", "data"):
            if isinstance(body.get(key), list):
                result_count = len(body[key])
                break
        if not request_id_present:
            request_id_present = _nonempty(body.get("request_id"))

    return {
        "status": status_val,
        "content_type": content_type,
        "top_level_keys": sorted(str(k) for k in body.keys()) if isinstance(body, Mapping) else [],
        "error_code": _bounded_error_field(err.get("code") if err else None, _ERROR_FIELD_MAX),
        "error_type": _bounded_error_field(err.get("type") if err else None, _ERROR_FIELD_MAX),
        "error_message": _bounded_error_field(err.get("message") if err else None, max_error_chars),
        "choices_count": choices_count,
        "result_count": result_count,
        "request_id_present": request_id_present,
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_duration_ms(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def build_incident_record(
    *,
    provider: str,
    operation: str,
    model: Optional[str] = None,
    method: str = "GET",
    url: str = "",
    headers: Optional[Mapping[str, Any]] = None,
    payload: Optional[Mapping[str, Any]] = None,
    response_status: Optional[int] = None,
    response_headers: Optional[Mapping[str, Any]] = None,
    response_body: Any = None,
    duration_ms: int = 0,
    exception: Optional[BaseException] = None,
    ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one schema-versioned, fully redacted incident record.

    When ``exception`` carries a ``.response`` (httpx-style status errors),
    status/headers/body are extracted from it unless passed explicitly — the
    exception's free-text (which embeds the request URL) is never persisted.
    """
    exc_response = getattr(exception, "response", None) if exception is not None else None
    if exc_response is not None:
        if response_status is None:
            response_status = getattr(exc_response, "status_code", None)
        if response_headers is None:
            response_headers = getattr(exc_response, "headers", None)
        if response_body is None:
            try:
                response_body = exc_response.json()
            except Exception:
                response_body = None

    url_template, query_keys = redact_url_template(url)
    header_names, auth_present = redact_headers(headers)
    payload_summary = summarize_payload(payload)

    return {
        "schema_version": SCHEMA_VERSION,
        "ts": str(ts) if ts else _utc_now_iso(),
        "provider": str(provider or ""),
        "operation": normalize_operation(operation),
        "model": str(model) if model is not None else None,
        "request": {
            "method": _VALID_METHODS.get(str(method).strip().upper(), "GET"),
            "url_template": url_template,
            "query_keys": query_keys,
            "header_names": header_names,
            "auth_present": auth_present,
            "stream": payload_summary["stream"],
            "tool_count": payload_summary["tool_count"],
            "payload_keys": payload_summary["payload_keys"],
        },
        "response": summarize_response(response_status, response_headers, response_body),
        "duration_ms": _safe_duration_ms(duration_ms),
        "exception_type": type(exception).__name__ if exception is not None else None,
    }


def append_incident_record(record: Mapping[str, Any], path: Optional[Path | str] = None) -> Path:
    """Append one UTF-8 JSON line to the incident log, creating parents."""
    target = Path(path) if path is not None else DEFAULT_INCIDENT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(record), ensure_ascii=False, default=str)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return target


def emit_incident_safe(
    sink: Optional[Callable[[Dict[str, Any]], None]], record: Dict[str, Any]
) -> None:
    """Dispatch a record to ``sink`` — sink errors are swallowed so
    observability can never break catalog sync or provider calls."""
    if not callable(sink):
        return
    try:
        sink(record)
    except Exception:
        pass
