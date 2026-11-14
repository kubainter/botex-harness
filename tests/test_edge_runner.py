# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit & loopback tests for Cloud-Edge Bridge Phase 2 worker-B slice:

- ``botex/edge/runner.py``: ROOT_JAIL traversal/symlink jail, edge_caps
  ceilings (allow_exec / allow_destructive / max_output_bytes /
  allowed_tools), secret masking before send, call_id idempotency,
  cached_sha256 digest dedupe, WSS hello/hello_ack/ping/tool_call frames,
  backoff reconnect, and the 1008 stop-without-retry rule.
- ``server.py``: ``botex edge --help`` smoke test and the
  ``resolve_edge_executor`` lookup wiring in start_task / run_subagent.

No pytest-asyncio: async paths run under ``asyncio.run``. Loopback
WebSockets use ``websockets.asyncio.server.serve`` on 127.0.0.1:0, which
the conftest socket guard permits.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from botex.edge import EdgeCapabilities, EdgeRunner
from botex.edge.runner import DEFAULT_ALLOWED_TOOLS, _IdemCache
from botex.patch_engine import snapshot_manager


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_TASK_IDS = (
    "edge_t_jail",
    "edge_t_exec",
    "edge_t_destr",
    "edge_t_idem",
    "edge_t_patch",
    "edge_t_ws",
)


def _purge_snapshot_tasks(*task_ids: str) -> None:
    import shutil

    for tid in task_ids:
        shutil.rmtree(snapshot_manager._task_dir(tid), ignore_errors=True)


@pytest.fixture(autouse=True)
def _cleanup_edge_snapshots():
    yield
    _purge_snapshot_tasks(*_TASK_IDS)


def _caps(**kwargs) -> EdgeCapabilities:
    return EdgeCapabilities(**kwargs)


def _runner(root: Path, caps: EdgeCapabilities | None = None) -> EdgeRunner:
    return EdgeRunner(
        "ws://127.0.0.1:0",
        token="tok_test",
        device_id="dev-1",
        workspace_id="ws-1",
        workspace_root=root,
        caps=caps or EdgeCapabilities(),
    )


def _call(
    runner: EdgeRunner,
    tool: str,
    args: dict,
    call_id: str = "c1",
    task_id: str = "edge_t_ws",
    **extra,
) -> dict:
    frame = {"call_id": call_id, "task_id": task_id, "tool_name": tool, "args": args}
    frame.update(extra)
    return asyncio.run(runner.handle_tool_call(frame))


# ---------------------------------------------------------------------------
# EdgeCapabilities — fail-closed defaults & config loading
# ---------------------------------------------------------------------------


def test_caps_defaults_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    caps = EdgeCapabilities.load(tmp_path)
    assert caps.allow_exec is False
    assert caps.allow_destructive is False
    assert caps.max_output_bytes == 65536
    assert caps.allowed_tools == DEFAULT_ALLOWED_TOOLS
    assert "run_command" not in caps.allowed_tools
    assert "delete_file" not in caps.allowed_tools


def test_caps_load_workspace_json(tmp_path):
    botex_dir = tmp_path / ".botex"
    botex_dir.mkdir()
    (botex_dir / "edge_caps.json").write_text(
        json.dumps(
            {
                "allow_exec": True,
                "max_output_bytes": 1024,
                "allowed_tools": ["read_file", "run_command", "read_url", "bogus"],
            }
        ),
        encoding="utf-8",
    )
    caps = EdgeCapabilities.load(tmp_path)
    assert caps.allow_exec is True
    assert caps.max_output_bytes == 1024
    # read_url/bogus are filtered to the dispatchable universe at load
    assert caps.allowed_tools == frozenset({"read_file", "run_command"})


def test_caps_to_dict_wire_shape():
    d = _caps().to_dict()
    assert d["allow_exec"] is False
    assert d["allow_destructive"] is False
    assert d["max_output_bytes"] == 65536
    assert sorted(d["allowed_tools"]) == sorted(DEFAULT_ALLOWED_TOOLS)


# ---------------------------------------------------------------------------
# ROOT_JAIL — traversal & symlink escape rejection before execution
# ---------------------------------------------------------------------------


def test_root_jail_blocks_dotdot_traversal(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "outside.txt").write_text("outside-secret", encoding="utf-8")
    runner = _runner(ws)
    for tool, args in (
        ("read_file", {"path": "../outside.txt"}),
        ("apply_patch", {"path": "../outside.txt", "search_block": "a", "replace_block": "b"}),
        ("create_file", {"path": "../evil.txt", "content": "x"}),
        ("list_dir", {"path": ".."}),
    ):
        res = _call(runner, tool, args, call_id=f"jail-{tool}", task_id="edge_t_jail")
        assert res["ok"] is False, (tool, res)
        assert "outside-secret" not in json.dumps(res)
    assert not (tmp_path / "evil.txt").exists()


def test_root_jail_blocks_symlink_escape(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("symlink-secret", encoding="utf-8")
    link = ws / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires privilege on this host")
    runner = _runner(ws)
    res = _call(runner, "read_file", {"path": "link.txt"}, task_id="edge_t_jail")
    assert res["ok"] is False
    assert "symlink-secret" not in json.dumps(res)


def test_root_jail_blocks_blocked_secret_files(tmp_path):
    (tmp_path / "botex.config.local.json").write_text("{}", encoding="utf-8")
    runner = _runner(tmp_path)
    res = _call(runner, "read_file", {"path": "botex.config.local.json"}, task_id="edge_t_jail")
    assert res["ok"] is False
    assert "blocked" in res["error"].lower()


# ---------------------------------------------------------------------------
# edge_caps ceilings — cloud can never exceed the local ceiling
# ---------------------------------------------------------------------------


def test_caps_block_run_command_not_whitelisted(tmp_path):
    res = _call(_runner(tmp_path), "run_command", {"command": 'python -c "print(1)"'})
    assert res["ok"] is False
    assert "capabilities" in res["error"]


def test_caps_block_run_command_without_allow_exec(tmp_path):
    caps = _caps(allowed_tools=DEFAULT_ALLOWED_TOOLS | {"run_command"})
    res = _call(_runner(tmp_path, caps), "run_command", {"command": 'python -c "print(1)"'})
    assert res["ok"] is False
    assert "allow_exec" in res["error"]


def test_caps_block_delete_and_move_without_allow_destructive(tmp_path):
    (tmp_path / "victim.txt").write_text("data", encoding="utf-8")
    caps = _caps(allowed_tools=DEFAULT_ALLOWED_TOOLS | {"delete_file", "move_file"})
    runner = _runner(tmp_path, caps)
    for tool, args in (
        ("delete_file", {"path": "victim.txt"}),
        ("move_file", {"src_path": "victim.txt", "dst_path": "victim2.txt"}),
    ):
        res = _call(runner, tool, args, call_id=f"destr-{tool}", task_id="edge_t_destr")
        assert res["ok"] is False
        assert "allow_destructive" in res["error"]
    assert (tmp_path / "victim.txt").exists()
    assert not (tmp_path / "victim2.txt").exists()


def test_caps_allow_destructive_executes(tmp_path):
    (tmp_path / "victim.txt").write_text("data", encoding="utf-8")
    caps = _caps(allow_destructive=True, allowed_tools=DEFAULT_ALLOWED_TOOLS | {"delete_file"})
    res = _call(
        _runner(tmp_path, caps), "delete_file", {"path": "victim.txt"}, task_id="edge_t_destr"
    )
    assert res["ok"] is True
    assert not (tmp_path / "victim.txt").exists()


def test_network_tool_rejected_even_when_whitelisted(tmp_path):
    caps = _caps()
    caps.allowed_tools = caps.allowed_tools | {"read_url"}
    res = _call(_runner(tmp_path, caps), "read_url", {"url": "https://example.com"})
    assert res["ok"] is False
    assert "not available" in res["error"]


def test_unknown_tool_rejected(tmp_path):
    res = _call(_runner(tmp_path), "format_c_drive", {})
    assert res["ok"] is False
    assert "unsupported tool" in res["error"]


def test_run_command_executes_when_allowed(tmp_path):
    caps = _caps(
        allow_exec=True,
        allowed_tools=DEFAULT_ALLOWED_TOOLS | {"run_command"},
        exec_allowlist=["python"],
    )
    res = _call(
        _runner(tmp_path, caps),
        "run_command",
        {"command": "python -c \"print('hi')\""},
        task_id="edge_t_exec",
    )
    assert res["ok"] is True
    assert "hi" in res["stdout"]


def test_stdout_truncated_at_max_output_bytes(tmp_path):
    caps = _caps(
        allow_exec=True,
        allowed_tools=DEFAULT_ALLOWED_TOOLS | {"run_command"},
        exec_allowlist=["python"],
        max_output_bytes=128,
    )
    res = _call(
        _runner(tmp_path, caps),
        "run_command",
        {"command": "python -c \"print('A' * 500)\""},
        task_id="edge_t_exec",
    )
    assert res["ok"] is True
    assert len(res["stdout"].encode("utf-8")) <= 128
    assert "[truncated on edge]" in res["stdout"]
    assert res["truncated"] is True


def test_sanitize_result_truncates_stdout_and_stderr(tmp_path):
    caps = _caps(max_output_bytes=64)
    runner = _runner(tmp_path, caps)
    out = runner._sanitize_result(
        {"ok": True, "stdout": "lorem ipsum " * 200, "stderr": "error trace " * 200}
    )
    assert len(out["stdout"].encode("utf-8")) <= 64
    assert len(out["stderr"].encode("utf-8")) <= 64
    assert "[truncated on edge]" in out["stdout"]
    assert "[truncated on edge]" in out["stderr"]
    assert out["truncated"] is True


# ---------------------------------------------------------------------------
# Secret masking on every outbound string
# ---------------------------------------------------------------------------


def test_mask_secrets_in_tool_result(tmp_path):
    raw_key = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    bearer = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    (tmp_path / "creds.txt").write_text(f"api {raw_key}\nauth {bearer}\n", encoding="utf-8")
    res = _call(_runner(tmp_path), "read_file", {"path": "creds.txt"})
    assert res["ok"] is True
    payload = json.dumps(res)
    assert raw_key not in payload
    assert bearer not in payload
    assert "[REDACTED]" in res["content"]


def test_mask_secrets_recurses_nested_fields(tmp_path):
    runner = _runner(tmp_path)
    out = runner._sanitize_result(
        {
            "ok": False,
            "error": "key sk-SECRETKEYSECRETKEY1",
            "nested": {"list": ["sk-SECRETKEYSECRETKEY1"]},
        }
    )
    payload = json.dumps(out)
    assert "sk-SECRETKEYSECRETKEY1" not in payload


# ---------------------------------------------------------------------------
# call_id idempotency — replays never re-execute mutations
# ---------------------------------------------------------------------------


def test_call_id_idempotent_replay_run_command(tmp_path):
    caps = _caps(
        allow_exec=True,
        allowed_tools=DEFAULT_ALLOWED_TOOLS | {"run_command"},
        exec_allowlist=["python"],
    )
    runner = _runner(tmp_path, caps)
    call = {
        "call_id": "same-id",
        "task_id": "edge_t_idem",
        "tool_name": "run_command",
        "args": {"command": "python -c \"open('marker.txt', 'a').write('X')\""},
    }
    r1 = asyncio.run(runner.handle_tool_call(dict(call)))
    r2 = asyncio.run(runner.handle_tool_call(dict(call)))
    assert r1["ok"] is True
    assert r2 == r1
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == "X"


def test_call_id_idempotent_replay_apply_patch(tmp_path):
    (tmp_path / "p.txt").write_text("hello\n", encoding="utf-8")
    runner = _runner(tmp_path)
    call = {
        "call_id": "patch-1",
        "task_id": "edge_t_patch",
        "tool_name": "apply_patch",
        "args": {"path": "p.txt", "search_block": "hello", "replace_block": "world"},
    }
    r1 = asyncio.run(runner.handle_tool_call(dict(call)))
    assert r1["ok"] is True
    r2 = asyncio.run(runner.handle_tool_call(dict(call)))
    # Without the idem ring this replay would fail ("search block could not
    # be located") — a cached identical result proves no re-execution.
    assert r2 == r1
    assert (tmp_path / "p.txt").read_text(encoding="utf-8") == "world\n"


def test_different_call_id_reexecutes(tmp_path):
    (tmp_path / "p.txt").write_text("hello\n", encoding="utf-8")
    runner = _runner(tmp_path)
    args = {"path": "p.txt", "search_block": "hello", "replace_block": "world"}
    r1 = _call(runner, "apply_patch", args, call_id="p1", task_id="edge_t_patch")
    r2 = _call(runner, "apply_patch", args, call_id="p2", task_id="edge_t_patch")
    assert r1["ok"] is True
    assert r2["ok"] is False  # real re-execution: search block already gone


def test_idem_cache_bounded_and_ttl(tmp_path):
    cache = _IdemCache(maxlen=4, ttl_s=600)
    for i in range(10):
        cache.put(("t", f"c{i}"), {"ok": True, "i": i})
    assert len(cache._items) == 4
    assert cache.get(("t", "c0")) is None
    assert cache.get(("t", "c9")) == {"ok": True, "i": 9}
    cache2 = _IdemCache(maxlen=4, ttl_s=-1)
    cache2.put(("t", "x"), {"ok": True})
    assert cache2.get(("t", "x")) is None


# ---------------------------------------------------------------------------
# cached_sha256 digest dedupe
# ---------------------------------------------------------------------------


def test_cached_sha256_match_returns_unchanged(tmp_path):
    f = tmp_path / "d.txt"
    f.write_bytes(b"digest-me")
    sha = _sha256(b"digest-me")
    res = _call(_runner(tmp_path), "read_file", {"path": "d.txt"}, cached_sha256=sha)
    assert res == {"ok": True, "status": "unchanged", "path": "d.txt", "sha256": sha}
    assert "content" not in res


def test_cached_sha256_in_args_also_honored(tmp_path):
    f = tmp_path / "d.txt"
    f.write_bytes(b"digest-me")
    sha = _sha256(b"digest-me")
    res = _call(
        _runner(tmp_path),
        "read_file_lines",
        {"path": "d.txt", "start": 1, "end": 1, "cached_sha256": sha},
    )
    assert res == {"ok": True, "status": "unchanged", "path": "d.txt", "sha256": sha}


def test_cached_sha256_mismatch_returns_full_content(tmp_path):
    f = tmp_path / "d.txt"
    f.write_bytes(b"digest-me")
    res = _call(_runner(tmp_path), "read_file", {"path": "d.txt"}, cached_sha256=_sha256(b"other"))
    assert res["ok"] is True
    assert res.get("status") != "unchanged"
    assert "digest-me" in res["content"]
    assert res["file_sha256"] == _sha256(b"digest-me")


# ---------------------------------------------------------------------------
# WSS loopback — hello/hello_ack, ping/pong, tool_call, masking, 1008
# ---------------------------------------------------------------------------


def _serve_and_run(tmp_path, handler, caps=None, **runner_kw):
    """Drive one loopback session: run_once() against a local serve()."""
    from websockets.asyncio.server import serve

    state: dict = {}

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = EdgeRunner(
                f"ws://127.0.0.1:{port}",
                token="tok_test",
                device_id="dev-1",
                workspace_id="ws-1",
                workspace_root=tmp_path,
                caps=caps or EdgeCapabilities(),
                **runner_kw,
            )
            state["status"] = await runner.run_once()
            state["runner"] = runner
        return state

    return asyncio.run(main())


def test_ws_hello_ack_and_ping_pong(tmp_path, caplog):
    events: dict = {}

    async def handler(ws):
        hello = json.loads(await ws.recv())
        events["hello"] = hello
        await ws.send(
            json.dumps(
                {
                    "type": "hello_ack",
                    "device_id": hello["device_id"],
                    "workspace_id": hello["workspace_id"],
                    "server_version": "2.1.0",
                    "min_client_version": "99.0.0",
                    "latest_client_version": "99.9.9",
                }
            )
        )
        await ws.send(json.dumps({"type": "ping", "ts": 12345}))
        events["pong"] = json.loads(await ws.recv())
        await ws.close(code=1000)

    with caplog.at_level(logging.INFO, logger="botex.edge"):
        state = _serve_and_run(tmp_path, handler)
    assert state["status"] == "closed"
    hello = events["hello"]
    assert hello["type"] == "hello"
    assert hello["device_id"] == "dev-1"
    assert hello["workspace_id"] == "ws-1"
    assert hello["protocol_version"] == "1.0"
    assert hello["client_version"]
    assert hello["capabilities"]["allowed_tools"]
    assert events["pong"] == {"type": "pong", "ts": 12345}
    runner = state["runner"]
    assert runner.hello_ack["server_version"] == "2.1.0"
    assert any("min_client_version" in r.getMessage() for r in caplog.records)


def test_ws_tool_call_result_masked(tmp_path):
    raw_key = "sk-WIRESECRETWIRESECRET1"
    (tmp_path / "creds.txt").write_text(f"token {raw_key}", encoding="utf-8")
    events: dict = {}

    async def handler(ws):
        hello = json.loads(await ws.recv())
        await ws.send(
            json.dumps(
                {
                    "type": "hello_ack",
                    "device_id": hello["device_id"],
                    "workspace_id": hello["workspace_id"],
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "tool_call",
                    "call_id": "wire-1",
                    "task_id": "edge_t_ws",
                    "tool_name": "read_file",
                    "args": {"path": "creds.txt"},
                }
            )
        )
        events["reply"] = json.loads(await ws.recv())
        await ws.close(code=1000)

    state = _serve_and_run(tmp_path, handler)
    assert state["status"] == "closed"
    reply = events["reply"]
    assert reply["type"] == "tool_result"
    assert reply["call_id"] == "wire-1"
    wire = json.dumps(reply)
    assert raw_key not in wire
    assert "[REDACTED]" in wire


def test_ws_1008_stops_run_forever_without_retry(tmp_path):
    attempts = 0

    async def handler(ws):
        nonlocal attempts
        attempts += 1
        await ws.recv()  # hello
        await ws.close(code=1008)

    from websockets.asyncio.server import serve

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = _runner(tmp_path)
            runner.server_url = f"ws://127.0.0.1:{port}"
            await runner.run_forever()
            return attempts

    assert asyncio.run(main()) == 1


def test_ws_reconnects_after_transient_close(tmp_path):
    attempts = 0
    runner_ref: dict = {}

    async def handler(ws):
        nonlocal attempts
        attempts += 1
        await ws.recv()  # hello
        if attempts == 1:
            await ws.close(code=1012)  # transient — client must reconnect
        else:
            runner_ref["runner"].stop()
            await ws.close(code=1000)

    from websockets.asyncio.server import serve

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = _runner(tmp_path)
            runner.server_url = f"ws://127.0.0.1:{port}"
            runner.max_backoff_s = 0.05
            runner_ref["runner"] = runner
            await runner.run_forever()
            return attempts

    assert asyncio.run(main()) == 2


def test_run_once_reports_unauthorized_on_1008(tmp_path):
    async def handler(ws):
        await ws.recv()  # hello
        await ws.close(code=1008)

    assert _serve_and_run(tmp_path, handler)["status"] == "unauthorized"


def test_concurrent_tool_calls_reply_cleanly(tmp_path):
    """5 concurrent tool_call frames must produce 5 well-formed JSON replies
    (send serialization must not interleave or drop frames)."""
    replies: dict[str, dict] = {}
    n_calls = 5

    async def handler(ws):
        hello = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "hello_ack", "device_id": hello["device_id"]}))
        for i in range(n_calls):
            await ws.send(
                json.dumps(
                    {
                        "type": "tool_call",
                        "call_id": f"cc-{i}",
                        "task_id": "edge_t_ws",
                        "tool_name": "read_file",
                        "args": {"path": f"f{i}.txt"},
                    }
                )
            )
        for i in range(n_calls):
            msg = json.loads(await ws.recv())
            assert msg["type"] == "tool_result"
            replies[msg["call_id"]] = msg["result"]
        await ws.close(code=1000)

    for i in range(n_calls):
        (tmp_path / f"f{i}.txt").write_text(f"content-{i}", encoding="utf-8")

    state = _serve_and_run(tmp_path, handler)
    assert state["status"] == "closed"
    assert sorted(replies) == [f"cc-{i}" for i in range(n_calls)]
    for i in range(n_calls):
        res = replies[f"cc-{i}"]
        assert res["ok"] is True, res
        assert f"content-{i}" in res["content"]


def test_connect_headers_kwargs_version_compat():
    """websockets>=14 renamed extra_headers -> additional_headers; the runner
    must pass the kwarg the installed library actually accepts."""
    from botex.edge.runner import _connect_headers_kwargs

    def modern_connect(*, additional_headers=None, **kwargs): ...

    def legacy_connect(*, extra_headers=None, **kwargs): ...

    def ancient_connect(**kwargs): ...

    hdrs = {"Authorization": "Bearer tok"}
    assert _connect_headers_kwargs(modern_connect, hdrs) == {"additional_headers": hdrs}
    assert _connect_headers_kwargs(legacy_connect, hdrs) == {"extra_headers": hdrs}
    # Unintrospectable signature: legacy kwarg fallback (matches websockets>=12 floor)
    assert _connect_headers_kwargs(ancient_connect, hdrs) == {"extra_headers": hdrs}


# ---------------------------------------------------------------------------
# server.py — CLI smoke + edge executor routing
# ---------------------------------------------------------------------------


def test_cli_edge_help(monkeypatch):
    import server

    monkeypatch.setattr(sys, "argv", ["botex", "edge", "--help"])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 0


def test_cli_edge_requires_token(monkeypatch, tmp_path):
    import server

    monkeypatch.delenv("BOTEX_EDGE_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["botex", "edge", "--workspace", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 2


def test_start_task_routes_edge_workspace(monkeypatch):
    import server

    sentinel = object()
    seen: list = []
    monkeypatch.setattr(server, "resolve_edge_executor", lambda wid: seen.append(wid) or sentinel)

    async def fake_run_subagent(**kwargs):
        seen.append(kwargs)

        class _R:
            content = []
            structured_content = {}

        return _R()

    monkeypatch.setattr(server, "run_subagent", fake_run_subagent)

    async def main():
        res = await server.start_task(task="t", workspace_dir="edge-ws-1")
        await server._TASKS[res["task_id"]]["task"]
        return res

    res = asyncio.run(main())
    assert res["status"] == "QUEUED"
    assert seen[0] == "edge-ws-1"
    # Logical edge ID forwarded raw — no local containment resolution.
    assert seen[1]["workspace_dir"] == "edge-ws-1"


def test_start_task_local_workspace_unchanged(monkeypatch):
    import server

    monkeypatch.setattr(server, "resolve_edge_executor", lambda wid: None)
    seen: list = []

    async def fake_run_subagent(**kwargs):
        seen.append(kwargs)

        class _R:
            content = []
            structured_content = {}

        return _R()

    monkeypatch.setattr(server, "run_subagent", fake_run_subagent)

    async def main():
        res = await server.start_task(task="t", workspace_dir="")
        await server._TASKS[res["task_id"]]["task"]
        return res

    res = asyncio.run(main())
    assert res["status"] == "QUEUED"
    assert seen[0]["workspace_dir"] == server.resolve_workspace("")


def test_run_subagent_routes_edge_executor(monkeypatch):
    import server

    sentinel = object()
    seen: list = []
    monkeypatch.setattr(server, "resolve_edge_executor", lambda wid: seen.append(wid) or sentinel)
    captured: dict = {}

    async def fake_engine(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "status": "DONE",
            "summary": "done",
            "files_touched": [],
            "task_id": "edge-1",
        }

    monkeypatch.setattr(server, "run_botex_task", fake_engine)
    result = asyncio.run(server.run_subagent(task="t", workspace_dir="edge-ws-9"))
    assert seen[0] == "edge-ws-9"
    assert captured["tool_executor"] is sentinel
    # Engine gets the local scratch dir, not the logical edge ID.
    assert captured["workspace_dir"] == server.resolve_workspace("")
    assert captured["workspace_dir"] != "edge-ws-9"
    assert result.structured_content["status"] == "DONE"


def test_run_subagent_local_path_no_executor(monkeypatch):
    import server

    monkeypatch.setattr(server, "resolve_edge_executor", lambda wid: None)
    captured: dict = {}

    async def fake_engine(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "status": "DONE",
            "summary": "done",
            "files_touched": [],
            "task_id": "loc-1",
        }

    monkeypatch.setattr(server, "run_botex_task", fake_engine)
    asyncio.run(server.run_subagent(task="t", workspace_dir=""))
    assert captured["tool_executor"] is None


def test_cli_edge_loads_botex_env_file(tmp_path, monkeypatch):
    import server
    from botex import edge as edge_pkg

    env_file = tmp_path / "mcp.env"
    env_file.write_text(
        "BOTEX_EDGE_TOKEN=btxd_envtest.secret\n"
        "BOTEX_EDGE_DEVICE_ID=env-device\n"
        "BOTEX_EDGE_WORKSPACE_ID=env-ws\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BOTEX_DISABLE_DOTENV", raising=False)
    monkeypatch.delenv("BOTEX_EDGE_TOKEN", raising=False)
    monkeypatch.delenv("BOTEX_EDGE_DEVICE_ID", raising=False)
    monkeypatch.delenv("BOTEX_EDGE_WORKSPACE_ID", raising=False)
    monkeypatch.setenv("BOTEX_ENV_FILE", str(env_file))

    captured: dict = {}

    class DummyRunner:
        def __init__(self, server_url, *, token, device_id, workspace_id, workspace_root, caps):
            captured["server_url"] = server_url
            captured["token"] = token
            captured["device_id"] = device_id
            captured["workspace_id"] = workspace_id
            captured["workspace_root"] = workspace_root
            self.device_id = device_id
            self.workspace_id = workspace_id

        async def run_forever(self):
            return None

    monkeypatch.setattr(edge_pkg, "EdgeRunner", DummyRunner)
    server._cli_edge(["-w", str(tmp_path)])

    assert captured["token"] == "btxd_envtest.secret"
    assert captured["device_id"] == "env-device"
    assert captured["workspace_id"] == "env-ws"


def test_cli_edge_respects_disable_dotenv(tmp_path, monkeypatch):
    import pytest

    import server

    env_file = tmp_path / "mcp.env"
    env_file.write_text("BOTEX_EDGE_TOKEN=btxd_should_not_load\n", encoding="utf-8")
    monkeypatch.setenv("BOTEX_DISABLE_DOTENV", "1")
    monkeypatch.setenv("BOTEX_ENV_FILE", str(env_file))
    monkeypatch.delenv("BOTEX_EDGE_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exc:
        server._cli_edge(["-w", str(tmp_path)])
    assert exc.value.code == 2
