# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for F4 Pipe A: MCP origin provenance, X-Agent-Id/X-Host headers,
Windows-on-POSIX workspace path normalization, and SSE X-Accel-Buffering."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import server as botex_server
from botex.config import (
    MCP_CLIENT_ORIGIN,
    get_mcp_client_origin,
    is_cross_host_windows_path,
    resolve_workspace,
)
from botex.server.server_cloud import (
    _MCPSseErrorGuard,
    _extract_mcp_origin_from_headers,
)


def test_extract_mcp_origin_from_headers():
    assert (
        _extract_mcp_origin_from_headers(
            {b"x-agent-id": b"devin", b"x-host": b"Botek"}
        )
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
