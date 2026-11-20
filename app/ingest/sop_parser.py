"""Turn extracted PDF blocks into an SopDocument and section-based chunks.

Chunking strategy (see design_review.md): one chunk per numbered SOP section.
SOP sections are short (50-250 words) and are the unit staff ask about and the
unit we cite, so a section boundary is a better split than a fixed character
window. Table rows are rendered as "Header: value | Header: value" so a row keeps
its meaning even if it is the only thing retrieved. Sections longer than
MAX_CHUNK_CHARS are split between items (never mid-row), and every split repeats
the section heading.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.domain.access import AccessPolicy
from app.domain.errors import IngestionError
from app.domain.models import Chunk, SopDocument
from app.ingest.pdf_extract import Block, ExtractedPdf

MAX_CHUNK_CHARS = 1500

_RUNNING = re.compile(r"SOP-(\d+)\s*\|\s*Version\s+([\w.]+)\s*\|\s*Effective\s+(\d{4}-\d{2}-\d{2})")
_TITLE = re.compile(r"^(SOP-\d+):\s*(.+)$")
# "2. When you discover fire or smoke (R.A.C.E.)" -- titles may contain dots; a
# heading is short, does not end with a full stop, and is the next number in sequence.
_HEADING = re.compile(r"^(\d{1,2})\.\s+([A-Z].{0,80})$")
_STEP = re.compile(r"^(\d{1,2})\s+\S")
_BULLET = "•"


@dataclass
class _Section:
    no: str
    title: str
    page_start: int
    page_end: int
    items: list[str]
    last_step: int = 0


def _render_table(rows: list[list[str]]) -> list[str]:
    header, body = rows[0], rows[1:]
    out = []
    for r in body:
        cells = [f"{h}: {v}" for h, v in zip(header, r) if v]
        out.append(" | ".join(cells))
    return out


def _starts_item(sec: _Section, text: str) -> bool:
    """A line starts a new item if it is a bullet, or the NEXT step number in sequence.
    Checking the sequence matters: a wrapped sentence can begin with a number
    ("...until the patient turns / 21 or for 3 years") and must not become a step."""
    if text.startswith(_BULLET):
        return True
    m = _STEP.match(text)
    if m and int(m.group(1)) == sec.last_step + 1:
        sec.last_step += 1
        return True
    return False


def parse_sop(extracted: ExtractedPdf, file_name: str, policy: AccessPolicy) -> tuple[SopDocument, list[Chunk]]:
    m = _RUNNING.search(extracted.running_header)
    if not m:
        raise IngestionError(f"{file_name}: running header with SOP id/version not found")
    sop_id, version, effective = f"SOP-{int(m.group(1)):02d}", m.group(2), m.group(3)

    blocks = extracted.blocks
    # Title: 'SOP-xx: Title' line plus continuation lines up to the metadata table.
    title_parts: list[str] = []
    i = 0
    while i < len(blocks) and not (blocks[i].kind == "line" and _TITLE.match(blocks[i].text)):
        i += 1
    if i == len(blocks):
        raise IngestionError(f"{file_name}: title line not found")
    tm = _TITLE.match(blocks[i].text)
    if tm.group(1) != sop_id:
        raise IngestionError(f"{file_name}: title id {tm.group(1)} != header id {sop_id}")
    title_parts.append(tm.group(2))
    i += 1
    while i < len(blocks) and blocks[i].kind == "line":
        title_parts.append(blocks[i].text)
        i += 1
    if i == len(blocks) or blocks[i].rows[0][0] != "Department":
        raise IngestionError(f"{file_name}: metadata table not found after title")
    meta: dict[str, str] = {}
    for row in blocks[i].rows:
        for k, v in zip(row[0::2], row[1::2]):
            meta[k] = v
    i += 1

    if meta.get("Version") != version:
        raise IngestionError(f"{file_name}: header table version {meta.get('Version')} != running header {version}")
    access_field = meta.get("Access", "")
    level = policy.level_for_access_field(access_field)
    if level is None:
        # Fail closed: an unrecognised Access value must never default to 'all'.
        raise IngestionError(f"{file_name}: unknown Access value {access_field!r}")

    sections: list[_Section] = []
    change_note = ""
    in_revision = False
    for b in blocks[i:]:
        if b.kind == "line":
            if b.text == "Revision history":
                in_revision = True
                continue
            if b.text.startswith("Approved by:"):
                continue
            if in_revision:
                continue
            hm = _HEADING.match(b.text)
            if hm and not b.text.endswith(".") and int(hm.group(1)) == len(sections) + 1:
                sections.append(_Section(hm.group(1), hm.group(2).strip(), b.page, b.page, []))
                continue
            if not sections:
                raise IngestionError(f"{file_name}: text before first section: {b.text[:40]!r}")
            sec = sections[-1]
            sec.page_end = b.page
            if _starts_item(sec, b.text) or not sec.items or sec.items[-1].startswith("§table"):
                sec.items.append(b.text)
            else:
                sec.items[-1] += " " + b.text           # wrapped continuation line
        else:
            if in_revision:
                if b.rows and b.rows[0][:2] == ["Version", "Date"] and len(b.rows) > 1:
                    change_note = b.rows[1][2] if len(b.rows[1]) > 2 else ""
                continue
            if not sections:
                raise IngestionError(f"{file_name}: table before first section")
            sec = sections[-1]
            sec.page_end = b.page
            sec.items.extend("§table " + r for r in _render_table(b.rows))

    if not sections:
        raise IngestionError(f"{file_name}: no numbered sections found")

    title = " ".join(title_parts)
    canonical = "\n".join(
        [sop_id, version, effective, title, access_field]
        + [f"## {s.no}. {s.title}\n" + "\n".join(s.items) for s in sections]
    )
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    doc = SopDocument(
        sop_id=sop_id, title=title, version=version, effective_date=effective,
        access_field=access_field, access_level=level, content_hash=content_hash,
        file_name=file_name, page_count=extracted.page_count, change_note=change_note,
    )
    return doc, _chunk(doc, sections)


def _chunk(doc: SopDocument, sections: list[_Section]) -> list[Chunk]:
    chunks: list[Chunk] = []

    def emit(sec: _Section, items: list[str]) -> None:
        body = "\n".join(it.removeprefix("§table ") for it in items)
        ordinal = len(chunks)
        chunks.append(Chunk(
            chunk_id=f"{doc.sop_id}@{doc.content_hash[:12]}#{ordinal}",
            sop_id=doc.sop_id, version=doc.version, title=doc.title,
            access_level=doc.access_level, section_no=sec.no, section_title=sec.title,
            page_start=sec.page_start, page_end=sec.page_end, text=body, ordinal=ordinal,
        ))

    for sec in sections:
        buf: list[str] = []
        size = 0
        for it in sec.items:
            if buf and size + len(it) > MAX_CHUNK_CHARS:
                emit(sec, buf)
                buf, size = [], 0
            buf.append(it)
            size += len(it) + 1
        if buf:
            emit(sec, buf)
    return chunks
