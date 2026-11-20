"""In-memory cosine-similarity index over the active chunks.

At 200 SOPs x ~6 chunks the whole library is ~1,200 vectors; brute-force numpy is
sub-millisecond and removes a moving part. Swapping to pgvector / Vertex AI Vector
Search means re-implementing `search` with the same pre-filter semantics.

Access control is applied BEFORE scoring: rows the caller may not read are masked
out of the candidate set, so they cannot be returned whatever the query says.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from app.domain.models import Chunk, ScoredChunk
from app.store.db import Database


@dataclass(frozen=True)
class _Snapshot:
    generation: int
    chunks: list[Chunk]
    matrix: np.ndarray           # L2-normalised rows
    levels: np.ndarray           # access level per row


class VectorIndex:
    def __init__(self, db: Database):
        self._db = db
        self._lock = threading.Lock()
        self._snap: _Snapshot | None = None

    def snapshot(self) -> _Snapshot:
        """Return the current snapshot, reloading if a publish happened."""
        current = self._db.generation()
        snap = self._snap
        if snap is not None and snap.generation == current:
            return snap
        with self._lock:
            if self._snap is None or self._snap.generation != current:
                gen, chunks, mat = self._db.load_index()
                if len(chunks):
                    norms = np.linalg.norm(mat, axis=1, keepdims=True)
                    mat = mat / np.where(norms == 0, 1, norms)
                self._snap = _Snapshot(gen, chunks, mat, np.array([c.access_level for c in chunks], dtype=object))
            return self._snap

    @staticmethod
    def search(snap: _Snapshot, query_vec: np.ndarray, allowed_levels: frozenset[str], k: int) -> list[ScoredChunk]:
        if not allowed_levels or not snap.chunks:
            return []
        mask = np.isin(snap.levels, list(allowed_levels))
        idx = np.nonzero(mask)[0]
        if idx.size == 0:
            return []
        q = query_vec / (np.linalg.norm(query_vec) or 1.0)
        scores = snap.matrix[idx] @ q
        order = np.argsort(-scores)[:k]
        return [ScoredChunk(snap.chunks[idx[i]], float(scores[i])) for i in order]
