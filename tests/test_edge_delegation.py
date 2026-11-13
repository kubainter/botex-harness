# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit & loopback tests for Cloud-Edge Bridge Phase 3 worker-B slice:

- ``botex/edge/delegation.py``: opt-in policy (disabled by default,
  fail-closed on malformed config), SHA-256 digest pinning (frame pin +
  on-disk ``skill_path`` tamper detection), strict params schema
  (``additionalProperties: false``, recursive NFKC forbidden-key guard,
  bool-vs-int guard, str-length caps, ROOT_JAIL path params), masking +
  byte cap, and the built-in ``workspace-summary`` / ``skill-read``
  handlers plus the ``register_handler`` extension point.
- ``botex/edge/runner.py``: ``delegation`` advertisement inside the
  ``hello`` capabilities, ``delegate_skill`` -> ``delegate_result`` /
  ``delegate_error`` frames and ``del:<call_id>`` idempotent replays over
  a real WebSocket loopback.

No pytest-asyncio: async paths run under ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from botex.edge import EdgeCapabilities, EdgeRunner
from botex.edge.delegation import (
    FORBIDDEN_PARAM_KEYS,
    DelegationError,
    EdgeDelegationHandler,
    EdgeDelegationPolicy,
    EdgeSkillSpec,
    validate_params,
    verify_skill_digest,
)
from botex.tools.security import SecurityError

_SHA = "a" * 64
_SHA2 = "b" * 64


def _spec(name: str = "workspace-summary", sha256: str = _SHA, **kw) -> EdgeSkillSpec:
    return EdgeSkillSpec(name=name, sha256=sha256, **kw)


def _policy(enabled: bool = True, skills: dict | None = None) -> EdgeDelegationPolicy:
    if skills is None:
        skills = {"workspace-summary": _spec()}
    return EdgeDelegationPolicy(enabled=enabled, allowed_skills=skills)


def _handler(root: Path, policy: EdgeDelegationPolicy | None = None, **kw) -> EdgeDelegationHandler:
    return EdgeDelegationHandler(policy if policy is not None else _policy(), root, **kw)


def _handle(handler: EdgeDelegationHandler, skill: str, sha256=_SHA, params=None) -> dict:
    return asyncio.run(handler.handle(skill, sha256, params))


def _runner(
    root: Path,
    policy: EdgeDelegationPolicy | None = None,
    handler: EdgeDelegationHandler | None = None,
    caps: EdgeCapabilities | None = None,
) -> EdgeRunner:
    return EdgeRunner(
        "ws://127.0.0.1:0",
        token="tok_test",
        device_id="dev-1",
        workspace_id="ws-1",
        workspace_root=root,
        caps=caps or EdgeCapabilities(),
        delegation_policy=policy,
        delegation_handler=handler,
    )


# ---------------------------------------------------------------------------
# EdgeDelegationPolicy — opt-in disabled by default, fail-closed loading
# ---------------------------------------------------------------------------


def test_policy_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    policy = EdgeDelegationPolicy.load(tmp_path)
    assert policy.enabled is False
    assert policy.to_public_dict() == {"enabled": False, "skills": []}
    assert EdgeDelegationPolicy().to_public_dict() == {"enabled": False, "skills": []}


def test_policy_from_dict_fail_closed():
    for bad in (None, "x", 42, [], {"enabled": "yes"}, {"enabled": 1}, {"enabled": None}):
        assert EdgeDelegationPolicy.from_dict(bad).enabled is False, bad
    policy = EdgeDelegationPolicy.from_dict({"enabled": True, "allowed_skills": "nope"})
    assert policy.enabled is True
    assert policy.allowed_skills == {}


def test_policy_load_malformed_json_stays_disabled(tmp_path):
    cfg_dir = tmp_path / ".botex"
    cfg_dir.mkdir()
    (cfg_dir / "edge_caps.json").write_text("{not json", encoding="utf-8")
    policy = EdgeDelegationPolicy.load(tmp_path)
    assert policy.enabled is False
    assert policy.to_public_dict() == {"enabled": False, "skills": []}


def test_policy_load_missing_delegation_section_stays_disabled(tmp_path):
    cfg_dir = tmp_path / ".botex"
    cfg_dir.mkdir()
    (cfg_dir / "edge_caps.json").write_text(
        json.dumps({"allow_exec": True, "allowed_tools": ["read_file"]}), encoding="utf-8"
    )
    policy = EdgeDelegationPolicy.load(tmp_path)
    assert policy.enabled is False


def test_policy_load_enabled_from_delegation_section(tmp_path):
    cfg_dir = tmp_path / ".botex"
    cfg_dir.mkdir()
    (cfg_dir / "edge_caps.json").write_text(
        json.dumps(
            {
                "allow_exec": True,  # unrelated caps share the same file
                "delegation": {
                    "enabled": True,
                    "allowed_skills": {
                        "workspace-summary": {
                            "sha256": _SHA,
                            "read_only": True,
                            "description": "summary",
                            "params_schema": {"allowed_params": {}},
                        },
                        "bad-pin": {"sha256": "xyz"},  # skipped: not 64 hex
                        "no-pin": {},  # skipped: missing sha256
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    policy = EdgeDelegationPolicy.load(tmp_path)
    assert policy.enabled is True
    assert sorted(policy.allowed_skills) == ["workspace-summary"]
    public = policy.to_public_dict()
    assert public["enabled"] is True
    assert public["skills"][0]["name"] == "workspace-summary"
    assert public["skills"][0]["sha256"] == _SHA
    assert public["skills"][0]["read_only"] is True
    assert "skill_path" not in public["skills"][0]


def test_policy_load_explicit_path_standalone_document(tmp_path):
    cfg = tmp_path / "delegation.json"
    cfg.write_text(
        json.dumps({"enabled": True, "allowed_skills": {"s": {"sha256": _SHA}}}),
        encoding="utf-8",
    )
    policy = EdgeDelegationPolicy.load(tmp_path, cfg)
    assert policy.enabled is True
    assert list(policy.allowed_skills) == ["s"]
    assert EdgeDelegationPolicy.load(tmp_path, tmp_path / "missing.json").enabled is False


# ---------------------------------------------------------------------------
# validate_params — strict schema, forbidden keys, types, ROOT_JAIL
# ---------------------------------------------------------------------------


def test_validate_params_rejects_forbidden_keys(tmp_path):
    spec = _spec(
        params_schema={"allowed_params": {"path": "str", "command": "str", "files": "list[str]"}}
    )
    bad_params = (
        {"command": "rm -rf /"},  # even though an operator (mis)declared it
        {"ｃｏｍｍａｎｄ": "rm -rf /"},  # NFKC fullwidth fold -> 'command'
        {" COMMAND ": "x"},  # strip + lower -> 'command'
        {"path": "a", "files": [{"command": "x"}]},  # nested dict inside list
        {"path": {"nested": {"exec": "x"}}},  # nested dict inside dict value
        {"path": "a", "files": ["ok", {"shell": "x"}]},
    )
    for bad in bad_params:
        with pytest.raises(DelegationError, match="forbidden"):
            validate_params(spec, bad, tmp_path)
    for key in FORBIDDEN_PARAM_KEYS:
        with pytest.raises(DelegationError):
            validate_params(spec, {key: "x"}, tmp_path)


def test_validate_params_rejects_extra_and_missing_required(tmp_path):
    spec = _spec(params_schema={"allowed_params": {"path": "str"}, "required": ["path"]})
    assert validate_params(spec, {"path": "x"}, tmp_path) == {"path": "x"}
    with pytest.raises(DelegationError, match="required"):
        validate_params(spec, {}, tmp_path)
    with pytest.raises(DelegationError, match="unexpected"):
        validate_params(spec, {"path": "x", "extra": 1}, tmp_path)
    with pytest.raises(DelegationError, match="JSON object"):
        validate_params(spec, "nope", tmp_path)
    with pytest.raises(DelegationError, match="JSON object"):
        validate_params(spec, ["path"], tmp_path)


def test_validate_params_bool_vs_int_guard(tmp_path):
    spec = _spec(params_schema={"allowed_params": {"n": "int", "b": "bool", "f": "float"}})
    assert validate_params(spec, {"n": 5, "b": True, "f": 2.5}, tmp_path)
    assert validate_params(spec, {"f": 3}, tmp_path)  # int accepted for float
    for bad in ({"n": True}, {"b": 1}, {"b": "yes"}, {"f": True}, {"n": 2.5}):
        with pytest.raises(DelegationError):
            validate_params(spec, bad, tmp_path)


def test_validate_params_str_len_and_unknown_type(tmp_path):
    spec = _spec(
        params_schema={
            "allowed_params": {"s": "str", "l": "list[str]"},
            "max_str_len": 4,
        }
    )
    assert validate_params(spec, {"s": "ab", "l": ["x", "yz"]}, tmp_path)
    with pytest.raises(DelegationError):
        validate_params(spec, {"s": "abcde"}, tmp_path)
    with pytest.raises(DelegationError):
        validate_params(spec, {"l": ["abcde"]}, tmp_path)
    with pytest.raises(DelegationError):
        validate_params(spec, {"l": [1]}, tmp_path)
    bad_spec = _spec(params_schema={"allowed_params": {"x": "bytes"}})
    with pytest.raises(DelegationError, match="unsupported type"):
        validate_params(bad_spec, {"x": "a"}, tmp_path)


def test_validate_params_path_jail(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "outside.txt").write_text("outside-secret", encoding="utf-8")
    spec = _spec(
        params_schema={"allowed_params": {"path": "str", "dir": "str", "files": "list[str]"}}
    )
    assert validate_params(spec, {"path": "sub/ok.txt"}, ws)
    assert validate_params(spec, {"dir": "."}, ws)
    assert validate_params(spec, {"files": ["a.txt", "b/c.txt"]}, ws)
    for bad in (
        {"path": "../outside.txt"},
        {"path": ".."},
        {"dir": "../"},
        {"files": ["a.txt", "../outside.txt"]},
    ):
        with pytest.raises(SecurityError):
            validate_params(spec, bad, ws)


def test_validate_params_path_symlink_escape(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("symlink-secret", encoding="utf-8")
    link = ws / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires privilege on this host")
    spec = _spec(params_schema={"allowed_params": {"path": "str"}})
    with pytest.raises(SecurityError):
        validate_params(spec, {"path": "link.txt"}, ws)


def test_validate_params_path_requires_root():
    spec = _spec(params_schema={"allowed_params": {"path": "str"}})
    with pytest.raises(DelegationError):
        validate_params(spec, {"path": "a.txt"}, None)


# ---------------------------------------------------------------------------
# verify_skill_digest — frame pin + on-disk skill_path pinning
# ---------------------------------------------------------------------------


def test_verify_skill_digest_frame_pin(tmp_path):
    spec = _spec()
    verify_skill_digest(spec, _SHA, tmp_path)
    verify_skill_digest(spec, _SHA.upper(), tmp_path)  # case-insensitive compare
    with pytest.raises(DelegationError, match="non-empty"):
        verify_skill_digest(spec, "", tmp_path)
    with pytest.raises(DelegationError, match="mismatch"):
        verify_skill_digest(spec, _SHA2, tmp_path)
    with pytest.raises(DelegationError):
        verify_skill_digest(_spec(sha256="not-hex"), _SHA, tmp_path)


def test_verify_skill_digest_skill_path_pinning(tmp_path):
    payload = b"skill-body-v1"
    (tmp_path / "skill.md").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    spec = _spec(sha256=sha, skill_path="skill.md")
    verify_skill_digest(spec, sha, tmp_path)
    (tmp_path / "skill.md").write_bytes(b"tampered")
    with pytest.raises(DelegationError, match="on-disk"):
        verify_skill_digest(spec, sha, tmp_path)
    spec_missing = _spec(sha256=sha, skill_path="missing.md")
    with pytest.raises(DelegationError, match="missing"):
        verify_skill_digest(spec_missing, sha, tmp_path)
    spec_escape = _spec(sha256=sha, skill_path="../escape.md")
    with pytest.raises(SecurityError):
        verify_skill_digest(spec_escape, sha, tmp_path)


# ---------------------------------------------------------------------------
# EdgeDelegationHandler — dispatch, builtins, registry, masking, byte cap
# ---------------------------------------------------------------------------


def test_handler_disabled_and_unknown_skill(tmp_path):
    handler = _handler(tmp_path, _policy(enabled=False))
    with pytest.raises(DelegationError, match="disabled"):
        _handle(handler, "workspace-summary")
    handler_on = _handler(tmp_path)
    with pytest.raises(DelegationError, match="unknown skill"):
        _handle(handler_on, "does-not-exist")
    with pytest.raises(DelegationError, match="mismatch"):
        _handle(handler_on, "workspace-summary", sha256=_SHA2)
    with pytest.raises(DelegationError, match="non-empty"):
        _handle(handler_on, "workspace-summary", sha256="")


def test_handler_builtin_workspace_summary(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    res = _handle(_handler(tmp_path), "workspace-summary", _SHA, {})
    assert res["ok"] is True
    assert res["workspace"] == tmp_path.name
    assert any(e["path"] == "a.txt" for e in res["entries"])


def test_handler_builtin_skill_read(tmp_path):
    (tmp_path / "s.md").write_text("body-of-skill", encoding="utf-8")
    policy = _policy(
        skills={
            "skill-read": _spec(
                name="skill-read",
                params_schema={"allowed_params": {"path": "str"}, "required": ["path"]},
            )
        }
    )
    handler = _handler(tmp_path, policy)
    res = _handle(handler, "skill-read", _SHA, {"path": "s.md"})
    assert res["ok"] is True
    assert "body-of-skill" in res["content"]
    with pytest.raises(DelegationError):
        _handle(handler, "skill-read", _SHA, {})
    with pytest.raises(SecurityError):
        _handle(handler, "skill-read", _SHA, {"path": "../s.md"})


def test_handler_builtin_skill_read_missing_path_arg(tmp_path):
    policy = _policy(skills={"skill-read": _spec(name="skill-read")})
    handler = _handler(tmp_path, policy)
    with pytest.raises(DelegationError, match="path"):
        _handle(handler, "skill-read", _SHA, {"path": ""})


def test_handler_register_custom_sync_and_async(tmp_path):
    async def _echo(spec, params, root):
        return {"ok": True, "echo": params.get("msg")}

    policy = _policy(
        skills={
            "echo": _spec(
                name="echo",
                params_schema={"allowed_params": {"msg": "str"}, "required": ["msg"]},
            )
        }
    )
    handler = _handler(tmp_path, policy)
    handler.register_handler("echo", _echo)
    res = _handle(handler, "echo", _SHA, {"msg": "hi"})
    assert res == {"ok": True, "echo": "hi"}
    handler.register_handler("echo", lambda s, p, r: {"ok": True, "echo": "sync"})
    res = _handle(handler, "echo", _SHA, {"msg": "hi"})
    assert res["echo"] == "sync"
    with pytest.raises(ValueError):
        handler.register_handler("", _echo)
    with pytest.raises(ValueError):
        handler.register_handler("x", "not-callable")


def test_handler_masks_and_bounds_output(tmp_path):
    raw_key = "sk-EDGESECRETEDGESECRET1"

    def _leaky(spec, params, root):
        return {"ok": True, "content": f"token {raw_key}", "big": "A" * 5000}

    policy = _policy(skills={"leak": _spec(name="leak")})
    handler = _handler(tmp_path, policy, max_output_bytes=256)
    handler.register_handler("leak", _leaky)
    res = _handle(handler, "leak", _SHA, {})
    wire = json.dumps(res, ensure_ascii=False)
    assert raw_key not in wire
    assert "[REDACTED]" in res["content"]
    assert len(wire.encode("utf-8")) <= 256
    assert res["truncated"] is True
    assert "[truncated on edge]" in res["big"]


# ---------------------------------------------------------------------------
# EdgeRunner — hello advertisement, delegate frames, idempotent replay
# ---------------------------------------------------------------------------


def test_runner_hello_advertises_delegation(tmp_path):
    runner = _runner(tmp_path, EdgeDelegationPolicy())
    assert runner._hello_frame()["capabilities"]["delegation"] == {
        "enabled": False,
        "skills": [],
    }
    runner_on = _runner(tmp_path, _policy())
    delegation = runner_on._hello_frame()["capabilities"]["delegation"]
    assert delegation["enabled"] is True
    assert delegation["skills"][0]["name"] == "workspace-summary"
    assert delegation["skills"][0]["sha256"] == _SHA
    assert delegation["skills"][0]["read_only"] is True


def test_runner_loads_delegation_policy_from_edge_caps(tmp_path):
    cfg_dir = tmp_path / ".botex"
    cfg_dir.mkdir()
    (cfg_dir / "edge_caps.json").write_text(
        json.dumps(
            {
                "delegation": {
                    "enabled": True,
                    "allowed_skills": {"workspace-summary": {"sha256": _SHA}},
                }
            }
        ),
        encoding="utf-8",
    )
    runner = _runner(tmp_path)  # no explicit policy -> loaded from edge_caps
    assert runner._delegation_policy.enabled is True
    delegation = runner._hello_frame()["capabilities"]["delegation"]
    assert delegation["skills"][0]["name"] == "workspace-summary"


def _serve_and_run(tmp_path, server_handler, **runner_kw):
    """Drive one loopback session: run_once() against a local serve()."""
    from websockets.asyncio.server import serve

    state: dict = {}

    async def main():
        async with serve(server_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = EdgeRunner(
                f"ws://127.0.0.1:{port}",
                token="tok_test",
                device_id="dev-1",
                workspace_id="ws-1",
                workspace_root=tmp_path,
                caps=EdgeCapabilities(),
                **runner_kw,
            )
            state["status"] = await runner.run_once()
            state["runner"] = runner
        return state

    return asyncio.run(main())


def test_ws_delegate_skill_result_roundtrip(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    events: dict = {}

    async def server_handler(ws):
        hello = json.loads(await ws.recv())
        events["hello"] = hello
        await ws.send(
            json.dumps(
                {
                    "type": "delegate_skill",
                    "call_id": "d1",
                    "task_id": "t1",
                    "skill": "workspace-summary",
                    "sha256": _SHA,
                    "params": {},
                }
            )
        )
        events["reply"] = json.loads(await ws.recv())
        await ws.close(code=1000)

    state = _serve_and_run(tmp_path, server_handler, delegation_policy=_policy())
    assert state["status"] == "closed"
    assert events["hello"]["capabilities"]["delegation"]["enabled"] is True
    reply = events["reply"]
    assert reply["type"] == "delegate_result"
    assert reply["call_id"] == "d1"
    assert reply["result"]["ok"] is True
    assert any(e["path"] == "a.txt" for e in reply["result"]["entries"])


def test_ws_delegate_skill_disabled_policy_error(tmp_path):
    events: dict = {}

    async def server_handler(ws):
        await ws.recv()  # hello
        await ws.send(
            json.dumps(
                {
                    "type": "delegate_skill",
                    "call_id": "e0",
                    "task_id": "t1",
                    "skill": "workspace-summary",
                    "sha256": _SHA,
                    "params": {},
                }
            )
        )
        events["reply"] = json.loads(await ws.recv())
        await ws.close(code=1000)

    state = _serve_and_run(tmp_path, server_handler)
    assert state["status"] == "closed"
    reply = events["reply"]
    assert reply["type"] == "delegate_error"
    assert reply["call_id"] == "e0"
    assert "disabled" in reply["error"]


def test_ws_delegate_skill_error_paths(tmp_path):
    replies: dict = {}

    async def server_handler(ws):
        await ws.recv()  # hello
        frames = (
            ("e1", {"skill": "unknown", "sha256": _SHA, "params": {}}),
            ("e2", {"skill": "workspace-summary", "sha256": _SHA2, "params": {}}),
            ("e3", {"skill": "workspace-summary", "sha256": _SHA, "params": {"command": "rm"}}),
            ("e4", {"skill": "workspace-summary"}),  # missing sha256 pin
            ("e5", {"skill": "workspace-summary", "sha256": _SHA, "params": {"extra": 1}}),
        )
        for call_id, frame in frames:
            wire = {"type": "delegate_skill", "call_id": call_id, "task_id": "t1"}
            wire.update(frame)
            await ws.send(json.dumps(wire))
            replies[call_id] = json.loads(await ws.recv())
        await ws.close(code=1000)

    state = _serve_and_run(tmp_path, server_handler, delegation_policy=_policy())
    assert state["status"] == "closed"
    assert replies["e1"]["type"] == "delegate_error"
    assert "unknown skill" in replies["e1"]["error"]
    assert replies["e2"]["type"] == "delegate_error"
    assert "mismatch" in replies["e2"]["error"]
    assert replies["e3"]["type"] == "delegate_error"
    assert "forbidden" in replies["e3"]["error"]
    assert replies["e4"]["type"] == "delegate_error"
    assert "non-empty" in replies["e4"]["error"]
    assert replies["e5"]["type"] == "delegate_error"
    assert "unexpected" in replies["e5"]["error"]
    assert all(r["call_id"] == cid for cid, r in replies.items())


def test_ws_delegate_skill_idempotent_replay(tmp_path):
    counter = {"n": 0}

    def _counting(spec, params, root):
        counter["n"] += 1
        return {"ok": True, "n": counter["n"]}

    policy = _policy(skills={"count": _spec(name="count")})
    handler = EdgeDelegationHandler(policy, tmp_path)
    handler.register_handler("count", _counting)
    replies: list = []

    async def server_handler(ws):
        await ws.recv()  # hello
        frame = {
            "type": "delegate_skill",
            "call_id": "dup-1",
            "task_id": "t1",
            "skill": "count",
            "sha256": _SHA,
            "params": {},
        }
        await ws.send(json.dumps(frame))
        replies.append(json.loads(await ws.recv()))
        await ws.send(json.dumps(dict(frame)))  # reconnect-style replay
        replies.append(json.loads(await ws.recv()))
        await ws.close(code=1000)

    state = _serve_and_run(tmp_path, server_handler, delegation_handler=handler)
    assert state["status"] == "closed"
    assert counter["n"] == 1  # replay answered from the idem ring
    assert replies[0] == replies[1]
    assert replies[0]["type"] == "delegate_result"
    assert replies[0]["result"]["n"] == 1
