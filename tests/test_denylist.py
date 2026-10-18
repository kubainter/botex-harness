# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Model Denylist & Filtering Engine Unit Tests
==================================================
Comprehensive offline test suite for DenylistFilter and pattern matching engine.
Validates exact, glob, and regex pattern matching across global, provider,
and profile configurations, with candidate list filtering and tool gating.
"""

from __future__ import annotations

import os
import socket
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from botex.denylist import (
    _VALID_DECISION_MAP,
    DenylistFilter,
    check_model_denylist,
    filter_model_candidates,
    get_default_denylist_filter,
    matches_pattern,
)


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Ensure no live network calls can be made during denylist tests."""

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
def filter_config() -> Dict[str, Any]:
    return {
        "engine": {
            "denylist": [
                "openai/gpt-3.5-turbo",
                "*deprecated*",
                "re:^google/.*-exp$",
            ]
        },
        "providers": {
            "openrouter": {
                "denylist": [
                    "anthropic/claude-2*",
                    "*:free",
                ],
                "profile_denylists": {
                    "coding": [
                        "meta-llama/llama-3-8b-instruct",
                    ],
                    "fast": [
                        "deepseek/deepseek-chat",
                    ],
                },
            },
            "nvidia": {
                "denylist": [
                    "re:^nv/legacy-.*",
                ]
            },
        },
    }


# ---------------------------------------------------------------------------
# Group 1: Pattern Matching Engine
# ---------------------------------------------------------------------------


def test_denylist_exact_matching():
    """TEST-DNY-01: Exact pattern matching (case/whitespace insensitive)."""
    assert matches_pattern("openai/gpt-3.5-turbo", "openai/gpt-3.5-turbo") is True
    assert matches_pattern("OPENAI/GPT-3.5-TURBO", "openai/gpt-3.5-turbo") is True
    assert matches_pattern("  openai/gpt-3.5-turbo  ", "openai/gpt-3.5-turbo") is True
    assert matches_pattern("openai/gpt-4o", "openai/gpt-3.5-turbo") is False


def test_denylist_glob_wildcard_matching():
    """TEST-DNY-02: Glob wildcards (*deprecated*, anthropic/*, *:free)."""
    assert matches_pattern("company/deprecated-v1", "*deprecated*") is True
    assert matches_pattern("deprecated-model", "*deprecated*") is True
    assert matches_pattern("standard-model", "*deprecated*") is False

    assert matches_pattern("anthropic/claude-3-opus", "anthropic/*") is True
    assert matches_pattern("ANTHROPIC/CLAUDE-3.5-SONNET", "anthropic/*") is True
    assert matches_pattern("openai/claude-clone", "anthropic/*") is False

    assert matches_pattern("meta-llama/llama-3.1-8b:free", "*:free") is True
    assert matches_pattern("mistral/7b:FREE", "*:free") is True
    assert matches_pattern("meta-llama/llama-3.1-8b", "*:free") is False

    assert matches_pattern("meta/llama-3-instruct", "meta/*-instruct") is True
    assert matches_pattern("meta/llama-2-instruct", "meta/*-instruct") is True


def test_denylist_regex_matching():
    """TEST-DNY-03: Regex matching prefixed with re: or regex:."""
    assert matches_pattern("google/gemini-pro-exp", "re:^google/.*-exp$") is True
    assert matches_pattern("google/gemini-1.5-pro", "re:^google/.*-exp$") is False
    assert matches_pattern("google/gemini-pro-exp", "regex:^google/.*-exp$") is True
    assert matches_pattern("nv/legacy-12", r"regex:^nv/legacy-\d+") is True
    assert matches_pattern("nv/legacy-abc", r"regex:^nv/legacy-\d+") is False


def test_denylist_malformed_regex_handling():
    """TEST-DNY-04: Safe handling of invalid regex without crashing."""
    assert matches_pattern("google/gemini", "re:[unclosed-bracket") is False
    assert matches_pattern("google/gemini", "regex:(") is False


def test_denylist_edge_cases_and_empty_inputs():
    """TEST-DNY-05: Empty strings, None, and empty pattern lists."""
    assert matches_pattern("", "pattern") is False
    assert matches_pattern("model", "") is False
    assert matches_pattern("model", "   ") is False

    filt = DenylistFilter(global_denylist=["", "   ", "valid/pattern"])
    assert filt.is_denied("", provider="openrouter") is False
    assert filt.is_allowed("", provider="openrouter") is False
    assert filt.is_denied("valid/pattern") is True


# ---------------------------------------------------------------------------
# Group 2: Multi-Stage Configuration Hierarchy
# ---------------------------------------------------------------------------


def test_denylist_hierarchy_global(filter_config: Dict[str, Any]):
    """TEST-DNY-06: Global denylist blocks across all providers/profiles."""
    filt = DenylistFilter(config=filter_config)

    assert filt.is_denied("openai/gpt-3.5-turbo", provider="openrouter", profile="coding") is True
    assert filt.is_denied("openai/gpt-3.5-turbo", provider="nvidia", profile=None) is True
    assert filt.is_denied("openai/gpt-3.5-turbo", provider="", profile=None) is True
    assert filt.is_allowed("openai/gpt-3.5-turbo") is False


def test_denylist_hierarchy_provider(filter_config: Dict[str, Any]):
    """TEST-DNY-07: Provider denylist blocks only target provider."""
    filt = DenylistFilter(config=filter_config)

    assert filt.is_denied("anthropic/claude-2.0", provider="openrouter") is True
    assert filt.is_denied("anthropic/claude-2.0", provider="generic_openai") is False
    assert filt.is_allowed("anthropic/claude-2.0", provider="generic_openai") is True


def test_denylist_hierarchy_profile(filter_config: Dict[str, Any]):
    """TEST-DNY-08: Profile denylist blocks only target profile."""
    filt = DenylistFilter(config=filter_config)

    assert (
        filt.is_denied(
            "meta-llama/llama-3-8b-instruct",
            provider="openrouter",
            profile="coding",
        )
        is True
    )
    assert (
        filt.is_denied(
            "meta-llama/llama-3-8b-instruct",
            provider="openrouter",
            profile="fast",
        )
        is False
    )
    assert (
        filt.is_denied(
            "meta-llama/llama-3-8b-instruct",
            provider="openrouter",
            profile=None,
        )
        is False
    )


def test_denylist_hierarchy_cumulative_union(filter_config: Dict[str, Any]):
    """TEST-DNY-09: Union evaluation across global, provider, profile."""
    filt = DenylistFilter(config=filter_config)

    # 1. Global hit
    assert filt.is_denied("openai/gpt-3.5-turbo", provider="openrouter", profile="coding") is True
    # 2. Provider hit
    assert filt.is_denied("anthropic/claude-2.0", provider="openrouter", profile="coding") is True
    # 3. Profile hit
    assert (
        filt.is_denied("meta-llama/llama-3-8b-instruct", provider="openrouter", profile="coding")
        is True
    )
    # 4. Miss all
    assert (
        filt.is_allowed("anthropic/claude-3.5-sonnet", provider="openrouter", profile="coding")
        is True
    )


# ---------------------------------------------------------------------------
# Group 3: Candidate List Filtering & Fallback Pruning
# ---------------------------------------------------------------------------


def test_denylist_filter_models_order_preservation(filter_config: Dict[str, Any]):
    """TEST-DNY-10: Filter models preserves order of allowed candidates."""
    filt = DenylistFilter(config=filter_config)

    candidates = [
        "anthropic/claude-3.5-sonnet",
        "openai/gpt-3.5-turbo",  # Global deny
        "meta-llama/llama-3.1-70b",
        "anthropic/claude-2.1",  # Provider deny
    ]

    filtered = filt.filter_models(candidates, provider="openrouter", profile="coding")
    assert filtered == ["anthropic/claude-3.5-sonnet", "meta-llama/llama-3.1-70b"]


def test_denylist_filter_models_boundary_conditions(filter_config: Dict[str, Any]):
    """TEST-DNY-11: Empty candidate list, all denied, all allowed."""
    filt = DenylistFilter(config=filter_config)

    assert filt.filter_models([]) == []

    all_denied = ["openai/gpt-3.5-turbo", "anthropic/claude-2.0"]
    assert filt.filter_models(all_denied, provider="openrouter") == []

    all_allowed = ["anthropic/claude-3.5-sonnet", "meta-llama/llama-3.1-70b"]
    assert filt.filter_models(all_allowed, provider="openrouter") == all_allowed


def test_denylist_filter_models_deduplication(filter_config: Dict[str, Any]):
    """TEST-DNY-12: Case normalization and deduplication preserves first occurrence."""
    filt = DenylistFilter(config=filter_config)

    candidates = [
        "anthropic/claude-3.5-sonnet",
        "ANTHROPIC/CLAUDE-3.5-SONNET",
        "meta/llama-3",
    ]
    filtered = filt.filter_models(candidates, provider="openrouter")
    assert filtered == ["anthropic/claude-3.5-sonnet", "meta/llama-3"]


# ---------------------------------------------------------------------------
# Group 4: Tool Capability Filtering Integration
# ---------------------------------------------------------------------------


def test_denylist_filter_candidates_require_tools(filter_config: Dict[str, Any]):
    """TEST-DNY-13: Prune candidates lacking tool support when require_tools=True."""
    filt = DenylistFilter(config=filter_config)

    mock_catalog = MagicMock()
    mock_catalog.check_tool_support.side_effect = lambda m: m == "model-a"

    candidates = ["model-a", "model-b"]
    result = filt.filter_candidates(
        candidates,
        provider="openrouter",
        require_tools=True,
        catalog=mock_catalog,
    )
    assert result == ["model-a"]


def test_denylist_filter_candidates_tools_ignored(filter_config: Dict[str, Any]):
    """TEST-DNY-14: Ignore tool capability when require_tools=False."""
    filt = DenylistFilter(config=filter_config)

    mock_catalog = MagicMock()
    mock_catalog.check_tool_support.return_value = False

    candidates = ["model-a", "model-b"]
    result = filt.filter_candidates(
        candidates,
        provider="openrouter",
        require_tools=False,
        catalog=mock_catalog,
    )
    assert result == ["model-a", "model-b"]


def test_denylist_filter_candidates_combined(filter_config: Dict[str, Any]):
    """TEST-DNY-15: Combined pruning of denylist + tool-incapable models."""
    filt = DenylistFilter(config=filter_config)

    mock_catalog = MagicMock()
    # mock check_tool_support: True for claude and llama, False for mixtral
    mock_catalog.check_tool_support.side_effect = lambda m: m != "mistralai/mixtral-8x7b"

    candidates = [
        "anthropic/claude-3.5-sonnet",  # Allowed, has tools -> KEPT
        "openai/gpt-3.5-turbo",  # Denied (global), has tools -> PRUNED
        "mistralai/mixtral-8x7b",  # Allowed, NO tools -> PRUNED
        "anthropic/claude-2.0",  # Denied (provider) -> PRUNED
    ]

    result = filt.filter_candidates(
        candidates,
        provider="openrouter",
        profile="coding",
        require_tools=True,
        catalog=mock_catalog,
    )
    assert result == ["anthropic/claude-3.5-sonnet"]


# ---------------------------------------------------------------------------
# Group 5: Reason Reporting & CodeQL CWE-312 Compliance
# ---------------------------------------------------------------------------


def test_denylist_codeql_decision_literal_mapping(filter_config: Dict[str, Any]):
    """TEST-DNY-16: Static string literal mapping for decision results."""
    filt = DenylistFilter(config=filter_config)

    # 1. Allowed
    ok, code, _ = filt.check_model("anthropic/claude-3.5-sonnet", provider="openrouter")
    assert ok is True
    assert code == _VALID_DECISION_MAP["allowed"]

    # 2. Denied global
    ok, code, _ = filt.check_model("openai/gpt-3.5-turbo", provider="openrouter")
    assert ok is False
    assert code == _VALID_DECISION_MAP["denied_global"]

    # 3. Denied provider
    ok, code, _ = filt.check_model("anthropic/claude-2.0", provider="openrouter")
    assert ok is False
    assert code == _VALID_DECISION_MAP["denied_provider"]

    # 4. Denied profile
    ok, code, _ = filt.check_model(
        "meta-llama/llama-3-8b-instruct", provider="openrouter", profile="coding"
    )
    assert ok is False
    assert code == _VALID_DECISION_MAP["denied_profile"]

    # All codes are in values of map
    for status in [code]:
        assert status in _VALID_DECISION_MAP.values()
        assert status == status.upper()


def test_denylist_secret_masking_in_logs():
    """TEST-DNY-17: Secret masking with mask_secrets in reason strings."""
    secret_slug = "custom/model-sk-ant-api03-abcdef123456789012345678"
    filt = DenylistFilter(global_denylist=[secret_slug])

    ok, reason = filt.check(secret_slug)
    assert ok is False
    assert "sk-ant-api03-abcdef123456789012345678" not in reason
    assert "[REDACTED]" in reason or "***" in reason


def test_denylist_module_functions_helper():
    """Verify module-level convenience functions."""
    dfilter = get_default_denylist_filter({"engine": {"denylist": ["blocked/model"]}})
    assert dfilter.is_denied("blocked/model") is True

    ok, msg = check_model_denylist("blocked/model")
    assert ok is False
    assert "DENYLIST BLOCK" in msg

    filtered = filter_model_candidates(["blocked/model", "good/model"])
    assert filtered == ["good/model"]


# ---------------------------------------------------------------------------
# Group 6: Mock Isolation Verification Tests
# ---------------------------------------------------------------------------


def test_mock_isolation_zero_network_calls():
    """TEST-MCK-01: Socket blocking verification in offline test suite."""
    with pytest.raises(RuntimeError, match="Blocked live network socket call"):
        socket.socket().connect(("openrouter.ai", 443))


def test_suite_passes_with_empty_credentials():
    """TEST-MCK-02: Execution without environment keys."""
    for key in ["OPENROUTER_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY"]:
        assert key not in os.environ


# ---------------------------------------------------------------------------
# Group 7: Adversarial Resilience, Security & Harmonization (M2 R2)
# ---------------------------------------------------------------------------


def test_denylist_filter_candidates_debug_logs_secret_masking(caplog: pytest.LogCaptureFixture):
    """TEST-DNY-18: Secret candidate strings masked with mask_secrets in debug logs."""
    import logging

    secret_key = "sk-or-v1-1234567890abcdef1234567890"
    raw_candidate = f"openai/{secret_key}"

    filt = DenylistFilter(global_denylist=["openai/sk-*"])

    with caplog.at_level(logging.DEBUG, logger="botex.denylist"):
        filt.filter_candidates([raw_candidate])

    # The raw unmasked secret MUST NOT appear in debug log output
    assert secret_key not in caplog.text
    # Log message must contain masked candidate
    assert "[REDACTED]" in caplog.text or "***" in caplog.text


def test_denylist_regex_length_warning_secret_masking(caplog: pytest.LogCaptureFixture):
    """TEST-DNY-19: Oversized regex pattern warning masks secret prefix."""
    import logging

    secret_token = "sk-ant-api03-abcdef123456789012345678"
    oversized_pat = f"re:^{secret_token}" + ("a" * 300) + "$"

    with caplog.at_level(logging.WARNING, logger="botex.denylist"):
        filt = DenylistFilter(global_denylist=[oversized_pat])
        filt.is_allowed("some-model")

    # Unmasked raw token must not leak in warning logs
    assert secret_token not in caplog.text


@pytest.mark.parametrize("empty_pat", ["re:", "regex:", "re:   ", "regex:\t"])
def test_denylist_empty_regex_pattern_rejected_no_false_positive_block(empty_pat: str):
    """TEST-DNY-20: Empty regex pattern prefix rejected and does not match or block models."""
    # 1. matches_pattern directly
    assert matches_pattern("openai/gpt-4o", empty_pat) is False
    assert matches_pattern("anthropic/claude", empty_pat) is False

    # 2. DenylistFilter integration
    filt = DenylistFilter(global_denylist=[empty_pat])
    assert filt.is_allowed("openai/gpt-4o") is True
    assert filt.is_allowed("anthropic/claude-3.5-sonnet") is True


@pytest.mark.parametrize("invalid_input", ["", "   ", "\t\n", None])
def test_denylist_check_and_check_model_empty_none_input_rejected(invalid_input: Any):
    """TEST-DNY-21: check and check_model return False with INVALID_MODEL_INPUT for empty/None."""
    filt = DenylistFilter()

    # is_allowed is already False
    assert filt.is_allowed(invalid_input) is False

    # check() must return (False, "Empty or invalid model identifier")
    ok, reason = filt.check(invalid_input)
    assert ok is False
    assert "Empty or invalid" in reason

    # check_model() must return (False, "INVALID_MODEL_INPUT", reason)
    ok_m, code, reason_m = filt.check_model(invalid_input)
    assert ok_m is False
    assert code == _VALID_DECISION_MAP["invalid_model_input"]
    assert "Empty or invalid" in reason_m


def test_denylist_filter_candidates_case_insensitive_capability_override():
    """TEST-DNY-22: Uppercase candidate pruned against lowercase capability override."""
    config = {
        "providers": {"openrouter": {"capabilities": {"openai/gpt-4o": {"supports_tools": False}}}}
    }
    filt = DenylistFilter(config)

    # Uppercase candidate input
    candidates = ["OPENAI/GPT-4O", "anthropic/claude-3.5-sonnet"]
    # Mock catalog where everything supports tools
    mock_catalog = MagicMock()
    mock_catalog.check_tool_support.return_value = True

    # gpt-4o must be pruned because provider capability override says supports_tools=False
    pruned = filt.filter_candidates(
        candidates, provider="openrouter", require_tools=True, catalog=mock_catalog
    )
    assert "OPENAI/GPT-4O" not in pruned
    assert "anthropic/claude-3.5-sonnet" in pruned


@pytest.mark.parametrize(
    "scalar_config",
    [
        {"engine": {"denylist": 123}},
        {"engine": {"denylist": True}},
        {"providers": {"openrouter": {"denylist": 456}}},
        {"providers": {"openrouter": {"profile_denylists": {"coding": 789}}}},
        {"providers": {"openrouter": {"profile_denylists": {"coding": 3.14}}}},
    ],
)
def test_denylist_config_scalar_values_graceful_handling(scalar_config: Dict[str, Any]):
    """TEST-DNY-23: Scalar denylist values coerced safely without TypeError."""
    filt = DenylistFilter(scalar_config)
    # Must initialize and filter without TypeError: 'int' object is not iterable
    assert filt.is_allowed("openai/gpt-4o") is True
    assert filt.is_allowed("anthropic/claude", provider="openrouter", profile="coding") is True
