# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Shared HTTP Client Connection Pool
=========================================
Provides shared, thread-safe httpx2 client singletons for synchronous and
asynchronous LLM provider calls and network fetches.

Key features:
1. NonClosingAsyncClient facade protects the pooled AsyncClient against premature
   client disposal caused by AsyncOpenAI.close() / client.close() per chat turn.
2. Lazy initialization under threading.Lock.
3. 600.0s request timeout for long LLM reasoning chains.
4. HTTP connection limits (max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0).
5. Clean lifecycle management: close_http_pool() during server shutdown and
   reset_http_pool() for test isolation.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

import httpx2

logger = logging.getLogger(__name__)

__all__ = [
    "NonClosingAsyncClient",
    "get_async_client",
    "get_sync_client",
    "close_http_pool",
    "reset_http_pool",
]

# Per-phase timeouts: connect must stay short (a blackholed endpoint must not
# hold a pool slot for 10 min), while read/write/pool tolerate long LLM
# reasoning chains.
_DEFAULT_TIMEOUT = httpx2.Timeout(connect=10.0, read=600.0, write=600.0, pool=600.0)
_DEFAULT_LIMITS = httpx2.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)

_HTTP_POOL_LOCK = threading.Lock()
_ASYNC_CLIENT: Optional[NonClosingAsyncClient] = None
_SYNC_CLIENT: Optional[httpx2.Client] = None


class NonClosingAsyncClient(httpx2.AsyncClient):
    """AsyncClient facade ignoring client.close() and __aexit__() from OpenAI SDK."""

    async def __aenter__(self) -> NonClosingAsyncClient:
        """Return self for context manager reentrancy idempotency."""
        return self

    async def aclose(self) -> None:
        """No-op to prevent AsyncOpenAI from closing the shared pool client."""
        pass

    async def __aexit__(self, exc_type=None, exc_val=None, exc_tb=None) -> None:
        """No-op context manager exit."""
        pass

    async def force_aclose(self) -> None:
        """Perform the actual client disposal during server teardown."""
        await super().aclose()


def get_async_client() -> NonClosingAsyncClient:
    """Return the shared asynchronous HTTP client singleton."""
    global _ASYNC_CLIENT
    with _HTTP_POOL_LOCK:
        if _ASYNC_CLIENT is None or _ASYNC_CLIENT.is_closed:
            _ASYNC_CLIENT = NonClosingAsyncClient(
                timeout=_DEFAULT_TIMEOUT,
                limits=_DEFAULT_LIMITS,
            )
        return _ASYNC_CLIENT


def get_sync_client() -> httpx2.Client:
    """Return the shared synchronous HTTP client singleton."""
    global _SYNC_CLIENT
    with _HTTP_POOL_LOCK:
        if _SYNC_CLIENT is None or _SYNC_CLIENT.is_closed:
            _SYNC_CLIENT = httpx2.Client(
                timeout=_DEFAULT_TIMEOUT,
                limits=_DEFAULT_LIMITS,
            )
        return _SYNC_CLIENT


async def close_http_pool() -> None:
    """Force-close all pooled HTTP clients during server shutdown."""
    global _ASYNC_CLIENT, _SYNC_CLIENT
    with _HTTP_POOL_LOCK:
        async_client = _ASYNC_CLIENT
        sync_client = _SYNC_CLIENT
        _ASYNC_CLIENT = None
        _SYNC_CLIENT = None

    if async_client is not None and not async_client.is_closed:
        try:
            await async_client.force_aclose()
        except Exception:
            logger.exception("Error closing shared async HTTP client")

    if sync_client is not None and not sync_client.is_closed:
        try:
            sync_client.close()
        except Exception:
            logger.exception("Error closing shared sync HTTP client")


def reset_http_pool() -> None:
    """Synchronously drop and close pool singletons for test isolation."""
    global _ASYNC_CLIENT, _SYNC_CLIENT
    with _HTTP_POOL_LOCK:
        async_client = _ASYNC_CLIENT
        sync_client = _SYNC_CLIENT
        _ASYNC_CLIENT = None
        _SYNC_CLIENT = None

    if async_client is not None and not async_client.is_closed:
        try:
            asyncio.run(async_client.force_aclose())
        except RuntimeError:
            # A running event loop owns this thread — asyncio.run() cannot be
            # nested; the caller must use close_http_pool() instead.
            logger.warning(
                "reset_http_pool() skipped async client close inside a running event loop"
            )
        except Exception:
            logger.exception("Error closing async HTTP client during pool reset")

    if sync_client is not None and not sync_client.is_closed:
        try:
            sync_client.close()
        except Exception:
            logger.exception("Error closing sync HTTP client during pool reset")
