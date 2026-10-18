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
