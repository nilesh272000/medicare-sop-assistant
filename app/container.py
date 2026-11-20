"""Composition root: the only place that picks concrete providers."""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.domain.access import AccessPolicy, UserDirectory
from app.ports import ChatModel, Embedder
from app.service.answer_service import AnswerService
from app.store.db import Database
from app.store.vector_index import VectorIndex


@dataclass
class Container:
    settings: Settings
    db: Database
    index: VectorIndex
    embedder: Embedder
    chat: ChatModel
    policy: AccessPolicy
    users: UserDirectory
    answers: AnswerService


def build_providers(s: Settings) -> tuple[Embedder, ChatModel]:
    if s.provider == "fake":
        from app.providers.fake import ExtractiveChat, FakeEmbedder
        return FakeEmbedder(), ExtractiveChat()
    if s.provider == "gemini":
        from app.providers.gemini import GeminiChat, GeminiEmbedder
        emb = GeminiEmbedder(s.gemini_api_key, s.gemini_embed_model, timeout_s=s.embed_timeout_s,
                             max_retries=s.max_retries, price_per_m=s.price_embed_per_m)
        chat = GeminiChat(s.gemini_api_key, s.gemini_chat_model, timeout_s=s.llm_timeout_s,
                          max_retries=s.max_retries, price_in_per_m=s.price_in_per_m,
                          price_out_per_m=s.price_out_per_m)
        return emb, chat
    raise ValueError(f"unknown LLM_PROVIDER {s.provider!r} (expected 'fake' or 'gemini')")


def build_container(s: Settings, embedder: Embedder | None = None, chat: ChatModel | None = None) -> Container:
    if embedder is None or chat is None:
        e, c = build_providers(s)
        embedder, chat = embedder or e, chat or c
    db = Database(s.db_path)
    index = VectorIndex(db)
    policy = AccessPolicy.load(s.access_policy_path)
    users = UserDirectory.load_csv(s.staff_users_path)
    return Container(s, db, index, embedder, chat, policy, users,
                     AnswerService(db, index, embedder, chat, policy, users, s))
