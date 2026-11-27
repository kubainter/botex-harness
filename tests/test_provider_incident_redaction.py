# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Canary-secret redaction & schema tests for ``botex.providers.incident_probe``.

Offline only — canary strings stand in for API keys, Cloudflare account ids and
tokens placed in every position a secret could leak through. The artifacts
(record dict, JSONL file, URL template) must never contain them.
"""

from __future__ import annotations

import json

import httpx2

from botex.providers.incident_probe import (
    DEFAULT_INCIDENT_PATH,
    SCHEMA_VERSION,
    VALID_OPERATIONS,
    append_incident_record,
    build_incident_record,
    emit_incident_safe,
    normalize_operation,
    redact_headers,
    redact_url_template,
    summarize_payload,
    summarize_response,
)

CANARY_API_KEY = "CANARY_API_KEY_9988"
CANARY_CF_ACCOUNT = "CANARY_CF_ACCOUNT_ABCDEF123456"
CANARY_TOKEN = "CANARY_TOKEN_XYZ777"
CANARIES = (CANARY_API_KEY, CANARY_CF_ACCOUNT, CANARY_TOKEN)


def _assert_no_canary(blob: str) -> None:
    for canary in CANARIES:
        assert canary not in blob


# ---------------------------------------------------------------------------
# Constants / enum canonicalization
# ---------------------------------------------------------------------------


def test_schema_constants():
    assert SCHEMA_VERSION == 1
    assert str(DEFAULT_INCIDENT_PATH).replace("\\", "/").endswith(".botex/provider_incidents.jsonl")
    assert VALID_OPERATIONS == {
        "catalog_models_openai": "catalog_models_openai",
        "catalog_models_native": "catalog_models_native",
        "chat_completion": "chat_completion",
        "catalog_skipped_unconfigured": "catalog_skipped_unconfigured",
        "catalog_discovery_unsupported": "catalog_discovery_unsupported",
    }


def test_normalize_operation_canonicalizes():
    assert normalize_operation("catalog_models_openai") == "catalog_models_openai"
    assert normalize_operation("  Chat_Completion \n") == "chat_completion"
    assert normalize_operation("CATALOG_MODELS_NATIVE") == "catalog_models_native"
    for bogus in ("", "post", "models", "DELETE /*", "unknown"):
        assert normalize_operation(bogus) == "unknown"


# ---------------------------------------------------------------------------
# URL template redaction
# ---------------------------------------------------------------------------


def test_url_template_strips_userinfo_query_fragment_and_account():
    url = (
        f"https://user:{CANARY_TOKEN}@api.cloudflare.com/client/v4/accounts/"
        f"{CANARY_CF_ACCOUNT}/ai/v1/models?key={CANARY_API_KEY}&foo=bar#frag"
    )
    template, keys = redact_url_template(url)
    assert template == "https://api.cloudflare.com/client/v4/accounts/<account_id>/ai/v1/models"
    assert keys == ["foo", "key"]
    _assert_no_canary(template + json.dumps(keys))


def test_url_template_query_values_never_survive():
    template, keys = redact_url_template(
        f"https://example.test/v1/models?api_key={CANARY_API_KEY}&token={CANARY_TOKEN}&empty="
    )
    assert template == "https://example.test/v1/models"
    assert keys == ["api_key", "empty", "token"]
    _assert_no_canary(template + json.dumps(keys))


def test_url_template_empty_and_relative():
    assert redact_url_template("") == ("", [])
    template, keys = redact_url_template("/v1/models?x=1&x=2")
    assert template == "/v1/models"
    assert keys == ["x"]


# ---------------------------------------------------------------------------
# Header / payload / response summaries
# ---------------------------------------------------------------------------


def test_redact_headers_names_and_auth_presence_only():
    names, auth = redact_headers(
        {
            "Authorization": f"Bearer {CANARY_API_KEY}",
            "X-Api-Key": "",
            "Accept": "application/json",
        }
    )
    assert names == ["accept", "authorization", "x-api-key"]
    assert auth is True
    _assert_no_canary(json.dumps(names))
    # An empty auth header value does not count as present.
    assert redact_headers({"Authorization": ""}) == (["authorization"], False)
    assert redact_headers(None) == ([], False)
    assert redact_headers("not-a-mapping") == ([], False)
    for name in ("x-goog-api-key", "api-key", "x-api-key"):
        assert redact_headers({name: "v"})[1] is True


def test_summarize_payload_shape_only():
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": CANARY_TOKEN}],
        "tools": [{"type": "function"}, {"type": "function"}],
        "stream": True,
    }
    summary = summarize_payload(payload)
    assert summary == {
        "stream": True,
        "tool_count": 2,
        "payload_keys": ["messages", "model", "stream", "tools"],
    }
    _assert_no_canary(json.dumps(summary))
    assert summarize_payload(None) == {
        "stream": False,
        "tool_count": 0,
        "payload_keys": [],
    }
    assert summarize_payload("junk")["payload_keys"] == []


def test_summarize_response_openai_error_masked_and_bounded():
    req = httpx2.Request("GET", f"https://api.groq.com/openai/v1/models?key={CANARY_API_KEY}")
    body = {
        "error": {
            "message": (
                f"GET https://api.groq.com/accounts/{CANARY_CF_ACCOUNT}/v1"
                f"?key={CANARY_API_KEY} denied token={CANARY_TOKEN}"
            ),
            "type": "forbidden",
            "code": "E403",
        }
    }
    resp = httpx2.Response(
        403,
        json=body,
        headers={"cf-ray": "abc123", "content-type": "application/json; charset=utf-8"},
        request=req,
    )
    s = summarize_response(resp.status_code, resp.headers, resp.json())
    assert s["status"] == 403
    assert s["content_type"] == "application/json"
    assert s["top_level_keys"] == ["error"]
    assert s["error_code"] == "E403"
    assert s["error_type"] == "forbidden"
    assert s["error_message"] is not None
    assert len(s["error_message"]) <= 256
    assert s["request_id_present"] is True
    assert s["choices_count"] is None
    assert s["result_count"] is None
    _assert_no_canary(json.dumps(s))


def test_summarize_response_cloudflare_errors_list_shape():
    body = {
        "success": False,
        "errors": [{"code": 7000, "message": f"bad route accounts/{CANARY_CF_ACCOUNT}"}],
        "messages": [],
    }
    s = summarize_response(405, {}, body)
    assert s["error_code"] == "7000"
    assert s["error_message"] == "bad route accounts/<account_id>"
    assert s["error_type"] is None
    _assert_no_canary(json.dumps(s))


def test_summarize_response_malformed_body_structural():
    s = summarize_response(500, {"content-type": "text/html"}, "<html>boom</html>")
    assert s["status"] == 500
    assert s["content_type"] == "text/html"
    assert s["top_level_keys"] == []
    assert s["error_message"] is None
    assert s["request_id_present"] is False
    s2 = summarize_response(None, None, None)
    assert s2["status"] is None
    assert s2["top_level_keys"] == []


def test_summarize_response_counts_and_request_id_presence():
    s = summarize_response(200, {}, {"data": [1, 2], "choices": [{}]})
    assert s["result_count"] == 2
    assert s["choices_count"] == 1
    s2 = summarize_response(200, {}, {"result": [1, 2, 3], "request_id": "req-9"})
    assert s2["result_count"] == 3
    assert s2["request_id_present"] is True
    # Request-id VALUES are never recorded — presence boolean only.
    s3 = summarize_response(200, {"openai-request-id": "req-secret-value"}, {})
    assert s3["request_id_present"] is True
    assert "req-secret-value" not in json.dumps(s3)


# ---------------------------------------------------------------------------
# build_incident_record — schema & exception-response extraction
# ---------------------------------------------------------------------------


def test_record_schema_exact_keys_and_canary_free():
    req = httpx2.Request(
        "GET",
        f"https://api.cloudflare.com/client/v4/accounts/{CANARY_CF_ACCOUNT}"
        f"/ai/v1/models?key={CANARY_API_KEY}",
    )
    resp = httpx2.Response(
        405,
        json={
            "errors": [{"code": 7001, "message": f"method not allowed token={CANARY_TOKEN}"}],
            "success": False,
        },
        headers={"content-type": "application/json"},
        request=req,
    )
    exc = None
    try:
        resp.raise_for_status()
    except httpx2.HTTPStatusError as err:
        exc = err
    rec = build_incident_record(
        provider="cloudflare",
        operation="catalog_models_openai",
        method="GET",
        url=str(req.url),
        headers={"Authorization": f"Bearer {CANARY_TOKEN}", "Accept": "application/json"},
        duration_ms=42,
        exception=exc,
    )
    assert set(rec) == {
        "schema_version",
        "ts",
        "provider",
        "operation",
        "model",
        "request",
        "response",
        "duration_ms",
        "exception_type",
    }
    assert rec["schema_version"] == SCHEMA_VERSION
    assert rec["provider"] == "cloudflare"
    assert rec["model"] is None
    assert set(rec["request"]) == {
        "method",
        "url_template",
        "query_keys",
        "header_names",
        "auth_present",
        "stream",
        "tool_count",
        "payload_keys",
    }
    assert set(rec["response"]) == {
        "status",
        "content_type",
        "top_level_keys",
        "error_code",
        "error_type",
        "error_message",
        "choices_count",
        "result_count",
        "request_id_present",
    }
    assert rec["exception_type"] == "HTTPStatusError"
    assert rec["duration_ms"] == 42
    assert rec["response"]["status"] == 405
    assert rec["response"]["error_code"] == "7001"
    assert rec["response"]["error_message"] == "method not allowed [REDACTED]"
    assert rec["request"]["url_template"].endswith("/accounts/<account_id>/ai/v1/models")
    assert rec["request"]["query_keys"] == ["key"]
    assert rec["request"]["auth_present"] is True
    assert rec["request"]["header_names"] == ["accept", "authorization"]
    _assert_no_canary(json.dumps(rec, ensure_ascii=False))


def test_record_exception_freetext_with_secrets_never_persisted():
    url = f"https://api.test/v1/models?key={CANARY_API_KEY}"
    exc = httpx2.ConnectError(f"connection failed for {url} token={CANARY_TOKEN}")
    rec = build_incident_record(
        provider="x",
        operation="catalog_models_openai",
        url=url,
        exception=exc,
    )
    assert rec["exception_type"] == "ConnectError"
    assert rec["response"]["status"] is None
    assert rec["response"]["error_message"] is None
    _assert_no_canary(json.dumps(rec))


def test_record_exception_response_non_json_body():
    class FakeResp:
        status_code = 502
        headers = {"content-type": "text/html"}

        def json(self):
            raise ValueError("not json")

    class FakeErr(Exception):
        def __init__(self):
            super().__init__(f"boom {CANARY_API_KEY}")
            self.response = FakeResp()

    rec = build_incident_record(
        provider="x", operation="catalog_models_native", exception=FakeErr()
    )
    assert rec["exception_type"] == "FakeErr"
    assert rec["response"]["status"] == 502
    assert rec["response"]["content_type"] == "text/html"
    assert rec["response"]["top_level_keys"] == []
    _assert_no_canary(json.dumps(rec))


def test_record_explicit_fields_and_ts():
    rec = build_incident_record(
        provider="groq",
        operation="catalog_models_openai",
        model="llama-3.3-70b-versatile",
        method="post",
        ts="2026-11-27T00:00:00Z",
        payload={"messages": [{"content": CANARY_TOKEN}], "tools": [1], "stream": True},
        response_status=200,
        response_body={"data": [{"id": "m1"}], "object": "list"},
    )
    assert rec["ts"] == "2026-11-27T00:00:00Z"
    assert rec["model"] == "llama-3.3-70b-versatile"
    assert rec["request"]["method"] == "POST"
    assert rec["request"]["stream"] is True
    assert rec["request"]["tool_count"] == 1
    assert "messages" in rec["request"]["payload_keys"]
    assert rec["response"]["result_count"] == 1
    assert rec["response"]["top_level_keys"] == ["data", "object"]
    assert rec["exception_type"] is None
    _assert_no_canary(json.dumps(rec))


def test_record_auto_ts_format():
    rec = build_incident_record(provider="p", operation="chat_completion")
    assert rec["ts"].endswith("Z")
    assert "T" in rec["ts"]


# ---------------------------------------------------------------------------
# append_incident_record / emit_incident_safe
# ---------------------------------------------------------------------------


def test_append_incident_record_jsonl(tmp_path):
    target = tmp_path / "nested" / "incidents.jsonl"
    rec = build_incident_record(
        provider="groq",
        operation="catalog_models_openai",
        url=f"https://api.test/accounts/{CANARY_CF_ACCOUNT}/v1?key={CANARY_API_KEY}",
        headers={"Authorization": f"Bearer {CANARY_API_KEY}"},
        response_status=200,
        response_body={"data": []},
    )
    returned = append_incident_record(rec, path=target)
    append_incident_record({"second": 1}, path=target)
    assert returned == target
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["operation"] == "catalog_models_openai"
    assert json.loads(lines[1]) == {"second": 1}
    _assert_no_canary("\n".join(lines))


def test_emit_incident_safe_never_raises():
    emit_incident_safe(None, {"x": 1})
    emit_incident_safe("not-callable", {"x": 1})
    calls = []
    emit_incident_safe(calls.append, {"x": 1})
    assert calls == [{"x": 1}]

    def boom(_rec):
        raise RuntimeError("sink exploded")

    emit_incident_safe(boom, {"x": 2})
