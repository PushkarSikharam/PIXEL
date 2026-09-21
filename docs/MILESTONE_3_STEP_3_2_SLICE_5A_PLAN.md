# Milestone 3.2, Slice 5a: Shadow Mode — Implementation Plan

Status: **revision 2, APPROVED by the stakeholder on 2026-09-21 (sections 3, 6 and 7 cleared for
implementation). Built; see `docs/MILESTONE_3_STEP_3_2_SLICE_5A.md`. Section 11 records where the
build differs from this text and why.**
Date: 2026-09-21. Parent plan: `docs/MILESTONE_3_STEP_3_2_PLAN.md` (revision 4.3), section 13.
Prerequisites met: 4a and 4b signed off; private demo instances live and accepted
(`docs/DEMO_VISITOR_ISOLATION_PLAN.md`).

## Revision 2 (2026-09-21)

The review rejected revision 1 with five blockers. Each is resolved below; two rest on premises the
code contradicts, and the correction is stated rather than silently absorbed.

1. **Mutation comparison.** The review assumed the live engine returns `status="executed"`, an
   `execution` payload and committed writes. It does not: `TurnResponse.status` is one of
   `completed`, `cancelled`, `stale`, `denied`; the schema has no `execution` field until 5b; and
   the live engine writes no records during a turn (the browser writes afterwards). The real gap
   stands, though: a live mutation and a shadow proposal of the same change would have been counted
   as `behaviour`. Section 5.2 now defines the `lifecycle` class and an exact equivalence rule.
2. **Telemetry writes.** Accepted in full. Counts aggregate in memory and are flushed in one
   batched upsert, at most once a minute, after the response is sent; the table has a composite
   primary key. The 25 ms budget measures the engine only (sections 5.1, 5.3).
3. **Signal accumulation.** Accepted in full. The engine takes and returns an explicit, immutable
   `SignalHistory`; `ConversationMemory`, a signed-off contract, is unchanged (sections 4.1, 4.4).
4. **Pre-turn copy.** Accepted, with the premise narrowed: the records are small (at most 1,000 per
   private instance by limit), but the plan did not say what happens with the switch off. Now: off
   means no snapshot and no copy at all; on means one immutable snapshot built before the live turn,
   with no separate deep copy (section 5.1).
5. **Locks and workers.** Accepted. Striped locks replace per-key locks (no lock table to leak), and
   the single-process invariant is stated and tested (section 4.3).

Found in our own re-review, not raised by the review: revision 1 said the seed "binds the product
to v2", but the seed only creates missing bindings, so production would have stayed on v1 without
registering v2. Section 7 now specifies an explicit, reversible version-move command.

## 1. Outcome

The new engine answers every live turn **in the shadow of** the current engine, on exactly the same
input, and a structured report says where the two differ and why. The current engine stays
authoritative: nothing a visitor sees, and nothing stored for them, changes because the shadow runs.

5a is complete when three things are true:

1. **The shadow is provably inert.** Zero provider calls, zero execution keys, zero writes to any
   live table, and a byte-identical live response with the shadow on or off.
2. **The comparison is complete and honest.** Every `TurnResponse` field is compared, and every
   difference on the golden conversations is listed with its kind and reason. Platform-wording
   differences are counted separately from behaviour differences.
3. **Linear v2 exists** as an immutable, published definition written for the revision 4.3
   response boundary, and new sessions pin it.

**Not in 5a:** the execution envelope and browser keys (5b); the new engine becoming authoritative
(5c); removing the old engine (5d); the model path in the shadow (it stays off, see section 4).

## 2. Mandatory conditions (from the 2026-09-18 and 2026-09-20 reviews)

| # | Condition | How it is enforced (section) |
| --- | --- | --- |
| C1 | Shadow memory is independent and cannot mutate live sessions, records, ledgers or pending confirmations | Separate in-process store, no write capability passed in (4.3, 6.2) |
| C2 | Zero paid calls | No model transport, no speech; construction takes none (4.1, 6.1) |
| C3 | Zero execution keys | No `ExecutionLedger` reachable from the shadow; mutations stop at "awaiting confirmation" (4.1) |
| C4 | Platform-wording differences classified separately from behaviour differences | Comparison classes (5.2) |
| C5 | The response-ownership boundary holds before Linear v2 | v2 carries names only; nothing it writes is spoken (7) |

## 3. Components

| Component | Location | Kind |
| --- | --- | --- |
| `DefinitionCache` | `apps/api/app/engine/definition_cache.py` | core, product-neutral |
| `ConversationEngine` | `apps/api/app/engine/conversation_engine.py` | core, product-neutral, pure |
| `SignalExtractor` | `apps/api/app/engine/signals.py` | core, driven by the definition's `prospect_signals` |
| Person follow-up rule | `apps/api/app/engine/router.py` | core router addition |
| `ShadowRunner` | `apps/api/app/services/shadow.py` | service: knows the legacy `TurnResponse` |
| `ParityComparator` | `apps/api/app/services/shadow_parity.py` | service |
| Linear v2 | `products/linear_simplified/definition/v2.yaml` | product content |

Core modules keep the existing rule: no product imports, directly or transitively (plan 10.2).

## 4. The engine

### 4.1 `ConversationEngine`: a pure function of its inputs

```text
EngineTurn = ConversationEngine(definition, snapshot, capability_policy, knowledge)
               .turn(message, memory, history, context)
```

- **Inputs, all immutable:** the approved `ProductDefinition` (from the cache, section 4.2), one
  `TurnSnapshot`, a `CapabilityPolicy`, an optional `KnowledgeLookup`, the visitor's message, the
  prior `ConversationMemory`, the prior `SignalHistory` (section 4.4), and `TurnContext` (turn
  number, selected record).
- **Output, immutable:** `EngineTurn` = the route result, the validated action or refusal, the
  composer `Reply`, the legacy translation of the action (for comparison only), this turn's
  signals, retrieved passages, a session summary, the **next** memory and the **next** history.
  Given the same inputs, `turn()` returns the same output; it reads nothing else.
- **No capabilities it could misuse.** The constructor accepts no database connection, no
  `ExecutionLedger`, no model transport and no speech service. There is nothing to switch off
  because nothing is there. A test inspects the signature (`inspect.signature`) and the module's
  imports.
- **Mutations stop before execution.** A proposed mutation is validated and composed as
  `proposed` or `awaiting_confirmation`. The engine never produces `executed`. An affirmation of a
  pending confirmation in the shadow is recorded as "would execute" and composed as the proposal:
  it can never look like a completed write, because in 5a nothing executes.

**Turn order (normative for 5a):**

1. Normalize the message (`Normalizer`, definition vocabulary).
2. Route with `IntentRouter` and the prior memory (precedence as in plan section 3: refusals,
   pending confirmation, pending clarification, exact phrases, intent groups, requirements,
   clarification rules). The router has no model stage, so nothing here can call a provider.
3. If routing proposes an action: validate with `ActionContractValidator` against the snapshot.
   Refusal → `refused`. Mutation → `proposed` or `awaiting_confirmation` per the confirmation rule.
   Navigation and reads → `proposed`.
4. Only if routing reached `FALLBACK`:
   a. platform conversational detection (`conversation.detect`): greeting, named greeting,
      identity, capabilities (offers filtered by the capability policy and the snapshot);
   b. otherwise, knowledge: `ground(knowledge, message)`; a grounded passage is answered with the
      fixed attribution; an ungrounded one with the platform's "no approved information" reply;
   c. otherwise the platform fallback.
   Conversational and knowledge answers come after routing so they can never pre-empt a refusal,
   a pending question or a real product request.
5. Compose every sentence with `ResponseComposer` (revision 4.3: all platform wording).
6. Translate the validated action with the product's registered legacy translator, **for the
   comparison only**. A missing mapping is a recorded `coverage` difference, never a guess.
7. Extract signals (section 4.4) and build the session summary from the next memory and signals.

### 4.2 `DefinitionCache`

- Holds **parsed definition content only**, keyed by `(definition_id, version, checksum)`.
- Every use is preceded by fresh gates against the database, exactly as a live turn runs them:
  product access for the principal, binding state and team, the definition version's lifecycle
  state and recorded checksum, the file's current checksum, and the session pin
  (`check_pinned_session`). The cache is consulted only after all pass, and only for the exact key
  they approved. It never stores approval.
- A failed gate means the shadow records `gated` for that turn and stops; it never falls back to
  an older or different version.
- Bounded: at most 32 entries, least recently used evicted. Content is immutable (frozen models).

### 4.3 Memory: independent, in process, bounded (C1)

- Shadow memory lives in a `ShadowMemoryStore`: an in-process map, never a database table.
  **Nothing the live engine reads or writes is reachable from it**, and the store offers no method
  that takes a connection.
- Key: `(tenant_id, product_id, session_id, visitor_or_user_id, instance_id, instance_generation)`.
  A private demo reset changes the generation, so old shadow memory can never apply to the reset
  instance. Another visitor can never reach the key, because it is built from the authenticated
  principal after the live turn's own ownership check passed.
- Bounded: 5,000 entries, 2-hour idle expiry, least recently used evicted. Each entry holds the
  memory, the signal history and the last compared turn number. A process restart clears it; the
  comparator then records `memory_reset` for the rest of that session instead of a false
  behaviour difference.
- **Ordering without a lock table.** A fixed pool of 64 locks is created at start-up; a key uses
  the lock at `hash(key) % 64`. Nothing is allocated per session, so evicting an entry leaves
  nothing behind, and unrelated sessions that share a stripe only wait for each other's
  sub-millisecond update. The shadow advances an entry only for turns the live engine completed
  or denied for a product reason, and only when the turn number is newer than the entry's last
  compared turn. Stale and cancelled live turns are recorded as `not_compared` and do not
  advance it.
- **Single-process invariant.** In-process memory and the rate limiter both assume one API
  process. The production image starts exactly one uvicorn process (`Dockerfile.api`, no
  `--workers`), and Railway runs one replica. A test fails if the start command gains `--workers`
  or the deployment configuration sets more than one replica. Running more than one process is a
  change that must first move shadow memory and rate limits to a shared store (5c).
- **Why not a table now:** memory holds record identifiers, candidate lists and the normalized
  text of a pending question. Persisting that is a deliberate storage decision (retention,
  identifiers-only form) that belongs to 5c, when the engine becomes authoritative and memory must
  survive restarts. 5a observes; it does not need durability, and must not create a new store of
  visitor text.

### 4.4 Signals, intent trace and session summary

Today these come from Linear-specific code (`intent_extractor.py`, `reasoning_policy.py`,
`session_manager` summaries). Parity on `signals`, `intent_trace` and `session_summary` is the
largest cutover risk named in the parent plan, so 5a builds their generic replacement now, to be
measured before it is ever relied on:

- `SignalExtractor` reads the definition's `prospect_signals` (roles, current tools, goals, pain
  points) and matches literal terms on the normalized message, producing the same `Signal` shape
  and the `intent_trace` role, current tool, goal and pain point fields.
- `current_intent`, `relevant_feature`, `reason`, `confidence` and `status` derive from the route
  result and validation outcome, with fixed platform rules (documented in the code, tested).
- **Accumulation is an explicit input and output, not hidden state.** `SignalHistory` is an
  immutable value: the distinct signals seen so far in the session, as `(type, value)` pairs,
  capped at 50 (oldest dropped first). `turn()` receives the prior history and returns the next
  one. `ConversationMemory` is not changed, because it is a signed-off contract and signals are
  not routing state.
- The session summary is computed inside `turn()` from the next memory (last person, last view,
  pending question) and the next history, so the engine stays a pure function.
- The shadow store (section 4.3) keeps memory and history together in one entry and passes both
  back on the next turn.
- `team_size` (a number parsed from free text) is not generic today; it is compared and reported
  as `coverage` until a definition-level rule exists. No product-specific parsing is added to core.

### 4.5 Router gap closed in 5a: person follow-up

Recorded case `person-follow-up`: after "Show me the issues", the visitor says "what about
Noah". (Revision 2 described the first turn as "all tickets for Maya"; the recording says
otherwise, and the rule covers both.) Rule: when a message names exactly one person and matches no
intent, and the previous turn's request was accepted, the router re-applies that request to the
new person. A person-based request is re-applied as it was; any other request's subject is
filtered through the one person filter the definition declares for that entity (none, or more
than one, means no guess). Unknown or out-of-scope people get the same `unknown_person` answer as
everywhere else. It never applies across a scope change, a refusal, or an expired turn window.

## 5. Shadow runner and comparison

### 5.1 Where it runs

In `create_turn`, after the live engine returns and **only** when `PIXEL_SHADOW_ENGINE=on`
(unset means off — the switch adds work and data, so it defaults off):

1. **Switch off (the default):** nothing changes in the turn path. No snapshot, no copy, no
   shadow object, no lock; a test asserts the shadow module is never entered.
   **Switch on:** `create_turn` loads the caller's records once, as today. Before the live turn
   runs, the product package's lookup converts those same records into one `TurnSnapshot`, whose
   `RecordView`s are immutable. That conversion is the only copy: there is no separate deep copy
   of the record dictionary. The live engine then receives the original records exactly as today.
   Because the snapshot is built before the live turn and is immutable, **both engines see the
   same records, and neither can change what the other saw**. Its size is bounded by the
   per-instance record limit (1,000) for visitors and by the member demo's record count.
2. After the live response is final, the runner builds the engine turn, compares, adds the
   result to in-memory counters (section 5.3), and returns nothing. It performs no database
   write. The live response object is never passed to the engine; the comparator reads it
   through its serialized form (`model_dump`), so it cannot change it.
3. Any exception inside the shadow is caught, logged as `shadow_error` with its class name, and
   counted. It can never change the live status code, body or timing beyond the shadow's own
   run time.
4. Latency: the budget covers **only the in-memory shadow work** — gates, snapshot conversion,
   `turn()` and comparison — measured per turn (p50, p95). It excludes telemetry persistence,
   which never happens on the request path. Budget: p95 below 25 ms on the production image; if
   exceeded, the runner records `over_budget`, and 5a does not exit until it is understood.

### 5.2 Comparison classes (C4)

Every `TurnResponse` field (parent exit criterion 9: `session_id`, `turn_id`, `status`, `speech`,
`proposed_action`, `validated_action`, `execution`, `intent_trace`, `signals`,
`retrieved_context`, `session_summary`) is compared on every compared turn, with one class per
field:

| Class | Meaning |
| --- | --- |
| `match` | Equal after the documented normalization (ordering of signals, whitespace) |
| `lifecycle` | The same change at a different lifecycle point: the live turn returned a mutation action (which the browser then writes) and the shadow proposed it or asked to confirm it, with an identical translated action (see the rule below) |
| `platform_wording` | Only `speech` differs, and the shadow's reply is platform wording from the composer (revision 4.3). Expected almost everywhere; counted on its own |
| `behaviour` | Status, action type, action payload or clarification target differs, beyond the equivalences below |
| `security` | A listed, deliberate difference (for example unknown-equals-inaccessible) |
| `coverage` | The new engine cannot express the legacy result by design in 5a: a model-decided legacy turn, `team_size`, a missing translation |
| `memory_reset` | The shadow lost its memory (restart); later turns in that session are not compared |
| `not_compared` / `gated` / `shadow_error` / `over_budget` | As defined in sections 4 and 5.1 |

`platform_wording` is never merged into `behaviour`: a speech difference caused by a changed
action is `behaviour`, and a speech difference with an identical action and status is
`platform_wording`.

**Status and action equivalence (normative).** The shadow's outcome is first mapped to the live
vocabulary, then compared:

| Shadow outcome | Maps to live `status` | Compared action |
| --- | --- | --- |
| Proposed read or navigation (validated) | `completed` | translated action vs `validated_action` |
| Proposed mutation, or awaiting confirmation of one | `completed` | translated action vs `validated_action` |
| Affirmed pending confirmation ("would execute") | `completed` | the confirmed action, translated, vs `validated_action` |
| Refused (guardrail or validation) | `denied` | none on either side |
| Clarification, answer, knowledge, fallback | `completed` | none on either side |
| Gated | not compared | — |

- **`lifecycle`** applies when the live turn returned a mutation (`CREATE_DEMO_ISSUE` or
  `UPDATE_DEMO_ISSUE`) and the shadow's mapped action has the **same type, same target record and
  identical fields after translation**, while the shadow did not reach the point of execution in
  that turn (it proposed, or asked for confirmation). Any difference in type, target or any field
  value is `behaviour`. Live turns never contain executed state in 5a: the live engine writes
  nothing during a turn, `status` has no "executed" value, and `execution` does not exist until
  5b; a test asserts all three so this rule cannot silently go stale.
- A turn where the shadow **awaits confirmation** and the live engine returned the mutation is
  `lifecycle`, because asking first is the new engine's intended behaviour (parent plan section 11:
  "The backend claims completion before any write").
- The shadow's "would execute" on a later affirmation is compared with that later live turn's
  action only; it is never credited as a match for an earlier live turn.

### 5.3 What is stored (no customer-data copies)

- **Offline (tests):** the golden conversations run through the live engine and the shadow side
  by side. Full values are compared in memory. Every expected difference is listed in
  `products/linear_simplified/tests/golden/reviewed_differences.json` with case, turn, field,
  kind (the classes above) and reason. The test fails on any unlisted difference **and** on any
  listed difference that no longer occurs. Recordings are never regenerated to pass.
- **Live:** aggregate counts only. No message text, no speech, no record values, no session or
  visitor identifiers.

  ```sql
  create table if not exists shadow_parity_daily(
    day text not null,                 -- UTC date, YYYY-MM-DD
    tenant_id text not null,
    product_id text not null,
    definition_id text not null,
    definition_version integer not null,
    field text not null,               -- a TurnResponse field, or "turn" for turn-level classes
    class text not null,               -- a class from section 5.2
    count integer not null check (count >= 0),
    primary key (day, tenant_id, product_id, definition_id, definition_version, field, class)
  );
  ```

  **Writes never happen on the request path.** Each compared turn increments an in-memory
  counter map keyed exactly like the primary key. The map is flushed in **one transaction with one
  batched upsert** (`insert … on conflict (…) do update set count = count + excluded.count`):
  - at most once every 60 seconds, scheduled with FastAPI `BackgroundTasks` so it runs after the
    response has been sent, and only if the map is non-empty and no flush is already running;
  - and once at shutdown, from the application lifespan.

  A flush takes the counts atomically (swap in an empty map), so no increment is lost or counted
  twice between flushes. A failed flush is logged as `shadow_flush_failed` and its counts are
  discarded rather than retried on a later request: telemetry loss is acceptable, request-path
  work is not. A crash loses at most the last minute of counts, which is stated in the report.
  Rows older than 30 days are pruned in bounded batches during a flush.
  `python -m app.ops shadow-report [--days N]` prints the table.
- Per-turn log line `shadow_turn` with the same classes, the shadow's duration and the definition
  version; still no text or values.

## 6. Proof of inertness (tests)

### 6.1 Zero provider calls and zero keys (C2, C3)

- With the shadow on, every golden conversation is run with the HTTP client replaced by one that
  raises, the usage ledger inspected, and the execution ledger inspected: **zero** provider
  attempts and **zero** `action_executions` rows attributable to the shadow, including on turns
  that propose, confirm or cancel a mutation.
- An import-graph test: the shadow modules cannot reach `http_client`, `speech_service`,
  `agent_reasoner`, `model_turn` transports, or `ExecutionLedger.dispatch`.

### 6.2 Zero live writes and an identical live response (C1)

- For every golden conversation, run once with the shadow off and once on. Assert: the live
  `TurnResponse` JSON is byte-identical; the row counts and content digests of `sessions`,
  `messages`, `conversation_owners`, `action_executions`, `provider_attempts`, the member demo
  tables, `demo_instances`, `demo_instance_records` and `demo_instance_receipts` are identical.
- A pending confirmation created by the live engine is unaffected by a shadow "yes", and a shadow
  pending confirmation is invisible to the live engine.
- An exception injected into the engine leaves the live response and the database identical.

### 6.3 Isolation between visitors and products

- Two private visitors in parallel: each shadow sees only its own instance's records; shadow
  memory keys never collide; a reset of one visitor's demo resets only that visitor's shadow
  memory (generation in the key).
- Member and visitor principals on the same product never share shadow memory.

### 6.4 Cache and pinning (parent plan 10.4)

- Warm cache, same engine instance: revoking the version, removing the caller's product access,
  or disabling the product stops the next shadow turn (`gated`); nothing is served from cache.
- A changed definition file or recorded checksum stops the shadow before any work.
- Session A pinned to v1 and session B pinned to v2 each run on their own definition, observable
  through a fixture difference between the versions.

### 6.5 Engine behaviour

- The synthetic, non-project-management definition runs through the engine with no core change.
- Person follow-up: the recorded case, plus unknown, out-of-scope, expired and cross-scope cases.
- Signals: per `prospect_signals` category, plus negatives; no product vocabulary in core.
- Conversational and knowledge stages never pre-empt refusals, pending questions or intents.

### 6.6 Switch and failure behaviour

- `PIXEL_SHADOW_ENGINE` unset, empty, `off` or invalid: the shadow never runs, no snapshot is built
  and the shadow module is never entered (asserted by patching it to raise).
- The shadow never runs for stale, cancelled or gated live turns (recorded as such).

### 6.7 The revision 2 rules

- **Lifecycle equivalence:** a live `UPDATE_DEMO_ISSUE` and a shadow awaiting confirmation of the
  same change are `lifecycle`; the same with a different assignee, target or field is
  `behaviour`; a "would execute" is credited only against its own turn. Live responses in 5a
  have no "executed" status and no `execution` field (asserted).
- **Telemetry:** no database write occurs during a compared request (the connection is patched to
  fail on write inside the request); counts reach the table after a flush; concurrent turns during
  a flush lose or double-count nothing; a failed flush leaves the live response unaffected; the
  upsert adds to existing rows under the primary key.
- **Signals:** `turn()` is deterministic for equal inputs; history accumulates across turns, is
  capped at 50, and resets with the shadow entry on a private demo reset.
- **Locks and process:** evicting 10,000 entries leaves the lock pool at 64 locks; concurrent turns
  in one session advance the entry in turn order only; the start command and deployment
  configuration are checked for a single process.
- **Version move:** on a database bound to v1, the command registers and publishes v2, moves the
  binding, and leaves open v1 sessions working; moving back to v1 works; a breaking or unapproved
  definition is refused with nothing changed; a fresh database seeds straight to v2.

## 7. Linear v2 (C5)

`products/linear_simplified/definition/v2.yaml` is a new immutable file. v1 is not edited.

| Change | Reason (slice-2 comparison note) |
| --- | --- |
| `highlight_assignment` no longer requires a record | `assignment-workflow`: walkthrough should not ask which ticket |
| The add-member rule that names a person gains `unknown_person` | `member-add-named`: prefill the named person |
| GitHub intent made more specific than generic ticket words | `github-explain`: "How does GitHub work with tickets?" |
| `broad_scope_refused` moves from clarifications to guardrails | `scope-broad-request`: cross-workspace requests are refused before intents |
| Stated rule: a person's *tickets* (plural) is the filtered list; a person's *ticket* (singular) opens one. No rule change; the recorded difference is listed as `behaviour` | `scope-platform-own-person`: the note asks v2 to state which the plural means |
| Profile statements ("I'm an engineering manager… moving from Jira") are handled as prospect signals, not navigation; no navigation rule is added, and the difference is listed | `prospect-profile`: no file-order tie-breaking; section 4.4 extracts the role and tool |
| Legacy response bodies removed; `identity` keeps names only (the schema-required greeting field stays, inert) | Revision 4.3: nothing product-written is spoken |

Rules:

- Validated by the full contract and copy rules; registered, validated and published through the
  registry; classified **additive** against v1 by the compatibility checker (no entity changes).
- LF line endings enforced by `.gitattributes`; the readiness check proves new sessions can start.
- **Moving production to v2 is an explicit, reversible operator step, not a side effect of the
  seed.** The seed creates a binding only when none exists, so on an existing database it would
  never register v2 or move the binding. A new command does both, once:
  `python -m app.ops move-product-version --tenant pixel-dev --product linear-demo --version 2`.
  It registers, validates and publishes v2 through the registry (which classifies it against v1
  and refuses a breaking change), then calls `move_product_version`, re-runs the readiness check,
  and prints the result. Moving back is the same command with `--version 1`; v1 stays published.
- The demo organization seed is updated to v2, so a fresh database starts on v2.
- **Existing sessions keep their v1 pin** until they end. Because the live engine reads no
  definition content, visitors see no change.
- The router comparison runs against v1 (unchanged, historical) and v2. On v2, four of the six
  `definition_v2` notes must resolve; the other two are listed as reviewed differences by the
  decisions in the table above.
- The private demo seed pin is unaffected: it is product data, not definition content.

## 8. Rollout

1. Merge with the switch off. Evidence: all suites and Linux CI, including the inertness tests.
2. Deploy; run `move-product-version --version 2` in the Railway console; confirm readiness, the
   smoke test, and that a new session pins v2 while an open one keeps v1.
3. Set `PIXEL_SHADOW_ENGINE=on` on Railway. Confirm with the smoke test that live responses are
   unchanged, then run live traffic.
4. `shadow-report` after real use: review every `behaviour` and `coverage` count against the
   golden reviewed differences. New live-only behaviour differences become golden cases.
5. The switch can be turned off at any time with no data change.

## 9. Exit criteria

1. Inertness proven by section 6.1 and 6.2 tests, and in production: provider attempts and
   execution rows unchanged by the shadow over an observed period (compared with the switch off).
2. Every `TurnResponse` field compared; every golden difference listed with kind and reason;
   `platform_wording` reported separately.
3. Cache, pinning, isolation, switch, failure and revision 2 tests green (sections 6.3 to 6.7).
4. Linear v2 published, additive over v1, pinned by new sessions. Of the six v2 notes, four
   resolve on v2 (`assignment-workflow`, `member-add-named`, `github-explain`,
   `scope-broad-request`); two are listed as reviewed `behaviour` differences by decision
   (`scope-platform-own-person`, `prospect-profile`).
5. Shadow p95 within the 25 ms in-memory budget on the production image (telemetry persistence
   excluded, and proven off the request path).
6. Purity and transitive-dependency tests green; no product vocabulary in core.
7. Green Linux CI, including the existing browser tests, and a reviewed `shadow-report` from
   production.

## 10. Risks and decisions for the review

- **Browser-decided turns are invisible to the shadow.** 41 of 61 golden turns are decided in
  the browser (3.1 finding). The live report covers only turns that reach the backend; the
  offline golden comparison covers the rest. Closing that gap is 3.3's work.
- **Signals and summaries** are the biggest likely difference. 5a measures them; it does not
  promise parity. 5c's exit requires it.
- **In-process memory** means restarts reset shadow comparisons for open sessions. Accepted for
  observation; 5c decides durable memory.
- **Model-decided legacy turns** (whenever `LLM_ENABLED` is on in production) are `coverage` in
  5a, because the shadow never calls a model. The model path's parity is proven by the scripted-model
  suites (parent plan 10.1), not by live shadowing.
- **Review decisions (2026-09-21):** in-process shadow memory conditionally approved (lock and
  single-process conditions now in section 4.3); aggregate storage rejected as written and revised
  (section 5.3); the 25 ms budget approved for in-memory engine work only (section 5.1); binding new
  sessions to v2 approved, subject to strict validation and the LF checks (section 7).
- **Decision requested:** approve revision 2.

## 11. Where the build differs from this plan

Found while building and measuring; each is covered by tests.

1. **Reviewed-difference file.** The shadow's list is `golden/shadow_differences.json`, not
   `golden/reviewed_differences.json`. That file is the live engine's own list against its
   recording, with different kinds (`wording`, `behaviour`, `security`) and its own test; mixing
   the two would corrupt that evidence. Same rules: case, turn, field, kind and reason, and the test
   fails on an unlisted difference and on a listed one that no longer occurs. A reviewer may list
   a comparator `behaviour` as `security`; nothing else may differ from the class found.
2. **The shadow is narrowed to the selected workspace.** The live engine answers inside the
   workspace the request selects, while the grant's records span every workspace the caller may
   use. A shadow built on the grant alone proposed assigning a ticket to someone outside the
   selected workspace. `prepare` now narrows to the request's workspace, the same filter the record
   store applies.
3. **Documents as evidence.** The live engine attaches retrieved documents to every product turn
   except refused proposals and questions back to the visitor. Section 4.1 retrieved only for
   knowledge answers, which would have emptied `retrieved_context` at cutover. The engine now
   retrieves the same way, as evidence that is never spoken.
4. **Feature of a turn.** The feature is the topic of the action's subject when the definition
   names one, otherwise the first topic in the corrected request. Signals read the corrected
   request, so "not cycles, show me the issues" is about issues.
5. **Coverage for undeclared labels.** A live trace value, feature signal or summary interest that
   the definition does not declare (goals such as "Issue lookup" derived from legacy action names,
   the features "Dashboard" and "voice") is `coverage`: no generic engine can produce it.
6. **Gates in one read transaction.** Every gate still runs on every turn, now inside one read
   transaction: one consistent moment and about eight fewer connections per turn.
7. **Linear v2 vocabulary.** Topics and prospect goals also match "plan", "week by week",
   "projects" and "integrations". Entities are unchanged; v2 remains additive over v1.
8. **Named greeting.** "Hi, I'm Priya" is a named greeting; before, only an introduction without
   a leading greeting word was recognized.
