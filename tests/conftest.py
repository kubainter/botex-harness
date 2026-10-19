# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Global pytest fixtures for hermetic test isolation."""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Ensure no live network calls can be made anywhere in the test suite.

    Allows internal asyncio socketpair / loopback communication on Windows.
    """
    orig_connect = socket.socket.connect

    def blocked_connect(self: Any, *args: Any, **kwargs: Any) -> Any:
        import inspect

        frame = inspect.currentframe()
        try:
            curr = frame.f_back if frame else None
            while curr:
                if (
                    curr.f_code.co_name in ("_fallback_socketpair", "socketpair")
                    or "asyncio" in curr.f_code.co_filename
                ):
                    addr = args[0] if args else kwargs.get("address")
                    if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
                        return orig_connect(self, *args, **kwargs)
                curr = curr.f_back
        finally:
            del frame

        raise RuntimeError("Blocked live network socket call during unit test execution")

    with patch.object(socket.socket, "connect", blocked_connect):
        yield


@pytest.fixture(autouse=True)
def isolate_botex_test_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    """Hermetically isolate filesystem state during test execution.

    Redirects snapshots, analytics, catalog cache, benchmarks cache, chat sessions,
    and global memory away from developer environment and git working tree into tmp_path.
    """
    from pathlib import Path

    t_path = Path(str(tmp_path))
    snapshots_dir = t_path / ".test_env_snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    analytics_file = t_path / ".test_env_analytics.json"
    catalog_file = t_path / ".test_env_pricing.json"
    benchmarks_file = t_path / ".test_env_benchmarks.json"
    chat_sessions_dir = t_path / ".test_env_chat_sessions"
    chat_sessions_dir.mkdir(parents=True, exist_ok=True)
    global_memory_dir = t_path / ".test_env_global_memory"
    global_memory_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("BOTEX_SNAPSHOT_DIR", str(snapshots_dir))
    monkeypatch.setenv("BOTEX_ANALYTICS_FILE", str(analytics_file))
    monkeypatch.setenv("BOTEX_CATALOG_CACHE_FILE", str(catalog_file))
    monkeypatch.setenv("BOTEX_BENCHMARKS_CACHE_FILE", str(benchmarks_file))
    monkeypatch.setenv("BOTEX_CHAT_SESSIONS_DIR", str(chat_sessions_dir))
    monkeypatch.setenv("BOTEX_GLOBAL_MEMORY_DIR", str(global_memory_dir))

    try:
        from botex.config import load_config

        load_config(reload=True)
    except Exception:
        pass

    yield
