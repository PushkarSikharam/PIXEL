# Private Demo Instances

Status: live in production and accepted by the stakeholder on 2026-09-21 (PR #18, merge
`6517c8b`; review follow-up PR #19, merge `92eb06f`). Part of phase-3, before 5a. Evidence is
recorded at the end of this document.
Updated: 2026-09-21.

## Outcome

Each customer organization has its own tenant. Each product belongs to one tenant. Each visitor
trying that product receives a private demo instance. Visitors see the same starting workflow,
but their changes, conversations, and resets do not affect another visitor.

An employee's actual shared workspace remains shared according to its existing permissions.
Creating a demo instance does not create a new tenant or copy a customer's live workspace.

Required demonstration: A reassigns LIN-142 from Maya to Noah. B still sees LIN-142 assigned to
Maya. A refreshes and still sees Noah. A and B cannot operate on each other's instance or session,
even when they know its ID. Restarting chat preserves A's edits; resetting A's demo restores only A.

## Defects this implementation closes

- Public sign-ins mint different tokens for the same `demo-visitor` principal. Session ownership
  compares user and tenant, so a second login can submit to a known first login's conversation.
- Record tables and legacy lookups contain one shared dataset. A's reassignment removes Maya's
  ticket from B's search. Waiting for an idle reset does not isolate overlapping visitors.
- The distinct visitor token path already exists, but record grants currently require members.
  Merely changing the browser's login endpoint would turn working workflows into 403 responses.
- Legacy lookup helpers can fall back to seed files on errors. A missing private instance must
  never become a successful lookup against shared records or seed files.

## Implemented identity and authorization

Use the existing product-scoped visitor-session endpoint as the entry point. Check active tenant,
product, definition, and public visitor access before allocation. The server issues a unique
visitor identity and demo instance together, with a token bound to both and to the tenant/product.

Persist the mapping; do not trust an instance ID sent in a header, URL, or JSON payload as proof
of access. Public demo configuration selects a product, not an employee identity. Keep the
restricted member login for explicitly configured test/operator workflows; it must not provide
access to public demo instances or a second route to their records.

A `DemoContext` resolved from authenticated server state contains tenant, product, visitor,
instance, generation, allowed workspace scopes, and seed version. Pass it explicitly into record
services and legacy lookups. Never select a database through a mutable process-global variable.

Check current access and expiry on every request. Bind conversations and execution keys to the
same visitor, instance and generation. Cross-instance, cross-product and cross-tenant access must
return the same unavailable response as an unknown identifier.

## Storage

Keep authorization, paid-provider accounting, conversations, execution outcomes and demo records
in the existing SQLite database so record changes and execution settlement can share one
transaction. Do not clone the full database per visitor: that would duplicate permissions and
budgets and make accounting and revocation inconsistent.

Instance metadata is keyed by tenant/product/instance and carries visitor owner, generation,
approved seed version, creation/expiry timestamps, state and quota accounting. Conversation
sessions separately pin the exact definition version and checksum; that pin remains the
conversation engine's responsibility rather than being duplicated in disposable record storage.

Store private records with composite identity `(tenant_id, product_id, instance_id, entity, id)`.
The familiar IDs such as LIN-142 can repeat across instances. A constrained JSON payload can
preserve today's record shapes. Relationships must reference records inside the same composite
boundary; use relational edges/composite foreign keys or equivalent transaction-level checks.
Include workspace membership, projects, tickets, cycles, members, and simulated integration state.

Record revisions support stale-edit rejection. A generation changes on reset; an older generation
can never write into the reset instance. The ledger and retry-receipt lookup must include instance
and generation, not only the familiar record ID or a client-chosen retry string.

Seed only from the installed product's approved immutable seed package, after checking its pinned
definition and seed checksum. Seed metadata and all records in one short write transaction.
The currently edited shared tables are not a seed source. Allocation, instance-count quota and
seed insertion are atomic. A seed failure leaves no partially usable instance or valid token.

## Service and browser changes

1. Add the instance repository and authorization context with ownership and transaction tests.
2. Route legacy record endpoints through the context. Keep the established response shapes for
   the current UI. Retain member-owned records under their current authorization without allowing
   an omitted visitor context to fall back to them.
3. Pass context through every legacy agent data lookup, person matcher, scope lookup and record
   validator, and through the installed product's RecordLookup. Find/count/open and create/update
   must use the same private dataset. Private lookup errors fail closed, never read seed files.
4. Bind chat, speech session checks, cancel, snapshots, execution keys and mutation receipts to
   visitor/instance/generation. Preserve product and tenant checks. Paid budgets stay deployment-
   and tenant-scoped in the existing accounting store, outside disposable demo data.
5. Change the public browser to request/resume an instance. Namespace its saved auth, data,
   conversation and selection caches by tenant/product/instance/generation. Flush old shared
   caches when upgrading. Refresh resumes an unexpired instance; it does not allocate repeatedly.
6. Split "Restart chat" from "Reset my demo". Starting a fresh conversation preserves records.
   Reset requires confirmation and affects the authenticated current instance only. Stop voice,
   cancel pending UI work and reload the new generation after the reset commits.
7. Remove the shared idle-reset behavior from the public visitor path. Instance expiry handles
   abandoned demos. No other visitor's login may reset an existing instance.

The browser still has local intent decisions until 3.3. This change must make every one of those
reads and writes instance-scoped; it does not claim to replace the browser decision engine.

## Reset, expiry and resource limits

Defaults for this synthetic demo: 24-hour absolute lifetime, 2-hour inactivity expiry,
100 active instances per product, and 1,000 records per instance including seed records. These are
configurable operator limits, subject to review. Do not silently evict an active instance to admit
a new one; reject allocation with 429 and Retry-After when its capacity budget is exhausted.

Reset and writes serialize in one database transaction. Reset increments generation, replaces
only that instance's records, invalidates pending conversations/actions for the old generation,
and rejects delayed writes and retry receipts carrying the old generation. An executed action
remains historically executed; reset does not rewrite its outcome or provider usage.

Expire in bounded scheduled batches. Expired instances cannot resume a conversation or consume
provider budget. Pruning disposable records must not erase paid usage or allow old execution keys
to become valid again. Bound retained metadata with a documented retention window and refuse
missing/expired keys. Test cleanup interruption and restart.

Unique identities must not weaken abuse protection: retain deployment/product-wide rate and paid
budget ceilings, and add atomic allocation/reset quotas. New instances never replenish provider
allowances. Keep one API replica until shared rate-limit storage is implemented.

## Acceptance tests

- Two fresh browser contexts receive different visitors and instances but identical seed content.
- A reassigns Maya's ticket; B still opens Maya's seeded ticket. A's edited state survives reload.
- Independent create/update/read behavior for tickets, projects, cycles and members, including
  relationships and generated ID collisions across instances.
- A cannot read, update, reset, submit a chat turn, cancel a turn, or replay B's key even with
  B's instance/session/record IDs. Repeat across products and tenants.
- A relationship to a foreign instance's project/member/cycle is refused with no partial write.
- Missing or expired instance context fails closed on every route and every legacy lookup; no
  fallback to shared tables, seed files, cached model context or browser sample data.
- Restart-chat preserves A's records. Reset-A restores only A and invalidates old-generation
  sessions, delayed writes, voice results and retry receipts. B stays unchanged.
- Simultaneous allocation at capacity, duplicate create, reset-versus-write, cleanup-versus-write,
  and restart during seeding are deterministic and leave no mixed-instance records.
- Provider budget and rate ceilings persist across new instances, resets and server restarts
  wherever the current accounting contract promises durability. No real paid calls in tests.
- Existing employee/member access, workspace restrictions, golden conversations and the public
  readiness check still pass. Health/smoke must prove private-instance startup, not only member login.

## Rollout and evidence

Local verification on 2026-09-21:

- 741 core API tests passed with 3 CI-only skips; 82 product tests passed.
- Web type checking, 35 web unit tests and the production build passed.
- 113 real-backend browser tests passed, including the Restart-versus-Reset lifecycle.
- The release smoke created two visitors, proved isolated writes, rotated one visitor's reset
  generation, rejected its stale token and the global reset, and exercised chat and guardrails.
- Paid speech and model providers were disabled for all isolation verification.

These are local implementation results, not production sign-off. Linux CI, a verified backup,
deployment against the previous production schema, and two independent production browser
contexts remain required.

All implementation stays on phase-3. Local evidence includes the API, product, web and real-backend
browser suites. CI and the exact Linux deployment image remain release gates, along with a test on
a copy of the previous production schema.

Take and verify a consistent SQLite backup before changing production storage. Add the new schema
without deleting the old tables; leave the employee path intact. Existing public shared tokens
must be revoked or refused on the private route, and the browser must restart with a private
instance. A rollback must not quietly restore public access to shared records: use a maintenance
state if reverting would break isolation.

Production acceptance uses two genuinely independent browser contexts on the public URL. Record
the deployed revision, instance IDs (no tokens), checks performed, and result. Obtain separate
approval for any real paid voice synthesis. This plan alone is not evidence that isolation works.

After isolation and 4b sign-off, 5a stays deterministic and read-only: independent shadow memory,
zero provider calls, zero execution keys, and platform-wording differences reported separately.

## Evidence

**Continuous integration.** PR #18 (commit `e75e929`): pull-request run 35618632336 and `main` push
run 35618980742 are green on all three jobs: API tests (with the container build and its smoke
test), web checks and browser tests.

**Deployment.** Railway and Vercel both deployed `6517c8b` on 2026-09-21. Railway waited for the
`main` CI run and reported success.

**Production acceptance, 2026-09-21, on the public URL.**

- `scripts/smoke-live.mjs` passed: two visitors received different instances with identical seed
  data; a reassignment of LIN-142 by the first was invisible to the second; the first visitor's
  private reset restored its seed, moved it to generation 2 and refused its old token; the
  administrator login and the global reset were refused. (The script checks that the two instance
  IDs differ; it does not print them, and no tokens were recorded.)
- In the live page: "Open Maya's ticket" and "Assign it to Noah" completed; after a reload the
  visitor still saw Noah on LIN-142, while a newly allocated visitor saw Maya.
**Open item found in production, now closed.** The legacy member demo login still answered `200`
for `demo-visitor`, so the shared member records remained reachable by direct API calls.
Production now sets `PIXEL_SYNTHETIC_DEMO=false`, and the login answers `403 Demo login is
disabled.` (verified after the PR #19 deployment).

**Review findings fixed in the follow-up** (`apps/api/tests/test_private_demo_hardening.py`, each test
shown to fail without its fix):

1. The product's record lookup defaulted to the shared member records. It now reads the grant's
   private instance and refuses a store belonging to anyone else. This is the lookup the 5a shadow
   engine uses.
2. One caller could claim every demo slot within the deployment's sign-in ceiling and hold it for the
   idle window. Capacity is now 1,000 per product; when full, the instance unused for longest (at
   least 15 minutes) is reclaimed; an instance in use is never evicted.
3. Lookup helpers fell back to the shared records, a seed file or a built-in list of names. They now
   require the caller's records and refuse to run without them.
4. The seed was rebuilt from files with no approved checksum. The product package now pins its
   version and checksum; an unreviewed change refuses new visitors and resets.

**Follow-up evidence.** PR #19 (commit `d2a638e`): pull-request run 35622323715 and `main` push run
35622688005 are green on all three jobs. Railway and Vercel both deployed `92eb06f` on 2026-09-21.
Afterwards, on the public URL:

- `scripts/smoke-live.mjs` passed again with the same checks as above.
- A new visitor with empty browser storage ran the whole guided path: sprint planning opened,
  "Open Maya's ticket" found LIN-142 with Maya Chen, "Assign it to Noah" updated it, the Lucifer
  request handed off to Teams, and Salesforce was refused.
- A browser session that had reassigned LIN-142 earlier resumed its own instance and still saw
  Noah, while a separately allocated visitor saw Maya. This was run in one automated browser
  session plus a second allocated visitor, not by a person using two browsers.

**Stakeholder acceptance:** approved on 2026-09-21.
