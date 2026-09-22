# Milestone 3.2, Slice 5d: Legacy Engine Removal - Implementation Plan

Status: **revision 2, awaiting owner approval. No 5d runtime code has changed.**
Date: 2026-09-22. Parent plan: `docs/MILESTONE_3_STEP_3_2_PLAN.md` revision 4.3. Prerequisite:
slice 5c is merged, signed off and accepted in production under the gates in section 2.

## 0. What changed from revision 1

Revision 1 was written before 5c settled. A strict review against the repository found that it
would have deleted code that still owns live behaviour, voice, test routing and later-milestone
shims. Revision 2:

- replaces the loose deletion list with a file-by-file **delete / keep / move ledger** built from
  the import graph (section 4);
- adds a **behaviour ownership matrix** for every reply the legacy engine owns after 5c, measured
  by sending each one through both authorities (section 5);
- adds a hard gate that the **full browser suite and the browser golden run under definition
  authority** (section 2.4);
- fixes the **emergency rollback** so the retained 5c artifact starts in `definition`, never in its
  legacy default (section 2.3);
- requires the legacy **telemetry baseline to be archived** before 30-day pruning (section 2.5);
- defines the **500-turn gate**: real versus synthetic turns, window, and reset rules (section 2.1);
- keeps `turn_telemetry` and removes only shadow telemetry (section 4.3);
- corrects stale text: 5c already deleted the browser's local decision code (section 6).

## 1. Outcome

Remove the previous conversation engine, its rollback switch and the temporary shadow-comparison
machinery. After 5d there is one backend conversation path: the definition-driven engine accepted
in 5c.

This slice deliberately ends **configuration-only rollback**. It is not a refactor to perform while
5c is still being evaluated. Cleanup is complete only when:

1. no production, test, operator or product-package import can reach the previous engine;
2. `PIXEL_ENGINE_MODE`, `PIXEL_SHADOW_ENGINE`, `PIXEL_TESTING_ENTRY` and their branching code are
   gone;
3. every behaviour the legacy engine owned has a decided owner (section 5), and old-engine tests are
   replaced by equal or stronger definition-engine coverage before deletion;
4. every 3.2 product-coupling allowlist entry is either removed with its file or honestly relabelled
   to the later milestone that still uses it (section 4.4);
5. the full product, including voice, works with the definition engine as the only authority;
6. shims assigned to later milestones remain in place and are not mislabelled as legacy-engine code.

## 2. Hard entry gates

5d coding may start only after the owner explicitly accepts the loss of switch rollback. The pull
request may merge only when every gate below has recorded evidence in the 5c report or the 5d PR.

### 2.1 5c production acceptance

- **Volume.** Definition authority has served at least **500 accepted turns across at least 50
  distinct sessions**, covering every workflow in the 5c matrix (5c plan, section 2.2).
- **Real versus synthetic.** At least **250 turns and 25 sessions come from real visitors**.
  Synthetic sessions may fill the rest and any workflow real traffic did not reach; they use the
  public visitor path, no paid model call, and are labelled as synthetic in the evidence.
- **Window.** The turns span at least **7 consecutive days** of definition authority, and include at
  least one API restart or redeploy while conversations were open.
- **Reset rules.** The count restarts from zero after any rollback to legacy authority, any section
  11.4 rollback trigger of the 5c plan, any definition-version move, or any change to the definition
  engine's code. Turns served before the reset do not count.
- **Quality.** No isolation failure, unauthorized write, duplicate execution, dishonest completion
  claim or definition-pin bypass. Definition-engine 5xx rate at or below 1%. Latency and fallback
  within the 5c limits, measured against the archived legacy baseline (section 2.5).
- Every model/provider failure observed has a deterministic, bounded outcome. Paid model use is not
  required; if enabled, its budget and kill-switch evidence is attached.
- All `behaviour`, `coverage`, `security` and `lifecycle` differences are closed or explicitly
  accepted in the signed 5c report.

### 2.2 Rollback is rehearsed before it is removed

Using the release candidate and one populated database:

1. run a session in definition mode;
2. switch to legacy and restart without a data command;
3. continue the same session and verify the documented pending-state limitation;
4. switch back to definition and continue again;
5. verify records, execution ledger, definition pins and private-instance generation are unchanged
   except for the deliberate test actions.

The rehearsal is the final proof that 5c's safety net works. Only after it passes may 5d remove it.

### 2.3 Recovery remains operational

- A fresh encrypted database backup has a verified restore.
- The accepted 5c application artifact, environment manifest and deployment instructions are
  retained as an **emergency artifact rollback**, not as a supported runtime switch.
- **The 5c artifact defaults to legacy authority when `PIXEL_ENGINE_MODE` is unset.** Its retained
  environment manifest therefore sets `PIXEL_ENGINE_MODE=definition` explicitly, and the recovery
  instructions require checking that `/health` reports `"authority": "definition"` before traffic.
  Restoring the 5c artifact must never bring the legacy engine back by default.
- No 5d database migration is destructive. The 5c artifact can still start against the additive
  schema during the immediate deployment window.

### 2.4 The kept engine is the one the browser proves

Before any removal, the complete browser suite (`tests/e2e/**`) and the browser golden
(`products/linear_simplified/tests/browser-golden.spec.ts`) pass with the isolated test API under
**definition authority**. Today only `keyed-writes.spec.ts` runs the definition engine; the others
run the legacy default, so a green browser suite does not yet prove the engine 5d keeps.

- The browser golden under definition authority is compared with `browser_decisions.json`; every
  difference is listed in a reviewed file for this authority, and none is regenerated to pass.
- Every behaviour-matrix row (section 5) marked *port* is green in this run before its legacy code
  is deleted.
- Linux CI runs this definition-authority browser job on the 5d pull request, in addition to the
  normal suites.

### 2.5 Evidence survives its retention

`turn_telemetry_daily` keeps 30 days. Before the first legacy-baseline rows would be pruned, and
again at the end of the section 2.1 window, the operator exports
`python -m app.ops cutover-report --days 30` for both authorities and attaches it to the 5c report.
The latency and fallback comparison in section 2.1 uses these archived reports, not whatever
telemetry happens to remain.

### 2.6 Behaviour ownership is decided

Every row of the behaviour matrix (section 5) has an owner decision recorded in the 5c report or
the 5d PR: *port* (with its destination and test), *accept* (with the owner's approval of the
changed behaviour), or *5c blocker* (fixed before cutover). No row may be undecided.

The decisions are made **before the first deletion commit**, not while deleting: the 5d PR may not
remove any legacy module until every *decide* row carries its recorded decision. A decision taken
under deletion pressure defaults to whatever the remaining code happens to do, which is exactly the
drift this gate exists to prevent. The *decide* rows open today are: guided path, correction
wording, create without an owner (highlight or not), update with nothing open, and quoted names in
replies.

## 3. Deletion principles

- A file is deleted only after its importer list (section 4) shows no supported caller, and the
  import-graph and source-only tests (section 8) pass without it.
- The ledger in section 4 is checked in with the PR as a machine-readable manifest; the manifest,
  not prose, is authoritative. A file missing from it fails the manifest test.
- Removal proceeds in dependency order, one reviewable commit per group, each leaving a runnable
  application.

## 4. Delete / keep / move ledger

Importers were measured from the repository at 5c (`apps/api/app`, `products`); they are rechecked
when the manifest is produced.

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
| `services/demo_data.py` | legacy modules only | becomes dead with the legacy engine; its 3.5 allowlist entry leaves early, and any record helper a kept module needs moves to `product_data_store.py` first |
| `testing_main.py` and `PIXEL_TESTING_ENTRY` | test harness only | the harness starts the one app entry; no second entry remains |
| `ExecutionLedger.dispatch_if_current` | `legacy_adapter.py` | with the adapter |
| `SessionManager.recent_user_messages` | `agent.py` | with `agent.py`, unless the name-memory row (section 5) is ported to use it |
| legacy-only tests and runners (`test_agent.py`, `test_backend_conversation.py`, legacy parts of `test_engine_cutover.py`, `golden_backend.py`, the live side of `golden_shadow.py`) | tests | only after section 7 coverage transfer |

### 4.2 Delete (shadow and switch)

- `services/shadow.py` (runner, controller, scheduler, memory store, breaker, watchdog) and its
  tests; `PIXEL_SHADOW_ENGINE`; shadow startup/shutdown hooks and background counter flushing.
- `services/shadow_parity.py` writes and the live `shadow-report` command. The table
  `shadow_parity_daily` stays **dormant** (not dropped), documented, until a later maintenance
  migration removes it after retention and backup requirements are met.
- `PIXEL_ENGINE_MODE`, `engine_mode()`, `turn_engine()`, the `live_turn` path, the authority field in
  `/health`, and the `TurnResponse._engine_entered` private attribute (shadow-only).
- No hidden header, test flag, admin endpoint or undocumented environment escape hatch may re-enable
  the old engine.

### 4.3 Keep

| Kept | Why |
| --- | --- |
| `services/retriever.py` | imported by the definition engine's knowledge package `products/linear_simplified/backend/knowledge.py`; replaced in 3.4 |
| `services/language_normalizer.py` | imported by `retriever.py`; leaves with it in 3.4 |
| `product_config.py` | imported by `retriever.py`; leaves with it in 3.4 (its other uses move, section 4.5) |
| `workspace_config.py`, `services/product_data_store.py`, demo-data endpoints and write shims | records and scopes, replaced in 3.5 |
| product action translator and web action schema | replaced in 3.6 |
| product web adapters and rendering shims | replaced in 3.6 |
| execution ledger, receipts, `engine_state` and its retention, provider budgets, rate limits, speech | platform contracts |
| `services/turn_telemetry.py` and `ops cutover-report` | the only per-turn health evidence; the authority dimension becomes a constant, and the command is renamed to a turn report |
| `services/model_gateway.py`, strict parser, provenance, confirmation boundary, `PIXEL_MODEL_GATEWAY` | the generic model path (5c section 8) |
| installed product registry and every published definition, with v4 bound | product definitions |
| `shadow_parity_daily` table (dormant) and all reviewed-difference files | audit evidence |

### 4.4 Allowlist (`KNOWN_PRODUCT_COUPLING`) changes

- Removed with their files: `services/agent.py`, `action_planner.py`, `action_validator.py`,
  `agent_reasoner.py`, `conversation_manager.py`, `intent_extractor.py`, `services/demo_data.py`
  (its 3.5 entry leaves early because the file is gone), `services/legacy_adapter.py`.
- **Relabelled, not removed:** `services/language_normalizer.py` and `product_config.py` move from
  3.2 to **3.4**, because the kept retriever still imports them. The allowlist test records the new
  owner milestone; it is not an exception to the shrink rule.
- Entries assigned to 3.3, 3.4, 3.5 and 3.6 remain.

### 4.5 Move

| From | To | Why |
| --- | --- | --- |
| `main.py` speech: `PRODUCTS_BY_ID[definition_id].voice_style` | the pinned definition's `identity.voice_style` | voice must not depend on the legacy product configuration; every published version declares it |
| any `demo_data.py` helper still needed by a kept module | `product_data_store.py` | keeps 3.5 record code in one place |
| behaviour rows marked *port* (section 5) | the product definition (routing, prefill) or the platform composer/engine (generic wording) | never re-implemented as product logic in core |

## 5. Behaviour ownership matrix

Every reply the legacy engine owns after 5c, sent through both authorities on the golden seed
(`test_engine_cutover` fixture, product engineering workspace). *Blocker* rows are defects of the
definition engine that visitors would meet **on 5c cutover itself**; they must be fixed before the
5c switch, not deferred to 5d. All four are fixed and tested under definition authority. *Port* rows move before their legacy code is deleted. *Decide* rows
need an owner decision to accept or port.

| Behaviour | Legacy (today) | Definition engine (today) | Class | Proposed owner |
| --- | --- | --- | --- | --- |
| "start a ticket assigned to Noah" | opens the ticket form prefilled with Noah Patel | was: **opened Noah's existing ticket LIN-137**. Now: drafts a create for Noah Patel and asks for the title, then the project | blocker, **fixed** | Linear v4 routes start/draft, and open with new/assign, as a create (`test_backend_conversation`) |
| "open a ticket for Maya and assign to Jen" (Jen unknown) | asks to add Jen first | was: **opened LIN-142**. Now: highlights add-member for Jen, nothing drafted | blocker, **fixed** | router: a create is never drafted while a named person is unknown; the wording is the *port* row "create for an unknown owner" |
| "what did we just change?" | ledger answer | was: **answered from product documentation**. Now: ledger answer | blocker, **fixed** | platform last-change cues |
| "I'm an engineering manager ... moving from Jira" | acknowledges, records signals | was: "I'm not sure how to help with that". Now: acknowledges, records signals | blocker, **fixed** | engine: a message that only describes the visitor is acknowledged; a request in it is still served |
| greeting remembers the visitor's name | "Hi Pushkar ..." | generic greeting | port | engine: greeting_named from session memory |
| guided path | lists the demo path, opens Dashboard | opens Dashboard, no path | decide | definition response or accept |
| next step | page-aware suggestion | product documentation passage | port | engine: next-step answer |
| project / team count | states the count and names | opens the view only | port | composer: `anchor_count` / `people_count` answers |
| correction ("not cycles, ...") | "Got it. I'll switch to Issues." | "I'll open Issues." | decide | wording only |
| member add (named / unnamed) | explicit sentence; asks for a name | "I'll highlight the requested control in Teams." | port | composer wording for highlight with a named person, and a question when unnamed |
| create without an owner | asks and highlights the create button | asks, no highlight | decide | behaviour: highlight or not |
| create for an unknown owner | asks to add them first | highlights add-member, generic sentence | port | composer wording |
| filter by person / "what about Noah" | names the count and IDs | "I'll filter the available records." | port | composer: filtered answer with count |
| update with nothing open | "which ticket: Maya's, Noah's, or the open one?" | "Which ticket do you mean?" | decide | wording only |
| quoted names in replies | plain names | `Welcome to "Pixel". I am "Edith"`; quoted field keys in capabilities | decide | a platform wording choice from 4b; approve or change before cutover |
| broad workspace request | completed | denied | accept | the v2 guardrail decision |
| out-of-scope person | unknown person, ticket list | unknown person, ticket list | same | none |

The matrix is regenerated from the probe for the 5d PR, and every row carries its decision and test.

## 6. Browser decisions (corrected)

5c removed the browser's local decision code outright: every visitor message reaches `/api/turn`,
`localTurnResponse` and the local intent handlers no longer exist, and a web unit test fails if they
return. Milestone 3.3 therefore no longer deletes them; its remaining browser work (for example the
parsing that prefills the add-member form from the visitor's text) is re-scoped in its own plan. 5d
does not touch the browser beyond removing test-entry routing.

## 7. Coverage transfer before deletion

Deleting old tests is allowed only after their behaviour has a destination:

1. Inventory every old-engine test and golden case.
2. Classify it as preserved (definition-engine replacement test), intentionally changed (reviewed
   golden difference and a behaviour-matrix decision), obsolete implementation detail, or
   security/regression behaviour that is never dropped.
3. Add or identify the replacement before deleting the old test.
4. A machine-readable coverage-transfer manifest fails when an old case has no classification, a
   replacement path does not exist, or two old cases map to nothing.

Security, isolation, cancellation, confirmation, replay, provider-budget and response-integrity
tests are never classified as obsolete merely because they were first found through the old engine.

The canonical product golden becomes the accepted definition-engine recording. It is generated only
for inspection; reviewed expected files are never overwritten automatically.

## 8. Purity and dependency closure

- Starting from the API entry point, no transitive import reaches a deleted module.
- Starting from `app.engine`, no transitive import reaches product data, product packages, network,
  database, provider, speech or execution modules except the approved registry boundary.
- Dynamic imports remain banned in engine code.
- A clean interpreter can import the API, run readiness, serve a turn and speak a reply with every
  deleted file physically absent. The test copies the release source without them; stale
  `__pycache__`, editable paths or build output cannot make it pass.

## 9. Single authoritative runtime

After cleanup the turn lifecycle is exactly the accepted 5c lifecycle: authorize and activate;
materialize one scoped snapshot; load durable engine state; route deterministically, optionally
consult the model gateway; validate and compose; finalize state and any key in one transaction;
return one response and execute mutations only through receipts. There is no comparison run,
legacy projection for engine rollback, alternate response mapper or old memory owner. Compatibility
writes (messages, signals, `visitor_context`) remain only where a live feature reads them; dead
writes are removed after a read/write usage test proves them dead.

No product-specific reply or action logic moves into core merely to make deleting a legacy file
easier; it belongs in the product definition or package, or stays a recorded gap.

## 10. Data and schema policy

- Keep additive 5c state and 5b execution schema; do not rewrite session pins, engine state or
  execution outcomes.
- Keep historical messages, signals, usage, parity and execution rows under their retention rules.
- Do not drop old columns or tables in the deployment that removes the code.
- Startup remains forward-compatible and backward-compatible with the immediate 5c artifact.
- Sessions pinned to a definition version the definition engine is not tested on (v1) must have
  expired, or be ended, before deployment; the deployment checks and reports this.

## 11. Configuration, operations and documentation

- Remove `PIXEL_ENGINE_MODE`, `PIXEL_SHADOW_ENGINE` and `PIXEL_TESTING_ENTRY` from `.env.example`,
  CI and runbooks. For one release, startup **refuses to start** on any setting that would select
  the old runtime: `PIXEL_ENGINE_MODE` with any value other than `definition`,
  `PIXEL_SHADOW_ENGINE=on`, or `PIXEL_TESTING_ENTRY` set. `PIXEL_ENGINE_MODE=definition` is
  **accepted with a startup warning** for that release, because it is the one value that matches
  the 5d runtime.
- **Why `definition` must stay accepted.** In the 5c build an unset `PIXEL_ENGINE_MODE` means
  `legacy`. If 5d refused the variable outright, production would have no safe order: removing it
  first silently puts the running 5c build back on the legacy engine, and keeping it makes 5d refuse
  to start. It would also break rollback, because the retained 5c artifact (section 2.3) must run
  with `PIXEL_ENGINE_MODE=definition`. So production keeps `PIXEL_ENGINE_MODE=definition` through
  the 5d deployment and its rollback window, and the variable is removed from the production
  service only in the following release, once rollback to the 5c artifact is retired.
- **Configuration and documentation change together.** The same PR that adds the refusal updates
  `.env.example`, the CI workflow, the deployment runbook and the 5c operator steps, and its
  description lists every place a retired variable was removed or kept, and why. Before deploying,
  the operator confirms production has `PIXEL_ENGINE_MODE=definition`, no `PIXEL_SHADOW_ENGINE=on`
  and no `PIXEL_TESTING_ENTRY`, and records `/health` before and after.
- Preserve independent model, speech, provider-budget and paid-provider kill switches.
- Operator surface: keep version movement, readiness, data maintenance, usage and the turn report;
  remove `shadow-report` and any command that instantiates the old engine.
- Documentation: mark 5c accepted with its evidence; state that 5d ends switch rollback and name the
  emergency artifact procedure (section 2.3); update architecture wording to one engine; keep
  historical slice reports unchanged.

## 12. Test plan

- **Negative existence:** deleted modules and retired switches do not exist; repository search finds
  no import, string, doc instruction or CI reference that selects the old runtime; startup refuses
  every setting that would select the old runtime, and starts with a warning on
  `PIXEL_ENGINE_MODE=definition` (section 11).
- **Coverage transfer:** every deleted case is classified; every preserved or security case points
  to a passing replacement; reviewed differences exactly match the canonical golden.
- **Runtime:** all 5c authority-independent contracts (state, required fields, model boundary,
  response mapping, execution, isolation matrix, restart, races, cancellation, confirmation,
  replay, private instances, keyless forms, voice) stay green with the deleted files absent.
- **Browser:** the full browser suite and golden, now with a single engine, as in section 2.4.
- **Voice:** a reply is spoken with the voice style from the pinned definition.
- **Packaging and deployment:** source-only and container builds start without removed files; a
  populated 5c database starts with the new artifact; the retained 5c artifact starts against the
  same database with `"authority": "definition"` (section 2.3); the public smoke matrix passes.

## 13. Implementation order

1. Produce the dependency, ledger, behaviour-matrix and coverage-transfer manifests without
   deleting code.
2. Close section 2.4: run the browser suite and golden under definition authority; port the rows
   marked *port*; record the reviewed differences.
3. Move voice style to the definition; move any kept `demo_data` helper.
4. Remove shadow runtime, reporting hooks and retired configuration references.
5. Remove the authority switch, `live_turn`, `testing_main` and the adapter; build the definition
   service unconditionally.
6. Remove legacy modules and legacy-only tests in dependency order.
7. Apply the allowlist changes of section 4.4 and strengthen source-only and import-graph tests.
8. Remove dead compatibility writes proven unused; leave the schema intact.
9. Update operator commands, environment examples, architecture and runbooks.
10. Run clean-source, populated-database, artifact-rollback and full regression rehearsals.
11. Merge only after Linux CI is green, deploy, run the public smoke matrix and record sign-off.

Each commit keeps a runnable application; no single deletion commit makes failures impossible to
localize.

## 14. Rollout and incident policy

1. Back up and restore-check the production database.
2. Deploy the new-only artifact without changing provider, model or voice settings.
3. Require readiness, then run the complete public smoke matrix.
4. Review the first 100 accepted turns and at least 60 minutes of the turn report.
5. On an isolation, mutation-integrity, startup or sustained availability defect, deploy the
   retained 5c artifact **with `PIXEL_ENGINE_MODE=definition`** and confirm `/health` before traffic.
6. Do not reintroduce copied old modules into main. A defect is fixed in the definition engine, or
   the deployment is temporarily rolled back to the signed 5c artifact.

## 15. Exit criteria

5d is signed off only when:

1. Every entry gate in section 2, and the owner's explicit end-of-switch-rollback approval, are
   recorded.
2. The old engine, authority switch, test entry and shadow runtime are absent from source and release.
3. Every behaviour-matrix row has its decision implemented and tested.
4. Every deleted test or case is accounted for by the coverage-transfer manifest.
5. Allowlist entries are removed or relabelled exactly as section 4.4 states.
6. Transitive dependency, purity, source-only import and dynamic-import tests are green.
7. Every 5c security, memory, response, model and execution contract remains green.
8. API, product, web, production build, browser (single engine) and Linux CI suites pass.
9. A populated database starts without destructive migration, and the retained 5c artifact starts
   in `definition` against it.
10. Production smoke and turn telemetry pass with one engine and no retired configuration.
11. Documentation states plainly that configuration-only rollback has ended.

## 16. Risks and non-goals

- **Behaviour can disappear during deletion.** The behaviour matrix and the definition-authority
  browser gate are mandatory, not advisory.
- **Coverage can disappear during deletion.** The transfer manifest and replacement-first order are
  mandatory.
- **A hidden import can survive.** Source-only artifact tests and transitive graph traversal catch
  what ordinary imports miss.
- **Removing switches can be mistaken for removing safety controls.** Provider, model, speech,
  budget and execution kill switches remain.
- **Emergency rollback can restore the removed engine by default.** Section 2.3 pins it to
  `definition`.
- **Schema cleanup can make artifact rollback impossible.** No destructive schema removal occurs.
- **The product is not fully generic after 5d.** Knowledge (3.4), records (3.5) and web rendering
  (3.6) keep their migrations; `retriever`, `language_normalizer` and `product_config` leave in 3.4.
- **5d adds no features.** A behaviour row is ported only to preserve what visitors already have.
