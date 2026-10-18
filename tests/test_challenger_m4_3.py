# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Challenger Adversarial Test Suite - Milestone M4
================================================
Empirical stress-testing of candidate pruning, denylist matching,
preflight rejection, HTTP 429 recoverable fallback, and rollback integrity.
"""

import copy
import json
import socket
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from botex.config import (
    load_config,
    resolve_model_candidates,
)
from botex.denylist import DenylistFilter
from botex.engine import _run_botex_task_once_impl, run_botex_task


# ==============================================================================
# Hermetic Isolation Fixtures
# ==============================================================================


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Ensure no live network calls can be made during adversarial tests."""
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
    """Clear all provider API keys from environment."""
    for key in [
        "OPENROUTER_API_KEY",
        "NVIDIA_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_FREE_KEY",
        "CUSTOM_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)


# ==============================================================================
# Mock Helpers
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
        self.cached_tokens = 0
        self.reasoning_tokens = 0


class FakeCompletionResponse:
    def __init__(
        self,
        content: str = "",
        finish_reason: str = "stop",
        tool_calls: Optional[List[Any]] = None,
    ) -> None:
        self.id = "fake-resp-adv"
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
        return FakeCompletionResponse(content="STATUS: DONE\ntask completed")


class MockHTTPStatusError(Exception):
    """Simulated provider HTTP error with status_code attribute."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


# ==============================================================================
# Scenario 1: All Models in Profile Denylisted
# ==============================================================================


@pytest.mark.anyio
async def test_challenger_01_all_models_denylisted_returns_config_error(tmp_path: Any):
    """Scenario 1: When all models in a profile are denylisted, resolve_model_candidates

    returns [] and run_botex_task immediately returns CONFIG_ERROR with
    security_or_policy_block without attempting any API calls.
    """
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["openrouter/*", "anthropic/*", "meta/*"]
    cfg["providers"]["openrouter"]["models"]["default"] = "openrouter/auto"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = [
        "anthropic/claude-3-haiku",
        "meta/llama-3-8b",
    ]

    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
    ):
        candidates = resolve_model_candidates("", profile="default", provider="openrouter")
        assert candidates == []

        fake_client = FakeClient()
        with (
            patch("botex.engine.build_async_client", return_value=fake_client),
            patch("botex.engine.check_model_price", return_value=(True, "")),
            patch("botex.engine.check_model_tools", return_value=(True, "")),
        ):
            result = await run_botex_task(
                task="adversarial test: all denylisted",
                workspace_dir=str(tmp_path),
                profile="default",
                provider="openrouter",
                mode="readonly",
                api_key="sk-test",
            )

        assert result["ok"] is False
        assert result["status"] == "CONFIG_ERROR"
        assert result["failure_kind"] == "security_or_policy_block"
        assert result["attempts"] == []
        assert result["files_touched"] == []
        assert result["exec_ran"] is False
        assert result["steps"] == 0
        assert result["cost_usd"] == 0.0
        assert "No eligible model candidates available" in result["message"]
        # Crucial: Zero API calls made
        assert len(fake_client.calls) == 0


# ==============================================================================
# Scenario 2: Explicit Model Denylisted Preflight Rejection
# ==============================================================================


@pytest.mark.anyio
async def test_challenger_02_explicit_denylisted_model_rejected_preflight(tmp_path: Any):
    """Scenario 2: When an explicit model argument is denylisted, verify pre-flight

    rejection before token consumption in both run_botex_task and _run_botex_task_once_impl.
    """
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["forbidden/*"]

    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
    ):
        # 1. Candidate resolution prunes explicit forbidden model
        candidates = resolve_model_candidates("forbidden/specialized-model", provider="openrouter")
        assert candidates == []

        # 2. Outer run_botex_task rejects immediately
        fake_client = FakeClient()
        with (
            patch("botex.engine.build_async_client", return_value=fake_client),
            patch("botex.engine.check_model_price", return_value=(True, "")),
            patch("botex.engine.check_model_tools", return_value=(True, "")),
        ):
            result = await run_botex_task(
                task="test explicit forbidden",
                workspace_dir=str(tmp_path),
                model="forbidden/specialized-model",
                provider="openrouter",
                mode="readonly",
                api_key="sk-test",
            )

        assert result["ok"] is False
        assert result["status"] == "CONFIG_ERROR"
        assert result["failure_kind"] == "security_or_policy_block"
        assert result["attempts"] == []
        assert result["cost_usd"] == 0.0
        assert len(fake_client.calls) == 0

        # 3. Direct once-impl pre-flight Step 0.35 rejection
        result_once = await _run_botex_task_once_impl(
            task="direct once call",
            workspace_dir=str(tmp_path),
            model="forbidden/specialized-model",
            provider="openrouter",
            mode="readonly",
            api_key="sk-test",
        )
        assert result_once["ok"] is False
        assert result_once["status"] == "DENIED_BY_GLOBAL_POLICY"
        assert result_once["failure_kind"] == "security_or_policy_block"
        assert "[DENYLIST BLOCK]" in result_once["message"]
        assert len(fake_client.calls) == 0


# ==============================================================================
# Scenario 3: Multi-Model Fallback Chain with Recoverable HTTP 429
# ==============================================================================


@pytest.mark.anyio
async def test_challenger_03_multi_model_fallback_chain_on_http_429(tmp_path: Any):
    """Scenario 3: Primary model fails with recoverable HTTP 429, failover model

    executes successfully and completes the task contract.
    """
    cfg = copy.deepcopy(load_config())
    cfg["providers"]["openrouter"]["models"]["default"] = "primary/rate-limited"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = ["failover/healthy-worker"]

    err_429 = MockHTTPStatusError("Rate limit exceeded (Too Many Requests)", status_code=429)
    resp_success = FakeCompletionResponse(content="STATUS: DONE\nFixed successfully")

    fake_client = FakeClient([err_429, resp_success])

    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine.build_async_client", return_value=fake_client),
        patch("botex.engine.check_model_price", return_value=(True, "")),
        patch("botex.engine.check_model_tools", return_value=(True, "")),
    ):
        result = await run_botex_task(
            task="fix rate limit issue",
            workspace_dir=str(tmp_path),
            profile="default",
            provider="openrouter",
            mode="readonly",
            api_key="sk-test",
        )

    assert result["ok"] is True
    assert result["status"] == "DONE"
    assert len(result["attempts"]) == 2

    # Attempt 0: Primary failed with 429
    att0 = result["attempts"][0]
    assert att0["model"] == "primary/rate-limited"
    assert att0["status"] == "API_ERROR"
    assert att0["failure_kind"] == "provider_unavailable"

    # Attempt 1: Failover succeeded
    att1 = result["attempts"][1]
    assert att1["model"] == "failover/healthy-worker"
    assert att1["status"] == "DONE"

    assert result["fallback_from"] == "primary/rate-limited"
    assert len(fake_client.calls) == 2
    assert fake_client.calls[0]["model"] == "primary/rate-limited"
    assert fake_client.calls[1]["model"] == "failover/healthy-worker"


# ==============================================================================
# Scenario 4: Rollback Integrity Across Diverse File Mutations
# ==============================================================================


@pytest.mark.anyio
async def test_challenger_04_rollback_integrity_restores_created_and_modified(tmp_path: Any):
    """Scenario 4: Verify workspace snapshot rollbacks between failed attempts

    do not corrupt state. Created files are removed, modified files are restored.
    """
    # Create baseline files in workspace
    baseline_file = tmp_path / "baseline.txt"
    baseline_file.write_text("pristine original content", encoding="utf-8")

    cfg = copy.deepcopy(load_config())
    cfg["providers"]["openrouter"]["models"]["default"] = "failing/candidate"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = ["saving/candidate"]

    # Candidate 1 actions:
    # 1. Modify baseline.txt
    # 2. Create dirty_pollutant.txt
    tcall_modify = SimpleNamespace(
        id="call_mod",
        type="function",
        function=SimpleNamespace(
            name="replace_in_file",
            arguments=json.dumps(
                {
                    "path": "baseline.txt",
                    "target": "pristine original content",
                    "replacement": "corrupted text",
                }
            ),
        ),
    )
    tcall_create = SimpleNamespace(
        id="call_cre",
        type="function",
        function=SimpleNamespace(
            name="create_file",
            arguments=json.dumps(
                {
                    "path": "dirty_pollutant.txt",
                    "content": "hazardous debris",
                }
            ),
        ),
    )

    resp_cand1_tool1 = FakeCompletionResponse(tool_calls=[tcall_modify])
    resp_cand1_tool2 = FakeCompletionResponse(tool_calls=[tcall_create])
    resp_cand1_err = MockHTTPStatusError("Upstream 503 Service Unavailable", status_code=503)
    resp_cand2_done = FakeCompletionResponse(content="STATUS: DONE\nTask verified")

    fake_client = FakeClient([resp_cand1_tool1, resp_cand1_tool2, resp_cand1_err, resp_cand2_done])

    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine.build_async_client", return_value=fake_client),
        patch("botex.engine.check_model_price", return_value=(True, "")),
        patch("botex.engine.check_model_tools", return_value=(True, "")),
    ):
        result = await run_botex_task(
            task="modify and cleanup",
            workspace_dir=str(tmp_path),
            profile="default",
            provider="openrouter",
            mode="edit",
            api_key="sk-test",
        )

    assert result["status"] == "DONE"
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["rolled_back"] is True
    assert result["attempts"][0]["rollback_verified"] is True

    # Critical assertions on disk state:
    # 1. Created file MUST be completely deleted by rollback
    assert not (tmp_path / "dirty_pollutant.txt").exists()
    # 2. Modified file MUST be restored to pristine content
    assert baseline_file.read_text(encoding="utf-8") == "pristine original content"


@pytest.mark.anyio
async def test_challenger_04b_exec_mutation_aborts_fallback(tmp_path: Any):
    """Scenario 4b: If candidate 1 executed a command (exec_ran=True), fallback

    MUST BE BLOCKED because shell mutations cannot be verified or rolled back.
    """
    cfg = copy.deepcopy(load_config())
    cfg["exec"] = {"enabled": True, "allowlist": ["echo"]}
    cfg["providers"]["openrouter"]["models"]["default"] = "executing/candidate"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = ["next/candidate"]

    # Candidate 1 executes command then fails with 502
    tcall_exec = SimpleNamespace(
        id="call_ex",
        type="function",
        function=SimpleNamespace(
            name="run_command",
            arguments=json.dumps({"command": "echo mutation"}),
        ),
    )
    resp_cand1_tool = FakeCompletionResponse(tool_calls=[tcall_exec])
    resp_cand1_err = MockHTTPStatusError("502 Bad Gateway", status_code=502)
    resp_cand2_done = FakeCompletionResponse(content="STATUS: DONE\nShould not run")

    fake_client = FakeClient([resp_cand1_tool, resp_cand1_err, resp_cand2_done])

    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine.build_async_client", return_value=fake_client),
        patch("botex.engine.check_model_price", return_value=(True, "")),
        patch("botex.engine.check_model_tools", return_value=(True, "")),
        patch(
            "botex.engine.run_command",
            new_callable=AsyncMock,
            return_value={"ok": True, "output": "mutation"},
        ),
    ):
        result = await run_botex_task(
            task="exec test",
            workspace_dir=str(tmp_path),
            profile="default",
            provider="openrouter",
            mode="full",
            api_key="sk-test",
        )

    # Fallback MUST be blocked because exec_ran=True cannot be rolled back safely
    assert result["ok"] is False
    assert result["status"] == "API_ERROR"
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["model"] == "executing/candidate"
    assert result["attempts"][0]["status"] == "API_ERROR"


# ==============================================================================
# Scenario 5: Case-Insensitive, Glob, and Regex Pattern Matching in Denylists
# ==============================================================================


def test_challenger_05_case_insensitive_matching():
    """Scenario 5.1: Verify case-insensitive pattern matching across exact, glob, and regex."""
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = [
        "OPENAI/GPT-4O",
        "Anthropic/*",
        "re:^google/GEMINI.*$",
    ]
    cfg["providers"]["openrouter"]["models"]["default"] = "openai/gpt-4o"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = [
        "anthropic/claude-3-5-sonnet",
        "google/gemini-1.5-pro",
        "allowed/Model-1",
    ]

    with patch("botex.config.load_config", return_value=cfg):
        candidates = resolve_model_candidates("", profile="default", provider="openrouter")
    # Only allowed/Model-1 should survive
    assert candidates == ["allowed/Model-1"]


def test_challenger_05_glob_wildcards_and_character_sets():
    """Scenario 5.2: Verify glob wildcards (*, ?, [a-z]) in denylist."""
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = [
        "deepseek/*",
        "meta/llama-3.?-70b",
        "vendor/model-[ab]",
    ]
    cfg["providers"]["openrouter"]["models"]["default"] = "deepseek/deepseek-coder"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = [
        "deepseek/chat",
        "meta/llama-3.1-70b",
        "meta/llama-3.2-70b",
        "vendor/model-a",
        "vendor/model-b",
        "vendor/model-c",  # should be allowed
        "meta/llama-3-8b",  # should be allowed (different length)
    ]

    with patch("botex.config.load_config", return_value=cfg):
        candidates = resolve_model_candidates("", profile="default", provider="openrouter")
    assert candidates == ["vendor/model-c", "meta/llama-3-8b"]


def test_challenger_05_regex_patterns_and_resilience():
    """Scenario 5.3: Verify regex patterns with prefix 're:' or 'regex:',

    including safety checks on invalid or oversized regex patterns.
    """
    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = [
        "re:^openai/(o1|o3).*$",
        "regex:(?i)preview$",
        "re:[invalid-unclosed-regex",  # Must not crash!
        "re:" + "a" * 300,  # Over 256 chars, must be safely rejected without crashing!
    ]
    cfg["providers"]["openrouter"]["models"]["default"] = "openai/o1-mini"
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = [
        "openai/o3-mini",
        "google/gemini-2.0-flash-preview",
        "openai/gpt-4o",  # Allowed
        "meta/llama-3.3-70b",  # Allowed
    ]

    with patch("botex.config.load_config", return_value=cfg):
        candidates = resolve_model_candidates("", profile="default", provider="openrouter")
    assert candidates == ["openai/gpt-4o", "meta/llama-3.3-70b"]


def test_challenger_05_deduplication_and_case_preservation():
    """Scenario 5.4: Deduplication is case-insensitive but preserves the first casing."""
    flt = DenylistFilter({"engine": {"denylist": []}})
    raw = [
        "OpenAI/GPT-4o",
        "openai/gpt-4o",
        "OPENAI/GPT-4O",
        "Anthropic/Claude-3",
        "anthropic/claude-3",
    ]
    res = flt.filter_candidates(raw)
    assert res == ["OpenAI/GPT-4o", "Anthropic/Claude-3"]


def test_challenger_05_multi_tier_denylist_hierarchy():
    """Scenario 5.5: Multi-tier denylist precedence: Global > Provider > Profile."""
    cfg = {
        "engine": {
            "denylist": ["global/blocked"],
        },
        "providers": {
            "openrouter": {
                "denylist": ["openrouter/blocked"],
                "profile_denylists": {
                    "fast": ["openrouter/fast-blocked"],
                },
                "models": {
                    "fast": "global/blocked",
                },
                "model_fallbacks": {
                    "fast": [
                        "openrouter/blocked",
                        "openrouter/fast-blocked",
                        "openrouter/allowed-model",
                    ],
                    "coding": [
                        "openrouter/fast-blocked",  # Allowed under coding profile
                    ],
                },
            }
        },
    }
    flt = DenylistFilter(cfg)

    # For 'fast' profile: all three blocked models pruned
    res_fast = flt.filter_candidates(
        [
            "global/blocked",
            "openrouter/blocked",
            "openrouter/fast-blocked",
            "openrouter/allowed-model",
        ],
        provider="openrouter",
        profile="fast",
    )
    assert res_fast == ["openrouter/allowed-model"]

    # For 'coding' profile: 'openrouter/fast-blocked' is NOT blocked
    res_coding = flt.filter_candidates(
        [
            "global/blocked",
            "openrouter/blocked",
            "openrouter/fast-blocked",
        ],
        provider="openrouter",
        profile="coding",
    )
    assert res_coding == ["openrouter/fast-blocked"]
