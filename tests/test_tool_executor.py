# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for ToolExecutor abstraction, read-only batching, and apply_patch conflict retries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from botex.core.tool_executor import READ_ONLY_TOOLS, LocalToolExecutor, ToolExecutor
import botex.engine as engine


def _make_tool_call(call_id: str, name: str, args: Dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _make_response(content: str = "", tool_calls: List[Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason="tool_calls" if tool_calls else "stop",
            )
        ],
        usage=None,
    )


class _ScriptedClient:
    def __init__(self, responses: List[Any], default_content: str = "STATUS: DONE\nCompleted"):
        self.responses = list(responses)
        self.default_content = default_content
        self.calls: List[Dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            return _make_response(self.default_content)
        return self.responses.pop(0)


class _ProbeExecutor(ToolExecutor):
    """Async ToolExecutor measuring concurrency and recording batch/execute invocations."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.order: List[str] = []
        self.batch_calls: List[List[str]] = []
        self.patch_base_shas: List[Any] = []
        self.patch_attempts = 0
        self.conflict_limit = 2

    async def execute_batch(self, calls, workspace_root: Path, task_id: str = "default"):
        self.batch_calls.append([str(c.get("tool_name") or "") for c in calls])
        return await super().execute_batch(calls, workspace_root, task_id=task_id)

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        workspace_root: Path,
        task_id: str = "default",
        call_id: str = "",
    ) -> Dict[str, Any]:
        del task_id, call_id
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.order.append(f"start:{tool_name}")
        try:
            await asyncio.sleep(0.015)
            if tool_name == "read_file_lines":
                return {
                    "ok": True,
                    "path": args.get("path", ""),
                    "start": 1,
                    "end": 10,
                    "total_lines": 10,
                    "content": "def hello():\n    return 1\n",
                    "file_sha256": "sha-v1",
                }
            if tool_name == "get_file_outline":
                return {
                    "ok": True,
                    "path": args.get("path", ""),
                    "outline": "def hello() -> line 1",
                }
            if tool_name == "apply_patch":
                self.patch_attempts += 1
                self.patch_base_shas.append(args.get("base_sha256"))
                if self.patch_attempts <= self.conflict_limit:
                    return {
                        "ok": False,
                        "status": "conflict",
                        "error": "Concurrent edit conflict",
                        "current_sha256": f"sha-v{self.patch_attempts + 1}",
                    }
                return {
                    "ok": True,
                    "path": args.get("path", ""),
                    "lines_added": 1,
                    "lines_removed": 1,
                    "file_sha256": f"sha-v{self.patch_attempts + 1}",
                }
            return {"ok": True}
        finally:
            self.order.append(f"end:{tool_name}")
            self.active -= 1


@pytest.mark.anyio
async def test_execute_batch_concurrent_for_readonly_and_sequential_for_mutations(tmp_path: Path):
    ex = _ProbeExecutor()
    assert {"read_file_lines", "get_file_outline", "list_dir"}.issubset(READ_ONLY_TOOLS)

    ro_results = await ex.execute_batch(
        [
            {"tool_name": "read_file_lines", "args": {"path": "a.py"}, "call_id": "1"},
            {"tool_name": "get_file_outline", "args": {"path": "a.py"}, "call_id": "2"},
        ],
        tmp_path,
    )
    assert len(ro_results) == 2
    assert ex.max_active == 2

    ex.max_active = 0
    ex.order.clear()
    mixed_results = await ex.execute_batch(
        [
            {"tool_name": "read_file_lines", "args": {"path": "a.py"}, "call_id": "3"},
            {"tool_name": "apply_patch", "args": {"path": "a.py"}, "call_id": "4"},
        ],
        tmp_path,
    )
    assert len(mixed_results) == 2
    assert ex.max_active == 1
    assert ex.order == [
        "start:read_file_lines",
        "end:read_file_lines",
        "start:apply_patch",
        "end:apply_patch",
    ]


@pytest.mark.anyio
async def test_local_tool_executor_forwards_base_sha256_conditionally(tmp_path: Path):
    seen_with_sha: List[Any] = []

    def patch_with_sha(
        path, search_block, replace_block, root, *, task_id="default", base_sha256=None
    ):
        seen_with_sha.append((path, task_id, base_sha256))
        return {"ok": True}

    def legacy_patch(path, search_block, replace_block, root, *, task_id="default"):
        return {"ok": True, "legacy": True}

    ex1 = LocalToolExecutor(handlers={"apply_patch": patch_with_sha})
    res1 = await ex1.execute(
        "apply_patch",
        {"path": "a.py", "search_block": "x", "replace_block": "y", "base_sha256": "abc123"},
        tmp_path,
        task_id="t1",
    )
    assert res1["ok"] is True
    assert seen_with_sha == [("a.py", "t1", "abc123")]

    ex2 = LocalToolExecutor(handlers={"apply_patch": legacy_patch})
    res2 = await ex2.execute(
        "apply_patch",
        {"path": "a.py", "search_block": "x", "replace_block": "y", "base_sha256": "abc123"},
        tmp_path,
        task_id="t1",
    )
    assert res2 == {"ok": True, "legacy": True}


@pytest.mark.anyio
async def test_local_tool_executor_rejects_unknown_tool_and_empty_batch(tmp_path: Path):
    ex = LocalToolExecutor()
    res = await ex.execute("does_not_exist", {}, tmp_path)
    assert res == {"ok": False, "error": "Unknown tool 'does_not_exist'."}
    assert await ex.execute_batch([], tmp_path) == []


class _ShortBatchExecutor(_ProbeExecutor):
    """Contract violation: drops every batch result."""

    async def execute_batch(self, calls, workspace_root: Path, task_id: str = "default"):
        await super().execute_batch(calls, workspace_root, task_id=task_id)
        return []


@pytest.mark.anyio
async def test_engine_pads_malformed_batch_results(tmp_path: Path):
    (tmp_path / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    ex = _ShortBatchExecutor()

    client = _ScriptedClient(
        [
            _make_response(
                tool_calls=[
                    _make_tool_call(
                        "c1", "read_file_lines", {"path": "app.py", "start": 1, "end": 10}
                    ),
                    _make_tool_call("c2", "get_file_outline", {"path": "app.py"}),
                ]
            ),
            _make_response("STATUS: DONE\nRecovered"),
        ]
    )

    with (
        patch.object(engine, "resolve_model_candidates", return_value=["test-model"]),
        patch.object(engine, "build_async_client", return_value=client),
        patch.object(engine, "check_model_price", return_value=(True, "")),
        patch.object(engine, "check_model_tools", return_value=(True, "")),
        patch.object(engine, "check_budget_limit", return_value=(False, 0.0)),
        patch.object(engine, "log_run", return_value={"cost_usd": 0.0}),
        patch.object(engine, "print_run_summary", return_value=None),
    ):
        res = await engine.run_botex_task(
            task="Read app.py",
            workspace_dir=str(tmp_path),
            provider="openrouter",
            api_key="test-or-key",
            mode="edit",
            tool_executor=ex,
        )

    assert res["status"] == "DONE"
    # Every tool_call_id still got a tool message for the next provider call.
    answered = {
        m.get("tool_call_id") for m in client.calls[1]["messages"] if m.get("role") == "tool"
    }
    assert {"c1", "c2"} <= answered


class _DriftingExecutor(LocalToolExecutor):
    """Simulates a concurrent writer drifting the file once before the first patch."""

    def __init__(self) -> None:
        super().__init__()
        self.drifted = False

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        workspace_root: Path,
        task_id: str = "default",
        call_id: str = "",
    ) -> Dict[str, Any]:
        if tool_name == "apply_patch" and not self.drifted:
            self.drifted = True
            target = workspace_root / str(args.get("path", ""))
            if target.is_file():
                target.write_text(
                    target.read_text(encoding="utf-8") + "# drifted\n", encoding="utf-8"
                )
        return await super().execute(
            tool_name, args, workspace_root, task_id=task_id, call_id=call_id
        )


@pytest.mark.anyio
async def test_engine_real_executor_conflict_retry_recovers_after_drift(tmp_path: Path):
    """Full seam: real read sha -> external drift -> conflict -> retry against
    the reported digest -> patch lands on the drifted content."""
    (tmp_path / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    ex = _DriftingExecutor()

    client = _ScriptedClient(
        [
            _make_response(tool_calls=[_make_tool_call("c1", "read_file", {"path": "app.py"})]),
            _make_response(
                tool_calls=[
                    _make_tool_call(
                        "c2",
                        "apply_patch",
                        {"path": "app.py", "search_block": "y = 2", "replace_block": "y = 3"},
                    )
                ]
            ),
            _make_response("STATUS: DONE\nPatched"),
        ]
    )

    with (
        patch.object(engine, "resolve_model_candidates", return_value=["test-model"]),
        patch.object(engine, "build_async_client", return_value=client),
        patch.object(engine, "check_model_price", return_value=(True, "")),
        patch.object(engine, "check_model_tools", return_value=(True, "")),
        patch.object(engine, "check_budget_limit", return_value=(False, 0.0)),
        patch.object(engine, "log_run", return_value={"cost_usd": 0.0}),
        patch.object(engine, "print_run_summary", return_value=None),
    ):
        res = await engine.run_botex_task(
            task="Bump y in app.py",
            workspace_dir=str(tmp_path),
            provider="openrouter",
            api_key="test-or-key",
            mode="edit",
            tool_executor=ex,
        )

    assert res["status"] == "DONE"
    final = (tmp_path / "app.py").read_text(encoding="utf-8")
    assert "y = 3" in final
    assert "# drifted" in final


@pytest.mark.anyio
async def test_engine_uses_injected_executor_batches_reads_and_retries_conflict(tmp_path: Path):
    (tmp_path / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    ex = _ProbeExecutor()
    ex.conflict_limit = 2

    client = _ScriptedClient(
        [
            _make_response(
                tool_calls=[
                    _make_tool_call(
                        "c1", "read_file_lines", {"path": "app.py", "start": 1, "end": 10}
                    ),
                    _make_tool_call("c2", "get_file_outline", {"path": "app.py"}),
                ]
            ),
            _make_response(
                tool_calls=[
                    _make_tool_call(
                        "c3",
                        "apply_patch",
                        {
                            "path": "app.py",
                            "search_block": "return 1",
                            "replace_block": "return 2",
                        },
                    )
                ]
            ),
            _make_response("STATUS: DONE\nPatched successfully"),
        ]
    )

    with (
        patch.object(engine, "resolve_model_candidates", return_value=["test-model"]),
        patch.object(engine, "build_async_client", return_value=client),
        patch.object(engine, "check_model_price", return_value=(True, "")),
        patch.object(engine, "check_model_tools", return_value=(True, "")),
        patch.object(engine, "check_budget_limit", return_value=(False, 0.0)),
        patch.object(engine, "log_run", return_value={"cost_usd": 0.0}),
        patch.object(engine, "print_run_summary", return_value=None),
    ):
        res = await engine.run_botex_task(
            task="Update hello return value in app.py",
            workspace_dir=str(tmp_path),
            provider="openrouter",
            api_key="test-or-key",
            mode="edit",
            tool_executor=ex,
        )

    assert res["status"] == "DONE"
    assert ["read_file_lines", "get_file_outline"] in ex.batch_calls
    assert ex.patch_attempts == 3
    assert ex.patch_base_shas == ["sha-v1", "sha-v2", "sha-v3"]


@pytest.mark.anyio
async def test_engine_stops_conflict_retries_after_three_attempts(tmp_path: Path):
    (tmp_path / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    ex = _ProbeExecutor()
    ex.conflict_limit = 99  # always conflict

    client = _ScriptedClient(
        [
            _make_response(
                tool_calls=[
                    _make_tool_call(
                        "c1", "read_file_lines", {"path": "app.py", "start": 1, "end": 10}
                    ),
                ]
            ),
            _make_response(
                tool_calls=[
                    _make_tool_call(
                        "c2",
                        "apply_patch",
                        {
                            "path": "app.py",
                            "search_block": "return 1",
                            "replace_block": "return 2",
                        },
                    )
                ]
            ),
            _make_response("STATUS: BLOCKED\nRepeated conflict after retries"),
        ],
        default_content="STATUS: BLOCKED\nRepeated conflict after retries",
    )

    with (
        patch.object(engine, "resolve_model_candidates", return_value=["test-model"]),
        patch.object(engine, "build_async_client", return_value=client),
        patch.object(engine, "check_model_price", return_value=(True, "")),
        patch.object(engine, "check_model_tools", return_value=(True, "")),
        patch.object(engine, "check_budget_limit", return_value=(False, 0.0)),
        patch.object(engine, "log_run", return_value={"cost_usd": 0.0}),
        patch.object(engine, "print_run_summary", return_value=None),
    ):
        res = await engine.run_botex_task(
            task="Attempt conflicting edit",
            workspace_dir=str(tmp_path),
            provider="openrouter",
            api_key="test-or-key",
            mode="edit",
            tool_executor=ex,
        )

    assert ex.patch_attempts == 4  # 1 initial + 3 retries
    assert ex.patch_base_shas == ["sha-v1", "sha-v2", "sha-v3", "sha-v4"]
    tool_msgs = [
        m
        for m in client.calls[2]["messages"]
        if m.get("role") == "tool" and m.get("tool_call_id") == "c2"
    ]
    assert len(tool_msgs) == 1
    assert '"status": "conflict"' in tool_msgs[0]["content"]
    assert res["ok"] is False
    assert res["status"] in ("BLOCKED", "INCOMPLETE", "FAILED", "UNSUPPORTED", "EMPTY_RESPONSE")
