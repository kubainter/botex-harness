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


# -- curator dead-link reconciliation -----------------------------------------


def test_dead_link_repairs_suggest_nearest_id():
    from botex.ops.curator.curator_service import _compute_dead_link_repairs

    nodes = [{"id": "botex_prompt_cache_deployed"}, {"id": "arena_recap"}]
    broken = [
        {"source": "handoff_a", "target": "botex_prompt_cache_depl0yed"},
        {"source": "handoff_b", "target": "totally_unknown_note_xyz"},
    ]
    repairs = _compute_dead_link_repairs(broken, nodes)
    assert len(repairs) == 1
    assert repairs[0]["suggested_target"] == "botex_prompt_cache_deployed"
    assert repairs[0]["source_id"] == "handoff_a"


# -- cross-vault qualified ids + swarm-root guard ----------------------------


def test_swarm_dir_repo_root_guard(monkeypatch, tmp_path):
    from botex.memory import memory
    from botex.server import config as srv_config

    (tmp_path / "AGENTS.md").write_text("# repo", "utf-8")
    (tmp_path / "note.md").write_text("x", "utf-8")
    monkeypatch.setattr(srv_config, "swarm_memory_dir", lambda: str(tmp_path))
    assert memory.get_swarm_memory_dir() is None

    vault = tmp_path / "real-vault"
    vault.mkdir()
    (vault / "n1.md").write_text("x", "utf-8")
    monkeypatch.setattr(srv_config, "swarm_memory_dir", lambda: str(vault))
    assert memory.get_swarm_memory_dir() == vault.resolve()


def test_search_dedup_is_per_scope(monkeypatch, tmp_path):
    """Same stem in local and global vaults must both surface (no shadowing)."""
    from botex.memory import memory

    local = tmp_path / "ws" / ".botex" / "memory"
    local.mkdir(parents=True)
    (local / "dup.md").write_text("---\nid: dup\ntitle: local note\n---\nlocal body", "utf-8")
    glob_dir = tmp_path / "glob" / ".botex" / "memory"
    glob_dir.mkdir(parents=True)
    (glob_dir / "dup.md").write_text("---\nid: dup\ntitle: global note\n---\nglobal body", "utf-8")
    monkeypatch.setattr(memory, "get_global_memory_dir", lambda: glob_dir)
    res = memory.search_memory(query="", workspace_dir=str(tmp_path / "ws"), scope="all")
    ids = {(m.get("scope"), m.get("id")) for m in res.get("memories", [])}
    assert ("local", "dup") in ids
    assert ("global", "dup") in ids


# -- repair_dead_links ---------------------------------------------------------


def _note(dir_: Path, stem: str, body: str):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{stem}.md").write_text(f"---\nid: {stem}\ntitle: {stem}\n---\n{body}", "utf-8")


def test_repair_dead_links_suggest_and_apply(monkeypatch, tmp_path):
    from botex.ops import maintenance

    ws = tmp_path / "ws"
    mem = ws / ".botex" / "memory"
    _note(mem, "botex_prompt_cache_deployed", "real note")
    _note(mem, "handoff", "see [[botex_prompt_cache_depl0yed]] for details")
    _note(mem, "other", "see [[totally_missing_xyz]] nowhere")
    monkeypatch.setenv("BOTEX_GLOBAL_MEMORY_DIR", str(tmp_path / "none"))

    res = maintenance.repair_dead_links(str(ws), apply=False)
    assert res["broken"] >= 2
    assert res["suggested"] >= 1
    assert res["applied"] == 0
    # body untouched in suggest mode
    assert "depl0yed" in (mem / "handoff.md").read_text("utf-8")

    res2 = maintenance.repair_dead_links(str(ws), apply=True)
    assert res2["applied"] >= 1
    text = (mem / "handoff.md").read_text("utf-8")
    assert "[[botex_prompt_cache_deployed]]" in text
    # ambiguous link left as-is
    assert "[[totally_missing_xyz]]" in (mem / "other.md").read_text("utf-8")


# -- code review remediation regressions --------------------------------------


def test_rotate_aborts_on_staged_read_failure_preserving_data(monkeypatch):
    """CR #1/#4: if reading staged fails, rotation must abort and restore live."""
    from botex.ops import maintenance
    from botex.providers import analytics

    _, live, _ = _analytics_paths()
    recent_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    analytics.save_analytics(
        [_record("ancient", "2020-01-01T12:00:00"), _record("recent", recent_ts)]
    )

    orig_read = analytics._read_jsonl

    def _boom_read(path, strict=False):
        if str(path).endswith(".rotating"):
            if strict:
                raise OSError("simulated locked/corrupt staged read")
            return []
        return orig_read(path, strict=strict)

    monkeypatch.setattr(analytics, "_read_jsonl", _boom_read)
    res = maintenance.rotate_analytics_ledger()

    assert res["archived"] == 0
    assert res["kept"] == 0
    assert live.exists()
    ids = {r["task_id"] for r in analytics.load_analytics()}
    assert ids == {"ancient", "recent"}


def test_rotate_preserves_rows_without_task_id_or_timestamp_in_tail():
    """CR #5/#6: tail rows lacking task_id/timestamp must not collapse onto (None, None)."""
    from botex.ops import maintenance
    from botex.providers import analytics

    _, live, _ = _analytics_paths()
    analytics.save_analytics([_record("ancient", "2020-01-01T12:00:00")])
    with open(live, "a", encoding="utf-8") as f:
        f.write(json.dumps({"model": "m1", "cost_usd": 0.01}) + "\n")
        f.write(json.dumps({"model": "m2", "cost_usd": 0.02}) + "\n")

    res = maintenance.rotate_analytics_ledger()
    assert res["archived"] == 1
    assert res["kept"] == 2
    kept_models = {r.get("model") for r in analytics._read_jsonl(live)}
    assert kept_models == {"m1", "m2"}


def test_rotate_recovers_orphan_rotating_segment():
    """CR #7: an orphan .rotating file from a prior crash is folded into rotation and load."""
    from botex.ops import maintenance
    from botex.providers import analytics

    _, live, _ = _analytics_paths()
    live.parent.mkdir(parents=True, exist_ok=True)
    recent_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    orphan = live.with_suffix(".jsonl.rotating")
    orphan.write_text(json.dumps(_record("orphan_row", recent_ts)) + "\n", "utf-8")
    live.write_text(json.dumps(_record("live_row", recent_ts)) + "\n", "utf-8")

    # Even before rotation, load_analytics sees the orphan row
    assert {"orphan_row", "live_row"} <= {r.get("task_id") for r in analytics.load_analytics()}

    maintenance.rotate_analytics_ledger()
    assert not orphan.exists()
    assert {"orphan_row", "live_row"} <= {r.get("task_id") for r in analytics.load_analytics()}


def test_repair_dead_links_skips_fenced_code_blocks_and_unreadable_candidate(monkeypatch, tmp_path):
    """CR #8/#9: fenced code blocks stay untouched and one unreadable candidate doesn't abort."""
    from botex.ops import maintenance

    ws = tmp_path / "ws"
    mem = ws / ".botex" / "memory"
    _note(mem, "botex_prompt_cache_deployed", "real target")
    _note(
        mem,
        "handoff",
        "prose [[botex_prompt_cache_depl0yed]] and\n```markdown\n[[botex_prompt_cache_depl0yed]]\n```\ndone",
    )
    monkeypatch.setenv("BOTEX_GLOBAL_MEMORY_DIR", str(tmp_path / "none"))

    res = maintenance.repair_dead_links(str(ws), apply=True)
    assert res["applied"] == 1
    updated = (mem / "handoff.md").read_text("utf-8")
    assert "prose [[botex_prompt_cache_deployed]] and" in updated
    assert "```markdown\n[[botex_prompt_cache_depl0yed]]\n```" in updated


def test_swarm_memory_dir_resolves_relative_path_portably(monkeypatch):
    """Committed botex.config.json uses portable relative path and resolves to PROJECT_ROOT."""
    from botex.server import config as srv_config

    raw_cfg = json.loads((srv_config.PROJECT_ROOT / "botex.config.json").read_text("utf-8"))
    raw_swarm = raw_cfg.get("paths", {}).get("swarm_memory_dir", "")
    assert "\\" not in raw_swarm and not raw_swarm.startswith("C:")

    # Call the real implementation (undoing conftest's autouse stub)
    monkeypatch.undo()
    resolved = Path(srv_config.swarm_memory_dir())
    assert resolved.is_absolute()
    assert resolved == (srv_config.PROJECT_ROOT / ".botex" / "memory").resolve()
