# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Stage 4.6 & TODO #1 Comprehensive Test Suite
============================================
Covers:
1. OpenRouter Live Key & Rate-Limit Introspection (GET /api/v1/auth/key, /api/key/status, low_budget_warning)
2. Multi-Layer Model Routing (openrouter/pareto-code, validated extra_body["models"] 5-gate filter & dirty-workspace cutoff)
3. Provider Selection & Constraint Enforcement (extra_body.provider, require_parameters, reasoning stripping)
4. Post-Generation Exact Cost & Latency Telemetry (GET /api/v1/generation?id=..., 404 retry, inline usage cost, Task Receipts)
5. Structured Outputs & Strict Tool Calling (additionalProperties: False, TASK_COMPLETION_JSON_SCHEMA, parse_structured_completion)
6. Model Recommendation & Catalog Alignment Overhaul (Bearer auth in benchmarks.py, tier & task-aware scoring)
"""

import copy
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import botex.engine as engine
from botex.analytics import calculate_cost_usd, log_run
from botex.benchmarks import _classify_model_tier, get_recommended_models
from botex.config import load_config
from botex.contracts import TASK_COMPLETION_JSON_SCHEMA, parse_structured_completion
from botex.denylist import DenylistFilter
from botex.providers import (
    GenerationStats,
    OpenRouterProvider,
    _extract_openrouter_key_meta,
    _GenerationNotIndexedError,
    fetch_openrouter_generations_async,
    is_reasoning_capable_slug,
    normalize_response,
)
from botex.server_cloud import app
from botex.wallet import WalletService, parse_openrouter_payloads


class _FakeHttpResponse:
    def __init__(self, payload: Dict[str, Any]):
        self._bytes = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._bytes

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeChatCompletion:
    def __init__(
        self,
        content: str = "",
        tool_calls: List[Any] | None = None,
        finish_reason: str = "stop",
        resp_id: str = "gen-test-1",
        model: str = "qwen/qwen3-coder",
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
        cached_tokens: int = 20,
        cache_write_tokens: int = 10,
        cost_usd: float | None = None,
    ):
        self.id = resp_id
        self.model = model
        msg = SimpleNamespace(content=content, tool_calls=tool_calls or [])
        self.choices = [SimpleNamespace(message=msg, finish_reason=finish_reason)]
        prompt_details = SimpleNamespace(
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        comp_details = SimpleNamespace(reasoning_tokens=0)
        self.usage = SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=prompt_details,
            completion_tokens_details=comp_details,
            cost=cost_usd,
        )


class _RecordingAsyncClient:
    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


# ==============================================================================
# 1. Key Status & Rate-Limit Introspection (/api/v1/auth/key & /api/key/status)
# ==============================================================================


def test_stage_4_6_extract_openrouter_key_meta_and_wallet_parsing():
    meta_reset, meta_reqs = _extract_openrouter_key_meta(
        {
            "limit_reset": "2026-10-18T00:00:00Z",
            "rate_limit": {"requests": 250, "interval": "10s"},
        },
        is_free_tier=False,
    )
    assert meta_reset == "2026-10-18T00:00:00Z"
    assert meta_reqs == 250

    # Default free-tier vs paid-tier daily request fallback when rate_limit omitted
    _, free_default = _extract_openrouter_key_meta({"is_free_tier": True}, is_free_tier=True)
    _, paid_default = _extract_openrouter_key_meta({"is_free_tier": False}, is_free_tier=False)
    assert free_default == 50
    assert paid_default == 1000

    info = parse_openrouter_payloads(
        key_payload={
            "label": "prod-key",
            "usage": 4.25,
            "limit": 5.0,
            "limit_remaining": 0.75,
            "is_free_tier": False,
            "resets_at": "2026-11-01T00:00:00Z",
            "free_model_daily_requests": 1000,
        },
        credits_payload={"total_credits": 10.0, "total_usage": 9.25},
        provider_name="openrouter",
    )
    assert info.spend_usd == 4.25
    assert info.limit_usd == 5.0
    assert info.limit_remaining_usd == 0.75
    assert info.balance_usd == 0.75
    assert info.resets_at == "2026-11-01T00:00:00Z"
    assert info.free_model_daily_requests == 1000


def test_stage_4_6_wallet_service_key_status_and_cloud_endpoint(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    svc = WalletService(cache_ttl_seconds=60.0)

    def _fake_urlopen(req, timeout=10):
        url = req.full_url
        if url.endswith("/auth/key"):
            return _FakeHttpResponse(
                {
                    "data": {
                        "label": "ci-key",
                        "usage": 9.5,
                        "limit": 10.0,
                        "limit_remaining": 0.5,
                        "is_free_tier": False,
                        "limit_reset": "daily",
                    }
                }
            )
        return _FakeHttpResponse({"data": {"total_credits": 20.0, "total_usage": 19.5}})

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        status = svc.get_key_status(
            provider_name="openrouter",
            api_key="sk-or-test-key",
            force_refresh=True,
        )
    assert status["supported"] is True
    assert status["limit"] == 10.0
    assert status["limit_remaining"] == 0.5
    assert status["spend_usd"] == 9.5
    assert status["balance_usd"] == 0.5
    assert status["resets_at"] == "daily"
    assert status["free_model_daily_requests"] == 1000
    assert status["low_budget_warning"] is True

    client = TestClient(app)
    with (
        patch("botex.server_cloud.default_wallet_service", svc),
        patch("urllib.request.urlopen", side_effect=_fake_urlopen),
    ):
        r_key = client.get("/api/key/status?provider=openrouter")
        r_wal = client.get("/api/wallet?provider=openrouter")
    assert r_key.status_code == 200
    k_json = r_key.json()
    assert k_json["low_budget_warning"] is True
    assert k_json["limit_remaining"] == 0.5
    assert r_wal.status_code == 200
    w_json = r_wal.json()
    assert w_json["spend_usd"] == 9.5
    assert w_json["resets_at"] == "daily"
    assert w_json["free_model_daily_requests"] == 1000


# ==============================================================================
# 2. Provider Routing & Generation Stats (botex/providers.py)
# ==============================================================================


def test_stage_4_6_openrouter_extra_body_and_reasoning_filter():
    assert is_reasoning_capable_slug("deepseek/deepseek-r1") is True
    assert is_reasoning_capable_slug("anthropic/claude-sonnet-4") is True
    assert is_reasoning_capable_slug("meta-llama/llama-3.3-70b-instruct") is False

    prov = OpenRouterProvider(
        "openrouter",
        {
            "extra_body": {
                "provider": {"data_collection": "deny"},
                "reasoning": {"max_tokens": 4000},
            },
            "zdr": {"enabled": True},
        },
    )
    # Non-reasoning model with tools -> require_parameters=True and reasoning stripped
    eb_non_reasoning = prov.get_extra_body(
        model="meta-llama/llama-3.3-70b-instruct",
        has_tools=True,
        fallback_models=["qwen/qwen3-coder", "deepseek/deepseek-v3.2", "extra/fourth-ignored"],
    )
    assert eb_non_reasoning["provider"]["data_collection"] == "deny"
    assert eb_non_reasoning["provider"]["allow_fallbacks"] is True
    assert eb_non_reasoning["provider"]["sort"] == "throughput"
    assert eb_non_reasoning["provider"]["require_parameters"] is True
    assert "reasoning" not in eb_non_reasoning
    assert eb_non_reasoning["usage"] == {"include": True}
    assert eb_non_reasoning["models"] == [
        "meta-llama/llama-3.3-70b-instruct",
        "qwen/qwen3-coder",
        "deepseek/deepseek-v3.2",
    ]

    # Meta-router primary model -> models array suppressed
    eb_meta = prov.get_extra_body(
        model="openrouter/pareto-code",
        has_tools=True,
        fallback_models=["qwen/qwen3-coder"],
    )
    assert "models" not in eb_meta


@pytest.mark.anyio
async def test_stage_4_6_fetch_openrouter_generations_async_with_404_retry():
    calls = {"gen-1": 0, "gen-2": 0}

    def _fake_urlopen(req, timeout=1.5):
        url = req.full_url
        if "gen-1" in url:
            calls["gen-1"] += 1
            if calls["gen-1"] == 1:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return _FakeHttpResponse(
                {
                    "data": {
                        "id": "gen-1",
                        "model": "anthropic/claude-sonnet-4",
                        "total_cost": 0.0042,
                        "latency": 120.5,
                        "generation_time": 850.0,
                        "native_tokens_prompt": 500,
                        "native_tokens_completion": 150,
                        "native_tokens_cached": 300,
                        "native_tokens_cache_write": 100,
                        "cache_discount": 0.001,
                    }
                }
            )
        calls["gen-2"] += 1
        return _FakeHttpResponse(
            {
                "data": {
                    "id": "gen-2",
                    "model": "anthropic/claude-sonnet-4",
                    "total_cost": 0.0018,
                    "latency": 79.5,
                    "generation_time": 450.0,
                    "native_tokens_prompt": 600,
                    "native_tokens_completion": 80,
                    "native_tokens_cached": 400,
                    "native_tokens_cache_write": 0,
                }
            }
        )

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        agg = await fetch_openrouter_generations_async(
            generation_ids=["gen-1", "gen-2", "gen-1"],
            provider_name="openrouter",
            api_key="sk-or-test",
            retry_delay_s=0.01,
        )

    assert calls["gen-1"] == 2
    assert calls["gen-2"] == 1
    assert agg["ok"] is True
    assert agg["records_count"] == 2
    assert agg["total_cost_usd"] == 0.006
    assert agg["queue_time_ms"] == 200.0
    assert agg["generation_time_ms"] == 1300.0
    assert agg["cached_tokens"] == 700
    assert agg["cache_write_tokens"] == 100
    assert agg["models_used"] == ["anthropic/claude-sonnet-4"]


def test_stage_4_6_get_generation_stats_error_handling():
    prov = OpenRouterProvider("openrouter", {})
    # Missing API key returns None gracefully
    with patch.object(prov, "resolve_api_key", return_value=""):
        st = prov.get_generation_stats("gen-missing-key")
    assert st is None

    # Empty generation_id returns None
    st_empty = prov.get_generation_stats("", api_key="sk-test")
    assert st_empty is None

    # 404 HTTP error returns None gracefully
    def _raise_404(req, timeout=5.0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    with patch("urllib.request.urlopen", side_effect=_raise_404):
        st_404 = prov.get_generation_stats("gen-404", api_key="sk-test")
    assert st_404 is None


# ==============================================================================
# 3. Structured Outputs & Tool Schema Strictness (botex/contracts.py & engine.py)
# ==============================================================================


def test_stage_4_6_tool_schemas_have_additional_properties_false():
    all_tools = (
        engine.BOTEX_TOOLS
        + engine.BOTEX_DESTRUCTIVE_TOOLS
        + engine.BOTEX_NET_TOOLS
        + engine.BOTEX_EXEC_TOOLS
    )
    assert len(all_tools) >= 9
    for decl in all_tools:
        params = decl["function"]["parameters"]
        assert params.get("additionalProperties") is False, decl["function"]["name"]


def test_stage_4_6_parse_structured_completion_variants():
    assert TASK_COMPLETION_JSON_SCHEMA["type"] == "json_schema"
    schema_inner = TASK_COMPLETION_JSON_SCHEMA["json_schema"]
    assert schema_inner["strict"] is True
    assert schema_inner["schema"]["additionalProperties"] is False

    # Direct JSON object
    direct = parse_structured_completion(
        json.dumps(
            {
                "status": "DONE",
                "summary": "Implemented feature X",
                "files_modified": ["botex/engine.py"],
                "verification_notes": "All tests pass",
            }
        )
    )
    assert direct is not None
    assert direct["status"] == "DONE"
    assert direct["summary"] == "Implemented feature X"
    assert direct["data"]["files_modified"] == ["botex/engine.py"]

    # Markdown fenced JSON block
    fenced = parse_structured_completion(
        'Here is the result:\n```json\n{"status": "UNSUPPORTED", "summary": "Missing capability"}\n```'
    )
    assert fenced is not None
    assert fenced["status"] == "UNSUPPORTED"
    assert fenced["summary"] == "Missing capability"

    # Plain prose returns None
    assert parse_structured_completion("STATUS: DONE\nRegular text") is None
    assert parse_structured_completion('{"status": "UNKNOWN_CODE", "summary": "bad"}') is None


# ==============================================================================
# 4. Engine In-Payload Models Security Gates, Dirty Cutoff & Live Telemetry
# ==============================================================================


@pytest.mark.anyio
async def test_stage_4_6_engine_in_payload_models_validation_and_dirty_cutoff(tmp_path: Path):
    target_file = tmp_path / "sample.py"
    target_file.write_text("x = 1\n", encoding="utf-8")

    # Turn 1: model calls create_file (mutating workspace)
    tc_write = SimpleNamespace(
        id="tc_1",
        type="function",
        function=SimpleNamespace(
            name="create_file",
            arguments=json.dumps({"path": "new_file.py", "content": "y = 2\n"}),
        ),
    )
    # Turn 2: model returns structured JSON completion
    structured_done = json.dumps(
        {
            "status": "DONE",
            "summary": "Created new_file.py with structured output.",
            "files_modified": ["new_file.py"],
            "verification_notes": "Syntax checked",
        }
    )
    fake_client = _RecordingAsyncClient(
        [
            _FakeChatCompletion(tool_calls=[tc_write], resp_id="gen-turn-1", cost_usd=0.0012),
            _FakeChatCompletion(content=structured_done, resp_id="gen-turn-2", cost_usd=0.0018),
        ]
    )

    cfg = copy.deepcopy(load_config())
    cfg["engine"]["denylist"] = ["denied/model"]
    cfg["providers"]["openrouter"]["decisions_models"] = ["typesafe/jev-*"]

    def _price_gate(m: str, *a, **k):
        if m == "expensive/model":
            return False, "too expensive"
        return True, ""

    def _tools_gate(m: str, *a, **k):
        if m == "notools/model":
            return False, "no tools"
        return True, ""

    fake_gen_agg = {
        "ok": True,
        "records_count": 2,
        "total_cost_usd": 0.0035,
        "queue_time_ms": 95.0,
        "generation_time_ms": 640.0,
        "cached_tokens": 120,
        "cache_write_tokens": 45,
        "models_used": ["qwen/qwen3-coder"],
    }

    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine.build_async_client", return_value=fake_client),
        patch("botex.engine.check_model_price", side_effect=_price_gate),
        patch("botex.engine.check_model_tools", side_effect=_tools_gate),
        patch(
            "botex.engine.fetch_openrouter_generations_async",
            return_value=fake_gen_agg,
        ),
    ):
        result = await engine.run_botex_task(
            task="Create new_file.py",
            workspace_dir=str(tmp_path),
            model="qwen/qwen3-coder",
            provider="openrouter",
            mode="edit",
            api_key="sk-or-test",
            response_format=TASK_COMPLETION_JSON_SCHEMA,
            fallback_models=[
                "denied/model",  # blocked by Denylist
                "typesafe/jev-v1",  # blocked by Decisions gate
                "free/model:free",  # blocked by ZDR gate
                "expensive/model",  # blocked by Price gate
                "notools/model",  # blocked by Tools gate
                "openrouter/auto",  # blocked by meta-router exclusion
                "anthropic/claude-sonnet-4",  # PASSES all 5 gates (#2)
                "deepseek/deepseek-v3.2",  # PASSES all 5 gates (#3)
                "google/gemini-2.5-pro",  # capped at 3 models total
            ],
        )

    assert result["ok"] is True
    assert result["status"] == "DONE"
    assert result["cost_usd"] == 0.0035
    assert result["cost_source"] == "openrouter_generation_api"
    assert result["queue_time_ms"] == 95.0
    assert result["generation_time_ms"] == 640.0
    assert result["cache_write_tokens"] == 45
    assert result["actual_model"] == "qwen/qwen3-coder"
    assert result["structured_output"]["status"] == "DONE"

    # Turn 1 (clean workspace): extra_body["models"] contains only the 3 validated models
    call_1_eb = fake_client.calls[0]["extra_body"]
    assert call_1_eb["models"] == [
        "qwen/qwen3-coder",
        "anthropic/claude-sonnet-4",
        "deepseek/deepseek-v3.2",
    ]
    assert call_1_eb["provider"]["require_parameters"] is True
    assert call_1_eb["usage"] == {"include": True}
    assert fake_client.calls[0]["response_format"] == TASK_COMPLETION_JSON_SCHEMA

    # Turn 2 (dirty workspace after create_file): extra_body["models"] MUST be removed!
    call_2_eb = fake_client.calls[1]["extra_body"]
    assert "models" not in call_2_eb


# ==============================================================================
# 5. Benchmarks Authenticated Fetch & Task-Tier Scoring (botex/benchmarks.py)
# ==============================================================================


def test_stage_4_6_benchmarks_authenticated_header_and_tier_scoring():
    assert _classify_model_tier("anthropic/claude-sonnet-4", 3.0, 15.0) == "high"
    assert _classify_model_tier("google/gemini-2.5-flash-lite", 0.05, 0.2) == "budget"
    assert _classify_model_tier("qwen/qwen3-coder", 0.8, 2.4) == "standard"

    captured_headers: Dict[str, str] = {}
    sample_catalog = {
        "data": [
            {
                "id": "google/gemini-2.5-flash-lite",
                "name": "Gemini 2.5 Flash Lite",
                "context_length": 1000000,
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0.00000005", "completion": "0.0000002"},
            },
            {
                "id": "anthropic/claude-sonnet-4",
                "name": "Claude Sonnet 4",
                "context_length": 200000,
                "supported_parameters": ["tools", "reasoning", "structured_outputs"],
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            },
            {
                "id": "qwen/qwen3-coder",
                "name": "Qwen 3 Coder",
                "context_length": 131072,
                "supported_parameters": ["tools", "structured_outputs"],
                "pricing": {"prompt": "0.0000008", "completion": "0.0000024"},
            },
        ]
    }

    def _inspect_urlopen(req, timeout=10):
        for k, v in req.header_items():
            captured_headers[k.lower()] = v
        return _FakeHttpResponse(sample_catalog)

    with patch("urllib.request.urlopen", side_effect=_inspect_urlopen):
        recs_review = get_recommended_models(
            task_type="review",
            limit=3,
            api_key="sk-or-custom-guardrail-key",
        )
        recs_high = get_recommended_models(
            task_type="coding",
            tier="high",
            limit=3,
            api_key="sk-or-custom-guardrail-key",
        )
        recs_fast = get_recommended_models(
            task_type="fast",
            tier="budget",
            limit=3,
            api_key="sk-or-custom-guardrail-key",
        )

    assert captured_headers.get("authorization") == "Bearer sk-or-custom-guardrail-key"
    # On review tasks, flash-lite is penalized so capable models rank ahead of it
    assert recs_review[0]["id"] != "google/gemini-2.5-flash-lite"
    # High tier filter returns only high-tier models
    assert [r["id"] for r in recs_high] == ["anthropic/claude-sonnet-4"]
    assert "reasoning" in recs_high[0]["capabilities"]
    # Fast + budget tier returns the lightweight model
    assert recs_fast[0]["id"] == "google/gemini-2.5-flash-lite"
    assert recs_fast[0]["tier"] == "budget"


def test_stage_4_6_analytics_pareto_code_and_extended_log_run(tmp_path: Path):
    cost = calculate_cost_usd("openrouter/pareto-code", 100_000, 10_000)
    assert cost > 0.0

    analytics_path = tmp_path / "analytics.json"
    with patch("botex.analytics.ANALYTICS_FILE", analytics_path):
        entry = log_run(
            task_id="t46",
            model="openrouter/pareto-code",
            provider="openrouter",
            duration_s=2.5,
            steps=2,
            prompt_tokens=1000,
            completion_tokens=300,
            cached_tokens=500,
            cache_write_tokens=200,
            queue_time_ms=80.5,
            generation_time_ms=920.0,
            files_touched=["a.py"],
            lines_added=10,
            lines_removed=2,
            status="DONE",
            summary="ok",
            cost_usd=0.0025,
        )
    assert entry["cost_usd"] == 0.0025
    assert entry["cache_write_tokens"] == 200
    assert entry["queue_time_ms"] == 80.5
    assert entry["generation_time_ms"] == 920.0


@pytest.mark.anyio
async def test_stage_4_6_edge_cases_and_error_branches(tmp_path: Path):
    from botex.providers import _safe_float, _safe_int

    assert _safe_float("not-a-float") is None
    assert _safe_int("not-an-int", 7) == 7
    assert is_reasoning_capable_slug("") is False

    # parse_structured_completion edge cases
    assert parse_structured_completion("") is None
    assert parse_structured_completion("no braces at all") is None
    assert parse_structured_completion('{"status": 123}') is None
    res_list = parse_structured_completion('{"status": "done", "findings": ["item1", "item2"]}')
    assert res_list is not None and res_list["status"] == "DONE" and "item1" in res_list["summary"]
    res_empty_sum = parse_structured_completion('{"status": "blocked"}')
    assert (
        res_empty_sum is not None
        and res_empty_sum["status"] == "INCOMPLETE"
        and res_empty_sum["summary"] == ""
    )

    # OpenRouterProvider.get_generation_stats success & generic exception
    prov = OpenRouterProvider("openrouter", {})
    with patch(
        "urllib.request.urlopen",
        return_value=_FakeHttpResponse(
            {
                "data": {
                    "id": "gen-ok",
                    "model": "qwen/qwen3-coder",
                    "total_cost": 0.002,
                    "latency": 50.0,
                    "generation_time": 300.0,
                }
            }
        ),
    ):
        st_ok = prov.get_generation_stats("gen-ok", api_key="sk-test")
    assert st_ok is not None and st_ok.total_cost == 0.002

    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert prov.get_generation_stats("gen-err", api_key="sk-test") is None

    # fetch_openrouter_generations_async edge cases
    assert await fetch_openrouter_generations_async([]) == {}
    assert await fetch_openrouter_generations_async(["gen-1"], provider_name="nvidia") == {}
    with patch.object(OpenRouterProvider, "resolve_api_key", return_value=""):
        assert await fetch_openrouter_generations_async(["gen-1"], provider_name="openrouter") == {}
    with patch("urllib.request.urlopen", side_effect=OSError("net down")):
        assert (
            await fetch_openrouter_generations_async(
                ["gen-1"], provider_name="openrouter", api_key="sk-test"
            )
            == {}
        )

    # benchmarks edge cases (malformed entries, negative price, small context on fast/coding task, denylist, require_tools, network error)
    assert _classify_model_tier("unknown/expensive-model", 4.0, 4.0) == "high"
    edge_catalog = {
        "data": [
            "not-a-dict",
            {"id": ""},
            {
                "id": "denied/slug",
                "context_length": 64000,
                "pricing": {"prompt": "0.000001", "completion": "0.000001"},
            },
            {
                "id": "no/tools-slug",
                "context_length": 64000,
                "supported_parameters": ["temperature"],
                "pricing": {"prompt": "0.000001", "completion": "0.000001"},
            },
            {"id": "bad/price", "context_length": 64000, "pricing": {"prompt": "nan_str"}},
            {
                "id": "neg/price",
                "context_length": 64000,
                "pricing": {"prompt": "-0.001", "completion": "0"},
            },
            {
                "id": "tiny/ctx-fast",
                "context_length": 4096,
                "pricing": {"prompt": "0.000001", "completion": "0.000001"},
            },
            {
                "id": "small/ctx-coding",
                "context_length": 20000,
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0.000001", "completion": "0.000001"},
            },
            {
                "id": "generic/non-flagship",
                "context_length": 64000,
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            },
            {
                "id": "good/fast-nano",
                "context_length": 32000,
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
            },
        ]
    }
    dfilt = DenylistFilter({"engine": {"denylist": ["denied/*"]}})
    with patch("urllib.request.urlopen", return_value=_FakeHttpResponse(edge_catalog)):
        recs = get_recommended_models(
            task_type="fast", api_key="sk-test", denylist_filter=dfilt, require_tools=True
        )
        recs_coding = get_recommended_models(
            task_type="coding", api_key="sk-test", denylist_filter=dfilt, require_tools=True
        )
    assert "good/fast-nano" in [r["id"] for r in recs]
    assert "small/ctx-coding" not in [r["id"] for r in recs_coding]
    assert "generic/non-flagship" in [r["id"] for r in recs_coding]

    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        err_recs = get_recommended_models(task_type="coding", api_key="sk-test")
    assert "error" in err_recs[0]

    # OpenRouterProvider empty config get_extra_body with allow_non_zdr=True
    empty_prov = OpenRouterProvider("openrouter", {})
    eb_empty = empty_prov.get_extra_body(model="deepseek/deepseek-r1", allow_non_zdr=True)
    assert eb_empty["provider"]["data_collection"] == "allow"
    assert eb_empty["reasoning"] == {"max_tokens": 4000}

    # Engine: string model_fallbacks, non-reasoning model stripping reasoning, and structured UNSUPPORTED
    cfg = copy.deepcopy(load_config())
    cfg["providers"]["openrouter"]["model_fallbacks"]["default"] = "qwen/qwen3-coder"
    fake_unsu = _RecordingAsyncClient(
        [
            _FakeChatCompletion(
                content='{"status": "UNSUPPORTED", "summary": "Cannot execute this task"}',
                resp_id="gen-unsu",
            )
        ]
    )
    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine.build_async_client", return_value=fake_unsu),
        patch("botex.engine.check_model_price", return_value=(True, "")),
        patch("botex.engine.check_model_tools", return_value=(True, "")),
    ):
        res_unsu = await engine.run_botex_task(
            task="test unsupported",
            workspace_dir=str(tmp_path),
            model="meta-llama/llama-3.3-70b-instruct",
            provider="openrouter",
            mode="readonly",
            api_key="sk-test",
        )
    assert res_unsu["status"] == "UNSUPPORTED"
    assert "reasoning" not in fake_unsu.calls[0]["extra_body"]

    # Engine DONE when fetch_openrouter_generations_async raises an exception (graceful fallback to inline cost)
    fake_done_err = _RecordingAsyncClient(
        [
            _FakeChatCompletion(
                content="STATUS: DONE\nCompleted detailed analysis report for the module.",
                resp_id="gen-err-telemetry",
                cost_usd=0.0044,
            )
        ]
    )
    with (
        patch("botex.config.load_config", return_value=cfg),
        patch("botex.engine.load_config", return_value=cfg),
        patch("botex.engine.build_async_client", return_value=fake_done_err),
        patch("botex.engine.check_model_price", return_value=(True, "")),
        patch("botex.engine.check_model_tools", return_value=(True, "")),
        patch(
            "botex.engine.fetch_openrouter_generations_async",
            side_effect=RuntimeError("telemetry endpoint unreachable"),
        ),
    ):
        res_done_fallback = await engine.run_botex_task(
            task="analyze module",
            workspace_dir=str(tmp_path),
            model="qwen/qwen3-coder",
            provider="openrouter",
            mode="readonly",
            api_key="sk-test",
        )
    assert res_done_fallback["ok"] is True
    assert res_done_fallback["cost_usd"] == 0.0044
    assert res_done_fallback["cost_source"] == "openrouter_inline_usage"
