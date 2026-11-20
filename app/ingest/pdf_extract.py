"""PDF -> ordered blocks (text lines and tables) with page numbers.

The only module that knows about pdfplumber. Tables are detected separately from
running text so each table row can be rendered as a self-contained line
("Bin / container: Red bag | What goes in: ... ") instead of column-scrambled text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pdfplumber

_RUNNING_HEADER = re.compile(r"^MediCare Clinics\s+SOP-\d+\s*\|\s*Version")
_FOOTER = re.compile(r"^Internal document\. Uncontrolled when printed\.")


@dataclass
class Block:
    page: int
    top: float
    kind: str                     # "line" | "table"
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class ExtractedPdf:
    running_header: str           # e.g. "MediCare Clinics SOP-04 | Version 3.0 | Effective 2026-06-01"
    blocks: list[Block]
    page_count: int


def _clean_cell(c: str | None) -> str:
    return re.sub(r"\s+", " ", c or "").strip()


def extract_pdf(path: str) -> ExtractedPdf:
    blocks: list[Block] = []
    running_header = ""
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            tables = page.find_tables()
            for t in tables:
                rows = [[_clean_cell(c) for c in row] for row in t.extract()]
                rows = [r for r in rows if any(r)]
                if rows:
                    blocks.append(Block(page=pno, top=t.bbox[1], kind="table", rows=rows))
            # Text outside every table bbox.
            outside = page
            for t in tables:
                outside = outside.outside_bbox(t.bbox)
            for line in outside.extract_text_lines(layout=False, strip=True):
                text = re.sub(r"\s+", " ", line["text"].replace("(cid:127)", "•")).strip()
                if not text:
                    continue
                if _RUNNING_HEADER.match(text):
                    running_header = running_header or text
                    continue
                if _FOOTER.match(text):
                    continue
                blocks.append(Block(page=pno, top=line["top"], kind="line", text=text))
        page_count = len(pdf.pages)
    blocks.sort(key=lambda b: (b.page, b.top))
    return ExtractedPdf(running_header=running_header, blocks=blocks, page_count=page_count)
