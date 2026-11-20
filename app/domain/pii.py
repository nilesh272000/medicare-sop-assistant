"""Patient-data guard.

Deterministic, local, and run before anything else touches the question: before the
cache, the embedder, the LLM and the logs. A flagged question is rejected (not masked),
so patient data never leaves the process and is never written anywhere.

Trade-off (see ADR-3): regexes have recall gaps for bare names ("ask Ramesh about his
fever"). We accept this for the PoC and bias the name rules toward over-flagging; a
false positive costs the user one rephrase, a false negative sends patient data to a
third-party LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Indian mobile: optional +91/0, then 10 digits starting 6-9, allowing one space/dash split.
_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
# Aadhaar: 12 digits, first digit 2-9, usually grouped 4-4-4.
_AADHAAR = re.compile(r"(?<!\d)[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
# MRN followed by an actual number (the word "MRN" alone is a normal policy question).
_MRN = re.compile(r"\b(?:MRN|UHID|medical record (?:no|number))\b[\s.:#-]*\d{3,}", re.I)
# Dates of birth: the DOB/born keyword near a date, or a date in brackets after a name.
_DATE = r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}"
_DOB = re.compile(rf"\b(?:DOB|D\.O\.B\.?|date of birth|born(?: on)?)\b[\s:]*\(?\s*{_DATE}", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@(?!medicare-clinics\.example\b)[\w-]+\.[\w.]+\b", re.I)
# Names: honorific + (initials) + capitalised word, e.g. "Mrs. S. Rao", "Mr Ramesh".
_HONORIFIC_NAME = re.compile(
    r"\b(?:Mr|Mrs|Ms|Miss|Smt|Shri|Sri|Kumari|Master|Baby)\b\.?\s+(?:[A-Z]\.\s*)*[A-Z][a-z]+"
)
# "patient Ramesh Kumar", "patient's name is Ramesh" -> capitalised word(s) after 'patient'.
_PATIENT_NAME = re.compile(
    r"\b(?i:patient)(?:'s)?(?:\s+name)?(?:\s+is)?[\s,:]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"
)
# Capitalised words that legitimately follow "patient" in SOP vocabulary.
_NOT_NAMES = {
    "Registration", "Identity", "Complaints", "Complaint", "Data", "Privacy", "Experience",
    "Safety", "Records", "Record", "Grievance", "The", "A", "An", "If", "Who", "What",
    "When", "Can", "Aadhaar", "PAN", "ID", "MRN", "UHID", "OPD", "IPD", "How", "Should", "Is", "Are", "Does", "Do", "With", "Without", "In", "On",
}


@dataclass(frozen=True)
class PiiFinding:
    category: str
    span: tuple[int, int]


def detect_patient_data(text: str) -> list[PiiFinding]:
    findings: list[PiiFinding] = []

    def add(cat: str, m: re.Match, group: int = 0) -> None:
        findings.append(PiiFinding(cat, m.span(group)))

    aadhaar_spans = []
    for m in _AADHAAR.finditer(text):
        add("aadhaar", m)
        aadhaar_spans.append(m.span())
    for m in _PHONE.finditer(text):
        # A 12-digit Aadhaar contains a 10-digit substring; don't double count.
        if not any(a <= m.start() and m.end() <= b for a, b in aadhaar_spans):
            add("phone", m)
    for m in _MRN.finditer(text):
        add("mrn", m)
    for m in _DOB.finditer(text):
        add("date_of_birth", m)
    for m in _EMAIL.finditer(text):
        add("email", m)
    for m in _HONORIFIC_NAME.finditer(text):
        add("name", m)
    for m in _PATIENT_NAME.finditer(text):
        words = m.group(1).split()
        if words and words[0] not in _NOT_NAMES:
            add("name", m, 1)
    return findings


def categories(findings: list[PiiFinding]) -> list[str]:
    return sorted({f.category for f in findings})
