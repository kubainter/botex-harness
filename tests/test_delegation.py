# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
import json
import os

import pytest

from botex import delegation


def test_exe_path_empty_when_unconfigured(monkeypatch):
    monkeypatch.delenv("DEVIN_ACP_EXE", raising=False)
    monkeypatch.setattr(delegation, "_delegation_cfg", lambda: {"exe_path": ""})
    assert delegation.resolve_devin_exe() == ""


def test_exe_path_rejects_non_devin_name(monkeypatch, tmp_path):
    fake = tmp_path / "malicious.exe"
    fake.write_text("x")
    monkeypatch.setattr(delegation, "_delegation_cfg", lambda: {"exe_path": str(fake)})
    assert delegation.resolve_devin_exe() == ""


def test_exe_path_rejects_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        delegation,
        "_delegation_cfg",
        lambda: {"exe_path": str(tmp_path / "devin.exe")},
    )
    assert delegation.resolve_devin_exe() == ""


def test_exe_path_accepts_devin_exe(monkeypatch, tmp_path):
    exe = tmp_path / "devin.exe"
    exe.write_text("x")
    monkeypatch.setattr(delegation, "_delegation_cfg", lambda: {"exe_path": str(exe)})
    assert delegation.resolve_devin_exe() == str(exe)


def test_auth_status_unavailable_without_exe(monkeypatch):
    monkeypatch.delenv("DEVIN_ACP_EXE", raising=False)
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.setattr(delegation, "_delegation_cfg", lambda: {"exe_path": "", "enabled": True})
    monkeypatch.setattr(delegation, "_api_key", lambda: "")
    st = delegation.devin_auth_status()
    assert st["available"] is False
    assert "exe" in st["reason"]


def test_auth_status_disabled(monkeypatch):
    monkeypatch.setattr(delegation, "_delegation_cfg", lambda: {"enabled": False})
    monkeypatch.setattr(delegation, "resolve_devin_exe", lambda: "/x/devin.exe")
    monkeypatch.setattr(delegation, "_api_key", lambda: "k")
    st = delegation.devin_auth_status()
    assert st["available"] is False
    assert st["reason"] == "disabled in config"


def test_kill_run_refuses_non_devin_pid(monkeypatch, tmp_path):
    run_dir = tmp_path / "r1"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"pid": 1234, "started_ts": 100.0}))
    monkeypatch.setattr(delegation, "RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(delegation, "_proc_start_time", lambda pid: ("python", 100.0))
    res = delegation.kill_run("r1")
    assert res["ok"] is False
    assert "PID reuse" in res["error"]


def test_kill_run_refuses_start_time_mismatch(monkeypatch, tmp_path):
    run_dir = tmp_path / "r2"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"pid": 1234, "started_ts": 100.0}))
    monkeypatch.setattr(delegation, "RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(delegation, "_proc_start_time", lambda pid: ("devin", 999999.0))
    res = delegation.kill_run("r2")
    assert res["ok"] is False
    assert "start-time" in res["error"]


def test_kill_run_already_dead(monkeypatch, tmp_path):
    run_dir = tmp_path / "r3"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"pid": 1234, "started_ts": 100.0}))
    monkeypatch.setattr(delegation, "RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(delegation, "_proc_start_time", lambda pid: (None, None))
    res = delegation.kill_run("r3")
    assert res["ok"] is True and res["already_dead"] is True


def test_list_runs_reads_meta(monkeypatch, tmp_path):
    run_dir = tmp_path / "20240101-abc"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "pid": 9999,
                "started_ts": 1.0,
                "agent_type": "review",
                "owner": "botex",
                "task_id": "t1",
                "session_id": "s1",
            }
        )
    )
    monkeypatch.setattr(delegation, "RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(delegation, "_proc_start_time", lambda pid: (None, None))
    runs = delegation.list_runs()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "20240101-abc"
    assert runs[0]["live"] is False
    assert runs[0]["task_id"] == "t1"
    assert runs[0]["session_id"] == "s1"


def test_list_runs_empty_when_no_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(delegation, "RUNS_DIR", str(tmp_path / "nope"))
    assert delegation.list_runs() == []


def test_pick_allow_prefers_allow_once():
    options = [{"optionId": "deny", "kind": "deny"}, {"optionId": "o1", "kind": "allow_once"}]
    assert delegation._pick_allow({"options": options}) == "o1"


def test_pick_allow_fallback():
    assert delegation._pick_allow({"options": []}) == "allow_once"


@pytest.mark.skipif(os.name != "nt", reason="windows-only")
def test_attach_job_returns_none_on_failure():
    class FakeProc:
        pid = -1

    # Should not raise even with a bogus pid/handle.
    delegation._attach_kill_on_close(FakeProc())


def test_cancel_run_for_task_unknown():
    assert delegation.cancel_run_for_task("no-such-task")["ok"] is False


def test_cancel_run_for_task_kills_registered_run():
    class FakeRun:
        run_id = "r1"
        killed = False
        emitted = []

        def kill(self):
            self.killed = True

        def _emit(self, kind, **data):
            self.emitted.append(kind)

    run = FakeRun()
    delegation._ACTIVE_RUNS["t1"] = run
    try:
        res = delegation.cancel_run_for_task("t1")
    finally:
        delegation._ACTIVE_RUNS.pop("t1", None)
    assert res["ok"] is True and res["run_id"] == "r1"
    assert run.killed is True
    assert run.emitted == [delegation.EventKind.DEVIN_UPDATE]


# ---------------------------------------------------------------------------
# Multi-turn session continuation (session/load resume + CLI fallback)
# ---------------------------------------------------------------------------


class _FakeRun:
    """Scripted DevinRun stand-in: captures JSON-RPC sends, replays canned
    responses keyed by request id. Never spawns a real devin.exe."""

    def __init__(self, run_id="r1", agent_type="default", task_id="", responses=None):
        self.run_id = run_id
        self.agent_type = agent_type
        self.task_id = task_id
        self.sent = []
        self.killed = False
        self.log_path = ""
        self.meta = {}
        self._responses = responses or {}

    def send(self, obj):
        self.sent.append(obj)

    def read_for(self, msg_id, timeout_s):
        return self._responses.get(msg_id)

    def collect(self, timeout_s, idle_s):
        return "done text", None

    def kill(self):
        self.killed = True

    def _meta_set(self, **fields):
        self.meta.update(fields)

    def _meta_set_session(self, sid):
        self.meta["session_id"] = sid

    def _emit(self, kind, **data):
        pass


def _patch_delegate_env(monkeypatch, run):
    monkeypatch.setattr(
        delegation,
        "devin_auth_status",
        lambda: {"available": True, "enabled": True, "reason": "ok"},
    )
    monkeypatch.setattr(delegation, "DevinRun", lambda *a, **kw: run)
    monkeypatch.setattr(delegation, "_api_key", lambda: "k")


def _sent_methods(run):
    return [o.get("method") for o in run.sent]


def test_delegate_run_new_session_without_session_id(monkeypatch):
    """No session_id -> classic session/new flow, receipt marked resumed=False."""
    responses = {
        0: {
            "id": 0,
            "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
        },
        1: {"id": 1, "result": {}},
        2: {"id": 2, "result": {"sessionId": "sess-new"}},
    }
    run = _FakeRun(responses=responses)
    _patch_delegate_env(monkeypatch, run)
    res = delegation.delegate_run("t1", "do it", "C:/ws", "default")
    methods = _sent_methods(run)
    assert "session/new" in methods
    assert "session/load" not in methods
    assert res["ok"] is True
    assert res["resumed"] is False
    assert res["resume_mode"] == "new"
    assert res["session_id"] == "sess-new"


def test_delegate_run_resumes_via_session_load(monkeypatch):
    """session_id + loadSession capability -> ACP session/load, never session/new."""
    responses = {
        0: {
            "id": 0,
            "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
        },
        1: {"id": 1, "result": {}},
        2: {"id": 2, "result": None},
    }
    run = _FakeRun(responses=responses)
    _patch_delegate_env(monkeypatch, run)
    res = delegation.delegate_run("t1", "follow up", "C:/ws", "default", session_id="sess-42")
    methods = _sent_methods(run)
    assert "session/load" in methods
    assert "session/new" not in methods
    load = next(o for o in run.sent if o.get("method") == "session/load")
    assert load["params"]["sessionId"] == "sess-42"
    # session/prompt must target the resumed session
    prompt = next(o for o in run.sent if o.get("method") == "session/prompt")
    assert prompt["params"]["sessionId"] == "sess-42"
    assert res["ok"] is True
    assert res["resumed"] is True
    assert res["resume_mode"] == "acp_load"
    assert res["session_id"] == "sess-42"


def test_delegate_run_cli_fallback_when_load_unsupported(monkeypatch):
    """No loadSession capability -> devin -r CLI resume, before authenticate."""
    responses = {
        0: {"id": 0, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
    }
    run = _FakeRun(responses=responses)
    _patch_delegate_env(monkeypatch, run)
    called = {}

    def fake_cli(task_id, prompt, cwd, session_id, timeout_s, run_id):
        called.update(task_id=task_id, prompt=prompt, session_id=session_id, run_id=run_id)
        return {
            "ok": True,
            "session_id": session_id,
            "resumed": True,
            "resume_mode": "cli",
        }

    monkeypatch.setattr(delegation, "_run_cli_resume", fake_cli)
    res = delegation.delegate_run("t1", "fu", "C:/ws", "default", session_id="sess-9")
    methods = _sent_methods(run)
    # Bails out right after initialize — no auth/session RPCs on the dead run.
    assert "session/load" not in methods
    assert "session/new" not in methods
    assert "authenticate" not in methods
    assert run.killed is True
    assert called["session_id"] == "sess-9"
    assert called["prompt"] == "fu"
    assert res["resume_mode"] == "cli" and res["resumed"] is True


def test_delegate_run_cli_fallback_on_load_error(monkeypatch):
    """Advertised loadSession but session/load errors -> CLI resume fallback."""
    responses = {
        0: {
            "id": 0,
            "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
        },
        1: {"id": 1, "result": {}},
        2: {"id": 2, "error": {"code": -32000, "message": "session not found"}},
    }
    run = _FakeRun(responses=responses)
    _patch_delegate_env(monkeypatch, run)
    called = {}

    def fake_cli(task_id, prompt, cwd, session_id, timeout_s, run_id):
        called["session_id"] = session_id
        return {
            "ok": True,
            "session_id": session_id,
            "resumed": True,
            "resume_mode": "cli",
        }

    monkeypatch.setattr(delegation, "_run_cli_resume", fake_cli)
    res = delegation.delegate_run("t1", "fu", "C:/ws", "default", session_id="sess-dead")
    assert "session/load" in _sent_methods(run)
    assert called["session_id"] == "sess-dead"
    assert res["resume_mode"] == "cli"
    assert res["acp_load_error"]["error"]["code"] == -32000


def test_delegate_run_rejects_invalid_session_id(monkeypatch):
    """Path-injection-shaped session_id is refused before spawning anything."""
    _patch_delegate_env(monkeypatch, _FakeRun())
    res = delegation.delegate_run("t1", "x", "C:/ws", "default", session_id="../evil")
    assert res["ok"] is False
    assert "session_id" in res["error"]


def test_run_cli_resume_receipt(monkeypatch):
    """_run_cli_resume wraps CliResumeRun into a compact resumed receipt."""

    class FakeCli:
        def __init__(self, run_id, task_id, session_id, cwd, prompt):
            self.run_id = run_id
            self.log_path = "log"
            self.pair = (session_id, prompt)

        def _emit(self, *a, **kw):
            pass

        def wait(self, timeout_s):
            return "cli output", None

        def kill(self):
            pass

    monkeypatch.setattr(delegation, "CliResumeRun", FakeCli)
    res = delegation._run_cli_resume("t1", "fu", "C:/ws", "sess-1", 10, "rid")
    assert res["ok"] is True
    assert res["resume_mode"] == "cli"
    assert res["resumed"] is True
    assert res["session_id"] == "sess-1"
    assert res["summary"] == "cli output"


def test_cli_resume_run_spawn_args_and_meta(monkeypatch, tmp_path):
    """CliResumeRun launches `devin -r <sid> -p <prompt>` and records run.json."""
    captured = {}

    class FakeProc:
        pid = 4242
        returncode = 0

        def communicate(self, timeout=None):
            return (b"out", b"")

    monkeypatch.setattr(delegation, "RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(delegation, "resolve_devin_exe", lambda: "C:/devin/devin.exe")
    monkeypatch.setattr(delegation, "_api_key", lambda: "K")
    monkeypatch.setattr(delegation, "_attach_kill_on_close", lambda p: None)

    def fake_popen(args, **kw):
        captured["args"] = args
        captured["cwd"] = kw.get("cwd")
        return FakeProc()

    monkeypatch.setattr(delegation.subprocess, "Popen", fake_popen)
    run = delegation.CliResumeRun("r1", "t1", "sess-9", "C:/ws", "fix it")
    args = captured["args"]
    assert args[:3] == ["C:/devin/devin.exe", "-r", "sess-9"]
    assert "--model" in args and delegation.PINNED_MODEL in args
    assert "-p" in args
    assert args[-1] == "fix it"
    assert captured["cwd"] == "C:/ws"
    meta = json.loads((tmp_path / "r1" / "run.json").read_text())
    assert meta["session_id"] == "sess-9"
    assert meta["task_id"] == "t1"
    assert meta["resume_mode"] == "cli"
    text, err = run.wait(5)
    assert err is None and text == "out"


# ---------------------------------------------------------------------------
# POST /api/tasks/{task_id}/followup (cloud gateway)
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_client(monkeypatch, tmp_path):
    """TestClient over the cloud app with persistent-task IO sandboxed."""
    from fastapi.testclient import TestClient

    # Import BEFORE delenv: botex.config load_dotenv() runs at import time and
    # would otherwise re-set BOTEX_ACCESS_TOKEN after the cleanup.
    from botex import server_cloud

    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(server_cloud, "_load_tasks_persistent", lambda *a, **k: None)
    with TestClient(server_cloud.app) as c:
        yield c, server_cloud


def _seed_devin_task(server_cloud, tid, workspace):
    server_cloud._TASKS[tid] = {
        "task_id": tid,
        "task": "original task",
        "request": {
            "task": "original task",
            "executor": "devin",
            "workspace_dir": str(workspace),
            "agent_type": "default",
        },
        "status": "DONE",
        "executor": "devin",
        "session_id": "sess-1",
        "turns": [{"turn": 1, "prompt": "original task", "summary": "done", "ok": True}],
        "created_at": 1.0,
        "finished_at": 2.0,
        "result": "done",
        "result_data": {"ok": True, "session_id": "sess-1"},
        "async_task": None,
    }


def test_followup_endpoint_drives_session_resume(cloud_client, monkeypatch, tmp_path):
    """Follow-up POST resumes the stored session via delegate_run(session_id)."""
    import time

    client, server_cloud = cloud_client
    calls = {}

    def fake_delegate(task_id, prompt, cwd, agent_type, timeout_s=1800, session_id=""):
        calls["session_id"] = session_id
        calls["prompt"] = prompt
        return {
            "ok": True,
            "session_id": "sess-1",
            "resumed": True,
            "resume_mode": "acp_load",
            "summary": "turn2 done",
        }

    monkeypatch.setattr("botex.delegation.delegate_run", fake_delegate)
    monkeypatch.setattr(
        "botex.delegation.devin_auth_status",
        lambda: {"available": True, "enabled": True, "reason": "ok"},
    )

    tid = "devintask1"
    _seed_devin_task(server_cloud, tid, tmp_path)
    try:
        resp = client.post(f"/api/tasks/{tid}/followup", json={"prompt": "fix tests"})
        assert resp.status_code == 202
        data = resp.json()
        assert data["ok"] is True and data["session_id"] == "sess-1"

        entry = server_cloud._TASKS[tid]
        for _ in range(200):
            if entry["status"] in {"DONE", "FAILED", "CANCELLED"}:
                break
            time.sleep(0.05)
        assert entry["status"] == "DONE"
        assert calls["session_id"] == "sess-1"
        assert calls["prompt"] == "fix tests"
        assert len(entry["turns"]) == 2
        assert entry["turns"][-1]["resumed"] is True
        assert entry["turns"][-1]["prompt"] == "fix tests"

        detail = client.get(f"/api/tasks/{tid}").json()
        assert detail["session_id"] == "sess-1"
        assert len(detail["turns"]) == 2
    finally:
        server_cloud._TASKS.pop(tid, None)


def test_followup_endpoint_rejects_non_devin_task(cloud_client, tmp_path):
    client, server_cloud = cloud_client
    tid = "botextask1"
    server_cloud._TASKS[tid] = {
        "task_id": tid,
        "task": "x",
        "request": {"executor": "botex"},
        "status": "DONE",
        "executor": "botex",
        "created_at": 1.0,
        "finished_at": 2.0,
        "result": "done",
        "result_data": {},
        "async_task": None,
    }
    try:
        resp = client.post(f"/api/tasks/{tid}/followup", json={"prompt": "hi"})
        assert resp.status_code == 400
    finally:
        server_cloud._TASKS.pop(tid, None)


def test_followup_endpoint_requires_session_id(cloud_client, tmp_path):
    client, server_cloud = cloud_client
    tid = "devintask2"
    _seed_devin_task(server_cloud, tid, tmp_path)
    server_cloud._TASKS[tid]["session_id"] = None
    server_cloud._TASKS[tid]["result_data"] = {}
    try:
        resp = client.post(f"/api/tasks/{tid}/followup", json={"prompt": "hi"})
        assert resp.status_code == 409
    finally:
        server_cloud._TASKS.pop(tid, None)


def test_followup_endpoint_rejects_empty_prompt(cloud_client, tmp_path):
    client, server_cloud = cloud_client
    tid = "devintask3"
    _seed_devin_task(server_cloud, tid, tmp_path)
    try:
        resp = client.post(f"/api/tasks/{tid}/followup", json={"prompt": "   "})
        assert resp.status_code == 400
    finally:
        server_cloud._TASKS.pop(tid, None)


def test_followup_endpoint_rejects_running_task(cloud_client, tmp_path):
    client, server_cloud = cloud_client
    tid = "devintask4"
    _seed_devin_task(server_cloud, tid, tmp_path)
    server_cloud._TASKS[tid]["status"] = "RUNNING"
    try:
        resp = client.post(f"/api/tasks/{tid}/followup", json={"prompt": "hi"})
        assert resp.status_code == 409
    finally:
        server_cloud._TASKS.pop(tid, None)


def test_followup_endpoint_404(cloud_client):
    client, server_cloud = cloud_client
    resp = client.post("/api/tasks/nope12345/followup", json={"prompt": "hi"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# ACP fs bridge jail + scrubbed child env
# ---------------------------------------------------------------------------


def test_fs_read_rejects_traversal(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "outside.txt").write_text("outside-secret")
    res = delegation._fs_read(str(ws), {"path": "../outside.txt"})
    assert "outside-secret" not in res
    assert "outside workspace" in res
    res = delegation._fs_read(str(ws), {"path": str(tmp_path / "outside.txt")})
    assert "outside-secret" not in res
    assert "outside workspace" in res


def test_fs_read_rejects_blocked_pattern(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("TOKEN=zzz")
    res = delegation._fs_read(str(ws), {"path": ".env"})
    assert "TOKEN=zzz" not in res
    assert "blocked" in res.lower()


def test_fs_read_masks_secrets(tmp_path):
    (tmp_path / "cfg.txt").write_text('api_key = "sk-livekey-1234567890"')
    res = delegation._fs_read(str(tmp_path), {"path": "cfg.txt"})
    assert "sk-livekey-1234567890" not in res
    assert "[REDACTED]" in res


def test_fs_write_jailed_to_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    err = delegation._fs_write(str(ws), {"path": "../evil.txt", "content": "x"})
    assert err is not None
    assert not (tmp_path / "evil.txt").exists()
    err = delegation._fs_write(str(ws), {"path": ".env", "content": "x"})
    assert err is not None
    assert not (ws / ".env").exists()
    err = delegation._fs_write(str(ws), {"path": "ok.txt", "content": "hi"})
    assert err is None
    assert (ws / "ok.txt").read_text() == "hi"


def test_devin_env_allowlist_drops_provider_keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-secret")
    monkeypatch.setenv("DEVIN_API_KEY", "devin-key")
    monkeypatch.setenv("WINDSURF_API_KEY", "ws-key")
    env = delegation._devin_env()
    assert "OPENROUTER_API_KEY" not in env
    assert "NVIDIA_API_KEY" not in env
    assert env["DEVIN_API_KEY"] == "devin-key"
    assert env["WINDSURF_API_KEY"] == "ws-key"
    assert env.get("PATH") == os.environ.get("PATH")


def test_devin_run_fs_handlers_scoped_to_workspace(monkeypatch, tmp_path):
    """_handle_agent_request jails fs/* calls in the run's session workspace."""

    class _Stdin:
        def write(self, data):
            pass

        def flush(self):
            pass

    class _Proc:
        pid = 4242
        stdin = _Stdin()
        stdout = []

    monkeypatch.setattr(delegation, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(delegation, "resolve_devin_exe", lambda: "C:/devin/devin.exe")
    monkeypatch.setattr(delegation, "_attach_kill_on_close", lambda p: None)
    monkeypatch.setattr(delegation.subprocess, "Popen", lambda *a, **kw: _Proc())
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.txt").write_text('token = "ghp_secretsecret99"')
    run = delegation.DevinRun("r1", "default", task_id="t", cwd=str(ws))
    sent = []
    run.send = sent.append
    run._handle_agent_request(
        {"id": 1, "method": "fs/read_text_file", "params": {"path": "ok.txt"}}
    )
    run._handle_agent_request(
        {"id": 2, "method": "fs/read_text_file", "params": {"path": "../outside.txt"}}
    )
    run._handle_agent_request(
        {
            "id": 3,
            "method": "fs/write_text_file",
            "params": {"path": "../evil.txt", "content": "x"},
        }
    )
    read_ok = sent[0]["result"]["content"]
    assert "ghp_secretsecret99" not in read_ok
    assert "[REDACTED]" in read_ok
    assert "outside workspace" in sent[1]["result"]["content"]
    assert sent[2]["error"]["code"] == -32603
    assert not (tmp_path / "evil.txt").exists()
