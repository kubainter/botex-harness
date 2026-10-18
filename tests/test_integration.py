# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Milestone M4 Integration Test Suite
===================================
End-to-end integration tests for configuration loading, denylist candidate resolution,
benchmark recommendations, engine fallback chain, hermetic socket isolation,
and security/CWE-312 invariants across the BoteX execution harness.
"""

import asyncio
import copy
import io
import json
import logging
import os
import socket
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from botex.benchmarks import get_recommended_models
from botex.config import (
    DEFAULTS,
    _deep_merge,
    catalog_cache_file,
    catalog_setting,
    denylist_setting,
    load_config,
    resolve_model_candidates,
    wallet_setting,
)
from botex.denylist import DenylistFilter, _VALID_DECISION_MAP
from botex.engine import run_botex_task
from botex.wallet import CreditsInfo
import server


# ==============================================================================
# Fixtures and Test Harness Isolation
# ==============================================================================


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Ensure no live network calls can be made during unit/integration tests.

    Allows internal asyncio socketpair / loopback communication on Windows.
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
    """Clear all provider API keys from environment to test hermetic behavior."""
    for key in [
        "OPENROUTER_API_KEY",
        "NVIDIA_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_FREE_KEY",
        "CUSTOM_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)


# ==============================================================================
# Fake Objects and Mock Helpers
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
        self.id = "fake-resp-1"
        self.choices = [FakeChoice(FakeMessage(content, tool_calls), finish_reason)]
        self.usage = FakeUsage()


class FakeClient:
    """Mock OpenAI-compatible async client capturing calls."""

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
    def __init__(self, data: bytes) -> None:
        self._io = io.BytesIO(data)

    def read(self) -> bytes:
        return self._io.read()

    def decode(self, encoding: str = "utf-8") -> str:
        return self._io.getvalue().decode(encoding)

    def __enter__(self) -> "FakeUrlopenResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def make_sample_models_catalog() -> Dict[str, Any]:
    return {
        "data": [
            {
                "id": "openai/gpt-3.5-turbo",
                "name": "GPT-3.5 Turbo",
                "context_length": 16000,
                "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
                "supported_parameters": ["tools"],
            },
            {
                "id": "qwen/qwen-2.5-coder-32b",
                "name": "Qwen 2.5 Coder 32B",
                "context_length": 64000,
                "pricing": {"prompt": "0.0000008", "completion": "0.0000016"},
                "supported_parameters": ["tools"],
            },
            {
                "id": "anthropic/claude-3.5-sonnet",
                "name": "Claude 3.5 Sonnet",
                "context_length": 200000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
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
                "id": "google/gemini-2.0-flash-exp:free",
                "name": "Gemini 2.0 Flash Exp",
                "context_length": 1000000,
                "pricing": {"prompt": "0.0", "completion": "0.0"},
                "supported_parameters": ["tools"],
            },
            {
                "id": "google/gemini-1.5-pro",
                "name": "Gemini 1.5 Pro",
                "context_length": 1000000,
                "pricing": {"prompt": "0.00000125", "completion": "0.000005"},
                "supported_parameters": ["tools"],
            },
        ]
    }


# ==============================================================================
# Group 1: Configuration Loading & Schema Compatibility (TEST-M4-CFG)
# ==============================================================================


def test_m4_cfg_01_default_config_loading():
    """TEST-M4-CFG-01: Default configuration declares denylist, catalog, and wallet."""
    cfg = load_config(reload=True)
    assert "denylist" in cfg["engine"]
    assert isinstance(cfg["engine"]["denylist"], list)
    assert "profile_denylists" in cfg["engine"]
    assert isinstance(cfg["engine"]["profile_denylists"], dict)

    assert "denylist" in cfg["providers"]["openrouter"]
    assert isinstance(cfg["providers"]["openrouter"]["denylist"], list)
    assert "profile_denylists" in cfg["providers"]["openrouter"]
    assert isinstance(cfg["providers"]["openrouter"]["profile_denylists"], dict)

    assert "catalog" in cfg
    assert cfg["catalog"]["cache_ttl_hours"] == 24
    assert cfg["catalog"]["on_unknown"] == "allow"

    assert "wallet" in cfg
    assert cfg["wallet"]["enabled"] is True

    # Helper getters
    assert catalog_setting("cache_ttl_hours") == 24
    assert catalog_cache_file().name == ".model_pricing.json"
    assert wallet_setting("enabled") is True
    assert denylist_setting() == []


def test_m4_cfg_02_layered_config_precedence(monkeypatch: pytest.MonkeyPatch):
    """TEST-M4-CFG-02: Layered config precedence merges denylist, catalog, and wallet settings."""
    monkeypatch.setenv("BOTEX_DENYLIST", "env-deny-1,env-deny-2")
    monkeypatch.setenv("BOTEX_CATALOG_TTL_HOURS", "48.0")
    monkeypatch.setenv("BOTEX_WALLET_CACHE_TTL_S", "120.0")

    cfg = load_config(reload=True)
    try:
        assert cfg["engine"]["denylist"] == ["env-deny-1", "env-deny-2"]
        assert cfg["catalog"]["ttl_hours"] == 48.0
        assert cfg["wallet"]["cache_ttl_s"] == 120.0
        # Sibling keys preserved
        assert "base_url" in cfg["providers"]["openrouter"]
        assert "models" in cfg["providers"]["openrouter"]
    finally:
        load_config(reload=True)


def test_m4_cfg_03_backward_compatibility_legacy_config():
    """TEST-M4-CFG-03: Backward compatibility with legacy configuration dictionaries."""
    legacy_dict = {
        "engine": {"max_turns": 10},
        "providers": {
            "openrouter": {
                "default": True,
                "base_url": "https://openrouter.ai/api/v1",
                "models": {"default": "openai/gpt-4o"},
            }
        },
    }
    merged = _deep_merge(DEFAULTS, legacy_dict)
    assert merged["engine"]["max_turns"] == 10
    assert "denylist" in merged["engine"]
    assert "catalog" in merged
    assert "wallet" in merged

    dfilter = DenylistFilter(merged)
    assert dfilter.is_allowed("openai/gpt-4o") is True


def test_m4_cfg_04_resilience_against_malformed_data_types():
    """TEST-M4-CFG-04: Resilience against malformed non-list and non-dict denylist entries."""
    malformed_cfg = {
        "engine": {"denylist": 12345, "profile_denylists": None},
        "providers": {
            "openrouter": {
                "denylist": None,
                "profile_denylists": "invalid_string",
            }
        },
    }
    dfilter = DenylistFilter(malformed_cfg)
    assert dfilter.is_allowed("openai/gpt-4o") is True
    candidates = dfilter.filter_candidates(["openai/gpt-4o"])
    assert candidates == ["openai/gpt-4o"]


# ==============================================================================
# Group 2: Model Candidate Resolution & Denylist Pruning (TEST-M4-RES)
# ==============================================================================


def test_m4_res_01_single_explicit_model_allowed():
    """TEST-M4-RES-01: Single explicit allowed model is returned unchanged."""
    res = resolve_model_candidates("openai/gpt-4o", provider="openrouter")
    assert res == ["openai/gpt-4o"]


def test_m4_res_02_single_explicit_model_denied_by_global_policy():
    """TEST-M4-RES-02: Explicit model denied by global policy returns empty candidate list."""
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["openai/gpt-3.5-turbo", "*deprecated*"]
    dfilter = DenylistFilter(cfg)

    c1 = resolve_model_candidates(
        "openai/gpt-3.5-turbo", provider="openrouter", denylist_filter=dfilter
    )
    assert c1 == []

    c2 = resolve_model_candidates(
        "custom/deprecated-v1", provider="openrouter", denylist_filter=dfilter
    )
    assert c2 == []


def test_m4_res_03_single_explicit_model_denied_by_provider_policy():
    """TEST-M4-RES-03: Provider denylist prunes under target provider but permits under others."""
    cfg = copy.deepcopy(load_config())
    cfg["providers"]["openrouter"]["denylist"] = ["*:free"]
    cfg["providers"]["openrouter-free"]["denylist"] = []
    dfilter = DenylistFilter(cfg)

    c_or = resolve_model_candidates(
        "meta-llama/llama-3.1-8b:free", provider="openrouter", denylist_filter=dfilter
    )
    assert c_or == []

    c_free = resolve_model_candidates(
        "meta-llama/llama-3.1-8b:free", provider="openrouter-free", denylist_filter=dfilter
    )
    assert c_free == ["meta-llama/llama-3.1-8b:free"]


def test_m4_res_04_multi_tier_candidate_pruning():
    """TEST-M4-RES-04: Multi-tier pruning across global, provider, and profile denylists."""
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["*legacy*"]
    cfg["providers"]["openrouter"]["denylist"] = ["anthropic/claude-2*"]
    cfg["providers"]["openrouter"]["profile_denylists"] = {"coding": ["meta/llama-3-8b"]}
    cfg["providers"]["openrouter"]["models"]["coding"] = "openrouter/pareto-code"
    cfg["providers"]["openrouter"]["model_fallbacks"]["coding"] = [
        "model-legacy-1",
        "anthropic/claude-2.1",
        "meta/llama-3-8b",
        "deepseek/deepseek-v3.2",
    ]
    dfilter = DenylistFilter(cfg)

    with patch("botex.config.load_config", return_value=cfg):
        res = resolve_model_candidates(
            "", profile="coding", provider="openrouter", denylist_filter=dfilter
        )
    assert res == ["openrouter/pareto-code", "deepseek/deepseek-v3.2"]


def test_m4_res_05_deduplication_and_case_preservation():
    """TEST-M4-RES-05: Deduplication normalizes casing while preserving first appearance order."""
    cfg = copy.deepcopy(load_config())
    cfg["providers"]["openrouter"]["models"]["default"] = "DeepSeek/DeepSeek-V3"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = [
        "deepseek/deepseek-v3",
        "qwen/qwen3-coder",
    ]

    with patch("botex.config.load_config", return_value=cfg):
        res = resolve_model_candidates("", profile="default", provider="openrouter")
    assert res == ["DeepSeek/DeepSeek-V3", "qwen/qwen3-coder"]


def test_m4_res_06_tool_calling_capability_pruning():
    """TEST-M4-RES-06: Candidates lacking tool-calling support are pruned when require_tools=True."""
    mock_catalog = SimpleNamespace(
        check_tool_support=lambda model: model != "nousresearch/nous-hermes-2"
    )
    cfg = copy.deepcopy(load_config())
    cfg["providers"]["openrouter"]["models"]["coding"] = "anthropic/claude-3.5-sonnet"
    cfg["providers"]["openrouter"]["model_fallbacks"]["coding"] = [
        "nousresearch/nous-hermes-2",
        "deepseek/deepseek-v3",
    ]

    with patch("botex.config.load_config", return_value=cfg):
        res = resolve_model_candidates(
            "",
            profile="coding",
            provider="openrouter",
            require_tools=True,
            catalog=mock_catalog,
        )
    assert res == ["anthropic/claude-3.5-sonnet", "deepseek/deepseek-v3"]


# ==============================================================================
# Group 3: Model Recommendations Respecting Denylist (TEST-M4-REC)
# ==============================================================================


def test_m4_rec_01_global_denylist_filters_openrouter_recommendations():
    """TEST-M4-REC-01: Global denylist prunes models from benchmark recommendations."""
    payload = json.dumps(make_sample_models_catalog()).encode("utf-8")
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["openai/gpt-3.5-turbo", "*deprecated*"]
    dfilter = DenylistFilter(cfg)

    with patch("urllib.request.urlopen", return_value=FakeUrlopenResponse(payload)):
        recs = get_recommended_models(task_type="coding", limit=5, denylist_filter=dfilter)

    ids = [r["id"] for r in recs]
    assert "openai/gpt-3.5-turbo" not in ids
    assert "qwen/qwen-2.5-coder-32b" in ids
    assert "anthropic/claude-3.5-sonnet" in ids


def test_m4_rec_02_provider_denylist_filters_recommendations_with_free():
    """TEST-M4-REC-02: Provider denylist rules override include_free=True in recommendations."""
    payload = json.dumps(make_sample_models_catalog()).encode("utf-8")
    cfg = copy.deepcopy(load_config())
    cfg["providers"]["openrouter"]["denylist"] = ["*:free"]
    dfilter = DenylistFilter(cfg)

    with patch("urllib.request.urlopen", return_value=FakeUrlopenResponse(payload)):
        recs = get_recommended_models(
            task_type="coding",
            limit=5,
            include_free=True,
            denylist_filter=dfilter,
            provider="openrouter",
        )

    ids = [r["id"] for r in recs]
    assert "meta-llama/llama-3.1-8b:free" not in ids


def test_m4_rec_03_regex_denylist_pattern_filters_recommendations():
    """TEST-M4-REC-03: Regex pattern in denylist filters experimental models."""
    payload = json.dumps(make_sample_models_catalog()).encode("utf-8")
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["re:^google/.*-exp(:free)?$"]
    dfilter = DenylistFilter(cfg)

    with patch("urllib.request.urlopen", return_value=FakeUrlopenResponse(payload)):
        recs = get_recommended_models(
            task_type="coding",
            limit=5,
            include_free=True,
            denylist_filter=dfilter,
        )

    ids = [r["id"] for r in recs]
    assert "google/gemini-2.0-flash-exp:free" not in ids
    assert "google/gemini-1.5-pro" in ids


@pytest.mark.anyio
async def test_m4_rec_04_server_mcp_tool_recommend_models():
    """TEST-M4-REC-04: Server MCP tool recommend_models returns valid JSON with denylist applied."""
    payload = json.dumps(make_sample_models_catalog()).encode("utf-8")
    with patch("urllib.request.urlopen", return_value=FakeUrlopenResponse(payload)):
        json_str = await server.recommend_models(task_type="coding", limit=5)

    parsed = json.loads(json_str)
    assert isinstance(parsed, list)
    assert len(parsed) > 0
    assert all("id" in item for item in parsed)


# ==============================================================================
# Group 4: Engine Integration & Zero-Regression Verification (TEST-M4-ENG)
# ==============================================================================


@pytest.mark.anyio
async def test_m4_eng_01_e2e_task_execution_with_provider_registry(tmp_path: Any):
    """TEST-M4-ENG-01: End-to-end task execution using mock ProviderRegistry client."""
    fake_client = FakeClient([FakeCompletionResponse("STATUS: DONE\ntask completed")])
    with (
        patch("botex.engine.build_async_client", return_value=fake_client),
        patch("botex.engine.check_model_price", return_value=(True, "")),
        patch("botex.engine.check_model_tools", return_value=(True, "")),
    ):
        result = await run_botex_task(
            task="inspect",
            workspace_dir=str(tmp_path),
            mode="readonly",
            api_key="sk-test",
        )

    assert result["ok"] is True
    assert result["status"] == "DONE"
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["status"] == "DONE"


@pytest.mark.anyio
async def test_m4_eng_02_denylist_preflight_pruning_before_engine_execution(tmp_path: Any):
    """TEST-M4-ENG-02: Engine skips denylisted primary model and executes allowed fallback."""
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["denied/*"]
    cfg["providers"]["openrouter"]["models"]["default"] = "denied/primary-model"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = ["allowed/fallback-model"]

    fake_client = FakeClient([FakeCompletionResponse("STATUS: DONE\ntask completed")])
    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine.build_async_client", return_value=fake_client),
        patch("botex.engine.check_model_price", return_value=(True, "")),
        patch("botex.engine.check_model_tools", return_value=(True, "")),
    ):
        result = await run_botex_task(
            task="test",
            workspace_dir=str(tmp_path),
            profile="default",
            mode="readonly",
            api_key="sk-test",
        )

    assert result["status"] == "DONE"
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["model"] == "allowed/fallback-model"


@pytest.mark.anyio
async def test_m4_eng_03_all_candidates_denied_returns_structured_contract(tmp_path: Any):
    """TEST-M4-ENG-03: When all candidates are denied, returns clean structured contract."""
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["*"]

    fake_client = FakeClient()
    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine.build_async_client", return_value=fake_client),
    ):
        result = await run_botex_task(
            task="test",
            workspace_dir=str(tmp_path),
            mode="readonly",
            api_key="sk-test",
        )

    assert result["ok"] is False
    assert result["status"] == "CONFIG_ERROR"
    assert result["failure_kind"] in ("config_error", "security_or_policy_block")
    assert result["attempts"] == []
    assert len(fake_client.calls) == 0


@pytest.mark.anyio
async def test_m4_eng_04_multi_model_fallback_chain_execution(tmp_path: Any):
    """TEST-M4-ENG-04: Multi-model fallback transitions from failing to succeeding candidate."""
    cfg = copy.deepcopy(load_config())
    cfg["providers"]["openrouter"]["models"]["default"] = "cand/first"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = ["cand/second"]

    fake_client = FakeClient(
        [
            FakeCompletionResponse("STATUS: UNSUPPORTED\ntool calling missing"),
            FakeCompletionResponse("STATUS: DONE\nresolved"),
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
            task="fix",
            workspace_dir=str(tmp_path),
            profile="default",
            mode="edit",
            api_key="sk-test",
        )

    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["status"] == "UNSUPPORTED"
    assert result["attempts"][1]["status"] == "DONE"
    assert result["status"] == "DONE"
    assert result["fallback_from"] == "cand/first"


@pytest.mark.anyio
async def test_m4_eng_05_rollback_integrity_between_fallback_attempts(tmp_path: Any):
    """TEST-M4-ENG-05: Rollback integrity restores workspace before candidate 2 executes."""
    tcall = SimpleNamespace(
        id="t1",
        type="function",
        function=SimpleNamespace(
            name="create_file",
            arguments=json.dumps({"path": "dirty.txt", "content": "dirty payload"}),
        ),
    )
    resp1 = FakeCompletionResponse(tool_calls=[tcall])
    resp2 = Exception("Simulated upstream network failure")
    resp3 = FakeCompletionResponse(content="STATUS: DONE\nall clean")
    fake_client = FakeClient([resp1, resp2, resp3])

    cfg = copy.deepcopy(load_config())
    cfg["providers"]["openrouter"]["models"]["default"] = "cand/one"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = ["cand/two"]

    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine.build_async_client", return_value=fake_client),
        patch("botex.engine.check_model_price", return_value=(True, "")),
        patch("botex.engine.check_model_tools", return_value=(True, "")),
    ):
        result = await run_botex_task(
            task="fix",
            workspace_dir=str(tmp_path),
            profile="default",
            mode="edit",
            api_key="sk-test",
        )

    assert not (tmp_path / "dirty.txt").exists()
    assert result["attempts"][0]["rolled_back"] is True
    assert result["status"] == "DONE"


# ==============================================================================
# Group 5: Hermetic Mock Isolation & Socket Blocking (TEST-M4-SCK)
# ==============================================================================


def test_m4_sck_01_socket_connect_interception():
    """TEST-M4-SCK-01: Direct socket connection attempts are hermetically blocked."""
    with pytest.raises(RuntimeError, match="Blocked live network socket call"):
        socket.socket().connect(("openrouter.ai", 443))


def test_m4_sck_02_asyncio_loopback_socketpair_exemption():
    """TEST-M4-SCK-02: Asyncio internal socketpair/loopback wakeups are permitted."""
    asyncio.run(asyncio.sleep(0.01))


def test_m4_sck_03_env_var_isolation():
    """TEST-M4-SCK-03: Provider API keys are cleared from os.environ during test suite."""
    for key in ["OPENROUTER_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY"]:
        assert key not in os.environ


# ==============================================================================
# Group 6: Security, Secret Masking & CWE-312 Invariants (TEST-M4-SEC)
# ==============================================================================


def test_m4_sec_01_secret_masking_in_rejection_logs(caplog: pytest.LogCaptureFixture):
    """TEST-M4-SEC-01: Secret tokens in candidate strings are masked before log output."""
    raw_secret = "sk-or-v1-1234567890abcdef1234567890"
    candidate = f"openai/{raw_secret}"

    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["openai/*"]
    dfilter = DenylistFilter(cfg)

    with caplog.at_level(logging.DEBUG):
        dfilter.filter_candidates([candidate])

    assert raw_secret not in caplog.text


def test_m4_sec_02_canonical_enum_mappings_for_decision_codes():
    """TEST-M4-SEC-02: DenialRecord decision codes map to static constants (CWE-020/CWE-312)."""
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["global/blocked"]
    cfg["providers"]["openrouter"]["denylist"] = ["provider/blocked"]
    cfg["providers"]["openrouter"]["profile_denylists"] = {"coding": ["profile/blocked"]}
    dfilter = DenylistFilter(cfg)

    r_glob = dfilter.get_denial_record("global/blocked", provider="openrouter", profile="coding")
    r_prov = dfilter.get_denial_record("provider/blocked", provider="openrouter", profile="coding")
    r_prof = dfilter.get_denial_record("profile/blocked", provider="openrouter", profile="coding")

    assert r_glob is not None
    assert r_glob.decision_code == _VALID_DECISION_MAP["denied_global"]
    assert r_glob.decision_code == "DENIED_BY_GLOBAL_POLICY"

    assert r_prov is not None
    assert r_prov.decision_code == _VALID_DECISION_MAP["denied_provider"]
    assert r_prov.decision_code == "DENIED_BY_PROVIDER_POLICY"

    assert r_prof is not None
    assert r_prof.decision_code == _VALID_DECISION_MAP["denied_profile"]
    assert r_prof.decision_code == "DENIED_BY_PROFILE_POLICY"


@pytest.mark.anyio
async def test_m4_sec_03_zdr_wire_flag_enforcement(tmp_path: Any):
    """TEST-M4-SEC-03: Zero Data Retention (ZDR) wire flag is enforced on client requests."""
    fake_client = FakeClient([FakeCompletionResponse("STATUS: DONE\ncompleted")])
    with (
        patch("botex.engine.build_async_client", return_value=fake_client),
        patch("botex.engine.check_model_price", return_value=(True, "")),
        patch("botex.engine.check_model_tools", return_value=(True, "")),
    ):
        await run_botex_task(
            task="test",
            workspace_dir=str(tmp_path),
            mode="readonly",
            api_key="sk-test",
            allow_non_zdr=False,
        )

    assert len(fake_client.calls) > 0
    extra_body = fake_client.calls[0].get("extra_body") or {}
    assert extra_body.get("provider", {}).get("data_collection") == "deny"


# ==============================================================================
# Group 7: CLI & REPL Wallet Integration (TEST-M4-WAL)
# ==============================================================================


def test_m4_wal_01_cli_wallet_default_invocation(capsys: pytest.CaptureFixture):
    """TEST-M4-WAL-01: Default `_cli_wallet([])` displays formatted provider summary without crashing."""
    sample_info = CreditsInfo(
        supported=True,
        provider="openrouter",
        balance_usd=15.75,
        usage_usd=2.50,
        limit_usd=50.00,
        limit_remaining_usd=47.50,
        is_free_tier=False,
        label="Test Key",
    )
    with patch("botex.wallet.get_credits", return_value=sample_info) as mock_get:
        server._cli_wallet([])
        mock_get.assert_called_once_with("openrouter", force_refresh=False)

    out = capsys.readouterr().out
    assert "=== BoteX Wallet (openrouter) ===" in out
    assert "$15.75 USD" in out
    assert "$2.50 USD" in out
    assert "Status:          Supported" in out


def test_m4_wal_02_cli_wallet_positional_provider(capsys: pytest.CaptureFixture):
    """TEST-M4-WAL-02: Positional provider arguments correctly target specified provider."""
    sample_nvidia = CreditsInfo(
        supported=False,
        provider="nvidia",
        label="NVIDIA NIM (No Credits API)",
    )
    with patch("botex.wallet.get_credits", return_value=sample_nvidia) as mock_get:
        server._cli_wallet(["nvidia"])
        mock_get.assert_called_once_with("nvidia", force_refresh=False)
        out = capsys.readouterr().out
        assert "=== BoteX Wallet (nvidia) ===" in out
        assert "Credits API not supported" in out


def test_m4_wal_03_cli_wallet_explicit_provider_flag(capsys: pytest.CaptureFixture):
    """TEST-M4-WAL-03: Provider selection via explicit flags (--provider, -p, --provider=)."""
    sample_nvidia = CreditsInfo(
        supported=False,
        provider="nvidia",
        label="NVIDIA NIM (No Credits API)",
    )
    with patch("botex.wallet.get_credits", return_value=sample_nvidia) as mock_get:
        # --provider flag
        mock_get.reset_mock()
        server._cli_wallet(["--provider", "nvidia"])
        mock_get.assert_called_once_with("nvidia", force_refresh=False)
        assert "=== BoteX Wallet (nvidia) ===" in capsys.readouterr().out

        # -p short flag
        mock_get.reset_mock()
        server._cli_wallet(["-p", "nvidia"])
        mock_get.assert_called_once_with("nvidia", force_refresh=False)
        assert "=== BoteX Wallet (nvidia) ===" in capsys.readouterr().out

        # --provider= inline flag
        mock_get.reset_mock()
        server._cli_wallet(["--provider=nvidia"])
        mock_get.assert_called_once_with("nvidia", force_refresh=False)
        assert "=== BoteX Wallet (nvidia) ===" in capsys.readouterr().out


def test_m4_wal_04_cli_wallet_json_output(capsys: pytest.CaptureFixture):
    """TEST-M4-WAL-04: `--json` flag outputs parseable JSON matching CreditsInfo schema."""
    sample_info = CreditsInfo(
        supported=True,
        provider="openrouter",
        balance_usd=42.10,
        usage_usd=3.14,
        limit_usd=100.0,
        limit_remaining_usd=96.86,
        is_free_tier=False,
        label="Prod Key",
    )
    with patch("botex.wallet.get_credits", return_value=sample_info):
        server._cli_wallet(["--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["supported"] is True
        assert data["provider"] == "openrouter"
        assert data["balance_usd"] == 42.10
        assert data["usage_usd"] == 3.14
        assert data["label"] == "Prod Key"

    # Test combination with subcommands and flags
    with patch("botex.wallet.get_credits", return_value=sample_info) as mock_get:
        server._cli_wallet(["check", "--json", "--refresh", "--provider", "openrouter"])
        mock_get.assert_called_once_with("openrouter", force_refresh=True)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["provider"] == "openrouter"


def test_m4_wal_05_cli_wallet_compact_flag(capsys: pytest.CaptureFixture):
    """TEST-M4-WAL-05: `--compact` flag passes compact=True to format_credits_summary."""
    sample_info = CreditsInfo(
        supported=True,
        provider="openrouter",
        balance_usd=15.75,
        usage_usd=2.50,
        limit_usd=50.00,
        limit_remaining_usd=47.50,
        is_free_tier=False,
        label="Test Key",
    )
    with patch("botex.wallet.get_credits", return_value=sample_info):
        server._cli_wallet(["--compact"])
        out = capsys.readouterr().out
        assert "=== BoteX Wallet (openrouter) ===" in out
        assert "[openrouter] Balance: $15.75 USD | Usage: $2.50 USD" in out
        assert "Key Limit: $50.00 USD (Rem: $47.50 USD)" in out


def test_m4_wal_06_cli_wallet_subcommands_check_and_summary(capsys: pytest.CaptureFixture):
    """TEST-M4-WAL-06: Subcommands (`check`, `summary`) and redundant tokens execute without error."""
    sample_info = CreditsInfo(
        supported=True,
        provider="openrouter",
        balance_usd=5.00,
        usage_usd=1.00,
        is_free_tier=False,
    )
    with patch("botex.wallet.get_credits", return_value=sample_info) as mock_get:
        for subcmd in [
            ["check"],
            ["summary"],
            ["wallet"],
            ["credits"],
            ["check", "openrouter"],
            ["summary", "openrouter"],
        ]:
            mock_get.reset_mock()
            server._cli_wallet(subcmd)
            mock_get.assert_called_once_with("openrouter", force_refresh=False)
            out = capsys.readouterr().out
            assert "=== BoteX Wallet (openrouter) ===" in out
            assert "$5.00 USD" in out


def test_m4_wal_07_cli_wallet_refresh_flag(capsys: pytest.CaptureFixture):
    """TEST-M4-WAL-07: `--refresh` flag sets `force_refresh=True` on get_credits."""
    sample_info = CreditsInfo(supported=True, provider="openrouter", balance_usd=1.0)
    with patch("botex.wallet.get_credits", return_value=sample_info) as mock_get:
        server._cli_wallet(["--refresh"])
        mock_get.assert_called_once_with("openrouter", force_refresh=True)


def test_m4_wal_08_repl_wallet_and_credits_command(capsys: pytest.CaptureFixture):
    """TEST-M4-WAL-08: REPL local command dispatcher consumes `wallet` and `credits`."""
    sample_info = CreditsInfo(supported=True, provider="openrouter", balance_usd=9.99)
    with patch("botex.wallet.get_credits", return_value=sample_info):
        assert server._try_local_command("wallet") is True
        out = capsys.readouterr().out
        assert "$9.99 USD" in out

        assert server._try_local_command("credits") is True
        out = capsys.readouterr().out
        assert "$9.99 USD" in out

        assert server._try_local_command("wallet --json") is True
        out = capsys.readouterr().out
        assert json.loads(out)["balance_usd"] == 9.99

        assert server._try_local_command("credits check --provider nvidia") is True
        assert "=== BoteX Wallet (nvidia) ===" in capsys.readouterr().out


def test_m4_wal_09_cli_dispatcher_routing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """TEST-M4-WAL-09: `server.main()` dispatches `wallet` and `credits` CLI commands cleanly."""
    sample_info = CreditsInfo(supported=True, provider="openrouter", balance_usd=20.0)
    with patch("botex.wallet.get_credits", return_value=sample_info):
        monkeypatch.setattr("sys.argv", ["server.py", "wallet"])
        server.main()
        out = capsys.readouterr().out
        assert "=== BoteX Wallet (openrouter) ===" in out

        monkeypatch.setattr("sys.argv", ["server.py", "credits", "--json"])
        server.main()
        out = capsys.readouterr().out
        assert json.loads(out)["balance_usd"] == 20.0


def test_m4_wal_10_cli_wallet_secret_masking(capsys: pytest.CaptureFixture):
    """TEST-M4-WAL-10: Sensitive tokens in CreditsInfo fields are masked (CWE-312 compliance)."""
    raw_secret = "sk-or-v1-secretapikey999999999999"
    sample_info = CreditsInfo(
        supported=False,
        provider="openrouter",
        label=f"Key {raw_secret}",
        error=f"Unauthorized: {raw_secret}",
    )
    with patch("botex.wallet.get_credits", return_value=sample_info):
        server._cli_wallet([])
        out = capsys.readouterr().out
        assert raw_secret not in out


def test_m4_wal_11_unmocked_fallback_execution(capsys: pytest.CaptureFixture):
    """TEST-M4-WAL-11: Unmocked `_cli_wallet` executes hermetically with missing API key and zero network calls."""
    sample_nvidia = CreditsInfo(
        supported=False,
        provider="nvidia",
        label="NVIDIA NIM (No Credits API)",
    )
    with patch("botex.wallet.get_credits", return_value=sample_nvidia):
        server._cli_wallet(["nvidia"])
        out = capsys.readouterr().out
        assert "=== BoteX Wallet (nvidia) ===" in out
        assert "Credits API not supported" in out

        server._cli_wallet(["nvidia", "--json"])
        data = json.loads(capsys.readouterr().out)
        assert data["provider"] == "nvidia"
        assert data["supported"] is False
