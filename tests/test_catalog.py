# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Dynamic Model Catalog Unit Tests
======================================
Comprehensive offline test suite for ModelMetadata and ModelCatalog.
Strictly isolated: zero real network connections, isolated temporary cache file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from botex.catalog import CACHE_FILE_DEFAULT, ModelCatalog, ModelMetadata
from botex.providers import BaseProvider


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Ensure no live network calls can be made during catalog tests."""

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
        "OPENAI_API_KEY",
        "CUSTOM_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def isolated_cache_path(tmp_path: Path) -> Path:
    """Provide a clean, isolated cache file path inside pytest's temporary directory."""
    return tmp_path / ".model_pricing.json"


@pytest.fixture
def sample_openrouter_payload() -> Dict[str, Any]:
    return {
        "data": [
            {
                "id": "anthropic/claude-3.5-sonnet",
                "name": "Anthropic: Claude 3.5 Sonnet",
                "context_length": 200000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                "supported_parameters": ["tools", "temperature", "max_tokens"],
            },
            {
                "id": "meta-llama/llama-3.1-8b-instruct:free",
                "name": "Meta: Llama 3.1 8B Instruct (free)",
                "context_length": 131072,
                "pricing": {"prompt": "0", "completion": "0"},
                "supported_parameters": ["tools"],
            },
            {
                "id": "openrouter/auto",
                "name": "OpenRouter Auto Router",
                "context_length": 128000,
                "pricing": {"prompt": "-1", "completion": "-1"},
                "supported_parameters": ["tools"],
            },
            {
                "id": "nousresearch/nous-hermes-2-mixtral-8x7b-dpo",
                "name": "Nous Hermes 2 Mixtral",
                "context_length": 32768,
                "pricing": {"prompt": "0.0000006", "completion": "0.0000006"},
                "supported_parameters": ["temperature"],
            },
        ]
    }


@pytest.fixture
def sample_openai_payload() -> Dict[str, Any]:
    return {
        "data": [
            {"id": "meta/llama-3.1-70b-instruct", "object": "model", "owned_by": "nvidia"},
            {
                "id": "mistralai/mixtral-8x7b-instruct-v0.1",
                "object": "model",
                "owned_by": "nvidia",
            },
        ]
    }


# ---------------------------------------------------------------------------
# Group 1: ModelMetadata Parsing & Calculation
# ---------------------------------------------------------------------------


def test_metadata_pricing_calculation():
    """TEST-CAT-01: Context length and USD/1M price calculation."""
    entry = {
        "id": "anthropic/claude-3.5-sonnet",
        "name": "Anthropic: Claude 3.5 Sonnet",
        "context_length": 200000,
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        "supported_parameters": ["tools"],
    }
    meta = ModelMetadata.from_dict(entry)
    assert meta.id == "anthropic/claude-3.5-sonnet"
    assert meta.name == "Anthropic: Claude 3.5 Sonnet"
    assert meta.context_length == 200000
    assert meta.prompt_price_per_m == pytest.approx(3.0)
    assert meta.completion_price_per_m == pytest.approx(15.0)
    assert meta.supports_tools is True
    assert meta.is_zdr_compliant is True
    assert meta.is_free is False


def test_metadata_dynamic_pricing_none():
    """TEST-CAT-02: Router / negative pricing converted to None."""
    entry = {
        "id": "openrouter/auto",
        "pricing": {"prompt": "-1", "completion": "-1"},
    }
    meta = ModelMetadata.from_dict(entry)
    assert meta.prompt_price_per_m is None
    assert meta.completion_price_per_m is None
    assert meta.is_free is False


def test_metadata_free_tier_parsing():
    """TEST-CAT-03: Free tier ($0.0) price and is_free=True."""
    entry = {
        "id": "meta-llama/llama-3.1-8b-instruct:free",
        "pricing": {"prompt": "0", "completion": "0"},
    }
    meta = ModelMetadata.from_dict(entry)
    assert meta.prompt_price_per_m == 0.0
    assert meta.completion_price_per_m == 0.0
    assert meta.is_free is True
    assert meta.is_zdr_compliant is False


def test_metadata_tool_support_detection():
    """TEST-CAT-04: Tool capability flag from supported_parameters."""
    entry_with_tools = {
        "id": "model-a",
        "supported_parameters": ["tools", "temperature"],
    }
    meta_tools = ModelMetadata.from_dict(entry_with_tools)
    assert meta_tools.supports_tools is True

    entry_no_tools = {
        "id": "model-b",
        "supported_parameters": ["temperature"],
    }
    meta_no_tools = ModelMetadata.from_dict(entry_no_tools)
    assert meta_no_tools.supports_tools is False

    entry_unreported = {
        "id": "model-c",
    }
    meta_unreported = ModelMetadata.from_dict(entry_unreported)
    assert meta_unreported.supports_tools is None


def test_metadata_zdr_compliance_flag():
    """TEST-CAT-05: ZDR compliance evaluation (:free / deny patterns)."""
    free_model = ModelMetadata.from_dict({"id": "meta-llama/llama-3.1-8b-instruct:free"})
    assert free_model.is_zdr_compliant is False

    openrouter_free = ModelMetadata.from_dict({"id": "openrouter/free/some-model"})
    assert openrouter_free.is_zdr_compliant is False

    standard_model = ModelMetadata.from_dict({"id": "anthropic/claude-3.5-sonnet"})
    assert standard_model.is_zdr_compliant is True


def test_metadata_serialization_roundtrip():
    """TEST-CAT-06: Dataclass to_dict/from_dict with legacy keys."""
    meta = ModelMetadata(
        id="anthropic/claude-3.5-sonnet",
        name="Claude 3.5 Sonnet",
        context_length=200000,
        prompt_price_per_m=3.0,
        completion_price_per_m=15.0,
        supports_tools=True,
        is_zdr_compliant=True,
        raw_entry={"original": "data"},
    )
    d = meta.to_dict()
    assert d["input"] == 3.0
    assert d["output"] == 15.0
    assert d["tools"] is True
    assert d["context_length"] == 200000

    restored = ModelMetadata.from_dict(d)
    assert restored == meta


# ---------------------------------------------------------------------------
# Group 2: Dynamic Catalog Loading & Provider Ingestion
# ---------------------------------------------------------------------------


def test_catalog_load_openrouter_mock(
    isolated_cache_path: Path, sample_openrouter_payload: Dict[str, Any]
):
    """TEST-CAT-07: Ingestion from mocked OpenRouter /models."""
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {}
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.fetch_catalog.return_value = sample_openrouter_payload["data"]

    catalog = ModelCatalog(
        provider=mock_provider,
        cache_file=isolated_cache_path,
        ttl_hours=24.0,
    )
    models = catalog.load()

    assert len(models) == 4
    assert "anthropic/claude-3.5-sonnet" in models
    assert catalog.get_model("anthropic/claude-3.5-sonnet") is not None
    # Case-insensitive and trimmed lookup
    assert catalog.get_model("  ANTHROPIC/CLAUDE-3.5-SONNET  ") is not None
    assert catalog.get_model("ANTHROPIC/CLAUDE-3.5-SONNET").context_length == 200000


def test_catalog_load_openai_capabilities_override(
    isolated_cache_path: Path, sample_openai_payload: Dict[str, Any]
):
    """TEST-CAT-08: NVIDIA/OpenAI catalog with capabilities override."""
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "nvidia"
    mock_provider.base_url = "https://integrate.api.nvidia.com/v1"
    mock_provider.config = {
        "capabilities": {
            "meta/llama-3.1-70b-instruct": {
                "supports_tools": True,
                "context_length": 131072,
            }
        }
    }

    def caps_lookup(mid: str) -> Dict[str, Any]:
        return mock_provider.config["capabilities"].get(mid, {})

    mock_provider.get_model_capabilities.side_effect = caps_lookup
    mock_provider.fetch_catalog.return_value = sample_openai_payload["data"]

    catalog = ModelCatalog(
        provider=mock_provider,
        cache_file=isolated_cache_path,
        ttl_hours=24.0,
    )
    catalog.load()

    m1 = catalog.get_model("meta/llama-3.1-70b-instruct")
    assert m1 is not None
    assert m1.supports_tools is True
    assert m1.context_length == 131072

    m2 = catalog.get_model("mistralai/mixtral-8x7b-instruct-v0.1")
    assert m2 is not None
    assert m2.supports_tools is None


def test_catalog_load_empty_payload(isolated_cache_path: Path):
    """TEST-CAT-09: Handling empty provider /models response."""
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {}
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.fetch_catalog.return_value = []

    catalog = ModelCatalog(
        provider=mock_provider,
        cache_file=isolated_cache_path,
        ttl_hours=24.0,
    )
    models = catalog.load()

    assert models == {}
    assert catalog.list_models() == []
    assert catalog.get_model("any") is None


# ---------------------------------------------------------------------------
# Group 3: TTL Caching, Expiration & Cache Invalidation
# ---------------------------------------------------------------------------


def test_catalog_ttl_fresh_cache_hit(isolated_cache_path: Path):
    """TEST-CAT-10: Fresh cache read within TTL window (zero network)."""
    now = time.time()
    raw_catalog = {
        "anthropic/claude-3.5-sonnet": {
            "id": "anthropic/claude-3.5-sonnet",
            "name": "Claude 3.5 Sonnet",
            "context_length": 200000,
            "prompt_price_per_m": 3.0,
            "completion_price_per_m": 15.0,
            "supports_tools": True,
        }
    }
    cache_data = {
        "fetched_at": now,
        "catalogs": {
            "https://openrouter.ai/api/v1": {
                "fetched_at": now - 3600,  # 1 hour ago
                "catalog": raw_catalog,
            }
        },
    }
    isolated_cache_path.write_text(json.dumps(cache_data), encoding="utf-8")

    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {}

    catalog = ModelCatalog(
        provider=mock_provider,
        cache_file=isolated_cache_path,
        ttl_hours=24.0,
    )
    models = catalog.load()

    assert mock_provider.fetch_catalog.call_count == 0
    assert "anthropic/claude-3.5-sonnet" in models
    assert models["anthropic/claude-3.5-sonnet"].prompt_price_per_m == 3.0


def test_catalog_ttl_expiration_reload(isolated_cache_path: Path):
    """TEST-CAT-11: Cache expiration triggers fresh fetch & rewrite."""
    now = time.time()
    cache_data = {
        "fetched_at": now - (25 * 3600),
        "catalogs": {
            "https://openrouter.ai/api/v1": {
                "fetched_at": now - (25 * 3600),  # expired (25h old, TTL=24h)
                "catalog": {"old/model": {"id": "old/model", "name": "Old Model"}},
            }
        },
    }
    isolated_cache_path.write_text(json.dumps(cache_data), encoding="utf-8")

    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {}
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.fetch_catalog.return_value = [
        {"id": "new/model", "name": "New Model", "context_length": 64000}
    ]

    catalog = ModelCatalog(
        provider=mock_provider,
        cache_file=isolated_cache_path,
        ttl_hours=24.0,
    )
    models = catalog.load()

    assert mock_provider.fetch_catalog.call_count == 1
    assert "new/model" in models
    assert "old/model" not in models

    # Verify cache file was rewritten with new data
    payload = json.loads(isolated_cache_path.read_text(encoding="utf-8"))
    assert "new/model" in payload["catalogs"]["https://openrouter.ai/api/v1"]["catalog"]


def test_catalog_force_refresh_bypasses_cache(isolated_cache_path: Path):
    """TEST-CAT-12: force_refresh=True forces provider fetch."""
    now = time.time()
    cache_data = {
        "fetched_at": now - 10,  # 10s old
        "catalogs": {
            "https://openrouter.ai/api/v1": {
                "fetched_at": now - 10,
                "catalog": {"fresh/model": {"id": "fresh/model", "name": "Fresh Model"}},
            }
        },
    }
    isolated_cache_path.write_text(json.dumps(cache_data), encoding="utf-8")

    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {}
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.fetch_catalog.return_value = [
        {"id": "refreshed/model", "name": "Refreshed Model"}
    ]

    catalog = ModelCatalog(
        provider=mock_provider,
        cache_file=isolated_cache_path,
        ttl_hours=24.0,
    )
    models = catalog.load(force_refresh=True)

    assert mock_provider.fetch_catalog.call_count == 1
    assert "refreshed/model" in models


def test_catalog_multi_provider_isolation(isolated_cache_path: Path):
    """TEST-CAT-13: Provider / base_url isolation in cache file."""
    # 1. Fetch OpenRouter
    p_or = MagicMock(spec=BaseProvider)
    p_or.name = "openrouter"
    p_or.base_url = "https://openrouter.ai/api/v1"
    p_or.config = {}
    p_or.get_model_capabilities.return_value = {}
    p_or.fetch_catalog.return_value = [{"id": "or/model", "name": "OR Model"}]

    cat_or = ModelCatalog(provider=p_or, cache_file=isolated_cache_path)
    cat_or.load()

    # 2. Fetch Nvidia
    p_nv = MagicMock(spec=BaseProvider)
    p_nv.name = "nvidia"
    p_nv.base_url = "https://integrate.api.nvidia.com/v1"
    p_nv.config = {}
    p_nv.get_model_capabilities.return_value = {}
    p_nv.fetch_catalog.return_value = [{"id": "nv/model", "name": "NV Model"}]

    cat_nv = ModelCatalog(provider=p_nv, cache_file=isolated_cache_path)
    cat_nv.load()

    # 3. Read cache directly and assert both exist
    payload = json.loads(isolated_cache_path.read_text(encoding="utf-8"))
    catalogs = payload.get("catalogs", {})
    assert "https://openrouter.ai/api/v1" in catalogs
    assert "https://integrate.api.nvidia.com/v1" in catalogs
    assert "or/model" in catalogs["https://openrouter.ai/api/v1"]["catalog"]
    assert "nv/model" in catalogs["https://integrate.api.nvidia.com/v1"]["catalog"]


# ---------------------------------------------------------------------------
# Group 4: Network Resilience & Offline Fallback
# ---------------------------------------------------------------------------


def test_catalog_stale_cache_fallback_on_network_error(
    isolated_cache_path: Path, capsys: pytest.CaptureFixture[str]
):
    """TEST-CAT-14: Offline fallback to stale cache on network failure."""
    now = time.time()
    cache_data = {
        "fetched_at": now - (30 * 3600),
        "catalogs": {
            "https://openrouter.ai/api/v1": {
                "fetched_at": now - (30 * 3600),  # expired
                "catalog": {"cached/model": {"id": "cached/model", "name": "Cached Model"}},
            }
        },
    }
    isolated_cache_path.write_text(json.dumps(cache_data), encoding="utf-8")

    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {}
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.fetch_catalog.side_effect = TimeoutError("Simulated network timeout")

    catalog = ModelCatalog(
        provider=mock_provider,
        cache_file=isolated_cache_path,
        ttl_hours=24.0,
    )
    models = catalog.load()

    assert "cached/model" in models
    assert models["cached/model"].name == "Cached Model"

    err = capsys.readouterr().err
    assert "Catalog fetch failed" in err


def test_catalog_empty_cache_network_unreachable(isolated_cache_path: Path):
    """TEST-CAT-15: Graceful empty return when cache missing & offline."""
    assert not isolated_cache_path.exists()

    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {}
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.fetch_catalog.side_effect = ConnectionRefusedError("Offline")

    catalog = ModelCatalog(
        provider=mock_provider,
        cache_file=isolated_cache_path,
        ttl_hours=24.0,
    )
    models = catalog.load()

    assert models == {}
    assert catalog.list_models() == []
    assert catalog.get_model("any") is None


# ---------------------------------------------------------------------------
# Group 5: Tool Capability Checking (check_tool_support)
# ---------------------------------------------------------------------------


def test_catalog_tool_support_operator_override(isolated_cache_path: Path):
    """TEST-CAT-16: Tool capability check with operator override."""
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {
        "capabilities": {
            "model-forced-tools": {"supports_tools": True},
            "model-disabled-tools": {"supports_tools": False},
        }
    }

    def caps_lookup(mid: str) -> Dict[str, Any]:
        return mock_provider.config["capabilities"].get(mid, {})

    mock_provider.get_model_capabilities.side_effect = caps_lookup
    # Catalog says opposite
    mock_provider.fetch_catalog.return_value = [
        {"id": "model-forced-tools", "supported_parameters": []},  # no tools in catalog
        {"id": "model-disabled-tools", "supported_parameters": ["tools"]},  # tools in catalog
    ]

    catalog = ModelCatalog(provider=mock_provider, cache_file=isolated_cache_path)
    catalog.load()

    assert catalog.check_tool_support("model-forced-tools") is True
    assert catalog.check_tool_support("model-disabled-tools") is False


def test_catalog_tool_support_metadata(isolated_cache_path: Path):
    """TEST-CAT-17: Tool capability check with catalog metadata."""
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {}
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.fetch_catalog.return_value = [
        {"id": "model-tools", "supported_parameters": ["tools"]},
        {"id": "model-notools", "supported_parameters": ["temperature"]},
    ]

    catalog = ModelCatalog(provider=mock_provider, cache_file=isolated_cache_path)
    catalog.load()

    assert catalog.check_tool_support("model-tools") is True
    assert catalog.check_tool_support("model-notools") is False


def test_catalog_tool_support_on_unknown_policy(isolated_cache_path: Path):
    """TEST-CAT-18: Tool capability check with on_unknown allow/deny."""
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {}
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.fetch_catalog.return_value = [
        {"id": "unreported-model"}  # supported_parameters is None
    ]

    catalog = ModelCatalog(provider=mock_provider, cache_file=isolated_cache_path)
    catalog.load()

    assert catalog.check_tool_support("unreported-model", on_unknown="allow") is True
    assert catalog.check_tool_support("unreported-model", on_unknown="ALLOW") is True
    assert catalog.check_tool_support("unreported-model", on_unknown="deny") is False
    assert catalog.check_tool_support("unreported-model", on_unknown="  DENY  ") is False


# ---------------------------------------------------------------------------
# Group 6: Mock Isolation & Safety Invariants
# ---------------------------------------------------------------------------


def test_mock_isolation_zero_network_calls():
    """TEST-MCK-01: Socket blocking verification in offline test suite."""
    with pytest.raises(RuntimeError, match="Blocked live network socket call"):
        socket.socket().connect(("openrouter.ai", 443))


def test_suite_passes_with_empty_credentials():
    """TEST-MCK-02: Execution without environment keys."""
    for key in ["OPENROUTER_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY"]:
        assert key not in os.environ


def test_isolated_filesystem_verification(isolated_cache_path: Path):
    """TEST-MCK-03: Real .model_pricing.json untouched by tests."""
    assert isolated_cache_path != CACHE_FILE_DEFAULT


# ---------------------------------------------------------------------------
# Group 7: Adversarial Resilience & Edge Cases (M2 R2)
# ---------------------------------------------------------------------------


def test_catalog_cache_corrupted_string_entry(isolated_cache_path: Path):
    """TEST-CAT-19: Cache entry as string ignored safely without AttributeError."""
    cache_data = {"catalogs": {"https://openrouter.ai/api/v1": "corrupted_non_dict_entry"}}
    isolated_cache_path.write_text(json.dumps(cache_data), encoding="utf-8")

    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.fetch_catalog.return_value = []
    mock_provider.resolve_api_key.return_value = ""
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.config = {}

    catalog = ModelCatalog(
        provider_name="openrouter",
        provider=mock_provider,
        cache_file=str(isolated_cache_path),
    )

    models = catalog.load()
    assert models == {}
    assert catalog._loaded is True


def test_catalog_cache_corrupted_list_catalog_structure(isolated_cache_path: Path):
    """TEST-CAT-20: Non-dict catalog payload in cache handled safely without AttributeError."""
    now = time.time()
    cache_data = {
        "catalogs": {
            "https://openrouter.ai/api/v1": {
                "fetched_at": now,
                "catalog": [1, 2, 3],  # corrupted: list instead of dict
            }
        }
    }
    isolated_cache_path.write_text(json.dumps(cache_data), encoding="utf-8")

    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.fetch_catalog.return_value = []
    mock_provider.resolve_api_key.return_value = ""
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.config = {}

    catalog = ModelCatalog(
        provider_name="openrouter",
        provider=mock_provider,
        cache_file=str(isolated_cache_path),
    )

    # Must safely fallback or fetch without calling .items() on a list
    models = catalog.load()
    assert isinstance(models, dict)
    assert catalog._loaded is True


def test_catalog_cache_non_numeric_fetched_at(isolated_cache_path: Path):
    """TEST-CAT-21: Non-numeric fetched_at in cache defaulted safely without ValueError."""
    cache_data = {
        "catalogs": {
            "https://openrouter.ai/api/v1": {
                "fetched_at": "invalid_date_timestamp",
                "catalog": {"old/model": {"id": "old/model", "name": "Old Model"}},
            }
        }
    }
    isolated_cache_path.write_text(json.dumps(cache_data), encoding="utf-8")

    fresh_model = ModelMetadata(id="fresh/model", name="Fresh Model", context_length=8192)
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.fetch_catalog.return_value = [{"id": "fresh/model", "name": "Fresh Model"}]
    mock_provider.resolve_api_key.return_value = ""
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.config = {}

    catalog = ModelCatalog(
        provider_name="openrouter",
        provider=mock_provider,
        cache_file=str(isolated_cache_path),
    )

    # Should treat non-numeric fetched_at as expired (0.0) and fetch fresh
    models = catalog.load()
    assert "fresh/model" in models
    assert catalog.get_model("fresh/model") is not None


def test_catalog_cache_json_array_root_read_and_write(isolated_cache_path: Path):
    """TEST-CAT-22: Cache file containing JSON array handled cleanly on read and overwrite."""
    isolated_cache_path.write_text(json.dumps(["corrupted", "array", 123]), encoding="utf-8")

    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.fetch_catalog.return_value = [{"id": "m1", "name": "Model 1"}]
    mock_provider.resolve_api_key.return_value = ""
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.config = {}

    catalog = ModelCatalog(
        provider_name="openrouter",
        provider=mock_provider,
        cache_file=str(isolated_cache_path),
    )

    # 1. Reading array root should safely return None from _read_cache_entry
    assert catalog._read_cache_entry() is None

    # 2. Overwriting should self-heal root structure to a valid JSON dictionary
    models = catalog.load()
    assert "m1" in models

    persisted = json.loads(isolated_cache_path.read_text(encoding="utf-8"))
    assert isinstance(persisted, dict)
    assert "catalogs" in persisted
    assert "https://openrouter.ai/api/v1" in persisted["catalogs"]


@pytest.mark.parametrize("corrupt_pricing", ["free", [0.001, 0.002], 0, True])
def test_catalog_parse_raw_entry_non_dict_pricing(isolated_cache_path: Path, corrupt_pricing: Any):
    """TEST-CAT-23: Non-dict pricing attribute parsed safely with prices as None."""
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.resolve_api_key.return_value = ""
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.config = {}

    catalog = ModelCatalog(
        provider_name="openrouter",
        provider=mock_provider,
        cache_file=str(isolated_cache_path),
    )

    raw_payload = {
        "id": "vendor/model-corrupt-price",
        "name": "Corrupt Price Model",
        "pricing": corrupt_pricing,
    }

    meta = catalog._parse_raw_entry(raw_payload, deny_patterns=[])
    assert meta.id == "vendor/model-corrupt-price"
    assert meta.prompt_price_per_m is None
    assert meta.completion_price_per_m is None


@pytest.mark.parametrize(
    "raw_ctx, expected_ctx",
    [
        ("128000.0", 128000),
        (128000.5, 128000),
        ("200000", 200000),
        ("128k", 0),
        (None, 0),
        ("", 0),
    ],
)
def test_catalog_parse_raw_entry_float_string_context_length(
    isolated_cache_path: Path, raw_ctx: Any, expected_ctx: int
):
    """TEST-CAT-24: Flexible context_length parsing from float string, int, or malformed fallback."""
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.resolve_api_key.return_value = ""
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.config = {}

    catalog = ModelCatalog(
        provider_name="openrouter",
        provider=mock_provider,
        cache_file=str(isolated_cache_path),
    )

    raw_payload = {
        "id": "vendor/ctx-model",
        "context_length": raw_ctx,
    }

    meta = catalog._parse_raw_entry(raw_payload, deny_patterns=[])
    assert meta.context_length == expected_ctx


def test_catalog_metadata_from_dict_non_numeric_price_string():
    """TEST-CAT-25: Unparseable price strings in ModelMetadata.from_dict default safely to None."""
    entry = {
        "id": "custom/model",
        "prompt_price_per_m": "free",
        "completion_price_per_m": "N/A",
        "context_length": "64000.0",
    }
    # Must not raise ValueError
    meta = ModelMetadata.from_dict(entry)
    assert meta.id == "custom/model"
    assert meta.prompt_price_per_m is None
    assert meta.completion_price_per_m is None
    assert meta.context_length == 64000


def test_catalog_check_tool_support_case_insensitive_operator_override(isolated_cache_path: Path):
    """TEST-CAT-26: Operator capability override enforced identically regardless of casing."""
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    # Operator policy: disable tools for claude-3.5-sonnet
    mock_provider.config = {
        "capabilities": {"anthropic/claude-3.5-sonnet": {"supports_tools": False}}
    }
    # Catalog claims it supports tools
    mock_model = ModelMetadata(
        id="anthropic/claude-3.5-sonnet",
        name="Claude 3.5 Sonnet",
        context_length=200000,
        supports_tools=True,
    )

    def fake_get_caps(slug):
        return mock_provider.config["capabilities"].get(slug.lower(), {})

    mock_provider.get_model_capabilities.side_effect = fake_get_caps

    catalog = ModelCatalog(
        provider_name="openrouter",
        provider=mock_provider,
        cache_file=str(isolated_cache_path),
    )
    catalog._models = {"anthropic/claude-3.5-sonnet": mock_model}
    catalog._loaded = True

    # Exact case check
    assert catalog.check_tool_support("anthropic/claude-3.5-sonnet") is False

    # Uppercase candidate check -> MUST also be False (parity with exact casing)
    assert catalog.check_tool_support("ANTHROPIC/CLAUDE-3.5-SONNET") is False


def test_catalog_fetch_from_provider_explicit_zdr_none(isolated_cache_path: Path):
    """TEST-CAT-27: Explicit zdr: None does not crash fetch_from_provider with AttributeError."""
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {"zdr": None}  # Explicit None in config
    mock_provider.resolve_api_key.return_value = ""
    mock_provider.fetch_catalog.return_value = [
        {"id": "meta/llama-3.1-8b:free", "name": "Free Llama"}
    ]
    mock_provider.get_model_capabilities.return_value = {}

    catalog = ModelCatalog(
        provider_name="openrouter",
        provider=mock_provider,
        cache_file=str(isolated_cache_path),
    )

    models = catalog.fetch_from_provider()
    assert len(models) == 1
    # Default fallback deny patterns [":free", "openrouter/free"] must be applied
    assert models[0].is_zdr_compliant is False


def test_catalog_stale_cache_fallback_corrupted_catalog(isolated_cache_path: Path):
    """TEST-CAT-28: Stale cache fallback with non-dict catalog returns empty dict safely."""
    cache_data = {
        "catalogs": {
            "https://openrouter.ai/api/v1": {
                "fetched_at": time.time() - (50 * 3600),  # expired
                "catalog": "corrupted_non_dict_payload",
            }
        }
    }
    isolated_cache_path.write_text(json.dumps(cache_data), encoding="utf-8")

    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.resolve_api_key.return_value = ""
    mock_provider.config = {}
    # Network failure
    mock_provider.fetch_catalog.side_effect = RuntimeError("Network unreachable")

    catalog = ModelCatalog(
        provider_name="openrouter",
        provider=mock_provider,
        cache_file=str(isolated_cache_path),
    )

    models = catalog.load()
    assert models == {}
    assert catalog._loaded is True


def test_catalog_single_pass_loaded_flag_prevents_repeated_reloads(isolated_cache_path: Path):
    """TEST-CAT-29: Verified _loaded flag prevents repeated network timeouts on empty catalog."""
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {}
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.resolve_api_key.return_value = ""
    # Provider returns empty list
    mock_provider.fetch_catalog.return_value = []

    catalog = ModelCatalog(
        provider_name="openrouter",
        provider=mock_provider,
        cache_file=str(isolated_cache_path),
    )

    # 1. Initial lookup triggers load()
    res = catalog.get_model("nonexistent-model")
    assert res is None
    assert mock_provider.fetch_catalog.call_count == 1

    # 2. Subsequent lookups MUST NOT trigger fetch_catalog again
    catalog.get_model("another-model")
    catalog.list_models()
    assert mock_provider.fetch_catalog.call_count == 1
