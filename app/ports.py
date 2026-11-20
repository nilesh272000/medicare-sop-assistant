"""Interfaces the domain/service layer depends on. Providers implement these;
nothing in app.service or app.ingest imports a vendor SDK or HTTP client."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

import numpy as np


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float


class ChatModel(Protocol):
    model_id: str

    def generate(self, system: str, user: str, *, correlation_id: str) -> LLMResult: ...


class Embedder(Protocol):
    model_id: str          # part of the embedding cache key: changing model => re-embed

    def embed_documents(self, texts: Sequence[str], *, correlation_id: str) -> np.ndarray: ...

    def embed_query(self, text: str, *, correlation_id: str) -> np.ndarray: ...
