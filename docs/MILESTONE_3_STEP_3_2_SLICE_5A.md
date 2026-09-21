# Milestone 3.2, Slice 5a: Shadow Mode

Status: **BUILT, NOT SIGNED OFF.** Local suites are green. Sign-off needs green Linux CI on the
pull request, the production version move, and a reviewed `shadow-report` from live traffic
(exit criteria 1, 5 and 7).

Date: 2026-09-21. Plan: `docs/MILESTONE_3_STEP_3_2_SLICE_5A_PLAN.md` (revision 2, approved).
Section 11 of the plan lists where the build differs from its text.

## What was built

The new engine answers every live turn in the shadow of the current engine, on the same records,
and nothing it does reaches a visitor or the database. The switch `PIXEL_SHADOW_ENGINE` is off
unless set to exactly `on`.

| Part | Where |
| --- | --- |
| Conversation engine, a pure function of its inputs | `apps/api/app/engine/conversation_engine.py` |
| Signals, prospect profile, session summary, `SignalHistory` | `apps/api/app/engine/signals.py` |
| Fresh gates and the definition cache | `apps/api/app/engine/definition_cache.py` |
| Person follow-up ("what about Noah") | `apps/api/app/engine/router.py` |
| Shadow runner and in-process memory store | `apps/api/app/services/shadow.py` |
| Comparison classes and parity counts | `apps/api/app/services/shadow_parity.py` |
| Count table `shadow_parity_daily` | `apps/api/app/db.py` |
| Hook in the turn endpoint | `apps/api/app/main.py` (`create_turn`) |
| Operator commands `move-product-version`, `shadow-report` | `apps/api/app/ops.py` |
| Linear v2 | `products/linear_simplified/definition/v2.yaml` |
| Offline parity over the golden conversations | `products/linear_simplified/tests/golden_shadow.py` |
| Reviewed shadow differences | `products/linear_simplified/tests/golden/shadow_differences.json` |

## Evidence (local, Windows development machine)

- API suite: 804 tests pass (3 skipped). Product suite: 102 tests pass.
- Inertness: the whole golden set run with the shadow on and off gives identical live responses
  and identical table contents (times and generated IDs normalized). No write happens while the
  shadow compares a turn (a SQLite authorizer denies every insert, update and delete). The shadow
  modules import no provider, speech, model or execution code. An exception inside the engine is
  counted as `shadow_error` and the live answer is unchanged.
- The switch: unset, empty, `off`, `ON!` and `true` never enter the shadow, through the real
  `/api/turn` endpoint.
- Golden parity: all 63 turns are compared. 228 field differences are listed with a reason:
  111 `platform_wording`, 59 `behaviour`, 30 `coverage`, 22 `security`, 6 `lifecycle`.
- Linear v2 is additive over v1 (identical entities). On the router comparison it answers four v1
  notes and decides the other two; no other turn changes.
- Shadow time per turn, gates to comparison: p50 7.4 ms, p95 12.9 ms, first turn 21.8 ms (parsing
  the definition into the cache). Budget: p95 under 25 ms. **Not yet measured on the production
  image.**

## Production (2026-09-21)

- PR #20 merged; `main` CI run 35632762962 green on all three jobs (API tests, web checks,
  browser tests). Note: the PR was merged before its own CI finished; the `main` run is the
  evidence.
- `move-product-version --version 2` moved `linear-demo` from v1 to v2. The reported checksum
  `725202389f3a1c0f215f6df7fcec931f538ca3ed905c9d3e66603adf11e8dff0` equals the sha256 of `v2.yaml`
  on `main`. Readiness: ready, no problems.
- `PIXEL_SHADOW_ENGINE=on` set. The live smoke test passed unchanged (private instances separate,
  administrator login refused, private write isolated, reset to a new generation, conversation
  completed with `OPEN_CYCLES`, guardrail denied, global reset refused).
- First `shadow-report`: the smoke conversation turn was `compared` (within budget) on v2.
  Status, both actions, retrieved context, session summary and the absent `execution` field
  `match`; speech, trace wording and signal confidence are `platform_wording`, as the golden entry
  for `nav-sprint-planning` expects. No `behaviour`, `coverage`, `gated` or `shadow_error`.
- Counts reach the table on the next flush, at most once a minute and only when a request
  arrives, or at shutdown. With little traffic the report lags; that is expected.

Remaining for sign-off: a `shadow-report` over real visitor traffic, reviewed against
`shadow_differences.json`, with `over_budget` rare.

## Security findings from building the shadow

1. **The shadow must be narrowed to the selected workspace.** The first build converted every
   record the caller's grant allows, while the live engine answers inside the one workspace the
   request selects. The shadow then proposed assigning a ticket to Avery, who is outside the
   selected workspace. Fixed before any production run; covered by the golden cases
   `update-outside-person`, `scope-outside-person` and `scope-platform-outside-person`.
2. **The live engine discloses people outside the scope.** It refuses with "Avery Brooks is
   outside Product Engineering Workspace" and records Avery as a signal, which confirms the person
   exists. The new engine answers exactly as for someone unknown. Listed as `security`; the live
   engine is not changed in 5a.

## Gaps that must close before 5c

1. **Ticket creation is refused by the new engine.** The definition requires priority, project
   and status for a ticket and declares no defaults; the router supplies only title and
   assignee, so the validator refuses (`missing_required_field`). The live engine invents Medium,
   Issue Triage and Todo. The fix is a question for the missing values or declared defaults, never
   a guess. Golden case `create-known-owner`.
2. **Shadow memory is in process.** A restart loses it (counted as `memory_reset`), and it assumes
   one API process, which a test enforces. The authoritative engine needs memory that survives
   restarts, stored as identifiers with bounded retention.
3. **Every production `behaviour` and `coverage` count needs a decision** before cutover.
4. **Intent trace goals.** The live engine derives goals from its own action names; the new engine
   reads goals only from the definition. Counted as `coverage`; the web app must not depend on the
   legacy labels after cutover.

## Rollout (operator steps, in order)

1. Merge with the switch off, after green Linux CI.
2. After the deploy, in the Railway console:
   `cd /app && PYTHONPATH=apps/api python -m app.ops move-product-version --tenant pixel-dev --product linear-demo --version 2`.
   Open sessions keep v1; new sessions pin v2. `--version 1` moves back.
3. Run the smoke test, then set `PIXEL_SHADOW_ENGINE=on` on Railway and run it again.
4. After real use: `python -m app.ops shadow-report --days 7`. Review every `behaviour` and
   `coverage` count against `shadow_differences.json`, and confirm `over_budget` stays rare.
5. The switch can be turned off at any time with no data change.

Counts are flushed at most once a minute; a crash loses at most the last minute of counts.
