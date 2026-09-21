# Milestone 3, Step 3.2: Generic Conversation Engine — Implementation Plan

Status: **plan, revision 4.3. Slices 1, 2, 3 and 4a are signed off. PR #13 merged with all three Linux CI jobs green (run 35396288897, merge f3cc571). The 2026-09-20 review reopened 4b for capability descriptions, identity/choice prose, and knowledge-title attribution. Corrections are locally verified as of 2026-09-21 and await review and fresh CI evidence; that earlier CI run does not certify these new edits. Slice 5a has not started.**

- Slices 2 and 3 were signed off on 2026-09-17, after three review rounds that reproduced eight defects, all fixed with regression tests, and green Linux CI including browser tests.
- Slice 4a (the model boundary) merged through PR #8. Pull-request run 35363767851 and the `main` push run 35364069464 are green on all three jobs, including browser tests. The stakeholder accepted it on 2026-09-18.
- Slice 4b (the response composer) merged through PR #10, with pull-request run 35380052283 and push run 35380344483 green. The review then found that product-controlled templates were still spoken in answers, refusals and clarifications, and reopened the sign-off. Revision 4.2 records the boundary that closes it (section 8.5); `docs/MILESTONE_3_STEP_3_2_SLICE_4B.md` has the evidence.
- Slice 5 is approved only as a planning structure, not for implementation.

Revision 4.3 (2026-09-20) supersedes the revision 4.2 wording ownership rules.
Every generic-engine sentence is platform-owned, including identity and choice questions.
Capability descriptions derive from the allowed operation and structured targets/fields.
Knowledge titles stay in citation metadata; spoken attribution is fixed. Legacy response bodies
remain in published definitions for compatibility but are never spoken by the new composer.
Visitor identity and demo data isolation are separately planned in
`docs/DEMO_VISITOR_ISOLATION_PLAN.md`, with a review gate before schema implementation.

Earlier history: revision 4 rewrote the model boundary and split the remaining work; revision 4.1 corrected six contradictions found in it. Revision 3.2 recorded the routing rules as built in slice 2 (section 3, stages 5 and 6). Revision 3.3 recorded two decisions from the slice 3 transaction reviews: the execution ledger stores identifiers and outcomes, never raw customer record content, and a replay returns the record's current visible state without a second write (section 5).

Revision 4 corrects the model boundary after review. In summary:

1. Escaping and delimiting are **not** an injection defence. Definition configuration and retrieved records are untrusted model input (section 8.1).
2. The output contract gains strict, numeric parser limits and explicit rejections (section 8.2).
3. Every parameter of a model-proposed mutation must have backend-verified **provenance** (section 8.3).
4. Every model-originated mutation requires confirmation, on a turn that makes no provider call (sections 4.3 and 8.4).
5. One immutable, scope-bound **turn snapshot** is shared by routing, lookup, validation, candidates, prompt and composition (section 7.1).
6. Greetings, identity and capabilities become **platform conversation intents** whose content comes from the pinned definition; the product schema gains no reply-only execution (section 8.5).
7. A read-only **`KnowledgeLookup`** boundary, with honest fallback when no knowledge source is installed (section 7.2).
8. **Only mutations receive execution keys**, carried in a dedicated response envelope (sections 5.5 and 10.1).

Revision 4.1 corrects six contradictions in revision 4:

1. Cutover and cleanup cannot both happen in one slice, so rollback survives 5c and the old engine is removed in a new 5d (section 13).
2. A lazily loaded view is not a point-in-time snapshot. The turn snapshot is materialized in one short read transaction, which is closed before any model call (section 7.1).
3. Provenance does not prove intent. The honest guarantee is stated, and confirmation — not provenance — is what stops an injected mutation whose values the visitor did supply (sections 8.1, 8.3 and 8.4).
4. Response parity covers every actual field, including `proposed_action` and the new `execution` envelope (exit criterion 9).
5. The turn API is backward-compatible through an *additive* envelope, and the translator does not carry the execution key (sections 1 and 9).
6. A capability reply lists only actions that are honestly executable for this caller right now (section 8.5).
Date: 2026-09-17. Design references: `docs/MILESTONE_3_PRODUCT_PROFILE_DESIGN.md` sections 5, 8, 9.1, 10 and 11; `docs/MILESTONE_3_STEP_3_1.md`.

Revision 3 corrects four findings in the execution and confirmation rules:
- execution is atomic, and a commit decides the race between cancellation and execution (section 5.2);
- confirmation has one rule that covers both its sources (sections 3 and 4.3);
- replay has a single contract (section 5.3);
- the limits of keyless writes are stated (section 5.4).

Revision 2 corrected five review findings:
- guardrails now run before any pending state, and corrections never guess (section 3);
- the generic action contract is complete (section 4);
- execution has an explicit owner and lifecycle (section 5);
- cached definitions never carry approval (section 2);
- the dependency check is transitive (section 10.2).

## 1. Goal and boundaries

**Goal:** Edith's backend decisions come from the session's pinned Product Definition, not from Linear-specific code. The turn API remains backward-compatible through an **additive** execution envelope (section 5.5): every existing field keeps its meaning, one optional field is added, and the current web app keeps working.

**In scope:**
- a text normalizer driven by the definition's vocabulary;
- an intent router driven by its intents, clarifications and guardrails, with explicit conversation memory;
- an action-contract validator;
- an execution ledger for dispatched actions;
- a narrow, product-neutral record lookup interface;
- a prompt builder and a response composer;
- the temporary backend translation to today's action names;
- Linear definition v2.

**Out of scope (unchanged owners):**

| Work | Step |
| --- | --- |
| Moving the browser's own decisions into the backend, including the browser's own "Done. I updated…" replies | 3.3 |
| Knowledge store | 3.4 |
| Record store, relationships and the demo-data migration | 3.5 |
| Generic web shell and adapters | 3.6 |

3.2 defines the lookup *interface*, a temporary Linear implementation on today's store, and a thin execution check on today's write endpoints. It does not start the record-store migration.

## 2. Target components

All new core code lives in `apps/api/app/engine/` and must not depend on product-specific modules, directly or indirectly (section 10.2).

| Component | Responsibility | Replaces |
| --- | --- | --- |
| `DefinitionCache` | Caches **parsed definition content only**, keyed by (definition ID, version, checksum) | Direct `PRODUCTS_BY_ID` lookups |
| `Normalizer` | Lower-casing, punctuation and whitespace rules (platform), plus the definition's `vocabulary.corrections` | `language_normalizer.py` |
| `IntentRouter` | Deterministic routing with the precedence in section 3, and parameter extraction through `RecordLookup` | `action_planner.py`, `intent_extractor.py`, `reasoning_policy.py`, `conversation_manager.py`, the boundary checks in `agent.py` |
| `ConversationMemory` | Explicit per-session state: pending clarification, pending confirmation, focused record, last person, last view (section 6) | Signals re-read ad hoc in `agent.py` |
| `ActionContractValidator` | Checks a `GenericAction` against the contract in section 4, the pinned definition, the caller's access and the lookup's scope | `action_validator.py` |
| `ExecutionLedger` | Records dispatched actions and their outcome (section 5) | Nothing today: success is assumed |
| `RecordLookup` (protocol) | Scope-filtered, read-only record and people lookup (section 7) | `demo_data.py` imports in the engine and reasoner |
| `PromptBuilder` | Fixed platform instructions, with product text and data in a delimited section. Keeps the Milestone 2 token limits. | Prompt code in `agent_reasoner.py` |
| `ModelProposalParser` | Strict parsing of the model output (section 8) | Parsing in `agent_reasoner.py` |
| `ResponseComposer` | Platform-owned lifecycle speech from validated actions and committed results; product-defined conversational speech for identity, capabilities and clarifications. Never uses model speech or customer-authored templates to assert an action outcome. | Sentences in `agent.py` |
| `ConversationEngine` | Turn orchestration. Turn, session, cancellation, pinning and accounting semantics are unchanged. | `DemoAgent` internals |

**Cache rule:** the cache holds parsed content, never approval.

Every turn, before the cache is consulted, the platform gates (section 3, stage 0) re-check all of the following against the database:
- the principal's product access;
- the product binding's state and team;
- the definition version's lifecycle state and recorded checksum;
- the file's current checksum;
- the session pin.

A cached entry is used only after all of them pass, and only for the exact (ID, version, checksum) they approved.

**Platform vocabulary additions** (code-owned, additive; v1 stays valid):
- response keys `record_create_proposed`, `record_update_proposed`, `confirm_action` and `action_cancelled`;
- closed lists `AFFIRMATIONS` and `CORRECTION_CUES`.

No definition *schema* change is planned (section 10.4).

## 3. Routing precedence (normative)

The router evaluates these stages in order and stops at the first that decides.

0. **Platform gates.** These run before routing:
   - authorization, session pinning, definition lifecycle and checksum (unchanged from 3.1/3.1a);
   - the cache rule in section 2.

   No definition text can influence them, and they never depend on guardrail wording.
1. **Refusals first.** If any guardrail matches, Edith refuses with its response. Guardrails can only refuse, never permit.
   - A refusal also **discards** any pending clarification or confirmation.
   - Nothing pending is completed on a turn that matches a refusal.
2. **Pending confirmation.** This exists whenever `requires_confirmation` is true for a validated action (section 4.3).
   - It executes only if the whole normalized message is in `AFFIRMATIONS` ("yes", "yes please", "confirm", "go ahead", "do it").
   - Any other message cancels it (`action_cancelled`), and the message is then routed from stage 3.
3. **Pending clarification.**
   - **New request:** if the message matches any intent or clarification rule, the pending clarification is discarded and the message is routed from stage 4.
   - **One candidate named:** if the message identifies **exactly one** remaining candidate (by name, record ID, or ordinal such as "the second one"), the request proceeds with it.
   - **Correction cue** (`CORRECTION_CUES`: "no", "not that one", "the other one", "wrong one"). The cue removes a candidate only when the conversation has singled one out:
     - **Singled out:** Edith's previous reply named exactly one candidate (for example, "Did you mean Maya Chen?"). That candidate is removed, and Edith **asks again** with the rest. She never picks one herself.
     - **Not singled out:** the previous reply listed several candidates. Edith asks which one to exclude, or which one the visitor wants.
   - **Mutations need an explicit yes.** When a correction leaves exactly one candidate for a mutating action, the action is validated with `correction_requires_confirmation`. It then goes through stage 2, and is never executed directly from a correction.
   - **Anything else:** the question is asked again once, then expires.
4. **Exact phrases.** An intent or clarification whose `exact` list contains the whole normalized message wins.
5. **Intent match groups** (with stage 6). Every intent whose every `match` group has a hit, and no `exclude` term, is evaluated together with its requirements.
   - **Specificity**, compared in this order: an exact phrase; the number of match groups; the number of requirements satisfied; the number of distinct matched terms; the length of the longest matched term. Revision 3.2 added "requirements satisfied", so that "Maya's ticket" (open one record) beats "tickets" (open the list) without relying on file order.
   - **A remaining tie never falls back to file order.** When the most specific satisfiable intents propose different things, the first matching clarification rule asks, else the platform `fallback` answers.
6. **Requirements** (`person`, `record`, `selected_record`, `unknown_person`) are resolved through `RecordLookup`, which is always scope-bound.
   - **Satisfied:** exactly one visible match.
   - **Ambiguous** (several visible matches): the router asks with at most three candidates, recorded as pending. It uses the slot's platform question first (`clarify_assign` for people, `clarify_update_target` for records), then the first matching clarification rule.
   - **Missing** (nothing named): the first matching clarification rule asks, else the record question when a record is missing, recorded as pending.
   - **Not visible** (a name was given, but no visible person has it): the most specific satisfiable intent still proceeds, with the `unknown_person` reply; if there is none, `unknown_person` is the answer. Hidden and non-existent people therefore produce identical results.
   - **Precedence:** an unsatisfied intent affects the result only when it is more specific than the best satisfied one. Among unsatisfied intents, ambiguous comes before missing, and missing before not visible.
   - **Not applicable:** an `unknown_person` requirement with no unknown name simply does not apply.
7. **Clarification rules.** If no intent matched, the first matching clarification rule asks its question and records it as pending.
8. **Model path.** Used only when enabled, within budget, and none of the stages above decided. The proposal passes through `ModelProposalParser` and `ActionContractValidator`. Any failure falls back to stage 9. Model output never authorizes anything and never completes a pending state.
9. **Fallback.** The definition's `fallback` response, or the platform default.

**Never guess:** when a decision would need a guess (tied intents, several candidates, a correction with more than one remaining candidate, a missing required field), Edith asks. Mutations always need either a fully specified request or an explicit confirmation.

## 4. Generic action contract (normative)

### 4.1 Shape and capability

```text
GenericAction {
  action_key                          # must exist in the pinned definition
  params: { view?, control?, target?, filter?, fields?, prefill? }
}
target  = { entity, id }              # resolved through RecordLookup before validation passes
filter  = { field, value }
fields  = { name: value }
prefill = { name: value }
```

- **The capability is always derived from `action_key`.** It is never accepted from the model or from any request. A model output containing `capability` is malformed.
- **Internal defence:** the engine's typed `GenericAction` carries the derived capability, and the validator re-checks that it equals the definition's value. That catches router bugs, not model output.

### 4.2 Parameters per capability

Values not listed here are forbidden, and an unknown parameter is always rejected.

| Capability | Required | Optional | Forbidden | Value rules |
| --- | --- | --- | --- | --- |
| `NAVIGATE_VIEW` | `view` | — | `control`, `target`, `filter`, `fields`, `prefill` | `view` equals the action's `view` and is navigable (or a platform view) |
| `OPEN_RECORD` | `target` | — | `view`, `control`, `filter`, `fields`, `prefill` | `target.entity` equals the action's `entity`; the ID resolves to a visible record |
| `FILTER_RECORDS` | `filter` | — | `view`, `control`, `target`, `fields`, `prefill` | `filter.field` equals the action's `by`. The value must be valid for that field's type: a reference resolves to a visible record of the target entity, and an enum value is one of the declared values. |
| `CREATE_RECORD` | `fields` | — | `view`, `control`, `target`, `prefill` | Only names in the action's `fields`, and every required entity field present. Each value passes its field spec (type, enum, maximum length, text safety). References resolve to visible records. |
| `UPDATE_RECORD` | `target`, `fields` (non-empty) | — | `view`, `control`, `filter`, `prefill` | `target` resolves to a visible record of the action's entity. Fields are only names in the action's `fields`, and none is marked `editable: false`. Each value passes its field spec, and references resolve to visible records. |
| `HIGHLIGHT_CONTROL` | `view`, `control` | `target` (required when the action sets `record: true`); `prefill` (only when the action declares `prefill`) | `filter`, `fields` | `view` and `control` equal the action's values. `target` follows the `OPEN_RECORD` rules. `prefill` names are a subset of the action's `prefill` and pass the field specs. |

### 4.3 Confirmation

```text
requires_confirmation = action.confirm                      # declared in the definition
                        OR correction_requires_confirmation  # a mutating action whose target
                                                             # came from a correction (section 3)
                        OR model_originated                  # the model proposed it (revision 4)
confirmation_reason   = "definition" | "correction" | "model_originated"
```

A definition author may lower friction on the deterministic path, but **may not waive
confirmation for a mutation a model proposed**: `confirm: false` does not apply to
`model_originated`. `ConfirmationReason` gains `MODEL_ORIGINATED`.

- **Actions needing confirmation** are validated in full, then **not dispatched**. The engine stores them as a pending confirmation, with their resolved parameters and the reason, and asks `confirm_action`. The execution key is created only at dispatch.
- **On an explicit affirmation** (section 3, stage 2), the platform builds a **fresh** turn snapshot, re-resolves and re-validates every parameter, and only then dispatches. Any other message cancels it. The confirmation turn itself makes **no provider call**: the question comes from a deterministic template naming the exact target and changes.
- **Cancellation paths:** a pending confirmation expires after one turn and is discarded by a refusal, a scope change or turn cancellation.
- **Test coverage:**
  - Linear v1 sets `confirm` on no action, so the recording is unaffected. The definition path is tested with the synthetic fixture's `update_contact` (`confirm: true`).
  - The correction path is tested with a `confirm: false` mutation (Linear's `update_issue`, and a synthetic action with `confirm: false`).

## 5. Execution boundary and lifecycle

**Who executes today:**
1. The backend returns a validated action.
2. The browser executor applies it.
3. For creates and updates, the browser writes through the backend record endpoints (`/api/demo-data/issues`, and so on) and updates the screen only after the write succeeds.

This boundary stays in 3.2. The browser remains the executor, and the backend write endpoints remain the only place a mutation happens.

**Lifecycle:**

| State | Owner | Meaning |
| --- | --- | --- |
| `proposed` | Router or model | A candidate action, with no authority |
| `validated` | `ActionContractValidator` | The contract, scope and access checks passed |
| `awaiting_confirmation` | Engine | A `confirm` action waiting for an explicit yes |
| `dispatched` | Engine and `ExecutionLedger` | Sent to the client with an **execution key** (unique per session, turn and action) |
| `executed` | Write endpoint and `ExecutionLedger` | The backend write committed under that key |
| `failed` | Write endpoint and `ExecutionLedger` | The write was rejected (validation, scope, conflict, not found) |
| `cancelled` | Engine and `ExecutionLedger` | The turn was cancelled or superseded before execution |

### 5.1 Temporary execution check
This runs on today's write endpoints until 3.5, for writes that carry an execution key.

- **Dispatch.** The engine creates the key, a random value bound to the session owner, organization, product, session, turn, action, target and field values. It stores the key as `dispatched`, with an expiry of 10 minutes.
- **Execution.** A keyed write is accepted only when every check in section 5.2 passes in the same transaction.

### 5.2 Atomic execution (normative)
One keyed write is **one database transaction** (`begin immediate`). The following happen inside that transaction, in this order, or not at all:

1. **Re-check authorization now:**
   - the caller's membership and product access, and the record grant and scope;
   - the session pin and the definition lifecycle, which must still be usable as at a new turn. Being usable at dispatch time is not enough.
2. **Load the key and apply the replay contract** (section 5.3).
3. **Check the request.** The request must equal the bound action, target and field values.
4. **Perform the record mutation.**
5. **Store the outcome** on the key row: `executed` with the changed record's ID and a result code, or `failed` with its reason. The ledger stores identifiers only — never a copy of the record, the submitted field values or the request body, of which it keeps a digest (stakeholder decision, slice 3 review). The digest is derived data, not a secret.

**One mechanism only.** An assistant-originated write is made idempotent by its execution key and by nothing else. A keyed request that also carries the legacy retry header is refused, because a second mechanism could report an older receipt as this action's outcome.

**No request creates reference data.** Demo records are seeded by the deployment's startup, behind the explicit seed switch. Authorization and the key claim therefore always precede any change, and a refused request leaves product data exactly as it found it.

**What the transaction guarantees:**
- **Concurrency.** SQLite serializes writers, so concurrent requests with the same key produce **exactly one** mutation. Every other request sees the stored outcome.
- **Cancellation versus execution: whichever commits first wins.**
  - **Cancellation first:** a turn's cancellation or supersession marks its `dispatched` keys `cancelled` in its own transaction, and a later write is refused.
  - **Execution first:** the outcome stays `executed`. Cancellation changes only keys that are still `dispatched`. It never relabels or undoes an executed or failed key, and the cancel response says the action already ran.
- **Two kinds of failure:**
  - **Rule rejection:** the request is authorized, but the record change is refused by a rule (validation, scope, not found, conflict). No mutation happens, but the transaction **commits** a `failed` outcome with its reason.
  - **Unexpected error:** anything else (database error, crash, bug). The transaction **rolls back** the mutation and the outcome together, and the key stays `dispatched`.
- **Failed authorization writes nothing.** If step 1 fails, or the key belongs to another owner, organization or product, the transaction ends without touching the ledger. A caller can never alter a ledger entry that is not theirs.

### 5.3 Replay contract (normative)

| Key state | Request | Result |
| --- | --- | --- |
| `dispatched`, not expired | Same owner, identical request | Execute (section 5.2) |
| `executed` | Same owner, identical request | Same execution attempt, no second write; return the record's current visible state. If it is gone or no longer visible to them, refuse with `execution_result_unavailable` |
| `failed` (rule rejection, committed) | Same owner, identical request | Return the stored failure; no retry under this key. A retry needs a new turn and a new key. |
| `dispatched` after a rolled-back attempt (unexpected error, nothing committed) | Same owner, identical request, not expired | Retry allowed |
| any state | Different parameters, action or target | `409`, nothing written |
| any state | Different owner, organization or product | Refused as not found |
| `cancelled`, unknown, or `dispatched` and expired | any | Refused, nothing written |

**Expiry and authorization for replay:**
- **Expiry only stops unused keys.** It prevents an unused `dispatched` key from executing. It never erases, relabels or re-executes a committed `executed` or `failed` outcome.
- **Replay needs current authorization.** A replay still requires the step 1 checks to pass now, and it reads the record through the caller's present access. A caller who has lost access gets a refusal, never content.

**Durability:** the outcome is stored in the same row, and committed with the mutation. Replay protection therefore survives restarts.

### 5.4 Stated limitation: keyless writes
- **Keyed writes only.** The execution ledger protects **assistant-originated** writes that carry a key. The product's own forms still write without a key, through today's endpoints, under the 3.1a record-access rules alone.
- **Not a universal boundary.** A client that omits the key falls back to that manual path. Assistant confirmation is therefore **not** a universal security boundary in 3.2: it guarantees what Edith executes, not what every client may write.
- **Acceptable temporarily.** Manual-write authorization independently permits exactly the same operations. The record store in 3.5 decides whether keyless writes remain.
- **Browser test.** A browser test proves that every assistant-originated mutation sends its execution key (section 10.6).

**Honest wording:**
- **Before the write:** for a dispatched mutation, the backend reply uses `record_create_proposed` or `record_update_proposed` ("I'll update LIN-142: assignee to Noah Patel.").
- **After the write:** completion wording (`record_created`, `record_updated`) is used only once the ledger shows `executed`, for example on a following "what changed?" turn.
- **Reproduced by the recording:** today's backend already says "Done. I updated…" and "I created…" before any write happens. This is corrected and listed in section 11 (five cases).
- **Client wording:** the client receipt reports success only after the write returns, as it does today.

### 5.5 Execution transport

A mutation's execution key travels in its own envelope on the turn response, never inside an
action payload:

```json
{
  "validated_action": { "type": "UPDATE_DEMO_ISSUE", "payload": {} },
  "execution": { "key": "...", "session_id": "...", "turn_id": 7, "expires_at": "..." }
}
```

- `execution` is **null** for every non-mutating action.
- `expires_at` is **advisory** information for the interface. The server remains authoritative.
- `turn_id` is for client correlation, **not** authorization.
- The key stays bound server-side to its session and turn. Starting or cancelling a newer turn
  cancels older unresolved mutation keys.
- The browser is never trusted to decide whether a key is still usable.

## 6. Conversation memory

| State | Set by | Survives | Cleared by |
| --- | --- | --- | --- |
| Pending clarification: key, expected slot, remaining candidates, rejected candidates, turn number | Stages 6 and 7 | One following turn | Resolution; any routed intent or refusal; expiry; scope change; turn cancellation |
| Pending confirmation: action, resolved parameters, confirmation reason, turn number | Section 4.3 | One following turn | Affirmation (then dispatch); any other message; refusal; scope change; turn cancellation |
| Focused record (entity, ID) | A validated action on a record | Navigation to other views | A new focus; scope change; the lookup no longer returning it for this caller |
| Last person | A resolved person requirement | Navigation | A new person; scope change |
| Last view | A validated navigation | Everything except scope change | A new navigation |
| Last change | An `executed` ledger entry | The session | A newer executed change |

**Memory is never trusted for authorization.** Every remembered reference is re-resolved through `RecordLookup` on the turn that uses it.

## 7. Record lookup boundary

```text
RecordLookup (protocol, read-only, scope-bound at construction):
  get(entity, record_id) -> RecordView | None
  search(entity, text, limit) -> list[RecordView]
  by_person(entity, person_ref, limit) -> list[RecordView]
  people(text, limit) -> PeopleMatch          # visible matches only
  count(entity) -> int
```

- **Scope is fixed when the lookup is built.** It comes from the authenticated principal, the product binding and the caller's record grant. Callers cannot pass another scope, organization or product.
- **Unknown and inaccessible look the same.** A person or record that exists only outside the caller's visible scope is reported exactly like one that doesn't exist.
  - The engine gets no signal that could reveal it.
  - Today's replies ("Avery Brooks is outside Product Engineering Workspace…") confirm that such people exist. 3.2 corrects this as a **security difference** (section 11).
- **Temporary implementation.** `LinearLegacyLookup` in the Linear product package reads today's store until 3.5. Authorization stays server-enforced through the 3.1a record grants.
- **Registration** follows section 9.

### 7.1 The turn snapshot

Every read a turn makes comes from **one immutable, scope-bound snapshot**, built when the turn
starts: routing, lookup, validation, clarification candidates, prompt construction and
composition all read the same one. Two reads within a turn can therefore never disagree, and a
clarification list cannot offer a record that validation then rejects.

- **Materialized in one read transaction.** The bounded data the turn needs is read inside a
  single short SQLite read transaction, which is then **closed**. Loading each entity on first
  access would not be a snapshot at all: projects read at T1 and issues read at T2 can disagree
  with each other, which is exactly the inconsistency this section exists to remove.
- **No transaction is ever held open across a model call.** The snapshot is materialized and the
  transaction closed before the prompt is built.
- **Bounded by scope.** What is materialized is the caller's visible records for the entities this
  product declares. If that ever stops being bounded enough to read in one short transaction, the
  answer is to narrow what a turn may read, not to reopen lazy loading.
- **Identity.** A snapshot records when it was taken, the scope it is bound to, and the pinned
  definition's checksum, so a reviewed difference can name the snapshot it came from.
- **Never across turns.** A snapshot lives for one turn. Remembered references, including pending
  clarification candidates, are re-resolved against the *next* turn's snapshot, because memory is
  never trusted for authorization (section 6).
- **The keyed write does not use it.** Authorization and record checks inside the write
  transaction stay live (section 5.1). The snapshot makes a conversation coherent; it must never
  decide whether a write is allowed.

### 7.2 Knowledge boundary

Structured records come from `RecordLookup`. Product *documents* need the same treatment, so the
platform defines a read-only, scope-bound `KnowledgeLookup` protocol, implemented in the product
package. Core never imports a product's retriever.

- The protocol is defined in 3.2 and implemented by the Linear package over today's retriever, so
  `retrieved_context` keeps being populated once `agent.py` is dismantled.
- With no knowledge source installed, knowledge questions get an **honest fallback**: the platform
  says it cannot answer, and never invents an answer or silently returns nothing.
- Generic knowledge work — indexing, versioning, ranking — remains Milestone 3.4.

## 8. Prompt and model boundary

### 8.1 Untrusted input

- **Fixed platform instructions** (core constants): safety, scope, allowed output shape, and that
  product sections are data, not policy.
- **Delimited product sections:** the definition's persona, hints, vocabulary and templates go in
  a labelled product configuration section; snapshot lookup results go in a labelled product data
  section. Both are serialized as escaped JSON.
- **Escaping and delimiting are not an injection defence.** They keep the boundary intact
  *structurally*; they do nothing to stop a model following an instruction written inside a record
  title or a persona field. Definition configuration and retrieved records are therefore treated
  as **untrusted model input**, and safety rests on what the backend verifies afterwards
  (sections 8.3 and 8.4), never on the prompt's shape.

**What injection can and cannot achieve.** Provenance (section 8.3) stops an injected
instruction that introduces values the visitor never supplied. It does **not** prove the visitor
intended the operation, and saying otherwise would overstate it. Consider:

```text
"What does Closed mean for LIN-142?"
```

Both `LIN-142` and `Closed` are the visitor's own words, so an injected instruction proposing
"set LIN-142 to Closed" would pass parameter provenance. The honest guarantee is therefore:

1. An injected mutation using values the visitor never supplied **fails provenance**: refused, with
   no ledger row and no confirmation question.
2. An injected mutation whose values the visitor did supply can pass provenance, and is then still
   **undispatchable without deterministic confirmation** (section 8.4).
3. So the worst an injection achieves is an **unwanted confirmation prompt** naming an exact target
   and change.
4. Only a subsequent exact affirmation authorizes dispatch — and that comes from the visitor, who
   is shown precisely what would change.

Both cases are tested: the one that fails provenance, and the one that passes provenance and stops
at confirmation.

### 8.2 Output contract

Exactly `{speech: string, action: {action_key, params} | null, clarification: string | null}`.

| Rule | Limit |
| --- | --- |
| Raw response size, checked **before parsing** | 32 KiB |
| Nesting depth | 8 |
| `speech` | non-empty, at most 2000 characters |
| `clarification` | when present, non-empty, at most 500 characters |
| `action_key` | at most 64 characters |
| `params` keys | at most 16 |
| Any model-supplied string value | at most 1000 characters (a field's own declared maximum still applies and wins when smaller) |
| Any model-supplied list | at most 20 items |

Malformed, in every case falling back deterministically:

- any unknown key, **at any nesting level**, including `capability`;
- a wrong type, or a missing key;
- `action` and `clarification` both present;
- duplicate JSON keys (detected while parsing, not after);
- `NaN`, `Infinity` or `-Infinity`;
- trailing text after the JSON value;
- Markdown code fences. These are **rejected, not stripped**: stripping would quietly weaken the
  contract. A fenced response is recorded under its own malformed reason, so routine model
  behaviour can be told apart from an attack and decided on deliberately.

`params` must also satisfy section 4. Model speech never contains completion wording for its own
proposed action; the composer replaces it with the proposed-action template.

### 8.3 Parameter provenance

A model may **suggest** values. It may never declare where they came from. For every parameter of
a model-proposed mutation, the backend verifies provenance itself against exactly these sources:

| Provenance | Meaning |
| --- | --- |
| `USER_EXPLICIT` | Directly present in the current message |
| `USER_RESOLVED` | Resolved by the scope-bound lookup from something the visitor explicitly named ("Maya's ticket" → a record ID) |
| `PENDING_STATE` | Carried from a prior clarification and re-resolved this turn |
| `DEFINITION_DEFAULT` | Supplied by the pinned definition |
| `DEFINITION_MAPPING` | A deterministic vocabulary mapping, such as "urgent" → `High` |

A value need **not** appear literally in the visitor's text: "open Maya's ticket" never says
`LIN-142`, and that request is legitimate. What matters is that the backend can attribute every
parameter to one of these sources.

If any mutation parameter lacks valid provenance, the platform: **refuses the proposal, dispatches
nothing, creates no ledger row, and does not ask for confirmation.**

**Provenance is a data check, not an intent check.** It establishes that every value came from a
verifiable source; it cannot establish that the visitor wanted the operation performed. Intent is
established only by the confirmation in section 8.4 (see also section 8.1).

### 8.4 Mutation authorization

Provenance shows where a value came from; it does not show that the visitor intended the
operation. So for every model-originated mutation, in order:

1. Validate provenance (section 8.3).
2. Validate the action against the current snapshot (section 4).
3. Ask for confirmation using a deterministic template containing the exact target and changes.
4. Make **no provider call** on the confirmation turn.
5. On an exact affirmation, build a **fresh** snapshot.
6. Re-resolve and re-validate everything.
7. Dispatch the execution key only then.

### 8.5 Platform conversation intents

Greetings, identity and capability questions are recognised by the **platform**, not by
product-declared reply-only intents: the contract still requires every intent to name an action,
and revision 4 does not add reply-only execution to the product schema.

- Detection is generic and lives in core.
- **Who owns the words (revision 4.3).** Core supplies every spoken sentence. The pinned
  definition supplies validated product/assistant/entity/view names, inserted as names.
  Product response bodies (including greetings and choice questions), persona prose and action
  descriptions never become spoken assertions. Lexical copy validation remains a compatibility
  check on definitions, not proof that their prose is true.
- **Capability wording follows its contract.** Each offered action gets a platform verb and
  structured entity/view/control/field names. Swapping a navigation description for
  "Refund payments" must not change a capability or guided-path reply.
- **Knowledge attribution is fixed.** Speak a checked excerpt with platform-owned attribution.
  Keep the checked document title in source metadata, never in spoken attribution. Only the
  passage actually quoted is cited. Refuse malformed or oversized excerpts and quote breakouts.
- **A capability reply lists only what is honestly executable now.** Generating it from every
  declared action would advertise things that cannot happen. An action is listed only when it is:
  permitted for this product and this caller; supported by the installed adapter; available under
  the caller's current scope; and honestly executable in this version. `create_member` is the
  worked example — declared by the definition, but untranslatable today (section 9), so it is
  never offered.
- The platform may ask a generic clarification when no verified choice list is available. It
  must not invent available operations from a product's clarification prose.

### 8.6 Unchanged

The Milestone 2 token limits, reserve-before-dispatch, settle on every outcome, and cancellation.

## 9. Temporary legacy action translation

**Decision:** the translation lives in the product package, with an explicit code-owned registration.

- **One registration point.** `apps/api/app/installed_products.py` is the only core file allowed to import product packages, like Django's `INSTALLED_APPS`. It lists them explicitly, and both the purity check and the dependency check (section 10.2) exempt only this file.
- **Keys and failure.** Registration is keyed by definition ID. A missing adapter fails the turn closed.
- **No executable code from definitions.** Nothing is ever imported from a path or name supplied by a definition.
- **`LinearLegacyTranslator`** (`products/linear_simplified/backend/`):
  - translates only `validated` actions, returning **action type and payload only**. It never
    carries an execution key: the orchestration layer attaches the separate execution envelope
    (section 5.5), so a key can never be hidden inside an action payload;
  - raises `TranslationMissing` for an unmapped action (no guessing), and the turn is refused;
  - contains no intent decisions and no permission logic;
  - has one test per supported mapping, plus one for a missing mapping;
  - is removed in 3.6.
- **Architecture change:** the design's section 9.1 planned a *web* shim from 3.3. It is replaced by this single *backend* translator, from 3.2 until 3.6, so there is never a second translator. The design table is updated.

## 10. Tests required before sign-off

### 10.1 Scripted-model tests (no paid calls)
All use the existing fake-transport pattern:
- **Valid proposals:** one per capability, validated and translated. **Only mutating
  capabilities are dispatched** and receive an execution key; navigation and highlighting are
  validated, translated and returned with `execution: null`, because they have no settlement
  endpoint and a key for them would sit `dispatched` until expiry.
- **Invalid proposals:** unknown action key; a `capability` key in the output; an unexpected parameter; a forbidden parameter for the capability; a field outside the action's list; a non-editable field; an enum value that isn't declared; a reference to an invisible record; missing required fields; an update without a resolved target. Each is refused, and nothing is dispatched.
- **Parser limits (section 8.2):** oversize raw response, excessive nesting, empty `speech`,
  over-long `speech` and `clarification`, `action` and `clarification` together, an empty
  `clarification`, too many `params`, an over-long string value, an over-long list, an unknown key
  at a nested level, duplicate JSON keys, `NaN`, trailing text, and a Markdown-fenced response.
  Each is malformed, each falls back deterministically, and the fenced case records its own reason.
- **Provenance (section 8.3):** a mutation parameter with no valid provenance is refused with no
  ledger row and no confirmation question. One test per provenance class proves the legitimate
  paths still work, including `USER_RESOLVED` ("open Maya's ticket") and `DEFINITION_MAPPING`
  ("make it urgent").
- **Model mutation confirmation (section 8.4):** a model-proposed mutation on an action with
  `confirm: false` still asks for confirmation, with reason `model_originated`; the confirmation
  turn makes **zero** provider calls; an affirmation re-validates against a fresh snapshot before
  dispatch; and a record that became invisible between the two turns is refused rather than
  dispatched.
- **Failure modes:** malformed JSON, missing keys, timeout, exhausted budget. Each falls back
  deterministically, and the ledger status is correct.
- **Cross-product reference:** a proposal naming another product's record or entity is refused. The lookup never returns it.
- **Injected instructions:** product configuration or record text containing instructions that
  conflict with the platform rules. The instructions stay inside the data section, and **both**
  outcomes from section 8.1 are tested:
  - an injected mutation using values the visitor never supplied **fails provenance** — nothing
    dispatched, no ledger row, no confirmation question;
  - an injected mutation whose values the visitor *did* supply (the "what does Closed mean for
    LIN-142?" case) passes provenance and **stops at confirmation** — nothing dispatched, and the
    prompt names the exact target and change, so a visitor who did not ask for it can decline.
- **Turn snapshot (section 7.1):** one turn's routing, candidates, validation, prompt and reply all
  read the same snapshot; a record changed mid-turn does not change the turn's own answers; and the
  next turn re-resolves everything, including remembered candidates.
- **Knowledge fallback (section 7.2):** with no knowledge source installed, a knowledge question
  gets the honest fallback and no invented answer.
- **No completion claims:** model speech saying "Done, I updated it" for a proposal is replaced by the proposed-action wording.
- **Existing tests unchanged:** accounting and cancellation tests stay green.

### 10.2 Architecture tests
- **Purity allowlist:** the engine files leave it in slice 5d, once the old engine and the rollback switch are removed (section 12).
- **Transitive dependency boundary (new):**
  - **The check:** it parses imports with `ast` and follows every `app.*` module reachable from `app/engine/*`, and from any module those import.
  - **It fails** if the graph reaches `app.services.demo_data`, `app.services.product_data_store`, `app.workspace_config`, `app.product_config` or any `products.*` module. The only exemption is `app/installed_products.py`, and only as the registry's entry point.
  - **What it can't see:** dynamic imports (`importlib`, `__import__`). These are banned under `app/engine/` by the same test.
  - **Review:** each pull request in this step also lists the engine's direct dependencies for review.

### 10.3 Synthetic-definition proof
- **Fixture:** the neutral `sample_desk` definition (accounts, contacts, notes), with an in-memory `RecordLookup`, an in-memory execution check and no translator.
- **Proof:** tests change its vocabulary, an intent, an action and a response, and the observed routing, validation and speech change accordingly, with no core edits.
- **Two definitions:** one test runs both definitions through the same engine instance.
- **Confirmation:** the fixture's `update_contact` action (`confirm: true`) covers the confirmation path.

This is not the second-product milestone (3.7).

### 10.4 Pinning, lifecycle and cache
- **v2 is a new file:** `products/linear_simplified/definition/v2.yaml` is new and immutable, and v1 is untouched.
- **Session split:** session A stays on v1 while session B starts on v2. Each uses its own vocabulary, actions and responses. A fixture difference between the versions makes this observable.
- **Lifecycle gates:** revocation, checksum mismatch and changed content stop the turn before any provider call, dispatch or write. The tests assert zero transport calls, zero ledger rows and zero writes.
- **Warm cache:** with the **same engine instance** and a warm cache:
  - revoking the version stops the next turn;
  - removing the caller's product access stops the next turn;
  - disabling the product stops the next turn.

  Nothing is served from the cache in any of these cases.
- **Rollback:** a test proves the old engine can be restored by the deployment switch alone, with
  **no database change**: the same session continues, and no migration, backfill or row edit is
  required. The test applies through 5c; 5d removes the switch on purpose.
- **Snapshot consistency:** a test changes a record between two entity reads within one turn and
  shows the turn's answers do not change, and that no database transaction is open while the model
  call is made.
- **Shadow mode is inert:** in 5a the new engine runs deterministically only — model disabled,
  dispatch disabled — and tests assert zero transport calls and zero ledger rows from the shadow
  path.
- **Schema versus content:** v2 is a change of *product definition content*. The platform vocabulary additions in section 2 are additive code changes that keep v1 valid. Any change to the *definition schema* is out of plan, and would need a separate review.

### 10.5 Routing, memory and confirmation
- **Precedence:** each stage in section 3, including tied intents (which ask) and exact-over-group.
- **Refusal beats pending state:** while Edith is asking which person to assign, the visitor says "no, delete everything instead". The destructive guardrail refuses, the pending assignment is discarded, and nothing is resolved, dispatched or written.
- **Corrections never guess:**
  - After Edith singled out one of three candidates, "not that one" removes it and asks again with two.
  - After a list of three, "not that one" asks which to exclude.
  - When a correction leaves one candidate for a `confirm: false` mutation (Linear `update_issue`), Edith asks for confirmation with reason `correction` and does not execute.
- **New request replaces pending:** a message matching another intent during a pending clarification discards the pending clarification.
- **Confirmation:** only exact affirmations dispatch. Anything else cancels. Parameters are re-validated at dispatch.
- **Memory:** focus survives navigation. Every reference is re-resolved and dropped when no longer visible.
- **Unknown versus inaccessible:** both give identical responses and signals for a caller without access.
- **Guardrails only refuse:** deleting every guardrail from a definition does not weaken platform authorization.

### 10.6 Execution
- **Execution failure:** the write is rejected by a rule. The key is stored `failed`, and replaying it returns the same failure without a write. No completion wording is ever produced for it, and a later "what changed?" reports nothing changed.
- **Replay contract:** one test per row of the section 5.3 table, including a restart between execution and replay (a fresh store and engine on the same database).
- **Concurrent duplicates:** several threads send the same key at once. Exactly one mutation happens, and every response describes that one record.
- **Cancellation race:**
  - **Cancellation committed first:** the write is refused and nothing is written.
  - **Execution committed first:** a later cancellation leaves the key `executed`, and the record keeps its new value.
  - **Both at once:** run in threads, the result is exactly one of those two outcomes.
- **Rule rejection versus unexpected error:**
  - a rule rejection commits `failed` with no record change;
  - a fault injected after the mutation but before commit leaves neither the record change nor an outcome, and the key stays `dispatched` and retryable.
- **No foreign ledger changes:** a caller from another owner, organization or product, or one whose access was revoked, gets a refusal. That caller's attempt leaves the ledger row unchanged, whatever its state.
- **Expiry never touches outcomes:** after expiry, an `executed` or `failed` key still replays its stored outcome to an authorized caller and never executes again. An unused `dispatched` key is refused.
- **Replay needs current access:** after revoking access, replaying an `executed` key is refused.
- **Checks at execution time:** after dispatch, revoke the definition, disable the product, suspend the organization, or remove the record grant. In each case the keyed write is refused and nothing is written.
- **Expiry:** an expired key is refused.
- **Manual writes:** form writes without a key behave exactly as today (existing tests).
- **Browser:**
  - intercepted write requests prove that every assistant-originated create and update carries its execution key;
  - form-originated writes carry none;
  - the existing failed-save tests stay green.

## 11. Golden parity rules

- **Exact comparison** of every recorded field against `backend_decisions.json`. The recording is never regenerated to make tests pass.
- **Reviewed differences** live in `products/linear_simplified/tests/golden/reviewed_differences.json`.
  - Each entry has: case, turn, field, recorded value, new value, kind (`wording`, `behaviour` or `security`), and reason.
  - The test fails on any unlisted difference **and** on any listed difference that no longer occurs.
- **Defects are not preserved for parity.** Each is corrected, listed, and gets its own regression test. Known so far:

| Kind | Defect | Recorded cases |
| --- | --- | --- |
| `security` | A reply confirms that a person exists outside the caller's scope | `update-outside-person`, `scope-outside-person`, `scope-platform-outside-person` |
| `behaviour` | The backend claims completion before any write | `update-reassign`, `update-priority`, `update-status` ("Done. I updated…"); `create-known-owner`, `create-open-and-assign` ("I created…") |

  **Likely also affected:** `update-unknown-person`, where an unknown person (Priya) is answered with "That work is outside…". Under the unknown-equals-inaccessible rule it should get the `unknown_person` response. If it changes, it is listed with that reason.

  Any further differences found by the slice 2 shadow harness are added case by case.
- **Deterministic path only.** The recording exercises the deterministic path (the LLM is off). The model path is proven by section 10.1, not by the recording.

## 12. Purity allowlist changes

These removals land in **slice 5d**, not at cutover: while the rollback switch exists, the old
engine is still imported and still has to be listed. Removing it from the allowlist is part of the
same deliberate act as removing the engine itself.

Expected removals:
- `action_planner.py`
- `action_validator.py`
- `agent.py`
- `agent_reasoner.py`
- `conversation_manager.py`
- `intent_extractor.py`
- `language_normalizer.py`
- `product_config.py`

Files deleted outright are removed from the list too. `retriever.py` (3.4) and the record modules (3.5) stay. `main.py` and `product_data_store.py` stay listed, although they gain the execution check.

## 13. Delivery slices

Each slice leaves every suite green and is reviewable on its own.

| Slice | Contents | Exit evidence |
| --- | --- | --- |
| **1. Contracts and fixtures** | Engine types (`GenericAction` with the section 4 rules, lifecycle states, memory model); `RecordLookup` protocol; `installed_products.py`; platform vocabulary additions; synthetic engine fixture; golden difference harness; transitive dependency test | New tests green; no behaviour change |
| **2. Normalizer and router** | `Normalizer`, `IntentRouter`, `ConversationMemory` with the section 3 precedence; a shadow harness compares router decisions with the current engine on every golden case | Section 10.5 routing tests; shadow differences listed |
| **3. Validator, lookup, execution and translator** (mandatory transaction review before slice 4) | `ActionContractValidator`; `ExecutionLedger` plus the execution check on today's write endpoints; `LinearLegacyLookup`; `LinearLegacyTranslator` | Section 4 validator tests; section 10.6 execution tests; lookup-scope tests; per-mapping translator tests |
| **4a. Model boundary** | `PromptBuilder`; the strict `ModelProposalParser` (section 8.2); the immutable `TurnSnapshot` (section 7.1); provenance verification (section 8.3); forced confirmation for model-originated mutations (section 8.4). No runtime wiring | Section 10.1 parser, provenance, injection, confirmation and snapshot tests; adversarial injection proposing a valid in-scope mutation |
| **4b. Response composer** | `ResponseComposer` as a response-lifecycle state machine; platform-owned proposed / confirmed / executed / failed / cancelled wording; platform conversation intents (section 8.5); **the response-integrity boundary (revision 4.3)**: every sentence platform-owned, capability wording derived from contracts, legacy product prose inert, fixed knowledge attribution with separate source titles | Wording tests per lifecycle state; no unsupported completion claims; `KnowledgeLookup` honest fallback and source evidence; **every product template replaced with adversarial wording changes no protected reply** |
| **5a. Backend integration, shadow mode** (starts after 4b sign-off) | `ConversationEngine` adapter; `DefinitionCache` keyed by definition ID, version **and** checksum, with fresh authorization and lifecycle checks before every cache use; Linear v2, written against the revision 4.3 response boundary. The current engine stays authoritative. **Mandatory:** shadow memory is independent and cannot mutate live sessions, records, ledgers or pending confirmations | Structured parity report, with platform-wording differences classified separately from behaviour differences; section 10.4 pinning and cache tests; shadow path proven inert: **zero paid calls and zero execution keys**, no model, no dispatch |
| **5b. Execution transport** | The execution envelope in `TurnResponse` (section 5.5); keys for mutations only; the browser sends both required headers; form-originated writes stay keyless | Stale-turn and interruption races; older unresolved keys cancelled by a newer turn; browser writes never triggered by failed, cancelled or stale turns |
| **5c. Cutover** | The new engine becomes authoritative behind a rollback switch that defaults to the old engine. The old engine **stays in place**, isolated behind that switch: a cutover you cannot reverse is not a cutover | Golden parity with reviewed differences; scripted-model suites; **explicit parity coverage for every `TurnResponse` field** (exit criterion 9); rollback test proving the old engine is restored by the switch alone with no database change; full API, web, browser and Linux CI evidence |
| **5d. Cleanup** (after cutover acceptance) | The old engine, the rollback switch and the purity exceptions are removed. This **deliberately ends the rollback capability**, which is why it is a separate slice with its own acceptance | Purity allowlist emptied of the engine files (section 12); transitive dependency test green; full suites and Linux CI with the new engine as the only engine |

## 14. Exit criteria

1. Golden compatibility, with every difference individually listed and justified.
2. Deterministic and scripted-model paths validated.
3. Pinning, cache, scope, cancellation and provider-budget protections preserved.
4. The generic lookup boundary, atomic keyed execution with the section 5.3 replay contract, the stated keyless-write limitation, and the tested legacy translation in place.
5. The synthetic-definition proof passing without core modifications.
6. Engine files removed from the purity allowlist by the end of slice 5d, and the transitive dependency test green throughout.
7. The model boundary enforced as revision 4 requires: strict parser limits, verified parameter
   provenance, confirmation for every model-originated mutation, and one immutable turn snapshot.
8. Execution keys issued for mutations only, carried in the section 5.5 envelope.
9. **Every `TurnResponse` field** covered by an explicit parity test, so a green routing
   comparison cannot hide a silently emptied field. The full list, as the schema actually declares
   it: `session_id`, `turn_id`, `status`, `speech`, `proposed_action`, `validated_action`,
   `execution` (the additive envelope, null for non-mutations), `intent_trace`, `signals`,
   `retrieved_context`, `session_summary`.
10. The old engine restorable by the deployment switch alone, with no database change, and
    default until that switch is set. This holds **through 5c**; slice 5d removes the old engine
    and the switch, ending rollback deliberately rather than by accident.
11. Green Linux CI, including the existing browser tests.

## 15. Sizing and risks

- **Slice 3 transaction review: complete.** Three rounds reproduced eight defects, all fixed with
  regression tests, and slices 2 and 3 were signed off on 2026-09-17 after green Linux CI. The
  review confirmed that the record store's write and the ledger's check and outcome share one
  connection and one transaction, shown in code and exercised by the section 10.6 tests.
- **Each remaining slice is its own review gate.** 4a, 4b, 5a, 5b, 5c and 5d are reviewed separately.
  The defect rate in slice 3 is the reason: every round found real defects in code already reported
  green, and the ones that mattered were in mechanisms nobody had thought to check rather than in
  the mechanism under construction.
- **Main risk:** separating routing, memory, record access and execution without changing behaviour, not the file sizes.
- **Specific risks:**
  - **Hidden ordering dependencies** in today's planner. The slice 2 shadow harness exposes them before the switch.
  - **The web change.** Passing the execution key is a small change to today's write calls, covered by the new key-presence browser test and the existing suite.
  - **Write-path coupling.** Today's store opens its own connection per write. The execution check must share that transaction, which means a small refactor of the store's write methods.
  - **Current reply wording** that uses organization data stays as recorded unless listed.
  - **Known corrections** (section 11) change at least eight golden turns. Each is listed.
  - **Provenance false negatives.** A legitimate request whose value the backend cannot attribute
    to one of the section 8.3 sources is refused. That is the safe direction, but the per-class
    tests exist so the refusal rate does not quietly swallow ordinary phrasing.
  - **Response-field parity.** Routing parity can be green while `signals`, `session_summary` or
    `retrieved_context` empties out. Exit criterion 9 exists for exactly that, and it is the
    largest single risk in the cutover.
  - **Speech wording.** Today's replies are hand-written per action and per feature, with a
    grounded prefix from retrieved documents. A generic composer driven by definition templates
    will not reproduce them, so many `wording` differences are expected — each listed, never
    resolved by regenerating a recording.
