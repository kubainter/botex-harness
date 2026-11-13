# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for F4 Pipe A: MCP origin provenance, X-Agent-Id/X-Host headers,
Windows-on-POSIX workspace path normalization, and SSE X-Accel-Buffering."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import server as botex_server
from botex.config import (
    MCP_CLIENT_ORIGIN,
    get_mcp_client_origin,
    is_cross_host_windows_path,
    resolve_workspace,
)
from botex.server.server_cloud import (
    CloudAuthMiddleware,
    _MCPSseErrorGuard,
    _extract_mcp_origin_from_headers,
    _normalize_origin_claim,
    _sanitize_mcp_client_origin,
)
from botex.server_cloud import app


@pytest.fixture
def client(monkeypatch):
    """Unauthenticated TestClient (Zero Friction Local mode)."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN_HASH", raising=False)
    with TestClient(app) as test_client:
        yield test_client


def test_extract_mcp_origin_from_headers():
    assert (
        _extract_mcp_origin_from_headers({b"x-agent-id": b"devin", b"x-host": b"Botek"})
        == "devin@Botek"
    )
    assert _extract_mcp_origin_from_headers({b"x-agent-id": b"zcode"}) == "zcode"
    assert _extract_mcp_origin_from_headers({b"x-host": b"vps-01"}) == "botex@vps-01"
    assert _extract_mcp_origin_from_headers({}) == ""
    # Sanitizes unsafe characters
    assert (
        _extract_mcp_origin_from_headers(
            {b"x-agent-id": b"antigravity;rm -rf", b"x-host": b"host\nname"}
        )
        == "antigravityrm-rf@hostname"
    )


def test_save_memory_uses_explicit_and_contextvar_origin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    # 1. Explicit origin parameter
    res1 = asyncio.run(
        botex_server.save_memory(
            title="Test Explicit Origin",
            content="Explicit origin body",
            workspace_dir=str(tmp_path),
            scope="local",
            project="swarm",
            origin="devin@host-a",
        )
    )
    assert res1["ok"] is True
    read1 = asyncio.run(
        botex_server.read_memory(
            memory_id=res1["id"],
            workspace_dir=str(tmp_path),
            scope="local",
        )
    )
    assert read1["ok"] is True
    assert read1["metadata"]["source"] == "devin@host-a"

    # 2. ContextVar fallback when origin="" is omitted
    tok = MCP_CLIENT_ORIGIN.set("antigravity@Botek")
    try:
        res2 = asyncio.run(
            botex_server.save_memory(
                title="Test Header Origin",
                content="Header origin body",
                workspace_dir=str(tmp_path),
                scope="local",
                project="swarm",
            )
        )
    finally:
        MCP_CLIENT_ORIGIN.reset(tok)

    assert res2["ok"] is True
    read2 = asyncio.run(
        botex_server.read_memory(
            memory_id=res2["id"],
            workspace_dir=str(tmp_path),
            scope="local",
        )
    )
    assert read2["ok"] is True
    assert read2["metadata"]["source"] == "antigravity@Botek"
    assert get_mcp_client_origin("botex") == "botex"


def test_cross_host_windows_path_detection_and_normalization(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("botex.server.config.os.name", "posix")
    assert is_cross_host_windows_path(r"C:\Users\kubai\.ai\harness") is True
    assert is_cross_host_windows_path("D:/Projects/app") is True
    assert is_cross_host_windows_path("/opt/botex/workspace") is False

    # On POSIX, passing a Windows drive path to resolve_workspace falls back to default/cwd
    # instead of concatenating /opt/botex/C:\Users\...
    monkeypatch.chdir(tmp_path)
    resolved = resolve_workspace(r"C:\Users\kubai\.ai\harness")
    assert "C:" not in resolved


def test_sse_error_guard_injects_no_buffering_header():
    sent_messages = []

    async def dummy_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def capture_send(msg):
        sent_messages.append(msg)

    guard = _MCPSseErrorGuard(dummy_app)
    asyncio.run(
        guard(
            {"type": "http", "path": "/mcp/sse"},
            lambda: None,
            capture_send,
        )
    )
    assert len(sent_messages) == 1
    hdrs = dict(sent_messages[0]["headers"])
    assert hdrs.get(b"x-accel-buffering") == b"no"


def test_cloud_auth_middleware_populates_mcp_client_origin(monkeypatch):
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "tok-test")
    seen_origin = []

    async def inner(scope, receive, send):
        seen_origin.append(MCP_CLIENT_ORIGIN.get(""))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = CloudAuthMiddleware(inner)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp/sse",
        "headers": [
            (b"authorization", b"Bearer tok-test"),
            (b"x-agent-id", b"devin"),
            (b"x-host", b"Botek"),
        ],
    }

    async def _noop_receive():
        return {"type": "http.request", "body": b""}

    async def _noop_send(_msg):
        pass

    asyncio.run(mw(scope, _noop_receive, _noop_send))
    assert seen_origin == ["devin@Botek"]
    assert MCP_CLIENT_ORIGIN.get("") == ""


# ---------------------------------------------------------------------------
# POST /api/memory — server-side origin hardening (PR 3)
# ---------------------------------------------------------------------------


def _post_memory_and_read_source(client, ws: Path, *, payload_extra=None, headers=None) -> str:
    payload = {
        "workspace_dir": str(ws),
        "title": "Origin Probe",
        "content": "provenance check",
        "kind": "context",
        "scope": "local",
    }
    if payload_extra:
        payload.update(payload_extra)
    resp = client.post("/api/memory", json=payload, headers=headers or {})
    assert resp.status_code == 200, resp.text
    mid = resp.json()["id"]
    rd = client.get(f"/api/memory?workspace_dir={ws}&memory_id={mid}&scope=local")
    assert rd.status_code == 200
    entry = rd.json()
    assert entry["ok"] is True
    return str(entry["metadata"].get("source") or "")


@pytest.mark.parametrize("claim", ["human", "operator", "curator", "HUMAN", "attacker@evil"])
def test_post_memory_ignores_client_origin_claim(client, tmp_path: Path, claim: str):
    """body.origin is never trusted: privileged claims are dropped and stored
    provenance always comes from server-side context."""
    source = _post_memory_and_read_source(client, tmp_path, payload_extra={"origin": claim})
    assert source == "cloud_api"
    assert source != "human"


def test_post_memory_records_safe_agent_headers(client, tmp_path: Path):
    """Safe X-Agent-Id/X-Host headers are honored and recorded as 'agent@host'."""
    source = _post_memory_and_read_source(
        client, tmp_path, headers={"X-Agent-Id": "devin", "X-Host": "Botek"}
    )
    assert source == "devin@Botek"
    # A spoofed body claim cannot override header-derived provenance either.
    source2 = _post_memory_and_read_source(
        client,
        tmp_path,
        payload_extra={"origin": "human"},
        headers={"X-Agent-Id": "devin", "X-Host": "Botek"},
    )
    assert source2 == "devin@Botek"


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Agent-Id": "human"},
        {"X-Agent-Id": "operator"},
        {"X-Agent-Id": "curator"},
        {"X-Agent-Id": "curator", "X-Host": "vps"},
    ],
)
def test_post_memory_rejects_reserved_header_claims(client, tmp_path: Path, headers):
    """Reserved identity claims via headers are normalized to 'cloud_api'."""
    source = _post_memory_and_read_source(client, tmp_path, headers=headers)
    assert source == "cloud_api"


def test_sanitize_mcp_client_origin_reserved_and_homoglyph():
    """Unit-level: NFKC folding neutralizes full-width homoglyph claims."""
    assert _sanitize_mcp_client_origin("") == ""
    assert _sanitize_mcp_client_origin("devin@Botek") == "devin@Botek"
    assert _sanitize_mcp_client_origin("devin") == "devin"
    assert _sanitize_mcp_client_origin("botex@vps-01") == "botex@vps-01"
    for claim in (
        "human",
        "HUMAN",
        "ｈｕｍａｎ",  # full-width lookalike folds to ASCII 'human' under NFKC
        "human-curated",
        "operator",
        "curator",
        "curator@vps",
    ):
        assert _sanitize_mcp_client_origin(claim) == "cloud_api"
    assert _normalize_origin_claim("ｈｕｍａｎ@vps") == "human"
    assert _normalize_origin_claim("curator@vps") == "curator"


@pytest.mark.parametrize(
    "raw_agent,expected",
    [
        (b"devin", "devin@Botek"),
        (b"human", "cloud_api"),
        (b"operator", "cloud_api"),
        (b"curator", "cloud_api"),
        # Non-ASCII claim bytes are stripped by the header extractor; the
        # sanitized fallback is the generic 'botex@<host>', never 'human'.
        ("ｈｕｍａｎ".encode("utf-8"), "botex@Botek"),
    ],
)
def test_middleware_sanitizes_api_origin_claims(monkeypatch, raw_agent, expected):
    """CloudAuthMiddleware honors X-Agent-Id/X-Host on /api paths too, but
    reserved identity claims can never survive sanitization."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN_HASH", raising=False)
    seen = []

    async def inner(scope, receive, send):
        seen.append(MCP_CLIENT_ORIGIN.get(""))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = CloudAuthMiddleware(inner)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/memory",
        "client": ("testclient", 50000),
        "headers": [(b"x-agent-id", raw_agent), (b"x-host", b"Botek")],
    }

    async def _noop_receive():
        return {"type": "http.request", "body": b""}

    async def _noop_send(_msg):
        pass

    asyncio.run(mw(scope, _noop_receive, _noop_send))
    assert seen == [expected]
    assert MCP_CLIENT_ORIGIN.get("") == ""
