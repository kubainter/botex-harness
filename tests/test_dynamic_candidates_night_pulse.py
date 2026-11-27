# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Plan 059 (wave 1-B): Night Pulse dynamic-candidates snapshot tests.

Covers the maintenance-layer offline generator end to end: mode gating
(zero-I/O off skip), atomic schema-valid artifact writes, malformed-input
tolerance, stale-prior-state preservation, per-provider isolation, zero
network, secret canaries, and the curator wiring.

The ``botex.providers.dynamic_candidates`` policy module (plan 059 wave 1-A)
lands in parallel against a frozen interface contract. While it is absent
the maintenance layer is exercised through a contract-faithful fake injected
via ``sys.modules``; the same matrix re-runs against the real module through
``@pytest.mark.skipif(not HAS_REAL, ...)`` once it lands.

No test performs network: sockets are blocked autouse and provider sync plus
HTTP plumbing are monkeypatched to raise.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import types
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from botex.ops import maintenance
from botex.ops.curator import tasks as curator_tasks

try:  # pragma: no cover - branch depends on wave 1-A landing state
    from botex.providers import dynamic_candidates as _real_dynamic_candidates  # noqa: F401

    HAS_REAL = True
except Exception:
    _real_dynamic_candidates = None
    HAS_REAL = False

_MODES = ("off", "shadow", "append")
_VALID_CANDIDATE_STATES = {"active", "eligible", "observed", "quarantined"}


# ---------------------------------------------------------------------------
# Hermetic environment: no sockets, no emergency-env leakage between tests.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Ensure no live network calls can be made during night pulse tests."""

    def blocked_connect(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Blocked live network socket call during unit test execution")

    with patch.object(socket.socket, "connect", side_effect=blocked_connect):
        yield


@pytest.fixture(autouse=True)
def clear_dynamic_mode_env(monkeypatch):
    monkeypatch.delenv("BOTEX_DYNAMIC_CANDIDATES_MODE", raising=False)


# ---------------------------------------------------------------------------
# Contract-faithful fake of botex.providers.dynamic_candidates (wave 1-A).
# Only the frozen interface + the plan-documented artifact schema; evidence
# extraction reads whitelisted fields only, mirroring the no-leak invariant.
# ---------------------------------------------------------------------------


def _fake_dynamic_candidates_module() -> types.ModuleType:
    mod = types.ModuleType("botex.providers.dynamic_candidates")
    calls: List[Dict[str, Any]] = []
    mod.DEFAULT_SNAPSHOT_PATH = Path(".botex/dynamic_model_candidates.json")
    mod.DEFAULT_MAX_SNAPSHOT_AGE_HOURS = 72.0
    # tests reset/read this through the fixture-returned module object
    mod.CALLS = calls

    def normalize_dynamic_mode(configured_mode: Any, *, emergency_env: str = None) -> str:
        env = str(emergency_env or "").strip().lower()
        if env in _MODES:
            return env
        raw = str(configured_mode or "").strip().lower()
        if raw in _MODES:
            return raw
        return "off"

    def load_dynamic_snapshot_offline(path=None, *, now_ts=None, max_age_hours=72.0):
        target = Path(path) if path is not None else mod.DEFAULT_SNAPSHOT_PATH
        try:
            raw = json.loads(target.read_text(encoding="utf-8")) if target.exists() else None
        except Exception:
            return (None, "malformed")
        if not isinstance(raw, dict):
            return (None, "missing")
        try:
            generated_at = float(raw.get("generated_at"))
        except (TypeError, ValueError):
            return (None, "malformed")
        if max_age_hours and max_age_hours > 0:
            now = float(now_ts) if now_ts is not None else time.time()
            if (now - generated_at) > float(max_age_hours) * 3600.0:
                return (raw, "stale")
        # max_age_hours=0 -> freshness gate disabled (generator-side reuse of
        # prior state); the runtime hot path keeps the 72h default.
        return (raw, "ok")

    def build_dynamic_candidates_snapshot(
        *,
        cfg,
        catalogs,
        analytics_rows,
        catalog_fetched_at_map=None,
        catalog_drift_generated_at=None,
        existing_snapshot=None,
        denylist_check=None,
        now_ts=None,
        generator="night-pulse",
    ):
        if now_ts is None:
            now_ts = time.time()
        calls.append(
            {
                "cfg": cfg,
                "catalogs": catalogs,
                "analytics_rows": list(analytics_rows or []),
                "catalog_fetched_at_map": catalog_fetched_at_map,
                "catalog_drift_generated_at": catalog_drift_generated_at,
                "existing_snapshot": existing_snapshot,
                "generator": generator,
            }
        )

        # Evidence: whitelisted fields only. Summaries, task ids, file lists
        # and any other free text never flow into the snapshot.
        evidence: Dict[tuple, Dict[str, int]] = {}
        for row in analytics_rows or []:
            if not isinstance(row, dict):
                continue
            prov = str(row.get("provider") or "")
            model = str(row.get("model") or "")
            if not prov or not model:
                continue
            ev = evidence.setdefault(
                (prov, model), {"runs": 0, "done_runs": 0, "quality_failures": 0}
            )
            ev["runs"] += 1
            if str(row.get("status") or "") == "DONE":
                ev["done_runs"] += 1
            else:
                ev["quality_failures"] += 1

        existing_providers: Dict[str, Any] = {}
        if isinstance(existing_snapshot, dict):
            prior = existing_snapshot.get("providers")
            if isinstance(prior, dict):
                existing_providers = prior

        providers_out: Dict[str, Any] = {}
        for name, models in (catalogs or {}).items():
            key = str(name)
            if models:
                candidates: List[Dict[str, Any]] = []
                for mid in sorted(models.keys()):
                    ev = evidence.get(
                        (key, str(mid)),
                        {"runs": 0, "done_runs": 0, "quality_failures": 0},
                    )
                    denied = bool(denylist_check(key, str(mid))) if denylist_check else False
                    if denied:
                        state = "observed"
                    elif ev["runs"] >= 2 and ev["done_runs"] >= 1:
                        state = "active"
                    elif ev["runs"] and ev["quality_failures"] >= 2:
                        state = "quarantined"
                    else:
                        state = "eligible"
                    candidates.append(
                        {
                            "model": str(mid),
                            "state": state,
                            "evidence": {
                                "runs": ev["runs"],
                                "done_runs": ev["done_runs"],
                                "quality_failures": ev["quality_failures"],
                            },
                        }
                    )
                providers_out[key] = {"catalog_state": "fresh", "candidates": candidates}
            else:
                prior_entry = existing_providers.get(key)
                if isinstance(prior_entry, dict):
                    kept = dict(prior_entry)
                    kept["catalog_state"] = "stale_preserved"
                    providers_out[key] = kept
                else:
                    providers_out[key] = {"catalog_state": "no_data", "candidates": []}

        return {
            "schema_version": 1,
            "generated_at": float(now_ts),
            "generator": str(generator),
            "source": {
                "catalog_drift_generated_at": catalog_drift_generated_at,
                "analytics_window_days": 30,
            },
            "providers": providers_out,
        }

    mod.normalize_dynamic_mode = normalize_dynamic_mode
    mod.load_dynamic_snapshot_offline = load_dynamic_snapshot_offline
    mod.build_dynamic_candidates_snapshot = build_dynamic_candidates_snapshot
    return mod


@pytest.fixture
def dyn_fake(monkeypatch):
    """Inject the contract-faithful fake as botex.providers.dynamic_candidates.

    Patches both sys.modules and the package attribute so every import style
    (``from botex.providers import dynamic_candidates`` included) resolves to
    the fake for the duration of the test.
    """
    fake = _fake_dynamic_candidates_module()
    monkeypatch.setitem(sys.modules, "botex.providers.dynamic_candidates", fake)
    import botex.providers as providers_pkg

    monkeypatch.setattr(providers_pkg, "dynamic_candidates", fake, raising=False)
    return fake


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _row(
    model: str,
    provider: str,
    status: str = "DONE",
    *,
    taxonomy: str = "analysis",
    summary: str = "ok",
    task_id: str = "t-1",
) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "timestamp": "2026-11-26T12:00:00",
        "model": model,
        "provider": provider,
        "status": status,
        "failure_kind": None,
        "taxonomy": taxonomy,
        "syntax_rejections": 0,
        "rollback_count": 0,
        "summary": summary,
    }


def _model_meta(model_id: str, **overrides: Any) -> Dict[str, Any]:
    meta = {
        "id": model_id,
        "name": model_id,
        "context_length": 131072,
        "prompt_price_per_m": 0.0,
        "completion_price_per_m": 0.0,
        "supports_tools": True,
        "is_zdr_compliant": True,
    }
    meta.update(overrides)
    return meta


def _cache_entry(catalog: Any, *, fetched_at: float = None) -> Dict[str, Any]:
    return {
        "fetched_at": fetched_at if fetched_at is not None else time.time() - 100.0,
        "catalog": catalog,
    }


def _cache_key(provider: str, provider_cfg: Dict[str, Any], cache_file: Path) -> str:
    from botex.providers.catalog import ModelCatalog

    return ModelCatalog(
        provider_name=provider,
        provider_cfg=provider_cfg if isinstance(provider_cfg, dict) else None,
        cache_file=cache_file,
    ).cache_key


def _write_catalog_cache(cache_file: Path, entries: Dict[str, Dict[str, Any]]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"catalogs": entries}), encoding="utf-8")


def _write_ledger(path: Path, rows: List[Any]) -> Path:
    """Write ledger rows verbatim — list items may be dicts or raw junk lines."""
    lines = [r if isinstance(r, str) else json.dumps(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _dc_cfg(
    cache_file: Path,
    *,
    mode: str = "shadow",
    snapshot_file: str = ".botex/dynamic_model_candidates.json",
    dc_providers: Any = None,
) -> Dict[str, Any]:
    if dc_providers is None:
        dc_providers = {"groq": {"enabled": True, "allow_models": ["groq/*"], "deny_models": []}}
    return {
        "catalog_sync": {"enabled": True, "providers": ["groq", "nvidia"], "timeout_s": 5.0},
        "catalog": {"cache_file": str(cache_file)},
        "dynamic_candidates": {
            "mode": mode,
            "snapshot_file": snapshot_file,
            "providers": dc_providers,
        },
        "providers": {
            "groq": {"models": {"default": "groq/model-a"}},
            "nvidia": {"models": {"default": "nvidia/model-b"}},
        },
    }


def _real_policy_dc_section(mode: str = "shadow") -> Dict[str, Any]:
    """Plan-059 documented configuration shape for the real 1-A builder."""
    return {
        "mode": mode,
        "snapshot_file": ".botex/dynamic_model_candidates.json",
        "max_per_provider": 2,
        "max_total_per_task": 2,
        "providers": {
            "groq": {
                "enabled": True,
                "allow_models": ["groq/*"],
                "deny_models": [],
            }
        },
        "task_classes": {
            "analysis": {
                "enabled": True,
                "allow_unproven": True,
                "min_context_length": 32768,
                "allow_paid": False,
            },
        },
        "modes": {"readonly": {"enabled": True}, "edit": {"enabled": True}},
        "promotion": {
            "window_days": 30,
            "min_runs": 3,
            "min_done_runs": 2,
            "min_done_rate": 0.6,
            "max_rollback_rate": 0.34,
            "max_syntax_rejections_per_run": 0.5,
            "quarantine_after_consecutive_quality_failures": 2,
            "quarantine_hours": 72,
        },
    }


def _groq_setup(tmp_path: Path, *, rows: Any = None) -> Dict[str, Any]:
    """Cache with a healthy two-model groq catalog + evidence ledger rows.

    Default rows give groq/model-a 2 DONE + 1 quality failure (-> active in
    the fake) while groq/model-b stays unproven (-> eligible); nvidia has no
    cache entry (-> no_data).
    """
    cache = tmp_path / "catalog_cache.json"
    cfg = _dc_cfg(cache)
    key = _cache_key("groq", cfg["providers"]["groq"], cache)
    _write_catalog_cache(
        cache,
        {
            key: _cache_entry(
                {
                    "groq/model-a": _model_meta("groq/model-a"),
                    "groq/model-b": _model_meta("groq/model-b"),
                }
            )
        },
    )
    ledger = tmp_path / ".agent_analytics.jsonl"
    if rows is None:
        rows = [
            _row("groq/model-a", "groq", status="DONE"),
            _row("groq/model-a", "groq", status="DONE"),
            _row("groq/model-a", "groq", status="FAILED"),
        ]
    _write_ledger(ledger, rows)
    return {
        "cfg": cfg,
        "cache": cache,
        "ledger": ledger,
        "snapshot": tmp_path / "snap" / "dynamic_model_candidates.json",
    }


def _read_snapshot(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Mode gating
# ---------------------------------------------------------------------------


def test_mode_off_skips_disk_reads_and_writes(dyn_fake, tmp_path, monkeypatch):
    setup = _groq_setup(tmp_path)
    cfg = _dc_cfg(setup["cache"], mode="off")
    snapshot = setup["snapshot"]
    ledger = setup["ledger"]

    def _boom(*args, **kwargs):
        raise AssertionError("mode=off must not read catalogs or analytics")

    monkeypatch.setattr(maintenance, "load_cached_catalogs", _boom)
    monkeypatch.setattr(maintenance, "_stream_analytics_rows", _boom)
    monkeypatch.setattr(maintenance, "_catalog_fetched_at_map", _boom)

    result = maintenance.run_dynamic_candidates_sync(
        cfg, snapshot_path=snapshot, analytics_path=ledger
    )
    assert result == {"status": "skipped", "mode": "off", "snapshot_path": ""}
    assert not snapshot.exists()
    assert dyn_fake.CALLS == []


def test_mode_off_skips_even_when_snapshot_and_ledger_exist(dyn_fake, tmp_path):
    setup = _groq_setup(tmp_path)
    cfg = _dc_cfg(setup["cache"], mode="off")
    snapshot = setup["snapshot"]
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        '{"schema_version": 1, "generated_at": 1.0, "providers": {}}', encoding="utf-8"
    )
    before = snapshot.read_text(encoding="utf-8")

    result = maintenance.run_dynamic_candidates_sync(
        cfg, snapshot_path=snapshot, analytics_path=setup["ledger"]
    )
    assert result["status"] == "skipped"
    assert snapshot.read_text(encoding="utf-8") == before


def test_mode_shadow_writes_schema_valid_atomic_snapshot(dyn_fake, tmp_path):
    setup = _groq_setup(tmp_path)
    result = maintenance.run_dynamic_candidates_sync(
        setup["cfg"], snapshot_path=setup["snapshot"], analytics_path=setup["ledger"]
    )
    assert result["status"] == "ok"
    assert result["mode"] == "shadow"
    assert result["snapshot_path"] == str(setup["snapshot"])
    assert result["providers"] == 2  # groq + nvidia (nvidia has no cache entry)
    assert result["counts"] == {"active": 1, "eligible": 1, "observed": 0, "quarantined": 0}

    parsed = _read_snapshot(setup["snapshot"])
    assert parsed["schema_version"] == 1
    assert parsed["generator"] == "night-pulse"
    assert isinstance(parsed["generated_at"], float)
    groq = parsed["providers"]["groq"]
    assert groq["catalog_state"] == "fresh"
    by_model = {c["model"]: c["state"] for c in groq["candidates"]}
    assert by_model == {"groq/model-a": "active", "groq/model-b": "eligible"}
    nvidia = parsed["providers"]["nvidia"]
    assert nvidia["catalog_state"] == "no_data"
    assert nvidia["candidates"] == []
    # atomic write: no torn temp files remain
    assert list(setup["snapshot"].parent.glob("*.tmp")) == []


def test_mode_append_writes_snapshot_with_append_mode(dyn_fake, tmp_path):
    setup = _groq_setup(tmp_path)
    cfg = _dc_cfg(setup["cache"], mode="append")
    result = maintenance.run_dynamic_candidates_sync(
        cfg, snapshot_path=setup["snapshot"], analytics_path=setup["ledger"]
    )
    assert result["status"] == "ok"
    assert result["mode"] == "append"
    assert setup["snapshot"].exists()


def test_invalid_configured_mode_fails_safe_to_off(dyn_fake, tmp_path):
    setup = _groq_setup(tmp_path)
    cfg = _dc_cfg(setup["cache"], mode="definitely-not-a-mode")
    result = maintenance.run_dynamic_candidates_sync(
        cfg, snapshot_path=setup["snapshot"], analytics_path=setup["ledger"]
    )
    assert result == {"status": "skipped", "mode": "off", "snapshot_path": ""}
    assert not setup["snapshot"].exists()


def test_emergency_env_can_enable_when_config_says_off(dyn_fake, tmp_path, monkeypatch):
    setup = _groq_setup(tmp_path)
    cfg = _dc_cfg(setup["cache"], mode="off")
    monkeypatch.setenv("BOTEX_DYNAMIC_CANDIDATES_MODE", "shadow")
    result = maintenance.run_dynamic_candidates_sync(
        cfg, snapshot_path=setup["snapshot"], analytics_path=setup["ledger"]
    )
    assert result["status"] == "ok"
    assert result["mode"] == "shadow"
    assert setup["snapshot"].exists()


def test_emergency_env_off_kills_configured_shadow(dyn_fake, tmp_path, monkeypatch):
    setup = _groq_setup(tmp_path)
    monkeypatch.setenv("BOTEX_DYNAMIC_CANDIDATES_MODE", "off")
    result = maintenance.run_dynamic_candidates_sync(
        setup["cfg"], snapshot_path=setup["snapshot"], analytics_path=setup["ledger"]
    )
    assert result == {"status": "skipped", "mode": "off", "snapshot_path": ""}
    assert not setup["snapshot"].exists()


# ---------------------------------------------------------------------------
# 2. Artifact robustness
# ---------------------------------------------------------------------------


def test_malformed_existing_snapshot_recovers_cleanly(dyn_fake, tmp_path):
    setup = _groq_setup(tmp_path)
    snapshot = setup["snapshot"]
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text("{ this is definitely not json", encoding="utf-8")

    result = maintenance.run_dynamic_candidates_sync(
        setup["cfg"], snapshot_path=snapshot, analytics_path=setup["ledger"]
    )
    assert result["status"] == "ok"
    parsed = _read_snapshot(snapshot)
    assert parsed["schema_version"] == 1
    assert parsed["providers"]["groq"]["catalog_state"] == "fresh"


def test_stale_cache_preserves_prior_provider_state(dyn_fake, tmp_path):
    # This run's cache covers nvidia only; groq's previous snapshot must
    # survive instead of being wiped by the now-empty groq catalog.
    cache = tmp_path / "catalog_cache.json"
    cfg = _dc_cfg(cache)
    nvidia_key = _cache_key("nvidia", cfg["providers"]["nvidia"], cache)
    _write_catalog_cache(
        cache,
        {nvidia_key: _cache_entry({"nvidia/model-b": _model_meta("nvidia/model-b")})},
    )
    ledger = _write_ledger(tmp_path / ".agent_analytics.jsonl", [])
    snapshot = tmp_path / "snap" / "dynamic_model_candidates.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    prior_candidate = {
        "model": "groq/old:free",
        "state": "eligible",
        "evidence": {"runs": 2, "done_runs": 2, "quality_failures": 0},
    }
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": time.time() - 7200.0,
                "providers": {"groq": {"catalog_state": "fresh", "candidates": [prior_candidate]}},
            }
        ),
        encoding="utf-8",
    )

    result = maintenance.run_dynamic_candidates_sync(
        cfg, snapshot_path=snapshot, analytics_path=ledger
    )
    assert result["status"] == "ok"

    # The generator loaded and forwarded the prior snapshot for preservation.
    call = dyn_fake.CALLS[-1]
    assert isinstance(call["existing_snapshot"], dict)
    prior_forwarded = call["existing_snapshot"]["providers"]["groq"]["candidates"]
    assert prior_forwarded[0]["model"] == "groq/old:free"

    parsed = _read_snapshot(snapshot)
    assert parsed["providers"]["groq"]["catalog_state"] == "stale_preserved"
    assert parsed["providers"]["groq"]["candidates"] == [prior_candidate]
    assert parsed["providers"]["nvidia"]["catalog_state"] == "fresh"


def test_one_malformed_provider_catalog_does_not_break_others(dyn_fake, tmp_path):
    cache = tmp_path / "catalog_cache.json"
    cfg = _dc_cfg(cache)
    groq_key = _cache_key("groq", cfg["providers"]["groq"], cache)
    nvidia_key = _cache_key("nvidia", cfg["providers"]["nvidia"], cache)
    _write_catalog_cache(
        cache,
        {
            # malformed: catalog payload is a list, not a dict
            groq_key: _cache_entry(["not", "a", "dict"]),
            nvidia_key: _cache_entry({"nvidia/model-b": _model_meta("nvidia/model-b")}),
        },
    )
    ledger = _write_ledger(tmp_path / ".agent_analytics.jsonl", [])
    snapshot = tmp_path / "snap" / "dynamic_model_candidates.json"

    result = maintenance.run_dynamic_candidates_sync(
        cfg, snapshot_path=snapshot, analytics_path=ledger
    )
    assert result["status"] == "ok"
    parsed = _read_snapshot(snapshot)
    # groq degrades to honest no_data instead of crashing the whole snapshot
    assert parsed["providers"]["groq"]["catalog_state"] == "no_data"
    assert parsed["providers"]["groq"]["candidates"] == []
    # nvidia is unaffected by groq's malformed entry
    assert parsed["providers"]["nvidia"]["catalog_state"] == "fresh"
    nvidia_models = {c["model"] for c in parsed["providers"]["nvidia"]["candidates"]}
    assert nvidia_models == {"nvidia/model-b"}


def test_empty_catalog_marks_no_data_without_inventing_candidates(dyn_fake, tmp_path):
    cache = tmp_path / "catalog_cache.json"
    cfg = _dc_cfg(cache)
    groq_key = _cache_key("groq", cfg["providers"]["groq"], cache)
    _write_catalog_cache(cache, {groq_key: _cache_entry({})})  # present but empty
    ledger = _write_ledger(
        tmp_path / ".agent_analytics.jsonl",
        [_row("groq/model-a", "groq", status="DONE")],  # evidence exists for it
    )
    snapshot = tmp_path / "snap" / "dynamic_model_candidates.json"

    result = maintenance.run_dynamic_candidates_sync(
        cfg, snapshot_path=snapshot, analytics_path=ledger
    )
    assert result["status"] == "ok"
    parsed = _read_snapshot(snapshot)
    assert parsed["providers"]["groq"]["catalog_state"] == "no_data"
    assert parsed["providers"]["groq"]["candidates"] == []
    # No candidate may be invented for a model the catalog does not list
    all_models = {
        c["model"] for entry in parsed["providers"].values() for c in entry.get("candidates", [])
    }
    assert "groq/model-a" not in all_models


def test_write_failure_returns_error_and_preserves_previous_file(dyn_fake, tmp_path, monkeypatch):
    setup = _groq_setup(tmp_path)
    snapshot = setup["snapshot"]
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text("ORIGINAL-CONTENT", encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("os.replace", _boom)
    result = maintenance.run_dynamic_candidates_sync(
        setup["cfg"], snapshot_path=snapshot, analytics_path=setup["ledger"]
    )
    assert result == {"status": "error", "error": "write_failed", "mode": "shadow"}
    assert snapshot.read_text(encoding="utf-8") == "ORIGINAL-CONTENT"
    assert list(snapshot.parent.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# 3. Input tolerance: analytics ledger
# ---------------------------------------------------------------------------


def test_malformed_analytics_lines_are_tolerated(dyn_fake, tmp_path):
    setup = _groq_setup(tmp_path)
    good_row = _row("groq/model-a", "groq", status="DONE")
    _write_ledger(
        setup["ledger"],
        [
            good_row,
            "{{{ not json at all",
            "",
            "[1, 2, 3]",  # valid JSON, not a dict
            '"just a string"',  # valid JSON, not a dict
            "null",  # valid JSON, not a dict
            good_row,
        ],
    )
    result = maintenance.run_dynamic_candidates_sync(
        setup["cfg"], snapshot_path=setup["snapshot"], analytics_path=setup["ledger"]
    )
    assert result["status"] == "ok"
    call = dyn_fake.CALLS[-1]
    assert len(call["analytics_rows"]) == 2
    assert all(r["model"] == "groq/model-a" for r in call["analytics_rows"])


def test_missing_analytics_file_is_not_an_error(dyn_fake, tmp_path):
    setup = _groq_setup(tmp_path)
    missing = tmp_path / "does_not_exist.jsonl"
    result = maintenance.run_dynamic_candidates_sync(
        setup["cfg"], snapshot_path=setup["snapshot"], analytics_path=missing
    )
    assert result["status"] == "ok"
    assert dyn_fake.CALLS[-1]["analytics_rows"] == []


# ---------------------------------------------------------------------------
# 4. Plumbing: cache metadata, drift report, stderr, path resolution
# ---------------------------------------------------------------------------


def test_cache_fetched_at_map_and_drift_report_forwarded(dyn_fake, tmp_path):
    setup = _groq_setup(tmp_path)
    fetched_at = time.time() - 42.0
    key = _cache_key("groq", setup["cfg"]["providers"]["groq"], setup["cache"])
    _write_catalog_cache(
        setup["cache"],
        {
            key: _cache_entry(
                {
                    "groq/model-a": _model_meta("groq/model-a"),
                    "groq/model-b": _model_meta("groq/model-b"),
                },
                fetched_at=fetched_at,
            )
        },
    )
    drift_ts = time.time() - 10.0
    result = maintenance.run_dynamic_candidates_sync(
        setup["cfg"],
        snapshot_path=setup["snapshot"],
        analytics_path=setup["ledger"],
        drift_report={"generated_at": drift_ts},
    )
    assert result["status"] == "ok"
    call = dyn_fake.CALLS[-1]
    assert call["catalog_fetched_at_map"] == {"groq": fetched_at}
    assert call["catalog_drift_generated_at"] == drift_ts
    parsed = _read_snapshot(setup["snapshot"])
    assert parsed["source"]["catalog_drift_generated_at"] == drift_ts


def test_stderr_summary_is_count_only(dyn_fake, tmp_path, capsys):
    setup = _groq_setup(tmp_path)
    result = maintenance.run_dynamic_candidates_sync(
        setup["cfg"], snapshot_path=setup["snapshot"], analytics_path=setup["ledger"]
    )
    assert result["status"] == "ok"
    err = capsys.readouterr().err
    lines = [l for l in err.splitlines() if "dynamic candidates snapshot:" in l]
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("[BoteX] dynamic candidates snapshot: mode=shadow providers=2")
    for token in ("active=", "eligible=", "quarantined="):
        assert token in line
    # counts only — no model ids, paths, or free text
    assert "groq/model-a" not in line
    assert str(setup["snapshot"]) not in line


def test_snapshot_path_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr("botex.server.config.PROJECT_ROOT", tmp_path)
    # default: module contract path anchored at PROJECT_ROOT
    assert maintenance.dynamic_candidates_snapshot_path() == (
        tmp_path / ".botex" / "dynamic_model_candidates.json"
    )
    # configured relative path is PROJECT_ROOT-anchored
    cfg = {"dynamic_candidates": {"snapshot_file": "custom/dir/snap.json"}}
    assert maintenance.dynamic_candidates_snapshot_path(cfg) == (
        tmp_path / "custom" / "dir" / "snap.json"
    )
    # absolute path is preserved as configured
    absolute = tmp_path / "elsewhere" / "snap.json"
    cfg_abs = {"dynamic_candidates": {"snapshot_file": str(absolute)}}
    assert maintenance.dynamic_candidates_snapshot_path(cfg_abs) == absolute


def test_run_honors_configured_snapshot_file(dyn_fake, tmp_path, monkeypatch):
    monkeypatch.setattr("botex.server.config.PROJECT_ROOT", tmp_path)
    cache = tmp_path / "catalog_cache.json"
    cfg = _dc_cfg(cache, snapshot_file="dyn/snap.json")
    key = _cache_key("groq", cfg["providers"]["groq"], cache)
    _write_catalog_cache(cache, {key: _cache_entry({"groq/model-a": _model_meta("groq/model-a")})})
    ledger = _write_ledger(tmp_path / ".agent_analytics.jsonl", [])

    result = maintenance.run_dynamic_candidates_sync(cfg, analytics_path=ledger)
    expected = tmp_path / "dyn" / "snap.json"
    assert result["status"] == "ok"
    assert result["snapshot_path"] == str(expected)
    assert expected.exists()
    assert _read_snapshot(expected)["schema_version"] == 1


def test_cache_file_override_beats_cfg_derived_cache(dyn_fake, tmp_path):
    cache_a = tmp_path / "cache_a.json"
    cache_b = tmp_path / "cache_b.json"
    cfg = _dc_cfg(cache_a)  # cfg points at cache_a...
    key_b = _cache_key("groq", cfg["providers"]["groq"], cache_b)
    _write_catalog_cache(
        cache_b, {key_b: _cache_entry({"groq/model-b": _model_meta("groq/model-b")})}
    )
    ledger = _write_ledger(tmp_path / ".agent_analytics.jsonl", [])

    # ...but the explicit override wins and the snapshot reads cache_b
    result = maintenance.run_dynamic_candidates_sync(
        cfg, snapshot_path=tmp_path / "snap.json", analytics_path=ledger, cache_file=cache_b
    )
    assert result["status"] == "ok"
    parsed = _read_snapshot(tmp_path / "snap.json")
    assert {c["model"] for c in parsed["providers"]["groq"]["candidates"]} == {"groq/model-b"}


def test_denylist_callback_routes_through_denylist_filter(dyn_fake, tmp_path):
    setup = _groq_setup(tmp_path)
    setup["cfg"]["denylist"] = ["groq/model-b"]  # global rule
    result = maintenance.run_dynamic_candidates_sync(
        setup["cfg"], snapshot_path=setup["snapshot"], analytics_path=setup["ledger"]
    )
    assert result["status"] == "ok"
    parsed = _read_snapshot(setup["snapshot"])
    by_model = {c["model"]: c["state"] for c in parsed["providers"]["groq"]["candidates"]}
    assert by_model["groq/model-b"] == "observed"  # denied -> never selectable
    assert by_model["groq/model-a"] == "active"


# ---------------------------------------------------------------------------
# 5. Security: no-network proof and secret canaries
# ---------------------------------------------------------------------------


def test_snapshot_generation_never_touches_network(dyn_fake, tmp_path, monkeypatch):
    from botex.providers import catalog as catalog_mod
    from botex.providers import http_pool
    import urllib.request

    def _net_boom(*args, **kwargs):
        raise AssertionError("network call attempted during offline snapshot generation")

    monkeypatch.setattr(catalog_mod, "sync_provider_catalogs", _net_boom)
    monkeypatch.setattr(urllib.request, "urlopen", _net_boom)
    monkeypatch.setattr(http_pool, "get_sync_client", _net_boom)
    monkeypatch.setattr(http_pool, "get_async_client", _net_boom)
    setup = _groq_setup(tmp_path)

    result = maintenance.run_dynamic_candidates_sync(
        setup["cfg"], snapshot_path=setup["snapshot"], analytics_path=setup["ledger"]
    )
    assert result["status"] == "ok"
    assert setup["snapshot"].exists()


def test_canary_secrets_and_task_text_never_land_in_snapshot(dyn_fake, tmp_path):
    cache = tmp_path / "catalog_cache.json"
    cfg = _dc_cfg(cache)
    cfg["providers"]["groq"]["base_url"] = "https://canary-host-9911.example/internal/secret-path"
    key = _cache_key("groq", cfg["providers"]["groq"], cache)
    _write_catalog_cache(cache, {key: _cache_entry({"groq/model-a": _model_meta("groq/model-a")})})
    canary_rows = [
        _row(
            "groq/model-a",
            "groq",
            status="DONE",
            summary="CANARY_TASK_TEXT fix the auth flow sk-CANARY-SECRET-9911",
            task_id="task-CANARY-9911",
        ),
        {  # row without provider/model keys: must be skipped without leaking
            "task_id": "task-CANARY-9911",
            "summary": "CANARY_TASK_TEXT orphan row sk-CANARY-SECRET-9911",
            "model": None,
            "provider": None,
        },
    ]
    ledger = _write_ledger(tmp_path / ".agent_analytics.jsonl", canary_rows)
    snapshot = tmp_path / "snap" / "dynamic_model_candidates.json"

    result = maintenance.run_dynamic_candidates_sync(
        cfg, snapshot_path=snapshot, analytics_path=ledger
    )
    assert result["status"] == "ok"
    text = snapshot.read_text(encoding="utf-8")
    assert "CANARY" not in text
    assert "canary-host-9911" not in text


# ---------------------------------------------------------------------------
# 6. Curator wiring
# ---------------------------------------------------------------------------


def test_curator_task_invokes_dynamic_candidates_sync(monkeypatch, tmp_path):
    cfg = _dc_cfg(tmp_path / "cache.json")
    monkeypatch.setattr(maintenance, "_load_config", lambda: cfg)
    drift_result = {
        "status": "ok",
        "providers_ok": 2,
        "providers_error": 0,
        "summary": {"drifted": 0, "no_data": 0, "present": 3},
        "sync": {"groq": {"status": "ok"}},
    }
    monkeypatch.setattr(maintenance, "run_catalog_drift_sync", lambda c, force=False: drift_result)
    dyn_calls = []

    def _dyn_spy(c, **kwargs):
        dyn_calls.append(kwargs)
        return {
            "status": "ok",
            "mode": "shadow",
            "providers": 2,
            "counts": {"active": 0, "eligible": 1, "observed": 0, "quarantined": 0},
        }

    monkeypatch.setattr(maintenance, "run_dynamic_candidates_sync", _dyn_spy)

    out = curator_tasks._task_catalog_drift_sync(".")
    assert out["status"] == "ok"
    assert out["dynamic_candidates_status"] == "ok"
    assert out["providers_ok"] == 2
    assert dyn_calls == [{"drift_report": drift_result}]


def test_curator_task_survives_dynamic_candidates_crash(monkeypatch, tmp_path):
    cfg = _dc_cfg(tmp_path / "cache.json")
    monkeypatch.setattr(maintenance, "_load_config", lambda: cfg)
    monkeypatch.setattr(
        maintenance,
        "run_catalog_drift_sync",
        lambda c, force=False: {
            "status": "ok",
            "providers_ok": 1,
            "providers_error": 0,
            "summary": {"drifted": 0, "no_data": 0, "present": 1},
            "sync": {},
        },
    )

    def _dyn_boom(c, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(maintenance, "run_dynamic_candidates_sync", _dyn_boom)

    out = curator_tasks._task_catalog_drift_sync(".")
    # the drift tick itself must not fail because the dynamic snapshot blew up
    assert out["status"] == "ok"
    assert out["dynamic_candidates_status"] == "error"


def test_curator_task_marks_non_dict_dynamic_result_as_error(monkeypatch, tmp_path):
    cfg = _dc_cfg(tmp_path / "cache.json")
    monkeypatch.setattr(maintenance, "_load_config", lambda: cfg)
    monkeypatch.setattr(
        maintenance,
        "run_catalog_drift_sync",
        lambda c, force=False: {
            "status": "ok",
            "providers_ok": 1,
            "providers_error": 0,
            "summary": {},
            "sync": {},
        },
    )
    monkeypatch.setattr(
        maintenance, "run_dynamic_candidates_sync", lambda c, **kwargs: ["not", "a", "dict"]
    )
    out = curator_tasks._task_catalog_drift_sync(".")
    assert out["status"] == "ok"
    assert out["dynamic_candidates_status"] == "error"


# ---------------------------------------------------------------------------
# 7. Wave-1A module absence: never-raise wrapper (parallel-work window)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(HAS_REAL, reason="wave-1A module already landed")
def test_missing_dynamic_candidates_module_returns_error_not_exception(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "botex.providers.dynamic_candidates", raising=False)
    import botex.providers as providers_pkg

    monkeypatch.delattr(providers_pkg, "dynamic_candidates", raising=False)
    cfg = _dc_cfg(tmp_path / "cache.json", mode="shadow")
    result = maintenance.run_dynamic_candidates_sync(
        cfg,
        snapshot_path=tmp_path / "snap.json",
        analytics_path=tmp_path / "ledger.jsonl",
    )
    assert result["status"] == "error"
    assert result.get("error") == "ModuleNotFoundError"
    assert not (tmp_path / "snap.json").exists()


# ---------------------------------------------------------------------------
# 8. Real-module integration matrix (runs once wave 1-A lands)
# ---------------------------------------------------------------------------


def _real_setup(tmp_path: Path, *, dc_section: Any = None, rows: Any = None) -> Dict[str, Any]:
    cache = tmp_path / "catalog_cache.json"
    cfg = _dc_cfg(cache)
    # keep cfg's catalog path repo-relative-looking for the builder's
    # source metadata; the explicit override below carries the real tmp path
    cfg["catalog"]["cache_file"] = ".botex/pricing_cache.json"
    cfg["dynamic_candidates"] = dc_section or _real_policy_dc_section()
    key = _cache_key("groq", cfg["providers"]["groq"], cache)
    _write_catalog_cache(
        cache,
        {
            key: _cache_entry(
                {
                    "groq/model-a": _model_meta("groq/model-a"),
                    "groq/model-b": _model_meta("groq/model-b"),
                }
            )
        },
    )
    if rows is None:
        rows = [
            _row("groq/model-a", "groq", status="DONE"),
            _row("groq/model-a", "groq", status="DONE"),
            _row("groq/model-a", "groq", status="DONE"),
        ]
    ledger = _write_ledger(tmp_path / ".agent_analytics.jsonl", rows)
    return {
        "cfg": cfg,
        "cache": cache,
        "ledger": ledger,
        "snapshot": tmp_path / "snap" / "dynamic_model_candidates.json",
    }


@pytest.mark.skipif(not HAS_REAL, reason="wave-1A dynamic_candidates module not landed yet")
class TestRealDynamicCandidatesModule:
    def test_mode_off_skips_everything(self, tmp_path):
        setup = _real_setup(tmp_path, dc_section=_real_policy_dc_section("off"))
        result = maintenance.run_dynamic_candidates_sync(
            setup["cfg"],
            snapshot_path=setup["snapshot"],
            analytics_path=setup["ledger"],
            cache_file=setup["cache"],
        )
        assert result == {"status": "skipped", "mode": "off", "snapshot_path": ""}
        assert not setup["snapshot"].exists()

    def test_shadow_writes_schema_valid_snapshot(self, tmp_path):
        setup = _real_setup(tmp_path)
        result = maintenance.run_dynamic_candidates_sync(
            setup["cfg"],
            snapshot_path=setup["snapshot"],
            analytics_path=setup["ledger"],
            cache_file=setup["cache"],
        )
        assert result["status"] == "ok"
        assert result["mode"] == "shadow"
        assert set(result["counts"]) == {"active", "eligible", "observed", "quarantined"}
        assert isinstance(result["providers"], int)

        parsed = _read_snapshot(setup["snapshot"])
        assert parsed["schema_version"] == 1
        assert parsed["generator"] == "night-pulse"
        assert isinstance(parsed["generated_at"], (int, float))
        assert isinstance(parsed["providers"], dict)
        for entry in parsed["providers"].values():
            for cand in entry.get("candidates", []):
                assert cand["state"] in _VALID_CANDIDATE_STATES
        assert list(setup["snapshot"].parent.glob("*.tmp")) == []

    def test_malformed_existing_snapshot_recovers(self, tmp_path):
        setup = _real_setup(tmp_path)
        snapshot = setup["snapshot"]
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text("][ broken json", encoding="utf-8")
        result = maintenance.run_dynamic_candidates_sync(
            setup["cfg"],
            snapshot_path=snapshot,
            analytics_path=setup["ledger"],
            cache_file=setup["cache"],
        )
        assert result["status"] == "ok"
        parsed = _read_snapshot(snapshot)
        assert parsed["schema_version"] == 1

    def test_stale_prior_provider_state_preserved(self, tmp_path):
        setup = _real_setup(tmp_path)
        snapshot = setup["snapshot"]
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": time.time() - 7200.0,
                    "providers": {
                        "nvidia": {
                            "catalog_state": "fresh",
                            "candidates": [
                                {
                                    "model": "nvidia/model-b",
                                    "state": "eligible",
                                    "evidence": {
                                        "runs": 1,
                                        "done_runs": 1,
                                        "quality_failures": 0,
                                    },
                                }
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        # nvidia has no cache entry in this run: prior state must survive
        result = maintenance.run_dynamic_candidates_sync(
            setup["cfg"],
            snapshot_path=snapshot,
            analytics_path=setup["ledger"],
            cache_file=setup["cache"],
        )
        assert result["status"] == "ok"
        parsed = _read_snapshot(snapshot)
        assert "nvidia" in parsed["providers"]

    def test_empty_catalog_invents_nothing(self, tmp_path):
        cache = tmp_path / "catalog_cache.json"
        cfg = _dc_cfg(cache)
        cfg["catalog"]["cache_file"] = ".botex/pricing_cache.json"
        cfg["dynamic_candidates"] = _real_policy_dc_section()
        key = _cache_key("groq", cfg["providers"]["groq"], cache)
        _write_catalog_cache(cache, {key: _cache_entry({})})
        ledger = _write_ledger(
            tmp_path / ".agent_analytics.jsonl",
            [_row("groq/model-a", "groq", status="DONE")],
        )
        snapshot = tmp_path / "snap" / "dynamic_model_candidates.json"
        result = maintenance.run_dynamic_candidates_sync(
            cfg,
            snapshot_path=snapshot,
            analytics_path=ledger,
            cache_file=cache,
        )
        assert result["status"] == "ok"
        parsed = _read_snapshot(snapshot)
        groq = parsed["providers"].get("groq", {})
        # no candidate may be invented for a model the catalog does not list
        assert groq.get("candidates", []) == []

    def test_canary_secrets_absent_from_snapshot(self, tmp_path):
        setup = _real_setup(
            tmp_path,
            rows=[
                _row(
                    "groq/model-a",
                    "groq",
                    status="DONE",
                    summary="CANARY_TASK_TEXT sk-CANARY-SECRET-9911",
                    task_id="task-CANARY-9911",
                )
            ],
        )
        result = maintenance.run_dynamic_candidates_sync(
            setup["cfg"],
            snapshot_path=setup["snapshot"],
            analytics_path=setup["ledger"],
            cache_file=setup["cache"],
        )
        assert result["status"] == "ok"
        text = setup["snapshot"].read_text(encoding="utf-8")
        assert "CANARY" not in text

    def test_no_network_during_generation(self, tmp_path, monkeypatch):
        from botex.providers import catalog as catalog_mod

        def _net_boom(*args, **kwargs):
            raise AssertionError("network call attempted during offline snapshot generation")

        monkeypatch.setattr(catalog_mod, "sync_provider_catalogs", _net_boom)
        setup = _real_setup(tmp_path)
        result = maintenance.run_dynamic_candidates_sync(
            setup["cfg"],
            snapshot_path=setup["snapshot"],
            analytics_path=setup["ledger"],
            cache_file=setup["cache"],
        )
        assert result["status"] == "ok"
