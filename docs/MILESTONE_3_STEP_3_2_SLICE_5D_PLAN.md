# Milestone 3.2, Slice 5d: Legacy Engine Removal — Implementation Plan

Status: **architecture-reviewed plan, awaiting owner approval. No 5d runtime code has changed.**
Date: 2026-09-21. Parent plan: `docs/MILESTONE_3_STEP_3_2_PLAN.md` revision 4.3. Prerequisite:
slice 5c is merged, signed off and accepted in production under the gates in section 2.

## 1. Outcome

Remove the previous conversation engine, its rollback switch and the temporary shadow-comparison
machinery. After 5d there is one backend conversation path: the definition-driven engine accepted
in 5c.

This slice deliberately ends **configuration-only rollback**. It is not a refactor to perform while
5c is still being evaluated. Cleanup is complete only when:

1. no production, test, operator or product-package import can reach the previous engine;
2. `PIXEL_ENGINE_MODE`, `PIXEL_SHADOW_ENGINE` and their branching code are gone;
3. old-engine tests are replaced by equal or stronger definition-engine coverage before deletion;
4. the 3.2 entries leave the product-coupling allowlist;
5. the full product still works with the definition engine as the only authority;
6. shims assigned to later milestones remain in place and are not mislabeled as legacy-engine code.

## 2. Hard entry gates

5d coding may start only after the owner explicitly accepts the loss of switch rollback. The pull
request may merge only when all gates below have evidence.

### 2.1 5c production acceptance

- Definition authority has served at least **500 accepted production turns across at least 50
  sessions**, including every workflow in the 5c matrix.
- No isolation failure, unauthorized write, duplicate execution, dishonest completion claim,
  definition-pin bypass or rollback trigger occurred.
- Definition-engine 5xx rate is at or below 1%; latency and fallback rates remain inside 5c's
  accepted limits.
- Every model/provider failure observed has a deterministic, bounded outcome. Paid model use is not
  required, but if enabled its budget and kill-switch evidence is attached.
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

The rehearsal is the final proof that 5c's safety net actually works. Only after it passes may 5d
remove that safety net.

### 2.3 Recovery remains operational

- A fresh encrypted database backup has a verified restore.
- The accepted 5c application artifact, environment manifest and deployment instructions are
  retained as an emergency artifact rollback, not as a supported runtime switch.
- No 5d database migration is destructive. The 5c artifact can still start against the additive
  schema during the immediate deployment window.

## 3. Deletion ledger

Create a checked-in deletion manifest before removing code. Every entry records its replacement and
the test that protects the replacement.

### 3.1 Remove after dependency proof

Expected old-engine production modules:

- the old agent orchestrator;
- old action planner and validator;
- old intent extractor, language normalizer, reasoning policy and conversation manager;
- the old product-specific LLM reasoner and its prompt/action parsing logic;
- the 5c legacy authority adapter and rollback-only compatibility orchestration;
- old hard-coded product configuration once all remaining callers use the installed product
  directory/package;
- startup construction and globals used only by the old engine;
- mode-selection branch and legacy readiness checks;
- tests, fixtures and golden runners whose only subject is the removed engine.

Names are discovered from the dependency graph at implementation time; the manifest, not this
human list, is authoritative. A file is deleted only after `rg`, import-graph tests and runtime
entry-point tests show no remaining supported caller.

### 3.2 Remove shadow-only machinery

After the 5c acceptance report is archived:

- remove the 5a shadow runner/controller, scheduler, memory store, comparator and circuit breaker;
- remove `PIXEL_SHADOW_ENGINE`, shadow startup/shutdown hooks and background counter flushing;
- remove the live `shadow-report` operator command and shadow-only test entry points;
- stop writing `shadow_parity_daily`.

Do not destructively drop the historical parity table in 5d. Leave it dormant and documented; a
later maintenance migration may remove it after retention and backup requirements are satisfied.
The signed 5a/5b/5c reports and reviewed difference files remain as audit evidence unless a later
documentation archive explicitly replaces them.

### 3.3 Remove the authority switch

- Delete `PIXEL_ENGINE_MODE` parsing and all `legacy` branches.
- Build the definition turn service unconditionally during lifespan.
- Readiness verifies the one engine and its dependencies; it no longer reports a selectable mode.
- Remove switch-only compatibility tests after their evidence is captured in the 5c report.
- Do not keep a hidden header, test flag, admin endpoint or undocumented environment escape hatch
  that can re-enable the old engine.

## 4. Explicit keep ledger

These are **not** removed in 5d:

- the product action translator and hard-coded web action schema, required until 3.6;
- the browser-side decision source, already unreachable from `sendMessage` after 5c and deleted in
  3.3;
- product knowledge/retriever shims, replaced in 3.4;
- product record store, scope configuration, demo-data endpoints and write shims, replaced in 3.5;
- product web adapters and rendering shims, replaced in 3.6;
- execution ledger, receipts, durable engine state, provider budgets, rate limits and speech;
- installed product registry and product definitions;
- the generic model gateway, strict parser, provenance checker and confirmation boundary;
- 5c compatibility columns/tables needed by current reads, retention or emergency artifact rollback.

The implementation PR includes a “removed” and “kept” table. This prevents broad cleanup from
pulling work from later milestones into 5d or breaking the live demo.

## 5. Coverage transfer before deletion

Deleting old tests is allowed only after their behavior has a destination:

1. Inventory every old-engine test and golden case.
2. Classify it as:
   - preserved behavior with a definition-engine replacement test;
   - intentionally changed behavior with a reviewed golden difference;
   - obsolete implementation detail with no public contract;
   - security/regression behavior that must never be dropped.
3. Add or identify the replacement test before deleting the old test.
4. Make a machine-readable coverage-transfer manifest fail when an old case has no classification,
   a replacement path does not exist, or two old cases accidentally map to nothing.

Security, isolation, cancellation, confirmation, replay, provider-budget and response-integrity
tests are never classified as obsolete merely because they were first found through the old engine.
They move to the authoritative service or remain at the API/browser boundary.

The canonical product golden becomes the accepted definition-engine recording. It is generated only
for inspection; reviewed expected files are never overwritten automatically. Every change remains
case-by-case and reviewable.

## 6. Purity and dependency closure

### 6.1 Allowlist changes

Remove the 3.2 entries from `KNOWN_PRODUCT_COUPLING` only after their files are deleted or contain no
product-specific terms:

- old product configuration;
- old action planner and validator;
- old agent orchestrator and reasoner;
- old conversation manager, intent extractor and language normalizer.

If the implementation inventory finds another 3.2 old-engine file, it is included in the same
review. Entries assigned to 3.3, 3.4, 3.5 and 3.6 remain.

### 6.2 Import-graph requirements

- Starting from the API entry point, no transitive import reaches a removed module.
- Starting from `app.engine`, no transitive import reaches product data, product packages, network,
  database, provider, speech or execution modules except the already-approved registry boundary.
- Dynamic imports remain banned in engine code.
- Product packages import generic protocols only; core discovers packages through the single
  installed-product registry.
- A clean interpreter can import the API, run readiness and serve a turn without any removed file
  present on disk.

The test physically copies the release source without removed modules and imports/runs it. Passing
only because stale `__pycache__`, editable paths or local build output exist is not acceptable.

## 7. Single authoritative runtime

After cleanup, the turn lifecycle is exactly the accepted 5c lifecycle:

1. authorize and activate;
2. materialize one scoped snapshot;
3. load durable definition-engine state;
4. route deterministically, optionally invoke the generic model gateway;
5. validate and compose through platform boundaries;
6. atomically finalize state and issue any 5b key;
7. return one response and execute mutations only through receipts.

There is no comparison run, legacy projection for engine rollback, alternate response mapper or old
memory owner. Every visitor message continues through `/api/turn`; the browser-local decision
functions remain unreachable pending their source deletion in 3.3. Compatibility projection fields
may remain only where another live feature reads them; dead writes are removed after a read/write
usage test proves they are dead.

No product-specific reply or action logic may move into the new service merely to make deleting the
old file easier. Such logic belongs in the product definition/package or remains a reviewed gap for
the later generic milestones.

## 8. Data and schema policy

5d favors source deletion over schema deletion.

- Keep additive 5c state and 5b execution schema.
- Keep historical messages, signals, usage, parity and execution rows under their existing
  retention/privacy rules.
- Do not rewrite session pins, engine state or execution outcomes.
- Do not drop old columns/tables in the same deployment that removes the code.
- Remove a compatibility write only when no current runtime, report or emergency 5c artifact needs
  it and a populated-database test proves the change.
- Database startup remains forward- and immediate-artifact-backward-compatible.

This separates an application cleanup from a destructive data migration and preserves a practical
deployment rollback if the new artifact itself fails to start.

## 9. Configuration, operations and documentation

### 9.1 Configuration

- Remove authority and shadow variables from `.env.example`, deployment manifests, CI and runbooks.
- Startup warns or fails on the retired variables for one release rather than silently accepting a
  false rollback switch. The production deployment removes them before sign-off.
- Preserve independent model, speech, provider-budget and paid-provider kill switches.

### 9.2 Operator surface

- Replace shadow/cutover reporting with authoritative-engine health and execution reports.
- Keep definition-version movement, readiness, data maintenance and usage commands.
- Remove any command that instantiates the old engine or claims configuration rollback exists.

### 9.3 Documentation

- Mark 5c accepted with its production and rollback evidence.
- Record that 5d ends switch rollback and names the retained emergency artifact procedure.
- Update architecture diagrams and README wording from “old plus shadow” to one definition engine.
- Preserve historical slice reports; do not rewrite them as though the old engine never existed.
- Update the next-step boundary: browser decisions still leave in 3.3, not in 5d.

## 10. Test plan

### 10.1 Negative existence tests

- removed modules and environment switches do not exist;
- repository search finds no imports, strings, docs instructions or CI references that imply the
  old runtime can be selected;
- API startup cannot discover or instantiate the old engine;
- no request, admin operation or test-only endpoint selects another engine;
- a source-only release artifact contains none of the removed files.

### 10.2 Coverage-transfer tests

- every deleted old test/case has a valid manifest classification;
- every preserved/security classification points to a passing replacement test;
- reviewed-difference entries exactly match the accepted canonical golden and stale entries fail;
- test count reduction is explained by the manifest, not accepted from a raw lower number.

### 10.3 Runtime regressions

- full 5c authority, state, required-field, model-boundary, response-field and execution tests stay
  green with the old files physically absent;
- tenant/product/user/session/instance/generation/workspace isolation matrix stays green;
- restart memory, stale-turn races, cancellation, confirmation and replay stay green;
- private visitor creation/update/reset remains isolated;
- browser assistant writes require execution envelopes and form writes remain keyless;
- every browser message reaches `/api/turn`, and the unreachable local decision functions cannot
  answer or mutate even when the API is unavailable;
- voice receives the same final response and interruption still cancels stale output.

### 10.4 Purity and packaging

- 3.2 allowlist entries are gone and `test_allowlist_only_shrinks` passes;
- transitive dependency and dynamic-import tests pass;
- wheel/container/source-only builds start without repository-only paths or caches;
- production build contains no old-engine module name or retired switch;
- API, product, web unit, production build, browser and Linux CI suites all pass.

### 10.5 Populated database and deployment

- start the new-only artifact against a populated 5c database;
- verify readiness, a complete turn, a keyed write/replay, reset and retention;
- redeploy/restart during a pending conversation and observe the documented safe state behavior;
- deploy the previous 5c artifact against the unchanged database in a rehearsal environment to
  prove emergency artifact compatibility;
- run the public smoke matrix after production deployment.

## 11. Implementation order

1. Produce dependency, deletion, keep and coverage-transfer manifests without deleting code.
2. Add all replacement tests and make the definition-engine golden canonical.
3. Remove shadow runtime, reporting hooks and retired configuration references.
4. Remove authority branching and make the definition service unconditional.
5. Remove old-engine modules and old-only tests in dependency order.
6. Remove 3.2 purity exceptions and strengthen source-only/import-graph tests.
7. Remove dead compatibility writes proven unused; leave schema intact.
8. Update operator commands, environment examples, architecture and runbooks.
9. Run clean-source, populated-database, artifact-rollback and full regression rehearsals.
10. Merge only after Linux CI is green, deploy, run the public smoke matrix and record sign-off.

Each commit keeps a runnable application. Do not submit a single deletion commit that makes failures
impossible to localize.

## 12. Rollout and incident policy

1. Back up and restore-check the production database.
2. Deploy the new-only artifact without changing provider/model/voice settings.
3. Require readiness, then run the complete public smoke matrix.
4. Review the first 100 accepted turns and at least 60 minutes of authoritative telemetry.
5. If an isolation, mutation-integrity, startup or sustained availability defect appears, deploy the
   retained 5c artifact. This is an artifact rollback, not a supported engine switch.
6. Do not reintroduce copied old modules into main. A defect is fixed in the definition engine or
   the deployment is temporarily rolled back to the signed 5c artifact.

## 13. Exit criteria

5d is signed off only when:

1. All entry gates and the owner's explicit end-of-switch-rollback approval are recorded.
2. The old engine, authority switch and shadow runtime are absent from the source and release
   artifact.
3. Every deleted test/case is accounted for by the coverage-transfer manifest.
4. All 3.2 product-coupling allowlist entries are removed; later-milestone entries remain honest.
5. The transitive dependency, purity, source-only import and dynamic-import tests are green.
6. Every 5c security, memory, response, model and execution contract remains green.
7. API, product, web, production build, browser and Linux CI suites pass before merge.
8. A populated database starts without destructive migration, and emergency 5c artifact
   compatibility is rehearsed.
9. Production smoke and acceptance telemetry pass with one engine and no retired configuration.
10. Documentation states plainly that configuration-only rollback has ended and 3.3 is next.

## 14. Risks and non-goals

- **Coverage can disappear during deletion.** The transfer manifest and replacement-first order are
  mandatory.
- **A hidden import can survive.** Source-only artifact tests and transitive graph traversal catch
  what ordinary unit imports may miss.
- **Removing switches can be mistaken for removing safety controls.** Provider, model, speech,
  budget and execution kill switches remain.
- **Schema cleanup can make artifact rollback impossible.** No destructive schema removal occurs.
- **Historical evidence can become misleading.** Reports remain immutable history; current docs
  separately state the new-only architecture.
- **The product is not yet fully generic after 5d.** Browser decisions, knowledge, records and web
  rendering still have their assigned 3.3–3.6 migrations.
- **5d does not add features.** Any new workflow discovered during cleanup is recorded for its
  owning milestone unless it is required to preserve an already accepted 5c contract.
