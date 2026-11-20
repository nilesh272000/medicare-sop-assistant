# AI design proposal (saved unedited)

**Tool:** Claude (Anthropic), chat session, before any code was written.
**Date:** 2026-11-19

## Prompt

> I'm building a production-minded proof of concept for a client. MediCare Clinics runs 25 outpatient clinics. Staff (300 people, ~2,000 questions/day, many repeated) need an internal assistant that answers questions only from the current SOP PDFs and cites SOP id, version, section and page. It must say "I don't know" when the SOPs don't cover a question. Some SOPs are restricted (clinical staff only; HR and managers only) — there is an access_policy.json mapping the "Access" header field to levels, and roles to levels. Users are identified by user id (staff_users.csv maps id → role). It must never process patient information (staff paste names, phone numbers, Aadhaar, MRNs into questions). The library has 15 SOP PDFs now (some with tables) and will grow to 200+. The document team drops a full snapshot of the library into a folder a few times a month; re-indexing must re-embed only changed documents, add new ones, remove withdrawn ones, leave no stale chunks, and be idempotent. Repeated questions should be cached, but the cache must never serve answers from superseded documents or leak across roles. Stack: Python, any LLM provider (free tier), REST API, offline test suite with a fake LLM. Please propose an architecture: components, data flow, storage, chunking, access control, re-indexing, caching, PII guard, refusal, evaluation, and the main risks.

## Proposal (verbatim)

### 1. Overview

A Python FastAPI service with three main flows: **ingestion**, **query**, and **evaluation**. Use LangChain for orchestration, ChromaDB as the vector store (persistent, local, metadata filtering built in), Google Gemini (`gemini-1.5-flash`) for generation and `text-embedding-004` for embeddings, and Redis for caching.

```
PDFs ──► Loader (PyPDFLoader) ──► Header parser ──► RecursiveCharacterTextSplitter ──► Embeddings ──► ChromaDB
                                                                                                      ▲
User ──► FastAPI /ask ──► PII guard ──► Cache (Redis) ──► Retriever (role filter) ──────────────────────┘
                                                              │
                                                              ▼
                                                 Gemini (answer + citations) ──► Response
```

### 2. Ingestion

- Load each PDF with `PyPDFLoader`, one `Document` per page (gives page numbers for free).
- Parse the header block with regex: `SOP id`, `Version`, `Effective date`, `Access`. Map `Access` to a level using `access_policy.json` and store it as metadata.
- Chunk with `RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=150)`. This keeps chunks small enough for precise retrieval while overlap preserves context across boundaries. Tables are extracted as text by the loader.
- Each chunk gets metadata: `sop_id, title, version, access_level, page, source_file`. Detect the section by searching for the nearest preceding `N. Heading` line.
- Store a document registry (SQLite or a JSON file) keyed by **file name** with the file's MD5 hash and last-ingested timestamp.

### 3. Incremental re-indexing

For each run over a snapshot folder:

1. List all PDFs and compute the MD5 of each file.
2. Compare to the registry:
   - new file name → ingest,
   - same name, different hash → delete existing chunks for that file (`collection.delete(where={"source_file": name})`) and re-ingest,
   - same name, same hash → skip,
   - name in registry but not in snapshot → delete its chunks (withdrawn).
3. Update the registry and write a JSON change report (`added / updated / removed / unchanged`).

Running it twice is idempotent because the hashes match on the second run, so nothing is re-embedded.

### 4. Access control

- Resolve `user_id → role` from `staff_users.csv`, then `role → allowed levels` from `access_policy.json`.
- Pass a metadata filter to Chroma: `where={"access_level": {"$in": allowed_levels}}`. Restricted chunks are never returned by the retriever, so they never reach the LLM.
- Also add a line to the system prompt: "Only answer using documents the user is allowed to see."

### 5. Query flow

1. PII guard (below).
2. Cache lookup.
3. Retrieve top-k=5 chunks with the role filter (MMR to diversify).
4. Prompt Gemini with a system prompt: answer only from context, cite `[SOP-xx vY, §N, p.P]`, say "I don't know" if the context doesn't contain the answer, and if two SOPs disagree, mention both and prefer the newer one.
5. Parse citations from the answer and return `{answer, citations[]}`.

### 6. Caching

Use a **semantic cache** in Redis: embed the question, and if a previous question has cosine similarity > 0.95 **for the same role**, return the cached answer. Key: `role + question_embedding`. TTL 24 hours. On re-index, flush the whole cache (`FLUSHDB`) so no answer from a superseded document is ever served.

### 7. PII guard

Two layers:
- Regex for phone numbers (10-digit Indian mobiles), Aadhaar (12 digits in 4-4-4), email addresses, MRN patterns.
- An LLM classification call ("Does this question contain patient-identifying information? yes/no") to catch names and DOBs that regex misses.

If flagged, either reject with a message asking the user to remove patient details, or mask the PII with placeholders (`[NAME]`, `[PHONE]`) and continue answering the policy part of the question.

### 8. Refusal

Rely on the system prompt instruction plus a similarity threshold: if the best retrieved chunk's similarity < 0.7, return "I don't know based on the current SOPs; please contact your Clinic Manager."

### 9. Evaluation

Use RAGAS (faithfulness, answer relevancy, context precision) with Gemini as judge over `eval_questions.csv` and the after-update set. Report averages.

### 10. Testing

- Unit tests for header parsing, PII regexes, access filter.
- Mock the LLM with `FakeListLLM` from LangChain for offline tests.
- `pytest` via `make test`.

### 11. Deployment and ops

- `docker compose up` with three services: api, chroma, redis.
- Config via `.env` (`GOOGLE_API_KEY`, `CHROMA_HOST`, `REDIS_URL`).
- Logging with Python `logging`; add LangSmith for tracing LLM calls.

### 12. Risks

- Table extraction from PDFs can scramble columns — consider `pdfplumber` for tables.
- LLM may hallucinate citations — validate them against retrieved chunks.
- Free-tier rate limits on Gemini — add retries with exponential backoff.
- Conflicting SOPs — handled by the prompt (prefer the newer one).
