# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for BoteX shared HTTP client connection pool and provider integration."""

from __future__ import annotations

import asyncio

import httpx2
import pytest

from botex.providers.http_pool import (
    NonClosingAsyncClient,
    close_http_pool,
    get_async_client,
    get_sync_client,
    reset_http_pool,
)
from botex.providers.providers import BaseProvider, OpenRouterProvider


@pytest.fixture(autouse=True)
def clean_pool():
    """Reset pool before and after each test for strict isolation."""
    reset_http_pool()
    yield
    reset_http_pool()


def test_get_sync_client_singleton():
    """Verify get_sync_client returns the same httpx2.Client instance."""
    client1 = get_sync_client()
    client2 = get_sync_client()
    assert client1 is client2
    assert isinstance(client1, httpx2.Client)
    assert not client1.is_closed
    assert client1.timeout.connect == 600.0


def test_get_async_client_singleton():
    """Verify get_async_client returns the same NonClosingAsyncClient instance."""

    async def _run():
        client1 = get_async_client()
        client2 = get_async_client()
        assert client1 is client2
        assert isinstance(client1, NonClosingAsyncClient)
        assert not client1.is_closed
        assert client1.timeout.connect == 600.0

    asyncio.run(_run())


def test_non_closing_async_client_lifecycle():
    """Verify NonClosingAsyncClient ignores aclose() and __aexit__() but honors force_aclose()."""

    async def _run():
        client = get_async_client()
        assert not client.is_closed

        # Simulated OpenAI SDK client.close()
        await client.aclose()
        assert not client.is_closed

        # Simulated async context manager exit and reentrancy idempotency
        async with client as ctx1:
            assert ctx1 is client
            async with client as ctx2:
                assert ctx2 is client
        assert not client.is_closed

        # Explicit teardown
        await client.force_aclose()
        assert client.is_closed

    asyncio.run(_run())


def test_close_http_pool():
    """Verify close_http_pool force-closes async client and closes sync client."""

    async def _run():
        async_client = get_async_client()
        sync_client = get_sync_client()
        assert not async_client.is_closed
        assert not sync_client.is_closed

        await close_http_pool()

        assert async_client.is_closed
        assert sync_client.is_closed

        # Subsequent requests spin up new instances
        new_async = get_async_client()
        new_sync = get_sync_client()
        assert new_async is not async_client
        assert new_sync is not sync_client
        assert not new_async.is_closed
        assert not new_sync.is_closed

    asyncio.run(_run())


def test_reset_http_pool():
    """Verify reset_http_pool synchronously resets singletons."""
    sync_client = get_sync_client()
    assert not sync_client.is_closed

    reset_http_pool()
    assert sync_client.is_closed

    new_sync = get_sync_client()
    new_async = get_async_client()
    assert new_sync is not sync_client
    assert not new_sync.is_closed
    assert not new_async.is_closed


class _MockClientFactory:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_base_provider_get_client_injects_http_client():
    """Verify BaseProvider.get_client injects the shared async client by default."""
    provider = OpenRouterProvider("openrouter")
    client = provider.get_client(
        api_key="test-key",
        client_factory=lambda **kw: _MockClientFactory(**kw),
    )
    pooled_async = get_async_client()
    assert client.kwargs.get("http_client") is pooled_async


def test_base_provider_get_client_respects_custom_http_client():
    """Verify BaseProvider.get_client preserves explicitly injected http_client in kwargs."""
    custom_client = httpx2.AsyncClient()
    provider = BaseProvider("generic", config={"base_url": "https://api.example.com"})
    client = provider.get_client(
        api_key="test-key",
        client_factory=lambda **kw: _MockClientFactory(**kw),
        http_client=custom_client,
    )
    assert client.kwargs.get("http_client") is custom_client


def test_legacy_module_domain_import():
    """Verify backward-compatible import of botex.http_pool via botex.__init__."""
    from botex import http_pool

    assert hasattr(http_pool, "get_async_client")
    assert hasattr(http_pool, "get_sync_client")
