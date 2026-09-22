# Milestone 3.2, Slice 5b: Execution Transport — Implementation Plan

Status: **revision 5, approved at the architecture and implementation-plan level. Owner review is
the final gate before coding. No runtime code has changed.**
Date: 2026-09-21. Parent plan: `docs/MILESTONE_3_STEP_3_2_PLAN.md` (revision 4.3), sections 4.3,
5, 10.6 and 13. Prerequisite: 5a merged (PR #20) and running in production with the shadow on.
5a is not signed off (that needs a reviewed `shadow-report` over real traffic); 5b does not depend
on that sign-off, 5c does.

## Revision history

**Revision 5.** Revision 4 was held for three blockers and four major corrections. Scope now moves
only with an accepted monotonic activation; terminal receipt outcomes have an explicit durable
state mapping; the shadow scheduler is ordered per session and bounded by both item count and
serialized bytes; a hung worker latches the shadow off until process restart; cancellation is
owner-bound across the cancelled turn's recorded workspaces; and latency has numeric acceptance
limits. Sections 6, 8, 10, 13 and 14 are normative.

**Revision 4.** Revision 3 was rejected with four blockers and four major corrections. Each was
checked against the code and holds.

| Finding | Confirmed in code | Resolution |
| --- | --- | --- |
| S1: supersession happens at dispatch, so an older key stays usable while a newer turn runs, and a non-mutating turn never supersedes | Activation (`activate_turn`) and ownership (`ensure_session`) are separate transactions; nothing cancels keys | Every turn's activation supersedes older keys in the same transaction; dispatch re-checks that its turn is still active (section 6) |
| S2: an "identical replay receipt" cannot be rebuilt; the ledger holds identifiers and a digest only | The ledger stores no field values, by decision | The first receipt is composed from the verified request; a replayed success says it was already applied and reloads the current record; failures replay identically from their stored code. No receipt text is stored (section 8) |
| S3: background comparisons break per-session order; a missing entry marks the session `memory_reset` for good | `ShadowRunner.complete` treats any missing entry on turn 2+ as a reset. This also affects 5a today: a session whose first turn was not compared is lost | A single shadow worker, a per-session in-flight register with a gap wait, and a reset only when context was really lost (section 10) |
| S4: owner binding omits the selected workspace | A session spans workspace switches; `conversation_owners.scope_id` is overwritten with the latest | Keys and history carry `scope_id`; a key writes only in its workspace; "what changed?" reports only the current workspace (section 7) |
| C5: lifecycle order unclear | — | One normative sequence for a turn, a keyed write and a cancel (section 6) |
| C6: "a newer request replaced it" was also used for expiry | — | One honest sentence per outcome code (section 8) |
| C7: "Production does not change" is false | — | "Production mutation authority is unchanged", with every production change listed (section 2) |
| C8: shadow cap and breaker need exact concurrency semantics | — | A single worker makes the cap exact; breaker state under one lock on a monotonic clock (section 10) |

**Revision 3.** Owner-bound ledger reads, a post-commit receipt, the shadow off the request path,
retention indexes and triggers. **Revision 2.** Change-set keys (the browser sends a whole ticket
from its own state, so a key cannot bind "the exact request"), the comparator rule for `execution`,
ledger retention, no production-readable test switch, ticket creation moved out, the confirmation
rule, existing versus new tests, one transaction for private-instance writes. **Revision 1.** The
private-instance write blocker (B1, section 4).

Process: the 5b pull request is merged only after its own CI is green (PR #20 was merged before its
CI finished).

## 1. Outcome

A mutation the engine proposes can be carried out by the browser under a one-time execution key,
and only under that key. When 5b is complete:

1. `TurnResponse` has an `execution` envelope (parent 5.5): a key for a dispatched mutation, `null`
   for everything else.
2. The browser sends that key, with the session and the bound change, on exactly the write it
   authorizes. Form writes are unchanged and carry no key.
3. A keyed write lands only in the caller's own records, inside the workspace the key was issued
   for.
4. Activating any newer turn, or cancelling a turn, makes every older unused key of that owner's
   session unusable, in the same transaction.
5. Completion is spoken only from a receipt the backend composes after the write committed.
6. Every ledger read is bound to its full owner and workspace. Every cancellation is bound to its
   full owner; it may invalidate that owner's recorded keys across workspaces but reads no records.
7. The 5a shadow **comparison** runs off the request path, in session order, and records gaps
   honestly; bounded preparation and enqueue remain on the request path and are measured.

## 2. What changes in production

**Production mutation authority is unchanged.** The live engine stays authoritative and dispatches
no keys, so `execution` is `null` on every live response until 5c. 5b proves the transport in API
and browser tests and in Linux CI; 5c turns it on behind the rollback switch.

These do change in production, each with its inertness evidence:

| Change | Why it is safe in production | Evidence |
| --- | --- | --- |
| `TurnResponse` gains `"execution": null` | The web client ignores unknown fields; the comparator treats `null` on both sides as `match` | Schema test, comparator test, browser suite, smoke test |
| Accepted turn activation also records the workspace and cancels the owner's older `dispatched` keys | No keys exist in production, so cancellation changes zero rows; moving scope into accepted activation prevents stale turns changing it | Tests: activation without keys writes no ledger rows; stale activation cannot change scope |
| Keyed endpoints accept a change set instead of a full ticket | Nothing calls them in production today | Browser test: form writes unchanged |
| `action_executions` gains `scope_id`, two indexes, settlement-integrity triggers, and a retention job | A startup gate, also available as an operator preflight, verifies there are no live legacy keys before pruning or serving traffic; nullable legacy rows are never claimable | Populated-schema migration test, trigger tests, startup-gate/preflight command, retention tests |
| The shadow moves to a byte-bounded worker scheduler with ordering, a queue bound and a breaker | Comparisons leave the request path; only an immutable serialized payload crosses the hand-off | Numeric latency benchmark; 5a golden parity unchanged |
| "What changed?" cue in the new engine | The new engine is not authoritative in production | Engine tests |

## 3. What exists and what is missing

| Part | State |
| --- | --- |
| `ExecutionLedger`: dispatch, claim, settle, cancel, replay (parent 5.1 to 5.3) | Built and tested in slice 3 (`test_execution_boundary.py`) |
| Keyed endpoints for ticket create and update | Built, but digest a full ticket body and write only to the shared member records |
| `TurnResponse.execution`, dispatch from a turn, the browser sending a key | Missing |
| Workspace-bound ledger reads and full-owner-bound cancellation | Missing: `last_executed` and `cancel_turn` filter by session only |
| Supersession at activation | Missing |
| Execution receipt | Missing |
| Ledger retention | Missing |
| "What changed?" in the new engine | Falls back to a document answer |
| Shadow off the request path, in order | Missing |

## 4. Carried blocker

**B1. Keyed writes ignore private demo instances.** `main._keyed` resolves the target through
`legacy_record_owner()`, the shared member demo, so a visitor's keyed write would change shared
records. Unreachable today only because nothing dispatches a key. Fixed in section 7.

## 5. Components

| Change | Where |
| --- | --- |
| `ExecutionEnvelope`, `ExecutionReceipt`; `TurnResponse.execution` | `apps/api/app/schemas.py` |
| Change-set request models | `apps/api/app/record_schemas.py` |
| Owner and scope columns, indexes, settlement-integrity triggers | `apps/api/app/db.py` (`action_executions.scope_id`) |
| `activate_and_supersede`, `dispatch_if_active`, owner-bound `claim`, `cancel`, `last_executed`, `prune` | `app/engine/execution.py`, `app/services/session_manager.py` |
| Keyed writes in the caller's store and workspace, one transaction | `app/main.py` (`_keyed`), `app/services/record_writes.py` |
| Turn orchestration for the new engine (dispatch, receipts, ledger view) | new `app/services/turn_execution.py` (service; the engine stays pure) |
| "What changed?" cue answered from a ledger view passed in as data | `app/engine/conversation.py`, `app/engine/conversation_engine.py` |
| Shadow worker, in-flight register, breaker, reset rule | `app/services/shadow.py`, `app/services/shadow_parity.py`, `app/main.py` |
| Test entry module for browser tests | new `apps/api/app/testing_main.py` |
| Browser: envelope from turn to write; receipts shown and spoken | `apps/web/lib/agent-api.ts`, `action-executor.ts`, `product-data-api.ts`, `app/page.tsx` |

## 6. The lifecycle, in order (S1, C5)

Each numbered request-lifecycle step that writes is **one** `begin immediate` transaction. The only
other writers are the already-defined private-reset transaction and the bounded retention job; no
other request-path code writes `sessions` or `action_executions`.

### 6.1 A turn

1. **Authorize** (as today): authentication, product access, record grant, and the selected
   workspace (`require_scope`).
2. **Session**: `ensure_session` creates the session for its owner, or verifies the owner. For an
   existing session it does **not** update `conversation_owners.scope_id`; a stale or delayed
   request is never allowed to move the session's workspace.
3. **Pin** (as today): the session's definition pin is checked.
4. **Activate, select scope and supersede** — one transaction, for **every** turn:
   - lock/read the session and owner rows and require the full owner (user, organization, product,
     instance and generation);
   - set `active_turn_id = latest_turn_id = turn_id` only where `latest_turn_id` is null or lower;
   - if no row changed, the turn is stale: stop, answer `stale`, nothing else is written;
   - only after that monotonic update succeeds, set `conversation_owners.scope_id` to this turn's
     already-authorized workspace;
   - mark `cancelled` every `dispatched` key of this owner in this session with an older `turn_id`,
     in any workspace, with result code `superseded`. Executed and failed keys are never touched.
5. **Run the engine.** It reads its snapshot and writes nothing.
6. **Dispatch** — only for a mutation that may be executed now (section 7.4), one transaction:
   - require the same owner, `active_turn_id = turn_id`, and the session's recorded workspace
     equal to this turn's workspace;
   - if any check fails, no key is issued and the turn is answered `stale`;
   - otherwise insert the key, bound as in section 7.
7. **Complete**: `active_turn_id` becomes `null` where it still equals `turn_id` (as today). It
   does not change `latest_turn_id` or `scope_id`. The key stays usable until it is executed, it
   expires, or step 4 of a newer turn cancels it.
8. **Respond**, then hand work to the background: the shadow queue, the counter flush and the
   retention job.

Two turns of one session in any timing therefore give one result: the newer turn's activation
cancels every older unused key, and an older turn that reaches dispatch after being superseded
issues nothing. A newer turn that is only a question, a navigation or a refusal still supersedes,
because supersession belongs to activation, not to dispatch.

### 6.2 A keyed write

One transaction: re-check authorization now (grant, workspace, pin, definition lifecycle); claim
the key under the replay contract (parent 5.3) with owner, instance, generation, workspace, state,
expiry and change-set digest; change the record in the caller's own store; store the outcome. Then,
outside the transaction, compose the receipt (section 8).

The write service catches only the enumerated deterministic rule failures in section 8.1 inside
this transaction, settles the key with their stable result code, and commits that settlement. Any
unclassified or infrastructure exception escapes the transaction, rolling back both the record
write and ledger mutation so the same key remains retryable.

### 6.3 An explicit cancel

One transaction (`/api/turn/{id}/cancel`): verify the full owner; clear `active_turn_id` where it
equals the cancelled turn; mark `cancelled` this owner's `dispatched` keys in the session with
`turn_id =` the cancelled turn and result code `user_cancelled`. A stale or fabricated high turn
ID therefore cannot cancel a newer key. Cancellation is deliberately
owner-bound, not client-scope-bound: the request carries no workspace, and it invalidates the
cancelled conversation work across the scopes already recorded on those keys. It never reads or
writes a product record.

## 7. Keys, owners and workspaces (S4, B1)

### 7.1 What a key binds

- **Owner:** organization, product, user, private instance ID and generation (as recorded in
  `conversation_owners`).
- **Workspace:** the `scope_id` of the turn that dispatched it. New nullable column
  `action_executions.scope_id`; every key issued from 5b on has it.
- **Change set, not a request body:**
  - Update: `{"action": "update_issue", "target": "<id>", "changes": {field: value}}`, exactly
    the changed fields, in the endpoint's field vocabulary.
  - Create: `{"action": "create_issue", "fields": {field: value}}`, exactly the supplied fields.
    The server assigns the ID and the workspace fields from the key's workspace.
- The ledger stores the canonical change set's digest; no values (unchanged decision).
- Migration is additive: existing rows receive `scope_id = null`. Before deployment an operator
  preflight reports counts by state and refuses rollout if a legacy `dispatched` row exists. The
  same check is a mandatory startup gate after migration and before retention or traffic, so a
  skipped command cannot make the deployment unsafe and pruning cannot erase the evidence first.
  A null-scope row is never claimable after 5b; settled legacy rows remain only for retention and
  are excluded from request-reachable history.
- Additive `before insert` and `before update` triggers reject any new `executed`, `failed` or
  `cancelled` row whose `result_code` is null. They do not rewrite existing settled legacy rows.

### 7.2 Keyed endpoints

- `PATCH /api/demo-data/issues/{id}` with `X-Execution-Key`, `X-Session-Id` and
  `{"changes": {...}}`: inside the transaction the server loads the current record, applies the
  changes and increments the revision from the database. The browser's copy is never used.
- `POST /api/demo-data/issues` with the same headers and `{"fields": {...}}`. Anything else in a
  keyed create is refused.
- The old full-body keyed paths are removed; form writes (no key) are unchanged.
- A change set that differs from the bound one in any field, value or target is `409`, nothing
  written.

### 7.3 Where a keyed write may land

- The caller's own store: `ProductDataStore(grant.demo_context)` for a visitor, the member demo for
  a member. Its write joins the claim transaction through `connection=`.
- The key's instance ID and generation must equal the caller's current instance; a key from before
  a private reset is unrecognized by the request path and returns plain 404 with no receipt. The
  old ledger row retains `cancelled/instance_reset` for audit and retention only.
- The target record must be inside the key's workspace **and** that workspace must still be in the
  caller's grant. A key never writes in another workspace, whatever the session's current
  selection (`scope_mismatch`).

### 7.4 When a mutation may be dispatched (parent 4.3)

`requires_confirmation = action.confirm OR target came from a correction OR model-originated`.
Linear declares `confirm` on no action, so a deterministic update ("assign it to Noah") is
dispatched on its turn, as today's live engine behaves. A correction-targeted update or any
model-originated mutation waits for a "yes"; that turn carries `execution: null`, and the "yes"
turn dispatches the key for the confirmed action only.

### 7.5 Owner- and workspace-bound reads

`last_executed(owner, session_id, scope_id)` first requires the session to be owned by this
caller, then filters on organization, product, user, instance, generation, session and workspace.
A session ID reused or guessed by anyone else, an old instance generation, and another workspace
all read nothing. Every other request-reachable ledger **read** gets the same binding; session-only
forms are removed. Cancellation follows section 6.3: full-owner-bound across the affected turns,
without reading customer records or accepting scope from the client.

## 8. Receipts and wording (S2, C6)

### 8.1 Durable outcome mapping

Receipt outcome is derived from a durable ledger state and `result_code`; it is not inferred from
an exception string. These transitions are normative:

| Event | Ledger transition, in the same transaction | API result |
| --- | --- | --- |
| Record change commits | `dispatched -> executed`, code `applied`, record ID stored | `executed` receipt |
| Valid owned key, but deterministic validation, conflict, missing record, revoked scope or request mismatch | `dispatched -> failed`, stable specific code | `failed` receipt |
| Newer turn activation | `dispatched -> cancelled`, code `superseded` | Later claim returns `refused` receipt |
| Explicit user cancel | `dispatched -> cancelled`, code `user_cancelled` | Later claim returns `refused` receipt |
| Key expires when claimed | `dispatched -> cancelled`, code `expired` | `refused` receipt |
| Private demo reset | `dispatched -> cancelled`, code `instance_reset` in the reset transaction, then generation increments | Reset endpoint confirms the reset; a later old-key claim is ordinary 404 with no receipt |
| Unknown key or mismatch on tenant, product, user or session ownership | no row changed and no detail disclosed | ordinary 404, no receipt |
| Infrastructure failure before commit | whole transaction rolls back; key remains `dispatched` | ordinary error, no receipt; retry remains possible |

The request digest is checked only after the key is proven to belong to the caller. A mismatch on a
known, owned key settles it as `failed/invalid_change`; the same key cannot be probed repeatedly or
later reused with another body. `result_code` is required for every settled row. The existing
states remain `dispatched`, `executed`, `failed`, and `cancelled`; `refused` is an API receipt
outcome, not a fifth ledger state.

### 8.2 Receipt contract

Every owned, recognized keyed-write outcome carries
`{"outcome": "executed" | "failed" | "refused", "code": "...", "speech": "...", "record": {...}}`.
`record` is present only for `executed`, read through the caller's current access and workspace.
Receipt text is composed by the platform composer and **never stored**.

- **First execution:** composed after commit from the committed outcome and the request's change
  set, which the server has just verified against the key's digest, so every value in the sentence
  is one the key bound: "Updated LIN-142: assignee to Noah Patel."
- **Replay of an executed key:** the ledger holds no values, so the receipt does not repeat them:
  "This change was already applied.", with the record reloaded as it is now (parent 5.3). The
  sentence differs from the first receipt by design; nothing claims otherwise.
- **Replay of a failed or refused key still recognized under the current owner:** identical to the
  first, because its ledger state and `result_code` were committed before the first receipt was
  returned. A private reset changes the owner generation, so its old keys are no longer recognized
  by request-reachable claim or replay paths.
- **Unexpected error (rolled back):** an error without a receipt; nothing claims a change.

### 8.3 Sentences by outcome code

| Code | Speech |
| --- | --- |
| executed (first) | "Updated {record}: {changes}." / "Created {record}: {changes}." |
| executed (replay) | "This change was already applied." |
| `execution_result_unavailable` (replay, record no longer visible) | "This change was already applied, and that record is no longer available here." |
| `superseded` | "A newer request replaced this change, so it wasn't applied." |
| `user_cancelled` | "This change was cancelled before it was applied." |
| `expired` | "This change expired before it was applied." |
| `record_conflict`, `record_not_found` | "The record changed or is no longer available, so this change wasn't applied." |
| `scope_mismatch`, `scope_unavailable` | "This change is not available in this workspace, so it wasn't applied." |
| `invalid_change`, other rule rejection | "This change isn't valid for that record, so it wasn't applied." |
| another owner, unknown key | Plain 404 with no receipt: nothing reveals that the key exists |

### 8.4 In the browser and in the conversation

- The browser appends the receipt's `speech` to the conversation and the activity log and speaks
  it through the normal voice path (Azure, within the existing speech budget), only after the
  response arrives. It never writes its own success or failure sentence for a keyed write, and
  never shows completion for a write whose receipt it did not receive.
- The dispatching turn uses proposed wording ("I'll update LIN-142: assignee to Noah Patel.").
- "What changed?" and "what did you do?" are platform conversation cues after routing. The service
  passes the engine the owner- and workspace-bound last `executed` entry as immutable data; the
  engine answers `last_change`, or `nothing_changed` when there is none. Failed and cancelled keys
  are never reported as changes.

## 9. Retention

- A key row is deleted 30 days after `settled_at` (executed, failed, cancelled), or 30 days after
  `expires_at` if never settled.
- Indexes: `action_executions_settled (settled_at) where settled_at is not null`;
  `action_executions_unsettled_expiry (expires_at) where state = 'dispatched'`.
- Exactly two triggers: application startup (`lifespan`, after `migrate()` and the legacy-key
  startup gate), and a
  `BackgroundTasks` job scheduled from `/api/turn` at most once a minute after the response is
  sent, independent of the shadow switch.
- Each run: at most 20 batches of 500 rows, oldest first, one short transaction per batch; a failed
  run is logged and retried at the next trigger.

## 10. The shadow: off the request path, in order (S3, C8)

This corrects 5a and lands first in the 5b pull request.

### 10.1 Controller, worker and byte-bounded hand-off

- `prepare` stays in the request, before the live turn, and converts only the records already
  loaded. After the live response is final, the request creates one immutable canonical-JSON work
  payload; the queue never retains the live response object or a second mutable record graph.
- One item may contain at most **512 KiB**. The scheduler accepts at most **128 items** and **16
  MiB** of serialized payload in total. All three limits must pass under one lock. Oversized items
  are `shed/snapshot_too_large`; count or byte exhaustion is `shed/queue_full`. A shed turn is
  recorded as lost context for its shadow session.
- Enqueue is a non-blocking `try_enqueue`: serialize, take the controller lock, account bytes and
  append, then return. It never waits for the worker. The worker releases the exact byte charge
  when an item is removed or discarded.
- Exactly one daemon worker executes comparisons. A separate daemon watchdog observes heartbeat
  state but never executes a comparison. The controller, scheduler, breaker and counters have one
  explicit lifecycle owned by FastAPI lifespan; tests may construct and close them directly.

### 10.2 Session-order scheduler

- **Registration.** At `prepare`, register `(shadow key, turn_id)` as `in_flight` under the
  controller lock (at most 8 per session; excess is shed). A request `finally` resolves it to
  `queued`, `no_context_effect` (stale, cancelled or gated before the shadow), or `lost` (error or
  shedding). `no_context_effect` is allowed only when the request is proven not to have entered the
  conversation engine and therefore cannot change engine memory. Every eligible live turn that
  entered the engine but did not complete a shadow comparison is `lost`, including breaker-open,
  worker-unhealthy, preparation/serialization failure, queue shedding, comparison error and
  shutdown. Every transition notifies the scheduler condition.
- **Data structures.** Accepted items live in a min-heap per shadow session, ordered by `turn_id`.
  A round-robin ready-session deque prevents one session monopolizing the worker. Sessions waiting
  for a lower registered turn live in a deadline heap; the item does not occupy the worker while
  parked, so ready work from other sessions continues.
- **Ordering.** The worker may compare a session's lowest item only when every lower registered
  turn is already compared or resolved `no_context_effect`. If a lower turn is still in flight, the
  session is parked for at most 2 seconds and revisited on notification or deadline.
- **Lost gap.** If the deadline expires, or a lower registration resolves `lost`, the waiting item
  is counted `memory_reset/gap_lost` and is not compared against incomplete memory. The session is
  reset for its remaining life. A lower item arriving after a higher item was resolved is
  `not_compared/late`; it never rewinds memory.
- Registration, queue accounting, parking and removal are tested as one state machine. An item is
  in exactly one state and its bytes are charged exactly once.

### 10.3 When a session is reset

A missing entry no longer means a reset by itself. A session is reset, and its remaining turns are
counted `memory_reset`, only when the shadow actually lost part of its context:

- the session started before this process (its `sessions.started_at` precedes the application
  lifespan timestamp): a restart. The timestamp is captured at lifespan start and passed into the
  lazily-created shadow controller; worker creation time is not used;
- the session predates the current **shadow-enabled epoch**. Turning shadow off and back on creates
  a new epoch, so an existing conversation cannot resume against memory that missed the disabled
  interval;
- a tombstone exists for its key: its entry was evicted or expired. Tombstones are keyed hashes in
  a bounded LRU of 10,000 and contain no customer content;
- one of its eligible turns was not compared: it was shed, breaker-gated, worker-gated, errored,
  lost at a gap, shut down, or arrived late (section 10.2).

Otherwise a missing entry on any turn number starts fresh memory only when every earlier observed
turn in the current epoch is proven `no_context_effect`. A reset lasts for the rest of that session,
because lost context cannot be rebuilt; that is honest and bounded by the session's life.

### 10.4 Breaker, hung worker and shutdown

- Recoverable breaker state is: consecutive slow count, recent error times and `open_until`. The
  controller lock guards all state; all elapsed time uses `time.monotonic()`.
- Five consecutive completed comparisons over 250 ms, or five completed errors within 60 seconds,
  open the breaker for five minutes. While open, `prepare` does not materialize a snapshot and the
  eligible turn counts `circuit_open` and its session is marked as having lost context. After
  cool-down exactly one trial is admitted: fast success closes; slow or error reopens. The trial
  starts with reset memory; it never resumes the incomplete pre-breaker memory.
- The watchdog samples every 250 ms. If the current comparison has no heartbeat/return for **2
  seconds**, it atomically latches `worker_unhealthy`, opens the breaker without a cool-down,
  rejects new shadow work, drains queued payloads with `shed/worker_unhealthy`, and emits one
  operator alert. Python cannot safely terminate a stuck thread, so this plan does not pretend to
  recover it: an application-process restart is required. No replacement worker is started beside
  an abandoned one.
- Shutdown stops admission, resolves queued work as shutdown shedding, asks the worker to stop,
  joins worker and watchdog for at most 250 ms each, flushes counters, and returns. Both threads are
  daemon threads, so a stuck comparison cannot block deployment or process exit.

### 10.5 Reported

`shed` with reason, `circuit_open`, `worker_unhealthy`, and the reason a session was reset join the
turn-level report. Per-turn metadata records preparation, serialization, queue wait and comparison
time separately; it never records messages, snapshots or record values. The 25 ms engine budget
applies to comparison time.

## 11. Browser and test path

- `agent-api.ts` keeps `execution`; the executor sends the change set with `X-Execution-Key` and
  `X-Session-Id` and **no** `Idempotency-Key` (one mechanism), updates the screen from the returned
  record, and shows the receipt. An assistant mutation without an envelope is not written; no write
  is made for a failed, cancelled or stale turn. Form writes are unchanged.
- **No environment switch.** API tests drive the new-engine turn path directly or through a FastAPI
  dependency override. Browser tests start the API from `app.testing_main`, which production never
  imports and which refuses to start without an explicit test marker. Tests prove `Dockerfile.api`
  starts `app.main` and that nothing reachable from `app.main` imports `app.testing_main`.

## 12. Approved decisions and implementation order

- **D1. Production mutation authority stays with the live engine until 5c.** Approved.
- **D2. Ticket creation is not in 5b** (section 15). Create keys are proven on the neutral
  definition, which can create records. Approved.
- **D3. Browser proof goes through `app.testing_main`.** Approved; no production-readable engine
  switch is introduced.
- **D4. Replay receipts are generic for successes** (section 8.2), rather than storing receipt
  text, because stored text would copy field values into the ledger against the identifiers-only
  decision. Approved.
- **D5. Keys and history are bound to the workspace** (section 7), rather than a new session per
  workspace, which would change live behaviour in 5b. Approved. Explicit cancellation remains
  full-owner-bound as specified in section 6.3.

Implementation lands as reviewable commits on the existing phase branch, in this order. Each step
keeps all suites that can exercise it green before the next begins:

1. **Shadow correction only:** controller, scheduler, byte limits, breaker/watchdog, shutdown and
   parity classes. Production shadow can be disabled instantly with its existing switch.
2. **Additive storage:** `scope_id`, result-code requirements for new settlements, retention
   indexes, settlement-integrity triggers, populated-schema migration and deployment/startup
   preflight. No endpoint contract changes.
3. **Ledger/session primitives:** accepted-turn scope update, activation supersession,
   owner/scope-bound reads, exact-turn cancellation, durable outcome mapping and concurrency tests.
4. **Keyed write service:** change-set models, caller-store routing, atomic claim/write/settle and
   platform receipts. Production still has no dispatcher.
5. **New-engine test orchestration:** envelope dispatch, confirmation and ledger view behind the
   isolated test entry module.
6. **Browser transport:** carry envelope, send change set, render/speak receipts, preserve keyless
   forms, and run full browser regression.
7. **Evidence and rollout:** all suites, fixed latency profiles, migration/preflight evidence,
   Linux CI, production smoke with mutation authority unchanged, and the updated 5b report.

Rollback before 5c is straightforward: turn the existing shadow switch off and redeploy the prior
application revision. The database changes are additive; old code ignores `scope_id`, the indexes
and result codes. No production engine can issue a 5b key, so rollback does not strand live
assistant mutations.

## 13. Tests

**Already built in slice 3 (`test_execution_boundary.py`), kept green and re-pointed to change
sets:** the replay contract per row (including across a restart), concurrent duplicates,
cancellation races in both orders and in threads, rule rejection versus injected fault, foreign
callers leaving the ledger unchanged, expiry never touching outcomes, replay needing current
access, execution-time re-checks, and refusal of a keyed request carrying `Idempotency-Key`.

**New in 5b:**

- **Supersession at activation (S1):** turn 1 dispatches; turn 2 activates and is a question: turn
  1's key is refused with stored code `superseded` before turn 2 finishes. Turn 2 slow and turn 1's
  write sent during it: refused. Turn 1 reaching dispatch after turn 2 activated: no key, `stale`. Both orders
  in threads: exactly one usable key, the newest. Executed keys are never cancelled. Another
  owner's activation or cancel on the same session ID changes nothing. Activation without keys
  writes no ledger rows. A delayed stale turn cannot overwrite the accepted turn's workspace;
  `ensure_session` never updates scope for an existing session. Cancelling an old or fabricated
  high turn ID cannot cancel a different turn's key.
- **Receipts (S2):** first execution speaks the bound values; a replay says "This change was
  already applied." with the current record and no second write; failed and refused replays are
  identical to the first; each transition in section 8.1 commits the required state and code and
  each code in section 8.3 has its sentence; unknown and foreign keys change no row and return no
  receipt; an injected infrastructure fault gives no receipt and leaves the key retryable; no
  receipt text or field value is written to the ledger. Direct insert and update attempts cannot
  create a newly settled row without a result code; populated legacy settled rows remain readable
  for retention without being rewritten.
- **Shadow order (S3):** turn 2 queued before turn 1 is compared after turn 1 with no reset; turn 1
  unregistered as stale releases turn 2 at once; turn 1 arriving after the 2-second gap is
  `not_compared` and resets the session; a session whose first turn was stale continues normally
  (no reset); a restart-era session, a shadow disable/enable epoch change, an evicted entry
  (tombstone), a breaker- or worker-gated eligible turn and a shed turn reset; two
  sessions never block each other; a parked session does not occupy the worker; count, per-item and
  total-byte limits shed with the correct reason and never leak an accounting charge; a parked
  session never blocks ready work from another session; the breaker
  opens, trials and closes under concurrent requests; a hung comparison latches
  `worker_unhealthy`, drains the queue, admits no replacement worker, and cannot delay a response
  or shutdown.
- **Workspace (S4):** a key issued in workspace A is refused (`scope_mismatch`) for a record in B
  and after the grant loses A; switching to B then asking "what changed?" reports nothing from A;
  a create assigns workspace fields from the key's workspace only. A populated old schema migrates
  without losing rows; null-scope rows cannot be claimed or returned by history; both the operator
  preflight and mandatory startup gate refuse a live legacy `dispatched` row before pruning runs.
  A private reset stores `cancelled/instance_reset` for audit and increments the generation; a
  later claim with the old key returns the same plain 404 as any unrecognized key and no receipt.
- **Envelope, change sets, own store, confirmation, comparator, retention, test-path isolation,
  browser:** as in revision 3: a dispatched mutation carries an envelope and everything else
  `null`; a change set succeeds against a ticket whose other fields and revision changed, and any
  differing field is `409`; a visitor's write changes only that instance, and a pre-reset key is
  refused; a fault after the instance write leaves nothing; confirmation cases dispatch only after
  "yes"; a live `"execution": null` is `match`; retention uses its indexes (`explain query plan`)
  and runs with the shadow off; `app.testing_main` is unreachable from `app.main`; intercepted
  browser requests carry key, session and change set and no idempotency header, form writes carry
  no key, and receipts are shown and spoken only after the response. Explicit cancellation is
  owner-bound across the cancelled turn's recorded scopes and requires no client-supplied scope.
- **Latency:** instrumentation reports preparation, serialization and enqueue independently. A
  fixed local benchmark uses two profiles: at least 500 paced representative turns with no
  shedding, and a burst that deliberately reaches admission limits. In the paced profile,
  shadow-on minus shadow-off request latency is at or below 10 ms p95 and 25 ms p99. In the burst,
  enqueue never waits and excess work is shed with the expected reason. The same stage metrics are
  inspected after deployment rather than inferred from total turn latency.
- Existing suites, the 5a golden parity and the router comparisons stay green; any changed
  difference is listed with its reason.

## 14. Exit criteria

1. Every test above green locally and in Linux CI, including the browser tests; the pull request
   merged only after its own CI is green.
2. Production mutation authority unchanged: `execution` is `null` on every live response; the live
   smoke test passes; every production change in section 2 shows its evidence.
3. No keyed write can reach records outside the caller's own store and the key's workspace.
4. Supersession happens at every accepted activation; every request-reachable ledger read is bound
   to the full owner and current workspace. Cancellation is bound to the full owner and exact turn,
   accepts no client workspace, and can invalidate only that owner's keys recorded for that turn.
5. Ledger retention active, indexed and triggered as specified.
6. The shadow runs on its worker scheduler, in session order, with no false resets; the fixed
   benchmark meets the 10 ms p95 and 25 ms p99 incremental request-path limits; production stage
   metrics for preparation plus serialization plus enqueue remain within those same absolute
   limits; `shadow-report` shows `shed`, `circuit_open` and
   `worker_unhealthy` at or near zero, with every nonzero reason reviewed.

## 15. After 5b (not in this plan)

- **Ticket creation** (5a golden case `create-known-owner`): the new engine refuses every create
  because priority, project and status have no source. Its own plan before 5c: definition-declared
  defaults and a platform question for a missing required reference, never a guess.
- **5c** needs the reviewed production `shadow-report`, restart-safe engine memory stored as
  identifiers with bounded retention, and a decision on every `behaviour` and `coverage` count.

## 16. Risks

- **The browser executor is shared by every action.** The unchanged form-write tests and the
  browser suite guard it.
- **Two write mechanisms until 3.5** (keyed assistant writes, keyless form writes): the stated
  limitation in parent 5.4.
- **Activation now writes in the same transaction as a cancellation.** It adds one indexed update
  per turn that changes no rows until keys exist; covered by the inertness and latency evidence.
- **A single shadow worker is a throughput ceiling.** At about 7 to 13 ms per comparison it handles
  far more than current traffic; above it, turns are shed, never delayed.
- **Serialized snapshots consume memory.** Per-item, item-count and total-byte limits are enforced
  before admission; queue accounting has invariant tests and no payload is persisted or logged.
- **A Python thread cannot be killed safely.** A hung comparison permanently disables shadow work
  in that process and requires restart. This is intentional fail-open behaviour for live traffic,
  surfaced as `worker_unhealthy`, not hidden as automatic recovery.
