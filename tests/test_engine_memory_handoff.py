# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Plan 052 Step 3 (Worker 2A): Memory Vault read tools in the engine.

Mocked end-to-end tests proving:
- ``read_memory`` / ``recall_memory`` are advertised alongside
  ``read_scratchpad`` in readonly and edit modes, while the mutating vault
  tools stay hidden.
- A readonly worker dereferences a cited ``[[mem_...]]`` via read_memory and
  completes without touching files.
- The per-attempt recall_memory budget (MAX_WORKER_RECALL_CALLS) routes
  LLM synthesis to the first two calls and rejects the 4th call.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

from botex.core import engine
from botex.core.tool_executor import ToolExecutor

MUTATING_VAULT_TOOLS = {"save_memory", "delete_memory", "patch_memory_section"}
READ_VAULT_TOOLS = {"read_memory", "recall_memory", "read_scratchpad"}


def _response(content: str = "", tool_calls: List[Any] | None = None, finish: str = "stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish,
            )
        ],
        usage=None,
    )


def _tool_call(call_id: str, name: str, args: Dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.responses:
            return self.responses.pop(0)
        return _response("STATUS: DONE\ndone")


class FakeMemoryExecutor(ToolExecutor):
    """Records tool invocations; serves canned vault read results."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        workspace_root,
        task_id: str = "default",
        call_id: str = "",
    ) -> Dict[str, Any]:
        self.calls.append((tool_name, dict(args)))
        if tool_name == "read_memory":
            return {
                "ok": True,
                "id": args.get("memory_id"),
                "content": "<untrusted_memory_content>note body</untrusted_memory_content>",
            }
        if tool_name == "recall_memory":
            return {
                "ok": True,
                "synthesis": "recalled facts with sources",
                "sources": ["mem_a", "mem_b"],
            }
        return {"ok": True}


def _patch_engine(monkeypatch_factory) -> None:
    """Swap engine externals for hermetic doubles (pattern of tests/test_botex.py)."""
    monkeypatch_factory("check_model_price", lambda *a, **k: (True, ""))
    monkeypatch_factory("check_model_tools", lambda *a, **k: (True, ""))
    monkeypatch_factory("check_budget_limit", lambda *a, **k: (False, 0.0))
    monkeypatch_factory("log_run", lambda **kw: {"cost_usd": 0.0})
    monkeypatch_factory("print_run_summary", lambda *a, **k: None)
    monkeypatch_factory("resolve_model_candidates", lambda *a, **k: ["mock-model"])


def _run_task(**kwargs) -> Dict[str, Any]:
    return asyncio.run(engine.run_botex_task(**kwargs))


def _advertised_names(fake: FakeClient) -> set:
    tools = fake.calls[0]["tools"] or []
    return {d["function"]["name"] for d in tools}


def test_vault_read_tools_advertised_and_mutators_hidden():
    original = {
        n: getattr(engine, n)
        for n in (
            "AsyncOpenAI",
            "resolve_model_candidates",
            "check_model_price",
            "check_model_tools",
            "check_budget_limit",
            "log_run",
            "print_run_summary",
        )
    }
    _patch_engine(lambda name, fn: setattr(engine, name, fn))
    try:
        for mode in ("readonly", "edit"):
            with tempfile.TemporaryDirectory() as tmpdir:
                fake = FakeClient([_response("STATUS: DONE\nready")])

                def _client(*a: Any, _c: FakeClient = fake, **kw: Any) -> FakeClient:
                    return _c

                engine.AsyncOpenAI = _client
                result = _run_task(
                    task="probe advertised tools",
                    workspace_dir=tmpdir,
                    model="mock-model",
                    api_key="x",
                    mode=mode,
                    tool_executor=FakeMemoryExecutor(),
                )
                assert result["ok"], result
                names = _advertised_names(fake)
                assert READ_VAULT_TOOLS <= names, (mode, names)
                assert not (MUTATING_VAULT_TOOLS & names), (mode, names)
    finally:
        for name, value in original.items():
            setattr(engine, name, value)


def test_readonly_worker_dereferences_cited_mem_id():
    original = {
        n: getattr(engine, n)
        for n in (
            "AsyncOpenAI",
            "resolve_model_candidates",
            "check_model_price",
            "check_model_tools",
            "check_budget_limit",
            "log_run",
            "print_run_summary",
        )
    }
    _patch_engine(lambda name, fn: setattr(engine, name, fn))
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = FakeClient(
                [
                    _response(
                        tool_calls=[_tool_call("c1", "read_memory", {"memory_id": "mem_deadbeef"})],
                        finish="tool_calls",
                    ),
                    _response("STATUS: DONE\nfetched [[mem_deadbeef]] from the vault"),
                ]
            )
            engine.AsyncOpenAI = lambda **_: fake
            executor = FakeMemoryExecutor()
            result = _run_task(
                task="Summarize the note cited as REFS: [[mem_deadbeef]].",
                workspace_dir=tmpdir,
                model="mock-model",
                api_key="x",
                mode="readonly",
                tool_executor=executor,
            )
            assert result["status"] == "DONE" and result["ok"], result
            assert result["files_touched"] == []
            assert ("read_memory", {"memory_id": "mem_deadbeef"}) in executor.calls
            # The worker saw the untrusted boundary marker in the tool result.
            last_messages = fake.calls[-1]["messages"]
            tool_msgs = [m for m in last_messages if m.get("role") == "tool"]
            assert any("untrusted_memory_content" in m["content"] for m in tool_msgs)
    finally:
        for name, value in original.items():
            setattr(engine, name, value)


def test_recall_budget_synthesis_and_exceeded():
    original = {
        n: getattr(engine, n)
        for n in (
            "AsyncOpenAI",
            "resolve_model_candidates",
            "check_model_price",
            "check_model_tools",
            "check_budget_limit",
            "log_run",
            "print_run_summary",
        )
    }
    _patch_engine(lambda name, fn: setattr(engine, name, fn))
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            recall_calls = [
                _tool_call(f"c{i}", "recall_memory", {"query": f"topic {i}", "project": "swarm"})
                for i in range(1, 5)
            ]
            fake = FakeClient(
                [
                    _response(tool_calls=recall_calls, finish="tool_calls"),
                    _response("STATUS: DONE\nused vault recall"),
                ]
            )
            engine.AsyncOpenAI = lambda **_: fake
            executor = FakeMemoryExecutor()
            result = _run_task(
                task="Research the swarm decision context.",
                workspace_dir=tmpdir,
                model="mock-model",
                api_key="x",
                mode="readonly",
                tool_executor=executor,
            )
            assert result["status"] == "DONE" and result["ok"], result
            recalls = [a for n, a in executor.calls if n == "recall_memory"]
            # Calls 1-2 get LLM synthesis, call 3 falls back to deterministic recall.
            assert [r.get("_use_llm_synthesis") for r in recalls] == [True, True, False]
            # Call 4 never reaches the executor — preset budget rejection instead.
            assert len(recalls) == 3
            last_messages = fake.calls[-1]["messages"]
            tool_msgs = [m for m in last_messages if m.get("role") == "tool"]
            assert any("RECALL_BUDGET_EXCEEDED" in m["content"] for m in tool_msgs)
    finally:
        for name, value in original.items():
            setattr(engine, name, value)
