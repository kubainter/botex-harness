# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Milestone M7 Adversarial Concurrency & Stress Verification Suite
================================================================
Empirical challenge tests verifying:
1. NonClosingAsyncClient concurrency resilience:
   - 50 concurrent async tasks instantiating AsyncOpenAI(http_client=get_async_client()).
   - Verifying mock request execution, client.close() immunity, and underlying singleton reuse.
   - Context manager async with client stress and teardown verification.
2. SQLite connection pool & DDL memoization concurrency stress:
   - 20 concurrent worker threads thundering herd on _get_index_db().
   - Strict verification that DDL is executed exactly once via _SCHEMA_READY memoization.
   - Zero SQLite database locked / operational errors under concurrent writes and reads in WAL mode.
   - Clean disposal via close_index_dbs() with zero connection leaks and full GC reclaim.
   - Multi-vault isolation across distinct directories.
3. Streaming secret masking (mask_stream_chunk) boundary stress:
   - Adversarial boundary slicing across multiple chunks with zero sensitive token leaks.
"""

from __future__ import annotations

import asyncio
import gc
import sqlite3
import tempfile
import threading
import weakref
from pathlib import Path
from typing import Any, List

import httpx2
import openai
import pytest

from botex import memory
from botex.providers.http_pool import (
    NonClosingAsyncClient,
    close_http_pool,
    get_async_client,
    get_sync_client,
    reset_http_pool,
)
from server import mask_stream_chunk


@pytest.fixture(autouse=True)
def clean_isolation():
    """Ensure HTTP and SQLite connection pools are strictly reset around every test."""
    reset_http_pool()
    memory.reset_memory_index_cache()
    yield
    reset_http_pool()
    memory.reset_memory_index_cache()


# ============================================================================
# 1. NonClosingAsyncClient Concurrency Stress Tests
# ============================================================================


@pytest.mark.anyio
async def test_non_closing_async_client_50_concurrent_tasks():
    """Adversarial Concurrency Stress: Spawn 50 concurrent tasks using AsyncOpenAI

    wrapped around get_async_client(). Verify all mock requests succeed, client.close()
    is safely ignored, the singleton remains open across all 50 tasks, task 51 reuses
    the same client, and close_http_pool() cleanly shuts it down.
    """
    reset_http_pool()
    shared_client = get_async_client()
    assert isinstance(shared_client, NonClosingAsyncClient)
    assert not shared_client.is_closed

    # Configure mock transport returning chat completions with async concurrency jitter
    def _mock_handler(request: httpx2.Request) -> httpx2.Response:
        import json

        body = json.loads(request.read().decode("utf-8")) if request.content else {}
        messages = body.get("messages", [])
        last_msg = messages[-1]["content"] if messages else "empty"

        return httpx2.Response(
            status_code=200,
            json={
                "id": "chatcmpl-stress-test",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"Echo: {last_msg}",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                },
            },
        )

    # Attach mock transport to pooled client
    shared_client._transport = httpx2.MockTransport(_mock_handler)

    task_results: List[str] = []
    task_errors: List[Exception] = []

    async def _worker_task(task_id: int):
        try:
            # High concurrency jitter to interleave close() and request executions
            await asyncio.sleep(0.001 * (task_id % 7))

            # Retrieve singleton async client
            http_client = get_async_client()
            assert http_client is shared_client, (
                f"Task {task_id} received a different client instance"
            )
            assert not http_client.is_closed, f"Task {task_id} found client prematurely closed"

            # Instantiate AsyncOpenAI per turn as done by SDK consumers
            sdk_client = openai.AsyncOpenAI(
                api_key="mock-api-key",
                base_url="https://api.mock.botex/v1",
                http_client=http_client,
                max_retries=0,
            )

            response = await sdk_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": f"task-payload-{task_id}"}],
            )
            content = response.choices[0].message.content
            assert content == f"Echo: task-payload-{task_id}"
            task_results.append(content)

            # Adversarial trigger: AsyncOpenAI close() calls self._client.aclose()
            await sdk_client.close()

            # The underlying client MUST remain open and reusable!
            assert not http_client.is_closed, f"Task {task_id} caused underlying client to close"
            assert not shared_client.is_closed, "Shared client singleton was closed unexpectedly"
        except Exception as exc:
            task_errors.append(exc)

    # Launch 50 concurrent async tasks simultaneously
    tasks = [_worker_task(i) for i in range(50)]
    await asyncio.gather(*tasks)

    # Verification: all 50 tasks succeeded without errors
    assert len(task_errors) == 0, f"Concurrent tasks encountered errors: {task_errors}"
    assert len(task_results) == 50, f"Expected 50 results, received {len(task_results)}"
    assert not shared_client.is_closed, "Underlying pooled client was closed after 50 tasks"

    # Task 51: Reusability verification — the client must still be fully functional
    task_51_client = openai.AsyncOpenAI(
        api_key="mock-api-key",
        base_url="https://api.mock.botex/v1",
        http_client=get_async_client(),
        max_retries=0,
    )
    resp_51 = await task_51_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "task-51-reuse-check"}],
    )
    assert resp_51.choices[0].message.content == "Echo: task-51-reuse-check"
    await task_51_client.close()
    assert not shared_client.is_closed

    # Clean server shutdown: close_http_pool must now force-close the client
    await close_http_pool()
    assert shared_client.is_closed, "Shared client was not closed after close_http_pool()"


@pytest.mark.anyio
async def test_non_closing_async_client_context_manager_stress():
    """Adversarial Stress: 30 concurrent tasks using async with AsyncOpenAI() as client.

    Verifies __aexit__ does not close the shared NonClosingAsyncClient.
    """
    reset_http_pool()
    shared_client = get_async_client()

    def _mock_handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": "cmpl-ctx",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ctx-ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    shared_client._transport = httpx2.MockTransport(_mock_handler)
    errors = []

    async def _worker(idx: int):
        try:
            async with openai.AsyncOpenAI(
                api_key="mock-key",
                http_client=get_async_client(),
                max_retries=0,
            ) as client:
                res = await client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": f"ping-{idx}"}],
                )
                assert res.choices[0].message.content == "ctx-ok"
            # Outside context: client.__aexit__() was called, shared client must remain open!
            assert not shared_client.is_closed
        except Exception as e:
            errors.append(e)

    await asyncio.gather(*[_worker(i) for i in range(30)])
    assert len(errors) == 0
    assert not shared_client.is_closed

    await close_http_pool()
    assert shared_client.is_closed


# ============================================================================
# 2. SQLite Connection Pool & DDL Memoization Concurrency Stress Tests
# ============================================================================


def test_sqlite_pool_and_ddl_memoization_20_threads_concurrency():
    """Adversarial Concurrency Stress: Spawn 20 threads simultaneously querying and

    inserting into _get_index_db(). Verify:
    1. Zero database locked errors.
    2. _SCHEMA_READY memoization executes DDL exactly once across all 20 threads.
    3. Thread isolation: each thread receives a distinct _PooledConnection.
    4. close_index_dbs() cleanly disposes all 20 connections without memory leaks.
    """
    memory.reset_memory_index_cache()

    with tempfile.TemporaryDirectory() as tmp_dir:
        mem_dir = Path(tmp_dir) / "stress_memory_vault"
        db_path = mem_dir / "index.db"
        db_key = str(db_path.resolve())

        # Instrument _PooledConnection.execute to count DDL table creation calls
        ddl_lock = threading.Lock()
        ddl_calls: List[str] = []
        orig_execute = memory._PooledConnection.execute

        def _traced_execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            if "CREATE TABLE IF NOT EXISTS memories_meta" in str(sql):
                with ddl_lock:
                    ddl_calls.append(sql)
            return orig_execute(self, sql, *args, **kwargs)

        memory._PooledConnection.execute = _traced_execute

        num_threads = 20
        barrier = threading.Barrier(num_threads)
        thread_errors: List[tuple[int, Exception]] = []
        thread_conns: List[sqlite3.Connection] = []
        conns_lock = threading.Lock()

        def _worker(thread_id: int):
            try:
                # Synchronize all 20 threads to hit _get_index_db at the exact same moment
                barrier.wait()

                conn = memory._get_index_db(mem_dir)
                assert isinstance(conn, memory._PooledConnection)
                with conns_lock:
                    thread_conns.append(conn)

                # Perform concurrent write and read operations
                for i in range(10):
                    mem_id = f"mem_thread_{thread_id}_item_{i}"
                    title = f"Memory Title {thread_id}-{i}"
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO memories_meta
                        (id, title, kind, timestamp, confidence)
                        VALUES (?, ?, ?, ?, ?);
                        """,
                        (mem_id, title, "lesson", "2026-11-06T18:00:00Z", 0.95),
                    )
                    conn.commit()

                    # Query back to test concurrent WAL reader/writer operations
                    row = conn.execute(
                        "SELECT title, confidence FROM memories_meta WHERE id = ?;",
                        (mem_id,),
                    ).fetchone()
                    assert row is not None, (
                        f"Thread {thread_id} failed to find inserted row {mem_id}"
                    )
                    assert row["title"] == title
                    assert abs(row["confidence"] - 0.95) < 1e-6

                    # Simulated close() per operation must NOT close the connection
                    conn.close()

                    # Verify connection remains valid after close()
                    test_row = conn.execute("SELECT 1;").fetchone()
                    assert test_row[0] == 1

            except Exception as exc:
                thread_errors.append((thread_id, exc))

        try:
            threads = [threading.Thread(target=_worker, args=(t,)) for t in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15.0)

            # Verification 1: Zero database locked or concurrency errors
            assert len(thread_errors) == 0, f"Encountered concurrency errors: {thread_errors}"
            assert len(thread_conns) == num_threads, f"Expected {num_threads} connections"

            # Verification 2: DDL memoization executed table creation EXACTLY once
            assert len(ddl_calls) == 1, (
                f"_SCHEMA_READY memoization failed: DDL executed {len(ddl_calls)} times instead of 1"
            )
            assert db_key in memory._SCHEMA_READY

            # Verification 3: Thread isolation — each thread got its own distinct connection
            unique_conns = set(thread_conns)
            assert len(unique_conns) == num_threads, (
                f"Thread isolation violated: expected {num_threads} unique connections, got {len(unique_conns)}"
            )

            # Verification 4: Total records inserted across all 20 threads (20 * 10 = 200)
            verify_conn = thread_conns[0]
            count_row = verify_conn.execute("SELECT count(*) FROM memories_meta;").fetchone()
            assert count_row[0] == 200, f"Expected 200 total records, found {count_row[0]}"

            # Verification 5: Clean disposal via close_index_dbs()
            memory.close_index_dbs()

            # All 20 connections must be closed in SQLite
            for idx, c in enumerate(thread_conns):
                with pytest.raises(sqlite3.ProgrammingError):
                    c.execute("SELECT 1;")

            # Caches must be completely emptied
            assert len(memory._ALL_CONNS) == 0
            assert len(memory._SCHEMA_READY) == 0

        finally:
            memory._PooledConnection.execute = orig_execute
            memory.close_index_dbs()


def test_sqlite_pool_multi_vault_isolation():
    """Adversarial Multi-Vault: Concurrently accessing two distinct vault directories

    correctly registers and tracks separate connections and schema readiness.
    """
    memory.reset_memory_index_cache()

    with tempfile.TemporaryDirectory() as tmp_dir:
        vault_a = Path(tmp_dir) / "vault_a"
        vault_b = Path(tmp_dir) / "vault_b"
        key_a = str((vault_a / "index.db").resolve())
        key_b = str((vault_b / "index.db").resolve())

        conn_a = memory._get_index_db(vault_a)
        conn_b = memory._get_index_db(vault_b)

        assert conn_a is not conn_b
        assert key_a in memory._SCHEMA_READY
        assert key_b in memory._SCHEMA_READY
        assert len(memory._SCHEMA_READY) == 2

        # Insert different records into each vault
        conn_a.execute(
            "INSERT INTO memories_meta (id, title, kind, timestamp) VALUES ('id_a', 'Title A', 'note', 'now');"
        )
        conn_a.commit()
        conn_b.execute(
            "INSERT INTO memories_meta (id, title, kind, timestamp) VALUES ('id_b', 'Title B', 'note', 'now');"
        )
        conn_b.commit()

        assert (
            conn_a.execute("SELECT id FROM memories_meta WHERE id = 'id_a';").fetchone() is not None
        )
        assert conn_a.execute("SELECT id FROM memories_meta WHERE id = 'id_b';").fetchone() is None

        assert (
            conn_b.execute("SELECT id FROM memories_meta WHERE id = 'id_b';").fetchone() is not None
        )
        assert conn_b.execute("SELECT id FROM memories_meta WHERE id = 'id_a';").fetchone() is None

        memory.close_index_dbs()
        assert len(memory._SCHEMA_READY) == 0
        assert len(memory._ALL_CONNS) == 0


def test_sqlite_pool_memory_leak_and_gc():
    """Adversarial Leak Check: Verify that worker connections are tracked via weakref

    and cleanly garbage collected without cyclic retention or leaking descriptors.
    """
    memory.reset_memory_index_cache()

    with tempfile.TemporaryDirectory() as tmp_dir:
        mem_dir = Path(tmp_dir) / "leak_test_vault"
        weak_refs: List[weakref.ReferenceType] = []

        def _worker():
            conn = memory._get_index_db(mem_dir)
            weak_refs.append(weakref.ref(conn))
            conn.execute("SELECT 1;").fetchone()

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(weak_refs) == 10

        # Before close: connections are active
        active_before = [r() for r in weak_refs if r() is not None]
        assert len(active_before) == 10

        # Close all pool connections
        memory.close_index_dbs()

        # Connections should now be closed
        for r in weak_refs:
            c = r()
            if c is not None:
                with pytest.raises(sqlite3.ProgrammingError):
                    c.execute("SELECT 1;")

        # Release local references and force garbage collection
        del active_before
        gc.collect()

        # Verify WeakSet in memory.py is completely empty
        assert len(memory._ALL_CONNS) == 0


def test_sqlite_transaction_isolation_between_threads():
    """Adversarial Isolation: A rollback or uncommitted transaction in Thread A

    must not affect or deadlock Thread B's connection.
    """
    memory.reset_memory_index_cache()

    with tempfile.TemporaryDirectory() as tmp_dir:
        mem_dir = Path(tmp_dir) / "tx_test_vault"

        def _worker_writer(barrier: threading.Barrier, res: list):
            try:
                conn = memory._get_index_db(mem_dir)
                barrier.wait()
                conn.execute(
                    "INSERT INTO memories_meta (id, title, kind, timestamp) VALUES ('tx_1', 'Pending', 'note', 'now');"
                )
                # Thread writer rolls back instead of committing
                conn.rollback()
                res.append("writer_done")
            except Exception as e:
                res.append(e)

        def _worker_reader(barrier: threading.Barrier, res: list):
            try:
                conn = memory._get_index_db(mem_dir)
                barrier.wait()
                # Reader commits independent write
                conn.execute(
                    "INSERT INTO memories_meta (id, title, kind, timestamp) VALUES ('tx_2', 'Committed', 'note', 'now');"
                )
                conn.commit()
                row = conn.execute("SELECT id FROM memories_meta WHERE id = 'tx_1';").fetchone()
                assert row is None, "Uncommitted rolled back row from writer leaked to reader!"
                res.append("reader_done")
            except Exception as e:
                res.append(e)

        barrier = threading.Barrier(2)
        writer_res: list = []
        reader_res: list = []

        tw = threading.Thread(target=_worker_writer, args=(barrier, writer_res))
        tr = threading.Thread(target=_worker_reader, args=(barrier, reader_res))

        tw.start()
        tr.start()
        tw.join(timeout=5.0)
        tr.join(timeout=5.0)

        assert writer_res == ["writer_done"]
        assert reader_res == ["reader_done"]

        memory.close_index_dbs()


# ============================================================================
# 3. Streaming Secret Masking (mask_stream_chunk) Boundary Stress Tests
# ============================================================================


def test_mask_stream_chunk_boundary_splitting():
    """Adversarial Secret Masking: Test token split across chunks at tricky boundaries."""
    # Scenario A: Split between "sk-" prefix in carry and secret body in chunk 2
    chunk1 = {"type": "content_chunk", "delta": "The key is: "}
    res1, carry1 = mask_stream_chunk(chunk1, carry="")
    assert res1["delta"] == "The key is: "

    chunk2 = {"type": "content_chunk", "delta": "sk-ant-api03-secret1234567890abcdef and finished."}
    res2, carry2 = mask_stream_chunk(chunk2, carry=carry1)
    assert "[REDACTED]" in res2["delta"]
    assert "secret1234567890abcdef" not in res2["delta"]
    assert "sk-" not in res2["delta"]

    # Scenario B: Secret body split across chunk boundary (prefix sk- at chunk 1 tail, suffix in chunk 2)
    c_a = {"type": "content_chunk", "delta": "Api key header sk-"}
    res_a, carry_a = mask_stream_chunk(c_a, carry="")
    assert carry_a == "Api key header sk-"

    c_b = {"type": "content_chunk", "delta": "ant-api03-secret1234567890abcdef rest of text"}
    res_b, carry_b = mask_stream_chunk(c_b, carry=carry_a)
    assert "[REDACTED]" in res_b["delta"]
    assert "ant-api03-secret1234567890abcdef" not in res_b["delta"]
    assert res_b["delta"] == "[REDACTED] rest of text"

    # Scenario C: Secret split across 3 consecutive chunks
    c1 = {"type": "content_chunk", "delta": "Secret starts: "}
    r1, k1 = mask_stream_chunk(c1, carry="")
    c2 = {"type": "content_chunk", "delta": "sk-"}
    r2, k2 = mask_stream_chunk(c2, carry=k1)
    c3 = {"type": "content_chunk", "delta": "ant-api03-secret9999999999abcdef end."}
    r3, k3 = mask_stream_chunk(c3, carry=k2)
    assert "[REDACTED]" in r3["delta"]
    assert "secret9999999999abcdef" not in r3["delta"]
    assert r3["delta"] == "[REDACTED] end."
