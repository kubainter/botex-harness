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
