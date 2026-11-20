"""End-to-end evaluation, in the order the brief asks for:

  1. fresh index of data/sops, run eval_questions.csv (15)
  2. warm the cache with the front-desk questions that are about to change
  3. re-index with data/sop_updates/2026-11-01, then re-run it to prove idempotence
  4. run eval_questions_after_update.csv (8, different roles)
  5. run pii_test_inputs.csv through the same service

Writes eval/results_<provider>.json and docs/eval_results_<provider>.md.
Usage: python -m eval.run_eval            (provider from LLM_PROVIDER, default fake)
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from app.config import Settings
from app.container import build_container
from app.domain.errors import PatientDataDetectedError
from app.ingest.indexer import run_ingestion
from app.observability import configure_logging
from eval.criteria import C, EXPECTED_DOCS

ROOT = Path(__file__).resolve().parent.parent
ROLE_USER = {"front_desk": "U1001", "nurse": "U2001", "doctor": "U3001", "pharmacist": "U4001",
             "clinic_manager": "U5001", "hr": "U6001"}


def grade(qid: str, qtype: str, ans, trace, context_meta: list[dict], role_levels: frozenset[str]) -> dict:
    crit = C[qid]
    text = ans.answer
    cited = {c.sop_id: c.version for c in ans.citations}
    checks: dict[str, bool] = {}
    if crit.get("refuse"):
        checks["refused"] = ans.status.value == "not_found" and not ans.citations
    else:
        checks["answered"] = ans.status.value == "answered"
        for s in crit.get("cite", []):
            checks[f"cites {s}"] = s in cited
        if crit.get("cite_any"):
            checks["cites one of " + "/".join(crit["cite_any"])] = any(s in cited for s in crit["cite_any"])
        for s, v in crit.get("cite_version", {}).items():
            if s in cited:
                checks[f"{s} cited as v{v}"] = cited[s] == v
        for pat in crit.get("must", []):
            checks[f"has /{pat}/"] = re.search(pat, text, re.I) is not None
    for pat in crit.get("must_not", []):
        checks[f"lacks /{pat}/"] = re.search(pat, text, re.I) is None
    if crit.get("no_conflict"):
        checks["no conflict reported"] = not ans.conflict
    for s, v in crit.get("forbid_versions", {}).items():
        checks[f"no {s} v{v} in context"] = not any(m["sop_id"] == s and m["version"] == v for m in context_meta)
    for s in crit.get("forbid_sops", []):
        checks[f"no {s} in context or answer"] = s not in cited and all(m["sop_id"] != s for m in context_meta) \
            and s not in text
    if crit.get("no_duplicate_chunks"):
        texts = [r["chunk_id"].split("@")[0] + "#" + r["section"] for r in trace.retrieved]
        checks["no duplicate retrieved sections"] = len(texts) == len(set(texts))
    # Governance invariants, every question:
    checks["context within role access"] = all(m["access_level"] in role_levels for m in context_meta)
    checks["context only from published versions"] = all(m["active"] for m in context_meta)

    exp = EXPECTED_DOCS.get(qid, [])
    ctx_docs = {m["sop_id"] for m in context_meta}
    quality = {}
    if crit.get("conflict_expected"):
        quality["conflict_flagged"] = ans.conflict
        quality["recommends_stricter"] = bool(re.search(crit["stricter"], text, re.I)) and \
            bool(re.search(r"strict|recommend|follow|newer|more specific|safer", text, re.I))
    return {
        "id": qid, "type": qtype, "pass": all(checks.values()), "checks": checks,
        "retrieval_hit": all(d in ctx_docs for d in exp) if exp else None,
        "status": ans.status.value, "answer": text,
        "citations": [c.label() for c in ans.citations], "conflict": ans.conflict, "cached": ans.cached,
        "context_docs": sorted(ctx_docs),
        "top_score": trace.retrieved[0]["score"] if trace.retrieved else None, "llm_called": trace.llm_called, "refusal_reason": trace.refusal_reason,
        "latency_ms": round(trace.latency_ms, 1), "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens, "cost_usd": trace.cost_usd, **quality,
    }


def _context_meta(c, trace) -> list[dict]:
    snap = c.index.snapshot()
    by_id = {ch.chunk_id: ch for ch in snap.chunks}
    out = []
    for cid in trace.context_chunk_ids:
        ch = by_id.get(cid)
        sop, h = cid.split("@")[0], cid
        out.append({"chunk_id": h, "sop_id": sop, "version": ch.version if ch else "?",
                    "access_level": ch.access_level if ch else "?", "active": ch is not None})
    return out


def _run_set(c, path: Path, role_col: str) -> list[dict]:
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    out = []
    for r in rows:
        role = r[role_col]
        ans, trace = c.answers.ask(ROLE_USER[role], r["question"], correlation_id=f"eval-{r['id']}")
        meta = _context_meta(c, trace)
        g = grade(r["id"], r.get("type", "after_update"), ans, trace, meta, c.policy.levels_for_role(role))
        g.update(role=role, question=r["question"], expected=r["expected_answer"])
        out.append(g)
        time.sleep(float(os.getenv("EVAL_SLEEP_S", "0")))   # pacing for free-tier rate limits
        print(f"  {r['id']:<4} {'PASS' if g['pass'] else 'FAIL'}  {role:<14} {g['status']:<10} "
              f"{', '.join(x.split(',')[0] for x in g['citations']) or '-'}", flush=True)
    return out


def main() -> int:
    s = Settings.from_env()
    configure_logging(os.getenv("EVAL_LOG_LEVEL", "WARNING"))
    tmp = tempfile.mkdtemp(prefix="sop-eval-")
    s = s.__class__(**{**s.__dict__, "db_path": str(Path(tmp) / "eval.sqlite3")})
    c = build_container(s)
    t0 = time.time()
    print(f"provider={s.provider} chat={c.chat.model_id} embed={c.embedder.model_id}")

    print("1) index original library")
    r1 = run_ingestion(str(ROOT / "data/sops"), c.db, c.embedder, c.policy)
    print("2) eval_questions.csv")
    before = _run_set(c, ROOT / "data/eval_questions.csv", "asked_as_role")

    print("3) warm cache with soon-to-be-stale answers (front desk)")
    for q in ["What percentage of slots should be kept free for walk-ins?",
              "Up to what amount can front-desk staff approve a refund?"]:
        c.answers.ask("U1001", q, correlation_id="eval-warm")
    warm_hit = c.answers.ask("U1001", "Up to what amount can front-desk staff approve a refund?",
                             correlation_id="eval-warm")[0].cached

    print("4) re-index with 2026-11-01 snapshot, then again (idempotence)")
    r2 = run_ingestion(str(ROOT / "data/sop_updates/2026-11-01"), c.db, c.embedder, c.policy)
    r3 = run_ingestion(str(ROOT / "data/sop_updates/2026-11-01"), c.db, c.embedder, c.policy)
    print("5) eval_questions_after_update.csv")
    after = _run_set(c, ROOT / "data/eval_questions_after_update.csv", "asked_as_role")

    print("6) PII guard")
    pii = []
    for r in csv.DictReader(open(ROOT / "data/pii_test_inputs.csv", encoding="utf-8")):
        expect_flag = r["expected_guard_result"].startswith("flag")
        try:
            c.answers.ask("U2001", r["input"], correlation_id="eval-pii")
            flagged, cats = False, []
        except PatientDataDetectedError as e:
            flagged, cats = True, e.details["categories"]
        pii.append({"input": r["input"], "expected": r["expected_guard_result"], "flagged": flagged,
                    "categories": cats, "pass": flagged == expect_flag})

    def rate(rows, t=None):
        sel = [x for x in rows if t is None or x["type"] == t]
        return f"{sum(x['pass'] for x in sel)}/{len(sel)}"

    summary = {
        "provider": s.provider, "chat_model": c.chat.model_id, "embed_model": c.embedder.model_id,
        "before": {"overall": rate(before), "answerable": rate(before, "answerable"),
                   "unanswerable": rate(before, "unanswerable"), "conflict": rate(before, "conflict")},
        "after_update": rate(after), "pii": f"{sum(p['pass'] for p in pii)}/{len(pii)}",
        "retrieval_hit": f"{sum(1 for x in before + after if x['retrieval_hit'])}/"
                         f"{sum(1 for x in before + after if x['retrieval_hit'] is not None)}",
        "governance_violations": sum(1 for x in before + after
                                     if not (x["checks"]["context within role access"]
                                             and x["checks"]["context only from published versions"])),
        "cache_warm_hit_before_reindex": warm_hit,
        "reindex": {k: r2[k] for k in ("added", "updated", "withdrawn", "renamed", "texts_embedded",
                                       "embeddings_reused", "embedding_calls")} | {"unchanged": len(r2["unchanged"])},
        "reindex_second_run": {"status": r3["status"], "embedding_calls": r3["embedding_calls"]},
        "initial_index": {"documents": len(r1["added"]), "texts_embedded": r1["texts_embedded"]},
        "llm_calls": sum(1 for x in before + after if x["llm_called"]),
        "total_cost_usd": round(sum(x["cost_usd"] for x in before + after), 6),
        "total_tokens": sum(x["input_tokens"] + x["output_tokens"] for x in before + after),
        "mean_latency_ms": round(sum(x["latency_ms"] for x in before + after) / len(before + after), 1),
        "wall_time_s": round(time.time() - t0, 1),
    }
    for t in ("conflict_flagged", "recommends_stricter"):
        summary[t] = f"{sum(1 for x in before if x.get(t))}/{sum(1 for x in before if t in x)}"

    out = {"summary": summary, "before": before, "after_update": after, "pii": pii}
    (ROOT / "eval").mkdir(exist_ok=True)
    (ROOT / f"eval/results_{s.provider}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    (ROOT / f"docs/eval_results_{s.provider}.md").write_text(render_md(out), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def render_md(out: dict) -> str:
    s = out["summary"]
    L = [f"# Evaluation results: provider `{s['provider']}`", "",
         f"Chat model `{s['chat_model']}`, embedding model `{s['embed_model']}`. "
         "Generated by `make eval` (`python -m eval.run_eval`). Pass criteria: `eval/criteria.py`.", "",
         "## Summary", "", "| Metric | Result |", "| --- | --- |",
         f"| Original library, all 15 | **{s['before']['overall']}** |",
         f"| - answerable (10) | {s['before']['answerable']} |",
         f"| - unanswerable (3) | {s['before']['unanswerable']} |",
         f"| - conflict (2), both rules surfaced | {s['before']['conflict']} |",
         f"| - conflict flag set / stricter rule recommended | {s['conflict_flagged']} / {s['recommends_stricter']} |",
         f"| After update, 8 questions as different roles | **{s['after_update']}** |",
         f"| PII guard, 5 inputs | **{s['pii']}** |",
         f"| Retrieval: expected SOP(s) in LLM context | {s['retrieval_hit']} |",
         f"| Governance violations (unauthorised or superseded chunk in context) | {s['governance_violations']} |",
         f"| Cache served before re-index (warm) | {s['cache_warm_hit_before_reindex']} |",
         f"| LLM calls / tokens / cost | {s['llm_calls']} / {s['total_tokens']} / ${s['total_cost_usd']} |",
         f"| Mean latency per question | {s['mean_latency_ms']} ms |", "",
         "## Re-index report (2026-11-01 snapshot)", "", "```json", json.dumps(s["reindex"], indent=2), "```", "",
         f"Second run on the same snapshot: `{s['reindex_second_run']['status']}`, "
         f"{s['reindex_second_run']['embedding_calls']} embedding calls.", ""]
    for name, rows in (("Original library", out["before"]), ("After update", out["after_update"])):
        L += [f"## {name}", "", "| Id | Role | Result | Status | Citations | Failed checks |",
              "| --- | --- | --- | --- | --- | --- |"]
        for x in rows:
            failed = "; ".join(k for k, v in x["checks"].items() if not v) or "-"
            L.append(f"| {x['id']} | {x['role']} | {'PASS' if x['pass'] else 'FAIL'} | {x['status']} | "
                     f"{'; '.join(c.split(',')[0] + ' s' + c.split('section ')[1].split(' ')[0] for c in x['citations']) or '-'} | {failed} |")
        L += ["", "<details><summary>Answers</summary>", ""]
        for x in rows:
            L += [f"**{x['id']}** ({x['role']}) {x['question']}", "", "> " + x["answer"].replace("\n", "\n> "), ""]
        L += ["</details>", ""]
    L += ["## PII guard", "", "| Input | Expected | Flagged | Categories | Result |", "| --- | --- | --- | --- | --- |"]
    for p in out["pii"]:
        L.append(f"| {p['input']} | {p['expected']} | {p['flagged']} | {', '.join(p['categories']) or '-'} | "
                 f"{'PASS' if p['pass'] else 'FAIL'} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
