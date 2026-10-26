# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Embeddings
================
Lightweight local embedding engine backed by ``fastembed`` (ONNX Runtime —
no PyTorch dependency) with Matryoshka Representation Learning (MRL)
truncation and L2 normalization (``v / norm(v)``).

The preferred model is ``google/gemma-2-embedding`` (EmbeddingGemma); when it
cannot be loaded the engine falls back through the fastembed-published
``google/embeddinggemma-300m`` id and then ``BAAI/bge-small-en-v1.5``. When
``fastembed`` itself is not installed — or no candidate model can be loaded —
a deterministic hash-based mock embedder takes over so imports and tests
never raise ImportError.

All CPU-heavy work (model load + inference) runs inside
``asyncio.to_thread`` so the asyncio event loop is never blocked.
"""

import asyncio
import hashlib
import math
import re
from typing import Dict, List, Optional, Sequence

__all__ = [
    "EmbeddingEngine",
    "get_engine",
    "embed_text",
    "embed_texts",
    "is_fastembed_available",
    "DEFAULT_MODEL",
    "FALLBACK_MODEL",
    "DEFAULT_DIM",
]

DEFAULT_MODEL = "google/gemma-2-embedding"
FALLBACK_MODEL = "BAAI/bge-small-en-v1.5"
# fastembed's published model id for EmbeddingGemma weights (preferred-model
# alias, tried when ``google/gemma-2-embedding`` is not resolvable).
_GEMMA_FASTEMBED_ID = "google/embeddinggemma-300m"
DEFAULT_DIM = 256

_TOKEN_RE = re.compile(r"\w+")

# Lazily resolved fastembed.TextEmbedding class: None = not attempted yet,
# False = import failed (unavailable).
_TextEmbedding: Optional[object] = None


def _import_text_embedding() -> Optional[object]:
    """Resolve ``fastembed.TextEmbedding`` lazily; ``None`` when unavailable."""
    global _TextEmbedding
    if _TextEmbedding is None:
        try:
            from fastembed import TextEmbedding

            _TextEmbedding = TextEmbedding
        except Exception:
            _TextEmbedding = False
    return _TextEmbedding or None


def is_fastembed_available() -> bool:
    """Whether the optional ``fastembed`` dependency can be imported."""
    return _import_text_embedding() is not None


def _l2_normalize(vec: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0.0:
        return [0.0] * len(vec)
    return [v / norm for v in vec]


def _mrl_truncate(vec: Sequence[float], dim: int) -> List[float]:
    """MRL truncation of ``vec`` to ``dim`` dims + L2 renormalization."""
    return _l2_normalize([float(v) for v in vec[:dim]])


def _mock_embed(text: str, dim: int) -> List[float]:
    """Deterministic token-hash projection — no model, no dependencies.

    Each token adds signed unit mass to a few SHA-256-derived dimensions, so
    lexically similar texts stay weakly similar and identical inputs always
    produce identical vectors.
    """
    vec = [0.0] * max(int(dim), 1)
    for token in _TOKEN_RE.findall(str(text).lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for off in (0, 4, 8):
            idx = int.from_bytes(digest[off : off + 4], "little") % len(vec)
            vec[idx] += 1.0 if digest[off + 12] & 1 else -1.0
    return _l2_normalize(vec)


class EmbeddingEngine:
    """Lazy fastembed-backed embedder with a deterministic mock fallback.

    ``dim`` is the MRL output width (default 256 floats ≈ 1 KB). The ONNX
    session is created on first use inside ``asyncio.to_thread`` —
    constructing the engine is always cheap.
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        dim: int = DEFAULT_DIM,
        cache_dir: Optional[str] = None,
    ) -> None:
        self.model_name = str(model_name or DEFAULT_MODEL)
        self.dim = int(dim) if dim and int(dim) > 0 else DEFAULT_DIM
        self.cache_dir = cache_dir
        self.active_model: Optional[str] = None
        self._model = None
        self._load_failed = False

    @property
    def backend(self) -> str:
        """``fastembed`` once a model loads, ``mock`` after fallback, else ``pending``."""
        if self._model is not None:
            return "fastembed"
        return "mock" if self._load_failed else "pending"

    def _candidates(self) -> List[str]:
        out: List[str] = []
        for name in (self.model_name, _GEMMA_FASTEMBED_ID, FALLBACK_MODEL):
            if name and name not in out:
                out.append(name)
        return out

    def _load(self) -> None:
        """Blocking model init — must only run inside ``asyncio.to_thread``."""
        if self._model is not None or self._load_failed:
            return
        te_cls = _import_text_embedding()
        if te_cls is None:
            self._load_failed = True
            return
        for name in self._candidates():
            try:
                kwargs: Dict[str, object] = {"model_name": name}
                if self.cache_dir:
                    kwargs["cache_dir"] = self.cache_dir
                self._model = te_cls(**kwargs)
                self.active_model = name
                return
            except Exception:
                continue
        self._load_failed = True

    def _embed_sync(self, texts: List[str]) -> List[List[float]]:
        """Blocking inference — must only run inside ``asyncio.to_thread``."""
        self._load()
        if self._model is None:
            return [_mock_embed(t, self.dim) for t in texts]
        try:
            return [_mrl_truncate(vec, self.dim) for vec in self._model.embed(texts)]
        except Exception:
            return [_mock_embed(t, self.dim) for t in texts]

    async def embed(self, text: str) -> List[float]:
        """Embed one text; returns an L2-normalized vector of ``dim`` floats."""
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed a batch of texts off the event loop via ``asyncio.to_thread``."""
        batch = [str(t) for t in texts]
        if not batch:
            return []
        return await asyncio.to_thread(self._embed_sync, batch)


_engines: Dict[int, EmbeddingEngine] = {}


def get_engine(dim: int = DEFAULT_DIM) -> EmbeddingEngine:
    """Process-wide engine cache, keyed by MRL output dimension."""
    width = int(dim) if dim and int(dim) > 0 else DEFAULT_DIM
    eng = _engines.get(width)
    if eng is None:
        eng = _engines[width] = EmbeddingEngine(dim=width)
    return eng


async def embed_text(text: str, dim: int = DEFAULT_DIM) -> List[float]:
    """Embed ``text`` with the shared engine (lazy model load)."""
    return await get_engine(dim).embed(text)


async def embed_texts(texts: Sequence[str], dim: int = DEFAULT_DIM) -> List[List[float]]:
    """Embed a batch of texts with the shared engine."""
    return await get_engine(dim).embed_batch(texts)
