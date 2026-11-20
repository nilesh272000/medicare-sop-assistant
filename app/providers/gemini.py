"""Google Gemini (Generative Language API) adapters over plain REST.

No SDK: one less dependency, and the adapter stays a thin translation layer.
NOTE: written against the public v1beta REST contract; it was not exercised in the
offline test suite (which uses app.providers.fake). See README "Known limitations".
"""
from __future__ import annotations

import logging
import time
from typing import Sequence

import httpx
import numpy as np

from app.domain.errors import UpstreamUnavailableError
from app.observability import log
from app.ports import LLMResult
from app.providers.http_retry import post_json

BASE = "https://generativelanguage.googleapis.com/v1beta"
logger = logging.getLogger("provider.gemini")


class GeminiChat:
    def __init__(self, api_key: str, model: str, *, timeout_s: float, max_retries: int,
                 price_in_per_m: float, price_out_per_m: float, client: httpx.Client | None = None):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        self.model_id = model
        self._key = api_key
        self._timeout = timeout_s
        self._retries = max_retries
        self._pin, self._pout = price_in_per_m, price_out_per_m
        self._client = client or httpx.Client()

    def generate(self, system: str, user: str, *, correlation_id: str) -> LLMResult:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
    "responseMimeType": "application/json",
    "maxOutputTokens": 1024,
    "thinkingConfig": {"thinkingLevel": "MINIMAL"},
},
        }
        t0 = time.perf_counter()
        data = post_json(self._client, f"{BASE}/models/{self.model_id}:generateContent", json=body,
                         headers={"x-goog-api-key": self._key}, timeout_s=self._timeout,
                         max_retries=self._retries, op="llm.generate")
        latency = (time.perf_counter() - t0) * 1000
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError):
            raise UpstreamUnavailableError("LLM returned no candidate (possibly blocked)",
                                           {"finish": data.get("candidates", [{}])[0].get("finishReason")
                                            if data.get("candidates") else None})
        usage = data.get("usageMetadata", {})
        tin = int(usage.get("promptTokenCount", 0))
        tout = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
        cost = tin / 1e6 * self._pin + tout / 1e6 * self._pout
        log(logger, "llm.call", model=self.model_id, latency_ms=round(latency, 1), input_tokens=tin,
            output_tokens=tout, cost_usd=round(cost, 6))
        return LLMResult(text, self.model_id, tin, tout, latency, cost)


class GeminiEmbedder:
    def __init__(self, api_key: str, model: str, *, timeout_s: float, max_retries: int,
                 price_per_m: float, dim: int = 768, client: httpx.Client | None = None):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        self.model_id = f"{model}:{dim}"
        self._model, self._dim, self._key = model, dim, api_key
        self._timeout, self._retries, self._price = timeout_s, max_retries, price_per_m
        self._client = client or httpx.Client()

    def _embed(self, texts: Sequence[str], task: str, op: str) -> np.ndarray:
        reqs = [{"model": f"models/{self._model}", "content": {"parts": [{"text": t}]},
                 "taskType": task, "outputDimensionality": self._dim} for t in texts]
        t0 = time.perf_counter()
        data = post_json(self._client, f"{BASE}/models/{self._model}:batchEmbedContents",
                         json={"requests": reqs}, headers={"x-goog-api-key": self._key},
                         timeout_s=self._timeout, max_retries=self._retries, op=op)
        est_tokens = sum(len(t) for t in texts) // 4    # API does not return usage for embeddings
        log(logger, "embed.call", model=self._model, n=len(texts), latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            est_tokens=est_tokens, cost_usd=round(est_tokens / 1e6 * self._price, 6))
        return np.array([e["values"] for e in data["embeddings"]], dtype=np.float32)

    def embed_documents(self, texts: Sequence[str], *, correlation_id: str) -> np.ndarray:
        return self._embed(texts, "RETRIEVAL_DOCUMENT", "embed.documents")

    def embed_query(self, text: str, *, correlation_id: str) -> np.ndarray:
        return self._embed([text], "RETRIEVAL_QUERY", "embed.query")[0]
