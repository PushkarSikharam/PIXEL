# Phase 1: Audit and Stabilization Baseline

Audit date: 2026-09-15. Scope: the checked-out Pixel application, backend, persistence,
conversation routing, voice plumbing, and automated tests. This document describes
observed implementation, not the future architecture.

Historical note: this audit predates private visitor demo instances. Current deployment behavior
and configuration are documented in `LIVE_DEPLOYMENT.md`; the findings below remain the Phase 1
record and must not be used as current operating instructions.

## Decision

Pixel has a working single-product demo foundation. It is not ready to accept private
customer products. UI workspace filtering is not authentication or customer isolation.
The audit and regression baseline can be used to start migration planning; the original
Phase 1 stabilization gate remains open while the critical blockers below exist.

Do not equate expected failures in the audit suite with satisfied requirements.
They reproduce release blockers and must become ordinary passing tests when fixed.

## Current Product Flow

1. The browser loads the shared demo dataset and filters it for the selected workspace.
2. Some chat requests are interpreted and answered directly in the browser.
3. Other requests go through the Next.js proxy to FastAPI.
4. FastAPI combines deterministic intent rules, optional Gemini reasoning, document
   retrieval, and an action allowlist with workspace checks.
5. The browser applies returned actions and separately submits record writes.
6. SQLite stores demo records and backend conversation state. Browser-only exchanges
   do not all enter that conversation history.
7. Voice uses a separate provider path: optional realtime sessions, browser recognition,
   and server speech synthesis with Azure, Gemini, OpenAI, and browser fallback paths.

## What We Can Reuse

| Capability | Observed evidence | Limitation |
| --- | --- | --- |
| Navigation and action registry | Typed actions and executor tests | Contracts remain product-specific and duplicated |
| Text demo loop | API and browser regression tests | Some browser responses bypass the backend |
| Gemini integration | Actual HTTP transport and structured-result parser exist | Live model success and accuracy are not established by stubbed tests |
| Action validation | Rejects unsupported actions and several out-of-scope targets | Direct record APIs bypass this validator |
| Turn management | Monotonic IDs, cancellation endpoint, stale-result checks | Identity binding and atomic cancellation still need work |
| Persistence | SQLite plus record creation/reload tests | Optimistic UI ignores failed writes |
| Product knowledge | Product-scoped Markdown lookup | Keyword scoring over short snippets; no ingestion or versioned publishing |
| Creation forms | Issues, projects, cycles, members | Server validation, empty states, dates, and ownership need strengthening |
| Voice plumbing | Mock recognition and speech interaction coverage | No physical microphone, echo, or Azure quality certification |
| Integration demo | GitHub/Slack navigation and highlights | GitHub connection and sync status are simulated |

## Prioritized Findings

### GAP-01: No authenticated customer or project boundary - Critical

Evidence: `apps/api/app/main.py` exposes full-data reads, reset, and record mutations
without an authenticated principal. `ProductDataStore.load` queries all records.
The caller supplies workspace and session IDs. CORS is permissive; it is not an
authorization mechanism. Dedicated customer deployment provisioning is absent.

Consequence: a user who can reach the API can read or change the shared demo dataset,
regardless of which workspace is visible in the browser.

Required: dedicated service identity, storage and network boundaries; authentication;
backend-derived permissions; owner-bound sessions; authenticated provider endpoints.
Owner: backend/platform. Phase: 2. Gate: unauthenticated requests fail and cross-project
and cross-customer access tests pass, including direct requests and background jobs.

*Superseded in Milestone 3 (design revision 4):* the default is one shared multi-tenant
deployment with **logical** isolation per organization, team and product, enforced on every
request. It does not provide dedicated compute, storage or network boundaries per customer,
and no document may describe it as physically separate. Dedicated deployments remain a later
enterprise option.

### GAP-02: Save failures can look successful - High

Evidence: `apps/web/app/page.tsx` updates local state and uses
`catch(() => undefined)` for record persistence. Creation closes forms and announces
saved events before persistence succeeds. Issue creation can submit the same write
both in `createIssue` and `runAction`; state updater callbacks contain side effects.

Consequence: a backend outage produces apparently saved work that disappears on reload.

Required: a single mutation path; await server success; keep failed drafts; show retryable
errors; make pending state explicit; return server-confirmed records to both chat and UI.
Owner: frontend/backend. Phase: stabilization before expanding features, then 6.
Gate: injected save failures retain drafts, never announce success, and retry once safely.

### GAP-03: Weak record integrity and identifier allocation - High

Evidence: record endpoints accept untyped dictionaries. Missing fields can raise 500s.
Update performs an upsert, including for nonexistent IDs. IDs are generated in browser
state or an in-process planner counter; duplicate IDs overwrite existing data.
Members use names as primary keys. Entity relationships are not enforced by foreign keys.

Reproduced in the browser: creating a Product Engineering project derives `PRJ-103`
from the visible project list and overwrites Platform's existing `PRJ-103` in SQLite.
The conservation regression is an expected failure until creation preserves all existing
records. This blocks multi-user use even before real customer isolation is introduced.

Required: typed request schemas, backend ID allocation, separate create/update semantics,
relationship validation, uniqueness handling, transactions, and idempotency keys.
Owner: backend. Phase: stabilization and 2/6. Gate: invalid payloads are rejected,
nonexistent updates return 404, and concurrent/retried creation preserves both records.

### GAP-04: Static and persisted scopes diverge - High

Evidence: project creation extends SQLite workspace scopes, but the agent and validator
read `WORKSPACE_SCOPES_BY_ID` from a static module. A new project's browser visibility
does not establish that the backend agent can use it.

Required: one persisted, permission-aware scope resolver shared by reads, writes,
retrieval, reasoning, and validation. Owner: backend. Phase: 2/3.
Gate: a newly created authorized project is usable through chat after reload; unauthorized
projects remain inaccessible.

### GAP-05: Session and cancellation boundaries are incomplete - High

Evidence: `SessionManager.ensure_session` accepts an existing ID without verifying
product ownership; sessions have no authenticated owner/workspace/version binding.
`cancel_turn` reads active state and later updates by session ID alone, allowing a newer
turn to be cleared between those operations. Some early reply branches skip the final
active-turn check. Backend response completion is separate from browser persistence.

Required: identity-bound sessions, atomic compare-and-update cancellation, consistent
stale checks, and transactional mutation authorization. Owner: backend. Phase: 2/5/6.
Gate: simultaneous cancel/new-turn tests preserve the newer turn and prevent stale writes.

### GAP-06: Product adaptation and grounding are incomplete - High

Evidence: product/action names, feature hints, synonyms, entities, and UI pages are
hardcoded across frontend and backend. Gemini receives the current message, selected
issue, visible records, and up to two snippets, rather than a full structured conversation
state. Retrieval caches documents by path and has no approved knowledge version.
Action validation does not independently establish that generated speech is grounded.

Required: versioned profiles and action contracts, ingestion/review, approved knowledge,
structured memory, clarification tests, and evidence-aware answers.
Owner: backend/ML and frontend adapter. Phases: 3-6.
Gate: two distinct products run without editing core agent behavior, and unanswered
product questions produce an honest clarification or knowledge-gap response.

### GAP-07: Voice has multiple execution and lifecycle paths - High

Evidence: `HybridVoiceEngine.start` can select realtime independently of the FastAPI
agent. Its realtime instructions do not carry workspace policy. Local recognition is
paused during synthesized speech, so the mocked button-interruption tests do not prove
hands-free interruption. `speakLocalResponse` keeps its fetch abort controller local;
`cancelSpeech` invalidates the separate `speakOnly` path, leaving a stale-audio risk.
Speech endpoints lack authentication and explicit server request deadlines; synthesized
audio responses use public cache headers.

Required: one policy-controlled conversation pipeline, consistent turn-bound audio
cancellation, protected speech endpoints, private caching policy, and physical-device QA.
Keep provider/model choice unchanged in this roadmap. Owner: frontend/backend.
Phases: 2/6/8. Gate: delayed audio never plays after cancellation; actual speech can
interrupt playback without the agent recognizing its own output.

### GAP-08: Demo status and operational readiness need clearer evidence - Medium

Evidence: GitHub health/repository/sync values are static. Creation defaults include
sample owners, titles and fixed dates. Cycle days-left replaces zero with 14, and date
ordering is unchecked. No checked-in deployment automation or CI workflow was found.
The backend health endpoint only reports process health. The `lint:web` script performs
TypeScript checking, not a lint ruleset. Python dependencies use broad ranges.

Desktop (1440x900) and mobile (390x844) screenshots show no horizontal document overflow
in the tested dashboard and issue form. Mobile places the entire assistant above the
dashboard; the dashboard is about 4,000 pixels tall, with repeated nested summary cards.
This passes containment checks but still imposes excessive scrolling. The initial voice
badge is not proof that Microsoft synthesized any audio.

Required: accurate simulated/live labels; complete date/empty-state validation;
deployment and migration automation; provider/error telemetry; reproducible dependencies;
backup and restore exercises. Owner: frontend/platform. Phases: 6-9.

## Stabilization Fix Status (2026-09-15, after audit)

This section tracks fixes made against the findings above. The findings keep their
original audit wording as the record of what was observed.

| Gap | Now enforced | Still open |
| --- | --- | --- |
| GAP-01 | Bearer tokens on all data, turn, cancel and speech routes (`/api/tts` and `/api/realtime-session` verify through `/api/auth/me`). Non-admin users only read their workspaces; issue create/update, cycle create, project/member create and chat turns check the workspace server-side. Reset is admin-only. Passwordless demo login is refused unless the deployment sets `PIXEL_SYNTHETIC_DEMO=true`. | Demo identities replace real sign-in (OIDC). The browser demo still signs in as `demo-admin` by default (`NEXT_PUBLIC_PIXEL_DEMO_USER`) because the demo switches between both workspaces. No dedicated per-customer deployment. |
| GAP-02 | Forms await server success, keep drafts, show errors and retry with the same idempotency key. Failed initial load and failed reset show a retryable banner instead of silently showing sample data. Expired or restarted-server tokens re-authenticate once and retry. | Chat-driven actions surface failures through the chat turn only. |
| GAP-03 | Typed request schemas; server-allocated IDs inside a write transaction; duplicate IDs return 409; updates to missing records return 404; issues and cycles must reference existing projects, and a new or changed issue assignee must be an existing team member working in that issue's workspace (422); issue and cycle links to projects are enforced by database foreign keys. Concurrent project creation is covered by a test. | Members are still keyed by name rather than a stable ID, deferred to Phase 2 where it can ride along with the isolation schema work. |
| GAP-04 | The agent and validator resolve workspaces from SQLite through one lookup that reads only the scope and team rows. | Scope data is not versioned. |
| GAP-05 | Sessions are bound to the authenticated user and customer (`conversation_owners`); another user's turn is denied and their cancel returns 404. Cancel is a single conditional update, and every reply path re-checks the active turn before storing. | Browser persistence of an action is still separate from backend turn completion. |
| GAP-07 | Cancelling speech aborts the `speakLocalResponse` fetch and discards late audio; speech responses are `private, no-store`; provider calls time out after 10 seconds. | Realtime voice still bypasses the backend agent and its workspace policy; physical-device QA is not done. |
| GAP-06, GAP-08 | Not started. | As described in the findings. |

### Project link migration

`migrate()` rebuilds `demo_issues` and `demo_cycles` once to add foreign keys to
`demo_projects`, because SQLite cannot add a constraint to an existing table. It runs at
API startup and is skipped when the links already exist. Safeguards: a full backup
(including WAL contents) is written to `apps/api/data/backups/` first; rows whose project
no longer exists keep the row and lose only the link; row counts are compared and
`pragma foreign_key_check` must be clean, or the whole rebuild rolls back.

Rehearsed on a copy of the working demo database before release: 4 projects, 5 issues,
2 cycles, 4 team members, 1,179 sessions, 3,478 messages and 2,357 signals were identical
before and after, with no link violations, and the application loaded the migrated data.
To roll back, stop the API and restore the backup file over `apps/api/data/demo_agent.sqlite3`.

### Verification after these fixes

| Check | Observed result |
| --- | --- |
| API suite | 73 passed, no expected failures |
| Frontend TypeScript check | Passed |
| Action executor unit suite | 18 passed |
| Full Chromium browser run | 46 passed, no expected-failure markers |
| Migration rehearsal on a copy of the working database | Row counts identical, no link violations, data loads |
| Diff whitespace check | Passed |

The four audit-era expected failures are now ordinary passing tests. Voice still needs a
physical-device test: microphone permissions, acoustic echo, and hands-free interruption
are simulated in these runs.

## Milestone 1: Defects Found After Phase 1 (2026-09-16)

A later review reproduced defects that the passing Phase 1 suite did not catch. Each now
has a regression test that failed against the previous code and passes after the fix.

| Defect | Fix | Regression test |
| --- | --- | --- |
| Agent context crossed workspaces: a same-named project ("Planning") pulled another workspace's issues into the model context | An issue's `projectId` alone decides its workspace; names are a fallback only for issues without one. The reasoner uses the same `issue_in_scope` check | `test_m1_same_named_project_does_not_expand_agent_context` |
| Member creation accepted project IDs outside the requested workspace | The whole write is rejected with 403, for admins too | `test_m1_member_write_rejects_project_outside_workspace` |
| Updates accepted a missing or out-of-workspace assignee | New or changed assignees must exist and share the issue's workspace; unchanged historical assignees stay editable | `test_m1_issue_writes_reject_missing_or_out_of_scope_assignee`, `test_m1_unchanged_historical_assignee_can_still_be_edited` |
| Reset cleared the session before the backend confirmed | Nothing is cleared until the reset succeeds; a failure keeps the session and shows an error | E2E `milestone 1 failed reset keeps the session and reports the failure` |
| E2E requests could escape to the development backend (the `.env.local` rewrite target) when interception was removed with requests still active | The e2e server rewrites to a sentinel port that records any escaped request; every test fails if one arrives; pages close before interception is removed | E2E `milestone 1 requests that escape interception never reach a real backend`, plus the per-test sentinel check |
| Anyone could request an admin demo token | Demo login requires `PIXEL_SYNTHETIC_DEMO=true`, for isolated synthetic demos only | `test_m1_demo_login_requires_explicit_synthetic_demo_flag` |

Initial verification: API suite 77 passed; frontend TypeScript check passed; action executor unit
suite 18 passed; full Chromium browser run 48 passed with no escaped requests.

### Milestone 1 Review Follow-up (2026-09-16)

The subsequent review found two uncovered data cases and intermittent escaped requests
despite the initial passing run. These were addressed without removing the sentinel checks:

- Browser issue filtering now treats a present project ID as authoritative, matching the
  backend. A real-browser regression creates a same-named project and verifies issues
  and team counts remain scoped, while switching workspaces still reveals authorized data.
- Moving a ticket to a different workspace revalidates its assignee even when the name
  is unchanged. Tests cover rejection without mutation, a valid destination assignee,
  and historical assignees remaining editable within the same workspace.
- Backend and speech interception now lives on the browser context until it closes.
  Individual turn mocks no longer remove the baseline backend route. The deliberate
  sentinel probe uses an API request without opening the app or removing live routes.
  Startup waits for persisted data, and test resets must succeed before tests proceed.
- A regression removes a page-level override and verifies the context-level backend
  route remains active. Unexpected sentinel requests still fail tests.

Follow-up verification: API suite 80 passed; frontend unit suite 18 passed; TypeScript
check passed. The 50-test Chromium suite passed once, then passed twice more with
`--repeat-each=2 --workers=1`: 150 successful browser test executions in total, no
unexpected escaped requests, and clean runner shutdown. The new scope and invalid-move
regressions were observed failing before the fixes. Tests use temporary databases,
disabled LLM calls, and mocked voice; this is not live-provider or physical-device QA.

Local development now needs `PIXEL_SYNTHETIC_DEMO=true` in the API environment (or a
root `.env`) for the browser demo to sign in.

## Stabilization Changes Made in This Audit

- Added `PIXEL_DB_PATH` so test servers can use temporary SQLite storage. Default app
  storage stays at its existing location. Unit tests restore their DB override afterward.
- Gave browser tests a fresh API port, temporary database, dedicated frontend port 3100,
  and separate Next.js build directory. Tests do not reuse the running app server.
- Disabled live Gemini calls in deterministic tests and intercepted external speech
  requests. These tests cannot certify paid-provider availability or voice quality.
- Corrected missing speech mock methods and added browser runtime-error assertions.
- Hardened Gemini parsing for empty/malformed envelopes and non-object JSON. Thought
  parts are excluded from speech parsing; split answer text is assembled safely.
- Added provider-parser regressions, explicit known-gap tests, direct creation/reload
  browser coverage, and desktop/mobile layout evidence.

## Verification

| Check | Observed result |
| --- | --- |
| API suite | 52 cases: 48 passed, 4 expected failures documenting GAP-01/03 |
| Frontend TypeScript check | Passed |
| Action executor unit suite | 18 passed |
| Full Chromium browser run | 46 cases: 45 passed, 1 expected failure for unsuccessful saves |
| Subsequent focused Phase 1 run | 6 cases: 4 passed, 2 expected failures; the strengthened project-preservation assertion also reproduces GAP-03 |
| Optimized production build | Passed using the isolated build directory |
| Desktop/mobile evidence | Dashboard and issue-form screenshots inspected at 1440px and 390px widths |
| Diff whitespace check | Passed |

Playwright's terminal summary counts expected failures as successful test outcomes.
The table separates them: the expected failures are real product defects, not passed
requirements. The initial browser run failed on an incomplete speech mock; after fixing
the mock, the full run passed its normal-path assertions. Duplicate project keys still
produce React console warnings, consistent with the reproduced project overwrite.

The final focused run strengthens project creation with a check that existing projects
remain unchanged; it does not claim that the prior happy-path creation test proved
data integrity. No live customer database was reset by these audit test runs.

Reproduce locally:

```text
npm.cmd run test:api
npm.cmd run lint:web
npm.cmd run test:web
npm.cmd run test:e2e
```

The isolated build was run in PowerShell with `PIXEL_TEST_BUILD=1` in the process
environment before `npm.cmd run build:web`. E2E config sets this automatically for its
own server. It also creates temporary SQLite storage through `PIXEL_DB_PATH`; the API
reads that variable from the process environment, not from a dotenv file.

Browser tests intentionally proxy API requests into their isolated backend. They do not
prove the deployed Next.js rewrite or production credentials are correctly configured.
Voice tests simulate recognition and browser synthesis; actual Azure audio, acoustic
echo, hands-free interruption, and microphone permissions require a separate device test.

## Migration Ownership and Order

| Next work | Existing owner/modules | Migration approach |
| --- | --- | --- |
| Customer deployment/auth | API routes, DB connection, provider routes | Establish dedicated resources and server principal first (superseded: shared deployment with logical isolation, see GAP-01) |
| Authoritative records/scope | Product data store, workspace config, schemas | Migrate relationships/IDs and resolve permissions from storage |
| Reliable mutations | Frontend creation handlers and API writes | Replace optimistic fire-and-forget success with acknowledged commands |
| Product profiles | Product config, schemas, planner, executor | Version one shared contract; preserve current profile as first adapter |
| Ingestion | Retriever, product documents | Add draft ingestion and approval before searchable publication |
| Conversation | Agent, reasoner, local frontend handlers | Centralize structured memory and policy in the backend |
| UI adapters | Main page and demo data | Extract by actual workflow boundaries; keep regression coverage |
| Onboarding/release | New setup/deployment modules | Add after isolation and second-profile proof |

Before a schema migration, back up the existing demo database, rehearse migration on a
copy, check row counts and relationships, and verify rollback using that backup. Do not
reset the running app as part of tests or silently reassign existing records.

## Exit Gates

- Audit baseline: evidence and repeatable tests recorded.
- Stabilization: critical workflows pass, including failed saves and concurrent turns.
- Customer-data gate: authentication and isolation verified; no open critical/high
  authorization or data-integrity blockers.
- Proposed pilot targets: all required safety tests pass; all declared demo workflows
  pass; at least 90% success on a customer-approved paraphrase/clarification evaluation
  set with at least 50 cases per product. Report latency/cost separately and set budgets
  using pilot measurements. These are proposed targets, not measured achievements.
