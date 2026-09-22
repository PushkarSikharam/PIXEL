# Milestone 3.2, Slice 5c: Authoritative Cutover

Status: **BUILT, CUT OVER, ACCEPTANCE WINDOW STILL OPEN.** Every code step of the plan's
implementation order (section 13, steps 1 to 10) is built and tested. Production now runs
definition authority, live smoke passes, and the first production defects found after cutover are
fixed and deployed. Full sign-off still needs the 5d entry evidence: the 500 accepted-turn window,
the real-versus-synthetic split, a restart or redeploy during open conversations, archived
telemetry, the retained 5c rollback artifact checklist, and the owner's explicit approval to end
switch rollback.

Date: 2026-09-22. Plan: `docs/MILESTONE_3_STEP_3_2_SLICE_5C_PLAN.md`. The first part landed in
PR #21; follow-up changes closed the findings of the review of that pull request and the first live
production defects (listed below).

## What was built

| Part | Where |
| --- | --- |
| Authority switch `PIXEL_ENGINE_MODE` (`legacy` default, `definition`), fixed at startup, reported by `/health` | `apps/api/app/main.py` |
| Definition turn service with one-transaction finalization | `apps/api/app/services/turn_execution.py` |
| Shared engine assembly | `apps/api/app/services/engine_assembly.py` |
| Durable engine state, in-process pending cache, workspace-scoped signal history, retention | `apps/api/app/services/engine_state.py` |
| Required-field completion for creates | `apps/api/app/engine/field_completion.py`, `apps/api/app/engine/conversation_engine.py` |
| Linear v4 (priority and status defaults; project always asked) and v5 (routing additions) | `products/linear_simplified/definition/v4.yaml`, `v5.yaml` |
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

- API suite: 965 tests pass (3 skipped), including the cutover visitor script run in-process under both authorities. Product suite: 104 tests pass. Web unit tests: 44 pass; type-check clean.
- Browser suite (Playwright, isolated servers): 116 tests pass under legacy authority and 116 under
  `PIXEL_ENGINE_MODE=definition`, each including the browser golden checked strictly against its
  own reviewed list.
- Golden evidence, never regenerated: backend recording 68 reviewed differences, shadow 211,
  browser 59. Each names its kind and reason; the tests fail on an unlisted or stale difference.

## Production evidence recorded so far

Production is cut over to definition authority, but this is not the 5d acceptance window yet.

- PR #25 merged on 2026-09-22 as merge commit `d493bb6`, after the follow-up fix for public
  guardrail reasons in definition traces.
- Main CI run `35778171127` is green: API tests, web checks and browser tests all passed. The
  browser job ran both authority modes, with **116 passed** in each run.
- Railway API deployment `250f3d04-a3ac-4582-81e4-aad946ae5656` is online in production.
- Live `/api/agent/health` reports `{"status":"ok","authority":"definition","shadow":"off"}`.
- `PIXEL_LIVE_URL=https://linear-simplified-web.vercel.app node scripts/smoke-live.mjs` passes:
  health ok, separate visitor instances, administrator login refused, private writes isolated,
  private reset rotates the generation, a normal turn completes, the Salesforce guardrail is
  denied for the expected reason, global reset is refused, and speech is not requested.

This evidence proves the deployed system is healthy enough to begin collecting the 5d acceptance
window. It does **not** prove the 500-turn gate.

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

### Browser suite under definition authority

A first local run with `PIXEL_ENGINE_MODE=definition` failed (24 failed, 38 passed, 54 not run).
The change that followed made the browser pass by breaking platform rules, and was reviewed as not
mergeable. Its findings, and how each is now closed:

1. **Linear demo content in the platform core.** The composer spoke a scripted guided path naming
   Maya, Noah and Salesforce to every product and workspace, and the engine hard-coded tickets,
   issues, members, projects, Teams and the assignee control. All of it is removed. Counts, the
   person filter, owner questions and control names are now read from the definition (the opened
   view's entity and its labels, the field a filter uses, the control's declared label); the guided
   path is drawn from the caller's filtered offers. The core-purity and platform-wording tests pass.
2. **Conversation answered before routing.** "Help me create a ticket for Noah ...", a title
   mentioning "the next step" or "voice", and "delete the contact, what can you do?" were answered
   as conversation. Routing is first again; conversation is answered when routing falls back, or
   when routing only produced a question back (which writes nothing), so "are you capable of doing"
   is answered while a refusal or a request never is.
3. **Replies that could crash.** New templates needed `{control}` and `{view}` values that some
   paths (including a replaced model sentence) never supply. Every lifecycle rendering now has a
   safe control description, and the found-records answer needs no view.
4. **A published definition edited in place.** v4 had been merged, and edits to it would make every
   v4 turn fail its checksum in any environment that registered it (reproduced locally: the API
   starts, then every v4 turn raises `DefinitionError`). v4 is back to its merged bytes, and the
   additions are **Linear v5**, which the seed now binds. v5 also routes "open the dashboard",
   which no version did.
5. **Navigation could not leave an unfinished create.** "Show me the cycles" became the ticket's
   title. Navigation phrased as navigation now interrupts a free-text question; a title that names a
   product area ("Investigate customer onboarding issue") is still read as the title.
6. **Weakened golden evidence.** The legacy browser golden had stopped checking stale and
   mismatched entries and ignored every reply difference; 52 entries shared three boilerplate
   reasons, and wording changes were labelled as security. The spec is back to its strict form and
   the legacy list to its reviewed version. Definition authority has its own equally strict list,
   `golden/browser_differences_definition.json`: 111 entries, each with a case-specific reason
   (49 wording, 59 behaviour, 3 security). Open owner decisions are named as pending, not accepted.
   Two e2e assertions that accepted either engine's sentence are exact again.

Port rows completed after that review, each tested under definition authority:

- A later greeting uses the name the visitor introduced themselves with in this session. The name
  is kept only in that session's engine state (an additive `engine_state.visitor_name` column),
  only when short and plain, and is removed with the state's normal retention.
- "What should I try next?" suggests what can be done from the open view, drawn from the filtered
  offers; a view the definition does not declare is never trusted, and the Dashboard gets the
  generic answer.
- "Add a new team member" with no name asks for it, and the answer prefills the add-member form.
  The rule is generic: a control whose only prepared value is a new record's title asks for it.
- In another workspace, "open Maya's ticket" now says the name was not found before opening the
  Issues list, identically for an unknown name.

Still open:

- None. Every owner decision is recorded and applied (below), and the behaviour matrix has no
  unported row left.

## Defects found by the first production run, and how each closed

The operator ran the cutover runbook to step 5 on the live deployment: the visitor script passed
11/11 under legacy authority, a backup was taken and verified, the product moved from v2 to v5, and
117 turns were compared by the shadow across about 90 sessions with no `shadow_error`,
`worker_unhealthy` or `over_budget`, and no `security` class. Ten of those turns were typed by hand
on the live site, and replaying them through the same comparison found defects no suite had:

1. **A complete request was read as the answer to an open question.** After "assign it to Noah"
   left a "which ticket?" question open, "Show me issue assignment" became a filter for Noah. The
   router combines a message with the earlier request only when the message is a fragment; a
   request of three or more words that routes on its own replaces the question.
2. **A lowercase introduction was not recognised.** "Hi there i am pushkar" fell through to the
   fallback. A lowercase name is now accepted where it can mean nothing else: "my name is sam", a
   greeting followed by "i'm priya", or "I'm Sam from Acme". "i am confused" is still not a name.
3. **A question was answered by quoting an unrelated document.** "who build pixel?" was answered
   from the integrations passage, because the shared retriever (which milestone 3.4 replaces)
   counts "who" and "is" as matches. A passage now carries whether it really matches; only a real
   match may be quoted, and a passage that merely supports an action reply is unchanged. Asking who
   the assistant is, or what the product is, is answered by the platform.
4. **Legacy proposed a change for a question.** "what is pixel capable of doing?" proposed
   "I'll update LIN-142: status to In progress" and issued an execution key, because the legacy
   planner reads "doing" as the in-progress status. The visitor's reply was interrupted, so nothing
   was written, and a visitor can only reach their own private copy. The legacy planner no longer
   proposes a record change for a question; legacy remains the rollback path until 5d.
5. **An interrupted reply vanished silently.** Sending a new message while the assistant is
   thinking cancels the unfinished turn by design, and the transcript showed nothing. The page now
   notes "Reply stopped when you sent a new message." on the message it answered. The note is the
   page's own, not speech: the browser still authors no assistant sentence, and the web test that
   enforces that is unchanged.

Two gaps from the behaviour matrix closed with them:

- **An unknown person named in any request** is now offered the control that adds one, prefilled
  with the typed name, not only in a create. The rule is generic: the definition declares exactly
  one highlight over the people entity whose single prepared value is that entity's title. A person
  in another workspace gets the identical reply, which `test_hidden_and_unknown_people_get_the_same_answer`
  now checks for this phrasing too.
- **"I'm new here"** was answered with "Which type of record would you like to create?"; a visitor
  describing their situation is acknowledged instead.

## Owner decisions (5d plan revision 2, section 5)

Recorded from the product owner and applied under definition authority; each is pinned by
`test_the_owners_wording_decisions` and reviewed in `golden/browser_differences_definition.json`.

| Row | Decision | What the visitor now hears |
| --- | --- | --- |
| Quoted names | Plain names | "I'm Edith, your guide to Pixel. Ask me what I can do, or tell me what you'd like to see." Capabilities are listed in plain words ("create a ticket, change a ticket's assignee, priority or status, ..."). |
| Guided path | Conversational, whatever suits the chat | "Here's a good way to explore Pixel: open Cycles, then open Integrations, then create a ticket, then find Add member in Teams, then ask me for something outside Pixel to see how I stay in scope." Drawn from the caller's offers, so it never names a person or record. |
| Correction | Acknowledge it | "Got it. I'll switch to Issues." |
| Nothing open | Whatever feels more human | "Which ticket do you mean? Open it first, or tell me which one." |
| Create without an owner | As legacy | Asks who should own it and highlights the create button (Linear v5). |

A visitor who introduced themselves is greeted again as "Hi Priya, good to see you again. What
would you like to explore next in Pixel?"; the first reply to an introduction stays "Nice to meet
you, Priya."

## Open before 5d deletion

5c is live under definition authority, but the project is not cleared to remove rollback yet. The
remaining gates are evidence gates, not feature work:

1. Build and run the 5d `acceptance-report` command so the acceptance window is measured from the
   database rather than remembered manually.
2. Collect at least 500 accepted definition-authority turns across at least 50 sessions, with at
   least 250 turns and 25 sessions from real visitors.
3. Keep the window open for at least 7 consecutive days, including at least one API restart or
   redeploy while conversations are open.
4. Archive the 30-day turn report before retention can prune the rows needed for comparison.
5. Record the retained 5c artifact exactly: annotated `5c-accepted` tag, Railway deployment ID,
   image digest, environment checklist, encrypted backup sha/location and proof the artifact still
   starts in definition authority.
6. Get the owner's explicit approval that 5d ends switch-based rollback.

Only after those gates are recorded should 5d-3 remove the authority switch. The next allowed work
is 5d-1: evidence tooling and manifests.

## Deliberate limits

- The model gateway ships with no transport. Enabling a paid model is a separate reviewed change
  with its own budget, per section 8.
- One API replica remains required (section 2.4).
- Slice 5d (removing the legacy engine and the switch) may not start until the acceptance above
  exists and the owner explicitly accepts losing switch rollback.
