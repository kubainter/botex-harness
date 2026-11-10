# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Opaque-Box E2E Contracts Test Suite for BoteX Framework Enhancement
===================================================================
Comprehensive requirements-driven verification covering:
- PR-1: Core & Backend Performance (HTTP connection pool, SQLite pooling, DDL memoization,
  lifespan teardown, carry-buffer stream secret masking, uvloop packaging).
- PR-2: Backend Catalog & Wallet Contracts (Z.ai catalog seeding & GLM isolation from
  openrouter-free, CreditsInfo dataclass extensions, ZaiGLMProvider quota window normalizer,
  Cloud Server /api/wallet JSON contract).
- PR-3: Cross-system contract invariants (wallet service serialization, model routing).

Organized across 4 verification tiers:
- Tier 1: Feature Coverage (primary happy paths for each subsystem)
- Tier 2: Boundary & Corner Cases (split secrets, timestamp variations, unit maps, zero quotas)
- Tier 3: Cross-Feature Interactions (HTTP pool + retry middleware, concurrent multithreaded DB,
  wallet serialization with secret masking)
- Tier 4: Realistic Application Workloads (full simulated lifecycle from server lifespan startup
  to chat token streaming, memory vault persistence, and shutdown resource reclamation).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import httpx2
import pytest


def _mock_http_response(status: int = 200, payload: Optional[Dict[str, Any]] = None) -> MagicMock:
    """Create a mock context manager for urllib.request.urlopen."""
    body_bytes = json.dumps(payload or {}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body_bytes
    mock_resp.status = status
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None
    return mock_resp


# ===========================================================================
# Tier 1: Feature Coverage
# ===========================================================================


class TestTier1FeatureCoverage:
    """Primary behavior (happy path) verification for PR-1 and PR-2 features."""

    @pytest.mark.anyio
    async def test_http_pool_non_closing_client_resists_aclose_and_force_closes(self):
        """Verify NonClosingAsyncClient ignores aclose() / __aexit__() but closes on force_aclose()."""
        from botex.providers.http_pool import NonClosingAsyncClient

        client = NonClosingAsyncClient()
        assert not client.is_closed

        # Calling standard aclose() must be a no-op to resist premature disposal by OpenAI SDK
        await client.aclose()
        assert not client.is_closed

        # Async context manager exit must also be a no-op
        await client.__aexit__(None, None, None)
        assert not client.is_closed

        # Explicit force_aclose() must actually tear down the client
        await client.force_aclose()
        assert client.is_closed

    def test_http_pool_singletons_and_reset(self):
        """Verify get_async_client() and get_sync_client() return singletons and reset_http_pool() works."""
        from botex.providers.http_pool import (
            NonClosingAsyncClient,
            get_async_client,
            get_sync_client,
            reset_http_pool,
        )

        try:
            reset_http_pool()
            c1 = get_async_client()
            c2 = get_async_client()
            assert c1 is c2
            assert isinstance(c1, NonClosingAsyncClient)

            s1 = get_sync_client()
            s2 = get_sync_client()
            assert s1 is s2
            assert isinstance(s1, httpx2.Client)

            # Resetting pool must drop the singletons
            reset_http_pool()
            c3 = get_async_client()
            assert c3 is not c1
        finally:
            reset_http_pool()

    def test_memory_pool_connection_reuse_and_close_noop(self, tmp_path: Path):
        """Verify _PooledConnection close() is a no-op, connections are reused, and force_close() closes."""
        from botex.memory.memory import (
            _get_index_db,
            _PooledConnection,
            reset_memory_index_cache,
        )

        try:
            conn1 = _get_index_db(tmp_path)
            assert isinstance(conn1, _PooledConnection)

            # Standard close() must be a no-op
            conn1.close()
            row = conn1.execute("SELECT 1 as val").fetchone()
            assert row["val"] == 1

            # Subsequent call in same thread returns the cached connection
            conn2 = _get_index_db(tmp_path)
            assert conn2 is conn1

            # force_close() must genuinely close the SQLite connection
            conn1.force_close()
            with pytest.raises(sqlite3.ProgrammingError):
                conn1.execute("SELECT 1")
        finally:
            reset_memory_index_cache()

    def test_memory_pool_close_index_dbs_cleanup(self, tmp_path: Path):
        """Verify close_index_dbs() cleanly closes all connections tracked across threads."""
        from botex.memory.memory import (
            _get_index_db,
            close_index_dbs,
            reset_memory_index_cache,
        )

        try:
            conn = _get_index_db(tmp_path)
            row = conn.execute("SELECT 42 as answer").fetchone()
            assert row["answer"] == 42

            # Global lifecycle close
            close_index_dbs()

            # Connection must now be closed
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        finally:
            reset_memory_index_cache()

    def test_memory_ddl_memoization_caches_schema(self, tmp_path: Path):
        """Verify _SCHEMA_READY memoizes schema migrations and skips redundant DDL queries."""
        from botex.memory.memory import (
            _get_index_db,
            _SCHEMA_LOCK,
            _SCHEMA_READY,
            reset_memory_index_cache,
        )

        try:
            reset_memory_index_cache()
            db_key = str((tmp_path / "index.db").resolve())

            with _SCHEMA_LOCK:
                assert db_key not in _SCHEMA_READY

            conn = _get_index_db(tmp_path)
            assert conn is not None

            with _SCHEMA_LOCK:
                assert db_key in _SCHEMA_READY

            # Inspect that tables exist
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            assert "memories_meta" in tables
            assert "memories_edges" in tables

            # Second call must reuse memoized schema without crashing
            conn2 = _get_index_db(tmp_path)
            assert conn2 is conn
        finally:
            reset_memory_index_cache()

    def test_streaming_secret_masking_basic_redaction(self):
        """Verify mask_stream_chunk redacts secrets in token streaming chunks."""
        from server import mask_stream_chunk

        chunk = {
            "type": "content_chunk",
            "delta": "Your API key is sk-proj-1234567890abcdef1234567890 please keep it safe.",
            "accumulated": "Your API key is sk-proj-1234567890abcdef1234567890",
        }
        masked, carry = mask_stream_chunk(chunk, carry="")
        assert masked["type"] == "content_chunk"
        assert "sk-proj-1234567890abcdef1234567890" not in masked["delta"]
        assert "sk-proj-1234567890abcdef1234567890" not in masked["accumulated"]
        assert "Your API key is " in masked["delta"]
        assert " please keep it safe." in masked["delta"]

    def test_catalog_seeding_zai_models_isolated_from_openrouter_free(self):
        """Verify GLM models are not present under 'openrouter-free' and 'zai' is not an active seeded provider."""
        from botex.providers.catalog import get_consolidated_models

        models = get_consolidated_models(
            providers=("openrouter-free", "zai"),
            force_refresh=True,
        )

        openrouter_free_models = [m for m in models if m.get("provider") == "openrouter-free"]
        free_ids = {m.get("id") for m in openrouter_free_models}
        assert "glm-5.3" not in free_ids
        assert "glm-5.3-flash" not in free_ids

        # GLM models and zai provider must NOT be active seeded models
        zai_models = [m for m in models if m.get("provider") == "zai"]
        assert len(zai_models) == 0

    def test_credits_info_zai_quota_structure(self):
        """Verify CreditsInfo has units='quota' and quota_windows support for quota-based providers."""
        from botex.providers.providers import CreditsInfo

        sample_quota_windows = (
            {
                "label": "5 Hours",
                "unit": "hours",
                "number": 5,
                "limit": 1000000.0,
                "current_usage": 150000.0,
                "percentage": 15.0,
                "next_reset_time": "2026-11-06T18:00:00Z",
            },
            {
                "label": "Weekly",
                "unit": "weeks",
                "number": 1,
                "limit": 5000000.0,
                "current_usage": 1500000.0,
                "percentage": 30.0,
                "next_reset_time": "2026-11-13T00:00:00Z",
            },
        )
        info = CreditsInfo(
            supported=True,
            provider="quota_provider",
            units="quota",
            quota_windows=sample_quota_windows,
        )

        assert info.supported is True
        assert info.provider == "quota_provider"
        assert info.units == "quota"
        assert isinstance(info.quota_windows, tuple)
        assert len(info.quota_windows) == 2

        w_5h = next(w for w in info.quota_windows if w["number"] == 5 and w["unit"] == "hours")
        assert w_5h["limit"] == 1000000.0
        assert w_5h["current_usage"] == 150000.0
        assert w_5h["percentage"] == 15.0

        w_week = next(w for w in info.quota_windows if w["number"] == 1 and w["unit"] == "weeks")
        assert w_week["limit"] == 5000000.0
        assert w_week["current_usage"] == 1500000.0
        assert w_week["percentage"] == 30.0

    def test_wallet_api_json_response_contract(self):
        """Verify GET /api/wallet JSON response contains units and quota_windows fields."""
        from botex.providers.providers import CreditsInfo
        from botex.server.server_cloud import app

        sample_quota_windows = (
            {
                "label": "5 Hours",
                "unit": "hours",
                "number": 5,
                "limit": 1000000.0,
                "current_usage": 150000.0,
                "percentage": 15.0,
                "next_reset_time": "2026-11-06T18:00:00Z",
            },
        )
        mock_credits = CreditsInfo(
            supported=True,
            provider="zai",
            units="quota",
            quota_windows=sample_quota_windows,
        )

        with patch(
            "botex.server.server_cloud.default_wallet_service.get_credits_async",
            new=AsyncMock(return_value=mock_credits),
        ):
            with TestClient(app) as client:
                resp = client.get("/api/wallet?provider=zai&force_refresh=true")
                assert resp.status_code == 200
                data = resp.json()
                assert data["supported"] is True
                assert data["provider"] == "zai"
                assert data["units"] == "quota"
                assert "quota_windows" in data
                assert len(data["quota_windows"]) == 1
                assert data["quota_windows"][0]["unit"] == "hours"

    def test_uvloop_optional_dependency_packaging(self):
        """Verify pyproject.toml defines optional dependency 'perf' with uvloop for non-win32 platforms."""
        pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
        assert pyproject_path.is_file(), "pyproject.toml must exist in project root"

        content = pyproject_path.read_text(encoding="utf-8")
        assert "uvloop>=0.19" in content, (
            "uvloop>=0.19 must be specified under optional dependencies"
        )
        assert 'sys_platform != "win32"' in content or "sys_platform != 'win32'" in content, (
            "uvloop dependency must be guarded by sys_platform != 'win32'"
        )


# ===========================================================================
# Tier 2: Boundary & Corner Cases
# ===========================================================================


class TestTier2BoundaryAndCornerCases:
    """Boundary conditions, split inputs, and parsing edge cases."""

    def test_split_secret_across_stream_chunks_with_carry_buffer(self):
        """Verify a secret split across chunk boundary is completely redacted via carry buffer."""
        from server import mask_stream_chunk

        # Split OpenAI-format key across boundary
        prefix = "sk-proj-"
        suffix = "SEC1234567890abcdef1234567890xyz"
        full_secret = prefix + suffix

        chunk1 = {
            "type": "content_chunk",
            "delta": f"Prefix text before secret {prefix}",
        }
        chunk2 = {
            "type": "content_chunk",
            "delta": f"{suffix} suffix text after secret.",
        }

        masked1, carry1 = mask_stream_chunk(chunk1, carry="", buffer_size=128)
        masked2, carry2 = mask_stream_chunk(chunk2, carry=carry1, buffer_size=128)

        combined_emitted = masked1["delta"] + masked2["delta"]

        # The full secret string MUST NEVER appear in emitted tokens
        assert full_secret not in combined_emitted
        assert suffix not in combined_emitted

        # Non-secret context text must be preserved
        assert "Prefix text before secret " in combined_emitted
        assert " suffix text after secret." in combined_emitted

    def test_zai_timestamp_parsing_formats(self):
        """Verify _normalize_reset_time handles ms-epoch, sec-epoch, and ISO strings gracefully."""
        from botex.providers.adapters.zai import _normalize_reset_time

        # Milliseconds epoch (timestamp > 1e11)
        ms_epoch = 1794074400000
        res_ms = _normalize_reset_time(ms_epoch)
        assert res_ms is not None
        assert "Z" in res_ms or "+00:00" in res_ms
        assert res_ms.startswith("2026-")

        # Seconds epoch (timestamp < 1e11)
        sec_epoch = 1794074400
        res_sec = _normalize_reset_time(sec_epoch)
        assert res_sec == res_ms

        # String representation of ms epoch
        res_str_ms = _normalize_reset_time("1794074400000")
        assert res_str_ms == res_ms

        # Pre-formatted ISO string
        iso_str = "2026-11-06T18:00:00Z"
        assert _normalize_reset_time(iso_str) == iso_str

        # None and empty inputs
        assert _normalize_reset_time(None) is None
        assert _normalize_reset_time("") is None

    def test_window_units_mapping_boundaries(self):
        """Verify _WINDOW_UNITS_MAP correctly maps units 3 -> hours, 6 -> weeks, 5 -> months."""
        from botex.providers.adapters.zai import _WINDOW_UNITS_MAP, _normalize_quota_window

        assert _WINDOW_UNITS_MAP[3] == "hours"
        assert _WINDOW_UNITS_MAP[5] == "months"
        assert _WINDOW_UNITS_MAP[6] == "weeks"

        # Test normalization handles unknown unit code gracefully
        unknown_item = {
            "unit": 99,
            "number": 2,
            "limit": 100,
            "current_usage": 10,
        }
        norm = _normalize_quota_window(unknown_item)
        assert norm["unit"] in ("99", "unknown")
        assert norm["limit"] == 100.0
        assert norm["current_usage"] == 10.0

    def test_credits_info_default_backward_compatibility(self):
        """Verify CreditsInfo defaults to units='usd' and quota_windows=() for legacy callers."""
        from botex.providers.providers import CreditsInfo

        # Legacy caller without units or quota_windows
        info = CreditsInfo(
            supported=True,
            provider="openrouter",
            balance_usd=12.34,
            usage_usd=5.67,
        )

        assert info.units == "usd"
        assert info.quota_windows == ()
        assert info.balance_usd == 12.34

        # Frozen dataclass invariant
        with pytest.raises((TypeError, AttributeError)):
            info.units = "other"  # type: ignore

    def test_zero_or_none_quota_windows_handling(self):
        """Verify quota normalization handles zero limits, None values, and missing fields safely."""
        from botex.providers.adapters.zai import _normalize_quota_window

        # Zero limit: must not raise ZeroDivisionError
        zero_lim_item = {
            "unit": 3,
            "number": 5,
            "limit": 0,
            "current_usage": 0,
        }
        norm_zero = _normalize_quota_window(zero_lim_item)
        assert norm_zero["limit"] == 0.0
        assert norm_zero["percentage"] in (0.0, None)
        assert norm_zero["remaining"] == 0.0

        # None limit with usage
        none_lim_item = {
            "unit": 6,
            "number": 1,
            "limit": None,
            "currentUsage": 500,
        }
        norm_none = _normalize_quota_window(none_lim_item)
        assert norm_none["limit"] is None
        assert norm_none["current_usage"] == 500.0
        assert norm_none["percentage"] is None


# ===========================================================================
# Tier 3: Cross-Feature Interactions
# ===========================================================================


class TestTier3CrossFeatureInteractions:
    """Interactions between subsystems: HTTP pool injection, multithreaded DB, wallet serialization."""

    def test_http_pool_injection_into_base_provider_with_retry(self):
        """Verify BaseProvider.get_client injects get_async_client() and sets max_retries=0 with retry."""
        from botex.providers.http_pool import NonClosingAsyncClient, get_async_client
        from botex.providers.providers import BaseProvider

        class DummyProvider(BaseProvider):
            def __init__(self):
                super().__init__(
                    name="dummy",
                    config={
                        "base_url": "https://dummy.example/v1",
                        "retry": {"enabled": True},
                    },
                )

        captured_kwargs: Dict[str, Any] = {}

        def mock_client_factory(**kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        prov = DummyProvider()
        prov.get_client(
            api_key="dummy-key",
            client_factory=mock_client_factory,
        )

        assert "http_client" in captured_kwargs
        assert isinstance(captured_kwargs["http_client"], NonClosingAsyncClient)
        assert captured_kwargs["http_client"] is get_async_client()
        assert captured_kwargs.get("max_retries") == 0

    def test_memory_vault_multithreaded_concurrency_and_pool_sharing(self, tmp_path: Path):
        """Verify multiple threads querying Memory Vault concurrently share connection pool safely."""
        from botex.memory.memory import (
            _get_index_db,
            close_index_dbs,
            reset_memory_index_cache,
        )

        num_threads = 6
        results: List[int] = []
        errors: List[Exception] = []

        def worker_fn(thread_id: int):
            try:
                conn = _get_index_db(tmp_path)
                # Execute table creation / read with valid schema columns (source_id, target_id, kind)
                conn.execute(
                    "INSERT INTO memories_edges (source_id, target_id, kind) VALUES (?, ?, ?)",
                    (f"src_{thread_id}", f"tgt_{thread_id}", "cites"),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM memories_edges WHERE source_id=?",
                    (f"src_{thread_id}",),
                ).fetchone()
                results.append(row["cnt"])
            except Exception as e:
                errors.append(e)

        try:
            reset_memory_index_cache()
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                futures = [executor.submit(worker_fn, i) for i in range(num_threads)]
                for f in futures:
                    f.result(timeout=10.0)

            assert len(errors) == 0, f"Thread errors encountered: {errors}"
            assert len(results) == num_threads
            assert all(r == 1 for r in results)
        finally:
            close_index_dbs()
            reset_memory_index_cache()

    def test_wallet_service_serialization_with_masked_secrets_and_quota(self):
        """Verify wallet service serialization preserves quota windows and masks embedded secret tokens."""
        from botex.providers.providers import CreditsInfo
        from botex.providers.wallet import WalletService

        secret_label = "Key sk-proj-1234567890abcdef1234567890"
        secret_error = "Error with sk-ant-api03-1234567890abcdef1234567890"

        sample_windows = (
            {
                "label": "5 Hours",
                "unit": "hours",
                "number": 5,
                "limit": 1000000.0,
                "current_usage": 200000.0,
                "percentage": 20.0,
                "next_reset_time": "2026-11-06T18:00:00Z",
            },
        )

        info = CreditsInfo(
            supported=True,
            provider="zai",
            units="quota",
            quota_windows=sample_windows,
            label=secret_label,
            error=secret_error,
        )

        formatted = WalletService._format_key_status_dict(info, low_budget_threshold_usd=1.0)

        assert formatted["units"] == "quota"
        assert len(formatted["quota_windows"]) == 1
        assert formatted["quota_windows"][0]["unit"] == "hours"

        # Quota accounts must not trigger low_budget_warning
        assert formatted["low_budget_warning"] is False

        # Embedded secrets must be masked
        assert "sk-proj-1234567890abcdef1234567890" not in str(formatted["label"])
        assert "sk-ant-api03-1234567890abcdef1234567890" not in str(formatted["error"])


# ===========================================================================
# Tier 4: Realistic Application Workloads
# ===========================================================================


class TestTier4RealisticApplicationWorkloads:
    """Full simulated application lifecycles exercising end-to-end paths."""

    @pytest.mark.anyio
    async def test_full_server_application_lifecycle(self, tmp_path: Path):
        """Simulate complete server lifecycle: startup, pooled client retrieval, streaming chat,

        database operations, and shutdown resource cleanup without Windows file lock leaks.
        """
        from botex.memory.memory import (
            _get_index_db,
            close_index_dbs,
            reset_memory_index_cache,
        )
        from botex.providers.http_pool import (
            close_http_pool,
            get_async_client,
            reset_http_pool,
        )
        from server import mask_stream_chunk

        try:
            # 1. Lifespan Startup & Warmup
            reset_http_pool()
            reset_memory_index_cache()

            async_client = get_async_client()
            assert not async_client.is_closed

            # 2. Simulated Chat Streaming Turn with Secret Masking
            chunks = [
                {"type": "content_chunk", "delta": "Analysis started. Using credential "},
                {"type": "content_chunk", "delta": "sk-proj-1234567890abcdef1234567890"},
                {"type": "content_chunk", "delta": " for model completion."},
                {"type": "done", "total_tokens": 42},
            ]

            emitted_deltas: List[str] = []
            carry = ""
            for c in chunks:
                masked_chunk, carry = mask_stream_chunk(c, carry=carry)
                if masked_chunk.get("type") == "content_chunk":
                    emitted_deltas.append(masked_chunk["delta"])

            full_emitted = "".join(emitted_deltas)
            assert "sk-proj-1234567890abcdef1234567890" not in full_emitted
            assert "Analysis started. Using credential " in full_emitted
            assert " for model completion." in full_emitted

            # 3. Database Operations with SQLite Connection Pooling
            conn = _get_index_db(tmp_path)
            conn.execute(
                "INSERT INTO memories_meta (id, title, kind, tags, links, timestamp, status, compiled_truth, confidence, mtime) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "mem_1",
                    "E2E Test Note",
                    "architecture",
                    "test",
                    "",
                    "2026-11-06T18:00:00Z",
                    "active",
                    0,
                    1.0,
                    1794074400.0,
                ),
            )
            conn.commit()

            # Verify query succeeds
            row = conn.execute("SELECT title FROM memories_meta WHERE id='mem_1'").fetchone()
            assert row["title"] == "E2E Test Note"

            # 4. Lifespan Teardown
            await close_http_pool()
            close_index_dbs()

            # 5. Post-Shutdown Verification
            assert async_client.is_closed

            # Verify SQLite connections are closed and file locks released
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

            # On Windows, locked files raise PermissionError if reopened exclusively or unlinked
            db_file = tmp_path / "index.db"
            if db_file.exists():
                with open(db_file, "r+b") as f:
                    assert f.readable()

        finally:
            reset_http_pool()
            reset_memory_index_cache()
