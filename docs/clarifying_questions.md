# Clarifying questions for the client (max 3)

Send these by replying to the assignment email. Each one changes a design decision. The assumption used until an answer arrives is noted.

1. **Conflicting SOPs.** When two current SOPs give different rules for the same situation (e.g. needle-stick reporting: 2 hours in SOP-04 vs 24 hours in SOP-11 v1.2), should the assistant recommend the stricter or more specific rule, or only show both and tell staff to check with Clinical Governance?
   *Assumption: show both, flag the conflict, and recommend the stricter / more specific rule, as in eval Q14/Q15.*

2. **Restricted SOPs.** When a staff member asks something covered only by an SOP they may not read (e.g. a nurse asking about the salary advance), should the assistant say "this is covered by a restricted SOP, please ask HR", or should it not reveal that such an SOP exists?
   *Assumption: don't reveal. The refusal text is identical to "not covered", so restricted titles and topics don't leak.*

3. **Patient data and where data may go.** If a question contains patient details, may we automatically strip them and still answer, or must the question be rejected? And may SOP text and staff questions be processed by a cloud AI provider outside India, or must processing stay in an Indian region?
   *Assumption: reject (nothing stored or sent), and a paid-tier Gemini API with no training on prompts is acceptable for the PoC.*
