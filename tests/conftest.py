from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings
from app.container import build_container
from app.ingest.indexer import run_ingestion
from app.ports import LLMResult
from app.providers.fake import ExtractiveChat, FakeEmbedder

ROOT = Path(__file__).resolve().parent.parent
SOPS = ROOT / "data" / "sops"
UPDATE = ROOT / "data" / "sop_updates" / "2026-11-01"


def make_settings(tmp_path: Path, **over) -> Settings:
    base = Settings(
        provider="fake", gemini_api_key="", gemini_chat_model="x", gemini_embed_model="x",
        price_in_per_m=0.3, price_out_per_m=2.5, price_embed_per_m=0.15,
        db_path=str(tmp_path / "t.sqlite3"),
        access_policy_path=str(ROOT / "data/access_policy.json"),
        staff_users_path=str(ROOT / "data/staff_users.csv"),
        top_k=8, max_context_docs=3, min_retrieval_score=0.10, llm_timeout_s=5, embed_timeout_s=5,
        max_retries=1, cache_ttl_s=3600, log_level="WARNING",
    )
    return replace(base, **over)


class SpyChat:
    """Wraps a ChatModel and records every prompt it receives."""

    def __init__(self, inner=None, scripted: str | None = None):
        self.inner = inner or ExtractiveChat()
        self.scripted = scripted
        self.prompts: list[str] = []
        self.model_id = self.inner.model_id

    def generate(self, system, user, *, correlation_id="-"):
        self.prompts.append(user)
        if self.scripted is not None:
            return LLMResult(self.scripted, "scripted", 10, 10, 1.0, 0.0)
        return self.inner.generate(system, user, correlation_id=correlation_id)


class SpyEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.queries: list[str] = []

    def embed_query(self, text, *, correlation_id="-"):
        self.queries.append(text)
        return super().embed_query(text, correlation_id=correlation_id)


@pytest.fixture(autouse=True, scope="session")
def _memoise_pdf_extraction():
    """Test-speed only: PDF extraction is pure, so memoise it by file bytes."""
    import hashlib
    import app.ingest.indexer as indexer
    real = indexer.extract_pdf
    memo = {}

    def cached(path):
        h = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if h not in memo:
            memo[h] = real(path)
        return memo[h]
    indexer.extract_pdf = cached
    yield
    indexer.extract_pdf = real


@pytest.fixture
def container(tmp_path):
    """Fresh container indexed with the original 15 SOPs (fake providers)."""
    c = build_container(make_settings(tmp_path), embedder=SpyEmbedder(), chat=SpyChat())
    run_ingestion(str(SOPS), c.db, c.embedder, c.policy)
    return c


@pytest.fixture
def snapshot_copy(tmp_path):
    """Copy a snapshot folder so a test can mutate it."""
    def _copy(src: Path, name: str) -> Path:
        dst = tmp_path / name
        shutil.copytree(src, dst)
        return dst
    return _copy


def answer_json(status="answered", answer="x", citations=("C1",), conflict=False) -> str:
    return json.dumps({"status": status, "answer": answer, "citations": list(citations), "conflict": conflict})
