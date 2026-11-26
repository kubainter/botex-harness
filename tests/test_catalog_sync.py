# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Multi-provider catalog sync tests (plan 053, step 2)
====================================================
Offline suite for ``sync_provider_catalogs``: per-provider failure isolation,
stale cache retention on empty/error responses, and secret-free cache writes.
Strictly no network — providers are mocks behind a fake registry.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from botex.catalog import ModelCatalog, sync_provider_catalogs
from botex.providers import BaseProvider
from botex.providers.providers import ProviderRegistry


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Ensure no live network calls can be made during catalog sync tests."""

    def blocked_connect(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Blocked live network socket call during unit test execution")

    with patch.object(socket.socket, "connect", side_effect=blocked_connect):
        yield


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch):
    """Clear all provider API keys from environment to test hermetic behavior."""
    for key in [
        "OPENROUTER_API_KEY",
        "NVIDIA_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def isolated_cache_path(tmp_path: Path) -> Path:
    return tmp_path / ".model_pricing.json"


class FakeRegistry:
    """Minimal registry stub exposing the same ``get`` surface as ProviderRegistry."""

    def __init__(self, providers: Dict[str, Any], failing: Optional[List[str]] = None) -> None:
        self._providers = providers
        self._failing = set(failing or [])

    def get(self, name: str, provider_cfg: Any = None, strict: bool = False) -> Any:
        if name in self._failing or name not in self._providers:
            raise KeyError(f"unknown provider: {name}")
        return self._providers[name]


def make_mock_provider(
    name: str,
    base_url: str,
    catalog_payload: Any = None,
    side_effect: Any = None,
) -> MagicMock:
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = name
    mock_provider.base_url = base_url
    mock_provider.config = {}
    mock_provider.get_model_capabilities.return_value = {}
    if side_effect is not None:
        mock_provider.fetch_catalog.side_effect = side_effect
    else:
        mock_provider.fetch_catalog.return_value = catalog_payload or []
    return mock_provider


def read_cache(cache_file: Path) -> Dict[str, Any]:
    return json.loads(cache_file.read_text(encoding="utf-8"))


def seed_stale_entry(cache_file: Path, base_url: str, fetched_at: float) -> Dict[str, Any]:
    stale_catalog = {
        "vendor/stale-model": {
            "id": "vendor/stale-model",
            "name": "Stale Model",
            "context_length": 32768,
            "prompt_price_per_m": 1.0,
            "completion_price_per_m": 2.0,
            "supports_tools": True,
        }
    }
    payload = {
        "fetched_at": fetched_at,
        "catalogs": {base_url: {"fetched_at": fetched_at, "catalog": stale_catalog}},
    }
    cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


# ---------------------------------------------------------------------------
# Happy path & cache format compatibility
# ---------------------------------------------------------------------------


def test_sync_threads_provider_config_into_registry_and_cache_key(isolated_cache_path: Path):
    """Configured base_url/auth env reach the adapter and determine the cache key."""
    captured: Dict[str, Any] = {}

    class ConfiguredRegistry:
        def get(self, name: str, provider_cfg: Any = None, strict: bool = False) -> Any:
            captured["name"] = name
            captured["provider_cfg"] = provider_cfg
            return make_mock_provider(
                name,
                str(provider_cfg["base_url"]),
                catalog_payload=[{"id": "vendor/live-model", "object": "model"}],
            )

    cfg = {"base_url": "https://proxy.example.test/v1", "api_key_env": "CUSTOM_KEY_ENV"}
    results = sync_provider_catalogs(
        ConfiguredRegistry(),
        ["nvidia"],
        cache_file=isolated_cache_path,
        provider_configs={"nvidia": cfg},
    )

    assert results["nvidia"]["status"] == "ok"
    assert captured["provider_cfg"] == cfg
    written = read_cache(isolated_cache_path)
    assert "https://proxy.example.test/v1" in written["catalogs"]
    assert "https://integrate.api.nvidia.com/v1" not in written["catalogs"]


def test_cloudflare_config_only_account_id_reaches_adapter():
    """ProviderRegistry receives config-only Cloudflare account routing information."""
    provider = ProviderRegistry.get(
        "cloudflare",
        {"account_id": "acct123", "api_key_env": "CLOUDFLARE_TEST_KEY"},
    )
    assert provider.config["account_id"] == "acct123"
    assert provider.config["api_key_env"] == "CLOUDFLARE_TEST_KEY"
    assert "/accounts/acct123/" in provider.base_url


def test_sync_success_writes_shared_cache(isolated_cache_path: Path):
    """Successful sync writes entry under provider cache_key in the standard format."""
    prov = make_mock_provider(
        "nvidia",
        "https://integrate.api.nvidia.com/v1",
        catalog_payload=[
            {"id": "meta/llama-3.1-70b-instruct", "object": "model"},
            {"id": "mistralai/mixtral-8x7b-instruct-v0.1", "object": "model"},
        ],
    )
    registry = FakeRegistry({"nvidia": prov})

    results = sync_provider_catalogs(registry, ["nvidia"], cache_file=isolated_cache_path)

    assert results["nvidia"]["status"] == "ok"
    assert results["nvidia"]["model_count"] == 2
    assert results["nvidia"]["error"] is None
    assert results["nvidia"]["fetched_at"] > 0.0

    payload = read_cache(isolated_cache_path)
    entry = payload["catalogs"]["https://integrate.api.nvidia.com/v1"]
    assert entry["fetched_at"] > 0.0
    assert "meta/llama-3.1-70b-instruct" in entry["catalog"]
    # Pricing compatibility: legacy keys consumed by botex/pricing.py must persist.
    model_entry = entry["catalog"]["meta/llama-3.1-70b-instruct"]
    assert "input" in model_entry
    assert "output" in model_entry
    assert "tools" in model_entry


def test_sync_written_cache_roundtrips_through_load(isolated_cache_path: Path):
    """Cache written by sync is loadable by a fresh ModelCatalog within TTL."""
    prov = make_mock_provider(
        "groq",
        "https://api.groq.com/openai/v1",
        catalog_payload=[{"id": "openai/gpt-oss-20b", "object": "model"}],
    )
    registry = FakeRegistry({"groq": prov})
    sync_provider_catalogs(registry, ["groq"], cache_file=isolated_cache_path)

    reader = make_mock_provider("groq", "https://api.groq.com/openai/v1")
    catalog = ModelCatalog(provider=reader, cache_file=isolated_cache_path, ttl_hours=24.0)
    models = catalog.load()
    assert "openai/gpt-oss-20b" in models
    # Fresh cache hit: no fetch needed on the reader provider.
    reader.fetch_catalog.assert_not_called()


# ---------------------------------------------------------------------------
# Per-provider failure isolation
# ---------------------------------------------------------------------------


def test_sync_failure_isolated_per_provider(isolated_cache_path: Path):
    """One provider's exception must not abort the sync of the others."""
    good = make_mock_provider(
        "groq",
        "https://api.groq.com/openai/v1",
        catalog_payload=[{"id": "openai/gpt-oss-20b", "object": "model"}],
    )
    bad = make_mock_provider(
        "nvidia",
        "https://integrate.api.nvidia.com/v1",
        side_effect=ConnectionError("simulated transport failure"),
    )
    registry = FakeRegistry({"groq": good, "nvidia": bad})

    results = sync_provider_catalogs(registry, ["nvidia", "groq"], cache_file=isolated_cache_path)

    assert results["nvidia"]["status"] == "error"
    assert results["nvidia"]["model_count"] == 0
    assert results["nvidia"]["fetched_at"] == 0.0
    assert "ConnectionError" in results["nvidia"]["error"]

    assert results["groq"]["status"] == "ok"
    assert results["groq"]["model_count"] == 1

    payload = read_cache(isolated_cache_path)
    assert "https://api.groq.com/openai/v1" in payload["catalogs"]
    assert "https://integrate.api.nvidia.com/v1" not in payload["catalogs"]


def test_sync_instantiation_failure_isolated(isolated_cache_path: Path):
    """A provider the registry cannot instantiate degrades to an error entry."""
    good = make_mock_provider(
        "groq",
        "https://api.groq.com/openai/v1",
        catalog_payload=[{"id": "openai/gpt-oss-20b", "object": "model"}],
    )
    registry = FakeRegistry({"groq": good}, failing=["cloudflare"])

    results = sync_provider_catalogs(
        registry, ["cloudflare", "groq"], cache_file=isolated_cache_path
    )

    assert results["cloudflare"]["status"] == "error"
    assert results["cloudflare"]["error"] is not None
    assert results["groq"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Stale cache retention
# ---------------------------------------------------------------------------


def test_sync_empty_response_preserves_stale_cache(isolated_cache_path: Path):
    """Empty live catalog must not wipe the last-known-good cache entry."""
    base_url = "https://integrate.api.nvidia.com/v1"
    stale_ts = time.time() - 3600.0
    seed_stale_entry(isolated_cache_path, base_url, stale_ts)

    prov = make_mock_provider("nvidia", base_url, catalog_payload=[])
    registry = FakeRegistry({"nvidia": prov})

    results = sync_provider_catalogs(registry, ["nvidia"], cache_file=isolated_cache_path)

    assert results["nvidia"]["status"] == "error"
    assert results["nvidia"]["model_count"] == 0
    assert results["nvidia"]["error"] is not None

    payload = read_cache(isolated_cache_path)
    entry = payload["catalogs"][base_url]
    assert entry["fetched_at"] == stale_ts
    assert "vendor/stale-model" in entry["catalog"]


def test_sync_error_response_preserves_stale_cache(isolated_cache_path: Path):
    """A raising provider must not modify the existing cache entry."""
    base_url = "https://api.groq.com/openai/v1"
    stale_ts = time.time() - 7200.0
    seed_stale_entry(isolated_cache_path, base_url, stale_ts)

    prov = make_mock_provider("groq", base_url, side_effect=TimeoutError("boom"))
    registry = FakeRegistry({"groq": prov})

    results = sync_provider_catalogs(registry, ["groq"], cache_file=isolated_cache_path)

    assert results["groq"]["status"] == "error"
    payload = read_cache(isolated_cache_path)
    entry = payload["catalogs"][base_url]
    assert entry["fetched_at"] == stale_ts
    assert "vendor/stale-model" in entry["catalog"]


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


def test_sync_cache_contains_no_secret_material(isolated_cache_path: Path):
    """Provider payloads embedding key-shaped strings must be scrubbed on write."""
    dummy_sk = "sk-dummykey1234567890abcdef"
    dummy_nvapi = "nvapi-abcdefgh12345678"
    prov = make_mock_provider(
        "nvidia",
        "https://integrate.api.nvidia.com/v1",
        catalog_payload=[
            {
                "id": "meta/llama-3.1-70b-instruct",
                "name": f"Leaky Model {dummy_sk}",
                "note": f"debug endpoint used {dummy_nvapi}",
                "api_key": dummy_nvapi,
            }
        ],
    )
    registry = FakeRegistry({"nvidia": prov})

    results = sync_provider_catalogs(registry, ["nvidia"], cache_file=isolated_cache_path)
    assert results["nvidia"]["status"] == "ok"

    raw_text = isolated_cache_path.read_text(encoding="utf-8")
    assert dummy_sk not in raw_text
    assert dummy_nvapi not in raw_text


def test_sync_error_string_is_masked(isolated_cache_path: Path):
    """Exception messages carrying key material are masked in the result."""
    dummy_nvapi = "nvapi-abcdefgh12345678"
    bad = make_mock_provider(
        "nvidia",
        "https://integrate.api.nvidia.com/v1",
        side_effect=RuntimeError(f"401 unauthorized for key {dummy_nvapi}"),
    )
    registry = FakeRegistry({"nvidia": bad})

    results = sync_provider_catalogs(registry, ["nvidia"], cache_file=isolated_cache_path)

    assert results["nvidia"]["status"] == "error"
    error_msg = results["nvidia"]["error"] or ""
    assert dummy_nvapi not in error_msg
    assert "RuntimeError" in error_msg


def test_sync_result_shape_keys(isolated_cache_path: Path):
    """Every result entry exposes exactly the documented keys."""
    prov = make_mock_provider(
        "groq",
        "https://api.groq.com/openai/v1",
        catalog_payload=[{"id": "openai/gpt-oss-20b", "object": "model"}],
    )
    registry = FakeRegistry({"groq": prov})

    results = sync_provider_catalogs(registry, ["groq"], cache_file=isolated_cache_path)

    assert set(results["groq"].keys()) == {"status", "model_count", "error", "fetched_at"}
