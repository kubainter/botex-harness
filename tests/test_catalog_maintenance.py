# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Tests for plan 062: catalog drift maintenance extraction and curator wiring.
============================================================================
Covers the maintenance-module frequency/outage contract, positive-evidence
health semantics, curator task registration/force propagation, compact output,
and redaction. All provider sync is monkeypatched — no test performs network.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from botex.ops import maintenance
from botex.ops.curator import tasks as curator_tasks


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Ensure no live network calls can be made during catalog maintenance tests."""

    def blocked_connect(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Blocked live network socket call during unit test execution")

    with patch.object(socket.socket, "connect", side_effect=blocked_connect):
        yield


def _controlled_cfg(tmp_path: Path, providers=("groq",)) -> dict:
    return {
        "catalog_sync": {"enabled": True, "providers": list(providers), "timeout_s": 5.0},
        "catalog": {"cache_file": str(tmp_path / "pricing_cache.json")},
        "providers": {
            "groq": {
                "models": {"default": "openai/gpt-oss-120b"},
            }
        },
    }


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Controlled config + tmp report path wired into the maintenance module."""
    report = tmp_path / ".botex" / "catalog_drift.json"
    monkeypatch.setattr(maintenance, "catalog_drift_report_path", lambda: report)
    monkeypatch.setattr(maintenance, "_load_config", lambda: _controlled_cfg(tmp_path))
    return {"report": report, "cfg": _controlled_cfg(tmp_path)}


@pytest.fixture
def sync_spy(monkeypatch):
    """Monkeypatched sync that records invocations and reports per-provider ok."""
    from botex.providers import catalog as catalog_mod

    calls = []

    def spy(registry, provider_names, cache_file=None, timeout=10.0, provider_configs=None):
        calls.append(list(provider_names))
        return {
            n: {"status": "ok", "model_count": 1, "error": None, "fetched_at": time.time()}
            for n in provider_names
        }

    monkeypatch.setattr(catalog_mod, "sync_provider_catalogs", spy)
    return calls


def _write_report(path: Path, sync_entries: dict, age_s: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": time.time() - age_s,
                "sync": sync_entries,
                "drift": [],
                "summary": {"drifted": 0, "no_data": 0, "present": 0},
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. Task registration and ordering
# ---------------------------------------------------------------------------


def test_catalog_drift_task_registration():
    names = [t.name for t in curator_tasks.DEFAULT_TASKS]
    assert "catalog_drift_sync" in names
    assert names.index("catalog_drift_sync") < names.index("provider_warmup")
    task = next(t for t in curator_tasks.DEFAULT_TASKS if t.name == "catalog_drift_sync")
    assert task.interval_s == curator_tasks.TICK_INTERVAL_S
    assert task.accepts_force is True


# ---------------------------------------------------------------------------
# 2. Force propagation
# ---------------------------------------------------------------------------


def test_force_propagates_only_to_opted_in_tasks(tmp_path):
    calls = []

    def fn_plain(ws):
        calls.append(("plain", None))
        return {}

    def fn_force(ws, force=False):
        calls.append(("force", force))
        return {}

    registry = [
        curator_tasks.CuratorTask("plain_task", 0.0, fn_plain),
        curator_tasks.CuratorTask("force_task", 0.0, fn_force, accepts_force=True),
    ]
    curator_tasks.run_due_tasks(str(tmp_path), force=True, registry=registry)
    assert ("plain", None) in calls
    assert ("force", True) in calls


def test_three_argument_task_construction_still_works():
    task = curator_tasks.CuratorTask("x", 1.0, lambda ws: {})
    assert task.accepts_force is False


# ---------------------------------------------------------------------------
# 3-7. Frequency gate: freshness, force, outage retry, corrupt repair
# ---------------------------------------------------------------------------


def test_fresh_successful_report_skips(wired, sync_spy):
    _write_report(wired["report"], {"groq": {"status": "ok", "model_count": 5, "error": None}})
    result = maintenance.run_catalog_drift_sync(wired["cfg"], force=False)
    assert result["status"] == "skipped_fresh"
    assert sync_spy == []


def test_force_bypasses_freshness(wired, sync_spy):
    _write_report(wired["report"], {"groq": {"status": "ok", "model_count": 5, "error": None}})
    result = maintenance.run_catalog_drift_sync(wired["cfg"], force=True)
    assert result["status"] == "ok"
    assert len(sync_spy) == 1


def test_total_outage_report_does_not_suppress_retry(wired, sync_spy):
    _write_report(
        wired["report"],
        {"groq": {"status": "error", "model_count": 0, "error": "boom", "fetched_at": 0.0}},
    )
    result = maintenance.run_catalog_drift_sync(wired["cfg"], force=False)
    assert len(sync_spy) == 1
    assert result["status"] == "ok"


def test_partial_outage_does_not_hot_loop(wired, sync_spy):
    _write_report(
        wired["report"],
        {
            "groq": {"status": "ok", "model_count": 5, "error": None},
            "nvidia": {"status": "error", "model_count": 0, "error": "boom", "fetched_at": 0.0},
        },
    )
    result = maintenance.run_catalog_drift_sync(wired["cfg"], force=False)
    assert result["status"] == "skipped_fresh"
    assert sync_spy == []


def test_missing_report_invokes_sync(wired, sync_spy):
    assert not wired["report"].exists()
    maintenance.run_catalog_drift_sync(wired["cfg"], force=False)
    assert len(sync_spy) == 1


def test_corrupt_report_invokes_sync(wired, sync_spy):
    wired["report"].parent.mkdir(parents=True, exist_ok=True)
    wired["report"].write_text("{not json", encoding="utf-8")
    maintenance.run_catalog_drift_sync(wired["cfg"], force=False)
    assert len(sync_spy) == 1


# ---------------------------------------------------------------------------
# 8. Single report-path source
# ---------------------------------------------------------------------------


def test_report_path_single_source(monkeypatch):
    from botex.server import server_cloud

    monkeypatch.setattr(server_cloud, "_CATALOG_DRIFT_REPORT_PATH", None)
    assert server_cloud._catalog_drift_report_file() == maintenance.catalog_drift_report_path()
    assert ".botex" in maintenance.catalog_drift_report_path().parts


# ---------------------------------------------------------------------------
# 10. Compact task output + real sync via curator --only --force
# ---------------------------------------------------------------------------


def test_curator_only_force_performs_real_sync(wired, sync_spy, tmp_path):
    registry = [t for t in curator_tasks.DEFAULT_TASKS if t.name == "catalog_drift_sync"]
    report = curator_tasks.run_due_tasks(
        str(tmp_path), only=["catalog_drift_sync"], force=True, registry=registry
    )
    entry = report["catalog_drift_sync"]
    assert entry["status"] == "ran"
    assert len(sync_spy) == 1
    assert wired["report"].is_file()
    result = entry["result"]
    assert result["providers_ok"] >= 1
    blob = json.dumps(result)
    # Compact contract: counts/status only — no paths, URLs, or raw error text.
    assert "report_path" not in blob
    assert "http" not in blob
    assert set(result.keys()) <= {
        "status",
        "drifted",
        "no_data",
        "present",
        "providers_ok",
        "providers_error",
    }


def test_dry_run_force_performs_no_io(wired, tmp_path):
    report = curator_tasks.run_due_tasks(
        str(tmp_path), only=["catalog_drift_sync"], force=True, dry_run=True
    )
    assert report["catalog_drift_sync"]["status"] == "due"
    assert not wired["report"].exists()
    assert curator_tasks.load_task_state(str(tmp_path)) == {}


# ---------------------------------------------------------------------------
# 11. Redaction: secret-shaped sync exception never reaches report or stdout
# ---------------------------------------------------------------------------


def test_sync_exception_redacted_and_counted(wired, monkeypatch):
    from botex.providers import catalog as catalog_mod

    secret = "gsk_abc123secretxyz"

    def boom(registry, provider_names, cache_file=None, timeout=10.0, provider_configs=None):
        raise RuntimeError(f"GET https://api.example.test/v1/models?api_key={secret} failed")

    monkeypatch.setattr(catalog_mod, "sync_provider_catalogs", boom)
    result = maintenance.run_catalog_drift_sync(wired["cfg"], force=True)
    assert wired["report"].is_file()
    on_disk = wired["report"].read_text(encoding="utf-8")
    blob = json.dumps(result) + on_disk
    assert secret not in blob
    # Countable evidence: every attempted provider surfaces as an error entry.
    report = json.loads(on_disk)
    assert report["sync"]["groq"]["status"] == "error"
    assert report["sync"]["groq"]["fetched_at"] == 0.0
    assert "sync_exception" in report


# ---------------------------------------------------------------------------
# 12. Failure semantics: masked error, no last_run advancement
# ---------------------------------------------------------------------------


def test_task_failure_masked_and_not_advanced(tmp_path):
    secret = "gsk_abc123secretxyz"

    def boom(ws):
        raise RuntimeError(f"api_key={secret} exploded")

    registry = [curator_tasks.CuratorTask("exploding", 0.0, boom)]
    report = curator_tasks.run_due_tasks(str(tmp_path), registry=registry)
    entry = report["exploding"]
    assert entry["status"] == "failed"
    assert secret not in entry["error"]
    assert len(entry["error"]) <= 220
    assert "exploding" not in curator_tasks.load_task_state(str(tmp_path))


# ---------------------------------------------------------------------------
# 13. No-evidence is never green (health summary)
# ---------------------------------------------------------------------------


def test_health_no_evidence_flag_for_all_error_report(tmp_path, monkeypatch):
    from botex.server import server_cloud

    report = tmp_path / "catalog_drift.json"
    _write_report(
        report,
        {"groq": {"status": "error", "model_count": 0, "error": "boom", "fetched_at": 0.0}},
    )
    monkeypatch.setattr(server_cloud, "_CATALOG_DRIFT_REPORT_PATH", report)
    health = server_cloud._catalog_drift_health()
    assert health["status"] == "ok"
    assert health["providers_ok"] == 0
    assert health["no_evidence"] is True
    assert health["sync_errors"] == 1


def test_health_positive_evidence_for_healthy_report(tmp_path, monkeypatch):
    from botex.server import server_cloud

    report = tmp_path / "catalog_drift.json"
    _write_report(report, {"groq": {"status": "ok", "model_count": 5, "error": None}})
    monkeypatch.setattr(server_cloud, "_CATALOG_DRIFT_REPORT_PATH", report)
    health = server_cloud._catalog_drift_health()
    assert health["providers_ok"] == 1
    assert health["no_evidence"] is False


# ---------------------------------------------------------------------------
# 14. Multi-tick curator integration: 12h network bounding across 25min ticks
# ---------------------------------------------------------------------------


def test_multi_tick_network_bounding_and_repair(wired, sync_spy, tmp_path):
    """Tick 1 syncs; tick 2 (25min later) hits the freshness gate with zero
    additional network calls; a corrupted report on tick 3 repairs despite the
    tick cadence — all through run_due_tasks with a warm tasks_state.json."""
    t0 = time.time()
    clock = lambda: t0  # noqa: E731
    registry = [t for t in curator_tasks.DEFAULT_TASKS if t.name == "catalog_drift_sync"]

    r1 = curator_tasks.run_due_tasks(
        str(tmp_path), only=["catalog_drift_sync"], registry=registry, clock=clock
    )
    assert r1["catalog_drift_sync"]["status"] == "ran"
    assert r1["catalog_drift_sync"]["result"]["status"] == "ok"
    assert len(sync_spy) == 1
    assert "catalog_drift_sync" in curator_tasks.load_task_state(str(tmp_path))

    clock = lambda: t0 + curator_tasks.TICK_INTERVAL_S  # noqa: E731
    r2 = curator_tasks.run_due_tasks(
        str(tmp_path), only=["catalog_drift_sync"], registry=registry, clock=clock
    )
    assert r2["catalog_drift_sync"]["status"] == "ran"
    assert r2["catalog_drift_sync"]["result"]["status"] == "skipped_fresh"
    assert len(sync_spy) == 1  # no second network sync

    wired["report"].write_text("{corrupted", encoding="utf-8")
    clock = lambda: t0 + 2 * curator_tasks.TICK_INTERVAL_S  # noqa: E731
    r3 = curator_tasks.run_due_tasks(
        str(tmp_path), only=["catalog_drift_sync"], registry=registry, clock=clock
    )
    assert r3["catalog_drift_sync"]["status"] == "ran"
    assert r3["catalog_drift_sync"]["result"]["status"] == "ok"
    assert len(sync_spy) == 2  # repair sync despite fresh cadence state


def test_multi_tick_total_outage_retries_next_tick(wired, monkeypatch, tmp_path):
    """A total provider outage writes countable error evidence; the next tick
    must retry the network rather than suppressing for 12h."""
    from botex.providers import catalog as catalog_mod

    def failing(registry, provider_names, cache_file=None, timeout=10.0, provider_configs=None):
        return {
            n: {"status": "error", "model_count": 0, "error": "boom", "fetched_at": 0.0}
            for n in provider_names
        }

    calls = []

    def counting(registry, provider_names, cache_file=None, timeout=10.0, provider_configs=None):
        calls.append(list(provider_names))
        return failing(registry, provider_names, cache_file=cache_file, timeout=timeout)

    monkeypatch.setattr(catalog_mod, "sync_provider_catalogs", counting)
    t0 = time.time()
    registry = [t for t in curator_tasks.DEFAULT_TASKS if t.name == "catalog_drift_sync"]

    curator_tasks.run_due_tasks(
        str(tmp_path), only=["catalog_drift_sync"], registry=registry, clock=lambda: t0
    )
    assert len(calls) == 1
    r2 = curator_tasks.run_due_tasks(
        str(tmp_path),
        only=["catalog_drift_sync"],
        registry=registry,
        clock=lambda: t0 + curator_tasks.TICK_INTERVAL_S,
    )
    assert r2["catalog_drift_sync"]["result"]["status"] == "ok"
    assert r2["catalog_drift_sync"]["result"]["providers_error"] >= 1
    # The outage must retry the network on the next tick, not just report ok.
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 15. End-to-end redaction: URL + key-secret never reach report/stdout/health
# ---------------------------------------------------------------------------


def test_end_to_end_redaction_report_and_health(wired, monkeypatch, tmp_path, capsys):
    from botex.providers import catalog as catalog_mod
    from botex.server import server_cloud

    secret = "mycustomsecret123456"

    def boom(registry, provider_names, cache_file=None, timeout=10.0, provider_configs=None):
        raise RuntimeError(
            f"GET https://generativelanguage.googleapis.com/v1beta/models?key={secret} "
            f"via https://user:{secret}@proxy.example.test failed"
        )

    monkeypatch.setattr(catalog_mod, "sync_provider_catalogs", boom)
    registry = [t for t in curator_tasks.DEFAULT_TASKS if t.name == "catalog_drift_sync"]
    report_out = curator_tasks.run_due_tasks(
        str(tmp_path), only=["catalog_drift_sync"], force=True, registry=registry
    )
    assert report_out["catalog_drift_sync"]["status"] == "ran"

    on_disk = wired["report"].read_text(encoding="utf-8")
    stdout_blob = capsys.readouterr().out
    monkeypatch.setattr(server_cloud, "_CATALOG_DRIFT_REPORT_PATH", wired["report"])
    health_blob = json.dumps(server_cloud._catalog_drift_health())
    blob = on_disk + stdout_blob + health_blob + json.dumps(report_out)
    assert secret not in blob
    assert "generativelanguage.googleapis.com" not in blob
    assert "proxy.example.test" not in blob

    parsed = json.loads(on_disk)
    assert parsed["sync"]["groq"]["status"] == "error"
    health = server_cloud._catalog_drift_health()
    assert health["providers_ok"] == 0
    assert health["no_evidence"] is True
    assert health["sync_errors"] == len(parsed["sync"])


def test_per_provider_error_urls_scrubbed(wired, monkeypatch):
    """Per-provider error strings arrive from catalog.py pre-masked but with
    raw URLs intact — the report boundary must scrub them."""
    from botex.providers import catalog as catalog_mod

    secret = "mycustomsecret123456"

    def partial(registry, provider_names, cache_file=None, timeout=10.0, provider_configs=None):
        return {
            n: {
                "status": "error",
                "model_count": 0,
                "error": f"HTTP 401 for url 'https://api.example.test/v1/models?key={secret}'",
                "fetched_at": 0.0,
            }
            for n in provider_names
        }

    monkeypatch.setattr(catalog_mod, "sync_provider_catalogs", partial)
    maintenance.run_catalog_drift_sync(wired["cfg"], force=True)
    on_disk = wired["report"].read_text(encoding="utf-8")
    assert secret not in on_disk
    assert "api.example.test" not in on_disk


# ---------------------------------------------------------------------------
# 16. Frequency-gate config hardening
# ---------------------------------------------------------------------------


def test_null_interval_does_not_disable_gate(wired, sync_spy):
    wired["cfg"]["catalog_sync"]["interval_s"] = None
    _write_report(wired["report"], {"groq": {"status": "ok", "model_count": 5, "error": None}})
    result = maintenance.run_catalog_drift_sync(wired["cfg"], force=False)
    assert result["status"] == "skipped_fresh"
    assert sync_spy == []


def test_future_timestamp_does_not_suppress_sync(wired, sync_spy):
    _write_report(
        wired["report"], {"groq": {"status": "ok", "model_count": 5, "error": None}}, age_s=-3600.0
    )
    result = maintenance.run_catalog_drift_sync(wired["cfg"], force=False)
    assert result["status"] == "ok"
    assert len(sync_spy) == 1


def test_whitespace_provider_list_means_no_providers(wired, sync_spy):
    """Explicit empty/whitespace provider config is distinguishable no-data,
    never silently reinterpreted as the default multi-provider set."""
    wired["cfg"]["catalog_sync"]["providers"] = ["   "]
    result = maintenance.run_catalog_drift_sync(wired["cfg"], force=True)
    assert result["status"] == "no_providers"
    assert sync_spy == []


def test_absent_provider_key_uses_defaults(wired, sync_spy):
    del wired["cfg"]["catalog_sync"]["providers"]
    result = maintenance.run_catalog_drift_sync(wired["cfg"], force=True)
    assert result["status"] == "ok"
    assert sync_spy and len(sync_spy[0]) > 1  # default multi-provider list


def test_malformed_catalog_cfg_never_raises(wired, sync_spy):
    wired["cfg"]["catalog"] = "not-a-dict"
    result = maintenance.run_catalog_drift_sync(wired["cfg"], force=True)
    assert result["status"] == "ok"
