---
name: korean-legal-writing
description: Use when a Korean lawyer wants a concise, evidence-grounded draft that follows an approved personal reasoning and writing profile. Requires the Seomyeon MCP tools.
---

# Korean Legal Writing

Produce a reviewable draft, not legal advice or an autonomous filing.

## Required workflow

1. Identify the lawyer and matter handles. Never treat a handle as authorization.
2. If no approved profile exists, register redacted prior briefs and build the baseline profile.
3. Use `list_prior_briefs` and its `brief://` resources to inspect one brief at a time. Add higher-order reasoning, structure, or evidence-use candidates only through `add_profile_trait_candidate`; its excerpt must occur verbatim in the cited brief.
4. Show every candidate trait with its evidence. Approval is recorded only through the separate human review surface; MCP tools cannot approve it. Do not infer personality, protected characteristics, mental state, or psychological traits.
5. Add case assertions with stable IDs. MCP creates them unconfirmed; only the separate human review surface may confirm them.
6. Add only allowlisted statutes and precedents as candidates. MCP creates them unverified; the separate review must check the source, identifier, proposition, and quotation.
7. Offer two or three perspectives only when requested. For each, state supporting assertions, counterevidence, missing facts, and risks. Never select a perspective for the lawyer.
8. Store the lawyer's selected perspective as a candidate, then wait for approval through the separate human review surface.
9. Call `prepare_drafting_context`. Retain its `packet_id` and use only the returned assertions, authorities, approved traits, and perspective.
10. Draft an outline before prose. Every substantive paragraph must perform at least one function: fact, evidence link, legal rule, application, rebuttal, or conclusion.
11. Cite case material as `[근거: ASSERTION_ID]` and law as `[법률: AUTHORITY_ID]`.
12. Call `save_and_verify_draft` with the same `packet_id`. Resolve every high-severity finding before presenting the result. Treat `deterministic_passed` only as a mechanical check; `filing_ready` remains false until human semantic source review and lawyer sign-off.

## Concision rules

- Delete decorative introductions, emotional emphasis, unsupported certainty, and semantic repetition.
- Do not reproduce bad habits merely because they appear in prior briefs.
- Prefer one proposition per paragraph.
- A paragraph that can be deleted without changing the reasoning must be deleted.
- Quote only when the exact text is supplied and verified.
- If evidence is missing, write `[확인 필요: ...]`; never fill the gap by inference.

## Output contract

Return:

1. Draft title and outline.
2. Draft with inline source IDs.
3. Unresolved factual or legal questions.
4. Verification findings, clearly distinguishing deterministic pass/fail from filing readiness.
5. A reminder that the responsible lawyer must review the final filing.

Read `references/pilot-evaluation.md` when the task is part of a pilot evaluation.
