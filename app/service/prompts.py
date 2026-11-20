"""Prompt construction and parsing of the model's JSON reply.

The prompt asks for behaviour (answer only from context, flag conflicts). It does
NOT carry business rules: access control, PII and the "is there any relevant
context at all" gate are enforced in code before the model is called, and
citations are validated in code after it replies.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.domain.models import ScoredChunk

PROMPT_VERSION = "p3"

SYSTEM_PROMPT = """You are the SOP assistant for MediCare Clinics staff.
Answer ONLY from the numbered SOP excerpts in the user message. Do not use outside or general medical knowledge.
Rules:
1. If the excerpts do not contain the answer, reply with status "not_found". Do not guess.
2. Be concise and practical: steps, numbers, time limits, forms and who to contact, as written in the excerpts.
3. Cite every excerpt you used by its label, e.g. "C2".
4. If two excerpts from different SOPs give different rules for the same thing, set "conflict": true, state both rules with their SOP ids and versions, say which SOP is newer or more specific (use the effective dates), and recommend the stricter rule.
Reply with JSON only:
{"status": "answered" | "not_found", "answer": "<text>", "citations": ["C1", ...], "conflict": true | false}"""


def build_user_prompt(question: str, context: list[ScoredChunk], effective_dates: dict[str, str]) -> str:
    parts = [f"QUESTION: {question}", "", "SOP EXCERPTS:"]
    for i, sc in enumerate(context, start=1):
        c = sc.chunk
        parts.append(
            f"[C{i}] {c.sop_id} v{c.version} (effective {effective_dates.get(c.sop_id, '?')}) | {c.title} | "
            f"Section {c.section_no}: {c.section_title} | page {c.page_start}"
        )
        parts.append(c.text)
        parts.append("")
    return "\n".join(parts)


@dataclass
class ModelReply:
    status: str
    answer: str
    labels: list[str]
    conflict: bool


_JSON_OBJ = re.compile(r"\{.*\}", re.S)


def parse_reply(text: str) -> ModelReply | None:
    """Lenient JSON extraction; returns None if the reply is unusable."""
    m = _JSON_OBJ.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    status = str(obj.get("status", "")).strip().lower()
    if status not in {"answered", "not_found"}:
        return None
    labels = [str(x).strip().strip("[]") for x in obj.get("citations", []) if str(x).strip()]
    return ModelReply(status, str(obj.get("answer", "")).strip(), labels, bool(obj.get("conflict", False)))
