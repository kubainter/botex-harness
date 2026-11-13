# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for Two-tier Memory Vault (botex/memory.py) and server MCP integration."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import threading
from unittest.mock import patch

import pytest

import server
from botex import memory


@pytest.fixture(autouse=True)
def cleanup_memory_cache():
    """Reset memory index cache after each test to prevent SQLite file locks."""
    yield
    memory.reset_memory_index_cache()


@pytest.fixture
def memory_dirs(tmp_path: Path):
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    global_dir = tmp_path / "global_memory"
    global_dir.mkdir()

    with patch("botex.memory.get_global_memory_dir", return_value=global_dir):
        yield ws_dir, global_dir


def test_two_tier_save_local_and_global(memory_dirs):
    """Verify saving to local workspace vs global shared vault."""
    ws_dir, global_dir = memory_dirs

    # 1. Local scope save
    res_local = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Local ADR: Cache policy",
        content="We use Redis for local session caching.",
        kind="decision",
        tags=["architecture", "cache"],
        scope="local",
    )
    assert res_local["ok"] is True
    assert res_local["scope"] == "local"
    local_id = res_local["id"]
    local_file = ws_dir / ".botex" / "memory" / f"{local_id}.md"
    assert local_file.exists()

    # 2. Global scope save
    res_global = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Global Guideline: Error handling",
        content="Always sanitize exception traces across all services.",
        kind="lesson",
        tags=["best_practice", "security"],
        scope="global",
    )
    assert res_global["ok"] is True
    assert res_global["scope"] == "global"
    global_id = res_global["id"]
    global_file = global_dir / f"{global_id}.md"
    assert global_file.exists()

    # Verify global file frontmatter
    text_global = global_file.read_text(encoding="utf-8")
    assert "scope: global" in text_global
    assert "namespace: _global" in text_global

    # 3. Namespace alias '_global'
    res_ns = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Global Standard: Python version",
        content="Target Python 3.10+ across repositories.",
        namespace="_global",
    )
    assert res_ns["ok"] is True
    assert res_ns["scope"] == "global"
    assert (global_dir / f"{res_ns['id']}.md").exists()


def test_two_tier_read_memory(memory_dirs):
    """Verify reading memory with scope separation and fallback."""
    ws_dir, _ = memory_dirs

    r_local = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Local Note",
        content="Sensitive local detail.",
        scope="local",
    )
    r_global = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Global Note",
        content="Shared global detail.",
        scope="global",
    )

    # Read from local scope
    read_local = memory.read_memory(
        workspace_dir=str(ws_dir), memory_id=r_local["id"], scope="local"
    )
    assert read_local["ok"] is True
    assert "Sensitive local detail." in read_local["content"]
    assert read_local["scope"] == "local"

    # Local scope cannot find global entry by default
    read_local_miss = memory.read_memory(
        workspace_dir=str(ws_dir), memory_id=r_global["id"], scope="local"
    )
    assert read_local_miss["ok"] is False

    # Global scope reads global entry
    read_global = memory.read_memory(
        workspace_dir=str(ws_dir), memory_id=r_global["id"], scope="global"
    )
    assert read_global["ok"] is True
    assert "Shared global detail." in read_global["content"]
    assert read_global["scope"] == "global"

    # All scope reads both
    read_all_1 = memory.read_memory(workspace_dir=str(ws_dir), memory_id=r_local["id"], scope="all")
    assert read_all_1["ok"] is True
    assert read_all_1["scope"] == "local"

    read_all_2 = memory.read_memory(
        workspace_dir=str(ws_dir), memory_id=r_global["id"], scope="all"
    )
    assert read_all_2["ok"] is True
    assert read_all_2["scope"] == "global"


def test_two_tier_search_and_list_cross_workspace(memory_dirs):
    """Verify cross-workspace search aggregating local + global knowledge."""
    ws_dir, _ = memory_dirs

    memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Local Auth Setup",
        content="Local OAuth2 implementation notes.",
        kind="context",
        tags=["auth", "oauth"],
        scope="local",
    )
    memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Global Auth Standards",
        content="Global OAuth2 token format requirements.",
        kind="decision",
        tags=["auth", "jwt"],
        scope="global",
    )

    # Search local only
    search_local = memory.search_memory(workspace_dir=str(ws_dir), query="OAuth2", scope="local")
    assert search_local["ok"] is True
    assert search_local["count"] == 1
    assert search_local["memories"][0]["scope"] == "local"

    # Search global only
    search_global = memory.search_memory(workspace_dir=str(ws_dir), query="OAuth2", scope="global")
    assert search_global["ok"] is True
    assert search_global["count"] == 1
    assert search_global["memories"][0]["scope"] == "global"

    # Search all (aggregates local + global)
    search_all = memory.search_memory(workspace_dir=str(ws_dir), query="OAuth2", scope="all")
    assert search_all["ok"] is True
    assert search_all["count"] == 2
    scopes = {m["scope"] for m in search_all["memories"]}
    assert scopes == {"local", "global"}

    # List memories
    list_all = memory.list_memories(workspace_dir=str(ws_dir), scope="all")
    assert list_all["ok"] is True
    assert list_all["count"] >= 2


def test_two_tier_secret_masking(memory_dirs):
    """Verify secret masking in both local and global memory vaults (CWE-312)."""
    ws_dir, _ = memory_dirs

    res = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Global API Key note sk-abcdef1234567890abcdef",
        content="Use key sk-abcdef1234567890abcdef with bearer token Bearer secret_12345678901234567890",
        tags=["api_key", "sk-secret123456789"],
        scope="global",
    )
    assert res["ok"] is True
    assert "sk-" not in res["title"]
    assert "[REDACTED]" in res["title"]

    read_res = memory.read_memory(workspace_dir=str(ws_dir), memory_id=res["id"], scope="global")
    assert read_res["ok"] is True
    assert "sk-" not in read_res["content"]
    assert "[REDACTED]" in read_res["content"]


def test_mcp_server_memory_tools_and_exact_tool_count(memory_dirs):
    """Verify server.py MCP tools accept scope and exactly 20 tools exist."""
    ws_dir, _ = memory_dirs

    async def _test():
        # 1. Verify exactly 20 tools registered on MCPServer
        tools = await server.mcp.list_tools()
        assert len(tools) == 20, (
            f"Expected exactly 20 MCP tools, found {len(tools)}: {[t.name for t in tools]}"
        )

        # 2. Test save_memory MCP tool with scope
        saved = await server.save_memory(
            title="MCP Test Global Memory",
            content="Created via MCP tool interface.",
            workspace_dir=str(ws_dir),
            scope="global",
        )
        assert saved["ok"] is True
        assert saved["scope"] == "global"
        mem_id = saved["id"]

        # 3. Test read_memory MCP tool with scope
        read_item = await server.read_memory(
            memory_id=mem_id,
            workspace_dir=str(ws_dir),
            scope="global",
        )
        assert read_item["ok"] is True
        assert read_item["scope"] == "global"

        # 4. Test search_memory MCP tool with scope="all"
        searched = await server.search_memory(
            workspace_dir=str(ws_dir),
            query="MCP Test",
            scope="all",
        )
        assert searched["ok"] is True
        assert searched["count"] >= 1

    asyncio.run(_test())


def test_cli_memory_commands(memory_dirs, capsys: pytest.CaptureFixture):
    """Verify botex memory CLI subcommands: save, search, read, list."""
    ws_dir, _ = memory_dirs

    # CLI save
    server._cli_memory(
        [
            "save",
            "CLI Note",
            "CLI Note body content",
            "--scope",
            "global",
            "-w",
            str(ws_dir),
            "--id",
            "mem_cli_test_1",
        ]
    )
    cap = capsys.readouterr()
    assert "Saved memory 'mem_cli_test_1'" in cap.out

    # CLI read
    server._cli_memory(["read", "mem_cli_test_1", "--scope", "global", "-w", str(ws_dir)])
    cap = capsys.readouterr()
    assert "CLI Note body content" in cap.out

    # CLI search
    server._cli_memory(["search", "CLI", "--scope", "all", "-w", str(ws_dir)])
    cap = capsys.readouterr()
    assert "mem_cli_test_1" in cap.out

    # CLI list
    server._cli_memory(["list", "--scope", "global", "-w", str(ws_dir)])
    cap = capsys.readouterr()
    assert "mem_cli_test_1" in cap.out


def test_purge_ephemeral_memories_global_and_namespace(memory_dirs):
    """Verify purge_ephemeral_memories works across global scope and namespace='_global'."""
    ws_dir, global_dir = memory_dirs

    # Save ephemeral memory in global vault
    res1 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Ephemeral Global 1",
        content="Temporary global note",
        tags=["ephemeral"],
        scope="global",
        memory_id="mem_eph_glob_1",
    )
    assert res1["ok"] is True

    # Save non-ephemeral memory in global vault
    res2 = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="Permanent Global 1",
        content="Permanent global note",
        tags=["decision"],
        scope="global",
        memory_id="mem_perm_glob_1",
    )
    assert res2["ok"] is True

    # Purge with max_age_days=0 and namespace="_global"
    deleted = memory.purge_ephemeral_memories(
        workspace_dir=str(ws_dir), max_age_days=0, namespace="_global"
    )
    assert deleted == 1

    # Verify ephemeral is removed and permanent is intact
    assert not (global_dir / "mem_eph_glob_1.md").exists()
    assert (global_dir / "mem_perm_glob_1.md").exists()


def test_engine_save_task_receipt(memory_dirs):
    """Verify engine._save_task_receipt creates a zero-token handoff receipt in .botex/memory/."""
    from botex.engine import _save_task_receipt

    ws_dir, _ = memory_dirs
    task_id = "test-auto-receipt-1234"
    prompt = "Refactor authentication flow to use JWT tokens\nAdditional requirements here"
    files_touched = ["auth/jwt.py", "auth/service.py"]
    lines_added = 42
    lines_removed = 10
    model = "anthropic/claude-3.7-sonnet"
    provider = "openrouter"
    verify_command = "pytest tests/test_auth.py"

    receipt = _save_task_receipt(
        task_id=task_id,
        prompt=prompt,
        files_touched=files_touched,
        lines_added=lines_added,
        lines_removed=lines_removed,
        model=model,
        provider=provider,
        verify_command=verify_command,
        root_path=ws_dir,
    )

    assert receipt is not None
    assert receipt["ok"] is True
    assert "mem_" in receipt["id"]
    assert receipt["kind"] == "handoff"
    assert "task-receipt" in receipt["tags"]
    assert receipt["scope"] == "local"

    # Verify content on disk
    mem_file = ws_dir / ".botex" / "memory" / f"{receipt['id']}.md"
    assert mem_file.exists()
    content = mem_file.read_text(encoding="utf-8")
    assert "schema: ecc.memory.v2" in content or "schema: ecc.memory.v1" in content
    assert "kind: handoff" in content
    assert f"Task Receipt: {task_id}" in content
    assert "Refactor authentication flow" in content
    assert "`auth/jwt.py`" in content
    assert "`pytest tests/test_auth.py`" in content


def test_sqlite_connection_pooling_and_noop_close(tmp_path: Path):
    """Verify _get_index_db returns the same connection and close() is a no-op."""
    mem_dir = tmp_path / "memory_pool_test"
    conn1 = memory._get_index_db(mem_dir)
    conn2 = memory._get_index_db(mem_dir)
    assert conn1 is conn2
    assert isinstance(conn1, memory._PooledConnection)

    # Calling close() must NOT close the connection
    conn1.close()
    row = conn1.execute("SELECT 1;").fetchone()
    assert row[0] == 1

    # Calling force_close() actually disposes the connection
    conn1.force_close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn1.execute("SELECT 1;")


def test_sqlite_ddl_memoization(tmp_path: Path):
    """Verify DDL execution is recorded once in _SCHEMA_READY."""
    mem_dir = tmp_path / "schema_memo_test"
    db_key = str((mem_dir / "index.db").resolve())
    assert db_key not in memory._SCHEMA_READY

    conn = memory._get_index_db(mem_dir)
    assert db_key in memory._SCHEMA_READY

    # Verify tables and WAL mode are present
    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert journal_mode.lower() == "wal"

    # Second call uses cached schema
    conn2 = memory._get_index_db(mem_dir)
    assert conn2 is conn


def test_sqlite_thread_isolation(tmp_path: Path):
    """Verify distinct threads receive distinct PooledConnection instances."""
    mem_dir = tmp_path / "thread_iso_test"
    main_conn = memory._get_index_db(mem_dir)
    thread_conns: list[sqlite3.Connection] = []

    def _worker():
        t_conn = memory._get_index_db(mem_dir)
        thread_conns.append(t_conn)
        row = t_conn.execute("SELECT 1;").fetchone()
        assert row[0] == 1

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()

    assert len(thread_conns) == 1
    worker_conn = thread_conns[0]
    assert worker_conn is not main_conn


def test_save_memory_fails_closed_on_unknown_scope(memory_dirs):
    """CWE-020: a non-empty unrecognized scope is rejected, never silently
    degraded to 'local'; namespace='_global' still forces global."""
    ws_dir, global_dir = memory_dirs

    for bad_scope in ("bogus", "all", "swarm", "both", "global-ish", " workspace "):
        res = memory.save_memory(
            workspace_dir=str(ws_dir),
            title="Scoped Note",
            content="x",
            scope=bad_scope,
        )
        assert res["ok"] is False, bad_scope
        # Static message — caller input is never echoed (CWE-117).
        assert res["error"] == "Invalid memory scope; expected 'local' or 'global'."

    # Nothing leaked into either vault from the rejected saves.
    local_mem = ws_dir / ".botex" / "memory"
    leaked = list(local_mem.glob("*.md")) if local_mem.exists() else []
    assert leaked == []
    assert list(global_dir.glob("*.md")) == []

    # Empty scope behaves like the default ('local').
    res_empty = memory.save_memory(
        workspace_dir=str(ws_dir), title="Empty Scope", content="x", scope=""
    )
    assert res_empty["ok"] is True
    assert res_empty["scope"] == "local"

    # namespace='_global' still forces global even with a bad scope string.
    res_ns = memory.save_memory(
        workspace_dir=str(ws_dir),
        title="NS Override",
        content="x",
        namespace="_global",
        scope="bogus",
    )
    assert res_ns["ok"] is True
    assert res_ns["scope"] == "global"
    assert (global_dir / f"{res_ns['id']}.md").exists()

    # Case/whitespace variants normalize to the canonical global scope.
    res_norm = memory.save_memory(
        workspace_dir=str(ws_dir), title="Norm Scope", content="x", scope=" Global "
    )
    assert res_norm["ok"] is True
    assert res_norm["scope"] == "global"
    assert (global_dir / f"{res_norm['id']}.md").exists()


def test_reset_memory_index_cache(tmp_path: Path):
    """Verify reset_memory_index_cache closes all connections and clears schema cache."""
    mem_dir = tmp_path / "reset_test"
    db_key = str((mem_dir / "index.db").resolve())
    conn = memory._get_index_db(mem_dir)
    assert db_key in memory._SCHEMA_READY

    memory.reset_memory_index_cache()
    assert db_key not in memory._SCHEMA_READY

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1;")
