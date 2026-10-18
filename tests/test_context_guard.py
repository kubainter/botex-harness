# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Context-Guard tests: failure-aware slicing, Unicode safety, Vault offload,
engine history folding, MCP report offload, ephemeral retention."""

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botex.context_guard import (  # noqa: E402
    compact_verification_summary,
    sanitize_and_vault_output,
    smart_slice_output,
)
from botex.engine import prune_tool_history  # noqa: E402
from botex.exec_tools import run_command  # noqa: E402
from botex.memory import (  # noqa: E402
    purge_ephemeral_memories,
    read_memory,
    save_memory,
)


def _pytest_like_output(passed: int = 120) -> str:
    lines = ["============ test session starts ============", "collected 121 items"]
    lines += [f"tests/test_x.py::test_ok_{i} PASSED" for i in range(passed)]
    lines += [
        "=================== FAILURES ===================",
        "_________________ test_broken _________________",
        "    def test_broken():",
        ">       assert compute() == 42",
        "E       AssertionError: assert 41 == 42",
        "tests/test_x.py:77: AssertionError",
        "=========== short test summary info ===========",
        "FAILED tests/test_x.py::test_broken - AssertionError: assert 41 == 42",
        "======== 1 failed, 120 passed in 1.23s ========",
    ]
    return "\n".join(lines)


def test_failure_aware_slicing():
    text = _pytest_like_output()
    out, truncated = smart_slice_output(text, memory_id="mem_exec_deadbeef")
    assert truncated
    assert len(out.encode("utf-8")) <= 3072
    assert "AssertionError: assert 41 == 42" in out
    assert "FAILED tests/test_x.py::test_broken" in out
    assert "1 failed, 120 passed" in out
    assert "test_ok_60 PASSED" not in out  # green noise collapsed/dropped
    assert "mem_exec_deadbeef" in out
    assert "read_memory(memory_id='mem_exec_deadbeef')" in out
    assert "Key failure tracebacks preserved below" in out
    # small text is untouched
    assert smart_slice_output("ok\n") == ("ok\n", False)


def test_failure_block_survives_long_passed_head_and_tail_cut():
    """A rigid tail(35) cut would drop the traceback; failure-aware must not."""
    lines = _pytest_like_output(10).splitlines()
    tb = [f"frame {i}: some_call()" for i in range(60)]
    idx = lines.index("E       AssertionError: assert 41 == 42")
    text = "\n".join(lines[:idx] + tb + lines[idx:])
    out, _ = smart_slice_output(text, tail_lines=5, memory_id="mem_exec_x")
    assert "AssertionError: assert 41 == 42" in out
    assert "FAILED tests/test_x.py::test_broken" in out


def test_unicode_safety():
    pl = "Zażółć gęślą jaźń — zażółcenie 🙂\n" * 400
    out, truncated = smart_slice_output(pl, max_preview_bytes=1500, memory_id="mem_exec_pl")
    assert truncated
    assert "\ufffd" not in out
    out.encode("utf-8")  # must be valid
    assert len(out.encode("utf-8")) <= 1500

    with tempfile.TemporaryDirectory() as tmp:
        py = sys.executable
        code = tmp + "/emit.py"
        Path(code).write_text(
            "import sys\nsys.stdout.buffer.write(('Zażółć gęślą jaźń 🙂\\n' * 3000).encode('utf-8'))\n",
            encoding="utf-8",
        )
        r = asyncio.run(
            run_command(
                f'"{py}" "{code}"',
                Path(tmp),
                allowlist=["python"],
                deny_args=[],
                timeout_s=30,
                max_output_bytes=2500,
            )
        )
        assert r["ok"]
        assert "\ufffd" not in r["stdout"]
        assert "Zażółć" in r["stdout"]


def test_vault_offload_and_readback():
    with tempfile.TemporaryDirectory() as tmp:
        sk_pref = "".join(["s", "k", "-"])
        dummy_api_key = sk_pref + "abcdefghij1234567890abcdefghij"
        big = f"api_token={dummy_api_key}\n" + "line of log output\n" * 800 + "Error: boom\n"
        preview, mem_id = sanitize_and_vault_output(big, "run_command", tmp, task_id="t1")
        assert mem_id and mem_id.startswith("mem_exec_")
        assert dummy_api_key not in preview
        assert mem_id in preview
        got = read_memory(tmp, mem_id)
        assert got["ok"]
        assert dummy_api_key not in got["content"]
        assert "<untrusted_command_output" in got["content"]
        assert "<untrusted_memory_content" in got["content"]
        assert "Error: boom" in got["content"]
        assert "ephemeral" in got["metadata"]["tags"]
        assert "context_guard" in got["metadata"]["tags"]
        assert got["metadata"]["title"] == "Context-Guard Exec Log"
        # below threshold: untouched, no vault entry
        small, none_id = sanitize_and_vault_output("hello", "run_command", tmp)
        assert small == "hello" and none_id is None
        # disabled -> pass-through (masked)
        same, nid = sanitize_and_vault_output(big, "x", tmp, guard_cfg={"enabled": False})
        assert nid is None and dummy_api_key not in same and len(same) > 10000


def test_run_command_exposes_vault_id():
    with tempfile.TemporaryDirectory() as tmp:
        py = sys.executable
        script = Path(tmp) / "noisy.py"
        script.write_text("for i in range(800):\n    print('noise line', i)\n", encoding="utf-8")
        r = asyncio.run(
            run_command(
                f'"{py}" "{script}"',
                Path(tmp),
                allowlist=["python"],
                deny_args=[],
                timeout_s=30,
                max_output_bytes=20000,
            )
        )
        assert r["ok"] and r["vault_memory_id"] in r["stdout"]
        full = read_memory(tmp, r["vault_memory_id"])["content"]
        assert "noise line 799" in full and "noise line 0" in full


def test_purge_ephemeral_only():
    with tempfile.TemporaryDirectory() as tmp:
        save_memory(tmp, "eph", "x", tags=["ephemeral"], memory_id="mem_old_eph")
        save_memory(tmp, "decision", "keep", kind="decision", tags=["adr"], memory_id="mem_old_adr")
        save_memory(tmp, "fresh", "y", tags=["ephemeral"], memory_id="mem_fresh_eph")
        mem_dir = Path(tmp) / ".botex" / "memory"
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        for name in ("mem_old_eph", "mem_old_adr"):
            p = mem_dir / f"{name}.md"
            txt = p.read_text(encoding="utf-8")
            ts_line = next(ln for ln in txt.splitlines() if ln.startswith("timestamp:"))
            p.write_text(txt.replace(ts_line, f"timestamp: {json.dumps(old)}"), encoding="utf-8")
        assert purge_ephemeral_memories(tmp, max_age_days=3) == 1
        assert not (mem_dir / "mem_old_eph.md").exists()
        assert (mem_dir / "mem_old_adr.md").exists()
        assert (mem_dir / "mem_fresh_eph.md").exists()


def _tool_exchange(i: int, name: str, args: dict, result: dict):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"c{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": json.dumps(result, ensure_ascii=False),
        },
    ]


def test_engine_history_folding():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        for n in range(6):
            (root / f"f{n}.py").write_text("x = 1\n", encoding="utf-8")
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
        for i in range(3):
            msgs += _tool_exchange(
                i,
                "run_command",
                {"command": "pytest -q"},
                {
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "BIG" * 500,
                    "vault_memory_id": f"mem_exec_{i}",
                },
            )
        prune_tool_history(msgs, "", root=root)
        tools = [m for m in msgs if m["role"] == "tool"]
        assert tools[0]["content"] == (
            "[Command 'pytest -q' exit code 1 — output pruned to preserve "
            "context window. Vault ID: mem_exec_0]"
        )
        assert tools[1]["content"].startswith("[Command 'pytest -q' exit code 1")
        assert "BIG" in tools[2]["content"]  # newest keeps its content

        # readonly: >4 unique reads fold oldest
        rmsgs = [{"role": "user", "content": "go"}]
        for n in range(6):
            rmsgs += _tool_exchange(
                10 + n,
                "read_file",
                {"path": f"./f{n}.py" if n == 0 else f"f{n}.py"},
                {"ok": True, "path": f"f{n}.py", "content": "CODE" * 100, "total_lines": 1},
            )
        prune_tool_history(rmsgs, "", root=root, readonly=False)
        assert all("CODE" in m["content"] for m in rmsgs if m["role"] == "tool")
        prune_tool_history(rmsgs, "", root=root, readonly=True)
        reads = [m["content"] for m in rmsgs if m["role"] == "tool"]
        assert "pruned" in reads[0] and "pruned" in reads[1]
        assert all("CODE" in c for c in reads[2:])

        # target pruning with path normalization (./f5.py == f5.py)
        prune_tool_history(rmsgs, "./f5.py", root=root)
        assert "pruned" in [m["content"] for m in rmsgs if m["role"] == "tool"][-1]


def test_compact_verification_summary():
    ver = {
        "ok": False,
        "exit_code": 1,
        "timed_out": False,
        "stdout": _pytest_like_output(),
        "vault_memory_id": "mem_exec_abc12345",
    }
    out = compact_verification_summary(ver)
    assert "exit code 1" in out
    assert "AssertionError" in out
    assert "mem_exec_abc12345" in out
    assert len(out.encode("utf-8")) < 3500
    assert "timed out" in compact_verification_summary({"timed_out": True, "stdout": ""})


def test_mcp_report_offload():
    import server

    with tempfile.TemporaryDirectory() as tmp:
        report = "Audit finding line.\n" * 600
        res = {
            "ok": True,
            "task_id": "task_8f1a2b",
            "status": "DONE",
            "summary": report,
            "message": report,
            "files_touched": ["a.py"],
            "exec_ran": False,
            "attempts": [],
            "steps": 3,
        }
        out = server._offload_report(dict(res), tmp)
        assert out["vault_memory_id"] == "mem_task_task_8f1a2b_report"
        assert len(out["summary"].encode("utf-8")) <= 2048
        text = server._format_result_text(out)
        assert len(text.encode("utf-8")) < 1024
        assert "read_memory(memory_id='mem_task_task_8f1a2b_report')" in text
        full = read_memory(tmp, out["vault_memory_id"])["content"]
        assert full.count("Audit finding line.") == 600
        # existing keys preserved
        for k in ("ok", "status", "files_touched", "exec_ran", "attempts"):
            assert k in out
        # small reports are untouched
        small = {"ok": True, "task_id": "t", "summary": "short"}
        assert server._offload_report(dict(small), tmp) == small
