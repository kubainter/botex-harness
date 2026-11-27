# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Cloudflare catalog discovery sequencing probes (plan 060, Track B).

Offline transport-mocked tests: native ``/ai/models/search`` first,
OpenAI-style ``/models`` fallback second, ``catalog_skipped_unconfigured``
for unresolved account ids — every attempt recorded, none silently
discarded.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

import botex.providers.http_pool as http_pool
from botex.providers.catalog import sync_provider_catalogs
from botex.providers.providers import CloudflareWorkersAIProvider

CANARY_CF_ACCOUNT = "CANARY_CF_ACCOUNT_ABCDEF123456"
CANARY_TOKEN = "CANARY_TOKEN_XYZ777"
CANARY_API_KEY = "CANARY_API_KEY_9988"

CF_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CANARY_CF_ACCOUNT}"
OPENAI_MODELS_URL = f"{CF_BASE}/ai/v1/models"
NATIVE_SEARCH_URL = f"{CF_BASE}/ai/models/search"


@pytest.fixture(autouse=True)
def cf_env(monkeypatch):
    for key in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)


def _resp(status, payload, url, headers=None):
    req = httpx2.Request("GET", url)
    return httpx2.Response(
        status,
        json=payload,
        headers=headers or {"content-type": "application/json"},
        request=req,
    )


class FakeClient:
    """Sync-client stand-in routing exact URLs to canned responses."""

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
    def __init__(self, providers, failing=()):
        self._providers = providers
        self._failing = set(failing)

    def get(self, name, provider_cfg=None, strict=False):
        if name in self._failing or name not in self._providers:
            raise KeyError(f"unknown provider: {name}")
        return self._providers[name]


def _provider():
    return CloudflareWorkersAIProvider("cloudflare", {"account_id": CANARY_CF_ACCOUNT})


# ---------------------------------------------------------------------------
# Sequencing: native search first -> OpenAI /models fallback, both recorded
# ---------------------------------------------------------------------------


def test_native_200_first_no_openai_call(monkeypatch):
    client = FakeClient(
        {
            NATIVE_SEARCH_URL: _resp(
                200,
                {
                    "result": [
                        {"name": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
                        {"name": "@cf/qwen/qwen2.5-coder-32b-instruct"},
                    ],
                    "success": True,
                },
                NATIVE_SEARCH_URL,
            ),
        }
    )
    monkeypatch.setattr(http_pool, "get_sync_client", lambda: client)

    records = []
    models = _provider().fetch_catalog(api_key=CANARY_TOKEN, incident_sink=records.append)

    assert models == [
        {"id": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
        {"id": "@cf/qwen/qwen2.5-coder-32b-instruct"},
    ]
    # Deterministic happy path: native route only, zero /ai/v1/models calls.
    assert [c["url"] for c in client.calls] == [NATIVE_SEARCH_URL]

    assert [r["operation"] for r in records] == ["catalog_models_native"]
    rec = records[0]
    assert rec["response"]["status"] == 200
    assert rec["response"]["result_count"] == 2
    assert rec["response"]["top_level_keys"] == ["result", "success"]
    assert rec["exception_type"] is None
    assert rec["request"]["url_template"].endswith("/accounts/<account_id>/ai/models/search")
    assert rec["provider"] == "cloudflare"
    assert rec["request"]["method"] == "GET"
    assert rec["request"]["auth_present"] is True

    blob = json.dumps(records, ensure_ascii=False)
    for canary in (CANARY_CF_ACCOUNT, CANARY_TOKEN, CANARY_API_KEY):
        assert canary not in blob


def test_native_failure_falls_back_to_openai_200(monkeypatch):
    client = FakeClient(
        {
            NATIVE_SEARCH_URL: _resp(
                500,
                {"errors": [{"code": 10000, "message": "boom"}], "success": False},
                NATIVE_SEARCH_URL,
            ),
            OPENAI_MODELS_URL: _resp(
                200,
                {"data": [{"id": "@cf/a/m1"}, {"id": "@cf/a/m2"}], "object": "list"},
                OPENAI_MODELS_URL,
            ),
        }
    )
    monkeypatch.setattr(http_pool, "get_sync_client", lambda: client)

    records = []
    models = _provider().fetch_catalog(api_key=CANARY_TOKEN, incident_sink=records.append)

    assert models == [{"id": "@cf/a/m1"}, {"id": "@cf/a/m2"}]
    assert [c["url"] for c in client.calls] == [NATIVE_SEARCH_URL, OPENAI_MODELS_URL]

    # The failed native route's evidence must survive the fallback.
    assert [r["operation"] for r in records] == [
        "catalog_models_native",
        "catalog_models_openai",
    ]
    first = records[0]
    assert first["response"]["status"] == 500
    assert first["exception_type"] == "HTTPStatusError"
    assert first["request"]["url_template"].endswith("/accounts/<account_id>/ai/models/search")
    second = records[1]
    assert second["response"]["status"] == 200
    assert second["response"]["result_count"] == 2
    assert second["exception_type"] is None
    assert (
        second["request"]["url_template"]
        == "https://api.cloudflare.com/client/v4/accounts/<account_id>/ai/v1/models"
    )
    for rec in records:
        assert rec["provider"] == "cloudflare"
        assert rec["request"]["method"] == "GET"
        assert rec["request"]["auth_present"] is True

    blob = json.dumps(records, ensure_ascii=False)
    for canary in (CANARY_CF_ACCOUNT, CANARY_TOKEN, CANARY_API_KEY):
        assert canary not in blob


def test_both_routes_failure_preserves_records_and_raises(monkeypatch):
    client = FakeClient(
        {
            NATIVE_SEARCH_URL: _resp(
                405,
                {"errors": [{"code": 7001, "message": "method not allowed"}], "success": False},
                NATIVE_SEARCH_URL,
            ),
            OPENAI_MODELS_URL: _resp(
                500,
                {"errors": [{"code": 10000, "message": "boom"}], "success": False},
                OPENAI_MODELS_URL,
            ),
        }
    )
    monkeypatch.setattr(http_pool, "get_sync_client", lambda: client)

    records = []
    with pytest.raises(httpx2.HTTPStatusError):
        _provider().fetch_catalog(api_key=CANARY_TOKEN, incident_sink=records.append)

    assert [c["url"] for c in client.calls] == [NATIVE_SEARCH_URL, OPENAI_MODELS_URL]
    assert [r["operation"] for r in records] == [
        "catalog_models_native",
        "catalog_models_openai",
    ]
    assert records[0]["response"]["status"] == 405
    assert records[0]["exception_type"] == "HTTPStatusError"
    assert records[0]["response"]["error_code"] == "7001"
    assert records[1]["response"]["status"] == 500
    assert records[1]["exception_type"] == "HTTPStatusError"


# ---------------------------------------------------------------------------
# Unconfigured provider: skipped locally, zero HTTP calls
# ---------------------------------------------------------------------------


def test_unresolved_account_id_emits_skipped_without_network(monkeypatch):
    client = FakeClient({})
    monkeypatch.setattr(http_pool, "get_sync_client", lambda: client)
    provider = CloudflareWorkersAIProvider("cloudflare", {})

    records = []
    with pytest.raises(ValueError, match="account id"):
        provider.fetch_catalog(api_key=CANARY_TOKEN, incident_sink=records.append)

    assert client.calls == []
    assert len(records) == 1
    rec = records[0]
    assert rec["operation"] == "catalog_skipped_unconfigured"
    assert rec["exception_type"] == "ValueError"
    assert "<account_id>" in rec["request"]["url_template"]
    assert "{account_id}" not in rec["request"]["url_template"]
    assert rec["request"]["auth_present"] is True
    assert CANARY_TOKEN not in json.dumps(rec)


def test_no_sink_no_records_and_same_behavior(monkeypatch):
    client = FakeClient({})
    monkeypatch.setattr(http_pool, "get_sync_client", lambda: client)
    provider = CloudflareWorkersAIProvider("cloudflare", {})
    with pytest.raises(ValueError):
        provider.fetch_catalog(api_key=CANARY_TOKEN)


# ---------------------------------------------------------------------------
# sync_provider_catalogs: incident wiring + backward compatibility
# ---------------------------------------------------------------------------


def test_sync_records_incidents_to_sink_and_log(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", CANARY_TOKEN)
    client = FakeClient(
        {
            NATIVE_SEARCH_URL: _resp(
                200,
                {"result": [{"name": "@cf/a/m1"}], "success": True},
                NATIVE_SEARCH_URL,
            ),
        }
    )
    monkeypatch.setattr(http_pool, "get_sync_client", lambda: client)

    registry = FakeRegistry({"cloudflare": _provider()})
    log_path = tmp_path / "incidents" / "provider_incidents.jsonl"
    mem_records = []

    results = sync_provider_catalogs(
        registry,
        ["cloudflare"],
        cache_file=tmp_path / "cache.json",
        incident_log_path=log_path,
        incident_sink=mem_records.append,
    )

    # Backward-compatible result schema.
    assert set(results["cloudflare"].keys()) == {
        "status",
        "model_count",
        "error",
        "fetched_at",
    }
    assert results["cloudflare"]["status"] == "ok"
    assert results["cloudflare"]["model_count"] == 1
    assert results["cloudflare"]["error"] is None
    assert results["cloudflare"]["fetched_at"] > 0.0

    # Both sinks received the single native attempt.
    ops = [r["operation"] for r in mem_records]
    assert ops == ["catalog_models_native"]
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert [json.loads(line)["operation"] for line in lines] == ops
    file_text = log_path.read_text(encoding="utf-8")
    for canary in (CANARY_CF_ACCOUNT, CANARY_TOKEN, CANARY_API_KEY):
        assert canary not in file_text


def test_sync_unknown_provider_emits_skipped_unconfigured(tmp_path):
    registry = FakeRegistry({}, failing=["cloudflare"])
    mem_records = []
    results = sync_provider_catalogs(
        registry,
        ["cloudflare"],
        cache_file=tmp_path / "cache.json",
        incident_sink=mem_records.append,
    )
    assert results["cloudflare"]["status"] == "error"
    assert results["cloudflare"]["error"] is not None
    assert len(mem_records) == 1
    assert mem_records[0]["operation"] == "catalog_skipped_unconfigured"
    assert mem_records[0]["exception_type"] == "KeyError"


def test_sync_unresolved_account_id_records_skipped_via_adapter(monkeypatch, tmp_path):
    """Adapter-level skip: one record, still an error result, no HTTP calls."""
    client = FakeClient({})
    monkeypatch.setattr(http_pool, "get_sync_client", lambda: client)
    registry = FakeRegistry({"cloudflare": CloudflareWorkersAIProvider("cloudflare", {})})
    mem_records = []
    results = sync_provider_catalogs(
        registry,
        ["cloudflare"],
        cache_file=tmp_path / "cache.json",
        incident_sink=mem_records.append,
    )
    assert client.calls == []
    assert results["cloudflare"]["status"] == "error"
    assert [r["operation"] for r in mem_records] == ["catalog_skipped_unconfigured"]
    assert mem_records[0]["exception_type"] == "ValueError"


def test_sync_legacy_provider_without_sink_kwarg_still_works(tmp_path):
    """Providers whose fetch_catalog lacks the incident_sink kwarg (mocks,
    third-party adapters) must keep syncing — the kwarg is never forced."""

    class LegacyProvider:
        name = "legacy"
        base_url = "https://legacy.invalid/v1"
        config: dict = {}

        def resolve_api_key(self, **kwargs):
            return "k"

        def get_model_capabilities(self, model):
            return {}

        def fetch_catalog(self, api_key=None, timeout=10.0):
            return [{"id": "legacy/m1", "object": "model"}]

    registry = FakeRegistry({"legacy": LegacyProvider()})
    results = sync_provider_catalogs(
        registry,
        ["legacy"],
        cache_file=tmp_path / "cache.json",
        incident_sink=lambda rec: None,
    )
    assert results["legacy"]["status"] == "ok"
    assert results["legacy"]["model_count"] == 1
    assert (tmp_path / "cache.json").exists()


def test_sync_sink_error_does_not_break_results(monkeypatch, tmp_path):
    client = FakeClient(
        {NATIVE_SEARCH_URL: _resp(200, {"result": [{"name": "@cf/a/m1"}]}, NATIVE_SEARCH_URL)}
    )
    monkeypatch.setattr(http_pool, "get_sync_client", lambda: client)

    def broken_sink(_rec):
        raise RuntimeError("sink down")

    results = sync_provider_catalogs(
        FakeRegistry({"cloudflare": _provider()}),
        ["cloudflare"],
        cache_file=tmp_path / "cache.json",
        incident_sink=broken_sink,
    )
    assert results["cloudflare"]["status"] == "ok"
    assert results["cloudflare"]["model_count"] == 1
