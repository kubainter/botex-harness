# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for task size and complexity tier estimation."""

import pytest

from botex.benchmarks import (
    estimate_task_tier,
    normalize_task_tier,
    resolve_tier_profile,
)


def test_estimate_task_tier_budget():
    tier = estimate_task_tier(
        prompt="Please check syntax and lint this single file",
        files=["file1.py"],
        outline_lines=150,
    )
    assert tier == "budget"


def test_estimate_task_tier_high():
    tier = estimate_task_tier(
        prompt="Design ADR and execute architectural migration across multiple services",
        files=["a.py", "b.py", "c.py", "d.py", "e.py"],
        outline_lines=2500,
    )
    assert tier == "high"


def test_estimate_task_tier_standard():
    tier = estimate_task_tier(
        prompt="Fix edge case in the user profile view",
        files=["views.py", "models.py"],
        outline_lines=500,
    )
    assert tier == "standard"


def test_estimate_task_tier_empty_task_is_standard():
    assert estimate_task_tier(prompt="", files=[], outline_lines=0) == "standard"


def test_normalize_task_tier_allowlist():
    assert normalize_task_tier("HIGH") == "high"
    assert normalize_task_tier(" budget ") == "budget"
    assert normalize_task_tier("made-up-tier") == "standard"
    assert normalize_task_tier(None) == "standard"


def test_resolve_tier_profile_mapping():
    tiers = {"high": "coding", "standard": "default", "budget": "fast"}
    assert resolve_tier_profile("high", tiers) == "coding"
    assert resolve_tier_profile("standard", tiers) == "default"
    assert resolve_tier_profile("budget", tiers) == "fast"


def test_resolve_tier_profile_malformed_config():
    assert resolve_tier_profile("high", None) == ""
    assert resolve_tier_profile("high", "not-a-dict") == ""
    assert resolve_tier_profile("high", {}) == ""
    assert resolve_tier_profile("high", {"high": "   "}) == ""
    assert resolve_tier_profile("high", {"high": 42}) == ""


def _routing_cfg(enabled, tiers=None):
    return {
        "engine": {
            "mode_profiles": {},
            "task_size_routing": {
                "enabled": enabled,
                "tiers": tiers
                if tiers is not None
                else {"high": "coding", "standard": "default", "budget": "fast"},
            },
        }
    }


def _capture_candidates(monkeypatch):
    import botex.engine as engine

    captured = {}

    def fake_candidates(model, profile, provider):
        captured.update(model=model, profile=profile, provider=provider)
        return []  # No candidates -> early CONFIG_ERROR, no provider touched.

    monkeypatch.setattr(engine, "resolve_model_candidates", fake_candidates)
    return captured


@pytest.mark.anyio
async def test_run_botex_task_routing_enabled_maps_tier(monkeypatch):
    import botex.engine as engine

    captured = _capture_candidates(monkeypatch)
    monkeypatch.setattr(engine, "load_config", lambda: _routing_cfg(True))
    result = await engine.run_botex_task(
        task="Design ADR and execute architectural migration",
        files=["a.py", "b.py", "c.py", "d.py"],
        workspace_dir=".",
    )
    assert result["status"] == "CONFIG_ERROR"
    assert captured["profile"] == "coding"


@pytest.mark.anyio
async def test_run_botex_task_routing_disabled_by_default(monkeypatch):
    import botex.engine as engine

    captured = _capture_candidates(monkeypatch)
    monkeypatch.setattr(engine, "load_config", lambda: _routing_cfg(False))
    result = await engine.run_botex_task(
        task="Design ADR and execute architectural migration",
        files=["a.py", "b.py", "c.py", "d.py"],
        workspace_dir=".",
    )
    assert result["status"] == "CONFIG_ERROR"
    assert captured["profile"] == ""


@pytest.mark.anyio
async def test_run_botex_task_explicit_model_bypasses_routing(monkeypatch):
    import botex.engine as engine

    captured = _capture_candidates(monkeypatch)
    monkeypatch.setattr(engine, "load_config", lambda: _routing_cfg(True))
    await engine.run_botex_task(
        task="Architectural migration",
        files=["a.py", "b.py", "c.py", "d.py"],
        model="explicit/model",
        workspace_dir=".",
    )
    assert captured["model"] == "explicit/model"
    assert captured["profile"] == ""


@pytest.mark.anyio
async def test_run_botex_task_explicit_profile_bypasses_routing(monkeypatch):
    import botex.engine as engine

    captured = _capture_candidates(monkeypatch)
    monkeypatch.setattr(engine, "load_config", lambda: _routing_cfg(True))
    await engine.run_botex_task(
        task="Architectural migration",
        files=["a.py", "b.py", "c.py", "d.py"],
        profile="custom-profile",
        workspace_dir=".",
    )
    assert captured["profile"] == "custom-profile"


@pytest.mark.anyio
async def test_run_botex_task_mode_profile_beats_routing(monkeypatch):
    import botex.engine as engine

    captured = _capture_candidates(monkeypatch)
    cfg = _routing_cfg(True)
    cfg["engine"]["mode_profiles"] = {"edit": "mode-picked"}
    monkeypatch.setattr(engine, "load_config", lambda: cfg)
    await engine.run_botex_task(
        task="Architectural migration",
        files=["a.py", "b.py", "c.py", "d.py"],
        mode="edit",
        workspace_dir=".",
    )
    assert captured["profile"] == "mode-picked"


@pytest.mark.anyio
async def test_run_botex_task_routing_malformed_config(monkeypatch):
    import botex.engine as engine

    captured = _capture_candidates(monkeypatch)
    monkeypatch.setattr(
        engine,
        "load_config",
        lambda: {"engine": {"mode_profiles": {}, "task_size_routing": "yes"}},
    )
    result = await engine.run_botex_task(task="x", workspace_dir=".")
    assert result["status"] == "CONFIG_ERROR"
    assert captured["profile"] == ""


@pytest.mark.anyio
async def test_run_botex_task_routing_missing_tier_key_falls_back(monkeypatch):
    import botex.engine as engine

    captured = _capture_candidates(monkeypatch)
    monkeypatch.setattr(engine, "load_config", lambda: _routing_cfg(True, tiers={}))
    await engine.run_botex_task(
        task="Architectural migration",
        files=["a.py", "b.py", "c.py", "d.py"],
        workspace_dir=".",
    )
    assert captured["profile"] == ""


@pytest.mark.anyio
async def test_run_botex_task_routed_profile_reaches_result(monkeypatch):
    """A routed task emits routed_tier/routed_profile in events and result."""
    import botex.engine as engine

    events = []

    def fake_candidates(model, profile, provider):
        return ["candidate-model"]

    async def fake_once(**kwargs):
        return {
            "ok": True,
            "task_id": "t1",
            "status": "DONE",
            "files_touched": [],
            "steps": 1,
        }

    monkeypatch.setattr(engine, "resolve_model_candidates", fake_candidates)
    monkeypatch.setattr(engine, "_run_botex_task_once", fake_once)
    monkeypatch.setattr(engine, "load_config", lambda: _routing_cfg(True))
    result = await engine.run_botex_task(
        task="Design ADR and execute architectural migration",
        files=["a.py", "b.py", "c.py", "d.py"],
        workspace_dir=".",
        progress_cb=lambda kind, **data: events.append((kind, data)),
    )
    assert result.get("routed_tier") == "high"
    assert result.get("routed_profile") == "coding"
    resolved = [d for k, d in events if k == engine.EventKind.PROVIDER_RESOLVED]
    assert resolved and resolved[0].get("routed_tier") == "high"
