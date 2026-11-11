# SPDX-License-Identifier: Apache-2.0
"""L1 idle-layer tests: cooldown persistence and circuit-breaker prober."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from botex.providers.rate_limiter import ProviderRateLimiter
from botex.providers.retry import ProviderCircuitBreaker


def _limiter(tmp_path: Path, **kw) -> ProviderRateLimiter:
    return ProviderRateLimiter(
        {"openrouter": {"rpm": 10, "cooldown_s": 30}},
        state_path=tmp_path / "rate_limits_state.json",
        **kw,
    )


def test_report_limited_persists_cooldown(tmp_path):
    lim = _limiter(tmp_path)
    lim.report_limited("openrouter", retry_after_s=20.0)
    state = json.loads((tmp_path / "rate_limits_state.json").read_text("utf-8"))
    until = state["cooldowns"]["openrouter"]
    assert time.time() < until <= time.time() + 20.5


def test_cooldown_restored_on_restart(tmp_path):
    lim = _limiter(tmp_path)
    lim.report_limited("openrouter", retry_after_s=30.0)
    # "restart": fresh limiter reading the same state file
    lim2 = _limiter(tmp_path)
    remaining = lim2.cooldown_remaining("openrouter")
    assert 25.0 < remaining <= 30.0


def test_expired_cooldown_not_restored(tmp_path):
    state = tmp_path / "rate_limits_state.json"
    state.write_text(json.dumps({"cooldowns": {"openrouter": time.time() - 5.0}}), "utf-8")
    lim = _limiter(tmp_path)
    assert lim.cooldown_remaining("openrouter") == 0.0


def test_restore_skips_unknown_provider(tmp_path):
    state = tmp_path / "rate_limits_state.json"
    state.write_text(json.dumps({"cooldowns": {"ghost": time.time() + 100.0}}), "utf-8")
    lim = _limiter(tmp_path)
    assert lim.cooldown_remaining("ghost") == 0.0


def test_no_state_path_means_no_persistence(tmp_path, monkeypatch):
    lim = ProviderRateLimiter({"openrouter": {"rpm": 10, "cooldown_s": 30}})
    lim.report_limited("openrouter", retry_after_s=10.0)
    assert not (tmp_path / "rate_limits_state.json").exists()


# -- breaker prober ----------------------------------------------------------


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.mark.anyio
async def test_probe_tick_recovers_half_open(monkeypatch):
    from botex import server_cloud

    breaker = ProviderCircuitBreaker(failure_threshold=1, recovery_timeout_s=0.001)
    breaker.record_failure("openrouter")
    breaker._tripped_at["openrouter"] = time.time() - 10.0  # past recovery window

    class _Client:
        async def get(self, url, timeout):
            return _Resp(200)

    monkeypatch.setattr("botex.providers.http_pool.get_async_client", lambda: _Client())
    await server_cloud._breaker_probe_tick(breaker)
    assert breaker.get_state("openrouter") == ProviderCircuitBreaker.CLOSED


@pytest.mark.anyio
async def test_probe_tick_retrips_on_failure(monkeypatch):
    from botex import server_cloud

    breaker = ProviderCircuitBreaker(failure_threshold=1, recovery_timeout_s=0.001)
    breaker.record_failure("openrouter")
    breaker._tripped_at["openrouter"] = time.time() - 10.0

    class _Client:
        async def get(self, url, timeout):
            return _Resp(503)

    monkeypatch.setattr("botex.providers.http_pool.get_async_client", lambda: _Client())
    await server_cloud._breaker_probe_tick(breaker)
    assert breaker.get_state("openrouter") == ProviderCircuitBreaker.OPEN


@pytest.mark.anyio
async def test_probe_tick_releases_slot_on_transport_error(monkeypatch):
    from botex import server_cloud

    breaker = ProviderCircuitBreaker(failure_threshold=1, recovery_timeout_s=0.001)
    breaker.record_failure("openrouter")
    breaker._tripped_at["openrouter"] = time.time() - 10.0

    class _Client:
        async def get(self, url, timeout):
            raise OSError("connect failed")

    monkeypatch.setattr("botex.providers.http_pool.get_async_client", lambda: _Client())
    await server_cloud._breaker_probe_tick(breaker)
    # HALF_OPEN slot freed, breaker stays HALF_OPEN (not counted as failure)
    assert breaker.get_state("openrouter") == ProviderCircuitBreaker.HALF_OPEN
    assert not breaker._probe_in_flight.get("openrouter")


# -- CLI dispatch ------------------------------------------------------------


def test_cli_maintenance_repair(tmp_path, monkeypatch, capsys):
    import server

    monkeypatch.setattr(
        "botex.ops.maintenance.repair_frontmatter_ids",
        lambda ws: {"repaired": 2, "skipped": 0},
    )
    server._cli_maintenance(["repair", "-w", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert out["repair_frontmatter_ids"]["repaired"] == 2


# -- analytics ledger: append-only jsonl + rotation --------------------------


def _analytics_paths():
    from botex.server.config import analytics_file

    base = analytics_file()
    return base, base.with_suffix(".jsonl"), base.parent / "analytics-archive"


def _record(task_id: str, ts: str):
    return {
        "task_id": task_id,
        "timestamp": ts,
        "model": "openrouter-free/x:free",
        "status": "DONE",
        "cost_usd": 0.0,
    }


def test_log_run_migrates_legacy_json_to_jsonl():
    from botex.providers import analytics

    base, jsonl, _ = _analytics_paths()
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(json.dumps([_record("old-1", "2026-01-01T00:00:00")]), "utf-8")

    entry = analytics.log_run(
        task_id="new-1",
        model="openrouter-free/x:free",
        duration_s=1.0,
        steps=1,
        prompt_tokens=0,
        completion_tokens=0,
        cached_tokens=0,
        files_touched=[],
        lines_added=0,
        lines_removed=0,
    )
    assert entry["task_id"] == "new-1"
    assert not base.exists()  # legacy folded away
    ids = {r["task_id"] for r in analytics.load_analytics()}
    assert ids == {"old-1", "new-1"}


def test_rotate_archives_old_rows_and_keeps_recent():
    from botex.ops import maintenance
    from botex.providers import analytics

    base, jsonl, archive = _analytics_paths()
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(json.dumps([_record("ancient", "2020-01-15T12:00:00")]), "utf-8")
    recent_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    jsonl.write_text(json.dumps(_record("recent", recent_ts)) + "\n", "utf-8")

    res = maintenance.rotate_analytics_ledger()
    assert res["migrated"] == 1
    assert res["archived"] == 1
    assert res["kept"] == 1

    gz = archive / "2020-01.jsonl.gz"
    assert gz.exists()
    live_ids = {r["task_id"] for r in analytics._read_jsonl(jsonl)}
    assert live_ids == {"recent"}
    # merged view still contains archived row (stats horizon preserved)
    all_ids = {r["task_id"] for r in analytics.load_analytics()}
    assert all_ids == {"ancient", "recent"}


def test_save_analytics_replaces_live_segment():
    from botex.providers import analytics

    analytics.save_analytics([_record("a", "2026-01-01T00:00:00")])
    analytics.save_analytics([_record("b", "2026-01-02T00:00:00")])
    ids = {r["task_id"] for r in analytics.load_analytics()}
    assert ids == {"b"}
