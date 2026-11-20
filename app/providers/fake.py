"""Deterministic offline providers used by the test suite and the offline eval.

FakeEmbedder: hashed bag-of-words (+bigrams) vectors. Lexical, not semantic, but
deterministic and good enough to exercise retrieval, access filtering and re-indexing.

ExtractiveChat: reads the SAME prompt the real model gets (it parses the excerpts
out of the user message) and answers by extracting the best-matching lines. It
refuses when no excerpt covers enough of the question's content words. It never
invents text, so it is a lower bound on answer quality, not an imitation of an LLM.
It does not judge conflicts; it reports multi-SOP answers with conflict=false.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Sequence

import numpy as np

from app.ports import LLMResult

_STOP = set("""a an the of to in on at for by with from and or is are was were be been being do does did
what which who whom whose when where why how can could should would will shall may might must i we you
he she it they my our your their this that these those up into about over under than then there here
as if not no yes any all each per much many more most some such only also just get got need needs
medicare clinic clinics staff sop sops policy procedure procedures say says tell me please mean""".split())
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _stem(w: str) -> str:
    for suf in ("ings", "ing", "ies", "es", "ed", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(text.lower()):
        for part in t.split("-") if "-" in t else [t]:
            if part and part not in _STOP:
                out.append(_stem(part))
    return out


class FakeEmbedder:
    model_id = "fake-hash-bow-v1:1024"
    dim = 1024

    def __init__(self):
        self.calls = 0
        self.texts_embedded = 0

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        toks = tokens(text)
        feats = [(t, 1.0) for t in toks] + [(a + "_" + b, 0.5) for a, b in zip(toks, toks[1:])]
        for f, w in feats:
            h = int.from_bytes(hashlib.md5(f.encode()).digest()[:4], "little")
            v[h % self.dim] += w      # unsigned buckets: cosine stays in [0, 1]
        v = np.log1p(v)
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts: Sequence[str], *, correlation_id: str = "-") -> np.ndarray:
        self.calls += 1
        self.texts_embedded += len(texts)
        return np.vstack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)

    def embed_query(self, text: str, *, correlation_id: str = "-") -> np.ndarray:
        return self._vec(text)


_EXCERPT_HDR = re.compile(r"^\[(C\d+)\] (SOP-\d+) v([\w.]+)", re.M)


class ExtractiveChat:
    model_id = "fake-extractive-v1"
    MIN_COVERAGE = 0.5

    def __init__(self):
        self.calls = 0

    def generate(self, system: str, user: str, *, correlation_id: str = "-") -> LLMResult:
        self.calls += 1
        t0 = time.perf_counter()
        q = user.split("\n", 1)[0].removeprefix("QUESTION: ").strip()
        qterms = set(tokens(q))
        excerpts = self._excerpts(user)
        scored = []
        for label, sop, body in excerpts:
            bterms = set(tokens(body))
            cov = len(qterms & bterms) / max(1, len(qterms))
            scored.append((cov, label, sop, body))
        scored.sort(key=lambda x: -x[0])
        if not scored or scored[0][0] < self.MIN_COVERAGE:
            reply = {"status": "not_found", "answer": "", "citations": [], "conflict": False}
        else:
            used_sops, cites, lines = set(), [], []
            for cov, label, sop, body in scored:
                if cov < self.MIN_COVERAGE or sop in used_sops or len(cites) >= 2:
                    continue
                used_sops.add(sop)
                cites.append(label)
                lines.append(f"{sop}: " + self._best_lines(body, qterms))
            reply = {"status": "answered", "answer": "\n".join(lines), "citations": cites, "conflict": False}
        text = json.dumps(reply)
        return LLMResult(text, self.model_id, input_tokens=len(system + user) // 4,
                         output_tokens=len(text) // 4, latency_ms=(time.perf_counter() - t0) * 1000, cost_usd=0.0)

    @staticmethod
    def _excerpts(user: str) -> list[tuple[str, str, str]]:
        marks = list(_EXCERPT_HDR.finditer(user))
        out = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(user)
            body = user[m.end():end].split("\n", 1)[1] if "\n" in user[m.end():end] else ""
            out.append((m.group(1), m.group(2), body.strip()))
        return out

    @staticmethod
    def _best_lines(body: str, qterms: set[str], n: int = 2) -> str:
        lines = [ln for ln in body.split("\n") if ln.strip()]
        sentences = []
        for ln in lines:
            sentences += [s.strip() for s in re.split(r"(?<=[.])\s+(?=[A-Z])", ln) if s.strip()]
        ranked = sorted(enumerate(sentences), key=lambda p: (-len(qterms & set(tokens(p[1]))), p[0]))[:n]
        return " ".join(s for _, s in sorted(ranked))
