# AI Usage Log

## Tools Used

| Tool | Used For |
|---|---|
| **Claude (Anthropic, Claude Opus 5.5), agentic session with a shell** | Architecture proposal; most of the implementation code, tests and evaluation harness; first drafts of the documentation. It ran the code, tests and evaluation in a sandbox and iterated on failures. |
| Web search (inside the same session) | Researching Gemini model and embedding pricing for the initial cost estimate. |

**Split of work, stated plainly:** AI wrote most of the code and the first drafts of the documents. My contribution was providing direction through requirements and constraints, challenging the proposed design, reviewing the AI's output against the assignment brief and data pack, deciding which design recommendations to overrule (ADR-1, ADR-2 and ADR-4), and checking the implementation and evaluation results. I can explain every module. The design decisions documented in `docs/design_review.md` are the ones I am prepared to defend in the live engineering discussion.

## Key Prompts

The build was one long agentic session. It was started with the assignment email and the supplied ZIP file ("assignment based on a realistic client scenario: an internal assistant that answers clinic staff questions from their SOP documents, using RAG ... Everything you need is in the attached zip ... start with README.md").

Prompt 1 below is verbatim. Prompts 2 to 5 are the steering instructions that shaped the work, reconstructed in short form from the session rather than copied verbatim.

### 1. Architecture Proposal

Saved with its original output in `docs/ai_design_proposal.md`.

> I'm building a production-minded proof of concept for a client. MediCare Clinics runs 25 outpatient clinics. Staff (300 people, ~2,000 questions/day, many repeated) need an internal assistant that answers questions only from the current SOP PDFs and cites SOP id, version, section and page. ... Please propose an architecture: components, data flow, storage, chunking, access control, re-indexing, caching, PII guard, refusal, evaluation, and the main risks.

### 2. Critique the Proposal Against the Real Data Before Coding

> Before writing code, compare the original library and the 2026-11-01 snapshot: file hashes, extracted-text hashes, versions, file names. For each weakness in the proposal, show the concrete file in the pack that breaks it.

This is how W1 was found: SOP-03's bytes differ but its text is identical (DMS re-export, only the PDF `Subject` changed), and SOP-05 was renamed with identical content.

### 3. Build with Explicit Constraints

> Python, no LangChain. LLM and embeddings behind interfaces we own; a deterministic fake for offline tests. Identity = SOP id + normalised-text hash. Whole snapshot diff published in one SQLite transaction with a generation counter. Access filter applied before scoring. Cache key = normalised question + access levels + generation. PII guard local and deterministic, reject not mask. Every business rule unit-tested.

### 4. Evaluation First, Then Fix

> Write the pass criteria per question in a file before running the model. Check governance on every question (no unauthorised or superseded chunk in the LLM context), warm the cache with soon-to-be-stale answers before the re-index, and report per-question failures with the reason.

### 5. Adversarial Tests

> Write tests that try to break each guarantee: prompt injection asking for HR SOPs as front desk; a crash injected between delete and insert during publish; a concurrent publish; a snapshot containing an old and a new copy of the same SOP; same version with different content; a corrupt PDF; a half-copied snapshot folder; a request that started before a re-index writing to the cache after it.

## Where the AI Was Wrong or Weak, and How It Was Caught

### 1. Document Identity by File Name + File MD5 (Design)

The AI's proposal would re-embed SOP-03 on every DMS re-export, treat the renamed SOP-05 as a withdrawal plus a new document, and index two copies of an SOP side by side if the drive held both.

**Caught by:** Hashing the files and extracted text in both snapshots before writing code.

**Fix:** ADR-1.

### 2. PII "LLM Classifier" Sends Patient Data to the LLM (Design)

The proposal's second PII layer asks Gemini whether a question contains patient data, which sends the data to the place it must not go.

**Caught by:** Reading the proposal against the requirement "never processes patient information".

**Fix:** Local rules and rejection of detected patient data (ADR-4).

### 3. Semantic Cache (Design)

The proposal reused answers for questions with similarity greater than 0.95 per role.

**Caught by:** Testing the idea against pairs from the pack that differ only in a number ("within 2 hours" vs "24 hours", "Rs 1,000" vs "Rs 1,500").

**Fix:** Exact cache key with access levels and generation (ADR-2).

### 4. Parser Bug: A Wrapped Line That Starts with a Number Became a Fake Step (Code)

The AI's first chunker treated any line starting with `<number> <text>` as a new numbered step. In SOP-12, "...kept until the patient turns / 21 or for 3 years" was split, so the retention rule was cut in half.

**Caught by:** The offline evaluation: Q10 failed with the answer "kept until the patient turns".

**Fix:** A step only starts if it is the next number in sequence. Regression test: `test_wrapped_line_starting_with_number_is_not_a_new_step`.

### 5. Parser Bug: Headings Containing Dots Were Missed (Code)

The heading regex excluded `.` in titles, so SOP-16 "2. When you discover fire or smoke (R.A.C.E.)" was merged into section 1, and answers would have cited the wrong section.

**Caught by:** Reading the U05 answer in the evaluation output, which contained "2. When you discover fire..." inside section 1's text.

**Fix:** Allow dots, require headings to be sequential and not end with a full stop. Test: `test_heading_with_dots_is_detected`.

### 6. Context Too Narrow for Procedure Questions (Design and Initial Implementation)

The first version sent the top-5 sections. For Q01 (needle-stick procedure), the answer spans SOP-04 sections 2 to 5, and top-5 retrieval returned a mix of SOP-11 and SOP-04 sections.

**Caught by:** The evaluation trace (`retrieved` vs expected sections).

**Fix:** Retrieve by section, but give the model all sections of the top 3 SOPs (about 600 tokens each), with sibling sections re-checked against the role.

### 7. Two Wrong Tests (Tests)

**(a)** The access test asserted "SOP-13" never appears in any prompt, but the question itself contained "SOP-13", so it failed for the wrong reason. Fixed to check excerpt headers (`] SOP-13 v`).

**(b)** The rename test generated new file names from the last 8 characters of each old name, so two files collided and one was silently overwritten (13 renames instead of 15).

Both were caught because the tests failed. They show that AI-written tests need the same review as AI-written code.

## What the AI Could Not Verify Here

- **Independent clinical validation:** Automated evaluation confirms that the application met its defined test criteria, but clinical experts must independently validate medical accuracy and approve the system before clinical use.
- **Production readiness:** The PoC has not undergone a production deployment, formal penetration test, or real-world clinic pilot.
- **Infrastructure scaling:** Performance under 300 simultaneous staff users has not been independently established through production load testing.
- **Provider data governance:** Applicable Gemini API data-processing, retention, and regional-processing terms must be reviewed before handling real clinic information.

**Verification completed:** The Gemini chat and embedding adapters were tested against the live Google Gemini API. The actual Gemini evaluation completed successfully, with 15/15 original questions, 8/8 updated questions, 5/5 PII tests, 18/18 retrieval checks, and zero governance violations. The offline test suite also passed all 110 tests.