# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Milestone M7 Empirical Challenge & Adversarial Verification Suite
==================================================================
Empirically stress-tests:
1. Streaming secret masking (`mask_stream_chunk`):
   - Boundary split resilience across arbitrary chunk sizes (1, 2, 7, 16, 64, 127, 128, 256).
   - Secret leakage: checks whether any partial secret tokens are emitted in prior chunks.
   - Non-secret preservation: ensures zero corruption, duplication, or truncation of non-secret text.
   - Multi-chunk secret redaction accuracy.
2. SQLite connection pooling and Windows teardown resilience:
   - Lifespan teardown in `server_cloud.py`.
   - Direct `close_index_dbs()` cleanup across multiple threads.
   - `shutil.rmtree` / `TemporaryDirectory` cleanup on Windows without WinError 32.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import List

import pytest

import server
from botex.memory.memory import (
    _ALL_CONNS,
    _CONNS_LOCK,
    _SCHEMA_READY,
    _THREAD_CONNS,
    _get_index_db,
    close_index_dbs,
    reset_memory_index_cache,
    save_memory,
    search_memory,
)
from botex.providers.http_pool import (
    close_http_pool,
    get_async_client,
    get_sync_client,
    reset_http_pool,
)
from botex.server_cloud import app, lifespan
from server import mask_stream_chunk


@pytest.fixture(autouse=True)
def clean_isolation():
    """Reset both HTTP pool and memory caches before and after each test."""
    reset_http_pool()
    reset_memory_index_cache()
    yield
    reset_http_pool()
    reset_memory_index_cache()


# ---------------------------------------------------------------------------
# Dimension 1: Plain Text Preservation (Zero Corruption, Duplication, Truncation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 16, 64, 127, 128, 256])
def test_plain_text_preservation_across_chunk_sizes(chunk_size: int):
    """Verify non-secret text is never corrupted, truncated, or duplicated."""
    plain_text = (
        "The quick brown fox jumps over the lazy dog. "
        "Here are numbers 1234567890 and punctuation !@#$%^&*()_+-=[]{}|;':,./<>? "
        "Special unicode characters: ąęśćłźżół and emojis 🚀🔥🎉. "
        "Multi-line paragraph\nLine 2 with indentation\n    Line 3 with code."
    )

    carry = ""
    emitted: List[str] = []
    for i in range(0, len(plain_text), chunk_size):
        chunk = {"type": "content_chunk", "delta": plain_text[i : i + chunk_size]}
        res, carry = mask_stream_chunk(chunk, carry=carry)
        emitted.append(res["delta"])

    reassembled = "".join(emitted)
    assert reassembled == plain_text, (
        f"Mismatch for chunk_size={chunk_size}:\n"
        f"Expected: {plain_text!r}\n"
        f"Got:      {reassembled!r}"
    )


# ---------------------------------------------------------------------------
# Dimension 2: Boundary Secret Masking Across Chunk Sizes
# ---------------------------------------------------------------------------


SECRET_SAMPLES = [
    ("OpenAI / OpenRouter API Key", "sk-abcdef1234567890fedcba0987654321"),
    ("GitHub Personal Access Token", "ghp_1234567890abcdefghijklmnopqrstuvwxyz"),
    ("AWS Access Key ID", "AKIAIOSFODNN7EXAMPLE"),
    ("Bearer Auth Token", "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdef1234567890"),
]


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 16, 64, 127, 128, 256])
@pytest.mark.parametrize("label,secret", SECRET_SAMPLES)
def test_streaming_masking_boundary_secrets(chunk_size: int, label: str, secret: str):
    """
    Test streaming chunks of varying sizes containing secrets.
    Verifies:
    1. Secret in its entirety is never in the reassembled output.
    2. [REDACTED] appears in the output.
    3. No raw secret fragment of significant length is leaked into emitted deltas.
    """
    prefix = "Authorization header: "
    suffix = " please keep this confidential."
    full_text = prefix + secret + suffix

    carry = ""
    emitted_deltas: List[str] = []
    for i in range(0, len(full_text), chunk_size):
        chunk = {"type": "content_chunk", "delta": full_text[i : i + chunk_size]}
        res, carry = mask_stream_chunk(chunk, carry=carry)
        emitted_deltas.append(res["delta"])

    full_emitted = "".join(emitted_deltas)

    # Invariant 1: The full secret must NEVER appear
    assert secret not in full_emitted, (
        f"Full secret leaked for {label} with chunk_size={chunk_size}!"
    )

    # Invariant 2: [REDACTED] must appear
    assert "[REDACTED]" in full_emitted, (
        f"[REDACTED] missing for {label} with chunk_size={chunk_size}!"
    )

    # Invariant 3: Check for partial secret leaks (e.g., prefix leaked in earlier chunks)
    # If the secret has length >= 12, leaking >= 8 chars is a serious information disclosure
    for k in range(8, len(secret)):
        sub = secret[:k]
        if sub in full_emitted:
            pytest.fail(
                f"Partial secret leak detected for {label} (chunk_size={chunk_size})!\n"
                f"Leaked fragment: {sub!r}\n"
                f"Full emitted stream: {full_emitted!r}"
            )


@pytest.mark.parametrize("split_pos", [2, 3, 5, 8, 12, 16, 20])
def test_two_chunk_arbitrary_split(split_pos: int):
    """Test splitting an API key across exactly two consecutive chunks at various split offsets."""
    secret = "sk-1234567890abcdef1234567890"
    prefix = "Secret key: "
    suffix = " (end of message)"
    full_text = prefix + secret + suffix

    # Split point inside secret
    split_index = len(prefix) + split_pos
    chunk1_text = full_text[:split_index]
    chunk2_text = full_text[split_index:]

    chunk1 = {"type": "content_chunk", "delta": chunk1_text}
    res1, carry1 = mask_stream_chunk(chunk1, carry="")

    chunk2 = {"type": "content_chunk", "delta": chunk2_text}
    res2, carry2 = mask_stream_chunk(chunk2, carry=carry1)

    combined = res1["delta"] + res2["delta"]

    # Verify secret is not present
    assert secret not in combined

    # Check whether chunk1 leaked part of the secret
    secret_part1 = full_text[len(prefix) : split_index]
    if len(secret_part1) >= 4 and secret_part1 in res1["delta"]:
        pytest.fail(
            f"Chunk 1 leaked partial secret at split_pos={split_pos}!\n"
            f"Chunk 1 emitted: {res1['delta']!r}\n"
            f"Secret part was: {secret_part1!r}"
        )


# ---------------------------------------------------------------------------
# Dimension 3: Lifespan Teardown & Windows SQLite Handle Resilience
# ---------------------------------------------------------------------------


def test_lifespan_teardown_cleans_sqlite_and_http():
    """Verify lifespan context manager cleanly closes HTTP pool and all SQLite handles."""

    async def _run():
        # Spin up temp dir with memory operations
        temp_dir = tempfile.mkdtemp(prefix="botex_lifespan_test_")
        try:
            # 1. Use async and sync http clients
            a_client = get_async_client()
            s_client = get_sync_client()
            assert not a_client.is_closed
            assert not s_client.is_closed

            # 2. Enter lifespan
            async with lifespan(app):
                # Perform memory operations to open pooled SQLite connections
                res = save_memory(
                    workspace_dir=temp_dir,
                    title="Lifespan test note",
                    content="Testing SQLite connection pooling in lifespan.",
                    kind="context",
                )
                assert res.get("ok") is True

                search_res = search_memory(
                    workspace_dir=temp_dir,
                    query="pooling",
                )
                assert len(search_res.get("memories", [])) >= 1

                # Verify connections are actively held
                with _CONNS_LOCK:
                    assert len(_ALL_CONNS) > 0

            # 3. Post-lifespan: verify HTTP pool is closed
            assert a_client.is_closed
            assert s_client.is_closed

            # 4. Post-lifespan: verify all SQLite connections were force closed
            with _CONNS_LOCK:
                assert len(_ALL_CONNS) == 0

            # 5. On Windows, verify we can delete the temp directory without WinError 32
            shutil.rmtree(temp_dir)
            assert not os.path.exists(temp_dir)

        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    asyncio.run(_run())


def test_multithreaded_sqlite_teardown_windows_rmtree():
    """Verify multi-threaded SQLite connections across threads are cleanly disposed by close_index_dbs()."""
    temp_dir = tempfile.mkdtemp(prefix="botex_multithread_test_")
    try:
        errors = []

        def worker(idx: int):
            try:
                save_memory(
                    workspace_dir=temp_dir,
                    title=f"Note {idx}",
                    content=f"Content for note {idx}",
                    kind="context",
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

        # Connections should exist across threads
        with _CONNS_LOCK:
            active_count = len(_ALL_CONNS)
            assert active_count >= 1

        # Force close all index dbs
        close_index_dbs()

        with _CONNS_LOCK:
            assert len(_ALL_CONNS) == 0

        # On Windows, attempting to delete open SQLite file without closing raises PermissionError [WinError 32].
        # Verify it deletes cleanly now:
        shutil.rmtree(temp_dir)
        assert not os.path.exists(temp_dir)

    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Dimension 4: Adversarial Literal [REDACTED] and Granular Prefix Verification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 16, 64])
def test_literal_redaction_tag_preservation_across_chunk_sizes(chunk_size: int):
    """Verify literal [REDACTED] in text is never corrupted or duplicated across chunk boundaries."""
    plain = "This is [REDACTED] text"
    carry = ""
    emitted: List[str] = []
    for i in range(0, len(plain), chunk_size):
        chunk = {"type": "content_chunk", "delta": plain[i : i + chunk_size]}
        res, carry = mask_stream_chunk(chunk, carry=carry)
        emitted.append(res["delta"])

    reassembled = "".join(emitted)
    assert reassembled == plain, f"Mismatch for chunk_size={chunk_size}: got {reassembled!r}"


ADVERSARIAL_PREFIX_SECRETS = [
    ("GitHub PAT", "github_pat_11AAAAAAA0abcdef1234567890"),
    ("Config password", "password=supersecretpassword123"),
    ("Stripe live", "sk_live_abcdef1234567890fedcba"),
    ("Bearer token", "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdef1234567890"),
    ("GitLab PAT", "glpat-12345678901234567890"),
]


@pytest.mark.parametrize("label,secret", ADVERSARIAL_PREFIX_SECRETS)
def test_granular_secret_prefix_leakage_1char_streaming(label: str, secret: str):
    """Verify 1-char streaming of secrets with long prefixes leaks zero unmasked characters."""
    text = "Key: " + secret + " end"
    carry = ""
    emitted: List[str] = []
    for ch in text:
        chunk = {"type": "content_chunk", "delta": ch}
        res, carry = mask_stream_chunk(chunk, carry=carry)
        emitted.append(res["delta"])

    reassembled = "".join(emitted)
    assert secret not in reassembled
    assert "[REDACTED]" in reassembled
    assert reassembled == "Key: [REDACTED] end"

    for k in range(3, len(secret)):
        sub = secret[:k]
        assert sub not in reassembled, f"{label}: Leaked prefix {sub!r} in {reassembled!r}"


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 16])
def test_mixed_literal_and_secret_redactions_across_chunk_sizes(chunk_size: int):
    """Verify text containing both literal [REDACTED] and active secrets preserves literals while redacting secrets."""
    mixed = (
        "Prior turn said [REDACTED]. Now key github_pat_11AAAAAAA0abcdef1234567890 "
        "and password=supersecretpassword123."
    )
    expected = "Prior turn said [REDACTED]. Now key [REDACTED] and [REDACTED]"
    carry = ""
    emitted: List[str] = []
    for i in range(0, len(mixed), chunk_size):
        chunk = {"type": "content_chunk", "delta": mixed[i : i + chunk_size]}
        res, carry = mask_stream_chunk(chunk, carry=carry)
        emitted.append(res["delta"])

    reassembled = "".join(emitted)
    assert reassembled == expected, f"Mismatch at cs={chunk_size}: got {reassembled!r}"
