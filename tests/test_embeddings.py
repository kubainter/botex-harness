# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for botex/embeddings.py."""

import math
import pytest
from botex import embeddings


@pytest.mark.anyio
async def test_embed_text_default_dim():
    """Verify single text embedding produces normalized vector of default width (256)."""
    vec = await embeddings.embed_text("Fast hybrid semantic search for AI memory vault.")
    assert len(vec) == 256
    norm = math.sqrt(sum(v * v for v in vec))
    assert pytest.approx(norm, 1e-4) == 1.0


@pytest.mark.anyio
async def test_embed_batch_and_custom_dim():
    """Verify batch embedding and custom MRL truncation dimension."""
    texts = [
        "First architectural decision on memory vault.",
        "Second decision on task receipts and receipts buffer.",
    ]
    vectors = await embeddings.embed_texts(texts, dim=128)
    assert len(vectors) == 2
    for vec in vectors:
        assert len(vec) == 128
        norm = math.sqrt(sum(v * v for v in vec))
        assert pytest.approx(norm, 1e-4) == 1.0


@pytest.mark.anyio
async def test_embed_deterministic_mock():
    """Verify deterministic vector generation for identical texts."""
    v1 = await embeddings.embed_text("Same identical text input")
    v2 = await embeddings.embed_text("Same identical text input")
    assert v1 == v2
