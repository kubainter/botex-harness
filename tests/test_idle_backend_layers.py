# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Idle backend layer tests: /mcp/sse guard, catalog freshness headers,
provider-metadata warmup, ghost chat-session sweep, and frontmatter ID repair.

These cover the curator/idle take-over work: request-time network work and
filesystem debris move to deterministic background layers whose failures
never touch the user-facing path.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# P4: /mcp/sse ASGI error guard (double-send aware)
# ---------------------------------------------------------------------------


def _noop_receive() -> Any:
    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def _noop_send() -> Any:
    async def send(message: dict) -> None:
        return None

    return send


def test_mcp_sse_guard_swallows_validation_error(caplog: Any) -> None:
    from botex.server.server_cloud import _MCPSseErrorGuard

    async def failing_app(scope: Any, receive: Any, send: Any) -> None:
        # Mirrors mcp sse.py connect_sse: error_response already sent, THEN raise.
        raise ValueError("Request validation failed")

    guard = _MCPSseErrorGuard(failing_app)
    with caplog.at_level("WARNING", logger="botex.server.server_cloud"):
        asyncio.run(guard({"type": "http"}, _noop_receive(), _noop_send()))
    assert any("MCP SSE transport rejected request" in rec.message for rec in caplog.records)


def test_mcp_sse_guard_propagates_non_validation_errors() -> None:
    from botex.server.server_cloud import _MCPSseErrorGuard

    async def failing_app(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("unexpected internals")

    guard = _MCPSseErrorGuard(failing_app)
    with pytest.raises(RuntimeError):
        asyncio.run(guard({"type": "http"}, _noop_receive(), _noop_send()))


def test_mcp_sse_guard_passes_through_happy_path() -> None:
    from botex.server.server_cloud import _MCPSseErrorGuard

    called: list[str] = []

    async def ok_app(scope: Any, receive: Any, send: Any) -> None:
        called.append(scope["type"])

    guard = _MCPSseErrorGuard(ok_app)
    asyncio.run(guard({"type": "http"}, _noop_receive(), _noop_send()))
    assert called == ["http"]


# ---------------------------------------------------------------------------
# P3a: /api/models freshness headers
# ---------------------------------------------------------------------------


def test_get_models_marks_stale_headers(tmp_path: Any, monkeypatch: Any) -> None:
    import botex.server.server_cloud as sc

    monkeypatch.setattr(
        sc, "get_consolidated_models", lambda **kwargs: [{"id": "m", "provider": "openrouter"}]
    )
    cache_file = tmp_path / "meta_cache.json"
    cache_file.write_text("{}", encoding="utf-8")
    stale_ts = 1_000_000.0  # far in the past → age far beyond TTL
    os.utime(cache_file, (stale_ts, stale_ts))
    monkeypatch.setattr(sc, "CACHE_FILE_DEFAULT", cache_file)

    response = sc.Response()
    models = asyncio.run(sc.get_models(response=response, provider=None, force_refresh=False))
    assert models and models[0]["id"] == "m"
    assert response.headers["X-Catalog-Stale"] == "true"
    assert response.headers["X-Catalog-Fetched-At"] == str(int(stale_ts))


def test_get_models_fresh_headers(tmp_path: Any, monkeypatch: Any) -> None:
    import time

    import botex.server.server_cloud as sc

    monkeypatch.setattr(sc, "get_consolidated_models", lambda **kwargs: [])
    cache_file = tmp_path / "meta_cache.json"
    cache_file.write_text("{}", encoding="utf-8")
    now = time.time()
    os.utime(cache_file, (now, now))
    monkeypatch.setattr(sc, "CACHE_FILE_DEFAULT", cache_file)

    response = sc.Response()
    asyncio.run(sc.get_models(response=response, provider=None, force_refresh=False))
    assert response.headers["X-Catalog-Stale"] == "false"


def test_get_models_missing_cache_is_stale(tmp_path: Any, monkeypatch: Any) -> None:
    import botex.server.server_cloud as sc

    monkeypatch.setattr(sc, "get_consolidated_models", lambda **kwargs: [])
    monkeypatch.setattr(sc, "CACHE_FILE_DEFAULT", tmp_path / "does_not_exist.json")

    response = sc.Response()
    asyncio.run(sc.get_models(response=response, provider=None, force_refresh=False))
    assert response.headers["X-Catalog-Stale"] == "true"
    assert response.headers["X-Catalog-Fetched-At"] == "0"


# ---------------------------------------------------------------------------
# P3b: provider metadata warmup
# ---------------------------------------------------------------------------


class _FakeCatalog:
    instances: list[Any] = []

    def __init__(
        self, provider_name: str = "openrouter", provider_cfg: Any = None, **kwargs: Any
    ) -> None:
        self.provider_name = provider_name
        _FakeCatalog.instances.append(self)

    def load(self) -> dict:
        return {"m1": object(), "m2": object()}


def test_warm_provider_metadata_hits_all_caches(monkeypatch: Any) -> None:
    import botex.ops.maintenance as maint

    _FakeCatalog.instances = []
    monkeypatch.setattr(
        maint,
        "_load_config",
        lambda: {
            "providers": {
                "openrouter": {"base_url": "https://or.test/api/v1", "api_key_env": "T_KEY"}
            }
        },
    )
    monkeypatch.setattr("botex.providers.catalog.ModelCatalog", _FakeCatalog)
    pricing_calls: list[str] = []

    def fake_pricing(base_url: str, api_key: str = "") -> dict:
        pricing_calls.append((base_url, api_key))
        return {"https://or.test/api/v1": {"m1": {"input": 0.0}, "m2": {"input": 0.0}}}

    monkeypatch.setattr("botex.providers.pricing.load_catalog", fake_pricing)
    bench_calls: list[int] = []
    monkeypatch.setattr(
        "botex.providers.benchmarks.fetch_openrouter_benchmarks",
        lambda *a, **k: bench_calls.append(1) or [{"id": 1}],
    )

    results = maint.warm_provider_metadata(workspace_dir=".")
    assert results["catalog:openrouter"] == 2
    assert results["pricing:openrouter"] == 2
    assert results["benchmarks"] == 1
    assert len(_FakeCatalog.instances) == 1
    assert pricing_calls and pricing_calls[0][1] == ""  # TEST_KEY env absent → empty key, no crash


def test_warm_provider_metadata_isolates_failures(monkeypatch: Any) -> None:
    import botex.ops.maintenance as maint

    monkeypatch.setattr(
        maint,
        "_load_config",
        lambda: {"providers": {"openrouter": {"base_url": "https://or.test/api/v1"}}},
    )

    class _BrokenCatalog:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("catalog down")

    monkeypatch.setattr("botex.providers.catalog.ModelCatalog", _BrokenCatalog)

    def broken_pricing(base_url: str, api_key: str = "") -> dict:
        raise RuntimeError("pricing down")

    monkeypatch.setattr("botex.providers.pricing.load_catalog", broken_pricing)
    monkeypatch.setattr(
        "botex.providers.benchmarks.fetch_openrouter_benchmarks",
        lambda *a, **k: {"data": True},
    )

    results = maint.warm_provider_metadata(workspace_dir=".")
    # Benchmark still warmed despite catalog+pricing failures; no exception escapes.
    assert results["benchmarks"] == 1
    assert "catalog:openrouter" not in results


# ---------------------------------------------------------------------------
# P3b: ghost chat-session sweep
# ---------------------------------------------------------------------------


def _write_session(path: Any, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _backdate(path: Any, seconds: float = 3600.0) -> None:
    stamp = os.path.getmtime(path) - seconds
    os.utime(path, (stamp, stamp))


def test_sweep_removes_only_empty_ghost_sessions(tmp_path: Any, monkeypatch: Any) -> None:
    import botex.ops.maintenance as maint
    import botex.server.config as server_config

    sessions = tmp_path / "chat_sessions"
    sessions.mkdir()
    ghost = sessions / "ghost.json"
    _write_session(ghost, {"session_id": "ghost", "messages": []})
    _backdate(ghost)
    active = sessions / "active.json"
    _write_session(
        active, {"session_id": "active", "messages": [{"role": "user", "content": "hi"}]}
    )
    fresh_empty = sessions / "fresh.json"
    _write_session(fresh_empty, {"session_id": "fresh", "messages": []})
    broken = sessions / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    _backdate(broken)
    foreign = sessions / "foreign.json"
    _write_session(foreign, {"foo": 1})
    _backdate(foreign)

    monkeypatch.setattr(server_config, "get_chat_sessions_dir", lambda ws: str(sessions))

    removed = maint.sweep_empty_chat_sessions(workspace_dir=".")
    assert removed == 1
    assert not ghost.exists()
    assert active.exists()
    assert fresh_empty.exists()  # touched too recently — never raced
    assert broken.exists()  # unparsable — left alone
    assert foreign.exists()  # not a BoteX session shape — left alone


def test_sweep_missing_dir_is_noop(tmp_path: Any, monkeypatch: Any) -> None:
    import botex.ops.maintenance as maint
    import botex.server.config as server_config

    monkeypatch.setattr(server_config, "get_chat_sessions_dir", lambda ws: str(tmp_path / "nope"))
    assert maint.sweep_empty_chat_sessions(workspace_dir=".") == 0


# ---------------------------------------------------------------------------
# P3c: memory frontmatter ID repair & canonical stem identity
# ---------------------------------------------------------------------------


def test_repair_frontmatter_ids_fixes_redacted_id(tmp_path: Any, monkeypatch: Any) -> None:
    from botex import memory as mem_mod
    import botex.ops.maintenance as maint

    ws = tmp_path / "ws"
    mem_dir = ws / ".botex" / "memory"
    mem_dir.mkdir(parents=True)
    monkeypatch.setattr(mem_mod, "get_global_memory_dir", lambda: tmp_path / "global_empty")
    monkeypatch.setattr(mem_mod, "get_swarm_memory_dir", lambda: None)

    note_path = mem_dir / "test_note.md"
    body_content = "This is a decision note.\nLine 2 of note body.\n"
    initial_text = f"---\nid: [REDACTED]\nschema: ecc.memory.v2\ntitle: Test Decision\nkind: decision\n---\n{body_content}"
    note_path.write_text(initial_text, encoding="utf-8")

    res = maint.repair_frontmatter_ids(workspace_dir=str(ws))
    assert res == {"scanned": 1, "repaired": 1, "skipped": 0}

    repaired_text = note_path.read_text(encoding="utf-8")
    meta, body = mem_mod._parse_frontmatter(repaired_text)
    assert meta.get("id") == "test_note"
    assert meta.get("title") == "Test Decision"
    assert meta.get("schema") == "ecc.memory.v2"
    assert meta.get("kind") == "decision"
    assert body == body_content

    conn = mem_mod._get_index_db(mem_dir)
    try:
        row = conn.execute(
            "SELECT id, title, mtime FROM memories_meta WHERE id = 'test_note';"
        ).fetchone()
        assert row is not None
        assert row["id"] == "test_note"
        assert row["title"] == "Test Decision"
        assert abs(float(row["mtime"]) - note_path.stat().st_mtime) < 0.001
    finally:
        conn.close()


def test_repair_frontmatter_ids_skips_invalid_stem(tmp_path: Any, monkeypatch: Any) -> None:
    from botex import memory as mem_mod
    import botex.ops.maintenance as maint

    ws = tmp_path / "ws"
    mem_dir = ws / ".botex" / "memory"
    mem_dir.mkdir(parents=True)
    monkeypatch.setattr(mem_mod, "get_global_memory_dir", lambda: tmp_path / "global_empty")
    monkeypatch.setattr(mem_mod, "get_swarm_memory_dir", lambda: None)

    bad_stem_file = mem_dir / "invalid stem note.md"
    content = "---\nid: old_id\ntitle: Invalid Stem Note\n---\nUntouched content.\n"
    bad_stem_file.write_text(content, encoding="utf-8")

    res = maint.repair_frontmatter_ids(workspace_dir=str(ws))
    assert res == {"scanned": 1, "repaired": 0, "skipped": 1}
    assert bad_stem_file.read_text(encoding="utf-8") == content


def test_repair_frontmatter_ids_leaves_valid_untouched(tmp_path: Any, monkeypatch: Any) -> None:
    from botex import memory as mem_mod
    import botex.ops.maintenance as maint

    ws = tmp_path / "ws"
    mem_dir = ws / ".botex" / "memory"
    mem_dir.mkdir(parents=True)
    monkeypatch.setattr(mem_mod, "get_global_memory_dir", lambda: tmp_path / "global_empty")
    monkeypatch.setattr(mem_mod, "get_swarm_memory_dir", lambda: None)

    valid_file = mem_dir / "valid_note.md"
    content = (
        "---\nid: valid_note\nschema: ecc.memory.v2\ntitle: Valid Note\n---\nValid note body.\n"
    )
    valid_file.write_text(content, encoding="utf-8")
    orig_mtime = valid_file.stat().st_mtime

    res = maint.repair_frontmatter_ids(workspace_dir=str(ws))
    assert res == {"scanned": 1, "repaired": 0, "skipped": 0}
    assert valid_file.read_text(encoding="utf-8") == content
    assert valid_file.stat().st_mtime == orig_mtime


def test_search_memory_canonical_stem_regression_153_152(tmp_path: Any, monkeypatch: Any) -> None:
    from botex import memory as mem_mod

    ws = tmp_path / "ws"
    mem_dir = ws / ".botex" / "memory"
    mem_dir.mkdir(parents=True)
    monkeypatch.setattr(mem_mod, "get_global_memory_dir", lambda: tmp_path / "global_empty")
    monkeypatch.setattr(mem_mod, "get_swarm_memory_dir", lambda: None)

    file_p = mem_dir / "botex_prompt_cache_deployed.md"
    file_p.write_text(
        """---
id: [REDACTED]
schema: ecc.memory.v2
title: BoteX prompt-cache optimization
kind: decision
confidence: 1.0
timestamp: "2026-11-07T12:02:29.337440+00:00"
---
Prompt cache deployed with 123 tests green.
""",
        encoding="utf-8",
    )
    mem_mod.sync_memory_index(mem_dir)

    search_res = mem_mod.search_memory(
        workspace_dir=str(ws),
        scope="local",
        since="2026-11-01T00:00:00Z",
    )
    assert search_res["ok"] is True
    assert search_res["count"] == 1
    found = search_res["memories"][0]
    assert found["id"] == "botex_prompt_cache_deployed"
    assert found["title"] == "BoteX prompt-cache optimization"
