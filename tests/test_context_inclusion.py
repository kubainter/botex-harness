# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Tests for the Context Inclusion Layer
=====================================
Verifies:
1. OutputNormalizer normalizes heterogeneous agent payloads to CanonicalAgentTurn.
2. DeterministicSignalGate collapses passed tests, pinpoints failure tracebacks,
   prunes diffs to an AST manifest, and de-duplicates observations (Jaccard).
3. ContextKnapsack packs state to a byte budget and spills overflow to scratchpad.
4. InFlightTaskState.ingest_swarm_payload folds turns into state with POSIX paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from botex.context_inclusion import (
    ContextKnapsack,
    DeterministicSignalGate,
    OutputNormalizer,
)
from botex.state import CanonicalAgentTurn, InFlightTaskState


# ==============================================================================
# OutputNormalizer
# ==============================================================================


class TestOutputNormalizer:
    def test_normalizes_devin_payload(self):
        payload = {
            "message": "Devin did work",
            "actions": [{"name": "edit", "path": "botex/state.py"}],
            "status": "DONE",
            "modified_files": ["botex/state.py"],
            "memory_keys": ["mem_1"],
        }
        turn = OutputNormalizer().normalize(payload, "devin")
        assert isinstance(turn, CanonicalAgentTurn)
        assert turn.agent_type == "devin"
        assert turn.content == "Devin did work"
        assert turn.status == "done"
        assert turn.tool_calls == [{"name": "edit", "path": "botex/state.py"}]
        assert turn.mutated_files == ["botex/state.py"]
        assert turn.metadata["shared_memory_keys"] == ["mem_1"]

    def test_normalizes_zcode_payload(self):
        payload = {
            "response": "ZCode output",
            "result": "ignored because response wins",
            "state": "OK",
            "changed_files": ["botex/context_inclusion.py"],
        }
        turn = OutputNormalizer().normalize(payload, "zcode")
        assert turn.content == "ZCode output"
        assert turn.status == "ok"
        assert turn.mutated_files == ["botex/context_inclusion.py"]

    def test_normalizes_botex_payload(self):
        payload = {"content": "BoteX content", "finish_reason": "stop"}
        turn = OutputNormalizer().normalize(payload, "botex")
        assert turn.content == "BoteX content"
        assert turn.status == "stop"

    def test_normalizes_plain_string(self):
        turn = OutputNormalizer().normalize("raw text", "botex")
        assert turn.content == "raw text"
        assert turn.agent_type == "botex"
        assert turn.role == "assistant"

    def test_windows_paths_are_converted_to_posix(self):
        payload = {"content": "x", "files": ["C:\\Users\\kubai\\repo\\botex\\state.py"]}
        turn = OutputNormalizer().normalize(payload, "botex")
        assert turn.mutated_files == ["Users/kubai/repo/botex/state.py"]
        assert all("\\" not in p for p in turn.mutated_files)

    def test_blocking_reason_captured(self):
        payload = {"content": "x", "blocking_reason": "awaiting approval"}
        turn = OutputNormalizer().normalize(payload, "botex")
        assert turn.metadata["blocking_reason"] == "awaiting approval"


# ==============================================================================
# DeterministicSignalGate - collapse passed tests
# ==============================================================================


class TestCollapsePassedTests:
    def test_collapses_consecutive_passes(self):
        text = "\n".join(
            [
                "test_a PASSED",
                "test_b PASSED",
                "test_c OK",
                "test_d FAILED",
            ]
        )
        result = DeterministicSignalGate.collapse_passed_tests(text)
        assert "[collapsed: 3 passed test(s)]" in result
        assert "test_d FAILED" in result
        assert "test_a PASSED" not in result

    def test_no_passes_unchanged(self):
        text = "line one\nline two"
        assert DeterministicSignalGate.collapse_passed_tests(text) == text

    def test_empty_input(self):
        assert DeterministicSignalGate.collapse_passed_tests("") == ""


# ==============================================================================
# DeterministicSignalGate - pinpoint failure trace
# ==============================================================================


class TestPinpointFailureTrace:
    def test_keeps_header_and_last_two_frames(self):
        text = "\n".join(
            [
                "Traceback (most recent call last):",
                '  File "a.py", line 1, in <module>',
                "    foo()",
                '  File "b.py", line 2, in foo',
                "    bar()",
                '  File "c.py", line 3, in bar',
                "    raise ValueError('boom')",
                "ValueError: boom",
            ]
        )
        result = DeterministicSignalGate.pinpoint_failure_trace(text)
        assert "ValueError: boom" in result
        assert 'File "b.py"' in result
        assert 'File "c.py"' in result
        assert 'File "a.py"' not in result

    def test_empty_input(self):
        assert DeterministicSignalGate.pinpoint_failure_trace("") == ""


# ==============================================================================
# DeterministicSignalGate - AST diff pruning
# ==============================================================================


class TestPruneDiffToAstManifest:
    def test_extracts_changed_symbols(self):
        diff = "\n".join(
            [
                "--- a/botex/state.py",
                "+++ b/botex/state.py",
                "@@ -1,3 +1,4 @@",
                "+def new_helper(x):",
                "+    return x",
                "-def old_helper():",
                " class MyClass:",
            ]
        )
        manifest = DeterministicSignalGate.prune_diff_to_ast_manifest(diff)
        assert "new_helper" in manifest["symbols"]
        assert "old_helper" in manifest["symbols"]
        assert manifest["added"] >= 2
        assert manifest["removed"] >= 1

    def test_empty_diff(self):
        manifest = DeterministicSignalGate.prune_diff_to_ast_manifest("")
        assert manifest == {"symbols": [], "added": 0, "removed": 0}

    def test_hunk_header_symbol(self):
        diff = "@@ -10,2 +10,3 @@ def target_function():"
        manifest = DeterministicSignalGate.prune_diff_to_ast_manifest(diff)
        assert "target_function" in manifest["symbols"]


# ==============================================================================
# DeterministicSignalGate - deduplicate observations
# ==============================================================================


class TestDeduplicateObservations:
    def test_removes_near_duplicates(self):
        obs = [
            "the quick brown fox jumps over the lazy dog",
            "the quick brown fox jumps over the lazy dog",
            "completely different observation about tests",
        ]
        result = DeterministicSignalGate.deduplicate_observations(obs)
        assert len(result) == 2

    def test_keeps_distinct(self):
        obs = [
            "alpha beta gamma",
            "one two three",
        ]
        result = DeterministicSignalGate.deduplicate_observations(obs)
        assert len(result) == 2

    def test_empty(self):
        assert DeterministicSignalGate.deduplicate_observations([]) == []


# ==============================================================================
# ContextKnapsack
# ==============================================================================


class TestContextKnapsack:
    def test_packs_within_budget(self, tmp_path: Path):
        knapsack = ContextKnapsack(budget_bytes=64)
        result = knapsack.pack(["short one", "short two"], base_dir=tmp_path)
        assert result["overflow_pointer"] is None
        assert "short one" in result["packed"]
        assert result["used_bytes"] <= 64

    def test_overflow_written_to_scratchpad(self, tmp_path: Path):
        knapsack = ContextKnapsack(budget_bytes=32)
        big = "overflow payload content " * 10
        result = knapsack.pack([big], scratch_id="ctx1", base_dir=tmp_path)
        assert result["overflow_pointer"] is not None
        scratch_file = tmp_path / ".botex" / "scratchpad" / "ctx1.log"
        assert scratch_file.exists()
        assert scratch_file.read_text(encoding="utf-8") == big
        assert ".botex/scratchpad/ctx1.log" in result["overflow_pointer"]
        assert "\\" not in result["overflow_pointer"]

    def test_default_budget_is_4096(self):
        knapsack = ContextKnapsack()
        assert knapsack.budget_bytes == 4096


# ==============================================================================
# InFlightTaskState.ingest_swarm_payload
# ==============================================================================


class TestIngestSwarmPayload:
    def test_ingests_and_folds_state(self):
        state = InFlightTaskState(task_id="t1", goal="do work")
        payload = {
            "content": "did stuff",
            "status": "done",
            "mutated_files": ["botex/state.py"],
            "shared_memory_keys": ["mem_x"],
            "blocking_reason": "waiting",
        }
        turn = state.ingest_swarm_payload(payload, "devin")
        assert isinstance(turn, CanonicalAgentTurn)
        assert len(state.agent_turns) == 1
        assert "botex/state.py" in state.mutated_files
        assert "botex/state.py" in state.inspected_files
        assert "mem_x" in state.shared_memory_keys
        assert state.active_blocking_reason == "waiting"

    def test_deduplicates_repeated_files(self):
        state = InFlightTaskState()
        payload = {"content": "x", "mutated_files": ["botex/state.py"]}
        state.ingest_swarm_payload(payload, "botex")
        state.ingest_swarm_payload(payload, "botex")
        assert state.mutated_files.count("botex/state.py") == 1
        assert len(state.agent_turns) == 2

    def test_roundtrip_serialization(self):
        state = InFlightTaskState(task_id="t2")
        state.ingest_swarm_payload({"content": "hello", "mutated_files": ["a/b.py"]}, "zcode")
        data = state.to_dict()
        restored = InFlightTaskState.from_dict(data)
        assert len(restored.agent_turns) == 1
        assert restored.agent_turns[0].content == "hello"
        assert restored.mutated_files == ["a/b.py"]
