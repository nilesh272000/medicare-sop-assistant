"""Answer-cache key. Pure function, unit tested.

key = sha256(normalised question | sorted access levels | index generation |
             prompt version | chat model | embed model | retrieval config)

* Access LEVELS, not role or user: two roles with the same levels (nurse, doctor,
  pharmacist) can see exactly the same documents, so they may share an answer; any
  difference in levels gives a different key, so an answer built from HR documents
  can never be served to a role without the 'hr' level.
* Index generation: every publish bumps it, so no answer computed from a superseded
  or withdrawn SOP is reachable after a re-index (old rows are also deleted in the
  publish transaction). A no-op re-index does not bump it, so the cache survives.
* Exact match after normalisation (case, whitespace, trailing punctuation), not a
  semantic cache: "within 2 hours" vs "within 24 hours" questions embed almost
  identically, and a wrong cache hit is worse than a miss (ADR-2 in docs/design_review.md).
"""
from __future__ import annotations

import hashlib
import re


def normalise_question(q: str) -> str:
    q = q.lower().strip()
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"[?!.\s]+$", "", q)
    return q


def cache_key(question: str, levels: frozenset[str], generation: int, prompt_version: str,
              chat_model: str, embed_model: str, retrieval_config: str) -> str:
    parts = [normalise_question(question), ",".join(sorted(levels)), str(generation),
             prompt_version, chat_model, embed_model, retrieval_config]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
