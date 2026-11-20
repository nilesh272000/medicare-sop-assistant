"""Pass criteria per question, written down before looking at model output.

What "pass" means, by question type:
* answerable  - status 'answered', cites the expected SOP, and contains every MUST fact
                (the number / rule a staff member would act on). Not judged on wording.
* unanswerable- status 'not_found' and no citations.
* conflict    - status 'answered', cites BOTH SOPs and states both rules. The
                'conflict' flag and recommending the stricter rule are reported
                separately as quality metrics (the offline stub cannot reason about them).
* after-update- as above, plus freshness rules: the superseded value must NOT appear,
                citations must carry the new version, withdrawn / unauthorised SOPs must
                not appear in the answer or in the LLM context.
Patterns are case-insensitive regexes.
"""

C: dict[str, dict] = {
    "Q01": {"cite": ["SOP-04"], "must": [r"2\s*hours?", r"OH-05", r"soap|running water", r"6\s*weeks"]},
    "Q02": {"cite": ["SOP-05"], "must": [r"\bred\b"]},
    "Q03": {"cite": ["SOP-02"], "must": [r"15\s*min"]},
    "Q04": {"cite": ["SOP-06"], "must": [r"do not use|don.t use|not be used", r"1\s*hour|one hour"]},
    "Q05": {"cite": ["SOP-10"], "must": [r"light blue"], "must_not": [r"lavender (?:is|should be|first)\b[^.]*first"]},
    "Q06": {"cite": ["SOP-08"], "must": [r"1,?000"]},
    "Q07": {"cite": ["SOP-07"], "must": [r"missing|abduct"]},
    "Q08": {"cite": ["SOP-12"], "must": [r"1\s*hour|one hour", r"data protection officer|\bDPO\b"]},
    "Q09": {"cite": ["SOP-15"], "must": [r"\bno\b|cannot|can.t|not (?:be )?(?:allowed|permitted|prescribed)|prohibited"]},
    "Q10": {"cite": ["SOP-12"], "must": [r"\b21\b"]},
    "Q11": {"refuse": True},
    "Q12": {"refuse": True},
    "Q13": {"refuse": True},
    "Q14": {"cite": ["SOP-04", "SOP-11"], "must": [r"\b2\s*hours?", r"24\s*hours?"], "conflict_expected": True,
            "stricter": r"2[- ]hour"},
    "Q15": {"cite": ["SOP-06", "SOP-09"], "must": [r"twice", r"once"], "conflict_expected": True,
            "stricter": r"twice"},
    # ---- after the 2026-11-01 snapshot ----
    "U01": {"cite": ["SOP-02"], "cite_version": {"SOP-02": "2.4"}, "must": [r"25\s*%"], "must_not": [r"\b20\s*%"]},
    "U02": {"cite": ["SOP-08"], "cite_version": {"SOP-08": "2.1"}, "must": [r"1,?500"],
            "must_not": [r"(?<![\d,])1,?000(?![\d,])"]},
    "U03": {"cite_any": ["SOP-04", "SOP-11"], "cite_version": {"SOP-11": "2.0"}, "must": [r"\b2\s*hours?"],
            "no_conflict": True, "forbid_versions": {"SOP-11": "1.2"}},
    "U04": {"refuse": True, "forbid_sops": ["SOP-15"]},
    "U05": {"cite": ["SOP-16"], "must": [r"alarm|code red|112"]},
    "U06": {"refuse": True, "forbid_sops": ["SOP-17"]},
    "U07": {"cite": ["SOP-17"], "must": [r"50,?000"]},
    "U08": {"cite": ["SOP-05"], "must": [r"\bred\b"], "no_duplicate_chunks": True},
}

EXPECTED_DOCS = {  # used for the retrieval metric: is the expected SOP in the LLM context?
    "Q01": ["SOP-04"], "Q02": ["SOP-05"], "Q03": ["SOP-02"], "Q04": ["SOP-06"], "Q05": ["SOP-10"],
    "Q06": ["SOP-08"], "Q07": ["SOP-07"], "Q08": ["SOP-12"], "Q09": ["SOP-15"], "Q10": ["SOP-12"],
    "Q14": ["SOP-04", "SOP-11"], "Q15": ["SOP-06", "SOP-09"], "U01": ["SOP-02"], "U02": ["SOP-08"],
    "U03": ["SOP-11"], "U05": ["SOP-16"], "U07": ["SOP-17"], "U08": ["SOP-05"],
}
