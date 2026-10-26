# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for analytics free-model pricing and zero-cost budget rules.

Stage 4.7: ``:free`` / ``openrouter-free/`` / ``openrouter/free`` / ``ollama/`` /
``nvidia/`` models and providers that cannot bill per request (``local: true``
or a static ``api_key_fallback``) must never accrue pseudo-cost and must never
be aborted by the daily BUDGET_BLOCK.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from types import SimpleNamespace

import pytest

from botex.analytics import (
    calculate_cost_usd,
    check_budget_limit,
    is_free_model,
    log_run,
    save_analytics,
)


def test_is_free_model_slug_markers():
    """Free-tier slugs and zero-cost namespaces are detected from the model id alone."""
    free_slugs = [
        "vendor/some-model:free",
        "openrouter/free",
        "openrouter-free/google/gemma-4-31b-it:free",
        "ollama/SpeakLeash/bielik-11b-v3.0-instruct:Q4_K_M",
        "nvidia/meta/llama-3.2-11b-vision-instruct",
        "  VENDOR/X:FREE  ",  # case and whitespace tolerant
    ]
    for slug in free_slugs:
        assert is_free_model(slug) is True, slug

    for slug in ["anthropic/claude-sonnet-4", "openrouter/auto", "qwen/qwen3-coder", ""]:
        assert is_free_model(slug) is False, slug


def test_is_free_model_provider_flags():
    """Local endpoints and static api_key_fallback providers cannot bill per request."""
    assert is_free_model("any/model", {"local": True}) is True
    assert is_free_model("any/model", {"api_key_fallback": "ollama"}) is True
    assert is_free_model("any/model", provider_name="ollama") is True
    assert is_free_model("any/model", provider_name="OLLAMA") is True
    assert is_free_model("any/model", {"local": False}, "openrouter") is False
    assert is_free_model("any/model", None, "") is False
    assert is_free_model("any/model", "not-a-dict", "") is False


def test_calculate_cost_usd_free_models_zero():
    """Pseudo-pricing never applies to free/zero-cost models."""
    free_slugs = [
        "vendor/some-model:free",
        "openrouter/free",
        "openrouter-free/google/gemma-4-31b-it:free",
        "ollama/SpeakLeash/bielik-11b-v3.0-instruct:Q4_K_M",
        "nvidia/meta/llama-3.2-11b-vision-instruct",
    ]
    for slug in free_slugs:
        assert calculate_cost_usd(slug, 1_000_000, 1_000_000) == 0.0, slug

    # Priced and unknown models still produce a positive estimate.
    assert calculate_cost_usd("anthropic/claude-3.7-sonnet", 1_000_000, 0) > 0.0
    assert calculate_cost_usd("unknown/paid-model", 1_000_000, 0) > 0.0


def test_log_run_free_model_records_zero_cost():
    """Ledger entries for free models persist exactly $0 without telemetry."""
    entry = log_run(
        task_id="free-ledger",
        model="vendor/x:free",
        duration_s=1.0,
        steps=1,
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        cached_tokens=0,
        files_touched=[],
        lines_added=0,
        lines_removed=0,
    )
    assert entry["cost_usd"] == 0.0


def test_check_budget_limit_counts_today_spend():
    """check_budget_limit sums today's records and flags exceeded limits."""
    today = datetime.now().strftime("%Y-%m-%d")
    save_analytics(
        [
            {"timestamp": f"{today}T01:00:00", "cost_usd": 0.6},
            {"timestamp": f"{today}T02:00:00", "cost_usd": 0.6},
            {"timestamp": "2000-01-01T00:00:00", "cost_usd": 99.0},
        ]
    )
    exceeded, spend = check_budget_limit(1.0)
    assert exceeded is True
    assert spend == pytest.approx(1.2)
    exceeded, spend = check_budget_limit(5.0)
    assert exceeded is False
    assert spend == pytest.approx(1.2)
    # Disabled limits never trigger.
    assert check_budget_limit(0.0) == (False, 0.0)
    assert check_budget_limit(-1.0) == (False, 0.0)


def _fake_response(content: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
    )


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _patch_engine_for_budget(monkeypatch: pytest.MonkeyPatch, engine):
    monkeypatch.setattr(engine, "check_model_price", lambda *a, **k: (True, ""))
    monkeypatch.setattr(engine, "check_model_tools", lambda *a, **k: (True, ""))
    # Past spend already exceeds any positive daily limit.
    monkeypatch.setattr(engine, "check_budget_limit", lambda *a, **k: (True, 999.0))
    monkeypatch.setattr(engine, "log_run", lambda **kw: {"cost_usd": kw.get("cost_usd") or 0.0})
    monkeypatch.setattr(engine, "print_run_summary", lambda *a, **k: None)


def test_free_model_bypasses_budget_block(monkeypatch: pytest.MonkeyPatch):
    """A zero-cost run must not be aborted by BUDGET_BLOCK even when past
    spend already exceeds budget_limit_usd; a paid model is still blocked."""
    import botex.engine as engine

    _patch_engine_for_budget(monkeypatch, engine)

    with tempfile.TemporaryDirectory() as tmpdir:
        free_fake = _FakeClient([_fake_response("STATUS: DONE\nfree ok")])
        monkeypatch.setattr(engine, "AsyncOpenAI", lambda **_: free_fake)
        res = asyncio.run(
            engine.run_botex_task(
                task="t",
                workspace_dir=tmpdir,
                model="vendor/x:free",
                api_key="x",
                mode="readonly",
                allow_non_zdr=True,
                budget_limit_usd=1.0,
            )
        )
        assert res["status"] == "DONE", res
        assert len(free_fake.calls) == 1

        paid_fake = _FakeClient([_fake_response("STATUS: DONE\npaid")])
        monkeypatch.setattr(engine, "AsyncOpenAI", lambda **_: paid_fake)
        res = asyncio.run(
            engine.run_botex_task(
                task="t",
                workspace_dir=tmpdir,
                model="vendor/paid",
                api_key="x",
                mode="readonly",
                budget_limit_usd=1.0,
            )
        )
        assert res["status"] == "BUDGET_EXCEEDED", res
        assert len(paid_fake.calls) == 0


def test_ollama_provider_bypasses_budget_block(monkeypatch: pytest.MonkeyPatch):
    """provider='ollama' (local endpoint) is exempt via provider flags even
    with a bare model slug, and needs no external API key."""
    import botex.engine as engine

    _patch_engine_for_budget(monkeypatch, engine)

    with tempfile.TemporaryDirectory() as tmpdir:
        fake = _FakeClient([_fake_response("STATUS: DONE\nollama ok")])
        monkeypatch.setattr(engine, "AsyncOpenAI", lambda **_: fake)
        res = asyncio.run(
            engine.run_botex_task(
                task="t",
                workspace_dir=tmpdir,
                provider="ollama",
                model="SpeakLeash/bielik-11b-v3.0-instruct:Q4_K_M",
                api_key="",
                mode="readonly",
                budget_limit_usd=1.0,
            )
        )
        assert res["status"] == "DONE", res
        assert len(fake.calls) == 1


def test_check_health_reports_local_endpoint():
    """check_health surfaces local providers as active without an external key."""
    import server

    health = asyncio.run(server.check_health())
    assert "[OK] ollama: local endpoint active" in health
    assert "OLLAMA_API_KEY (check .env)" not in health
