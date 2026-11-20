"""Question answering use case. Depends only on domain types and ports.

Order of operations (each step is a hard gate, enforced in code):
  1. validate input            -> 400
  2. resolve user -> role      -> 403 for unknown users
  3. patient-data guard        -> 422; nothing is cached, embedded, sent or logged
  4. answer cache (key includes access levels + index generation)
  5. retrieve ONLY from chunks whose access level the role may read
  6. relevance gate: best score below threshold -> "I don't know", no LLM call
  7. LLM over the permitted excerpts
  8. citation validation: labels must point at excerpts we sent; an "answer" with
     no valid citation is converted to "I don't know"
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field

from app.config import Settings
from app.domain.access import AccessPolicy, UserDirectory
from app.domain.errors import IndexNotReadyError, InvalidQuestionError, PatientDataDetectedError, UpstreamUnavailableError
from app.domain.models import Answer, AnswerStatus, Citation, ScoredChunk
from app.domain.pii import categories, detect_patient_data
from app.observability import log
from app.ports import ChatModel, Embedder
from app.service.cache import cache_key
from app.service.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt, parse_reply
from app.store.db import Database
from app.store.vector_index import VectorIndex

logger = logging.getLogger("answer")
MAX_QUESTION_CHARS = 1000

REFUSAL_TEXT = ("I don't know. The SOPs available to you do not cover this question. "
                "Please check with your Clinic Manager, or with HR for HR matters.")


@dataclass
class Trace:
    role: str = ""
    retrieved: list[dict] = field(default_factory=list)     # chunk_id, sop_id, version, score
    context_chunk_ids: list[str] = field(default_factory=list)
    llm_called: bool = False
    cache_hit: bool = False
    refusal_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0


class AnswerService:
    def __init__(self, db: Database, index: VectorIndex, embedder: Embedder, chat: ChatModel,
                 policy: AccessPolicy, users: UserDirectory, settings: Settings):
        self.db, self.index, self.embedder, self.chat = db, index, embedder, chat
        self.policy, self.users, self.s = policy, users, settings

    def ask(self, user_id: str, question: str, *, correlation_id: str) -> tuple[Answer, Trace]:
        t0 = time.perf_counter()
        trace = Trace()
        question = (question or "").strip()
        if not question or len(question) > MAX_QUESTION_CHARS:
            raise InvalidQuestionError(f"question must be 1-{MAX_QUESTION_CHARS} characters")

        user = self.users.get(user_id)
        trace.role = user.role
        levels = self.policy.levels_for_role(user.role)

        findings = detect_patient_data(question)
        if findings:
            cats = categories(findings)
            log(logger, "ask.patient_data_blocked", user_id=user.user_id, categories=cats)  # no question text
            raise PatientDataDetectedError(
                "Your question seems to contain patient information (" + ", ".join(cats) + "). "
                "Please remove names, phone numbers, ID numbers, MRNs and dates of birth and ask again.",
                {"categories": cats})

        snap = self.index.snapshot()
        if not snap.chunks:
            raise IndexNotReadyError("The SOP index is empty; run ingestion first.")
        if self.db.embed_model() != self.embedder.model_id:
            # Index built with another embedding model (e.g. provider switched): vectors are
            # not comparable. Re-running ingestion re-embeds everything.
            raise IndexNotReadyError("The SOP index was built with a different embedding model; re-run ingestion.",
                                     {"index_model": self.db.embed_model(), "query_model": self.embedder.model_id})

        key = cache_key(question, levels, snap.generation, PROMPT_VERSION, self.chat.model_id,
                        self.embedder.model_id,
                        f"k{self.s.top_k}-d{self.s.max_context_docs}-m{self.s.min_retrieval_score}")
        cached = self.db.cache_get(key, snap.generation, self.s.cache_ttl_s)
        if cached:
            ans = _answer_from_json(cached)
            ans.cached = True
            trace.cache_hit = True
            trace.latency_ms = (time.perf_counter() - t0) * 1000
            self._log_done(user.user_id, user.role, question, ans, trace)
            return ans, trace

        qvec = self.embedder.embed_query(question, correlation_id=correlation_id)
        hits: list[ScoredChunk] = self.index.search(snap, qvec, levels, self.s.top_k)
        trace.retrieved = [{"chunk_id": h.chunk.chunk_id, "sop_id": h.chunk.sop_id, "version": h.chunk.version,
                            "section": h.chunk.section_no, "score": round(h.score, 4)} for h in hits]

        if not hits or hits[0].score < self.s.min_retrieval_score:
            trace.refusal_reason = "below_relevance_threshold"
            ans = Answer(AnswerStatus.NOT_FOUND, REFUSAL_TEXT, index_generation=snap.generation)
        else:
            context = self._expand_to_documents(snap, hits, levels)
            ans = self._generate(question, context, snap.generation, trace, correlation_id)

        self.db.cache_put(key, snap.generation, json.dumps(_answer_to_dict(ans)))
        trace.latency_ms = (time.perf_counter() - t0) * 1000
        self._log_done(user.user_id, user.role, question, ans, trace)
        return ans, trace

    def _expand_to_documents(self, snap, hits: list[ScoredChunk], levels: frozenset[str]) -> list[ScoredChunk]:
        """Small-to-big: retrieve by section (precise scoring), but give the model every
        section of the best few SOPs. Procedures span sections (Q01: first aid in s.2,
        reporting in s.3, follow-up in s.5) and an SOP is ~1 page, so whole documents
        cost ~600 tokens each. Siblings are re-checked against the role's levels."""
        ranked_docs: list[str] = []
        best: dict[str, float] = {}
        for h in hits:
            if h.score < self.s.min_retrieval_score:
                continue
            if h.chunk.sop_id not in best:
                ranked_docs.append(h.chunk.sop_id)
                best[h.chunk.sop_id] = h.score
        ranked_docs = ranked_docs[: self.s.max_context_docs]
        out: list[ScoredChunk] = []
        for sop_id in ranked_docs:
            for ch in snap.chunks:
                if ch.sop_id == sop_id and ch.access_level in levels:
                    out.append(ScoredChunk(ch, best[sop_id]))
        return out

    def _generate(self, question: str, hits: list[ScoredChunk], generation: int, trace: Trace,
                  correlation_id: str) -> Answer:
        docs = self.db.active_documents()
        eff = {sid: r["effective_date"] for sid, r in docs.items()}
        user_prompt = build_user_prompt(question, hits, eff)
        trace.context_chunk_ids = [h.chunk.chunk_id for h in hits]
        trace.llm_called = True
        res = self.chat.generate(SYSTEM_PROMPT, user_prompt, correlation_id=correlation_id)
        trace.input_tokens, trace.output_tokens, trace.cost_usd = res.input_tokens, res.output_tokens, res.cost_usd
        log(logger, "llm.usage", model=res.model, latency_ms=round(res.latency_ms, 1),
            input_tokens=res.input_tokens, output_tokens=res.output_tokens, cost_usd=round(res.cost_usd, 6))

        reply = parse_reply(res.text)
        if reply is None:
            raise UpstreamUnavailableError("The language model returned an unreadable reply.", {"retryable": True})
        if reply.status == "not_found":
            trace.refusal_reason = "model_not_found"
            return Answer(AnswerStatus.NOT_FOUND, REFUSAL_TEXT, index_generation=generation)

        by_label = {f"C{i}": h.chunk for i, h in enumerate(hits, start=1)}
        valid = [by_label[l] for l in reply.labels if l in by_label]
        invalid = [l for l in reply.labels if l not in by_label]
        if invalid:
            log(logger, "llm.invalid_citations", logging.WARNING, labels=invalid)
        if not valid or not reply.answer:
            trace.refusal_reason = "answer_without_valid_citation"
            return Answer(AnswerStatus.NOT_FOUND, REFUSAL_TEXT, index_generation=generation)

        citations: list[Citation] = []
        seen = set()
        for ch in valid:
            k = (ch.sop_id, ch.section_no)
            if k in seen:
                continue
            seen.add(k)
            citations.append(Citation(ch.sop_id, ch.version, ch.title, ch.section_no, ch.section_title, ch.page_start))
        conflict = reply.conflict and len({c.sop_id for c in citations}) > 1
        return Answer(AnswerStatus.ANSWERED, reply.answer, citations, conflict=conflict, index_generation=generation)

    def _log_done(self, user_id: str, role: str, question: str, ans: Answer, trace: Trace) -> None:
        log(logger, "ask.done", user_id=user_id, role=role, question=question, status=ans.status.value,
            cached=trace.cache_hit, llm_called=trace.llm_called, refusal_reason=trace.refusal_reason,
            citations=[c.label() for c in ans.citations], retrieved=trace.retrieved,
            latency_ms=round(trace.latency_ms, 1), generation=ans.index_generation)


def _answer_to_dict(a: Answer) -> dict:
    return {"status": a.status.value, "answer": a.answer, "citations": [asdict(c) for c in a.citations],
            "conflict": a.conflict, "index_generation": a.index_generation}


def _answer_from_json(s: str) -> Answer:
    d = json.loads(s)
    return Answer(AnswerStatus(d["status"]), d["answer"], [Citation(**c) for c in d["citations"]],
                  conflict=d["conflict"], index_generation=d["index_generation"])
