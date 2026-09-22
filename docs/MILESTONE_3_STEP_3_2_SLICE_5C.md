# Milestone 3.2, Slice 5c: Authoritative Cutover

Status: **BUILT, NOT SIGNED OFF.** Every code step of the plan's implementation order (section 13,
steps 1 to 10) is built and tested. Sign-off still needs the production gates of section 2, the
inert deployment check of section 11.2 and the acceptance window of section 11.3. **Definition
authority is not enabled in production**, and must not be until those gates have evidence.

Date: 2026-09-22. Plan: `docs/MILESTONE_3_STEP_3_2_SLICE_5C_PLAN.md`. The first part landed in
PR #21; a follow-up change closed the findings of the review of that pull request (listed below).

## What was built

| Part | Where |
| --- | --- |
| Authority switch `PIXEL_ENGINE_MODE` (`legacy` default, `definition`), fixed at startup, reported by `/health` | `apps/api/app/main.py` |
| Definition turn service with one-transaction finalization | `apps/api/app/services/turn_execution.py` |
| Shared engine assembly | `apps/api/app/services/engine_assembly.py` |
| Durable engine state, in-process pending cache, workspace-scoped signal history, retention | `apps/api/app/services/engine_state.py` |
| Required-field completion for creates | `apps/api/app/engine/field_completion.py`, `apps/api/app/engine/conversation_engine.py` |
| Linear v4 (priority and status defaults; project always asked) | `products/linear_simplified/definition/v4.yaml` |
| Legacy authority adapter: keyed legacy mutations, honest proposal wording | `apps/api/app/services/legacy_adapter.py` |
| Legacy engine sees only the selected workspace | `apps/api/app/main.py` (`create_turn`) |
| Backend answers everything the browser used to answer itself | `apps/api/app/services/agent.py` (`_conversational_turn`) |
| Browser sends every message to `/api/turn`; local decision code removed | `apps/web/app/page.tsx` |
| Keyed create fields passed through unchanged; form prefill from the validated action | `apps/web/lib/agent-api.ts`, `apps/web/types/demo.ts` |
| Exhaustive stage-to-status mapping | `apps/api/app/services/turn_execution.py` (`STAGE_STATUS`) |
| Optional model gateway, off by default and without a transport | `apps/api/app/services/model_gateway.py` |
| Cutover telemetry (metadata only) and `ops cutover-report` | `apps/api/app/services/turn_telemetry.py`, `apps/api/app/ops.py` |

## Findings from the review of PR #21, and how each closed

1. **Linear v3 defaulted a new ticket's project to `PRJ-101`.** Every assistant ticket was filed
   under one Product Engineering project, and in the Platform workspace every create was refused.
   v3 is immutable and may already be registered somewhere, so it is left as it is and **v4**
   removes the default. The seed now binds v4; a test fails if the current version ever defaults
   the project again, and a test creates a ticket in each workspace.
2. **Contradictory golden reasons.** The `create-known-owner` shadow entries now carry one
   accurate reason (the definition engine asks for the project; the legacy planner picks one).
3. **The plan claimed no code had changed.** Its status line is corrected; this report exists.
4. **Finalization was not atomic.** The key, the engine state, the messages, the signals and the
   compatibility projection are now written in one `begin immediate` transaction that first
   re-checks the active turn and the state revision; a lost race writes nothing and answers `stale`.
5. **Authority was re-read per request and not reported.** It is fixed at startup and `/health`
   reports `authority` and `shadow`.
6. **Engine state had no retention.** Rows of ended or expired sessions, and rows idle for 30 days,
   are pruned in bounded batches by the same maintenance as the execution ledger.
7. **Rollback, telemetry, per-stage mapping and the model path were not built.** All four are now
   built and tested (sections 11.1.3, 10, 9 and 8 of the plan).

Other defects found and fixed while building this slice:

- Legacy mode refused every assistant create and update in the browser (no execution key existed
  in legacy authority). The legacy authority adapter dispatches keys for them.
- The browser's own conversation handlers wrote assistant changes without a key and said "Done"
  before any write. They are removed; the backend answers those turns.
- The browser silently dropped every keyed create, because it required a record ID the server
  only assigns when the create commits.
- The legacy planner, now answering turns the browser used to intercept, changed the wrong ticket
  in "assign Maya's ticket to Noah", created a ticket for "make Noah's ticket high priority", and
  guessed a ticket for "assign it to Noah" with nothing open. All three are corrected.
- **Security:** the legacy engine named people who work in another workspace ("Avery Brooks is
  outside ..."). It now sees only the selected workspace, so such a person reads exactly like an
  unknown one, as the definition engine already answers.
- An uncommitted edit to the published Linear v2 (defaults, including the project guess) would
  have changed its checksum under production sessions. v2 is back to its published bytes.

## Evidence (local, Windows development machine)

Filled in from the final local run of this change; Linux CI on the pull request is the gate.

- API suite: 947 tests pass (3 skipped). Product suite: 104 tests pass. Web unit tests: 44 pass; type-check clean.
- Browser suite (Playwright, isolated servers): 116 tests pass, including the browser golden recording.
- Golden evidence, never regenerated: backend recording 68 reviewed differences, shadow 211,
  browser 59. Each names its kind and reason; the tests fail on an unlisted or stale difference.

## Definition-engine defects found after this build (fixed)

Probing every reply the legacy engine owns through both authorities (5d plan revision 2,
section 5) found four definition-engine defects a visitor would have met as soon as definition
authority was switched on. All four are fixed, and `DefinitionAuthorityConversationTest` in
`apps/api/tests/test_backend_conversation.py` runs each through `/api/turn` under
`PIXEL_ENGINE_MODE=definition`:

1. "Start a ticket assigned to Noah" opened Noah's existing ticket (LIN-137). Linear v4 now routes
   "start" or "draft" a ticket, and "open" a ticket with "new", "fresh" or an assignment, as a
   create: the engine drafts it for Noah Patel and asks for the title. "Open a ticket for Noah"
   still opens LIN-137. v4 was edited in place because it has never been merged or registered.
2. "Open a ticket for Maya and assign to Jen" (Jen unknown) opened LIN-142. The router now never
   drafts a create while a named person is unknown, whoever else is named, and v4 offers adding
   the unknown person for every create phrasing. Nothing is drafted or dispatched.
3. "What did we just change?" answered from product documentation. The platform last-change cues
   now include the "we" phrasings and "what just happened"; the answer reads the ledger, before and
   after a committed change.
4. A profile statement ("I'm an engineering manager ... moving from Jira") got the fallback, and
   with "Jira" in it would have opened Integrations. A message that only describes the visitor is
   now acknowledged ("Thanks, that helps. What would you like to explore first in Pixel?") and
   its signals are recorded; a request in the same message ("I'm a manager, show me the
   projects") is still served, and a mutation, refusal or pending question is never replaced.

The shadow comparison also showed that a create drafted around a named person did not record that
person as a signal, so a later follow-up lost them; it is recorded now. Shadow differences were
revised, not regenerated: the stale v3 reasons for `create-start-assigned` are rewritten, one
difference no longer occurs and was removed, one new one (the pending title question in the
summary) is listed with its reason, and two signal entries are reclassified as confidence-only.

The browser suite and browser golden have also only proven the legacy engine; they must pass under
definition authority before cutover (5d plan revision 2, section 2.4).

## Open before sign-off (operator actions)

These cannot be produced by code, and none has evidence yet:

1. Confirm on Railway that `PIXEL_ENGINE_MODE` is unset or `legacy`, and check `/health` reports
   `"authority": "legacy"`.
2. Section 2.2: a shadow report of at least 100 compared turns across 20 fresh sessions, with the
   workflow matrix, zero `shadow_error` and `worker_unhealthy`, and every difference decided.
3. Section 2.4: an encrypted database backup, one verified restore, and the prior artifact ready.
4. Section 11.2: deploy inertly, then publish and bind v4 for new sessions with
   `python -m app.ops move-product-version --tenant <tenant> --product <product> --version 4`.
5. Section 11.3: switch to `definition`, run the visitor script, and review the first 100 turns and
   at least 60 minutes of `python -m app.ops cutover-report`, against the section 11.4 triggers.

## Deliberate limits

- The model gateway ships with no transport. Enabling a paid model is a separate reviewed change
  with its own budget, per section 8.
- One API replica remains required (section 2.4).
- Slice 5d (removing the legacy engine and the switch) may not start until the acceptance above
  exists and the owner explicitly accepts losing switch rollback.
