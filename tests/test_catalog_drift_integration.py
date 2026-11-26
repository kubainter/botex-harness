# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Integration tests for Plan 053 Night-Pulse catalog sync & drift detection.
==========================================================================
Covers the wiring half: night-pulse catalog sync step, drift report
persistence, stderr DRIFT lines, and the /api/health catalog_drift section.
All network-touching functions are monkeypatched — no test performs network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from botex.providers.catalog import ModelCatalog, ModelMetadata


def _controlled_config(tmp_path: Path) -> dict:
    return {
        "catalog_sync": {"enabled": True, "providers": ["groq"], "timeout_s": 5.0},
        "catalog": {"cache_file": str(tmp_path / "pricing_cache.json")},
        "providers": {
            "groq": {
                "base_url": "https://api.groq.com/openai/v1",
                "models": {"default": "openai/gpt-oss-120b", "fast": "openai/gpt-oss-20b"},
                "model_fallbacks": {"default": ["qwen/qwen3.8-27b"]},
            }
        },
    }


def _fake_sync_seed_unrelated(registry, provider_names, cache_file=None, timeout=10.0):
    """Mimics sync_provider_catalogs: writes a live catalog containing none of the configured ids."""
    results = {}
    for name in provider_names:
        cat = ModelCatalog(provider_name=name, provider_cfg={}, cache_file=cache_file)
        cat._write_cache_entry({"unrelated/model-x": ModelMetadata(id="unrelated/model-x")})
        results[name] = {
            "status": "ok",
            "model_count": 1,
            "error": None,
            "fetched_at": time.time(),
        }
    return results


@pytest.fixture()
def _wired(monkeypatch, tmp_path: Path):
    """Wire night-pulse catalog sync with monkeypatched sync + controlled config."""
    import server as botex_server
    from botex.core import swarm_store
    from botex.providers import catalog as catalog_mod

    monkeypatch.setenv("BOTEX_SWARM_STORE_DB", str(tmp_path / "swarm_store.db"))
    monkeypatch.setenv("BOTEX_SWARM_HOME", str(tmp_path / "swarm_home"))
    monkeypatch.setenv("BOTEX_DISABLE_DOTENV", "1")
    monkeypatch.delenv("BOTEX_CLOUD_URL", raising=False)
    (tmp_path / "swarm_home").mkdir(parents=True, exist_ok=True)

    report_path = tmp_path / ".botex" / "catalog_drift.json"
    monkeypatch.setattr(botex_server, "_CATALOG_DRIFT_REPORT_PATH", report_path)
    monkeypatch.setattr(botex_server, "load_config", lambda *a, **k: _controlled_config(tmp_path))
    monkeypatch.setattr(catalog_mod, "sync_provider_catalogs", _fake_sync_seed_unrelated)

    yield {
        "report_path": report_path,
        "cache_file": tmp_path / "pricing_cache.json",
        "server": botex_server,
        "catalog_mod": catalog_mod,
    }
    swarm_store.close_swarm_store_dbs()


def test_night_pulse_writes_drift_report_for_absent_configured_ids(_wired, tmp_path: Path):
    import server as botex_server

    res = botex_server._execute_night_pulse_cycle()

    assert "catalog_sync" in res
    assert "catalog_drift" in res
    assert res["catalog_drift"]["status"] == "ok"
    assert res["catalog_sync"]["groq"]["status"] == "ok"

    report_path = _wired["report_path"]
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["generated_at"] > 0
    assert "generated_at_iso" in report
    assert report["sync"]["groq"]["status"] == "ok"

    drifted = {
        (e["provider"], e["model_id"], e["slot"])
        for e in report["drift"]
        if e["present_in_catalog"] is False
    }
    assert ("groq", "openai/gpt-oss-120b", "default") in drifted
    assert ("groq", "openai/gpt-oss-20b", "fast") in drifted
    assert ("groq", "qwen/qwen3.8-27b", "fallback:default") in drifted
    assert report["summary"]["drifted"] == len(drifted)


def test_night_pulse_drift_emits_stderr_lines(_wired, capsys):
    import server as botex_server

    botex_server._execute_night_pulse_cycle()
    err_out = capsys.readouterr().err
    assert (
        "[BoteX] DRIFT: configured model 'openai/gpt-oss-120b' "
        "(provider 'groq', slot 'default') absent from live catalog" in err_out
    )


def test_night_pulse_sync_exception_does_not_escape(_wired, monkeypatch):
    import server as botex_server

    def _boom(registry, provider_names, cache_file=None, timeout=10.0):
        raise RuntimeError("simulated sync explosion")

    monkeypatch.setattr(_wired["catalog_mod"], "sync_provider_catalogs", _boom)

    res = botex_server._execute_night_pulse_cycle()
    assert res["catalog_drift"]["status"] == "ok"
    assert res["catalog_sync"] == {}
    # Report is still written even when the sync blew up.
    assert _wired["report_path"].is_file()


def test_night_pulse_drift_disabled_by_config(_wired, monkeypatch, tmp_path: Path):
    import server as botex_server

    cfg = _controlled_config(tmp_path)
    cfg["catalog_sync"]["enabled"] = False
    monkeypatch.setattr(botex_server, "load_config", lambda *a, **k: cfg)

    res = botex_server._execute_night_pulse_cycle()
    assert res["catalog_drift"]["status"] == "disabled"
    assert res["catalog_sync"] == {}
    assert not _wired["report_path"].exists()


def test_night_pulse_sync_frequency_gate_skips_fresh_report(_wired):
    import server as botex_server

    first = botex_server._execute_night_pulse_cycle()
    assert first["catalog_drift"]["status"] == "ok"

    second = botex_server._execute_night_pulse_cycle()
    assert second["catalog_drift"]["status"] == "skipped_fresh"
    # The fresh report from the first cycle is still on disk and unchanged.
    assert _wired["report_path"].is_file()


def test_night_pulse_missing_catalog_sync_section_uses_defaults(
    _wired, monkeypatch, tmp_path: Path
):
    import server as botex_server

    cfg = _controlled_config(tmp_path)
    del cfg["catalog_sync"]
    monkeypatch.setattr(botex_server, "load_config", lambda *a, **k: cfg)

    res = botex_server._execute_night_pulse_cycle()
    assert res["catalog_drift"]["status"] == "ok"
    assert _wired["report_path"].is_file()


# ---------------------------------------------------------------------------
# /api/health catalog_drift section
# ---------------------------------------------------------------------------


@pytest.fixture()
def cloud_client(monkeypatch):
    from fastapi.testclient import TestClient

    from botex.server.server_cloud import app

    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        "botex.ops.migrate_projects.ensure_projects_migrated",
        lambda *args, **kwargs: {"ok": True, "roots": [], "total_updated": 0},
    )
    with TestClient(app) as test_client:
        yield test_client


def _write_report(path: Path, drift_entries: list, age_s: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": time.time() - age_s,
                "sync": {"groq": {"status": "ok", "model_count": 5, "error": None}},
                "drift": drift_entries,
            }
        ),
        encoding="utf-8",
    )


def test_health_includes_catalog_drift_ok(cloud_client, monkeypatch, tmp_path: Path):
    from botex.server import server_cloud

    report = tmp_path / "catalog_drift.json"
    entries = [
        {
            "provider": "groq",
            "model_id": f"model-{i}",
            "slot": "default",
            "present_in_catalog": False,
        }
        for i in range(12)
    ]
    entries.append(
        {"provider": "groq", "model_id": "present-1", "slot": "fast", "present_in_catalog": True}
    )
    _write_report(report, entries)
    monkeypatch.setattr(server_cloud, "_CATALOG_DRIFT_REPORT_PATH", report)

    resp = cloud_client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    drift = data["catalog_drift"]
    assert drift["status"] == "ok"
    assert drift["drifted"] == 12
    assert drift["per_provider"] == {"groq": 12}
    # Bounded: at most 10 drifted ids despite 12 drifted entries.
    assert len(drift["drifted_ids"]) == 10
    assert drift["drifted_ids"][0] == "groq:model-0"


def test_health_catalog_drift_unknown_when_absent(cloud_client, monkeypatch, tmp_path: Path):
    from botex.server import server_cloud

    monkeypatch.setattr(
        server_cloud, "_CATALOG_DRIFT_REPORT_PATH", tmp_path / "does-not-exist.json"
    )
    resp = cloud_client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["catalog_drift"]["status"] == "unknown"


def test_health_catalog_drift_unknown_when_stale(cloud_client, monkeypatch, tmp_path: Path):
    from botex.server import server_cloud

    report = tmp_path / "catalog_drift.json"
    _write_report(report, [], age_s=49 * 3600)
    monkeypatch.setattr(server_cloud, "_CATALOG_DRIFT_REPORT_PATH", report)
    resp = cloud_client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["catalog_drift"]["status"] == "unknown"


def test_health_catalog_drift_never_fails_on_corrupt_report(
    cloud_client, monkeypatch, tmp_path: Path
):
    from botex.server import server_cloud

    report = tmp_path / "catalog_drift.json"
    report.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(server_cloud, "_CATALOG_DRIFT_REPORT_PATH", report)
    resp = cloud_client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["catalog_drift"]["status"] in ("unknown", "error")
