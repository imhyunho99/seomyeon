# Seomyeon Web

`Seomyeon` is a login-first personal SaaS for one focused job: produce a concise Korean legal brief draft that follows a lawyer-approved reasoning/writing profile and remains traceable to supplied records and verified authorities.

It is not a legal research service, autonomous legal advice, or a court-filing system.

## Web service scope

- Register redacted prior briefs.
- Extract observable style and argument-structure traits with exact source spans.
- Require approval through a local human-only boundary before traits affect drafting.
- Register and confirm matter assertions.
- Search and import official precedents and current statute articles from the
  국가법령정보 공동활용 API, then require lawyer approval.
- Require a lawyer-selected case perspective.
- Record court/case/party/relief metadata.
- Build a claim/defense matrix with elements, burden of proof, linked facts,
  linked authorities, and element-level conclusions.
- Record an admit/deny/lack-of-knowledge/partial response for each supplied
  opposing allegation.
- Block drafting until the deterministic argument plan has no unresolved
  source or structure gaps and is approved.
- Persist an immutable, integrity-checked drafting packet for each generated draft.
- Detect missing/unknown/unapproved citations, obvious citation-topic
  mismatches, and common Korean legal-writing fluff.
- Store a draft snapshot and its findings.

The current web service creates a deterministic, source-linked scaffold so the complete approval and review flow can be tested without sending client material to an external model. A real model gateway and Korean legal-research connectors are deliberately not represented as complete.

The web app has no marketing landing page. Each login owns an independent personal dataset and moves directly through prior-brief upload, profile approval, matter review, perspective approval, and grounded draft review. The MCP adapter remains in the repository for compatibility but is not part of the web product path.

## Local setup

Python 3.12 is recommended. The official MCP Python SDK v2 is required.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
PYTHONPATH=src python -m unittest discover -s tests -v
```

Run over stdio:

```bash
SEOMYEON_WORKSPACE_ID="pilot-lawyer" \
SEOMYEON_DATA_PATH="$PWD/data/local.json" \
seomyeon-mcp
```

Run over Streamable HTTP:

```bash
SEOMYEON_TRANSPORT=streamable-http \
SEOMYEON_ALLOW_INSECURE_HTTP=1 \
SEOMYEON_WORKSPACE_ID="pilot-lawyer" \
SEOMYEON_DATA_PATH="$PWD/data/local.json" \
seomyeon-mcp
```

The HTTP mode deliberately refuses to start unless the insecure-local flag is set. It is for loopback evaluation only until OAuth, origin checks, encrypted storage, malware-resistant document parsing, retention/deletion controls, and an external security review are added. One server process is pinned to one `SEOMYEON_WORKSPACE_ID`; caller-supplied IDs are never authorization.

Run the web service for local evaluation:

```bash
SEOMYEON_DB_PATH="$PWD/data/seomyeon-web.sqlite3" \
SEOMYEON_SEED_EMAIL="test@test.com" \
SEOMYEON_SEED_PASSWORD="1234" \
SEOMYEON_LAW_API_OC="test" \
seomyeon-web
```

Open `http://127.0.0.1:8787`. The seed variables are optional and exist only for a local tester account; production mode refuses them. Normal users create a personal account in the browser, and every profile, brief, matter, packet, and draft is stored under the authenticated account ID. There is no shared-workspace concept. New passwords require at least eight characters. The service accepts UTF-8 text pasted from redacted `.txt` or `.md` files. PDF, DOCX, and HWP/HWPX parsing is intentionally not claimed yet.

`SEOMYEON_LAW_API_OC` enables official precedent and current-statute search/import through the Ministry of Government Legislation's 국가법령정보 공동활용 API. `test` is the public documentation credential and is accepted only for local evaluation. Apply for a production OC and register the deployment domain/server IP through the [official application page](https://open.law.go.kr/LSO/usrJoin.do). The adapter uses the documented [precedent search](https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=precListGuide), [precedent detail](https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=precInfoGuide), and statute endpoints. It keeps the credential server-side, stores the official source ID/retrieval time/content hash, rate-limits account searches, and requires a separate lawyer approval after import. The available precedent corpus is not an exhaustive court-record search.

Web security controls include scrypt password hashing with per-user salts, random opaque sessions stored as hashes in SQLite, server-side logout revocation, HttpOnly same-site cookies, CSRF validation, per-user ownership checks, request-size and account-storage quotas, login throttling, origin/host validation, restrictive browser security headers, and private SQLite file permissions. Database and WAL artifacts are excluded from Git.

For an HTTPS deployment, explicitly enable fail-closed production mode behind a trusted TLS-terminating reverse proxy:

```bash
SEOMYEON_ENV=production \
SEOMYEON_COOKIE_SECURE=1 \
SEOMYEON_ALLOWED_ORIGINS="https://legal.example.com" \
SEOMYEON_ALLOWED_HOSTS="legal.example.com" \
SEOMYEON_DB_PATH="/var/lib/seomyeon/seomyeon-web.sqlite3" \
seomyeon-web
```

Production mode refuses insecure cookies, non-HTTPS origins, and tester-account seeding. The reverse proxy must also enforce HTTPS and a matching request-body limit. Legal materials remain plaintext within the SQLite database, so a real deployment must use encrypted disks and backups or migrate to encrypted managed storage.

## Current drafting-quality boundary

The web path does not yet call an LLM. It extracts observable document habits (including dominant transitions, heading order, conclusion placement, sentence length, formal endings, and evidence placement) and applies them to a deterministic, source-linked draft. A 100-document synthetic account E2E is included in the test suite.

The service now builds a lawyer-entered claim/defense matrix, burden-of-proof paragraphs, fact-to-element applications, bounded rule paragraphs, opposing-position responses, and a court/case/party/relief cover. This is controlled document assembly, not autonomous legal reasoning. It does **not** infer missing causes of action, decide the legally correct elements or burden, prove semantic entailment, select the best authority automatically, or guarantee filing completeness. The citation-topic check is lexical and can produce both false positives and false negatives. A green check therefore means that the approved structure and known source IDs passed the implemented gates; it is not a truth or filing-readiness decision.

A production model gateway must keep source text in an untrusted-data boundary and use versioned stages for profile extraction, argument planning, structured drafting, and independent semantic verification. Every material atomic claim must map to an official authority or an exact record span. Provider choice, retention/training terms, model revision, prompts, token budgets, and evaluation results must be recorded before real client material is enabled.

The MCP-facing model can create candidates but cannot approve them. After the lawyer inspects the evidence in a separate local review surface, the pilot operator records that decision with `seomyeon-admin`:

```bash
SEOMYEON_WORKSPACE_ID="pilot-lawyer" \
SEOMYEON_DATA_PATH="$PWD/data/local.json" \
seomyeon-admin --lawyer-id pilot-lawyer approve-profile TRAIT_ID

SEOMYEON_WORKSPACE_ID="pilot-lawyer" \
SEOMYEON_DATA_PATH="$PWD/data/local.json" \
seomyeon-admin --lawyer-id pilot-lawyer approve-matter MATTER_ID \
  --assertion F1 --authority A1 --perspective
```

## Intended MCP workflow

1. `register_prior_brief`
2. `list_prior_briefs` and `brief://...` resources
3. `build_lawyer_profile`
4. `add_profile_trait_candidate` for higher-order, verbatim-evidenced patterns
5. Lawyer reviews candidates; the separate local approval surface records accepted traits.
6. `create_matter`
7. `add_case_assertion`
8. `add_legal_authority`
9. `select_case_perspective`
10. Lawyer reviews the record, authorities, and perspective; the separate local approval surface records the decision.
11. `prepare_drafting_context`
12. Host model drafts with required source IDs.
13. `save_and_verify_draft` with the returned `packet_id`

Resources:

- `lawyer://{lawyer_id}/profile`
- `brief://{lawyer_id}/{brief_id}`
- `case://{lawyer_id}/{matter_id}`
- `draft://{lawyer_id}/{draft_id}`
- `packet://{lawyer_id}/{packet_id}`

## Safety invariants

- Every profile trait has evidence.
- Only approved traits enter the drafting packet.
- Only confirmed matter assertions and verified authorities enter the drafting packet.
- A lawyer-approved perspective is mandatory.
- Every substantive paragraph must cite supplied source IDs.
- Handles are not authorization; every matter/draft lookup rechecks ownership.
- Pilot data must be synthetic or redacted.
- A deterministic pass is not a filing-readiness decision: semantic source review and responsible-lawyer sign-off remain mandatory.
- Every draft is bound to the immutable packet actually supplied for drafting.

## Success criterion

For three to five pilot lawyers, the primary target is at least a 50% median reduction in active editing time from generated draft to filing-ready sign-off. Any fabricated fact, quotation, statute, or precedent fails the safety gate regardless of time saved.

See `skills/korean-legal-writing/references/pilot-evaluation.md` for the evaluation contract.

## Deferred production work

- HWP/HWPX/PDF ingestion and sandboxed OCR
- production OC capacity/licensing review and broader Korean legal-source coverage
- PostgreSQL/pgvector and encrypted object storage
- email verification, password reset, MFA, and optional external identity providers
- immutable audit events and deletion workflows
- model gateway with provider retention/training controls
- DOCX/HWP export and inline review UI
- billing and multi-lawyer firm administration
- PostgreSQL migration for horizontal scaling (the web service currently uses transactional SQLite)
- shared/distributed rate limiting and quota enforcement for multiple app instances
- encrypted database/backups and tenant-scoped key management
