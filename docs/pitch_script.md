# Pitch script: 4 minutes, for the Head of Operations (non-technical)

Record with Loom or a screen recorder. **No code on screen for the first 2 minutes**: use 4 simple slides (or this document's headings), then switch to the live demo (Swagger UI at `localhost:8000/docs`, or the `curl` calls in the README).

---

### 0:00 to 0:30 | The problem (slide: "Which version is right?")
"Today, when a nurse needs the needle-stick procedure, she opens a shared drive with old and new copies side by side. Finding the answer takes minutes, and sometimes she finds the wrong version. In a clinic, an outdated instruction is a safety risk, not just an inconvenience."

### 0:30 to 1:15 | What we built (slide: one question, one answer with its source)
"We built an assistant your staff can ask in plain English. It answers only from the current SOPs, and every answer shows its source: which SOP, which version, which section, which page. Staff can check it in seconds.
If the SOPs don't cover a question, for example a medicine dose or the parking policy, it says 'I don't know, please check with your manager.' It doesn't guess."

### 1:15 to 2:00 | Why you can trust it (slide: three shields: Current, Permitted, Private)
"Three safeguards are built into the software. They are rules, not AI instructions:
- **Current.** When your document team drops in a new copy of the library, the system works out what changed and switches everyone over in one step. In our test it picked up the new refund limit of Rs 1,500, dropped the withdrawn tele-consultation SOP, and never served the old version again.
- **Permitted.** HR-only and clinical-only SOPs are filtered out before the AI sees anything. A front-desk executive can't get the leave policy, however the question is worded.
- **Private.** If someone pastes a patient's name, phone number, Aadhaar or date of birth, the question is stopped before it goes anywhere, and the person is asked to remove it."

### 2:00 to 3:15 | Live demo (now OK to show the screen)
1. Front desk asks: "Up to what amount can front-desk staff approve a refund?" Answer: Rs 1,500, citing SOP-08 version 2.1, section 4.
2. A nurse asks the needle-stick question. Show the answer and the citations. Point out that it flags the old conflict between SOP-04 and SOP-11 *before* the update and not after.
3. The same nurse asks about the salary advance: "I don't know". The HR-only SOP never reached the AI.
4. Paste "Patient Ramesh Kumar, MRN 448812...": blocked with a clear message.
5. (Optional, 10 s) Show the update report: "3 updated, 2 new, 1 withdrawn, 1 renamed, nothing else touched."

### 3:15 to 3:50 | Cost and results (slide: numbers)
"For 2,000 questions a day the running cost is roughly $75 to $110 a month, about Rs 6,500 to 9,500, including hosting. Repeat questions are answered instantly from a cache and cost almost nothing.
On your test set: the right SOP was found every time, 18 out of 18. Zero restricted or outdated documents reached the AI, and all 5 patient-data tests were caught." *(Add the Gemini answer-accuracy figure from `docs/eval_results_gemini.md` here once you have run it.)*

### 3:50 to 4:15 | Next steps (slide: pilot plan)
"We propose a 4 to 6 week pilot in two clinics: staff sign in with their work accounts, give a thumbs up or down on each answer, and your Clinical Governance team agrees the accuracy bar before rollout to all 25 clinics. We'd also like your document owners to review the SOP pairs that contradict each other. The assistant will flag them for you."

---

**Tone tips:** say "SOP", not "chunk" or "embedding". One number per sentence. If they ask "what if the AI makes something up?", the answer is that every answer must point to a real SOP section, or it is replaced with "I don't know".
