"""Domain types. No framework, provider or database imports here."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class SopDocument:
    """One version of one SOP, as parsed from a PDF in a snapshot."""

    sop_id: str            # e.g. "SOP-04" -- the stable identity across runs
    title: str
    version: str           # e.g. "3.0"
    effective_date: str    # ISO date string from the header
    access_field: str      # raw 'Access' text from the header
    access_level: str      # mapped through access_policy.json
    content_hash: str      # sha256 of normalised extracted text (not file bytes)
    file_name: str         # informational only; never used as identity
    page_count: int
    change_note: str = ""  # latest revision-history line, for reporting


@dataclass(frozen=True)
class Chunk:
    chunk_id: str          # f"{sop_id}@{content_hash[:12]}#{ordinal}"
    sop_id: str
    version: str
    title: str
    access_level: str
    section_no: str        # "3" ; "" if not inside a numbered section
    section_title: str
    page_start: int
    page_end: int
    text: str              # body text as shown to the LLM
    ordinal: int

    @property
    def embed_text(self) -> str:
        """Text sent to the embedder: body plus a context line."""
        return f"{self.sop_id} {self.title}. Section {self.section_no}: {self.section_title}.\n{self.text}"


@dataclass(frozen=True)
class Citation:
    sop_id: str
    version: str
    title: str
    section: str
    section_title: str
    page: int

    def label(self) -> str:
        return f"{self.sop_id} v{self.version}, section {self.section} ({self.section_title}), page {self.page}"


class AnswerStatus(str, Enum):
    ANSWERED = "answered"
    NOT_FOUND = "not_found"        # SOPs available to this user don't cover it


@dataclass
class Answer:
    status: AnswerStatus
    answer: str
    citations: list[Citation] = field(default_factory=list)
    conflict: bool = False
    cached: bool = False
    index_generation: int = 0


@dataclass(frozen=True)
class ScoredChunk:
    chunk: Chunk
    score: float
