"""Embeddings for the recall nodes.

Two backends behind one function:
  * `sentence-transformers` (`all-MiniLM-L6-v2`) when the package is present —
    the real thing, ~90 MB of model, fully offline;
  * a deterministic hashed bag-of-words otherwise — not *semantic*, but stable,
    dependency-free, and good enough to prove the store/kNN plumbing in
    Phase 0 and in tests. Its name lands in `chunks.model`, so nothing can
    quietly mix two vector families in one table.
"""
from __future__ import annotations

import hashlib
import importlib
import math
import struct

FALLBACK_DIM = 256
FALLBACK_MODEL = "hash-bow-256"


class Embedder:
    """The seam. `Stores` should always read `model` alongside the blob."""

    model = FALLBACK_MODEL
    dim = FALLBACK_DIM

    def __init__(self) -> None:
        try:  # optional heavy dependency; degrade without touching the caller
            module = importlib.import_module("sentence_transformers")
            self._model = module.SentenceTransformer("all-MiniLM-L6-v2")
            self.model = "all-MiniLM-L6-v2"
            self.dim = int(self._model.get_sentence_embedding_dimension())
        except Exception:  # ImportError, HF download failure, no disk, …
            self._model = None

    def embed(self, text: str) -> list[float]:
        if self._model is not None:
            vec = self._model.encode([text], normalize_embeddings=True)[0]
            return [float(x) for x in vec]
        return _hash_bag(text)

    def pack(self, vec: list[float]) -> bytes:
        return struct.pack(f"{len(vec)}f", *vec)

    def unpack(self, blob: bytes) -> list[float]:
        count = len(blob) // 4
        return list(struct.unpack(f"{count}f", blob[:count * 4]))

    def cosine(self, a: list[float], b: list[float]) -> float:
        if len(a) != len(b) or not a:
            return -1.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(y * y for y in b)) or 1.0
        return dot / (na * nb)


def _hash_bag(text: str, dim: int = FALLBACK_DIM) -> list[float]:
    """Bag-of-words hashed into `dim` buckets, L2-normalised.

    Token order is thrown away and the mapping is hash-randomised by nothing —
    the same word always lands in the same bucket — which is why it is stable
    across processes. Two passages sharing vocabulary get similar vectors;
    two synonyms do not. That is the honest limit of a fallback.
    """
    counts = [0.0] * dim
    for token in text.lower().split():
        stripped = token.strip(".,!?;:'\"()[]{}")
        if not stripped:
            continue
        bucket = int(hashlib.sha1(stripped.encode("utf-8")).hexdigest(), 16) % dim
        counts[bucket] += 1.0
    norm = math.sqrt(sum(c * c for c in counts)) or 1.0
    return [c / norm for c in counts]
