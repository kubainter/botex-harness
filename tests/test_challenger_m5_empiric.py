# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Milestone M5 Empirical Challenge & Verification Suite
=====================================================
Comprehensive adversarial tests for:
1. Cross-module workflows end-to-end:
   - Config loading with denylist -> resolve_model_candidates() -> model pruning -> engine fallback chain.
   - get_recommended_models() / recommend_models integration with catalog and denylist filtering.
   - Wallet credits queries across OpenRouter, NVIDIA, and generic providers with mock responses and offline caching.
2. 100% hermetic offline isolation under block_live_sockets (ensuring zero live network egress).
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import socket
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

import server
from botex.benchmarks import get_recommended_models
from botex.catalog import ModelCatalog, ModelMetadata
from botex.config import (
    DEFAULTS,
    _deep_merge,
    load_config,
    resolve_model_candidates,
)
from botex.denylist import DenylistFilter, _VALID_DECISION_MAP
from botex.engine import run_botex_task
from botex.providers import (
    BaseProvider,
    CreditsInfo,
    GenericOpenAIProvider,
    NormalizedResponse,
    NormalizedToolCall,
    NormalizedUsage,
    NvidiaProvider,
    OpenRouterProvider,
    ProviderRegistry,
)
from botex.wallet import (
    DEFAULT_WALLET_CACHE_TTL_S,
    WalletService,
    format_credits_summary,
    get_credits,
)


# ==============================================================================
# Fixtures: Hermetic Socket Isolation & Clean Environment
# ==============================================================================


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Ensure no live network calls can be made during test execution.

    Permits internal asyncio socketpair / loopback communication on Windows.
    """
    orig_connect = socket.socket.connect

    def blocked_connect(self: Any, *args: Any, **kwargs: Any) -> Any:
        import inspect

        frame = inspect.currentframe()
        try:
            curr = frame.f_back if frame else None
            while curr:
                if (
                    curr.f_code.co_name in ("_fallback_socketpair", "socketpair")
                    or "asyncio" in curr.f_code.co_filename
                ):
                    addr = args[0] if args else kwargs.get("address")
                    if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
                        return orig_connect(self, *args, **kwargs)
                curr = curr.f_back
        finally:
            del frame

        raise RuntimeError("Blocked live network socket call during unit test execution")

    with patch.object(socket.socket, "connect", blocked_connect):
        yield


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch):
    """Clear provider API keys from environment to enforce hermetic isolation."""
    for key in [
        "OPENROUTER_API_KEY",
        "NVIDIA_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_FREE_KEY",
        "CUSTOM_KEY",
        "NVAPI_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)


# ==============================================================================
# Mock Helpers for Async Client & URL Responses
# ==============================================================================


class FakeChoice:
    def __init__(self, message: Any, finish_reason: str = "stop") -> None:
        self.message = message
        self.finish_reason = finish_reason


class FakeMessage:
    def __init__(self, content: str = "", tool_calls: Optional[List[Any]] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class FakeUsage:
    def __init__(
        self, prompt_tokens: int = 10, completion_tokens: int = 10, total_tokens: int = 20
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class FakeCompletionResponse:
    def __init__(
        self,
        content: str = "",
        finish_reason: str = "stop",
        tool_calls: Optional[List[Any]] = None,
    ) -> None:
        self.id = "fake-resp-m5"
        self.choices = [FakeChoice(FakeMessage(content, tool_calls), finish_reason)]
        self.usage = FakeUsage()


class FakeClient:
    """Mock OpenAI-compatible async client capturing calls and returning queued responses."""

    def __init__(self, responses: Optional[List[Any]] = None) -> None:
        self.responses = list(responses or [])
        self.calls: List[Dict[str, Any]] = []
        self.chat = self

    @property
    def completions(self) -> Any:
        return self

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return FakeCompletionResponse(content="STATUS: DONE\nno more steps")


class FakeUrlopenResponse:
    """Mock for urllib.request.urlopen returning in-memory bytes."""

    def __init__(self, data: bytes, code: int = 200) -> None:
        self._io = io.BytesIO(data)
        self.code = code
        self.status = code

    def read(self) -> bytes:
        return self._io.read()

    def decode(self, encoding: str = "utf-8") -> str:
        return self._io.getvalue().decode(encoding)

    def __enter__(self) -> "FakeUrlopenResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def make_test_catalog_payload() -> Dict[str, Any]:
    """Sample OpenRouter /api/v1/models response payload."""
    return {
        "data": [
            {
                "id": "openai/gpt-4o",
                "name": "GPT-4o",
                "context_length": 128000,
                "pricing": {"prompt": "0.0000025", "completion": "0.000010"},
                "supported_parameters": ["tools", "temperature"],
            },
            {
                "id": "anthropic/claude-3.5-sonnet",
                "name": "Claude 3.5 Sonnet",
                "context_length": 200000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                "supported_parameters": ["tools", "temperature"],
            },
            {
                "id": "qwen/qwen-2.5-coder-32b",
                "name": "Qwen 2.5 Coder 32B",
                "context_length": 64000,
                "pricing": {"prompt": "0.0000008", "completion": "0.0000016"},
                "supported_parameters": ["tools"],
            },
            {
                "id": "deepseek/deepseek-chat",
                "name": "DeepSeek Chat V3",
                "context_length": 64000,
                "pricing": {"prompt": "0.00000014", "completion": "0.00000028"},
                "supported_parameters": ["tools"],
            },
            {
                "id": "meta-llama/llama-3.1-8b:free",
                "name": "Llama 3.1 8B Free",
                "context_length": 128000,
                "pricing": {"prompt": "0.0", "completion": "0.0"},
                "supported_parameters": ["tools"],
            },
            {
                "id": "nousresearch/nous-hermes-2",
                "name": "Nous Hermes 2 (No Tools)",
                "context_length": 32000,
                "pricing": {"prompt": "0.0000005", "completion": "0.000001"},
                "supported_parameters": ["temperature"],  # Missing "tools"
            },
        ]
    }


# ==============================================================================
# Test Group 1: Config Loading -> resolve_model_candidates() -> Fallback Chain
# ==============================================================================


def test_m5_flow_01_config_loading_with_complex_denylist_rules():
    """Verify layered config loading parses exact, glob, and regex denylist rules cleanly."""
    custom_cfg = {
        "engine": {
            "denylist": [
                "banned/exact-slug",
                "glob/*-preview",
                "re:^regex/v[0-9]+$",
            ],
        },
        "providers": {
            "openrouter": {
                "denylist": ["openrouter/blocked-*"],
                "profile_denylists": {
                    "fast": ["openrouter-fast/blocked"],
                    "coding": ["openrouter-coding/blocked"],
                },
            }
        },
    }
    merged = _deep_merge(DEFAULTS, custom_cfg)
    dfilter = DenylistFilter(merged)

    # Global rules
    assert not dfilter.is_allowed("banned/exact-slug")
    assert not dfilter.is_allowed("glob/test-preview")
    assert not dfilter.is_allowed("regex/v2")
    assert dfilter.is_allowed("regex/v2-beta")  # doesn't match $
    assert dfilter.is_allowed("allowed/clean-model")

    # Profile-specific rules under openrouter
    assert not dfilter.is_allowed("openrouter-fast/blocked", provider="openrouter", profile="fast")
    assert dfilter.is_allowed("openrouter-fast/blocked", provider="openrouter", profile="coding")
    assert not dfilter.is_allowed(
        "openrouter-coding/blocked", provider="openrouter", profile="coding"
    )
    assert dfilter.is_allowed("openrouter-coding/blocked", provider="openrouter", profile="fast")

    # Provider rules
    assert not dfilter.is_allowed("openrouter/blocked-model", provider="openrouter")
    assert dfilter.is_allowed("openrouter/blocked-model", provider="nvidia")


def test_m5_flow_02_resolve_model_candidates_multi_stage_pruning():
    """Verify resolve_model_candidates prunes models across provider fallbacks and tool capability."""
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["*deprecated*"]
    cfg["providers"]["openrouter"]["denylist"] = ["openrouter/banned-*"]
    cfg["providers"]["openrouter"]["models"]["coding"] = "openrouter/primary-candidate"
    cfg["providers"]["openrouter"]["model_fallbacks"]["coding"] = [
        "model-deprecated-v1",  # Pruned by global glob
        "openrouter/banned-model",  # Pruned by provider glob
        "qwen/qwen-2.5-coder-32b",  # Allowed
        "nousresearch/nous-hermes-2",  # Pruned when require_tools=True
        "deepseek/deepseek-chat",  # Allowed
    ]

    mock_catalog = SimpleNamespace(check_tool_support=lambda m: m != "nousresearch/nous-hermes-2")

    with patch("botex.config.load_config", return_value=cfg):
        candidates = resolve_model_candidates(
            "",
            profile="coding",
            provider="openrouter",
            require_tools=True,
            catalog=mock_catalog,
        )

    assert candidates == [
        "openrouter/primary-candidate",
        "qwen/qwen-2.5-coder-32b",
        "deepseek/deepseek-chat",
    ]


@pytest.mark.anyio
async def test_m5_flow_03_engine_fallback_chain_success_on_third_candidate(tmp_path: Path):
    """Verify engine transitions through candidates on recoverable errors until one succeeds."""
    cfg = copy.deepcopy(load_config())
    cfg["providers"]["openrouter"]["models"]["default"] = "cand/fail-429"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = [
        "cand/fail-context",
        "cand/succeed-done",
    ]

    # Candidate 1: Rate limit 429 error
    # Candidate 2: Unsupported / context error
    # Candidate 3: Success
    fake_client = FakeClient(
        [
            Exception("Rate limit 429: Too Many Requests"),
            FakeCompletionResponse("STATUS: UNSUPPORTED\ncontext length exceeded"),
            FakeCompletionResponse("STATUS: DONE\ntask completed successfully"),
        ]
    )

    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine.build_async_client", return_value=fake_client),
        patch("botex.engine.check_model_price", return_value=(True, "")),
        patch("botex.engine.check_model_tools", return_value=(True, "")),
    ):
        result = await run_botex_task(
            task="refactor code",
            workspace_dir=str(tmp_path),
            profile="default",
            mode="readonly",
            api_key="sk-test",
        )

    assert result["ok"] is True
    assert result["status"] == "DONE"
    assert len(result["attempts"]) == 3
    assert result["attempts"][0]["model"] == "cand/fail-429"
    assert result["attempts"][1]["model"] == "cand/fail-context"
    assert result["attempts"][2]["model"] == "cand/succeed-done"
    assert result["attempts"][2]["status"] == "DONE"
    # fallback_from records the immediately preceding candidate
    assert result["fallback_from"] == "cand/fail-context"


@pytest.mark.anyio
async def test_m5_flow_04_engine_fallback_halted_on_unrollbackable_exec(tmp_path: Path):
    """Verify fallback chain immediately halts when a candidate executed shell commands."""
    cfg = copy.deepcopy(load_config())
    cfg["providers"]["openrouter"]["models"]["default"] = "cand/dirty-exec"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = ["cand/never-reached"]

    # Candidate 1 executes a tool and returns ERROR with exec_ran=True
    async def mock_run_once(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {
            "ok": False,
            "status": "ERROR",
            "failure_kind": "execution_failed",
            "message": "command exited with code 1",
            "files_touched": [],
            "exec_ran": True,  # Unrollbackable mutation!
            "steps": 1,
            "duration_s": 0.5,
            "cost_usd": 0.01,
        }

    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine._run_botex_task_once", side_effect=mock_run_once),
    ):
        result = await run_botex_task(
            task="run build",
            workspace_dir=str(tmp_path),
            profile="default",
            mode="full",
            api_key="sk-test",
        )

    # Only candidate 1 should be attempted because exec_ran=True halts fallback
    assert result["ok"] is False
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["model"] == "cand/dirty-exec"


@pytest.mark.anyio
async def test_m5_flow_05_engine_clean_rejection_when_all_candidates_pruned(tmp_path: Path):
    """Verify engine returns structured CONFIG_ERROR without exceptions when all models are denylisted."""
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["*"]  # Ban everything

    fake_client = FakeClient()
    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine.build_async_client", return_value=fake_client),
    ):
        result = await run_botex_task(
            task="do anything",
            workspace_dir=str(tmp_path),
            mode="readonly",
            api_key="sk-test",
        )

    assert result["ok"] is False
    assert result["status"] == "CONFIG_ERROR"
    assert result["failure_kind"] == "security_or_policy_block"
    assert "No eligible model candidates" in result["message"]
    assert result["attempts"] == []
    assert len(fake_client.calls) == 0


# ==============================================================================
# Test Group 2: get_recommended_models() / recommend_models Integration
# ==============================================================================


def test_m5_rec_01_recommend_models_with_denylist_filtering():
    """Verify get_recommended_models applies denylist rules against provider catalog."""
    catalog_bytes = json.dumps(make_test_catalog_payload()).encode("utf-8")
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["qwen/*", "deepseek/*"]
    dfilter = DenylistFilter(cfg)

    with patch("urllib.request.urlopen", return_value=FakeUrlopenResponse(catalog_bytes)):
        recs = get_recommended_models(
            task_type="coding",
            limit=5,
            denylist_filter=dfilter,
        )

    ids = [r["id"] for r in recs]
    assert "openai/gpt-4o" in ids or "anthropic/claude-3.5-sonnet" in ids
    assert "qwen/qwen-2.5-coder-32b" not in ids
    assert "deepseek/deepseek-chat" not in ids
    assert "meta-llama/llama-3.1-8b:free" not in ids  # excluded by default ZDR


def test_m5_rec_02_recommend_models_require_tools_parameter():
    """Verify get_recommended_models filters models lacking tool-calling capability."""
    catalog_bytes = json.dumps(make_test_catalog_payload()).encode("utf-8")

    with patch("urllib.request.urlopen", return_value=FakeUrlopenResponse(catalog_bytes)):
        recs = get_recommended_models(
            task_type="coding",
            limit=10,
            include_free=True,
            require_tools=True,
        )

    ids = [r["id"] for r in recs]
    assert "nousresearch/nous-hermes-2" not in ids


def test_m5_rec_03_recommend_models_upstream_error_resilience():
    """Verify get_recommended_models returns structured error object on network failure."""
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("Simulated connection timeout"),
    ):
        recs = get_recommended_models(task_type="coding")

    assert len(recs) == 1
    assert "error" in recs[0]
    assert "Failed to fetch models from OpenRouter" in recs[0]["error"]


@pytest.mark.anyio
async def test_m5_rec_04_server_mcp_tool_recommend_models_returns_valid_json():
    """Verify server.py recommend_models MCP tool returns JSON string formatted properly."""
    catalog_bytes = json.dumps(make_test_catalog_payload()).encode("utf-8")

    with patch("urllib.request.urlopen", return_value=FakeUrlopenResponse(catalog_bytes)):
        raw_json = await server.recommend_models(task_type="coding", limit=3)

    parsed = json.loads(raw_json)
    assert isinstance(parsed, list)
    assert len(parsed) <= 3
    for entry in parsed:
        assert "id" in entry
        assert "pricing" in entry


# ==============================================================================
# Test Group 3: Wallet Credits Queries & Offline Caching
# ==============================================================================


def test_m5_wal_01_openrouter_credits_parsing_and_mocked_query():
    """Verify OpenRouterProvider queries /auth/key and /credits and parses balance correctly."""
    key_resp = json.dumps(
        {
            "data": {
                "label": "Test Key",
                "usage": 4.50,
                "limit": 50.00,
                "limit_remaining": 45.50,
                "is_free_tier": False,
            }
        }
    ).encode("utf-8")

    credits_resp = json.dumps(
        {
            "data": {
                "total_credits": 100.00,
                "total_usage": 4.50,
            }
        }
    ).encode("utf-8")

    def mock_urlopen(req: Any, *args: Any, **kwargs: Any) -> Any:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/auth/key" in url:
            return FakeUrlopenResponse(key_resp)
        if "/credits" in url:
            return FakeUrlopenResponse(credits_resp)
        raise ValueError(f"Unexpected URL: {url}")

    prov = OpenRouterProvider(name="openrouter")
    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        info = prov.get_credits(api_key="sk-or-test-key")

    assert info.supported is True
    assert info.provider == "openrouter"
    assert info.balance_usd == 95.50  # 100.00 - 4.50
    assert info.usage_usd == 4.50
    assert info.limit_usd == 50.00
    assert info.limit_remaining_usd == 45.50
    assert info.is_free_tier is False
    assert info.label == "Test Key"


def test_m5_wal_02_wallet_service_ttl_caching_and_invalidation():
    """Verify WalletService caches credits response and invalidates properly."""
    sample_info = CreditsInfo(
        supported=True,
        provider="openrouter",
        balance_usd=25.00,
        usage_usd=5.00,
    )

    ws = WalletService(cache_ttl_seconds=60.0)
    mock_prov = SimpleNamespace(
        name="openrouter",
        resolve_api_key=lambda *a, **k: "sk-cached-key",
        get_credits=patch.object(OpenRouterProvider, "get_credits", return_value=sample_info),
    )

    with patch.object(ws.registry, "get", return_value=mock_prov):
        with patch.object(mock_prov, "get_credits", return_value=sample_info) as mock_gc:
            # Call 1: Fetch and cache
            res1 = ws.get_credits(provider="openrouter", api_key="sk-cached-key")
            assert res1.balance_usd == 25.00
            assert mock_gc.call_count == 1

            # Call 2: Should hit cache, zero network calls
            res2 = ws.get_credits(provider="openrouter", api_key="sk-cached-key")
            assert res2.balance_usd == 25.00
            assert mock_gc.call_count == 1

            # Call 3: force_refresh=True should bypass cache
            res3 = ws.get_credits(
                provider="openrouter", api_key="sk-cached-key", force_refresh=True
            )
            assert res3.balance_usd == 25.00
            assert mock_gc.call_count == 2

            # Call 4: After invalidate(), should fetch again
            ws.invalidate("openrouter", "sk-cached-key")
            res4 = ws.get_credits(provider="openrouter", api_key="sk-cached-key")
            assert res4.balance_usd == 25.00
            assert mock_gc.call_count == 3


def test_m5_wal_03_nvidia_and_generic_provider_wallet_handling():
    """Verify NVIDIA NIM and GenericOpenAI providers return supported=False with zero network egress."""
    nvidia_prov = NvidiaProvider(name="nvidia")
    nv_info = nvidia_prov.get_credits()
    assert nv_info.supported is False
    assert nv_info.provider == "nvidia"
    assert "No Credits API" in str(nv_info.label)

    generic_prov = GenericOpenAIProvider(name="generic")
    gen_info = generic_prov.get_credits()
    assert gen_info.supported is False
    assert gen_info.provider == "generic"


def test_m5_wal_04_unregistered_provider_returns_safe_credits_info():
    """Verify querying an unregistered provider returns a clean CreditsInfo error object."""
    info = get_credits("non_existent_provider_xyz")
    assert info.supported is False
    assert info.label == "Unknown provider"
    assert info.error == "Provider not registered"


# ==============================================================================
# Test Group 4: 100% Hermetic Isolation Under block_live_sockets
# ==============================================================================


def test_m5_iso_01_block_live_sockets_prevents_raw_socket_egress():
    """Verify block_live_sockets intercepts and blocks socket.connect to external hosts."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="Blocked live network socket call"):
            s.connect(("openrouter.ai", 443))
    finally:
        s.close()


def test_m5_iso_02_block_live_sockets_prevents_unmocked_urllib_request():
    """Verify block_live_sockets blocks unmocked urllib requests."""
    req = urllib.request.Request("https://openrouter.ai/api/v1/models")
    with pytest.raises((RuntimeError, urllib.error.URLError)):
        urllib.request.urlopen(req, timeout=1.0)


def test_m5_iso_03_asyncio_loopback_socketpair_allowed():
    """Verify internal asyncio loopback/socketpair is permitted on Windows."""

    async def sample_async_task() -> str:
        await asyncio.sleep(0.01)
        return "loopback-ok"

    res = asyncio.run(sample_async_task())
    assert res == "loopback-ok"


# ==============================================================================
# Test Group 5: Adversarial Boundary Stress (Denylist, Catalog, Wallet)
# ==============================================================================


def test_m5_flow_06_malformed_and_boundary_denylist_patterns():
    """Verify DenylistFilter gracefully handles malformed regex, nulls, and boundary patterns."""
    cfg = {
        "engine": {
            "denylist": [
                "",
                "   ",
                None,
                12345,
                "re:[invalid(unclosed-group",  # Invalid regex
                "re:" + "a" * 300,  # Regex exceeding _MAX_REGEX_LEN
                "valid/exact-model",
            ]
        }
    }
    dfilter = DenylistFilter(cfg)

    # Malformed patterns should be safely ignored
    assert dfilter.is_allowed("any/other-model")
    assert not dfilter.is_allowed("valid/exact-model")

    # Candidate filtering with dirty inputs
    raw_list = ["", "  ", "valid/exact-model", "clean/model-1"]
    pruned = dfilter.filter_candidates(raw_list)
    assert pruned == ["clean/model-1"]


def test_m5_flow_07_exact_matching_vs_prefix_isolation():
    """Verify exact denylist rules isolate target slugs without blocking prefix matches."""
    cfg = {
        "engine": {
            "denylist": [
                "openai/gpt-4o",  # Exact slug
            ]
        }
    }
    dfilter = DenylistFilter(cfg)

    # Exact matches are blocked
    assert not dfilter.is_allowed("openai/gpt-4o")
    assert not dfilter.is_allowed("OPENAI/GPT-4O")  # Case insensitive
    assert not dfilter.is_allowed("  openai/gpt-4o  ")  # Stripped

    # Prefixes / suffixes are NOT blocked by exact rules
    assert dfilter.is_allowed("openai/gpt-4o-mini")
    assert dfilter.is_allowed("openai/gpt-4o-2024-08-06")
    assert dfilter.is_allowed("custom/openai/gpt-4o")


def test_m5_cat_01_catalog_offline_stale_fallback_on_network_failure(tmp_path: Path):
    """Verify ModelCatalog loads stale cache file and serves metadata when provider fetch raises error."""
    cache_file = tmp_path / "test_pricing_cache.json"
    cache_data = {
        "catalogs": {
            "https://openrouter.ai/api/v1": {
                "fetched_at": 1000.0,  # Far in the past (expired)
                "models": {
                    "openai/gpt-4o": {
                        "id": "openai/gpt-4o",
                        "name": "GPT-4o",
                        "context_length": 128000,
                        "prompt_price_per_m": 2.5,
                        "completion_price_per_m": 10.0,
                        "supports_tools": True,
                        "is_zdr_compliant": True,
                    }
                },
            }
        }
    }
    cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

    from unittest.mock import MagicMock

    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.name = "openrouter"
    mock_provider.base_url = "https://openrouter.ai/api/v1"
    mock_provider.config = {}
    mock_provider.get_model_capabilities.return_value = {}
    mock_provider.resolve_api_key.return_value = None
    mock_provider.fetch_catalog.side_effect = urllib.error.URLError("Network unreachable")

    catalog = ModelCatalog(
        provider=mock_provider,
        cache_file=cache_file,
        ttl_hours=0.0001,  # Force immediate expiration
    )

    model = catalog.get_model("openai/gpt-4o")
    assert model is not None
    assert model.id == "openai/gpt-4o"
    assert model.supports_tools is True
    assert catalog.check_tool_support("openai/gpt-4o") is True


def test_m5_cat_03_empirical_vulnerability_baseprovider_swallowing_error_wipes_stale_cache(
    tmp_path: Path,
):
    """Verify BaseProvider.fetch_catalog propagates network errors and ModelCatalog retains stale cache."""
    cache_file = tmp_path / "test_pricing_cache_vuln.json"
    cache_data = {
        "catalogs": {
            "https://openrouter.ai/api/v1": {
                "fetched_at": 1000.0,
                "catalog": {
                    "openai/gpt-4o": {
                        "id": "openai/gpt-4o",
                        "name": "GPT-4o",
                    }
                },
            }
        }
    }
    cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

    # Real OpenRouterProvider with unmocked fetch_catalog
    prov = OpenRouterProvider(name="openrouter")
    catalog = ModelCatalog(
        provider=prov,
        cache_file=cache_file,
        ttl_hours=0.0001,
    )

    # When network fails, BaseProvider.fetch_catalog propagates URLError
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("DNS resolution failed"),
    ):
        # 1. BaseProvider.fetch_catalog propagates network error instead of swallowing
        with pytest.raises(urllib.error.URLError):
            prov.fetch_catalog()

        # 2. ModelCatalog.load() catches network failure and preserves stale cache
        loaded = catalog.load()
        assert "openai/gpt-4o" in loaded
        assert catalog.get_model("openai/gpt-4o") is not None
        assert catalog.get_model("openai/gpt-4o").name == "GPT-4o"


def test_m5_cat_02_catalog_tool_support_precedence_with_operator_override():
    """Verify operator capability overrides take precedence over catalog tool support."""
    # Catalog says supports_tools=False, but config capabilities says tools=True
    provider_cfg = {
        "capabilities": {
            "custom/override-model": {"tools": True},
        }
    }
    catalog = ModelCatalog(
        provider_name="openrouter",
        provider_cfg=provider_cfg,
    )
    # Manually inject model with supports_tools=False
    catalog._models["custom/override-model"] = ModelMetadata(
        id="custom/override-model",
        supports_tools=False,
    )
    catalog._loaded = True

    # Operator override takes precedence
    assert catalog.check_tool_support("custom/override-model") is True


def test_m5_cat_04_provider_returning_empty_list_preserves_stale_cache_and_disk_file(
    tmp_path: Path,
):
    """Verify that if a provider legitimately returns empty catalog [], ModelCatalog retains stale cache and does not wipe disk."""
    cache_file = tmp_path / "test_pricing_cache_empty_ret.json"
    cache_data = {
        "catalogs": {
            "https://openrouter.ai/api/v1": {
                "fetched_at": 1000.0,
                "catalog": {
                    "anthropic/claude-3-opus": {
                        "id": "anthropic/claude-3-opus",
                        "name": "Claude 3 Opus",
                    }
                },
            }
        }
    }
    initial_content = json.dumps(cache_data, indent=2)
    cache_file.write_text(initial_content, encoding="utf-8")

    prov = OpenRouterProvider(name="openrouter")
    catalog = ModelCatalog(
        provider=prov,
        cache_file=cache_file,
        ttl_hours=0.0001,
    )

    with patch.object(prov, "fetch_catalog", return_value=[]):
        loaded = catalog.load()
        assert "anthropic/claude-3-opus" in loaded
        assert catalog.get_model("anthropic/claude-3-opus").name == "Claude 3 Opus"
        # Verify file on disk was NOT overwritten or wiped
        assert cache_file.read_text(encoding="utf-8") == initial_content

        # Also verify force_refresh=True retains stale cache and disk file
        catalog_fr = ModelCatalog(
            provider=prov,
            cache_file=cache_file,
            ttl_hours=0.0001,
        )
        loaded_fr = catalog_fr.load(force_refresh=True)
        assert "anthropic/claude-3-opus" in loaded_fr
        assert cache_file.read_text(encoding="utf-8") == initial_content


def test_m5_cat_05_baseprovider_propagates_httperror_and_timeout_cleanly():
    """Verify BaseProvider.fetch_catalog propagates HTTPError and TimeoutError without swallowing."""
    prov = BaseProvider(name="test_prov", config={"base_url": "https://api.test/v1"})

    # 1. HTTPError 502 Bad Gateway
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            url="https://api.test/v1/models",
            code=502,
            msg="Bad Gateway",
            hdrs={},
            fp=None,
        ),
    ):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            prov.fetch_catalog()
        assert exc_info.value.code == 502

    # 2. TimeoutError
    with patch(
        "urllib.request.urlopen",
        side_effect=TimeoutError("Request timed out after 10s"),
    ):
        with pytest.raises(TimeoutError) as exc_info_to:
            prov.fetch_catalog()
        assert "timed out" in str(exc_info_to.value)


def test_m5_cat_06_cache_dual_key_support_matrix_catalog_and_models(tmp_path: Path):
    """Verify ModelCatalog seamlessly handles both 'catalog' and 'models' keys under all states."""
    prov = OpenRouterProvider(name="openrouter")
    base_url = "https://openrouter.ai/api/v1"

    for key in ["catalog", "models"]:
        cache_file = tmp_path / f"cache_{key}.json"
        cache_data = {
            "catalogs": {
                base_url: {
                    "fetched_at": 1000.0,
                    key: {
                        f"test/{key}-model": {
                            "id": f"test/{key}-model",
                            "name": f"Test {key} Model",
                        }
                    },
                }
            }
        }
        cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        # Fallback on network error
        cat_net = ModelCatalog(provider=prov, cache_file=cache_file, ttl_hours=0.0001)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("fail")):
            res = cat_net.load()
            assert f"test/{key}-model" in res

        # Fallback on empty return []
        cat_empty = ModelCatalog(provider=prov, cache_file=cache_file, ttl_hours=0.0001)
        with patch.object(prov, "fetch_catalog", return_value=[]):
            res2 = cat_empty.load()
            assert f"test/{key}-model" in res2


def test_m5_wal_05_wallet_partial_failure_handling():
    """Verify OpenRouterProvider returns supported=True with partial data when one endpoint fails."""
    key_resp = json.dumps(
        {
            "data": {
                "label": "Prod Key",
                "usage": 10.00,
                "limit": 100.00,
                "limit_remaining": 90.00,
                "is_free_tier": False,
            }
        }
    ).encode("utf-8")

    def mock_urlopen(req: Any, *args: Any, **kwargs: Any) -> Any:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/auth/key" in url:
            return FakeUrlopenResponse(key_resp)
        if "/credits" in url:
            # Simulate upstream 500 error on /credits endpoint
            raise urllib.error.HTTPError(
                url=url, code=500, msg="Internal Server Error", hdrs={}, fp=None
            )
        raise ValueError(f"Unexpected URL: {url}")

    prov = OpenRouterProvider(name="openrouter")
    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        info = prov.get_credits(api_key="sk-test-partial")

    # Partial success: key data is parsed, credits error is noted
    assert info.supported is True
    assert info.limit_usd == 100.00
    assert info.limit_remaining_usd == 90.00
    assert info.balance_usd is None
    assert "credits HTTP 500" in str(info.error)


def test_m5_wal_06_secret_masking_deep_scrubbing_under_adversarial_payload():
    """Verify sensitive tokens are completely redacted from debug payloads and summaries."""
    from botex.wallet import _sanitize_raw_payload

    dirty_payload = {
        "api_key": "sk-secret-12345",
        "nested": {
            "bearer_token": "bearer-token-abcde",
            "password": "super-secret-pass",
            "user_data": {
                "auth": "auth-token-xyz",
                "normal_field": "safe-value",
            },
        },
        "list_items": [
            {"token": "tok-999"},
            "regular string with sk-or-v1-abcdef123456 embedded",
            "regular string with sk-or-v1-1234567890abcdef1234567890 embedded",
        ],
    }

    cleaned = _sanitize_raw_payload(dirty_payload)
    clean_json = json.dumps(cleaned)

    assert "sk-secret-12345" not in clean_json
    assert "bearer-token-abcde" not in clean_json
    assert "super-secret-pass" not in clean_json
    assert "auth-token-xyz" not in clean_json
    assert "tok-999" not in clean_json
    assert "sk-or-v1-abcdef123456" not in clean_json
    assert "sk-or-v1-1234567890abcdef1234567890" not in clean_json
    assert "safe-value" in clean_json
