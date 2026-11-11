import json

import pytest

from botex.ops.curator import tasks as ct


def _registry(calls, fail=False):
    def ok(ws):
        calls.append("ok")
        return {"n": 1}

    def boom(ws):
        calls.append("boom")
        raise RuntimeError("nope")

    return [ct.CuratorTask("a", 100.0, ok), ct.CuratorTask("b", 1000.0, boom if fail else ok)]


def test_first_run_executes_all_then_skips_until_due(tmp_path):
    calls = []
    t = [1000.0]
    reg = _registry(calls)
    r1 = ct.run_due_tasks(str(tmp_path), registry=reg, clock=lambda: t[0])
    assert {k: v["status"] for k, v in r1.items()} == {"a": "ran", "b": "ran"}

    t[0] += 50
    r2 = ct.run_due_tasks(str(tmp_path), registry=reg, clock=lambda: t[0])
    assert r2["a"]["status"] == "skipped" and r2["a"]["next_in_s"] == 50.0

    t[0] += 60
    r3 = ct.run_due_tasks(str(tmp_path), registry=reg, clock=lambda: t[0])
    assert r3["a"]["status"] == "ran" and r3["b"]["status"] == "skipped"
    assert calls.count("ok") == 3


def test_dry_run_runs_nothing_and_writes_no_state(tmp_path):
    calls = []
    r = ct.run_due_tasks(str(tmp_path), registry=_registry(calls), dry_run=True)
    assert all(v["status"] == "due" for v in r.values())
    assert calls == []
    assert ct.load_task_state(str(tmp_path)) == {}


def test_failure_is_isolated_and_retried(tmp_path):
    calls = []
    t = [10.0]
    reg = _registry(calls, fail=True)
    r = ct.run_due_tasks(str(tmp_path), registry=reg, clock=lambda: t[0])
    assert r["a"]["status"] == "ran" and r["b"]["status"] == "failed"
    assert "b" not in ct.load_task_state(str(tmp_path))

    t[0] += 1
    r2 = ct.run_due_tasks(str(tmp_path), registry=reg, clock=lambda: t[0])
    assert r2["b"]["status"] == "failed"


def test_force_and_only(tmp_path):
    calls = []
    reg = _registry(calls)
    ct.run_due_tasks(str(tmp_path), registry=reg, clock=lambda: 5.0)
    r = ct.run_due_tasks(str(tmp_path), registry=reg, only=["a"], force=True, clock=lambda: 6.0)
    assert list(r) == ["a"] and r["a"]["status"] == "ran"


def test_unknown_task_rejected(tmp_path):
    with pytest.raises(ValueError):
        ct.run_due_tasks(str(tmp_path), registry=_registry([]), only=["zzz"])


def test_corrupt_state_is_treated_as_empty(tmp_path):
    p = tmp_path / ".botex" / "curator"
    p.mkdir(parents=True)
    (p / "tasks_state.json").write_text("{not json", encoding="utf-8")
    assert ct.load_task_state(str(tmp_path)) == {}


def test_scratchpad_prune_removes_only_old_logs(tmp_path):
    import os
    import time

    sp = tmp_path / ".botex" / "scratchpad"
    sp.mkdir(parents=True)
    old, new, other = sp / "old.log", sp / "new.log", sp / "keep.txt"
    for f in (old, new, other):
        f.write_text("x")
    past = time.time() - ct.SCRATCHPAD_LOG_MAX_AGE_S - 60
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    assert ct._task_scratchpad_prune(str(tmp_path)) == {"removed": 1}
    assert not old.exists() and new.exists() and other.exists()


def test_cli_list_and_dry_run(tmp_path, capsys):
    assert ct.main(["--list"]) == 0
    assert "index_sync" in capsys.readouterr().out
    assert ct.main(["--workspace", str(tmp_path), "--dry-run", "--only", "scratchpad_prune"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"scratchpad_prune": {"status": "due"}}
