# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for Plan 037: Multi-Host OS driver, process job guard, and epoch git worktrees."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

from botex.edge.host_os_driver import (
    HostOSDriver,
    SingleInstanceLock,
    SingleInstanceLockError,
    WindowsJobGuard,
    _validate_git_ref,
    check_runtime_blocklist,
    fence_local_epoch,
    kill_posix_process_group,
    prepare_epoch_worktree,
)


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return res.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@botex.local")
    _git(repo, "config", "user.name", "BoteX Test")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial")


def test_prepare_epoch_worktree_isolates_branch_and_uses_onexc(tmp_path: Path):
    origin_repo = tmp_path / "origin_repo"
    _init_repo(origin_repo)
    wt_root = tmp_path / "worktrees"

    wt_path = prepare_epoch_worktree(
        origin_repo,
        wt_root,
        order_id="ord-1",
        epoch=1,
        base_ref="HEAD",
    )
    assert wt_path.is_dir()
    assert _git(wt_path, "rev-parse", "--abbrev-ref", "HEAD") == "swarm/wip/ord-1/e1"
    assert _git(wt_path, "config", "--get", "core.autocrlf") == "false"
    assert _git(wt_path, "config", "--get", "core.eol") == "lf"

    # Place a read-only file inside the worktree and recreate the same epoch worktree
    # to exercise shutil.rmtree(..., onexc=...) with zero DeprecationWarning.
    ro_file = wt_path / "readonly.txt"
    ro_file.write_text("locked", encoding="utf-8")
    os.chmod(ro_file, stat.S_IREAD)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        wt_path2 = prepare_epoch_worktree(
            origin_repo,
            wt_root,
            order_id="ord-1",
            epoch=1,
            base_ref="HEAD",
        )
    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not dep_warnings, (
        f"Unexpected DeprecationWarning: {[str(w.message) for w in dep_warnings]}"
    )
    assert wt_path2 == wt_path
    assert not ro_file.exists()


def test_prepare_epoch_worktree_fetches_explicit_swarm_remote_only(tmp_path: Path):
    bare_swarm = tmp_path / "swarm.git"
    subprocess.run(["git", "init", "--bare", str(bare_swarm)], check=True, capture_output=True)

    seed_repo = tmp_path / "seed"
    _init_repo(seed_repo)
    _git(seed_repo, "remote", "add", "swarm", str(bare_swarm))
    _git(seed_repo, "push", "swarm", "main")

    # Clone into operator checkout; public origin points nowhere / stays untouched.
    operator_repo = tmp_path / "operator"
    _init_repo(operator_repo)
    _git(operator_repo, "remote", "add", "origin", str(tmp_path / "nonexistent_public_origin.git"))
    _git(operator_repo, "remote", "add", "swarm", str(bare_swarm))

    # Advance main in seed_repo and push only to private swarm remote.
    (seed_repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(seed_repo, "add", "app.py")
    _git(seed_repo, "commit", "-m", "advance on swarm")
    _git(seed_repo, "push", "swarm", "main")

    wt_path = prepare_epoch_worktree(
        operator_repo,
        tmp_path / "wts",
        order_id="ord-remote",
        epoch=3,
        base_ref="main",
        swarm_remote="swarm",
    )
    assert (wt_path / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert _git(wt_path, "rev-parse", "--abbrev-ref", "HEAD") == "swarm/wip/ord-remote/e3"


def test_validate_git_ref_rejects_unsafe_refs():
    assert _validate_git_ref("HEAD", "base_ref") == "HEAD"
    assert _validate_git_ref("swarm/main", "base_ref") == "swarm/main"
    for bad in (
        "main:refs/heads/pwn",
        "main..evil",
        "main~1",
        "main^0",
        "main@{1}",
        "main\\evil",
        "main\x01x",
    ):
        with pytest.raises(ValueError, match="Invalid base_ref"):
            _validate_git_ref(bad, "base_ref")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object test")
def test_windows_job_guard_kills_child_process_and_guards_reuse():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    guard = WindowsJobGuard()
    try:
        guard.assign(proc.pid)
        summary = guard.kill_all()
        assert summary["terminated_win_job"] is True
        rc = proc.wait(timeout=5)
        assert rc is not None
        with pytest.raises(RuntimeError, match="closed"):
            guard.assign(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


def test_job_guard_posix_killpg_and_external_cancel_cb(monkeypatch):
    killed: list[tuple[int, int]] = []
    daemon_cancelled: list[str] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))

    monkeypatch.setattr(os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1000, raising=False)
    monkeypatch.setattr(os, "getpgrp", lambda: 1, raising=False)

    guard = WindowsJobGuard(
        external_cancel_cb=lambda: daemon_cancelled.append("devin-session-1"),
        use_win_job=False,
    )
    guard.assign(4242)
    summary = guard.kill_all()

    assert summary["ok"] is True
    assert summary["callbacks_invoked"] == 1
    assert daemon_cancelled == ["devin-session-1"]
    assert len(killed) == 1 and killed[0][0] == 5242
    with pytest.raises(RuntimeError, match="closed"):
        guard.assign(4242)


def test_kill_posix_process_group_rejects_nonpositive_pgid(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(pgid), raising=False)
    assert kill_posix_process_group(0) is False
    assert kill_posix_process_group(-7) is False
    assert kill_posix_process_group(True) is False
    assert calls == []
    assert kill_posix_process_group(1234) is True
    assert calls == [1234]


def test_job_guard_kill_all_idempotent_and_survives_killpg_permission_error(monkeypatch):
    cancelled: list[str] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1000, raising=False)
    monkeypatch.setattr(os, "getpgrp", lambda: 1, raising=False)

    guard = WindowsJobGuard(
        external_cancel_cb=lambda: cancelled.append("sess-1"),
        use_win_job=False,
    )
    guard.assign(os.getpid() + 1)

    first = guard.kill_all()
    assert first["callbacks_invoked"] == 1
    assert first["kill_errors"]
    assert first["ok"] is False
    assert cancelled == ["sess-1"]

    second = guard.kill_all()
    assert second == {
        "ok": True,
        "terminated_win_job": False,
        "killed_pgids": [],
        "kill_errors": [],
        "callbacks_invoked": 0,
        "callback_errors": [],
    }
    assert cancelled == ["sess-1"]
    with pytest.raises(RuntimeError, match="closed"):
        guard.register_cancel_callback(lambda: None)


def test_job_guard_kill_all_records_os_kill_permission_error(monkeypatch):
    def fake_kill(pid: int, sig: int) -> None:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "kill", fake_kill, raising=False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None, raising=False)
    monkeypatch.setattr(os, "getpgid", lambda pid: 777, raising=False)
    monkeypatch.setattr(os, "getpgrp", lambda: 777, raising=False)

    cancelled: list[bool] = []
    guard = WindowsJobGuard(external_cancel_cb=lambda: cancelled.append(True), use_win_job=False)
    guard.assign(os.getpid() + 2)

    res = guard.kill_all()
    assert res["ok"] is False
    assert res["kill_errors"]
    assert res["callbacks_invoked"] == 1
    assert cancelled == [True]


def test_job_guard_assign_rejects_supervisor_pid():
    guard = WindowsJobGuard(use_win_job=False)
    with pytest.raises(ValueError, match="current supervisor process PID"):
        guard.assign(os.getpid())


def test_single_instance_lock_prevents_concurrent_acquisition(tmp_path: Path):
    lock_file = tmp_path / "night_pulse.lock"
    first = SingleInstanceLock(lock_file)
    second = SingleInstanceLock(lock_file)

    assert first.acquire() is True
    try:
        assert second.acquire() is False
        with pytest.raises(SingleInstanceLockError):
            with second:
                pass
    finally:
        first.release()

    assert second.acquire() is True
    second.release()


def test_fence_local_epoch_archives_tag_and_prunes(tmp_path: Path):
    origin_repo = tmp_path / "repo"
    _init_repo(origin_repo)
    wt_root = tmp_path / "wts"
    wt_path = prepare_epoch_worktree(origin_repo, wt_root, "ord-fence", 2)

    (wt_path / "wip.txt").write_text("in progress\n", encoding="utf-8")
    _git(wt_path, "add", "wip.txt")
    _git(wt_path, "commit", "-m", "wip epoch 2")

    cancelled: list[bool] = []
    guard = WindowsJobGuard(external_cancel_cb=lambda: cancelled.append(True), use_win_job=False)
    res = fence_local_epoch("ord-fence", 2, origin_repo, wt_path, job_guard=guard)

    assert res["ok"] is True
    assert res["fenced_tag"] == "local/fenced/ord-fence/e2"
    assert res["worktree_removed"] is True
    assert cancelled == [True]
    assert not wt_path.exists()
    tags = _git(origin_repo, "tag", "-l", "local/fenced/*")
    assert "local/fenced/ord-fence/e2" in tags.splitlines()
    worktree_list = _git(origin_repo, "worktree", "list")
    assert str(wt_path) not in worktree_list


def test_runtime_blocklist_blocks_matching_sha256_and_version(tmp_path: Path):
    cli_bin = tmp_path / "zcode.cjs"
    cli_bin.write_bytes(b"console.log('broken runtime');\n")
    digest = hashlib.sha256(cli_bin.read_bytes()).hexdigest()

    blocklist = tmp_path / "runtime_blocklist.json"
    blocklist.write_text(
        json.dumps(
            {
                "runtimes": {
                    "zcode": {
                        "versions": ["0.9.1"],
                        "sha256": [digest],
                        "reason": "Known broken ACP framing",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    blocked_by_sha = check_runtime_blocklist("zcode", cli_bin, blocklist_path=blocklist)
    assert blocked_by_sha["ok"] is False
    assert blocked_by_sha["status"] == "infra_blocked_runtime"
    assert blocked_by_sha["sha256"] == digest

    other_bin = tmp_path / "zcode_clean.cjs"
    other_bin.write_bytes(b"console.log('clean runtime');\n")
    blocked_by_ver = check_runtime_blocklist(
        "zcode", other_bin, version="0.9.1", blocklist_path=blocklist
    )
    assert blocked_by_ver["status"] == "infra_blocked_runtime"

    driver = HostOSDriver(blocklist_path=blocklist)
    allowed = driver.preflight_runtime("zcode", other_bin, version="0.9.2")
    assert allowed["ok"] is True
    assert allowed["status"] == "allowed"


def test_runtime_blocklist_malformed_json_and_scalar_root(tmp_path: Path):
    cli_bin = tmp_path / "zcode.cjs"
    cli_bin.write_bytes(b"console.log('x');\n")

    bad_syntax = tmp_path / "bad_syntax.json"
    bad_syntax.write_text("{ not valid json", encoding="utf-8")
    res = check_runtime_blocklist("zcode", cli_bin, blocklist_path=bad_syntax)
    assert res["ok"] is False
    assert res["status"] == "infra_blocked_runtime"
    assert "Malformed runtime blocklist" in res["reason"]

    for idx, scalar in enumerate(("42", '"foo"', "null")):
        scalar_path = tmp_path / f"scalar_{idx}.json"
        scalar_path.write_text(scalar, encoding="utf-8")
        res = check_runtime_blocklist("zcode", cli_bin, blocklist_path=scalar_path)
        assert res["ok"] is False
        assert res["status"] == "infra_blocked_runtime"
        assert res["reason"] == "Malformed runtime blocklist: root must be object or array"


def test_runtime_blocklist_runtimes_key_is_case_insensitive(tmp_path: Path):
    cli_bin = tmp_path / "zcode.cjs"
    cli_bin.write_bytes(b"console.log('x');\n")
    blocklist = tmp_path / "runtime_blocklist.json"
    blocklist.write_text(
        json.dumps({"runtimes": {"ZCode": {"versions": ["9.9.9"], "reason": "blocked ver"}}}),
        encoding="utf-8",
    )

    res = check_runtime_blocklist("zcode", cli_bin, version="9.9.9", blocklist_path=blocklist)
    assert res["ok"] is False
    assert res["status"] == "infra_blocked_runtime"
    assert res["reason"] == "blocked ver"

    allowed = check_runtime_blocklist("zcode", cli_bin, version="1.0.0", blocklist_path=blocklist)
    assert allowed["ok"] is True
    assert allowed["status"] == "allowed"


@pytest.fixture()
def _isolated_cloud_swarm(monkeypatch, tmp_path: Path):
    import botex  # noqa: F401
    import botex.server.config as server_config
    from botex.core import swarm_store
    from botex.server.server_cloud import _GATEWAY_RATE_LIMITER

    db = tmp_path / "swarm_store.db"
    briefs_dir = tmp_path / "bot_briefs"
    swarm_home = tmp_path / "swarm_home"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    swarm_home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("BOTEX_SWARM_STORE_DB", str(db))
    monkeypatch.setenv("BOTEX_SWARM_BRIEFS_DIR", str(briefs_dir))
    monkeypatch.setenv("BOTEX_SWARM_HOME", str(swarm_home))
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN_HASH", raising=False)
    monkeypatch.delenv("BOTEX_STRICT_AUTH", raising=False)
    monkeypatch.setenv("BOTEX_GATEWAY_RPM", "240")
    _GATEWAY_RATE_LIMITER.reset()
    server_config.load_config(reload=True)
    try:
        yield {"db": db, "briefs_dir": briefs_dir, "swarm_home": swarm_home}
    finally:
        swarm_store.close_swarm_store_dbs()
        _GATEWAY_RATE_LIMITER.reset()
        monkeypatch.delenv("BOTEX_SWARM_STORE_DB", raising=False)
        monkeypatch.delenv("BOTEX_SWARM_BRIEFS_DIR", raising=False)
        monkeypatch.delenv("BOTEX_SWARM_HOME", raising=False)
        server_config.load_config(reload=True)


def test_cloud_swarm_lease_renew_and_atomic_fence(_isolated_cloud_swarm):
    from fastapi.testclient import TestClient

    from botex.core import swarm_store
    from botex.server.server_cloud import app

    assert swarm_store.register_bot("bot-alpha", "swarm")["ok"] is True

    with TestClient(app) as client:
        # 1. Renew host lease
        r1 = client.post(
            "/api/swarm/leases/renew",
            json={
                "bot_slug": "bot-alpha",
                "host_id": "host-win",
                "scope_path": "/repo/main",
                "summary": "working on e0",
                "fencing_token": 1,
                "ttl_s": 600,
            },
        )
        assert r1.status_code == 200
        assert r1.json()["fencing_token"] == 1

        # Stale lease renew rejected with 409
        r_stale = client.post(
            "/api/swarm/leases/renew",
            json={
                "bot_slug": "bot-alpha",
                "host_id": "host-vps",
                "scope_path": "/repo/main",
                "fencing_token": 0,
            },
        )
        assert r_stale.status_code == 409

        # 2. Atomic fence: epoch 0 -> 1
        f1 = client.post(
            "/api/swarm/orders/ord-cloud-1/fence",
            json={
                "local_epoch": 0,
                "bot_slug": "bot-alpha",
                "host_id": "host-win",
                "reason": "timeout",
            },
        )
        assert f1.status_code == 200
        d1 = f1.json()
        assert d1["ok"] is True
        assert d1["previous_epoch"] == 0
        assert d1["epoch"] == 1
        assert d1["status"] == "FENCED"

        # 3. Stale local_epoch=0 rejected with 409 EPOCH_MISMATCH
        f_bad = client.post(
            "/api/swarm/orders/ord-cloud-1/fence",
            json={"local_epoch": 0, "reason": "duplicate"},
        )
        assert f_bad.status_code == 409
        assert f_bad.json()["detail"]["error"] == "EPOCH_MISMATCH"
        assert f_bad.json()["detail"]["remote_epoch"] == 1

        # 4. Matching local_epoch=1 succeeds -> epoch 2 and bumps active lease token
        f2 = client.post(
            "/api/swarm/orders/ord-cloud-1/fence",
            json={"local_epoch": 1, "bot_slug": "bot-alpha", "reason": "handoff"},
        )
        assert f2.status_code == 200
        assert f2.json()["epoch"] == 2

        leases = swarm_store.get_active_leases(bot_slug="bot-alpha")
        assert len(leases) == 1 and leases[0]["fencing_token"] == 2


def test_night_pulse_syncs_fences_and_runs_verify(_isolated_cloud_swarm, tmp_path: Path):
    import server as botex_server
    from fastapi.testclient import TestClient

    from botex.core import swarm_store
    from botex.server.server_cloud import app

    assert swarm_store.register_bot("bot-pulse", "swarm")["ok"] is True
    assert swarm_store.upsert_bot_brief("bot-pulse", "Keep diffs minimal.")["ok"] is True
    assert swarm_store.upsert_host_lease("bot-pulse", "host-1", "/repo", "active", fencing_token=1)[
        "ok"
    ]

    origin_repo = tmp_path / "repo"
    _init_repo(origin_repo)
    wt_root = tmp_path / "worktrees"
    stale_wt = prepare_epoch_worktree(origin_repo, wt_root, "ord-np-fence", 1)
    assert stale_wt.exists()

    # Fence order from epoch 0 -> 1 -> 2 so FENCED work_orders row at epoch=2 matches ord-np-fence_e1
    with TestClient(app) as client:
        assert (
            client.post("/api/swarm/orders/ord-np-fence/fence", json={"local_epoch": 0}).status_code
            == 200
        )
        assert (
            client.post("/api/swarm/orders/ord-np-fence/fence", json={"local_epoch": 1}).status_code
            == 200
        )

        verify_wt = prepare_epoch_worktree(origin_repo, wt_root, "ord-np-verify", 1)
        ledger_path = tmp_path / "supervisor_ledger.jsonl"
        lock_path = tmp_path / "pulse.lock"

        res = botex_server.run_night_pulse(
            base_url="http://testserver",
            once=True,
            lock_path=lock_path,
            origin_repo=origin_repo,
            worktree_root=wt_root,
            briefs_dir=_isolated_cloud_swarm["briefs_dir"],
            client=client,
            ledger_path=ledger_path,
            verify_jobs=[
                {
                    "order_id": "ord-np-verify",
                    "epoch": 1,
                    "origin_repo": origin_repo,
                    "wt_path": verify_wt,
                    "verify_command": [sys.executable, "-c", "print('night pulse ok')"],
                    "diff_text": "",
                    "changed_files": ["app.py"],
                }
            ],
        )

    assert res["ok"] is True
    assert res["cycles"] == 1
    assert res["sync"]["ok"] is True and res["sync"]["updated"] == 1
    assert res["active_leases"] == 1
    assert not stale_wt.exists()
    assert len(res["fenced"]) == 1
    assert res["fenced"][0]["fenced_tag"] == "local/fenced/ord-np-fence/e1"
    assert len(res["verified"]) == 1 and res["verified"][0]["verdict"] == "DONE"
    assert (_isolated_cloud_swarm["briefs_dir"] / "bot-pulse.md").is_file()

    # CLI entry point smoke check (`botex night-pulse --once`)
    cli_env = dict(os.environ)
    cli_env["BOTEX_DISABLE_DOTENV"] = "1"
    cli_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    cli_env["BOTEX_SWARM_STORE_DB"] = str(_isolated_cloud_swarm["db"])
    cli_env["BOTEX_SWARM_HOME"] = str(_isolated_cloud_swarm["swarm_home"])
    cli_proc = subprocess.run(
        [
            sys.executable,
            "server.py",
            "night-pulse",
            "--once",
            "--workspace",
            str(origin_repo),
            "--worktree-root",
            str(wt_root),
            "--lock-path",
            str(tmp_path / "cli_pulse.lock"),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=cli_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "night-pulse: 1 cycle(s)" in cli_proc.stdout
