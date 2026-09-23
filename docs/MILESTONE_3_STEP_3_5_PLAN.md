# Milestone 3.5, Records the Platform Owns - Implementation Plan

Status: **implementation started on `phase-3`; awaiting review and CI evidence before sign-off.**
Date: 2026-09-22. Parent plan: `docs/MILESTONE_3_STEP_3_2_PLAN.md`, which assigns "records, scopes
and the demo-data API" to 3.5. Platform rules: `docs/PIXEL_SAAS_PLATFORM_PLAN.md`.

This milestone is no longer only a clean-up. It is the gate for the proof of concept: the Pixel
application where someone adds a product of their own, adds team members, gives them roles and
assigns tickets. Until records follow a product's own definition, a product added to Pixel can be
talked about but cannot hold anything, and every button in that application is dead.

## 1. Outcome

Records belong to the platform and take their shape from the product definition that declares
them. After 3.5, a product the platform has never seen before - one with customers and invoices,
or candidates and interviews, or tickets and cycles - stores, reads, scopes, validates, creates
and changes its records through the same code, with no Python written for it.

Done means:

1. `apps/api/app/record_schemas.py`, `apps/api/app/workspace_config.py`,
   `apps/api/app/services/demo_data.py` and the record half of
   `apps/api/app/services/product_data_store.py` are deleted, and their 3.5 entries leave the
   core-purity allowlist.
2. `products/linear_simplified/backend/lookup.py` reads the platform store instead of tables named
   after its own entities.
3. The planning demo behaves identically, proven by the existing golden and browser evidence.
4. A second product with entirely different entities passes the same record tests, as a fixture.
5. Creating, reading, scoping and changing a record is decided by the definition's `entities`,
   `people` and `scope` sections and by nothing else.

## 2. What exists today, honestly

**Two record paths already run side by side.**

The *shared member path* stores records in tables named after the planning demo's entities:
`demo_projects`, `demo_issues`, `demo_cycles`, `demo_team_members`, `demo_workspace_scopes`. Each
has typed columns, a hand-written `_upsert_*`, a hand-written `_*_from_row`, and a hand-written
reference check. `ProductDataStore` is roughly 900 lines, and almost all of it is one product's
shape spelled out in Python.

The *private visitor path* already stores records generically. `demo_instance_records` is keyed by
`(tenant_id, product_id, instance_id, entity, record_id)` with a JSON payload, a revision, a
reference check performed inside the write transaction, a per-instance record limit and a
payload size limit. Every visitor to the live demo has been using it. **The storage this milestone
needs already exists and is in production**; what is still product-specific is the logic above it
that decides what a record's fields are, which records a reference may point at, and which scope a
record falls in.

**Scopes are hard-coded twice.** `workspace_config.WORKSPACE_SCOPES` names two workspaces, the
projects in each and the people in each, as a Python literal. `get_workspace_scope` then re-reads
them from the caller's own records so that newly created projects are included, which means the
literal is already half-dead. `_filter_to_scopes` walks issue -> project -> workspace by field
names written into the function.

**Validation is per-product pydantic.** `record_schemas.py` declares `IssueInput`, `ProjectInput`,
`CycleInput` and `MemberInput` with field names and limits copied by hand from the definition. The
definition already declares the same facts - type, required, min, max, enum values, editable,
default - in `EntitySpec.fields`. The two can disagree, and nothing detects it.

**The HTTP surface is per-entity.** `/api/demo-data/issues`, `/api/demo-data/projects`,
`/api/demo-data/cycles`, `/api/demo-data/team-members`, plus `/api/demo-data/issues/{id}` for the
one update the product supports. A new product cannot be served by any of them.

**What is already generic, and must not be rebuilt:** execution keys and receipts, the mutation
ledger, optimistic revisions, demo instance allocation and expiry, record grants
(`record_grants` is already keyed by tenant and product), the definition contract, and the
engine's `RecordLookup` seam.

## 3. Invariants (normative)

Every one of these is a test, not a comment.

1. **Shape comes from the definition.** A record's fields, their types, which are required, which
   are editable and what an enum may contain are read from the entity's `FieldSpec`. No record
   shape is written in core Python or in a product package.
2. **One store.** There is one record store. A caller's records are identified by organization,
   product and record space; a visitor's private demo is one space and an organization's own
   records are another. No code path reads a table named after an entity.
3. **References resolve inside the caller's own space.** A `ref` or `refs` field may only name a
   record of its declared target entity that exists in the same space, checked inside the write
   transaction. A dangling reference is refused, never stored.
4. **Scope is derived, never declared per product.** Which scopes a record falls in is computed by
   walking `scope.paths` from the record to the anchor entity. A record outside the caller's
   scopes is indistinguishable from one that does not exist, on every path: read, search, count,
   reference and change.
5. **Identity is minted by the platform.** A record's identifier follows `EntitySpec.id`
   (`prefix` or `slug`). A client-supplied identifier is never trusted for a create.
6. **Nothing widens.** A record grant fixes the caller's scopes at construction. No message,
   payload, query parameter or header can widen it.
7. **Mutations stay keyed.** Every create and change still requires a valid execution key bound to
   the turn that proposed it, and still writes one receipt. 3.5 changes where records live, never
   how a change is authorized.
8. **The planning demo does not change.** Its replies, its golden recordings and its browser
   suites are identical before and after, under both authorities.

## 4. Design

### 4.1 The record space

One table, replacing the four typed ones and generalising `demo_instance_records`:

```
product_records(
  tenant_id, product_id, space_id, entity, record_id,
  revision, payload, updated_at,
  primary key (tenant_id, product_id, space_id, entity, record_id)
)
```

A **space** is who the records belong to. Two kinds:

- `primary` - the organization's own records for that product. Long-lived, no expiry. This is
  where an onboarded product's real tickets and members live.
- a demo instance id - one visitor's private demo, allocated, expiring and reclaimable exactly as
  today.

This keeps one code path for both, and it is why a visitor's isolation guarantees carry over
unchanged rather than being written a second time for member records.

### 4.2 Validation

A `RecordContract` built from an `EntitySpec` validates a payload: unknown field rejected,
required field present, type checked, `min`/`max` applied, enum value inside `values`,
non-editable field refused on update, `default` applied on create. It replaces
`record_schemas.py` entirely, and it cannot disagree with the definition because it is derived
from it.

### 4.3 Identity

`EntitySpec.id` already declares `strategy: prefix` with a prefix, or `strategy: slug` with a
source field. The platform mints the identifier on create from that declaration. This is the only
place identifiers are produced.

### 4.4 Scope

A scope is a named set of anchor records, stored by the platform per product and space:

```
record_scopes(tenant_id, product_id, space_id, scope_id, name, description)
record_scope_anchors(tenant_id, product_id, space_id, scope_id, anchor_id)
```

A record's scopes are computed by following `scope.paths[entity]` - the chain of reference fields
from the record to the anchor entity - and collecting the scopes of the anchors it reaches. For
the planning demo this reproduces issue -> project -> workspace exactly, and for a product with
customer -> region it works with no new code. `workspace_config.py` is deleted; the two named
workspaces become seeded scope rows.

### 4.5 HTTP surface

Generic, per product and entity:

- `GET  /api/products/{product_id}/records` - every record the caller may see
- `POST /api/products/{product_id}/records/{entity}` - create, execution key required
- `PATCH /api/products/{product_id}/records/{entity}/{record_id}` - change, execution key required

The existing `/api/demo-data/*` endpoints remain during the migration, reimplemented as thin
translations onto the generic ones, so the current web app keeps working. They are deleted in 3.6
when the web app moves to the generic surface. No behaviour is added to them here.

### 4.6 People and roles

`people.entity` already names the entity that holds people, and `people.assigned_by` names the
fields that reference them. Adding a team member is therefore a create of that entity, and
assigning work is an update of a person field - both already expressible. A **role** is a field of
the people entity, which the planning demo already declares. Nothing new is invented here; the
milestone only has to stop the record layer from blocking it.

Platform membership (who may sign in to an organization and what they may administer) stays where
it is, in `memberships` and `record_grants`. A product's people records and the platform's users
are deliberately different things, and 3.5 does not merge them.

## 5. Slices

Each slice ends green on all suites, with the planning demo unchanged.

- **3.5a - contract-driven records, no storage change.** `RecordContract` and identity minting,
  validated against the planning demo's own seed and against a fixture product. `record_schemas.py`
  becomes a caller of it. Evidence: every seeded record validates; every rejection today is still a
  rejection.
- **3.5b - one store.** `product_records` and the space concept; `ProductDataStore` keeps its
  public methods but reads and writes the generic store; the four typed tables are migrated and
  dropped. Evidence: golden recordings and browser suites identical under both authorities.
- **3.5c - derived scope.** `record_scopes`, path-walking, `workspace_config.py` deleted. Evidence:
  the isolation suite passes unchanged, plus the same tests against a fixture product whose scope
  path has a different shape.
- **3.5d - generic surface.** The three generic endpoints, `/api/demo-data/*` reduced to
  translations, and the record half of `ProductDataStore` deleted. Evidence: the web app is
  untouched and its suites pass; the generic endpoints are exercised directly by a fixture product.

## 6. Evidence and acceptance

- The golden recordings and shadow differences are **never regenerated to make a test pass**. Any
  difference is listed with its kind and its reason, per the project's standing rule.
- A security defect is never preserved for parity.
- Acceptance is a pull request showing green Linux CI including the browser suites under both
  authorities. Local runs are development evidence only.
- The fixture product from `apps/api/tests/library_fixtures.py` is extended to cover records, so
  every record invariant is proven against a product the planning demo's shape cannot satisfy.

## 7. What this plan does not do

- It does not onboard a product from files or a git link. That is the next milestone and depends
  on this one.
- It does not build the Pixel application's screens. It removes the reason they cannot work.
- It does not merge platform users with a product's people records.
- It does not change how a mutation is authorized, proposed, keyed or receipted.
- It does not touch the marketing site, which comes last.

## 8. Risks

- **Migration of live records.** The production database holds the planning demo's records in the
  typed tables. 3.5b must migrate them in one transaction with a verified backup taken first, and
  a rollback that restores the typed tables from that backup.
- **Silent scope widening.** Deriving scope is the highest-risk change here: a bug widens what a
  caller sees rather than narrowing it. The isolation suite runs against both the planning demo and
  a fixture product with a different path shape, and every derived-scope test asserts the negative
  case as well as the positive one.
- **Size.** This is the largest remaining core change. The slices are ordered so that each one is
  independently revertible and none of them changes behaviour a visitor can see.
