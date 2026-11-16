# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Integration tests for SpiralTracker redirect nudges, abort guard, and Edge envfacts wiring."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import botex.core.engine as engine
from botex import memory
from botex.core import blackbox


def _resp(content=None, tool_calls=None, finish="stop"):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason=finish)],
        usage=SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=1,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
    )


def _tcall(call_id: str, name: str, args: dict):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _install_common_engine_mocks(monkeypatch):
    captured_calls = []

    class FakeClient:
        def __init__(self, responses):
            self._responses = list(responses)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **kwargs):
            captured_calls.append(
                {
                    "messages": [dict(m) for m in kwargs.get("messages", [])],
                    "kwargs": kwargs,
                }
            )
            return self._responses.pop(0)

    def _make_factory(responses):
        client = FakeClient(responses)
        monkeypatch.setattr(engine, "AsyncOpenAI", lambda **_: client)
        return client

    monkeypatch.setattr(engine, "resolve_model_candidates", lambda *a, **k: ["test-model"])
    monkeypatch.setattr(engine, "check_model_price", lambda *a, **k: (True, ""))
    monkeypatch.setattr(engine, "check_model_tools", lambda *a, **k: (True, ""))
    monkeypatch.setattr(engine, "check_budget_limit", lambda *a, **k: (False, 0.0))
    monkeypatch.setattr(engine, "log_run", lambda **kw: {"cost_usd": 0.0})
    monkeypatch.setattr(engine, "print_run_summary", lambda *a, **k: None)

    orig_load_config = engine.load_config
    monkeypatch.setattr(
        engine,
        "load_config",
        lambda reload=False: {
            **orig_load_config(reload),
            "exec": {**orig_load_config().get("exec", {}), "enabled": True},
        },
    )
    return _make_factory, captured_calls


def test_alternating_patch_and_failing_command_spiral_nudges_then_rolls_back(tmp_path, monkeypatch):
    """Alternating valid patches + repeated command failures trigger a one-shot redirect nudge
    before rolling back with STAGNANT_ROLLBACK and logging LOOP_01 to BlackBox."""
    penal_db = tmp_path / "penal_vault.db"
    monkeypatch.setenv("BOTEX_PENAL_DB_PATH", str(penal_db))

    ws = tmp_path / "ws"
    ws.mkdir()
    app_file = ws / "app.py"
    app_file.write_text("VAL = 1\n", encoding="utf-8")

    make_client, captured_calls = _install_common_engine_mocks(monkeypatch)

    async def fake_run_command(command, workspace_root, **kwargs):
        return {
            "ok": False,
            "exit_code": 1,
            "command": "pytest",
            "stdout": "FAILED tests/test_app.py::test_val - AssertionError: 1 != 2",
        }

    monkeypatch.setattr(engine, "run_command", fake_run_command)

    responses = []
    for step in range(1, 6):
        responses.append(
            _resp(
                tool_calls=[
                    _tcall(
                        f"p{step}",
                        "apply_patch",
                        {
                            "path": "app.py",
                            "search_block": f"VAL = {step}",
                            "replace_block": f"VAL = {step + 1}",
                        },
                    ),
                    _tcall(f"c{step}", "run_command", {"command": "pytest tests/"}),
                ]
            )
        )
    responses.append(_resp("STATUS: DONE\ndone"))
    make_client(responses)

    res = asyncio.run(
        engine.run_botex_task(
            task="Fix VAL in app.py and pass pytest",
            workspace_dir=str(ws),
            model="test-model",
            provider="openrouter",
            api_key="x",
            mode="full",
            allow_exec=True,
            require_read_before_write=False,
            max_turns=10,
        )
    )

    assert res["status"] == "STAGNANT_ROLLBACK"
    assert "repeated command/test failure spiral" in res["message"]
    assert res["rollback_verified"] is True
    assert app_file.read_text(encoding="utf-8") == "VAL = 1\n"

    nudge_msgs = [
        m["content"]
        for call in captured_calls
        for m in call["messages"]
        if m.get("role") == "user" and "Process check (automated):" in str(m.get("content", ""))
    ]
    assert len(nudge_msgs) >= 1
    # Prompt-injection safety: nudge must contain only counts/path, not raw command output.
    assert "AssertionError" not in nudge_msgs[0]

    sheet = blackbox.get_rap_sheet(workspace_id=ws.as_posix())
    assert sheet["counts_by_code"].get("LOOP_01", 0) >= 1


def test_recovery_after_redirect_nudge_persists_form_repair_envfact(tmp_path, monkeypatch):
    """If the worker pivots after the redirect nudge and finishes green, a FormRepair envfact
    lesson is persisted as a candidate note."""
    ws = tmp_path / "ws_recover"
    ws.mkdir()
    g_dir = tmp_path / "global"
    g_dir.mkdir()
    app_file = ws / "app.py"
    app_file.write_text("VAL = 1\n", encoding="utf-8")

    make_client, captured_calls = _install_common_engine_mocks(monkeypatch)
    pytest_calls = {"n": 0}

    async def fake_run_command(command, workspace_root, **kwargs):
        cmd = str(command)
        if cmd.startswith("python -m pytest"):
            return {"ok": False, "error": "Python module execution is denied by exec policy."}
        pytest_calls["n"] += 1
        if pytest_calls["n"] <= 2:
            return {
                "ok": False,
                "exit_code": 1,
                "command": "pytest",
                "stdout": "FAILED test_app - AssertionError: wrong value",
            }
        return {"ok": True, "exit_code": 0, "command": "pytest", "stdout": "1 passed"}

    monkeypatch.setattr(engine, "run_command", fake_run_command)

    responses = [
        _resp(
            tool_calls=[
                _tcall(
                    "p1",
                    "apply_patch",
                    {"path": "app.py", "search_block": "VAL = 1", "replace_block": "VAL = 2"},
                ),
                _tcall("c1", "run_command", {"command": "python -m pytest tests/"}),
            ]
        ),
        _resp(
            tool_calls=[
                _tcall(
                    "p2",
                    "apply_patch",
                    {"path": "app.py", "search_block": "VAL = 2", "replace_block": "VAL = 3"},
                ),
                _tcall("c2", "run_command", {"command": "pytest tests/"}),
            ]
        ),
        _resp(
            tool_calls=[
                _tcall(
                    "p3",
                    "apply_patch",
                    {"path": "app.py", "search_block": "VAL = 3", "replace_block": "VAL = 4"},
                ),
                _tcall("c3", "run_command", {"command": "pytest tests/"}),
            ]
        ),
        _resp(
            tool_calls=[
                _tcall(
                    "p4",
                    "apply_patch",
                    {"path": "app.py", "search_block": "VAL = 4", "replace_block": "VAL = 42"},
                ),
                _tcall("c4", "run_command", {"command": "pytest tests/"}),
            ]
        ),
        _resp("STATUS: DONE\nTests now pass."),
    ]
    make_client(responses)

    try:
        with (
            patch("botex.memory.get_global_memory_dir", return_value=g_dir),
            patch("botex.memory.get_swarm_memory_dir", return_value=None),
        ):
            res = asyncio.run(
                engine.run_botex_task(
                    task="Fix app.py and run pytest",
                    workspace_dir=str(ws),
                    model="test-model",
                    provider="openrouter",
                    api_key="x",
                    mode="full",
                    allow_exec=True,
                    require_read_before_write=False,
                    max_turns=10,
                )
            )
            assert res["status"] == "DONE"
            assert res["ok"] is True

            # Verify redirect nudge was delivered before recovery turn 4.
            turn4_msgs = captured_calls[3]["messages"]
            assert any(
                m.get("role") == "user"
                and "Process check (automated):" in str(m.get("content", ""))
                for m in turn4_msgs
            )

            found = memory.search_memory(
                workspace_dir=str(ws),
                query="",
                kind="lesson",
                status="all",
                scope="local",
            )
            memories = found.get("memories", [])
            envfacts = [m for m in memories if "envfact" in (m.get("tags") or [])]
            assert len(envfacts) >= 1
            body = str(envfacts[0].get("content") or "")
            assert "python -m pytest" in body
            assert "pytest" in body
    finally:
        memory.reset_memory_index_cache()


def test_edge_bridge_executor_wires_edge_sub_and_workspace_id_into_lessons(tmp_path, monkeypatch):
    """Edge Bridge execution scopes lesson recall and extracted envfacts to global edge:/ws: tags."""
    ws = tmp_path / "ws_edge"
    ws.mkdir()
    g_dir = tmp_path / "global"
    g_dir.mkdir()

    make_client, captured_calls = _install_common_engine_mocks(monkeypatch)

    class FakeEdgeExecutor:
        def __init__(self):
            self._session = SimpleNamespace(device_id="win1", workspace_id="harness")
            self.calls = 0

        async def execute_batch(self, batch, root_path, task_id=""):
            return [{"ok": True} for _ in batch]

        async def execute(self, tool_name, args, root_path, task_id="", call_id=""):
            if tool_name == "run_command":
                self.calls += 1
                if self.calls == 1:
                    return {
                        "ok": False,
                        "error": "Python module execution is denied by exec policy.",
                    }
                return {"ok": True, "exit_code": 0, "stdout": "1 passed"}
            return {"ok": True}

    edge_exec = FakeEdgeExecutor()
    responses = [
        _resp(tool_calls=[_tcall("c1", "run_command", {"command": "python -m pytest tests/"})]),
        _resp(tool_calls=[_tcall("c2", "run_command", {"command": "pytest tests/"})]),
        _resp("STATUS: DONE\nverified"),
        _resp("STATUS: DONE\nverified"),
    ]
    make_client(responses)

    try:
        with (
            patch("botex.memory.get_global_memory_dir", return_value=g_dir),
            patch("botex.memory.get_swarm_memory_dir", return_value=None),
        ):
            # Seed one active lesson for win1/harness and one for another host (must not bleed).
            memory.save_memory(
                workspace_dir=str(ws),
                title="Edge win1 test lesson",
                content="### Lesson Learned (test)\n- **Effective Resolution:** Run ruff check before pytest.\n",
                kind="lesson",
                tags=["meta-harness", "lesson", "test", "envfact", "edge:win1", "ws:harness"],
                memory_id="lesson_win1_env",
                scope="global",
                project="harness",
                status="active",
                confidence=0.9,
            )
            memory.save_memory(
                workspace_dir=str(ws),
                title="Other host lesson",
                content="### Lesson Learned (test)\n- **Effective Resolution:** Other host command only.\n",
                kind="lesson",
                tags=["meta-harness", "lesson", "test", "envfact", "edge:other", "ws:harness"],
                memory_id="lesson_other_env",
                scope="global",
                project="harness",
                status="active",
                confidence=0.9,
            )

            res = asyncio.run(
                engine.run_botex_task(
                    task="Run unit tests on edge repo",
                    workspace_dir=str(ws),
                    model="test-model",
                    provider="openrouter",
                    api_key="x",
                    mode="full",
                    allow_exec=True,
                    max_turns=6,
                    tool_executor=edge_exec,
                )
            )
            assert res["status"] == "DONE"

            first_user_prompt = str(captured_calls[0]["messages"][1]["content"])
            assert "Run ruff check before pytest." in first_user_prompt
            assert "Other host command only." not in first_user_prompt

            # Distilled envfact must live in global vault with edge:win1 + ws:harness, not local.
            global_found = memory.search_memory(
                workspace_dir=str(ws),
                query="",
                kind="lesson",
                status="all",
                scope="global",
            )
            new_envfacts = [
                m
                for m in global_found.get("memories", [])
                if m.get("id") not in {"lesson_win1_env", "lesson_other_env"}
                and "envfact" in (m.get("tags") or [])
            ]
            assert len(new_envfacts) >= 1
            tags = {str(t).lower() for t in (new_envfacts[0].get("tags") or [])}
            assert {"edge:win1", "ws:harness", "envfact"} <= tags
            assert not (ws / ".botex" / "memory").exists() or not list(
                (ws / ".botex" / "memory").glob("lesson_*.md")
            )
    finally:
        memory.reset_memory_index_cache()
