# Milestone 3.2, Slice 5d: Legacy Engine Removal - Implementation Plan

Status: **revision 6, awaiting owner approval. No 5d runtime code has changed.**
Date: 2026-09-22. Parent plan: `docs/MILESTONE_3_STEP_3_2_PLAN.md` revision 4.3. Prerequisite:
slice 5c is merged, signed off and accepted in production under the gates in section 2.

## 0. What changed, revision by revision

Revision 6 replaces the production acceptance gate (section 2.1). Revisions 1 to 5 assumed Pixel
would be exercised by real visitors before its rollback switch was removed. It will not be: Pixel
is developed, tested and deployed by one person, the demo is not advertised, and there is no
audience to draw 250 real turns from. The old gate was therefore not merely unmet but unmeetable,
and the only ways to "meet" it were to wait for traffic that is not coming or to relabel scripted
traffic as real. The second is fabricating the evidence the gate exists to provide, so the gate is
rewritten around evidence that can honestly be produced and that answers the same question. What
is lost by the change is stated in the new section 2.1, not hidden by it.

Revision 1 was written before 5c settled, and would have deleted code that still owned live
behaviour, voice, test routing and later-milestone shims. Revision 2 replaced the loose deletion
list with a file-by-file ledger, added the behaviour ownership matrix, made the browser suite under
definition authority a hard gate, fixed the rollback default, required the telemetry baseline to be
archived, and defined the 500-turn gate. Revision 3 separated the pre-removal dual-authority proof
from the post-removal single-engine proof, added the clean-baseline gate, and made the one-release
`PIXEL_ENGINE_MODE=definition` compatibility window explicit.

Revision 5 answers the owner's review of revision 4: the section 5 contradiction, the
under-specified acceptance evidence, the unspecified manifests, and the vague artifact identity.
Sections 2.1a, 2.3a, 3.1, 5.1 and 13.1 are new and normative. Two points of that review are
accepted without argument: the working tree must be committed and merged before section 2.0 is met,
and this work does not fit in one pull request (section 13.1).

Revision 4 corrected the plan against the repository as 5c actually left it:

- the **behaviour matrix is re-measured** (section 5). **No *port* row remains**: most rows differ
  only in wording, and three are accepted behaviour changes, tabled separately in section 5.1.
  Revision 3 still listed six ports that 5c completed;
- `/health` **keeps reporting `"authority"`** as a constant for the compatibility release, because
  the cutover tooling depends on it (section 4.2). Revision 3 deleted the field and broke
  `scripts/visitor_check.py`, its test and the 5c runbook;
- the **duplicated browser CI job** under definition authority is named for removal (section 11);
- the **shared test fixture is extracted before any legacy test file is touched** (section 4.1);
  four definition-authority test modules import it today;
- the **500-turn window and milestone 3.4 are reconciled** (section 2.1): which changes reset the
  count, and which do not;
- the operator surface records the **backup commands and the visitor script** that 5c added, and
  names the `cutover-report` rename as a documentation edit (section 11).

## 1. Outcome

Remove the previous conversation engine, its rollback switch and the temporary shadow-comparison
machinery. After 5d there is one backend conversation path: the definition-driven engine accepted
in 5c.

This slice deliberately ends **configuration-only rollback**. It is not a refactor to perform while
5c is still being evaluated. Cleanup is complete only when:

1. no production, test, operator or product-package import can reach the previous engine;
2. `PIXEL_ENGINE_MODE`, `PIXEL_SHADOW_ENGINE`, `PIXEL_TESTING_ENTRY` and their branching code are
   gone, on the schedule in section 11;
3. every behaviour the legacy engine owned has a decided owner (section 5), and old-engine tests are
   replaced by equal or stronger definition-engine coverage before deletion;
4. every 3.2 product-coupling allowlist entry is either removed with its file or honestly relabelled
   to the later milestone that still uses it (section 4.4);
5. the full product, including voice, works with the definition engine as the only authority;
6. shims assigned to later milestones remain in place and are not mislabelled as legacy-engine code.

## 2. Hard entry gates

5d coding may start only after the owner explicitly accepts the loss of switch rollback. The pull
request may merge only when every gate below has recorded evidence in the 5c report or the 5d PR.

### 2.0 Clean 5c baseline

- `phase-3` is up to date with the merged 5c pull request, and Linux CI for it is green: API,
  product, web, production build and both browser-authority jobs.
- No uncommitted runtime, product, golden, test or documentation changes, except this plan.
- Production runs definition authority with the evidence of section 2.1, or still runs legacy by
  explicit operator choice. A local-only pass never starts deletion.
- The owner has read the final 5c report and stated that the project may stop preserving switch
  rollback once section 2 is met.

### 2.1 5c acceptance, for a product with no visitors

The question this gate answers is unchanged: **is the definition engine safe to be the only
engine?** The evidence is not, because Pixel has no visitors and will have none before this work.
Volume from strangers is replaced by coverage, durability and rehearsal, each of which one
operator can actually produce.

1. **Coverage, not volume.** Every workflow in the 5c matrix is exercised under definition
   authority against the deployed production API, through the public visitor path, in one recorded
   run. `acceptance-report` prints `workflows` and `workflows.missing`; **`workflows.missing` must
   be empty**. These sessions are labelled `synthetic-visitor-check-` and are counted and reported
   as synthetic. No report, commit message or sign-off describes them as real visitors.
2. **Restart while conversations are open.** At least one redeploy or API restart happens with
   sessions open, and those sessions continue afterwards, proven by `restarts[]`.
3. **Durability across days.** At least two separate calendar days of definition authority in
   production with no rollback, so state that must survive a restart is exercised more than once.
4. **Quality.** No isolation failure, unauthorized write, duplicate execution, dishonest completion
   claim or definition-pin bypass, and no definition-engine 5xx in the recorded run.
5. **Green Linux CI** on the 5c head: API, product, web, production build and both
   browser-authority jobs.
6. **Rollback rehearsed** (section 2.2, unchanged). With traffic evidence gone this becomes the
   most valuable gate remaining, because it is the one that never depended on traffic.
7. **The owner's decision, recorded.** Because real-visitor evidence is absent, removing switch
   rollback is explicitly the owner's decision, taken knowing that Pixel has never been exercised
   by anyone other than its developer. The 5c report records that in those words.

**What this costs us, plainly.** We give up the evidence that unfamiliar people, phrasing requests
in ways we never imagined, do not break the engine. Nothing in this plan replaces that. The
partial mitigations are the golden recordings, the browser suites under both authorities, and the
live sweeps already performed against production, all of which test phrasing we chose. The first
real users will still find things we did not. That is an accepted risk of shipping a proof of
concept, not an oversight, and it is the reason section 2.2 is not also relaxed.

**What still resets the gate.** A rollback to legacy authority, any section 11.4 trigger of the 5c
plan, a definition-version move, or any change that can alter what a turn does or says. A change
is judged by evidence, not by file: it resets unless the shadow comparison and both browser
goldens are unchanged by it, and it touches no routing, wording, memory, validation or execution
path. Work that only adds tests, documentation or operator commands does not reset it. Milestone
3.4b and later change retrieval and therefore reset it; schedule them after this gate is met, or
accept the restart deliberately and record that choice.

### 2.1a The command that proves section 2.1

Sign-off is a report, not a recollection. `turn_telemetry_daily` holds daily counts per authority
and metric with no session identity, so it cannot answer "how many distinct sessions" or "how many
were real". The first 5d deliverable is therefore a read-only operator command,
`python -m app.ops acceptance-report --since <ISO date> [--deploy-at <ISO timestamp> ...]`, built
before any deletion and run by the operator. It reads `sessions`, `messages`, `action_executions`
and `turn_telemetry_daily`, and prints one JSON document with exactly these fields:

| Field | Source | Gate it proves |
| --- | --- | --- |
| `window.first_turn_at`, `window.last_turn_at`, `window.days` | `messages.created_at` | the 7-day window |
| `turns.accepted`, `turns.denied`, `turns.stale` | `turn_telemetry_daily` status counts, authority `definition` | volume and the 1% quality bar |
| `turns.by_session_p50`, `turns.by_session_max` | `messages` grouped by `session_id` | that volume is not one long session |
| `sessions.total`, `sessions.real`, `sessions.synthetic` | `sessions` joined to `messages`; a session whose id starts with `synthetic-visitor-check-` is synthetic | 50 distinct sessions, 25 real |
| `turns.real`, `turns.synthetic` | same split, counted per turn | 250 real turns |
| `workflows` | distinct `action_executions.action_key` and `capability`, plus the telemetry `stage` values, each with a count | every workflow in the 5c matrix was exercised |
| `workflows.missing` | the matrix list minus `workflows` | names any workflow with no evidence, instead of leaving it to memory |
| `restarts[]` | each `--deploy-at` timestamp, with the number of sessions whose first message precedes it and last message follows it | a restart happened while conversations were open |
| `failures` | counts of `stale`, `worker_unhealthy`, `shadow_error`, and executions in state `failed` | the quality bar |
| `resets.last_definition_version_move`, `resets.last_engine_change` | `product_bindings.updated_at`; the commit recorded in the report | the count was not reset mid-window |

The command exits non-zero when any gate is unmet, and prints which one. The 5c report attaches its
output verbatim. Adding it does not reset the acceptance window: it only reads.

### 2.2 Rollback is rehearsed before it is removed

Using the release candidate and one populated database:

1. run a session in definition mode;
2. switch to legacy and restart without a data command;
3. continue the same session and verify the documented pending-state limitation;
4. switch back to definition and continue again;
5. verify records, execution ledger, definition pins, engine state (including a remembered visitor
   name) and private-instance generation are unchanged except for the deliberate test actions.

The rehearsal is the final proof that 5c's safety net works. Only after it passes may 5d remove it.

### 2.3 Recovery remains operational

- A fresh encrypted database backup has a verified restore: `ops backup` reports
  `"restorable": true`, and the copy is encrypted and stored off the Railway volume with its
  `sha256` recorded.
- The accepted 5c application artifact, environment manifest and deployment instructions are kept as
  an **emergency artifact rollback**, not as a supported runtime switch.
- **The 5c artifact defaults to legacy authority when `PIXEL_ENGINE_MODE` is unset.** Its retained
  manifest therefore sets `PIXEL_ENGINE_MODE=definition` explicitly, and the recovery instructions
  require `/health` to report `"authority": "definition"` before traffic.
- No 5d database migration is destructive. The 5c artifact still starts against the additive schema
  during the deployment window.

### 2.3a What "the retained 5c artifact" means, exactly

"Keep the old build" is not a procedure. The 5c report records all five of these before 5d starts,
and the 5d PR links to them:

1. **Source identity.** An annotated git tag `5c-accepted` on the merged commit, with the commit
   SHA written in the 5c report. The tag is never moved; a later fix gets a new tag.
2. **Image identity.** The Railway deployment ID of the accepted 5c deployment and its image
   digest (`sha256:...`), read from the deployment's details and recorded in the report. Rollback
   is "redeploy that deployment ID", not "rebuild that branch": a rebuild is a different artifact.
3. **Environment.** The variable **names** required by that artifact, with the two non-secret values
   that matter (`PIXEL_ENGINE_MODE=definition`, and `PIXEL_SHADOW_ENGINE` absent), committed as a
   checklist in the runbook. Secret values are never written to the repository; they stay in the
   Railway service and are listed by name only.
4. **Data.** The `sha256` and location of the encrypted backup taken at cutover (`ops backup`),
   stored off the Railway volume.
5. **Proof it still starts.** The section 2.2 rehearsal redeploys that deployment ID against a
   populated database and records `/health` reporting `"authority": "definition"`. A retained
   artifact nobody has started is not a rollback plan.

To make step 5 checkable at a glance, the compatibility release adds the build commit SHA to
`/health` (an additive field beside `authority`), so an operator can see which artifact is serving
without reading deployment logs. If Railway's retention ever drops the accepted deployment, the
rollback path is gone: the operator re-checks that the deployment is still redeployable at each
rehearsal, and the 5c report records the date of the last successful check.

### 2.4 The kept engine is the one the browser proves

Two different proofs. They must not be collapsed.

**Before the first deletion commit**, the complete browser suite (`tests/e2e/**`) and the browser
golden (`products/linear_simplified/tests/browser-golden.spec.ts`) pass twice against the isolated
test API:

1. legacy authority, compared with `golden/browser_differences.json`;
2. definition authority, compared with `golden/browser_differences_definition.json`.

Definition authority is the kept path. Every difference from `browser_decisions.json` is listed with
its kind and reason in the definition file, and none is regenerated to pass.

**After deletion**, legacy authority does not exist. The browser suite and golden run once against
the single engine. The canonical `browser_decisions.json` is then re-recorded from the accepted
definition-engine behaviour, reviewed and committed deliberately, and the legacy difference file is
kept as audit history. The 5d pull request must show both phases: the dual-authority proof in its
commit history, and the single-engine proof at the head commit.

### 2.5 Evidence survives its retention

`turn_telemetry_daily` keeps 30 days. Before the first legacy-baseline rows would be pruned, and
again at the end of the section 2.1 window, the operator exports
`python -m app.ops cutover-report --days 30` for both authorities and attaches it to the 5c report.
The latency and fallback comparison uses these archived reports, not whatever telemetry remains.

### 2.6 Behaviour ownership is decided

Every row of section 5 carries a recorded decision: *accept* (with the owner's approval of the
changed behaviour), *port* (with its destination and test), or *5c blocker* (fixed before cutover).
No row may be undecided, and no decision is taken while deleting.

As measured on 2026-09-22 (section 5), **every row is decided**: four blockers fixed, six ports
completed during 5c, five owner decisions recorded, three behaviour differences accepted. This gate
is met at the time of writing and is re-measured for the PR.

## 3. Deletion principles

- A file is deleted only after its importer list (section 4) shows no supported caller, and the
  import-graph and source-only tests (section 8) pass without it.
- The ledger in section 4 ships with the PR as a machine-readable manifest. The manifest, not prose,
  is authoritative: a file missing from it fails the manifest test, and a file listed as deleted but
  still importable fails too.
- Removal proceeds in dependency order, one reviewable commit per group, each leaving a runnable
  application.
- No compatibility wrapper keeps the old engine reachable "temporarily". If a path still needs old
  behaviour, that behaviour is ported first or 5d stops.
- Every deletion commit carries the smallest relevant proof: import graph, focused tests, or a
  source-only startup check. The final PR runs the full suite.

### 3.1 The manifests, exactly

Two files ship with the PR. Both are JSON, both live in `apps/api/tests/manifests/`, and both are
validated by `apps/api/tests/test_5d_manifests.py`, which runs in the ordinary API suite. The
validator is the gate; prose in this plan is not.

**`deletion_manifest.json`** - one entry per file or symbol in section 4:

```json
{
  "generated_from": "5d plan revision 5, section 4",
  "entries": [
    {
      "path": "apps/api/app/services/agent.py",
      "kind": "file",
      "decision": "delete",
      "importers_at_5c": ["apps/api/app/main.py", "products/linear_simplified/tests/golden_shadow.py"],
      "condition": "behaviour matrix decided; main.py no longer constructs it",
      "removed_in_commit": null
    }
  ]
}
```

- `kind` is `file` or `symbol`; a symbol entry adds `symbol` (for example
  `ExecutionLedger.dispatch_if_current`).
- `decision` is `delete`, `keep`, `move` or `relabel`; `keep` adds `owner_milestone`, `move` adds
  `to`, `relabel` adds `from_milestone` and `to_milestone`.
- The validator fails when: a path in section 4 is missing from the manifest; a `delete` entry's
  path still exists after its commit; a `delete` entry is still importable from the API entry point;
  a `keep` entry no longer exists; an `importers_at_5c` list disagrees with the measured import
  graph at the head commit for any file that still exists.

**`coverage_transfer.json`** - one entry per old-engine test or golden case (section 7):

```json
{
  "entries": [
    {
      "case": "apps/api/tests/test_agent.py::AgentTest::test_assigns_the_named_person",
      "classification": "preserved",
      "replacement": "apps/api/tests/test_backend_conversation.py::DefinitionAuthorityConversationTest::test_an_unknown_person_is_offered_for_adding_in_every_request",
      "note": "same behaviour, definition authority"
    }
  ]
}
```

- `classification` is `preserved`, `changed`, `obsolete` or `security`.
- `preserved`, `changed` and `security` require a `replacement` that resolves to a test that exists
  and passes; `changed` also requires `difference`, naming the reviewed golden entry that records
  it; `obsolete` requires a `note` saying which implementation detail disappeared.
- The validator fails when: an entry is missing a required field; a `replacement` does not resolve;
  two entries share a `replacement` that covers only one of them; a deleted test file has cases with
  no entry; or any `security` entry is classified `obsolete`.

Both manifests are written in step 1 of section 13, before anything is deleted, and updated in the
commit that performs each deletion. `python -m unittest tests.test_5d_manifests` is the command; CI
runs it with the rest of the API suite.

## 4. Delete / keep / move ledger

Importers were measured from the repository at 5c (`apps/api/app`, `products`, `scripts`, `tests`)
and are rechecked when the manifest is produced.

### 4.1 Delete (legacy engine)

| File | Importers today | Condition |
| --- | --- | --- |
| `services/agent.py` (legacy orchestrator) | `main.py`, `golden_shadow.py` | behaviour matrix decided; `main.py` no longer constructs it |
| `services/action_planner.py` | `agent.py` | with `agent.py` |
| `services/action_validator.py` | `agent.py` | with `agent.py` |
| `services/intent_extractor.py` | `agent.py` | with `agent.py` |
| `services/reasoning_policy.py` | `agent.py` | with `agent.py` |
| `services/conversation_manager.py` | `agent.py` | with `agent.py` |
| `services/agent_reasoner.py` (legacy LLM reasoner) | `agent.py` | with `agent.py`; the generic model gateway stays |
| `services/legacy_adapter.py` | `main.py` | with the authority switch |
| `services/demo_data.py` | legacy modules only | dead with the legacy engine; its 3.5 allowlist entry leaves early, and any record helper a kept module needs moves to `product_data_store.py` first |
| `testing_main.py` and `PIXEL_TESTING_ENTRY` | test harness only | the harness starts the one app entry |
| `ExecutionLedger.dispatch_if_current` | `legacy_adapter.py` | with the adapter |
| `SessionManager.recent_user_messages` | `agent.py` only | unconditional: the visitor's name is remembered in `engine_state.visitor_name` since 5c, so nothing else needs it |
| legacy-only tests and runners (`test_agent.py`, the legacy class in `test_backend_conversation.py`, the legacy cases in `test_engine_cutover.py`, `golden_backend.py`, the live side of `golden_shadow.py`) | tests | only after section 7 coverage transfer |

**The shared fixture is extracted first.** `tests/test_engine_cutover.py` provides
`EngineCutoverFixture` to `test_backend_conversation.py`, `test_ops_backup.py`,
`test_turn_telemetry.py` and `test_visitor_check.py`. Before any legacy test file is touched, the
fixture moves to a neutral module (for example `tests/engine_app_fixture.py`) with its authority
default set to definition, and the four importers are updated in that same commit. Deleting the
legacy file must never be what moves the fixture.

### 4.2 Delete (shadow and switch)

- `services/shadow.py` (runner, controller, scheduler, memory store, breaker, watchdog) and its
  tests; `PIXEL_SHADOW_ENGINE`; shadow startup and shutdown hooks and background counter flushing.
- `services/shadow_parity.py` writes and the `shadow-report` command. The table
  `shadow_parity_daily` stays **dormant** (not dropped), documented, until a later maintenance
  migration removes it after retention and backup requirements are met.
- `PIXEL_ENGINE_MODE` branching: `engine_mode()`, `turn_engine()`, the `live_turn` path and the
  `TurnResponse._engine_entered` private attribute (shadow-only).
- **`/health` keeps its `authority` field**, reporting the constant `"definition"`, for the
  compatibility release. It is the first check of `scripts/visitor_check.py`, which the 5c runbook
  uses before and after every deployment, and of `apps/api/tests/test_visitor_check.py`. The field,
  the script's `--expect-authority` argument and that test's legacy case are removed together in the
  follow-up release that removes the environment variable (section 11), not in 5d.
- No hidden header, test flag, admin endpoint or undocumented environment escape hatch may
  re-enable the old engine.

### 4.3 Keep

| Kept | Why |
| --- | --- |
| `services/retriever.py` | imported by the product knowledge adapter; replaced in 3.4 |
| `products/linear_simplified/backend/knowledge.py` | the product's knowledge seam and the last caller of the retriever; both leave in 3.4 |
| `services/language_normalizer.py` | imported by `retriever.py`; leaves with it in 3.4 |
| `product_config.py` | imported by `retriever.py`; leaves with it in 3.4 (its voice use moves, section 4.5) |
| `workspace_config.py`, `services/product_data_store.py`, demo-data endpoints and write shims | records and scopes, replaced in 3.5 |
| product action translator, web action schema, web adapters and rendering shims | replaced in 3.6 |
| execution ledger, receipts, `engine_state` (including `visitor_name`) and its retention, provider budgets, rate limits, speech | platform contracts |
| `services/turn_telemetry.py` and the turn report command | the only per-turn health evidence; the authority dimension becomes a constant |
| `services/model_gateway.py`, strict parser, provenance, confirmation boundary, `PIXEL_MODEL_GATEWAY` | the generic model path (5c section 8) |
| `ops backup`, `ops verify-backup`, `ops move-product-version`, `ops check-readiness`, `ops execution-preflight`, `ops reset-demo-data` | operator surface the 5c runbook depends on |
| `scripts/visitor_check.py` and its test | the cutover and deployment check; its legacy-authority case leaves with the environment variable |
| installed product registry and every published definition, with v5 bound | product definitions |
| `shadow_parity_daily` table (dormant) and all reviewed-difference files | audit evidence |

### 4.4 Allowlist (`KNOWN_PRODUCT_COUPLING`) changes

- Removed with their files: `services/agent.py`, `action_planner.py`, `action_validator.py`,
  `agent_reasoner.py`, `conversation_manager.py`, `intent_extractor.py`, `services/demo_data.py`
  (its 3.5 entry leaves early because the file is gone), `services/legacy_adapter.py`.
- **Relabelled, not removed:** `services/language_normalizer.py` and `product_config.py` move from
  3.2 to **3.4**, because the kept retriever still imports them. The allowlist test records the new
  owner milestone; this is not an exception to the shrink rule.
- Entries assigned to 3.3, 3.4, 3.5 and 3.6 remain.

### 4.5 Move

| From | To | Why |
| --- | --- | --- |
| `main.py` speech: `PRODUCTS_BY_ID[definition_id].voice_style` | the pinned definition's `identity.voice_style` | voice must not depend on the legacy product configuration; every published version declares it |
| any `demo_data.py` helper still needed by a kept module | `product_data_store.py` | keeps 3.5 record code in one place |
| `EngineCutoverFixture` | a neutral test fixture module (section 4.1) | four definition-authority test modules depend on it |

The voice-style move lands before `product_config.py` is relabelled to the 3.4 bucket. A test proves
speech receives the exact `identity.voice_style` of the pinned definition version, not a product-ID
lookup.

## 5. Behaviour ownership matrix (measured 2026-09-22)

Every reply the legacy engine owns after 5c, sent through both authorities on the golden seed
(`test_engine_cutover` fixture, Product Engineering Workspace, Dashboard). Nothing is left to
port. Rows fall into two groups, and they are not mixed:

- **Same action, different words.** The validated action and its payload are identical in both
  engines; only the sentence differs. A visitor ends up in the same place, having changed the same
  records.
- **Accepted behaviour change.** The visitor's experience genuinely differs. Each one is an owner
  decision recorded here, and each is listed in the reviewed browser differences as `behaviour`.

Three rows are behaviour changes. They are tabled separately below so that "same action" is never
claimed for them.

| Behaviour | Action | Difference | Class |
| --- | --- | --- | --- |
| greeting | same | wording | accept |
| greeting remembers the visitor's name | same | both greet by name; wording | **ported in 5c** |
| identity | same | wording (owner decision: plain names) | **decided** |
| capabilities | same | wording, plain labels (owner decision) | **decided** |
| guided path | both open Dashboard | conversational route from the caller's offers (owner decision) | **decided** |
| next step | same | view-aware in both; on the Dashboard the platform points to what it can do | **ported in 5c**, residual wording accepted |
| profile statement | same | wording | **5c blocker, fixed** |
| project count / team count | same | same count and names; word order | **ported in 5c** |
| correction | same | identical sentence | **decided**, now matching |
| member add, named | same action and prefill | wording: unknown and hidden people read alike | **ported in 5c** |
| member add, unnamed | same | platform asks for the name and the answer prefills the form | **ported in 5c** |
| create without an owner | same | both ask and highlight (Linear v5) | **decided** |
| create for an unknown owner | same action and prefill | wording | **ported in 5c** |
| mixed create and assign, unknown person | same action and prefill | wording | **5c blocker, fixed** |
| update with nothing open | same | wording (owner decision) | **decided** |
| filter by person, and "what about <person>" | same action and payload | wording | **ported in 5c** |
| "what did we just change?" | same | identical sentence | **5c blocker, fixed** |
| vague assign | same | wording | accept |
| out-of-scope person | same view | definition says it cannot find the person first | accept, security-positive |

### 5.1 Accepted behaviour changes (not "same action")

| Behaviour | Legacy | Definition engine | Why it is accepted |
| --- | --- | --- | --- |
| "start a ticket assigned to Noah" | opens the ticket form prefilled with Noah Patel; the visitor fills the rest in the form | drafts the create in conversation: asks for the title, then the project, then proposes the complete create | 5c plan, section 7.2: no field is guessed, and Linear v4 removed the project default that filed every assistant ticket under one project. The visitor reaches a created ticket either way, through a different path. Recorded in the reviewed browser differences (`create-start-assigned`) |
| an update naming a person who is not available | "Which ticket should I update: Maya's ticket, Noah's ticket, or the issue currently open?" | "I can't find Priya in this workspace. I'll open Teams and highlight Add member.", prefilled with the typed name | answers the actual obstacle instead of a question the visitor cannot usefully answer; writes nothing; and reads identically for a person in another workspace, which the security tests enforce. Recorded as `update-unknown-person` and `update-outside-person` |
| a request across every workspace | completes, and shows this workspace's data | denied, with the platform's scope refusal | the v2 guardrail decision: a request the product cannot honestly satisfy is refused rather than quietly narrowed. Visible as `turn_status: Action blocked` in the browser golden (`scope-broad-request`) |

No other row changes what the visitor ends up with. The matrix is re-measured for the 5d PR from the
same probe; if the probe disagrees with either table, the probe wins and the plan is amended before
deletion continues.

## 6. Browser decisions (corrected)

5c removed the browser's local decision code outright: every visitor message reaches `/api/turn`,
`localTurnResponse` and the local intent handlers no longer exist, and a web unit test fails if the
browser ever authors an assistant sentence again. Milestone 3.3 therefore no longer deletes them;
its remaining browser work (for example the parsing that prefills the add-member form) is re-scoped
in its own plan. 5d does not touch the browser beyond removing test-entry routing and the
authority-specific branches in `tests/e2e/demo-agent.spec.ts`.

## 7. Coverage transfer before deletion

Deleting old tests is allowed only after their behaviour has a destination:

1. Inventory every old-engine test and golden case.
2. Classify each as preserved (definition-engine replacement test), intentionally changed (reviewed
   golden difference plus a section 5 decision), obsolete implementation detail, or
   security/regression behaviour that is never dropped.
3. Add or identify the replacement before deleting the old test.
4. A machine-readable coverage-transfer manifest fails when an old case has no classification, a
   replacement path does not exist, or two old cases map to nothing.
5. If a file holds both legacy and definition-authority coverage, split it first. `said(...)`
   branches in the browser specs collapse to the definition expectation rather than being deleted.

Security, isolation, cancellation, confirmation, replay, provider-budget and response-integrity
tests are never classified as obsolete merely because they were first found through the old engine.

The canonical product golden becomes the accepted definition-engine recording only after the final
single-engine proof: generated for inspection, reviewed, then committed deliberately. Reviewed
expected files are never overwritten automatically.

## 8. Purity and dependency closure

- Starting from the API entry point, no transitive import reaches a deleted module.
- Starting from `app.engine`, no transitive import reaches product data, product packages, network,
  database, provider, speech or execution modules except the approved registry boundary.
- Dynamic imports remain banned in engine code.
- A clean interpreter imports the API, runs readiness, serves a turn and speaks a reply with every
  deleted file physically absent. The test copies the release source without them; stale
  `__pycache__`, editable paths or build output cannot make it pass.
- A repository text scan fails on stale operational references: retired environment variables,
  old-engine import paths, old command names, and comments telling an operator to select legacy
  authority. Historical reports may mention them only when clearly labelled as history.

## 9. Single authoritative runtime

After cleanup the turn lifecycle is exactly the accepted 5c lifecycle: authorize and activate;
materialize one scoped snapshot; load durable engine state; route deterministically, optionally
consult the model gateway; validate and compose; finalize state and any key in one transaction;
return one response and execute mutations only through receipts. There is no comparison run, legacy
projection, alternate response mapper or second memory owner. Compatibility writes (messages,
signals, `visitor_context`) remain only where a live feature reads them; dead writes are removed
after a read/write usage test proves them dead.

No product-specific reply or action logic moves into core to make deleting a legacy file easier. It
belongs in the product definition or package, or stays a recorded gap.

## 10. Data and schema policy

- Keep the additive 5c state and 5b execution schema; do not rewrite session pins, engine state or
  execution outcomes.
- Keep historical messages, signals, usage, parity and execution rows under their retention rules.
- Do not drop old columns or tables in the deployment that removes the code.
- Startup stays forward- and backward-compatible with the immediate 5c artifact.
- **Open sessions.** Production moved from v2 to v5 during the 5c cutover, so sessions pinned to v2
  can still be open (24-hour maximum age). v2 is covered by definition-engine tests and stays
  supported. A session pinned to a version the definition engine has no test for must have expired,
  or be ended, before deployment; the deployment checks the pinned versions in use and reports them.

## 11. Configuration, operations and documentation

- Remove `PIXEL_ENGINE_MODE`, `PIXEL_SHADOW_ENGINE` and `PIXEL_TESTING_ENTRY` from `.env.example`,
  CI and runbooks. For one release, startup **refuses to start** on any setting that would select
  the old runtime: `PIXEL_ENGINE_MODE` with any value other than `definition`,
  `PIXEL_SHADOW_ENGINE=on`, or `PIXEL_TESTING_ENTRY` set. `PIXEL_ENGINE_MODE=definition` is
  **accepted with a startup warning**, because it is the one value that matches the 5d runtime.
- **Why `definition` must stay accepted.** In the 5c build an unset `PIXEL_ENGINE_MODE` means
  `legacy`. If 5d refused the variable outright, production would have no safe order: removing it
  first silently puts the running 5c build back on the legacy engine, and keeping it makes 5d refuse
  to start. It would also break the artifact rollback, which must run with the variable set. So
  production keeps `PIXEL_ENGINE_MODE=definition` through the 5d deployment and its rollback window.
- **CI.** The workflow currently runs the browser suite twice, the second job with
  `PIXEL_ENGINE_MODE=definition`. The deletion commit that removes the switch also removes that
  second job, leaving one browser job that proves the only engine. Until that commit, both jobs run.
- **Configuration and documentation change together.** The PR that adds the refusal updates
  `.env.example`, the CI workflow, the deployment runbook and the 5c operator steps, and lists every
  place a retired variable was removed or kept, and why. Before deploying, the operator confirms
  production has `PIXEL_ENGINE_MODE=definition`, no `PIXEL_SHADOW_ENGINE=on` and no
  `PIXEL_TESTING_ENTRY`, and records `/health` before and after.
- **Operator surface.** Keep version movement, readiness, data maintenance, usage, the turn report,
  `backup`, `verify-backup` and `scripts/visitor_check.py`. Remove `shadow-report` and anything that
  instantiates the old engine. `cutover-report` is renamed to a turn report; the 5c runbook and the
  5c report are edited in the same commit, and the old name keeps working for one release so an
  operator following a printed runbook is not stranded.
- **The compatibility window, explicitly.**
  1. 5d release: `PIXEL_ENGINE_MODE=definition` accepted with a warning; `/health` still reports
     `"authority": "definition"`; every other value or retired variable fails startup.
  2. Next release, after the rollback window: production removes the variable; startup then refuses
     it entirely; `/health` drops the field; `visitor_check.py` drops `--expect-authority` and its
     test drops the legacy case.
  3. The retained 5c artifact keeps its own manifest with `PIXEL_ENGINE_MODE=definition`.
- Preserve the independent model, speech, provider-budget and paid-provider kill switches.
- Documentation: mark 5c accepted with its evidence; state that 5d ends switch rollback and name the
  emergency artifact procedure; update the architecture page to one engine; leave historical slice
  reports unchanged.

## 12. Test plan

- **Negative existence:** deleted modules and retired switches do not exist; repository search finds
  no import, string, documentation instruction or CI reference that selects the old runtime; startup
  refuses every setting that would select it, and starts with a warning on
  `PIXEL_ENGINE_MODE=definition`.
- **Coverage transfer:** every deleted case is classified; every preserved or security case points
  to a passing replacement; reviewed differences match the canonical golden exactly.
- **Runtime:** every 5c authority-independent contract (state, required fields, model boundary,
  response mapping, execution, isolation matrix, restart, races, cancellation, confirmation, replay,
  private instances, keyless forms, voice) stays green with the deleted files absent.
- **Browser:** the full suite and golden, single engine, as in section 2.4.
- **Operator tooling:** `scripts/visitor_check.py` passes in-process against the single engine, and
  `ops backup` and `verify-backup` still prove a restore.
- **Voice:** a reply is spoken with the voice style from the pinned definition.
- **Definition pin:** a session pinned to one version keeps that version's identity, voice style,
  routing and reply templates after newer versions are published.
- **Packaging and deployment:** source-only and container builds start without the removed files; a
  populated 5c database starts with the new artifact; the retained 5c artifact starts against the
  same database with `"authority": "definition"`; the public smoke matrix passes.
- **No paid-call surprise:** the normal 5d CI path runs with paid providers disabled or faked. Any
  real speech or model check is authorized separately and logged.

## 13. Implementation order

1. Produce the dependency, ledger, behaviour-matrix and coverage-transfer manifests, deleting
   nothing.
2. Close the pre-removal half of section 2.4: browser suite and golden under both authorities,
   recorded.
3. Extract the shared test fixture (section 4.1) and update its four importers.
4. Move voice style to the pinned definition; move any kept `demo_data` helper.
5. Remove the shadow runtime, reporting hooks and retired configuration references.
6. Remove the authority switch, `live_turn`, `testing_main` and the adapter; build the definition
   service unconditionally; remove the duplicated browser CI job; keep `/health`'s constant field.
7. Remove the legacy modules and legacy-only tests in dependency order.
8. Apply the section 4.4 allowlist changes and strengthen the source-only and import-graph tests.
9. Re-record the canonical browser and backend goldens from the single engine, reviewed and
   committed deliberately.
10. Remove dead compatibility writes proven unused; leave the schema intact.
11. Update operator commands, environment examples, architecture and runbooks.
12. Run the clean-source, populated-database, artifact-rollback and full regression rehearsals.
13. Merge only after Linux CI is green; deploy; run the public smoke matrix; record sign-off; file
    the follow-up release that removes the compatibility variable and the `/health` field.

Each commit keeps a runnable application; no single deletion commit makes a failure hard to locate.

### 13.1 This is more than one pull request

Steps 1 to 13 delete engine code, shadow code, test routing, CI branches, operator commands, goldens
and documentation. One pull request that large cannot be reviewed honestly, so 5d ships as four,
each independently revertible, each green on Linux CI before the next begins:

| PR | Steps | What it contains | Risk if reverted |
| --- | --- | --- | --- |
| **5d-1: evidence** | 1 to 3 | acceptance report command, both manifests, the dual-authority browser proof, the fixture extraction | none: adds tooling and moves a fixture |
| **5d-2: move** | 4 | voice style from the pinned definition, kept `demo_data` helpers moved | small, behaviour-preserving, covered by tests |
| **5d-3: switch and shadow** | 5 and 6 | shadow runtime, authority switch, `live_turn`, `testing_main`, the adapter, the duplicated CI job | the rollback switch disappears here; this is the point of no easy return, and it deploys on its own |
| **5d-4: legacy and goldens** | 7 to 11 | legacy modules, legacy-only tests, allowlist changes, re-recorded goldens, dead compatibility writes, documentation | large but inert: the code it deletes is already unreachable after 5d-3 |

5d-3 deploys and soaks before 5d-4 begins. Steps 12 and 13 run against the head of 5d-4.

## 14. Rollout and incident policy

1. Back up and restore-check the production database (`ops backup`).
2. Deploy the new-only artifact without changing provider, model or voice settings.
3. Require readiness, then run `scripts/visitor_check.py --expect-authority definition` and the
   public smoke matrix.
4. Review the first 100 accepted turns and at least 60 minutes of the turn report.
5. On an isolation, mutation-integrity, startup or sustained availability defect, deploy the retained
   5c artifact **with `PIXEL_ENGINE_MODE=definition`** and confirm `/health` before traffic.
6. Do not reintroduce copied old modules into `main`. A defect is fixed in the definition engine, or
   the deployment is rolled back to the signed 5c artifact.
7. If rollback is used, the acceptance window for removing the compatibility variable restarts after
   the fix redeploys.

## 15. Exit criteria

5d is signed off only when:

1. Every entry gate in section 2, and the owner's explicit end-of-switch-rollback approval, are
   recorded.
2. The old engine, authority switch, test entry and shadow runtime are absent from source and
   release.
3. Every behaviour-matrix row has its decision implemented and tested.
4. Every deleted test or case is accounted for by the coverage-transfer manifest.
5. Allowlist entries are removed or relabelled exactly as section 4.4 states.
6. Transitive dependency, purity, source-only import and dynamic-import tests are green.
7. Every 5c security, memory, response, model and execution contract remains green.
8. API, product, web, production build, browser (single engine) and Linux CI suites pass.
9. A populated database starts without destructive migration, and the retained 5c artifact starts in
   `definition` against it.
10. Production smoke, the visitor script and turn telemetry pass with one engine and no retired
    configuration.
11. Documentation states plainly that configuration-only rollback has ended.
12. The follow-up release that removes the compatibility variable, the `/health` authority field and
    the script's authority argument is filed with its acceptance criteria.

## 16. Risks and non-goals

- **Behaviour can disappear during deletion.** The measured matrix and the definition-authority
  browser gate are mandatory, not advisory.
- **Coverage can disappear during deletion.** The transfer manifest and replacement-first order are
  mandatory, and the shared fixture moves before any legacy test file is touched.
- **A hidden import can survive.** Source-only artifact tests and transitive graph traversal catch
  what ordinary imports miss.
- **Operator tooling can break silently.** The visitor script, the backup commands and the runbook
  are part of the deletion review, not collateral.
- **Removing switches can be mistaken for removing safety controls.** Provider, model, speech,
  budget and execution kill switches remain.
- **Emergency rollback can restore the removed engine by default.** Section 2.3 pins it to
  `definition`.
- **Schema cleanup can make artifact rollback impossible.** No destructive schema removal occurs.
- **The product is not fully generic after 5d.** Knowledge (3.4), records (3.5) and web rendering
  (3.6) keep their migrations; `retriever`, `language_normalizer`, `product_config` and the product
  knowledge adapter leave in 3.4.
- **5d adds no features.** A behaviour row is ported only to preserve what visitors already have.
