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
import itertools
import math
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from botex.analytics import (
    _SHADOW_COST_FLOOR_USD,
    _effective_record_cost_usd,
    _within_days,
    aggregate_stats,
    calculate_cost_usd,
    check_budget_limit,
    get_shadow_routing_advice,
    is_free_model,
    log_run,
    recommend_shadow_model,
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


def test_is_free_model_namespace_prefix_requires_free_provider():
    """nvidia//ollama//gemini- prefixes mark free endpoints only while no
    billable provider is resolved — the same slug on a paid provider is priced."""
    # Resolved paid provider: namespace prefixes no longer apply.
    assert is_free_model("nvidia/nemotron-3-super-120b-a12b", {}, "openrouter") is False
    assert is_free_model("ollama/some-model", {}, "openrouter") is False
    assert is_free_model("gemini-2.5-pro", {}, "openrouter") is False
    # :free marker stays universal regardless of provider.
    assert is_free_model("nvidia/nemotron-3-ultra-550b-a55b:free", {}, "openrouter") is True
    # Free/quota providers and unresolved provider keep prefix heuristics.
    assert is_free_model("nvidia/anything", {}, "nvidia") is True
    assert is_free_model("nvidia/anything", {}, "openrouter-free") is True
    assert is_free_model("nvidia/anything") is True
    assert is_free_model("nvidia/anything", {}, "") is True


def test_is_free_model_provider_flags():
    """Local endpoints, package/quota providers (google, nvidia), and static api_key_fallback providers cannot bill per request."""
    assert is_free_model("any/model", {"local": True}) is True
    assert is_free_model("any/model", {"free_tier": True}) is True
    assert is_free_model("any/model", {"api_key_fallback": "ollama"}) is True
    assert is_free_model("any/model", provider_name="ollama") is True
    assert is_free_model("any/model", provider_name="OLLAMA") is True
    assert is_free_model("gemini-2.5-pro", provider_name="google") is True
    assert is_free_model("gemini-2.5-flash", provider_name="gemini") is True
    assert is_free_model("meta/llama-3.1-70b-instruct", provider_name="nvidia") is True
    assert is_free_model("any/model", provider_name="openrouter-free") is True
    assert is_free_model("any/model", {"local": False}, "openrouter") is False
    assert is_free_model("google/gemini-2.5-pro", {"local": False}, "openrouter") is False
    assert is_free_model("any/model", None, "") is False
    assert is_free_model("any/model", "not-a-dict", "") is False


def test_calculate_cost_usd_free_models_zero():
    """Pseudo-pricing never applies to free/zero-cost models or free-tier providers."""
    free_slugs = [
        "vendor/some-model:free",
        "openrouter/free",
        "openrouter-free/google/gemma-4-31b-it:free",
        "ollama/SpeakLeash/bielik-11b-v3.0-instruct:Q4_K_M",
        "nvidia/meta/llama-3.2-11b-vision-instruct",
    ]
    for slug in free_slugs:
        assert calculate_cost_usd(slug, 1_000_000, 1_000_000) == 0.0, slug

    assert calculate_cost_usd("gemini-2.5-pro", 1_000_000, 1_000_000, provider_name="google") == 0.0
    assert (
        calculate_cost_usd(
            "meta/llama-3.1-70b-instruct", 1_000_000, 1_000_000, provider_name="nvidia"
        )
        == 0.0
    )

    # Priced and unknown models on paid providers still produce a positive estimate.
    assert calculate_cost_usd("anthropic/claude-3.7-sonnet", 1_000_000, 0) > 0.0
    assert (
        calculate_cost_usd("google/gemini-2.5-pro", 1_000_000, 0, provider_name="openrouter") > 0.0
    )
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


def test_legacy_free_records_with_stale_cost_clamped_to_zero():
    """Historical records for free models/providers that stored pseudo-cost > 0
    must be clamped to $0.0 during aggregate_stats and check_budget_limit."""
    today = datetime.now().strftime("%Y-%m-%d")
    records = [
        {
            "task_id": "leg-google",
            "timestamp": f"{today}T10:00:00",
            "model": "gemini-2.5-flash",
            "provider": "google",
            "status": "API_ERROR",
            "prompt_tokens": 50000,
            "completion_tokens": 2000,
            "cost_usd": 0.026687,
        },
        {
            "task_id": "leg-nvidia",
            "timestamp": f"{today}T11:00:00",
            "model": "meta/llama-3.2-11b-vision-instruct",
            "provider": "nvidia",
            "status": "DONE",
            "prompt_tokens": 100000,
            "completion_tokens": 5000,
            "cost_usd": 0.105006,
        },
        {
            "task_id": "leg-or-free",
            "timestamp": f"{today}T12:00:00",
            "model": "cohere/north-mini-code:free",
            "provider": "openrouter-free",
            "status": "DONE",
            "prompt_tokens": 20000,
            "completion_tokens": 1000,
            "cost_usd": 0.028151,
        },
        {
            "task_id": "paid-or",
            "timestamp": f"{today}T13:00:00",
            "model": "deepseek/deepseek-v3.2",
            "provider": "openrouter",
            "status": "DONE",
            "prompt_tokens": 10000,
            "completion_tokens": 1000,
            "cost_usd": 0.05,
        },
    ]
    save_analytics(records)
    _, today_spend = check_budget_limit(10.0)
    assert today_spend == pytest.approx(0.05)

    stats = aggregate_stats(records)
    assert stats["cost_usd"] == pytest.approx(0.05)
    by_m = {m["model"]: m["cost_usd"] for m in stats["by_model"]}
    assert by_m["gemini-2.5-flash"] == 0.0
    assert by_m["meta/llama-3.2-11b-vision-instruct"] == 0.0
    assert by_m["cohere/north-mini-code:free"] == 0.0
    assert by_m["deepseek/deepseek-v3.2"] == pytest.approx(0.05)
    for r in stats["recent"]:
        if r["task_id"] != "paid-or":
            assert r["cost_usd"] == 0.0


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


def test_log_run_attribution_fields_and_guards(monkeypatch: pytest.MonkeyPatch):
    """log_run persists masked/flattened attribution fields and skips bool counters."""
    import botex.providers.analytics as analytics_mod

    written: list[dict] = []
    monkeypatch.setattr(analytics_mod, "_append_ledger", lambda e: written.append(dict(e)))

    # Legacy defaults omit new attribution keys to preserve record shape.
    legacy = log_run(
        task_id="t-legacy",
        model="vendor/free:free",
        duration_s=1.0,
        steps=1,
        prompt_tokens=10,
        completion_tokens=5,
        cached_tokens=0,
        files_touched=[],
        lines_added=0,
        lines_removed=0,
    )
    for key in (
        "origin",
        "recipe",
        "routed_tier",
        "taxonomy",
        "applied_skills",
        "applied_lessons",
        "syntax_rejections",
        "rollback_count",
    ):
        assert key not in legacy

    attributed = log_run(
        task_id="t-attr",
        model="vendor/free:free",
        duration_s=2.5,
        steps=4,
        prompt_tokens=100,
        completion_tokens=40,
        cached_tokens=10,
        files_touched=["botex/core/engine.py"],
        lines_added=3,
        lines_removed=1,
        origin="engine\ninjected",
        recipe="reviewer\r\n",
        routed_tier="free",
        taxonomy="bugfix",
        applied_skills=["code-review"],
        applied_lessons=["lesson_abc123"],
        syntax_rejections=2,
        rollback_count=1,
    )
    assert attributed["origin"] == "engine injected"
    assert attributed["recipe"] == "reviewer"
    assert attributed["routed_tier"] == "free"
    assert attributed["taxonomy"] == "bugfix"
    assert attributed["applied_skills"] == ["code-review"]
    assert attributed["applied_lessons"] == ["lesson_abc123"]
    assert attributed["syntax_rejections"] == 2
    assert attributed["rollback_count"] == 1

    # Bool must not coerce into integer counts (bool is a subclass of int in Python).
    bool_guarded = log_run(
        task_id="t-bool",
        model="vendor/free:free",
        duration_s=1.0,
        steps=1,
        prompt_tokens=1,
        completion_tokens=1,
        cached_tokens=0,
        files_touched=[],
        lines_added=0,
        lines_removed=0,
        syntax_rejections=True,  # type: ignore[arg-type]
        rollback_count=True,  # type: ignore[arg-type]
    )
    assert "syntax_rejections" not in bool_guarded
    assert "rollback_count" not in bool_guarded
    assert len(written) == 3


# ---------------------------------------------------------------------------
# Layer C — Shadow Meta-Routing Telemetry (PR 4)
# ---------------------------------------------------------------------------

_shadow_ids = itertools.count(1)


def _shadow_record(
    model: str,
    status: str = "DONE",
    *,
    task_id: str = "",
    recipe: str = "",
    taxonomy: str = "general",
    cost_usd=0.05,
    timestamp=None,
    provider: str = "",
    **extra,
):
    rec = {
        "task_id": task_id or f"task-{next(_shadow_ids)}",
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "model": model,
        "provider": provider,
        "status": status,
        "recipe": recipe,
        "taxonomy": taxonomy,
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cost_usd": cost_usd,
    }
    rec.update(extra)
    return rec


def test_recommend_shadow_model_requires_min_samples():
    """Buckets under ``min_samples`` return None; invalid min_samples raises."""
    recs = [_shadow_record("paid/model-a") for _ in range(9)]
    assert recommend_shadow_model(recs) is None

    recs.append(_shadow_record("paid/model-a"))
    out = recommend_shadow_model(recs)
    assert out is not None
    assert out["sample_count"] == 10
    assert out["recommended_model"] == "paid/model-a"

    for bad in (True, 0, -1, 1.5, "10"):
        with pytest.raises(ValueError):
            recommend_shadow_model(recs, min_samples=bad)

    single = recommend_shadow_model([_shadow_record("paid/solo")], min_samples=1)
    assert single is not None and single["model_samples"] == 1


def test_recommend_shadow_model_bucket_isolation():
    """Recommendations are scoped per (recipe, taxonomy) with normalization."""
    recs = [_shadow_record("paid/model-b", recipe="planner", taxonomy="bugfix") for _ in range(10)]
    assert recommend_shadow_model(recs, recipe="planner", taxonomy="general") is None
    assert recommend_shadow_model(recs, recipe="reviewer", taxonomy="bugfix") is None

    out = recommend_shadow_model(recs, recipe="  PLANNER  ", taxonomy="BugFix")
    assert out["recipe"] == "planner"
    assert out["taxonomy"] == "bugfix"
    assert out["recommended_model"] == "paid/model-b"


def test_recommend_shadow_model_cost_floor_free_vs_paid():
    """``_SHADOW_COST_FLOOR_USD`` keeps :free costs finite; reliability dominates."""
    # Reliable paid model still beats a low-DONE :free model.
    recs = [_shadow_record("paid/reliable", status="DONE", cost_usd=0.05) for _ in range(5)]
    recs += [_shadow_record("vendor/flaky:free", status="FAILED", cost_usd=0.0) for _ in range(5)]
    out = recommend_shadow_model(recs)
    assert out["recommended_model"] == "paid/reliable"
    assert math.isfinite(out["score"])

    # Equally reliable :free model wins via the floored cost bonus.
    recs2 = [_shadow_record("paid/decent", status="DONE", cost_usd=0.05) for _ in range(5)]
    recs2 += [_shadow_record("vendor/equal:free", status="DONE", cost_usd=0.0) for _ in range(5)]
    out2 = recommend_shadow_model(recs2)
    assert out2["recommended_model"] == "vendor/equal:free"
    assert out2["is_free"] is True
    assert out2["cost_floor_usd"] == _SHADOW_COST_FLOOR_USD


def test_recommend_shadow_model_zero_hardban_and_candidates():
    """Zero hard-ban: poor performers stay rankable and nothing is denylisted."""
    recs = [_shadow_record("paid/top", status="DONE", cost_usd=0.04) for _ in range(4)]
    recs += [_shadow_record("vendor/poor:free", status="FAILED") for _ in range(3)]
    recs += [_shadow_record("paid/mid", status="FAILED", cost_usd=0.03) for _ in range(3)]
    out = recommend_shadow_model(recs)
    assert out["recommended_model"] == "paid/top"
    assert out["shadow_only"] is True
    assert out["actual_override"] is False
    assert out["hard_banned_models"] == []
    names = [c["model"] for c in out["candidates"]]
    assert "vendor/poor:free" in names
    assert "paid/mid" in names
    assert out["candidates_considered"] == 3
    for cand in out["candidates"]:
        assert math.isfinite(cand["score"])
        assert math.isfinite(cand["avg_cost_usd"])
        assert math.isfinite(cand["floored_cost_usd"])


def test_effective_record_cost_usd_malformed_and_free():
    """Non-numeric / NaN / inf / bool / negative costs clamp to 0.0 — no crashes."""
    for bad_cost in ("not-a-number", float("nan"), float("inf"), float("-inf"), -1.0, True, None):
        rec = {"model": "paid/metered", "cost_usd": bad_cost}
        assert _effective_record_cost_usd(rec) == 0.0, bad_cost
    assert _effective_record_cost_usd({"model": "paid/metered", "cost_usd": 0.25}) == 0.25
    assert _effective_record_cost_usd({"model": "vendor/x:free", "cost_usd": 9.99}) == 0.0

    # Malformed costs flow through shadow scoring without non-finite results.
    recs = [
        _shadow_record("paid/weird", status="DONE", cost_usd=c)
        for c in ("x", float("nan"), float("inf"), True, -1.0, None, 0.02, 0.02, 0.02, 0.02)
    ]
    out = recommend_shadow_model(recs)
    assert out is not None
    assert math.isfinite(out["score"])
    cand = out["candidates"][0]
    assert math.isfinite(cand["avg_cost_usd"]) and cand["avg_cost_usd"] >= 0.0


def test_within_days_and_aggregate_mixed_timestamps():
    """Aware/naive/Z timestamps coexist; malformed ones are excluded, never raised."""
    naive_now = datetime.now().isoformat()
    aware_now = datetime.now(timezone.utc).isoformat()
    aware_z = aware_now.replace("+00:00", "Z")
    recs = [
        _shadow_record("paid/m", timestamp=naive_now),
        _shadow_record("paid/m", timestamp=aware_now),
        _shadow_record("paid/m", timestamp=aware_z),
        _shadow_record("paid/m", timestamp="2001-01-01T00:00:00"),
        _shadow_record("paid/m", timestamp="2001-01-01T00:00:00+00:00"),
        _shadow_record("paid/m", timestamp="not-a-timestamp"),
    ]
    stats = aggregate_stats(recs, days=1)
    assert stats["runs"] == 3
    assert aggregate_stats(recs, days=None)["runs"] == 6

    cutoff_naive = datetime.now() - timedelta(days=1)
    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=1)
    assert _within_days(naive_now, cutoff_naive, cutoff_utc) is True
    assert _within_days(aware_now, cutoff_naive, cutoff_utc) is True
    assert _within_days(aware_z, cutoff_naive, cutoff_utc) is True
    assert _within_days("2001-01-01T00:00:00", cutoff_naive, cutoff_utc) is False
    assert _within_days("2001-01-01T00:00:00+00:00", cutoff_naive, cutoff_utc) is False
    assert _within_days("garbage", cutoff_naive, cutoff_utc) is False
    assert _within_days("", cutoff_naive, cutoff_utc) is False
    assert _within_days("anything", None, None) is True

    mixed = [_shadow_record("paid/mm", timestamp=naive_now) for _ in range(5)]
    mixed += [_shadow_record("paid/mm", timestamp=aware_now) for _ in range(5)]
    assert recommend_shadow_model(mixed)["sample_count"] == 10


def test_log_run_shadow_recommended_model_masked_single_line(monkeypatch: pytest.MonkeyPatch):
    """``shadow_recommended_model`` persists masked/flattened only when provided."""
    import botex.providers.analytics as analytics_mod

    written: list[dict] = []
    monkeypatch.setattr(analytics_mod, "_append_ledger", lambda e: written.append(dict(e)))

    base = dict(
        task_id="t-shadow",
        model="vendor/live:free",
        duration_s=1.0,
        steps=1,
        prompt_tokens=1,
        completion_tokens=1,
        cached_tokens=0,
        files_touched=[],
        lines_added=0,
        lines_removed=0,
    )
    plain = log_run(**base)
    assert "shadow_recommended_model" not in plain

    entry = log_run(**base, shadow_recommended_model="best/model\r\ninjected")
    assert entry["shadow_recommended_model"] == "best/model injected"
    assert "\n" not in entry["shadow_recommended_model"]
    assert len(entry["shadow_recommended_model"]) <= 64
    assert written[-1]["shadow_recommended_model"] == "best/model injected"


def test_get_shadow_routing_advice_marks_divergence():
    """Advice is shadow-only: diverged flag set, actual_override never True."""
    recs = [_shadow_record("paid/best", status="DONE") for _ in range(10)]
    out = get_shadow_routing_advice(
        recipe="", taxonomy="general", actual_model="other/live", records=recs
    )
    assert out["recommended_model"] == "paid/best"
    assert out["actual_model"] == "other/live"
    assert out["diverged"] is True
    assert out["shadow_only"] is True
    assert out["actual_override"] is False

    same = get_shadow_routing_advice(
        recipe="", taxonomy="general", actual_model="paid/best", records=recs
    )
    assert same["diverged"] is False

    assert get_shadow_routing_advice(records=[_shadow_record("m/x")]) is None


def test_engine_shadow_advice_never_overrides_model(monkeypatch: pytest.MonkeyPatch):
    """Engine call site: advice runs off the event loop, lands in log_run and the
    SHADOW_ROUTING event — while the active model passed to the provider is unchanged."""
    import threading

    import botex.engine as engine
    from botex.events import EventKind

    monkeypatch.setattr(engine, "check_model_price", lambda *a, **k: (True, ""))
    monkeypatch.setattr(engine, "check_model_tools", lambda *a, **k: (True, ""))
    monkeypatch.setattr(engine, "check_budget_limit", lambda *a, **k: (False, 0.0))
    monkeypatch.setattr(engine, "print_run_summary", lambda *a, **k: None)

    logged: list[dict] = []
    monkeypatch.setattr(engine, "log_run", lambda **kw: logged.append(kw) or {"cost_usd": 0.0})

    advice_threads: list[bool] = []
    advice = {
        "recommended_model": "vendor/shadow-best:free",
        "recipe": "",
        "taxonomy": "general",
        "actual_model": "vendor/paid-live",
        "diverged": True,
        "sample_count": 25,
    }

    def _advice_spy(**kw):
        advice_threads.append(threading.current_thread() is not threading.main_thread())
        return dict(advice)

    monkeypatch.setattr(engine, "get_shadow_routing_advice", _advice_spy)

    def _run_once(events: list):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = _FakeClient([_fake_response("STATUS: DONE\nok")])
            monkeypatch.setattr(engine, "AsyncOpenAI", lambda **_: fake)
            res = asyncio.run(
                engine.run_botex_task(
                    task="t",
                    workspace_dir=tmpdir,
                    model="vendor/paid-live",
                    api_key="x",
                    mode="readonly",
                    allow_non_zdr=True,
                    progress_cb=lambda kind, **data: events.append((kind, data)),
                )
            )
        return res, fake

    events: list[tuple] = []
    res, fake = _run_once(events)
    assert res["status"] == "DONE", res

    # Advisory ran off the event loop (asyncio.to_thread worker).
    assert advice_threads and all(advice_threads)

    # Active model untouched; recommendation only lands in telemetry fields.
    assert fake.calls[0]["model"] == "vendor/paid-live"
    assert logged, "log_run never invoked"
    final_log = logged[-1]
    assert final_log["model"] == "vendor/paid-live"
    assert final_log["shadow_recommended_model"] == "vendor/shadow-best:free"

    shadow_events = [data for kind, data in events if kind == EventKind.SHADOW_ROUTING]
    assert len(shadow_events) == 1
    ev = shadow_events[0]
    assert ev["recommended_model"] == "vendor/shadow-best:free"
    assert ev["actual_model"] == "vendor/paid-live"
    assert ev["diverged"] is True
    assert ev["shadow_only"] is True

    # A failing advisor is swallowed: the run still completes, no shadow field.
    def _advice_boom(**kw):
        raise RuntimeError("shadow boom")

    monkeypatch.setattr(engine, "get_shadow_routing_advice", _advice_boom)
    events.clear()
    res2, _ = _run_once(events)
    assert res2["status"] == "DONE", res2
    assert "shadow_recommended_model" not in logged[-1]
    assert not [k for k, _ in events if k == EventKind.SHADOW_ROUTING]
