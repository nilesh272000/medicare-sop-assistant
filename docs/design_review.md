# Design review of the AI proposal

The proposal is in [`ai_design_proposal.md`](ai_design_proposal.md), saved unedited before any code was written. This document says what was wrong or risky in it, what was kept, and records the main decisions as ADRs.

## 1. Weaknesses and risks in the proposal, and what was built instead

### W1. Documents are identified by file name and file-byte MD5
The proposal keys its registry on the **file name** and compares the **MD5 of the file bytes**. The update snapshot breaks this in three ways:

| Case in the 2026-11-01 snapshot | What the proposal does | Consequence |
| --- | --- | --- |
| SOP-05 renamed (`..._Segregation_and_Disposal.pdf` to `..._Management_Rules.pdf`), content unchanged | Treats it as one withdrawal plus one new document | Re-embeds for nothing. If the shared drive keeps both names, which is the client's stated problem, both copies stay indexed and retrieval returns duplicates (U08). |
| SOP-03 re-exported from the DMS: different bytes (PDF `Subject` metadata), identical text | Bytes MD5 differs, so it re-embeds | Wasted embeddings, a needless re-index and a full cache flush |
| An old and a new copy of the same SOP under different names | Both indexed as separate documents | Staff can get the superseded rule, which is the exact failure the client complained about |

**Instead:** identity is the **SOP id printed in the header**, and "changed" means the **sha256 of the normalised extracted text** (header fields + section text; file name, PDF metadata and revision-history table excluded) differs. If a snapshot holds two copies of one SOP id, the higher version wins and a warning is logged; two copies with the **same version but different content** reject the whole snapshot. See ADR-1. Measured on the update: 3 updated, 2 added, 1 withdrawn, 1 renamed, 10 unchanged (SOP-03 included); 13 chunk texts embedded (10 for the two new SOPs, 1 changed section in each of SOP-02, SOP-08 and SOP-11) and 16 reused.

### W2. Re-indexing is not atomic, so a crash leaves a mixed library
The proposal deletes a file's chunks from Chroma, re-inserts them, then updates a separate registry (SQLite or JSON), file by file. Two stores and no transaction means:
- a crash between delete and insert leaves a document **missing**; a crash after some files leaves **some SOPs new and some old**, and staff are served that mix;
- a crash after the Chroma write but before the registry write makes the next run think nothing happened, or re-ingest on top and create **duplicates**;
- queries running during a re-index see a half-updated library.

**Instead:** embeddings are computed first and stored content-addressed (safe to keep after a crash). Then the whole diff (deletes of superseded and withdrawn chunks, inserts of new ones, document rows, history, generation bump, cache purge, run report) is applied in **one SQLite transaction** (`Database.publish`). Readers use WAL snapshots, so they see generation N or N+1, never a mix. Tested by `test_crash_mid_publish_leaves_old_library_intact`, which injects a failure after the deletes. A second concurrent run is detected by an optimistic generation check (`test_concurrent_publish_is_detected`). See ADR-3.

### W3. The PII guard sends patient data to the LLM, and "mask and continue" still processes it
The proposal's second PII layer is *"an LLM classification call: does this question contain patient-identifying information?"*. That sends the patient's name and DOB to a third-party API to decide whether it may send them to a third-party API. The requirement says the assistant must **never process** patient information. Masking and answering still logs, embeds and caches a question that contained it.

**Instead:** a local, deterministic guard (`app/domain/pii.py`) runs **first**, before cache, embedding, LLM and logs. A flagged question is **rejected** with HTTP 422 and the categories found; nothing is stored, and the API error does not echo the input. The rules lean toward over-flagging (honorific + name, "patient <Name>", DOB/born + date, MRN/UHID + digits, Indian mobile, Aadhaar, non-company e-mail). 5/5 on the pack inputs and 0 false positives on 8 normal policy questions. See ADR-4.

### W4. A semantic cache keyed on role is unsafe for this content
The proposal returns a cached answer when a new question has cosine similarity above 0.95 for the same role. SOP questions differ in exactly the tokens that embeddings blur: *"within 2 hours"* vs *"within 24 hours"*, *"Rs 1,000"* vs *"Rs 1,500"*, *"lavender before light blue?"* vs *"light blue before lavender?"*. A wrong cache hit is a confidently wrong answer with valid-looking citations. It also needs an embedding call even on a hit, and it `FLUSHDB`s on every re-index run, including runs that changed nothing.

**Instead:** an exact key after light normalisation (case, whitespace, trailing punctuation) over *question + sorted access levels + index generation + prompt version + models + retrieval config*. A generation bump makes every old key unreachable, and old rows are deleted in the publish transaction. A no-op re-index does not bump the generation, so the cache survives it. See ADR-2.

### W5. Header parsing by regex over flattened page text fails on this pack, with no fail-closed path
In the extracted text, SOP-06/09/10 print the Access field across two lines, with the "Review cycle Annual" column in between: `Access Clinical staff (doctors, nurses, Review cycle Annual` / `pharmacists)`. A line regex gets `Clinical staff (doctors, nurses,`, which matches no key in `access_policy.json`. The proposal does not say what happens then. The likely outcome is a missing or default level on the three clinical-only SOPs.

**Instead:** the header is read as a **table** (pdfplumber cell extraction). The Access value must match the policy exactly after whitespace/case normalisation. An unknown value **rejects the snapshot** (nothing is published), and an unknown role gets **no** levels. Tested in `test_unknown_access_value_fails_closed` and `test_unknown_access_value_rejects_snapshot`.

### W6. Fixed 800-character chunks and page-level documents fit these SOPs poorly
- Tables (slot durations, bin colours, order of draw, emergency codes, leave) get cut mid-row, so "Red bag" can be separated from "IV tubes, gloves".
- The revision-history table becomes a retrievable chunk. In SOP-02 v2.4 it says *"Walk-in reserve increased from 20% to 25%"*, which puts the **superseded value** back into the context.
- An 800-character window ignores the unit staff ask about and that must be cited: the numbered section.

**Instead:** one chunk per numbered section (SOP sections are 50 to 250 words). Table rows are rendered as `Header: value | Header: value` so each row stands alone. The revision history is kept only as a change note in the ingest report. Retrieval is by section, but the LLM receives **all sections of the top 3 SOPs** (small-to-big). Q01's answer spans SOP-04 sections 2 to 5, and a whole SOP is about 600 tokens. Expected SOPs were in the context for 18/18 eval questions.

### W7. Business rules sit in the prompt, and the evaluation does not test the client's requirements
- *"Only answer using documents the user is allowed to see"* in the system prompt suggests the prompt is a control. It is not. The filter is the control, and the line gives a false sense of a second layer.
- *"Prefer the newer one"* for conflicts is a business rule in a prompt and not always right. For Q15 the brief wants the **stricter** rule.
- The 0.7 similarity threshold is a number with no calibration behind it.
- RAGAS with Gemini judging Gemini gives non-deterministic averages, and it does not test what the client cares about: stale versions, withdrawn SOPs, role leakage, duplicates.

**Instead:** access, PII, the relevance gate and citation validation are code with unit tests. The model's citations are labels (`C1`..`Cn`) mapped back to chunk metadata **in code**. Labels the model invents are dropped, and an "answer" with no valid citation becomes "I don't know". The eval uses deterministic per-question criteria (`eval/criteria.py`), including "superseded value must not appear", "withdrawn/unauthorised SOP must not be in the LLM context" and "no duplicate sections", plus governance invariants checked on every question.

### W8. Too much infrastructure and framework coupling for the problem size
Chroma + Redis + API containers and LangChain for about 1,200 vectors at full library size. LangChain types (`FakeListLLM`, `PyPDFLoader`, retrievers) would leak into the domain code, against the requirement to put the LLM behind an interface we own.

**Instead:** two small protocols (`ChatModel`, `Embedder` in `app/ports.py`), a REST adapter for Gemini, offline fakes, SQLite and numpy. One process, one file of state. Swapping to pgvector or Vertex AI Vector Search means re-implementing `VectorIndex.search` with the same pre-filter rule.

## 2. What was kept, and why

- **FastAPI REST service plus a CLI for ingestion.** Simple, and it gives OpenAPI for free.
- **Access filtering inside retrieval, not after generation.** The proposal had the right idea (metadata filter). It is now a pre-scoring mask with tests that inspect the prompts actually sent.
- **Header-derived metadata** (SOP id, version, access) on every chunk.
- **Validating citations against retrieved chunks.** Listed only as a risk in the proposal; built in as a hard check.
- **pdfplumber for tables.** The proposal's own risk note was right, so it became the main extractor.
- **A similarity threshold for refusals,** kept as a cost-saving first gate. The LLM's `not_found` and citation validation are the main refusal path. The threshold is set per embedding model and must be calibrated (see Limitations).
- **Gemini Flash + Gemini embeddings,** environment-variable configuration, retries with backoff (now with an explicit policy, below).

## 3. Failure handling: what is retried and what is not

| Call | Timeout | Retried? | Why |
| --- | --- | --- | --- |
| Gemini `generateContent` | `LLM_TIMEOUT_S` (20 s) | Yes on timeout, connection error, 429, 5xx. Up to `MAX_RETRIES` (2), exponential backoff with jitter, honours `Retry-After` | Read-only call with no side effects at the provider. A retry costs tokens, nothing else |
| Gemini embeddings | `EMBED_TIMEOUT_S` (10 s) | Same | Same; ingestion results are content-addressed, so a retried batch cannot create duplicates |
| Any other 4xx (bad key, bad request, unknown model) | - | No | Deterministic; the same request fails again. Returned as 503 `upstream_unavailable` with `retryable:false` |
| Unparseable model reply | - | No (not cached either) | Surfaced as 503 so the client can retry. Silently converting it to "I don't know" would cache a wrong refusal |
| Re-index publish | SQLite busy timeout 30 s | No automatic retry | A failed or racing publish rolls back. The operator (or the next scheduled run) re-runs, which is idempotent and reuses the embeddings already paid for |

Error responses always have the form `{"error": {"code", "message", "details", "correlation_id"}}` (codes: `invalid_question`, `validation_error`, `unknown_user`, `patient_data_detected`, `index_not_ready`, `upstream_unavailable`, `ingestion_error`, `internal_error`). Validation errors never echo the request body, because it may contain patient data.

## 4. State and consistency

| State | Lives in | On restart | Under concurrency |
| --- | --- | --- | --- |
| Published library (documents, chunks) | SQLite `var/sop_index.sqlite3` (Docker volume) | Persists | Single writer via `BEGIN IMMEDIATE`; readers see WAL snapshots |
| Embeddings | SQLite, keyed by sha256(model + text) | Persists; never invalid | Append-only, `INSERT OR IGNORE` |
| In-memory vector index | Process memory | Rebuilt lazily on the first request | Reloaded when `generation` changes; swap under a lock; each request uses one snapshot object throughout |
| Answer cache | SQLite `answer_cache` | Persists (TTL 7 days) | The write is conditional on the generation still being current, so a request that started before a publish cannot store an old-generation answer after it |
| Ingest run reports, document history | SQLite | Persists | Audit trail of what changed in each run |

**If re-indexing crashes halfway:** before the publish transaction, only content-addressed embeddings were written, and they are harmless. During it, SQLite rolls back. In both cases staff keep getting generation N, complete and consistent. The retry reuses the stored embeddings, so the crash costs nothing extra. **Staff cannot get a mix of old and new chunks.**

**Where the access check sits:** in `VectorIndex.search`. Rows whose access level the caller's role may not read are masked out **before scoring**. Sibling sections added for context are re-checked. The role comes from the user directory, never from the question. No question wording can change the candidate set, because the question only affects ranking within the permitted rows. Tests send prompt-injection questions as front desk and assert that no excerpt from SOP-13/14/06 appears in any prompt the LLM received. Refusals use one fixed text, so a front-desk user cannot tell "no SOP covers this" from "an SOP you can't read covers this".

**Known concurrency gap:** two identical uncached questions arriving together both call the LLM (no single-flight). Harmless (same answer, double cost); worth adding at higher volume.

## 5. Architecture Decision Records

### ADR-1: Document identity and change detection (overrules the AI)
**Context.** The document team drops full snapshots. File names are not stable, PDFs get re-exported, and the shared drive is known to hold old copies next to new ones. We must re-embed only real changes and never serve a superseded SOP.

**Options.**
1. *File name + MD5 of bytes* (AI proposal). Simple. Breaks on rename (duplicates or needless churn), re-export (needless re-embed and cache flush) and old+new copies (both indexed).
2. *SOP id + MD5 of bytes.* Fixes rename and duplicates. Still re-embeds on re-export, and cannot tell a real change from a metadata change.
3. *SOP id + sha256 of normalised extracted text* (chosen), with explicit duplicate rules.

**Decision.** Option 3. The text hash covers SOP id, version, effective date, title, Access text and section contents. File name, PDF metadata and the revision-history table are excluded. Within one snapshot, the highest version of an SOP id wins (warning). Same version with different content rejects the snapshot. A lower version than the published one is rejected unless `--allow-downgrade`.

**Consequences.** Renames and re-exports cost nothing (SOP-03 and SOP-05 in the update). Embeddings are also content-addressed per chunk, so editing one section of an SOP re-embeds only that section (16 of 29 chunk texts reused in the update). Trade-offs: (a) we trust the printed SOP id. If the document team mislabels a new SOP with an existing id, the higher version replaces it; the run report shows the title, but nothing blocks it. (b) Upgrading the PDF extractor can change normalised text, so every SOP is re-embedded once, at a cost of a few cents. (c) Content changes without a version bump are applied, but flagged.

**Arguing the other side.** File-name identity needs no parsing to work out identity, so a PDF with a damaged header still gets indexed. We chose to reject such PDFs: for a clinical SOP, an unidentifiable document should not be served.

### ADR-2: Answer-cache key and invalidation (overrules the AI)
**Context.** About 2,000 questions a day with many repeats, and 300 staff in 6 roles. The cache must never serve a superseded SOP's answer and never leak across roles.

**Options.**
1. *Semantic cache, role + similarity > 0.95, flush on re-index* (AI proposal). Higher hit rate on paraphrases, but false hits on number-level differences, an embedding call per lookup, and a full flush even on no-op runs.
2. *Exact normalised key per user.* Safe, but low hit rate; it does not share answers across staff with the same rights.
3. *Exact normalised key over question + access levels + generation (+ prompt/model/retrieval config)* (chosen).
4. *No cache.* Simplest. Costs about 40% more LLM calls (estimate) and adds latency on repeats.

**Decision.** Option 3. Keying on **levels** rather than role lets nurse/doctor/pharmacist share answers (identical rights), and any difference in rights gives a different key. The generation is bumped only by a publish that changed something. The cache write is conditional on the generation still being current.

**Consequences.** Paraphrases miss the cache. That costs about $0.001 per miss and is never a wrong answer. Every content change invalidates the whole cache, including answers about SOPs that did not change. That is acceptable at a few re-indexes a month and avoids tracking which documents each answer depended on (which is also wrong when a *new* SOP should now answer a question that used to be refused, as SOP-16 does for U05). Upgrade path if the hit rate matters: add a canonical-question layer (FAQ ids) on top, not a similarity threshold.

### ADR-3: Index storage and atomic publish
**Context.** Re-indexing must leave no stale chunks, be idempotent, and survive a crash halfway without staff seeing a mix. The library will reach about 200 SOPs (about 1,200 chunks).

**Options.**
1. *Chroma (or any vector DB) + separate registry, delete/insert per file* (AI proposal). Not atomic across stores (W2).
2. *Blue/green collections in a vector DB:* build a full new collection, then switch an alias. Atomic switch, but re-writes every vector on every run (fine at this size), and needs alias support plus garbage collection.
3. *One SQLite database holding documents, chunks, embeddings, generation and cache; an in-memory numpy index rebuilt when the generation changes* (chosen).

**Decision.** Option 3. Metadata, chunks and the generation pointer change in one ACID transaction. Brute-force cosine over 1,200 x 768 floats takes well under a millisecond. Access filtering is a boolean mask before scoring.

**Consequences.** Simple, testable offline, no extra services. Trade-offs: single node, single writer, and the index sits in each process's memory (fine for 300 users; about 4 MB at full library size). Horizontal scaling or more than about 100k chunks would mean moving to Postgres + pgvector, where the same "one transaction + generation" design carries over directly. Chroma/Redis would not give us that transaction.

### ADR-4: Patient data: reject locally, don't mask or classify remotely
**Context.** Staff paste names, phone numbers, Aadhaar numbers, MRNs and DOBs. The requirement is to never process patient information.

**Options.** (1) Remote LLM classifier + masking (AI proposal). (2) Local NER model (spaCy or Presidio) + masking. (3) Local deterministic rules + **reject** (chosen).

**Decision.** Option 3 for the PoC. It is deterministic, unit-testable, adds no latency and nothing leaves the process. Rejecting rather than masking means a flagged question is never logged, embedded, cached or sent anywhere, and staff learn the habit.

**Consequences.** Recall gaps for bare first names without a cue ("ask Ramesh about his fever"). The rules lean toward false positives, and a false positive costs one rephrase. Next step: add a local NER model (Presidio with an Indian-names list) as a second local layer, measured on a larger labelled set from real (redacted) traffic.
