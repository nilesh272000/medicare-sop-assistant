# MediCare Clinics: SOP Assistant – Solution Brief

**For:** Head of Operations, MediCare Clinics  
**From:** D3V Technology Solutions  
**Status:** Working Proof of Concept (PoC)

## The Problem

Staff across 25 clinics search a shared drive containing SOP PDFs, where outdated copies sometimes exist alongside newer versions. Finding the correct procedure is time-consuming and can lead to outdated answers.

Some SOPs are restricted to specific roles (clinical-only, HR-only), and staff may accidentally include patient information in their questions. The solution must protect restricted information and prevent patient data from being sent to an external AI service.

## What We Built

An internal SOP assistant that answers staff questions in plain language using only the current, authorised SOP documents.

Every answer includes its source: **SOP ID, version, section and page number**.

If the authorised SOP documents do not contain enough information to answer a question, the assistant responds with "I don't know" rather than guessing and directs the user to their manager.

A local, deterministic PII detection layer checks questions for patient identifiers, including names, phone numbers, Aadhaar numbers, MRNs and dates of birth. Detected patient information is rejected before the request reaches the AI provider.

## How It Works

1. **Library updates:** The document team provides a complete snapshot of the SOP library. The system identifies new, updated, withdrawn and renamed documents, reprocesses only the necessary content, and publishes the new library in one atomic SQLite transaction. Staff never see a partially updated library or withdrawn SOPs. Each indexing run reports its changes, such as *"SOP-08 v2.0 to v2.1: refund authority raised to Rs 1,500; SOP-15 withdrawn"*.

2. **Who may read what:** Each SOP's Access field determines which staff roles can use it. Access filtering happens before retrieval scoring and before any content is sent to the AI model. This prevents users from retrieving documents outside their authorised roles.

3. **Answering:** The system retrieves relevant SOP sections and provides them to the AI model with instructions to answer only from the supplied information. Returned citations are checked against the retrieved sources. Answers without valid supporting sources are rejected. When conflicting SOP instructions are detected, the assistant identifies the conflict rather than silently choosing one.

4. **Repeat questions:** Frequently repeated questions can be answered using a cache. Cache entries are tied to the question, access permissions and current library generation. The cache is invalidated when the SOP library changes, preventing answers based on outdated documents.

5. **Patient-data protection:** Patient-data checks run locally before the external AI request. Questions containing detected patient identifiers are rejected rather than sent to the Gemini API.

## Results on Your Test Questions

The application was evaluated using the actual Google Gemini API, not just a simulated AI provider.

- **15/15 original evaluation questions passed.**
- **10/10 answerable questions passed.**
- **3/3 unanswerable questions passed.**
- **2/2 conflict questions passed.**
- **8/8 updated evaluation questions passed.**
- **5/5 patient-data protection tests passed.**
- **18/18 retrieval checks passed.**
- **Zero governance violations:** No unauthorised or superseded SOP content reached the AI context during evaluation.

After the November update, the assistant correctly handled the updated walk-in reserve (25%) and refund limit (Rs 1,500), stopped citing the withdrawn tele-consultation SOP, and did not expose the HR-only salary-advance SOP to nurses.

**Actual Gemini evaluation metrics:**

| Metric | Result |
|---|---:|
| LLM calls | 23 |
| Total tokens | 42,512 |
| Total API cost | $0.018067 |
| Mean evaluation latency | 5.73 seconds |
| Total evaluation time | 387.2 seconds |

The separate offline test suite also passed all **110 tests**, covering access control, PII protection, caching, API validation, PDF parsing, incremental indexing, crash safety and concurrent publishing.

These results demonstrate that the PoC met its defined automated evaluation criteria. They do not constitute independent clinical validation or production-scale performance testing.

## Trade-offs We Chose

| Area | Choice | Why |
|---|---|---|
| Accuracy vs coverage | Refuse rather than guess | A wrong clinical instruction can have serious consequences. Asking staff to check with their manager is safer. |
| Security | Access and patient-data checks are fixed software rules, not AI instructions | Deterministic rules can be tested and are harder to bypass through prompt manipulation. |
| Patient data | Block detected patient information instead of automatically redacting it | Prevents detected patient identifiers from reaching the external AI provider. |
| Latency | Actual mean evaluation latency: 5.73 seconds | Measured using the live Gemini evaluation. Further optimisation and production load testing are needed. |
| Data governance | Local PII rejection and role-based retrieval | Only authorised SOP context is provided to the model. Provider data-processing, retention and regional-processing terms must be reviewed before real clinic deployment. |

## Rough Monthly Running Cost (At 2,000 Questions/Day)

The following is a planning estimate, not an actual production bill.

**Assumptions:**
- 2,000 staff questions per day.
- Approximately 1,200 new AI requests per day after cache hits and rejected requests.
- 30 operating days per month.
- Approximately 2,000 input tokens and 250 output tokens per new AI request.
- Gemini 3.5 Flash Lite paid API pricing: $0.30 per million input tokens and $2.50 per million output tokens.

| Item | Assumption | USD / Month |
|---|---|---:|
| AI answers (Gemini 3.5 Flash Lite) | 72 million input tokens and 9 million output tokens | ~$44.10 |
| Search embeddings | Questions and monthly SOP updates | Less than $1 (estimated) |
| Hosting | One small always-on server or Cloud Run instance, disk and backups | $30–$60 (estimated) |
| **Total** | **Illustrative monthly estimate** | **~$75–$105** |

At the assumed usage, the AI calculation is:

- Input: 1,200 × 30 × 2,000 = 72 million tokens × $0.30 = $21.60.
- Output: 1,200 × 30 × 250 = 9 million tokens × $2.50 = $22.50.
- Total AI generation cost: approximately **$44.10/month**.

Without cache hits or refusals, assuming all 2,000 daily questions require AI generation, the estimated AI cost increases to approximately $73.50 per month.

Actual costs will depend on token usage, cache hit rate, API pricing, hosting configuration, backups and operational requirements. The estimate excludes implementation, monitoring and support costs.

## Risks and How We Handle Them

- **Wrong or outdated answer:** The system maintains a current-version-only index, validates citations, refuses unsupported answers and includes regression evaluation.
- **Patient data entered into the assistant:** Detected patient identifiers are blocked locally before an external AI request. Residual risk includes identifiers that are difficult to recognise, such as an isolated first name. Further local detection improvements are recommended.
- **SOPs that contradict each other:** Conflicting instructions, such as the 2-hour versus 24-hour needle-stick reporting requirements in the supplied SOP examples, are surfaced rather than silently resolved. Document owners should review flagged conflicts.
- **AI provider outage:** Bounded retries and timeouts prevent indefinite requests. Users receive a clear retry message, while the locally stored SOP library remains available.
- **Badly exported or incomplete library drop:** The indexing process rejects an invalid update as a whole, preserving the previously published library.
- **Clinical accuracy and governance:** Automated evaluation does not replace clinical review. Clinical experts must validate answers and approve the system before real-world clinical use.
- **Production scalability:** Performance under 300 simultaneous staff users has not yet been independently established through load testing.

## Next Steps (4–6 Weeks)

1. **Pilot in two clinics:** Integrate single sign-on (staff log in using their work accounts) and add feedback options to each answer.
2. **Clinical validation:** Have Clinical Governance review the evaluation questions, expected answers, refusal behaviour and acceptance thresholds.
3. **Document conflict reporting:** Develop a document-owner report highlighting conflicting SOP rules during library updates.
4. **Production infrastructure:** Evaluate managed database services, backups, monitoring, security and disaster recovery.
5. **Performance and security testing:** Conduct load testing, penetration testing and operational readiness reviews before wider deployment.
6. **Organisation-wide rollout:** After successful pilot approval, plan a phased rollout across all 25 clinics.

**Important:** This is a working proof of concept. It demonstrates the proposed architecture and tested functionality, but it is not yet approved for clinical production use.