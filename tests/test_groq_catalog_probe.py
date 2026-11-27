# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Groq catalog discovery wire-shape capture (plan 060, Track A).

A 403 on ``GET /models`` is recorded with the exact URL template, header
names, auth presence, status, and top-level body keys — never the key or
any secret material. Offline transport mocks only.
"""

from __future__ import annotations

import json

import httpx2
import pytest

import botex.providers.http_pool as http_pool
from botex.providers.catalog import sync_provider_catalogs
from botex.providers.providers import GroqProvider

CANARY_API_KEY = "CANARY_API_KEY_9988"
CANARY_TOKEN = "CANARY_TOKEN_XYZ777"

GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"


@pytest.fixture(autouse=True)
def groq_env(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


def _resp(status, payload, url, headers=None):
    req = httpx2.Request("GET", url)
    return httpx2.Response(
        status,
        json=payload,
        headers=headers or {"content-type": "application/json"},
        request=req,
    )


class FakeClient:
    def __init__(self, routes):
        self._routes = routes
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        resp = self._routes.get(url)
        if resp is None:
            raise AssertionError(f"unexpected url {url}")
        return resp


class FakeRegistry:
    def __init__(self, providers):
        self._providers = providers

    def get(self, name, provider_cfg=None, strict=False):
        if name not in self._providers:
            raise KeyError(f"unknown provider: {name}")
        return self._providers[name]


# ---------------------------------------------------------------------------
# 403: exact wire shape captured without secrets
# ---------------------------------------------------------------------------


def test_groq_models_403_records_exact_wire_shape(monkeypatch):
    resp = _resp(
        403,
        {
            "error": {
                "message": "Forbidden: insufficient scope",
                "type": "forbidden",
                "code": "forbidden",
            }
        },
        GROQ_MODELS_URL,
        headers={"content-type": "application/json", "x-request-id": "req_opaque_1"},
    )
    client = FakeClient({GROQ_MODELS_URL: resp})
    monkeypatch.setattr(http_pool, "get_sync_client", lambda: client)

    records = []
    provider = GroqProvider("groq", {})
    with pytest.raises(httpx2.HTTPStatusError):
        provider.fetch_catalog(api_key=CANARY_API_KEY, incident_sink=records.append)

    # The wire call carried the real Authorization header…
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == GROQ_MODELS_URL
    assert call["headers"]["Authorization"] == f"Bearer {CANARY_API_KEY}"
    assert call["headers"]["Accept"] == "application/json"

    # …but the record carries only names and presence booleans.
    assert len(records) == 1
    rec = records[0]
    assert rec["provider"] == "groq"
    assert rec["operation"] == "catalog_models_openai"
    assert rec["model"] is None
    req_rec = rec["request"]
    assert req_rec["method"] == "GET"
    assert req_rec["url_template"] == "https://api.groq.com/openai/v1/models"
    assert req_rec["query_keys"] == []
    assert req_rec["header_names"] == ["accept", "authorization"]
    assert req_rec["auth_present"] is True
    resp_rec = rec["response"]
    assert resp_rec["status"] == 403
    assert resp_rec["content_type"] == "application/json"
    assert resp_rec["top_level_keys"] == ["error"]
    assert resp_rec["error_type"] == "forbidden"
    assert resp_rec["error_code"] == "forbidden"
    assert resp_rec["error_message"] == "Forbidden: insufficient scope"
    assert resp_rec["request_id_present"] is True
    assert resp_rec["result_count"] is None
    assert resp_rec["choices_count"] is None
    assert rec["exception_type"] == "HTTPStatusError"

    for canary in (CANARY_API_KEY, CANARY_TOKEN):
        assert canary not in json.dumps(rec)


def test_groq_models_200_records_success(monkeypatch):
    resp = _resp(
        200,
        {
            "object": "list",
            "data": [
                {"id": "llama-3.3-70b-versatile", "object": "model"},
                {"id": "openai/gpt-oss-120b", "object": "model"},
            ],
        },
        GROQ_MODELS_URL,
    )
    client = FakeClient({GROQ_MODELS_URL: resp})
    monkeypatch.setattr(http_pool, "get_sync_client", lambda: client)

    records = []
    models = GroqProvider("groq", {}).fetch_catalog(
        api_key=CANARY_API_KEY, incident_sink=records.append
    )

    assert models == [
        {"id": "llama-3.3-70b-versatile", "object": "model"},
        {"id": "openai/gpt-oss-120b", "object": "model"},
    ]
    assert len(records) == 1
    rec = records[0]
    assert rec["operation"] == "catalog_models_openai"
    assert rec["response"]["status"] == 200
    assert rec["response"]["top_level_keys"] == ["data", "object"]
    assert rec["response"]["result_count"] == 2
    assert rec["exception_type"] is None
    assert CANARY_API_KEY not in json.dumps(rec)


def test_groq_transport_error_records_exception_type_only(monkeypatch):
    class BrokenClient:
        def get(self, url, headers=None, timeout=None):
            raise httpx2.ConnectError(f"dns resolution failed for {url}?key={CANARY_API_KEY}")

    monkeypatch.setattr(http_pool, "get_sync_client", lambda: BrokenClient())
    records = []
    with pytest.raises(httpx2.ConnectError):
        GroqProvider("groq", {}).fetch_catalog(api_key=CANARY_API_KEY, incident_sink=records.append)
    assert len(records) == 1
    rec = records[0]
    assert rec["operation"] == "catalog_models_openai"
    assert rec["exception_type"] == "ConnectError"
    assert rec["response"]["status"] is None
    # Exception free-text (URL + key) never enters the record.
    assert CANARY_API_KEY not in json.dumps(rec)


def test_groq_403_via_sync_sink_keeps_error_schema(monkeypatch, tmp_path):
    resp = _resp(
        403,
        {"error": {"message": "forbidden", "type": "forbidden", "code": "forbidden"}},
        GROQ_MODELS_URL,
    )
    client = FakeClient({GROQ_MODELS_URL: resp})
    monkeypatch.setattr(http_pool, "get_sync_client", lambda: client)
    monkeypatch.setenv("GROQ_API_KEY", CANARY_API_KEY)

    records = []
    results = sync_provider_catalogs(
        FakeRegistry({"groq": GroqProvider("groq", {})}),
        ["groq"],
        cache_file=tmp_path / "cache.json",
        incident_sink=records.append,
    )
    assert set(results["groq"].keys()) == {
        "status",
        "model_count",
        "error",
        "fetched_at",
    }
    assert results["groq"]["status"] == "error"
    assert results["groq"]["model_count"] == 0
    assert "403" in results["groq"]["error"]
    assert results["groq"]["fetched_at"] == 0.0
    assert len(records) == 1
    assert records[0]["operation"] == "catalog_models_openai"
    assert records[0]["response"]["status"] == 403
    assert CANARY_API_KEY not in json.dumps(records)
    assert CANARY_API_KEY not in json.dumps(results)
