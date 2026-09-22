# Milestone 3.2, Slice 5c: Authoritative Cutover — Implementation Plan

Status: **implemented, not signed off.** The code landed in PR #21 (2026-09-22) and a follow-up
change that closed the review findings; the build, its evidence and the open production gates are
in `docs/MILESTONE_3_STEP_3_2_SLICE_5C.md`. Definition authority is not enabled in production.
Date: 2026-09-21. Parent plan: `docs/MILESTONE_3_STEP_3_2_PLAN.md` revision 4.3. Prerequisites:
slice 5b merged and signed off, the 5a production evidence gate closed, and every gate in section 2
of this plan satisfied.

## 1. Outcome

The definition-driven conversation engine becomes the only engine whose decision is returned to a
visitor. The previous engine remains installed behind one deployment-level rollback switch until
slice 5d. A cutover is successful only when all of the following are true:

1. Every request runs under one explicitly selected authority: `legacy` or `definition`.
2. The definition engine owns routing, clarification, memory, response composition, action
   validation and mutation dispatch in definition mode. Every visitor message reaches `/api/turn`
   before the browser executes a returned UI action; the browser never fabricates an assistant
   response in either authority mode.
3. The definition engine's durable state survives a process restart without storing a second copy
   of customer prose in its memory row.
4. Every mutating action is executed only through the 5b key, receipt and replay boundary.
5. The old engine can be restored by changing one deployment setting and restarting the service;
   no schema migration, backfill, definition move or manual row edit is needed.
6. No request silently falls back from the definition engine to the old engine after an error.

The cutover is **additive and reversible**. Removing the old engine is 5d, after this cutover has
separate acceptance evidence.

## 2. Hard entry gates

Coding may begin before all production evidence exists, but the production switch may not move to
`definition` until every gate below is recorded in the 5c report.

### 2.1 Slice 5b is complete

- Its pull request is merged only after its own Linux CI is green.
- The execution transport, exact-turn cancellation, workspace binding, receipts, retention,
  startup migration gate and shadow scheduler meet the 5b exit criteria.
- A visitor write is proven to affect only that visitor's instance and the execution key's scope.
- Production still returns `execution: null` before 5c.

### 2.2 Shadow evidence is sufficient

The final 5a/5b shadow report contains at least **100 compared eligible turns across at least 20
fresh sessions**, including the required workflow matrix below. Synthetic production sessions may
fill gaps in low-traffic demos; they use the public visitor path and no paid model call.

- greeting and identity;
- navigation to issues, projects, cycles, teams, integrations and architecture;
- one known-record lookup, one unknown record and one inaccessible record;
- one assignee filter and one follow-up reference such as “it” or “her ticket”;
- one deterministic update proposal and one confirmation flow;
- one create flow, including missing required fields;
- one product-boundary refusal and one destructive-action refusal;
- one interruption/supersession sequence and one workspace switch.

Required report result:

- zero `shadow_error` and zero `worker_unhealthy`;
- `shed` below 1% of eligible turns, with every occurrence explained;
- `over_budget` below 5%, with no unexplained latency cluster;
- every `behaviour`, `coverage`, `security` and `lifecycle` difference has a named decision;
- platform wording differences remain separate and do not conceal a field-level behavior change.

The production report is evidence, not a golden-file replacement. Golden expectations are changed
only by reviewed entries committed with the code that intentionally changes behavior.

### 2.3 Known pre-cutover gaps are closed

- Ticket creation no longer fails with `missing_required_field` for ordinary requests. Section 7
  defines the generic completion flow and the Linear definition change.
- Restart-safe authoritative memory exists as section 6 specifies.
- The web app does not depend on legacy `intent_trace` labels. Every `TurnResponse` field has an
  explicit definition-engine mapping and a test.
- The `sendMessage` path no longer calls browser-local scope, draft or conversation intent handlers.
  Those functions may remain as unreachable source until 3.3, but no production or test message
  can obtain a `localTurnResponse`.
- In legacy authority mode, every old-engine create/update decision is adapted to the same 5b
  execution envelope. Rollback does not re-enable assistant-originated keyless writes.
- The definition package can translate every action the capability reply offers. An action that is
  untranslatable is never offered or dispatched.
- The 5b preflight reports no live legacy execution key that the new schema cannot safely claim.

### 2.4 Operational prerequisites

- A fresh encrypted database backup is made and one restore is verified before production cutover.
- The prior application artifact and its environment are identified and deployable.
- One API replica remains enforced. Multi-replica authoritative memory is outside this slice.
- Paid model reasoning and realtime voice are not required for cutover and remain independently
  kill-switchable.

## 3. Authority selection and rollback contract

### 3.1 One startup-selected mode

Add `PIXEL_ENGINE_MODE` with exactly two values:

- unset or `legacy`: the previous engine is authoritative;
- `definition`: the definition engine is authoritative.

Any other value makes readiness fail and prevents turn traffic. The mode is read once during
FastAPI lifespan and stored in an immutable application service graph; it is not re-read per turn.
No request header, cookie, user role, tenant value or query parameter can select an engine.

`/health` reports the selected mode and readiness result without exposing secrets. A deployment
whose selected engine cannot be constructed is unready and serves no turn. The existing
`PIXEL_SHADOW_ENGINE` controls comparison only while authority is `legacy`; it is ignored and
reported as inactive while authority is `definition`.

### 3.2 No per-turn fallback

If the definition engine fails after accepting a turn, the API returns a controlled unavailable or
stale response from the platform boundary. It does **not** invoke the old engine. Per-turn fallback
would use different memory, different safety rules and possibly execute a second decision for the
same request.

Rollback is an operator action: set `PIXEL_ENGINE_MODE=legacy` and restart/redeploy. The same
database and definition pins remain valid. The rollback test uses the same session before and after
the switch and performs no operator data mutation.

### 3.3 Rollback continuity

While `definition` is authoritative, the finalization transaction also maintains the existing
generic compatibility projection: user and assistant messages, signals and `visitor_context`.
The old engine can therefore resume basic context after rollback without being executed in shadow.
The old engine is never allowed to propose, speak, call a provider or write during definition mode.

Pending definition-engine questions and confirmations are **not translated into old-engine pending
state**. After rollback they are treated as expired; the visitor must repeat the request. Any 5b
execution key already issued remains governed by its normal scope, expiry, supersession and replay
rules. Rollback neither broadens it nor creates another write path.

The old engine may decide a rollback-mode turn, but it no longer owns the write lifecycle. A thin
adapter converts its final decision to the shared response/action contract before finalization.
Create/update actions receive a 5b envelope or fail closed; navigation/highlighting receives no
key. The adapter must not duplicate the old planner or validator and is deleted with the old engine
in 5d. It receives only records from the selected workspace, never every workspace in the caller's
grant, and the outer platform boundary preserves unknown-equals-inaccessible behavior. Rollback may
be less capable, but it may not restore the old cross-workspace disclosure.

## 4. Components

| Component | Responsibility |
| --- | --- |
| Authority configuration | Parse and validate `PIXEL_ENGINE_MODE` once; expose readiness metadata |
| Definition turn service | Authorize, pin, snapshot, load memory, run deterministic/model stages, finalize and map `TurnResponse` |
| Legacy authority adapter | Preserve rollback decisions while delegating activation, mutation dispatch and finalization to shared 5b-safe orchestration |
| Durable engine-state store | Persist the safe, versioned subset of `ConversationMemory` with optimistic turn ordering; rebuild bounded signal history from its existing rows |
| Ephemeral pending-state cache | Hold one-turn arbitrary values in memory only, bounded by owner, count and idle expiry, and revision-checked against durable state |
| Legacy-session bootstrap | Initialize safe definition memory for a session that began under the old engine |
| Required-field completion | Resolve definition defaults and ask deterministic questions for remaining required fields |
| Model gateway | Optional, budgeted provider transport around the existing strict prompt/parser/provenance boundary |
| Response mapper | Populate every public response field and attach a 5b execution envelope only for dispatchable mutations |
| Backend-authoritative web transport | Send every visitor message to `/api/turn`; execute only the validated action/envelope returned by that request |
| Cutover telemetry | Count mode, stage, status, dispatch, receipt, fallback, refusal, error and latency without storing messages |
| Operator checks | Readiness, cutover report, rollback rehearsal and environment validation |

The pure `app.engine` package still receives no database, network, provider, speech or execution
capability. Orchestration belongs in services. Product-specific lookup, knowledge and translation
remain behind the installed product package.

## 5. Normative authoritative turn lifecycle

No database transaction remains open across engine work or a provider call.

0. The web client sends the message to `/api/turn`. It performs no local intent, reply or mutation
   decision before the response; only UI state such as current page and selected record is context.
1. Authenticate, rate-limit and authorize the tenant, product, private demo instance and selected
   workspace exactly as 5b requires.
2. Create or verify the owned session and its immutable definition/knowledge pin. Existing sessions
   keep their pin.
3. Activate the turn through 5b's monotonic, owner-checked transaction. It records the accepted
   workspace and supersedes older unused keys. A stale turn stops here.
4. Materialize one bounded `TurnSnapshot` in one short read transaction, then close it.
5. Load the durable engine state for the exact owner, session, pin checksum, instance generation
   and workspace. A scope change clears remembered record/person references and pending state.
6. Run deterministic routing first. Only an unresolved fallback may enter the optional model path
   in section 8. A confirmation turn never calls the model.
7. Validate the action against the same snapshot. Translate only after validation. Missing or
   dishonest translation fails closed and cannot appear in a capability reply.
8. Build the platform-owned response. Model speech remains unusable until a separately reviewed
   citation/claim binding exists; 5c uses deterministic response composition.
9. Finalize in one `begin immediate` transaction:
   - re-check full ownership, grant, scope, product and pinned-definition lifecycle;
   - require `active_turn_id` to equal this turn and the persisted engine-state revision to match
     what step 5 loaded;
   - persist user/assistant messages, signals, safe engine memory and the compatibility projection;
   - for one dispatchable mutation, issue the 5b execution key in this same transaction;
   - clear the active turn and commit.
10. If step 9 loses a race, return `stale`; persist no message, memory, signal or key from that turn.
11. Return the response. Record telemetry and maintenance work after the response as bounded
    background activity.

There is no point where both engines are authoritative, and no point where a failed definition turn
is rerun through the old engine.

Legacy authority follows the same activation, dispatch, finalization and browser transport rules.
Its adapter supplies the decision in step 6, while the shared service owns steps 1–5 and 7–11. The
old turn path is split or wrapped so it cannot independently store a second message set, complete
the turn early or expose a keyless assistant mutation.

## 6. Durable conversation state

### 6.1 Storage contract

Add one versioned state row per session, owner-bound through the session and private-instance
generation. The row contains:

- codec version, definition ID/version/checksum and knowledge version;
- last committed turn, workspace and state revision;
- focus, last person, last view, last change key and person-follow-up action as identifiers;
- pending clarification/confirmation metadata only when every stored value is an identifier,
  definition enum/default, boolean or bounded number;
- update and expiry timestamps.

The row does not copy messages, record titles, descriptions, retrieved passages, prompts, model
output or arbitrary free text. Those already belong to their existing controlled stores and are not
duplicated into engine memory.

Add nullable `scope_id` to existing messages and signals. Every 5c write supplies the accepted
workspace; legacy null rows remain for audit but are never used to construct definition memory.
`SignalHistory` is rebuilt in the state-load read transaction from at most the latest 50 rows for
the exact owner, session, instance generation and current workspace, ordered deterministically by
turn, creation time and row ID. It is not copied into a second JSON field. A workspace switch starts
a workspace-local history and cannot surface a previous workspace's person, record or summary.
The rollback compatibility projection is likewise rebuilt for the selected workspace instead of
merging session-wide values.

### 6.2 Non-restorable pending state

Some create requests contain arbitrary text, such as a new ticket title. Full in-process memory may
hold it for the immediate next turn, but the durable codec does not. Persist only a
`pending_requires_repeat` marker and the originating turn. After process restart, an isolated short
answer or “yes” receives a deterministic request to repeat; a complete new request replaces the
marker normally. Nothing is reconstructed by guessing or by replaying old model output.

The full pending value lives in an owner-keyed in-process cache with at most 1,000 session entries
and a 15-minute idle expiry. Cache entries contain the durable state revision and are usable only
when it still matches the database row; mismatch, eviction, expiry, restart, scope change, reset or
owner mismatch discards the entry. The cache is updated only after the finalization transaction
commits. If the process dies between commit and cache update, the durable marker produces the safe
repeat behavior. The one-replica deployment gate makes this behavior deterministic.

### 6.3 Ordering, corruption and retention

- State commits only where `last_turn < turn_id` and the loaded revision still matches.
- Duplicate and out-of-order finalization cannot overwrite newer memory.
- Cache replacement happens under a bounded lock after commit and only for the committed revision;
  an older completion cannot replace a newer cached state.
- Unknown codec versions, checksum mismatches, malformed JSON and impossible references fail
  closed: discard pending state, retain no unsafe value and ask for a fresh request. They do not
  fall back to the old engine.
- State expires with the session and is removed by the existing bounded maintenance mechanism.
- Private reset deletes or invalidates state in the same generation-changing transaction.
- A test proves one user's state cannot be read through another tenant, product, user, session,
  instance, generation or workspace.

### 6.4 Legacy-session bootstrap

On the first definition turn for a session with no engine-state row:

- verify its existing owner and pin;
- initialize only from safe persisted identifiers: selected record, current page, current scope,
  latest visible person/feature signals and last committed execution key;
- do not replay historical messages, provider output or old-engine decisions;
- do not import any old pending question;
- persist the bootstrap only if this turn wins finalization.

This gives useful continuity without treating legacy prose as an instruction to the new engine.

## 7. Required-field completion and product version

Ticket creation is a cutover blocker, not an accepted regression.

### 7.1 Generic rule

For `CREATE_RECORD`, the platform applies only defaults already declared by the pinned entity field
definition. It then computes required missing fields in definition order:

- a bounded enum is asked as an explicit choice;
- a reference is selected only from visible records of the declared target entity;
- free text, integer, boolean and date fields use type-specific deterministic questions;
- several missing fields are collected one at a time in pending clarification memory;
- every answer is normalized, provenance-tagged and validated immediately;
- cancellation, refusal, scope change, timeout or a new request discards the unfinished create;
- no default is inferred from demo seed data, UI state or old-engine behavior.

The completed action is validated again against a fresh snapshot before dispatch. A create whose
product adapter has no honest execution translation is refused before confirmation or dispatch.

### 7.2 Linear definition

Published definitions are immutable. Do not edit v2. Publish v3 as a compatible version:

- issue priority default: `Medium`;
- issue status default: `Todo`;
- project remains an explicit visible reference and is asked when absent;
- the product supplies labels/options, while core supplies the question structure;
- the v3 golden cases record the intentional change from the old engine's silent project guess.

The generic required-field flow must also keep v2 sessions safe: they ask for priority, status and
project rather than failing or inventing values. New sessions pin v3 only after its compatibility,
checksum and offline parity checks pass. Rollback may move the deployment binding back, but no
existing session pin is rewritten.

## 8. Optional model path

Cutover does not depend on a paid model. Deterministic authority is enabled first.

- `LLM_ENABLED=false` or the provider kill switch bypasses the model and uses the deterministic
  grounded fallback.
- When enabled later, only a deterministic router fallback may call the model gateway.
- The gateway uses the existing provider budget, usage ledger, timeout, token limits and global
  paid-provider kill switch. One turn gets at most one reasoning attempt and no hidden retry.
- The exact 4a chain remains mandatory: prompt builder, strict parser, provenance verification,
  action validation and forced confirmation for every model-originated mutation.
- Model output cannot write platform speech in 5c. It may propose structured intent/action data;
  the response composer owns every sentence.
- Confirmation uses a fresh snapshot, makes zero provider calls and dispatches only the exact
  action named in the question.
- Provider timeout, malformed output, unattributable data and budget exhaustion produce named,
  deterministic outcomes. None falls back to the old engine.

Scripted-model tests cover ordinary answers, navigation, an in-scope mutation, prompt injection,
unknown action, duplicate JSON keys, timeout and budget exhaustion. Live paid evaluation is a
separate operator action with its own explicit budget.

## 9. Public response mapping

Every `TurnResponse` field has one named source and an exact test:

| Field | Definition-engine source |
| --- | --- |
| `session_id`, `turn_id` | accepted request |
| `status` | platform lifecycle mapping; never inferred from wording |
| `speech` | deterministic response composer or post-commit 5b receipt |
| `proposed_action` | product translation of the routed proposal, including denied proposals only where the public contract requires it |
| `validated_action` | product translation of the validated, scope-safe action |
| `execution` | 5b envelope for one dispatchable mutation; otherwise `null` |
| `intent_trace` | definition intent, route stage, feature and platform reason mapping |
| `signals` | generic signal extractor output after scope filtering |
| `retrieved_context` | fixed source metadata from the scoped knowledge boundary |
| `session_summary` | committed durable memory and bounded signal history |

The mapper is exhaustive over every engine stage. Adding a new stage fails a test until its status,
speech, action and execution behavior are declared. `status=completed` never implies a mutation
committed; only a 5b execution receipt may claim completion.

The web executor may navigate, highlight or submit the exact validated action it receives. It may
not reinterpret the visitor message, synthesize replacement speech, infer missing fields or turn a
failed backend request into a local success. If the API is unavailable, chat reports service
unavailability and performs no action.

## 10. Observability and cutover report

Record metadata only, partitioned by deployment, tenant, product, definition version and authority
mode:

- turns accepted, stale, cancelled, denied, unavailable and completed;
- engine stage, route stage and translated action key;
- clarification, fallback, ungrounded and refusal rates;
- execution keys dispatched, executed, failed, refused, replayed, expired and superseded;
- model attempted, blocked, malformed, timed out and over budget;
- snapshot, engine, model, finalization and total latency;
- state bootstrap, codec rejection, restart-expired pending state and optimistic-write conflict.

No message, field value, record title, prompt, provider output, execution key or session ID is
written to telemetry. The operator report compares the definition-mode rates with the accepted
legacy/shadow baseline and separates product behavior from platform failures.

## 11. Rollout and rollback

### 11.1 Before merge

1. Complete the entry gates and attach their evidence.
2. Run all suites with model transport disabled, then the scripted-model suite.
3. Run the exact rollback test against one database: legacy turn, definition turn, legacy turn,
   with the same session and no data command between them.
4. Run the database migration and rollback-compatible startup against a populated production-like
   copy.
5. Build both API and web production artifacts and run the full smoke path.

### 11.2 Deploy inertly

1. Back up and restore-check the production database.
2. Deploy code with `PIXEL_ENGINE_MODE=legacy` and the paid model off.
3. Confirm readiness reports legacy authority, run smoke tests, and confirm live responses are
   byte-compatible except for explicitly reviewed additive metadata.
4. Publish and bind Linear v3 for new sessions after compatibility and checksum verification.
5. Keep the 5a shadow on long enough to prove v3's reviewed matrix; then record the final report.

### 11.3 Cut over

1. Set `PIXEL_ENGINE_MODE=definition`; leave paid model reasoning off.
2. Restart/redeploy and require readiness before Vercel sends traffic.
3. Run the public visitor script: greeting, navigation, record lookup, create clarification,
   deterministic update and receipt, interruption, workspace isolation, product refusal, reset and
   voice playback.
4. Review the first 100 accepted turns and at least 60 minutes of telemetry. Evidence is based on
   both thresholds, not whichever completes first.
5. Enable the paid model only through a separate reviewed operator change after deterministic
   cutover is accepted.

### 11.4 Immediate rollback triggers

Set authority back to `legacy` and restart if any of these occurs:

- any cross-tenant, cross-user, cross-instance, cross-generation or cross-workspace disclosure;
- any mutation without a valid 5b key and receipt, duplicate write, dishonest completion claim or
  mismatch between the confirmed and committed change;
- any definition/checksum/pin bypass;
- any unknown engine-state codec accepted rather than refused;
- definition-engine 5xx rate above 1% over at least 20 accepted turns;
- p95 total turn latency above the accepted legacy baseline by more than 25% over at least 50
  comparable deterministic turns;
- fallback/ungrounded rate more than 15 percentage points above the reviewed shadow baseline;
- readiness disagreement with the configured authority mode.

After rollback, preserve logs and the database copy for diagnosis. Do not “fix forward” while an
isolation or duplicate-write defect is active.

## 12. Test plan

### 12.1 Authority and failure isolation

- unset and `legacy` select only the old engine; exact `definition` selects only the new engine;
  invalid values fail readiness and turn traffic;
- no request-controlled value can change authority;
- a definition exception, timeout or malformed model result never invokes the old engine;
- each mode calls exactly one engine and writes one set of messages/signals;
- the shadow is inactive in definition authority mode;
- every browser message makes one `/api/turn` request in both authority modes; local intent handlers
  and `localTurnResponse` are unreachable, including when the API is unavailable;
- a legacy-mode create/update either carries one valid 5b envelope or is refused; rollback never
  restores assistant keyless writes or duplicate finalization.

### 12.2 Durable state and races

- restart between follow-up turns preserves focus, last person, view, last change and summary;
- safe pending identifiers survive; pending arbitrary text becomes `pending_requires_repeat` and
  cannot be confirmed after restart;
- a new complete request replaces that marker normally;
- stale, cancelled and slower turns cannot overwrite newer state or dispatch a key;
- scope switch clears references; private reset invalidates the old generation;
- messages, signals, summaries and compatibility projection are workspace-scoped; legacy null-scope
  rows cannot introduce a person or feature from another workspace;
- corrupted/unknown codec and checksum mismatch fail closed;
- every owner/scope isolation permutation reads no foreign state;
- state finalization and key dispatch either commit together or both roll back.

### 12.3 Required-field completion

- v2 create asks for all three missing fields and never guesses;
- v3 applies only declared priority/status defaults and asks for project;
- enum, ref, text, integer, boolean and date questions validate their answers;
- ambiguous or inaccessible references are indistinguishable from unknown references;
- cancellation, refusal, scope change, expiry and a replacement request clear the draft;
- fresh-snapshot validation catches a project removed between question and confirmation.

### 12.4 Execution and response integrity

- one test for every public response field and every engine stage;
- non-mutations and clarification turns have `execution: null`;
- deterministic mutations dispatch once; model mutations dispatch only after exact affirmation;
- receipt speech appears only after commit and replay never writes twice;
- browser form writes remain keyless and assistant writes without an envelope are refused;
- “what changed?” reads only the current owner/workspace's executed entry.

### 12.5 Model boundary

- all 4a parser, injection, provenance and confirmation cases remain green through the runtime
  service;
- scripted provider success, timeout, malformed response, unknown action, budget exhaustion and
  cancellation each have one deterministic outcome;
- confirmation and deterministic turns make zero provider calls;
- paid-provider kill switch blocks every call before budget reservation.

### 12.6 Compatibility, browser and deployment

- every golden difference is listed and stale entries fail the test;
- the full API, product, web unit, production build and browser suites pass;
- Linux CI passes before merge;
- smoke tests cover both modes and the exact rollback sequence;
- no provider key is available in CI and no paid call occurs;
- production artifact starts in legacy mode before the operator flips it.

## 13. Implementation order

1. Authority configuration, readiness and tests, still defaulting to legacy.
2. Durable state codec/store, isolation, retention and legacy-session bootstrap.
3. Generic required-field completion and immutable Linear v3.
4. Definition turn service and atomic finalization without execution dispatch.
5. Backend-authoritative browser transport in both modes, with local decisions unreachable.
6. Response mapper and full-field parity tests.
7. Attach the 5b dispatch envelope and receipt lifecycle.
8. Optional model gateway and scripted-model tests, production-disabled.
9. Legacy authority adapter, compatibility projection and keyed rollback-mode mutations.
10. Rollback tests and operator reporting.
11. Full regression, migration rehearsal, Linux CI and inert deployment.
12. Production definition-mode cutover and acceptance report.

Each step is reviewable and green before the next. Do not combine the production switch change with
the code merge.

## 14. Exit criteria

5c is signed off only when:

1. Every entry gate in section 2 has recorded evidence.
2. Definition mode is authoritative in production and no request can reach two engines.
3. Every `TurnResponse` field and engine stage has explicit parity coverage.
4. Durable state survives restart, rejects corruption and cannot cross any ownership/scope boundary.
5. Ticket creation completes through declared defaults plus questions, never old-engine guesses.
6. Every assistant mutation uses one 5b key and one post-commit receipt.
7. Deterministic and scripted-model paths pass; paid model remains independently kill-switchable.
8. Golden, API, product, web, browser, production build and Linux CI suites are green.
9. The production acceptance window meets the error, latency and fallback limits.
10. Switching back to legacy restores the old engine for the same session without any operator
    database change; navigation and keyed mutation remain functional, and switching again to
    definition is equally clean.
11. The old engine and rollback switch remain present and tested. Their removal has not started.

## 15. Risks and deliberate limits

- **Two engines remain installed.** This is intentional only through 5c and is removed in 5d.
- **Compatibility projection can drift.** Field-by-field tests compare it with the new response and
  rollback exercises it; the old engine is never run secretly to maintain it.
- **Pending arbitrary text is not restart-restorable.** The platform asks for the request again
  rather than persisting another copy or reconstructing intent.
- **Single API replica remains required.** Shared durable state is built, but provider budgets,
  rate limits and current SQLite write characteristics still make multi-replica deployment out of
  scope.
- **Browser decision source remains until 3.3, but is unreachable after 5c.** This slice moves the
  live transport to backend authority because cutover would otherwise be false. Milestone 3.3
  deletes the dead handlers and closes any remaining direct browser-decision path.
- **Product shims remain.** The action translator stays until 3.6, record/data shims until 3.5 and
  knowledge/retriever shims until 3.4; none is old-engine cleanup.
- **Artifact rollback still exists after 5d, but switch rollback does not.** 5c proves the switch;
  5d deliberately removes it only after separate acceptance.
