# Pixel SaaS Platform Plan

Status: **Final proposed revision, awaiting owner approval.** Proposed post-Milestone-3
architecture. Milestone 3.2 must finish first, then steps 3.3-3.8 prove the generic platform core
before SaaS implementation begins. One owner decision is open: when invited members can first sign
in (section 15.1, decision A1).

## 1. Product Definition

Pixel is a B2B platform for building and operating product-specific AI guides. Edith is Pixel's
conversation and execution runtime. The current project-management workspace is one reference
product, not Pixel itself.

Pixel has five deliberately separate experiences:

1. **Customer Console** - authenticated control plane for products, releases, knowledge, policies,
   deployments, usage and audit.
2. **Authenticated Playground** - synthetic-data testing of drafts before publication.
3. **Signed-in Demo Mode** - a guided, disposable Pixel demonstration available inside the console,
   separate from the customer's drafts and products.
4. **Public Test Drive** - anonymous, disposable and isolated demonstration of Pixel.
5. **Deployed Runtime** - the customer's published Edith integration, scoped to one customer,
   product, environment, release and end-user session.

No conversation, record, execution entry or mutable demo instance is accessible across modes.
Physical infrastructure may be shared only through the tenant/mode boundaries defined below. Draft
configuration never reaches a deployed runtime.

## 2. Non-Negotiable Invariants

1. A tenant selector supplied by a client is never authorization.
2. Every request is authorized through:
   `organization -> team -> product -> environment -> resource -> permission`.
   Organization-wide roles (`org_admin`) hold no team and authorize at the organization step; every
   other role must pass the team step for the product it uses.
3. Every tenant-owned row, object, cache entry, queue message and retrieval document is explicitly
   tenant-bound. Shared infrastructure is permitted only where that boundary is enforced and tested.
4. Published releases are immutable and content-addressed.
5. A runtime session pins one release, knowledge version, policy version and UI adapter version.
6. The model may propose; deterministic platform code validates and authorizes.
7. Customer documents, product copy and model output are untrusted input.
8. No console page directly reads or writes a runtime database.
9. Execution credentials, provider secrets and raw prompts never appear in traces or UI.
10. Public demos use synthetic data and disposable private instances only.
11. Every privileged mutation produces an immutable audit event.
12. Deletion and revocation propagate to caches, indexes, sessions and derived data.

## 3. Architecture Decision

### 3.1 Control Plane

The control plane is shared infrastructure containing customer-management metadata:

- internal users and external identities;
- organizations, memberships, teams and permissions;
- products and environments;
- definition drafts and immutable releases;
- deployment metadata and release assignments;
- billing entitlements and configured provider-budget policies;
- redacted usage aggregates and audit events;
- webhook inbox and asynchronous job state.

The control plane does not contain customer runtime records, conversations, execution keys, raw
retrieval passages or provider credentials.

### 3.2 Runtime Deployment Model

The approved default remains one shared multi-tenant deployment. Organization identity is independent
of deployment identity. Shared-by-default is an economic and operational choice, not a relaxation of
the logical boundary.

The standard runtime uses:

- tenant-scoped PostgreSQL rows protected by service authorization and RLS;
- organization/product/environment prefixes in object storage;
- tenant-bound retrieval indexes or mandatory metadata filters;
- tenant-qualified cache keys and queue messages;
- provider credentials resolved through the authenticated tenant context and runtime-enforced
  budget policy;
- per-organization rate, concurrency and spending controls;
- deletion tests proving that one product package and one tenant can be removed without affecting
  another.

Later deployment options run the same semantics:

- **Dedicated enterprise:** dedicated runtime, database, storage, network and provider credentials.
- **Sovereign/customer-hosted:** separate account or customer environment; only signed releases and
  explicitly enabled telemetry cross the boundary.

Dedicated and sovereign modes are not part of the first SaaS pilot. Their cost, support and data
location contracts require a separate approval. The public Test Drive is a separate logical data
partition in the pilot; physical separation is optional until scale or risk justifies it.

### 3.3 Release Distribution

The control plane publishes a signed, immutable release bundle containing:

- normalized product definition;
- action and capability manifest;
- policy manifest;
- knowledge manifest and checksums, not raw secrets;
- UI adapter manifest;
- minimum compatible runtime version;
- migration compatibility metadata;
- complete bundle checksum and signature.

The runtime verifies the signature and checksum before activation. It keeps the previous known-good
release for rollback. A draft cannot be pulled by a production runtime.

## 4. Data Architecture

### 4.1 Database Choice

PostgreSQL is mandatory before the SaaS control plane opens to customers. SQLite remains supported
for local development and the reference demo only.

The shared control-plane database uses PostgreSQL row-level security as defense in depth:

- every tenant-owned row has an immutable `organization_id`;
- tenant context is derived from verified identity, never a request header;
- request transactions set tenant context locally;
- tenant request roles are not superusers, table owners or `BYPASSRLS` roles;
- tenant tables enable and force RLS;
- migrations and platform operations use separate privileged identities;
- CI fails if a tenant-owned table lacks its required RLS policy.

RLS complements service authorization; it does not replace it. PostgreSQL documents that superusers
and `BYPASSRLS` roles bypass row security, so deployed-role verification is part of readiness.

### 4.2 Canonical Hierarchy

Pixel preserves the approved hierarchy:

`Organization -> Team -> Product -> Environment -> that product's Pixel`

- An organization owns teams and organization-wide policy.
- A team owns or administers products.
- A product owns definitions, knowledge, adapters and action policy.
- An environment is a deployment boundary under a product: development, staging or production.
- Pixel is the product-specific Edith experience produced by that binding.

Environment does not replace Team and never sits above Product.

### 4.3 Core Control-Plane Entities

- `users`: internal stable identities.
- `external_identities`: provider plus immutable external subject, unique together.
- `organizations`: Pixel tenant and lifecycle state.
- `external_organizations`: identity-provider mapping.
- `memberships`: user, organization, one role (`org_admin`, `team_admin`, `team_member`), the team
  for team roles (required, and absent for `org_admin`, as enforced today), lifecycle state and
  synchronization version. Additional permission grants are separate rows, never a replacement role.
- `access_requests`: verified email, optional stated organization name and reason, review state,
  and bounded abuse counters; never a membership (section 5.6).
- `invitations`: organization, intended role/team, issuer, expiry, one-time acceptance and state.
- `teams`: required owners of products within an organization. An organization has at least one
  team; a product belongs to exactly one team.
- `products`: customer-owned product containers.
- `environments`: development, staging and production boundaries.
- `definition_drafts`: mutable workspace with optimistic revision.
- `releases`: immutable published versions and checksums.
- `release_assets`: signed manifests and object references.
- `deployments`: runtime deployment target and active/previous release.
- `knowledge_sources`: source configuration and ingestion state.
- `policy_sets`: versioned capabilities, guardrails and budgets.
- `api_credentials`: prefix, hash, scopes, owner, expiry and revocation only.
- `webhook_events`: provider event ID, digest, status and retry state.
- `audit_events`: append-only actor, action, target, result and redacted metadata.
- `usage_aggregates`: metered units without prompts or customer content.

External provider IDs never become Pixel primary keys. Email is mutable profile data, not identity.

### 4.4 Storage Classes

Every data type is classified before implementation:

| Class | Examples | Storage | Default retention |
| --- | --- | --- | --- |
| Identity | users, memberships | Control PostgreSQL | Account lifetime plus policy |
| Configuration | drafts, releases, policies | Control PostgreSQL/object store | Versioned, explicit retirement |
| Runtime customer data | records, conversations | Shared runtime PostgreSQL with tenant policies; dedicated store in later enterprise mode | Customer-configured |
| Knowledge | source files, chunks, embeddings | Shared object store under organization/product/environment prefixes, and a tenant-filtered retrieval index; dedicated store in later enterprise mode | Source lifecycle plus deletion SLA |
| Secrets | provider keys, webhook secrets | Managed secret store/KMS | Until rotated or revoked |
| Telemetry | counts, timings, result codes | Redacted telemetry store | Bounded by plan |
| Audit | administrative actions | Append-only audit store | Contractual retention |

Backups inherit the same tenant and encryption boundary. Restore tests are required; a backup that
has never been restored is not accepted as evidence.

Pilot backup retention is 30 days. Deleted online data may remain only in encrypted, access-restricted
backup snapshots until those snapshots expire; it is unavailable to the application during that
window. Deletion tombstones are retained longer than backups and must be replayed before a restored
database or object snapshot can serve traffic. Therefore a deleted source is absent from all active
systems immediately according to its deletion SLA and ages out of every backup within 30 days.

## 5. Authentication and Authorization

### 5.1 Provider Decision

Provider decision: **open until the proof gate** (section 5.2), behind a Pixel-owned identity
adapter. WorkOS AuthKit and Clerk are the two candidates; neither is preferred in advance.

What the pilot requires, and therefore what decides the choice:

- passwordless one-time email codes;
- invitation-only activation and a Pixel-owned request-access review (section 5.6);
- organizations mapped to Pixel's own organization, team and membership records, with Pixel as the
  authority for roles;
- signed webhooks and a server-side token verification path for FastAPI;
- free-tier and paid cost at pilot scale, data processing locations, subprocessors, and export and
  deletion support.

Enterprise SSO, directory sync and SCIM are deferred (section 5.2). They are a tie-breaker only:
between two candidates that both meet the pilot requirements, the one with the more credible path
to them is preferred.

The selection is not final until the proof compares required pilot capability, free-tier and paid
cost, data processing and storage locations, regional availability, export/deletion support and the
enterprise roadmap. Pixel's domain model remains provider-neutral either way.

### 5.2 Authentication Proof Gate

The selected provider must prove:

1. Next.js sign-in, sign-out, invitation and organization switching.
2. Passwordless one-time email code sign-in; passwords are not part of the pilot.
3. Invitation-only account activation and a request-access workflow: request, administrator review,
   invitation, acceptance. A request never grants access by itself.
4. FastAPI verification of signature, issuer, audience, authorized party, expiry and token type.
5. No active organization produces no tenant access.
6. Revoked membership loses access within the documented window and immediately on sensitive paths.
7. Staging and production identities and secrets are separate.
8. Signed webhook verification, duplicate delivery and out-of-order delivery handling.
9. Role and permission updates synchronize without recreating suspended local access.
10. Service-account/API-key authentication is distinguishable from a human session.
11. Pilot and later enterprise requirements fit the approved commercial plan, including documented
    data locations and subprocessors.

Enterprise SSO, SCIM and directory sync are deferred from the first pilot unless a pilot customer
requires them. The provider choice must still preserve a credible path to those capabilities.

### 5.3 Provisioning and Synchronization

Authentication middleware is read-only. It never creates organizations or memberships.

- Initial organization creation happens through an explicit, transactional onboarding command.
- Provider webhooks enter an idempotent inbox keyed by provider and event ID.
- A worker applies events in version order where the provider supplies a version or timestamp.
- Deleted or suspended local entities are never recreated by an ordinary authenticated request.
- Sensitive mutations re-check current Pixel membership and permission locally.
- Reconciliation detects missed webhook deliveries without granting access automatically.

### 5.4 Authorization Model

Permissions, not UI visibility, authorize operations. Initial permissions:

- `organization.manage`
- `members.read`, `members.manage`
- `products.read`, `products.manage`
- `definitions.read`, `definitions.edit`, `definitions.publish`
- `knowledge.read`, `knowledge.manage`
- `policies.read`, `policies.manage`
- `deployments.read`, `deployments.manage`
- `playground.use`, `traces.read_redacted`
- `usage.read`, `budgets.manage`
- `credentials.manage`
- `audit.read`
- `billing.manage`

Milestone 3 roles remain canonical during migration:

- `org_admin` keeps organization-wide administration.
- `team_admin` keeps administration of its team and that team's products.
- `team_member` keeps permitted use of its team and products.

Fine-grained permissions are introduced additively. Initial mapping:

- `org_admin` receives the organization permission set.
- `team_admin` receives member permissions plus product, definition, knowledge, policy, playground
  and deployment permissions for its team.
- `team_member` receives read, playground and allowed runtime-use permissions for its team.

Owner, Publisher, Analyst, Billing Manager and Viewer are later role presets over permissions, not
immediate replacements for the three existing roles. A migration is required before any preset can
change existing access. Production publishing can optionally require a different approver from the
last editor.

### 5.5 Machine Credentials

- Secret value shown once.
- Only a keyed hash and visible prefix are stored.
- Credentials are organization, product and environment scoped.
- Every key has explicit permissions, optional IP/origin constraints, expiry and last-used time.
- Rotation supports an overlap window; revocation is immediate.
- Browser applications never receive server API keys.

### 5.6 Request Access

Anyone may ask for access; nobody gets access by asking.

- **Form:** email address, optional organization name and a short reason. No other personal data is
  requested.
- **Verification first:** the request is stored only after the email address confirms a one-time
  code. An unconfirmed submission is discarded.
- **Abuse limits:** per-address and per-network rate limits, a daily global cap, and duplicate
  suppression (one open request per email). Abuse metadata is limited to counters and timestamps
  keyed by a keyed hash of the network address; raw addresses are not stored with the request.
- **Review:** an organization administrator of the Pixel operating organization approves or
  rejects. Approval issues an invitation (section 4.3); the request itself never creates an account,
  membership or organization.
- **Retention:** a rejected or withdrawn request is deleted 90 days after its decision; an
  unreviewed request expires and is deleted after 90 days; an approved request is deleted once its
  invitation is accepted or expires. Abuse counters are kept at most 30 days. The requester can ask
  for deletion at any time.
- **Audit:** approvals and rejections are audit events that record the reviewer and the request
  identifier, not the email address.

## 6. Product Onboarding and Release Lifecycle

### 6.1 Supported Inputs

Customers may provide approved:

- product documentation;
- OpenAPI specifications;
- feature and workflow descriptions;
- screenshots and page metadata;
- terminology and synonyms;
- sample synthetic data;
- allowed actions and required confirmation rules;
- forbidden topics and data boundaries;
- UI adapter configuration.

Pixel does not execute uploaded customer code during ingestion. Archives are size-limited, scanned,
unpacked safely and processed in isolated workers. URL ingestion blocks private-network and metadata
service access to prevent SSRF.

### 6.2 Definition and Release Lifecycle

Definition versions preserve the lifecycle already implemented in Milestone 3:

`draft -> validated -> published -> retired -> revoked`

`published -> revoked` is also allowed as the emergency path. Revoked is terminal.

- Drafts are mutable and use optimistic revisions.
- Validation produces immutable findings tied to a draft revision.
- Publishing creates an immutable release; it never edits an existing release.
- Retirement blocks new sessions and deployments while existing pinned sessions may finish.
- Revocation is the emergency brake: new use is refused and active sessions terminate on their next
  request, matching today's registry behavior.

Deployment has a separate lifecycle:

`pending -> deploying -> active -> superseded | failed | disabled`

- Production deployment requires a published, non-revoked release and passing compatibility checks.
- Rollback creates a new deployment attempt targeting a previous signed release; it does not mutate
  either release.
- Revoking the active release disables its deployment until an operator selects another valid
  release.

### 6.3 Publication Gate

A release cannot publish until all required checks pass:

- schema and copy validation;
- action registry and policy consistency;
- no unknown or untranslatable capability;
- product-boundary and tenant-deletion tests;
- golden conversation evaluation;
- unsupported-claim evaluation with citations;
- UI adapter compatibility;
- migration compatibility;
- provider and budget configuration validation;
- security review for any new action class.

The release record stores the exact evidence and tool versions used to approve it.

### 6.4 Release Signing

- An asymmetric signing key is held by the control plane in a managed KMS/HSM; the private key is
  non-exportable and unavailable to application request handlers.
- Releases record a signing-key ID. Runtime deployments receive a pinned verification-key ring.
- Rotation publishes the new verification key before it signs a release and preserves the old
  verification key until every trusted release using it is retired.
- Suspected compromise freezes publication, revokes the affected signing key and release manifests,
  moves deployments to a known-good release signed by a trusted key, and requires a documented
  incident review.
- Development, staging and production use different signing keys.

## 7. Knowledge Architecture

- Raw sources stay in the customer's storage boundary.
- Every source, chunk and embedding carries organization, product, environment and knowledge-version
  identity.
- Retrieval APIs do not accept arbitrary tenant IDs; they are constructed with scoped context.
- Production sessions retrieve only from their pinned knowledge version.
- Answers require source attribution; unsupported claims are refused.
- Retrieved text remains untrusted and cannot authorize an action or modify platform instructions.
- Re-ingestion creates a new version. It does not mutate the version used by active sessions.
- Source deletion removes raw files, chunks, embeddings and caches from every active system within
  the deletion SLA. Copies inside backup snapshots are unreachable by the application and expire
  with those snapshots within 30 days (section 4.4).

## 8. Runtime and Integration

### 8.1 Runtime Turn Contract

Each turn binds:

- organization and runtime deployment;
- product and environment;
- end-user/session identity;
- workspace or project scope;
- release and knowledge version;
- active turn ID;
- validated action and optional execution receipt;
- budget and provider policy.

The runtime never searches across products or projects. An inaccessible entity is indistinguishable
from a nonexistent one.

### 8.2 Customer Integration Options

1. **Pixel reference UI** - generated from an approved adapter schema.
2. **Embedded guide** - Pixel panel embedded in a customer application.
3. **Customer SDK** - customer renders UI and transports Pixel's existing execution envelope and
   validated action to the registered write endpoint.
4. **Server integration** - customer backend exchanges scoped machine credentials with the runtime.

There is no unrestricted browser automation. An adapter exposes named actions with validated
parameters.

The SDK does not introduce a second execution authorization mechanism. Embed tokens authenticate
the channel; they do not authorize mutations. A mutation is authorized only by the one-time
execution key and exact change-set contract established in Milestone 3.2. Additional message
signatures may protect transport integrity but can never replace or broaden that key.

### 8.3 Embedded Security

- Customer apps are embedded from an allowlisted exact origin and preferably a separate origin.
- Iframes are sandboxed with the minimum required permissions.
- `postMessage` checks exact `origin`, expected `source`, protocol version, nonce and schema.
- `targetOrigin` is never `*`.
- Embed tokens are short-lived and bind organization, product, environment, origin, session and
  allowed capabilities.
- CSP defines `frame-ancestors`, script sources, connection targets and reporting.
- Execution keys are never placed in URLs, DOM text, analytics or trace displays.

MDN explicitly recommends exact `targetOrigin` and sender validation for `postMessage`, and warns
that permissive iframe sandbox combinations can defeat isolation.

## 9. Demo and Playground Isolation

### Public Test Drive

- Anonymous identity using the existing usage-ledger reservation model, per-principal and
  per-deployment limits, and the paid-provider kill switch.
- Synthetic reference product only.
- Private disposable instance per visitor.
- No customer definitions, knowledge, records or credentials.
- Hard lifetime, capacity controls and reset generation.
- Separate demo tenant/data partition and telemetry partition.

### Authenticated Playground

- Uses one selected draft revision and synthetic data.
- Cannot access production records or provider credentials.
- Destructive and external side effects are disabled.
- Test executions are clearly labelled and separately metered.
- A production release is never modified by a playground turn.

### Signed-in Demo Mode

- Available from the console without creating or modifying a customer product.
- Uses the Pixel reference product, synthetic data and a disposable private instance.
- Demonstrates onboarding, knowledge, safe actions, voice and guardrails as a guided product tour.
- Has no access to the organization's drafts, production records, knowledge or credentials.
- Can be reset without affecting playground or production state.
- The disposable instance belongs to the signed-in user, not to the organization: another member
  cannot see or reset it, and it is deleted when its lifetime ends or the user leaves the
  organization.
- Its usage is metered to the Pixel demo budget, not the customer organization's budget, under the
  same per-user limits and kill switch as the public Test Drive.

### Production Runtime

- Published releases only.
- Customer-owned data and configured providers.
- Execution policy and audit active.
- No evaluator scripts, sample records or demo shortcuts.

## 10. Observability, Privacy and Audit

Use OpenTelemetry-compatible traces, metrics and logs with one trace ID across control-plane and
runtime calls. Telemetry carries identifiers and classifications, not customer content.

Allowed trace fields include:

- redacted organization/product/environment IDs;
- release checksum prefix;
- stage name and duration;
- routing outcome and refusal code;
- action key, never action secrets or unrestricted parameters;
- provider name, model, units, status and duration;
- execution state and redacted receipt reference.

Forbidden by default:

- raw prompts and model output;
- full snapshots or retrieved passages;
- access tokens, API keys and execution keys;
- personal data and customer record bodies;
- provider error bodies before sanitization.

Trace Inspector is permission-gated, redacted and retention-bounded. Support impersonation is off by
default and, if introduced, requires customer consent, time-bounded access and an audit trail.

Audit events are append-only and cover login policy changes, membership changes, credentials,
knowledge ingestion, policy changes, release publication, deployment, rollback and data export or
deletion.

## 11. Usage, Budgets and Billing

The runtime usage ledger is the enforcement and measured-usage source of truth. The control plane
owns configured limits and billing entitlements, distributes a versioned budget policy, and stores
redacted aggregates received from the runtime. Billing-provider events never overwrite measured
provider usage. Reconciliation compares runtime totals, control-plane aggregates and billed meter
events by stable event ID; disagreement raises an operator alert and never grants more allowance.

- Reserve before paid work and settle after the provider response.
- Enforce per-user, product, organization and deployment limits.
- Limit concurrent reasoning, speech and ingestion work.
- Provide warning thresholds, hard ceilings and a global paid-provider kill switch.
- Meter events have stable unique identifiers so retries do not double bill.
- Billing webhooks use a signed, idempotent inbox and tolerate reordering.
- A billing outage cannot grant a higher entitlement; use the last verified entitlement with a
  documented grace policy.

## 12. Console Information Architecture

Primary navigation:

1. **Overview** - deployment health, release state, budget and actionable alerts.
2. **Products** - product directory and environment status.
3. **Build** - definition, knowledge, actions and policies for one product.
4. **Test** - playground, evaluations and redacted turn traces.
5. **Deploy** - releases, compatibility, rollout and rollback.
6. **Operate** - usage, latency, provider health and incidents.
7. **Audit** - immutable administrative event history.
8. **Organization** - members, roles, credentials, billing and retention.

The first authenticated screen is the console, not a marketing page. Routes include organization,
product and environment identity; the backend derives and verifies the same context from auth.

## 13. Pixel Visual System

The console should look like an instrument, not an AI landing page.

### Direction

- Light and dark themes, with neutral operational surfaces.
- Graphite, white and cool gray as structural colors.
- Cobalt for selection, emerald for healthy, amber for warning and crimson for destructive/error.
- No purple/pink gradients, glassmorphism, glow decoration or floating color blobs.
- Radius 4-6 px; cards only for repeated records or framed tools.
- Full-width work areas for editors, tables, traces and release comparisons.
- Geist Sans and Geist Mono, with letter spacing `0`.
- 4 px base spacing with stable control dimensions.

Pixel's recognizable motif is a precise square status raster used sparingly for deployment and
pipeline state. It is functional: each cell represents an actual stage or state, never decoration.

### Component Stack

- Existing Next.js and React versions remain.
- Tailwind CSS v4 for tokens and layout utilities after an isolated migration proof.
- Radix primitives as the single accessibility/focus primitive library.
- Lucide icons.
- TanStack Table for dense data grids.
- Monaco only for the definition editor.
- Motion only for drawers, presence and layout transitions that CSS cannot express cleanly.

Do not mix Radix and Base UI without a reviewed exception. Animation uses short opacity/transform
transitions, never constant motion or ornamental springs.

### Accessibility Gate

- WCAG 2.2 AA target.
- Complete keyboard operation and visible, unobscured focus.
- Text contrast at least 4.5:1 and UI-state contrast at least 3:1.
- 200% zoom without lost functionality or horizontal page scrolling.
- `prefers-reduced-motion` removes nonessential movement.
- No status communicated by color alone.
- Screen-reader names and live-region behavior tested.

## 14. Security Program

Before pilot, Pixel has:

- documented trust boundaries and threat model;
- tenant-isolation matrix for every data store, cache, queue and object path;
- SSRF, archive bomb, malicious document and prompt-injection tests;
- CSRF, CORS, CSP, secure-cookie and redirect allowlist policy;
- dependency pinning, automated vulnerability review and SBOM generation;
- secret rotation and emergency provider kill procedures;
- encrypted backups, retention and successful restore evidence;
- incident response and customer notification procedure;
- export and deletion workflow with derived-data tracking;
- abuse detection, rate limits and noisy-neighbor controls;
- no production debug endpoint capable of returning raw customer state.

OWASP recommends establishing tenant context early, treating client tenant IDs only as selectors,
and testing isolation through the same roles and connection paths used in production.

## 15. Relationship to the Active Milestone

This plan does not replace Milestone 3. After 5d, the existing roadmap resumes:

| Existing step | Required proof | Relationship to this plan |
| --- | --- | --- |
| 3.3 One backend pipeline | Browser presents; backend decides every turn | Prerequisite for any console playground or SDK |
| 3.4 Knowledge | Tenant/product/version-bound retrieval | Supplies the knowledge boundary used by SaaS Phase 4 |
| 3.5 Records | Generic records, relationships, scopes and rehearsed migration | Supplies the runtime data model; SaaS Phase 1 migrates control-plane metadata only |
| 3.6 Web | Generic shell and product adapters; legacy shims removed | Supplies the adapter contract used by SaaS Phases 5 and 7 |
| 3.7 Second product | Billing product runs beside the reference product | Proves Pixel is not secretly project-management-specific |
| 3.8 Frozen core | Definition-only workflow change, deletion tests, no purity exceptions | Gate for beginning SaaS implementation |

Planning and provider evaluation may continue after 5d, but implementation of the SaaS control
plane begins only after 3.8 is signed off. Work already completed in 3.3-3.8 is reused and is not
rebuilt under a new phase name.

### 15.1 Owner decision A1: when invited members first sign in

The owner decided on 2026-09-21 that real sign-in follows slices 5a-5d. The SaaS phases in section
17 begin only after 3.8, which would move sign-in much later. That change is the owner's to make, so
both options are stated.

- **Option A: sign-in with SaaS Phase 2, after 3.8.** One identity build, on PostgreSQL and the
  final control plane. Invited members wait until Milestone 3 is complete.
- **Option B: a narrow sign-in step right after 5d, then Phase 2 later.** Scope, and nothing more:
  - one-time email code sign-in through the identity adapter and the provider chosen by the proof
    gate (section 5.2 runs once, at this step);
  - invited members of the existing organization only, mapped onto today's organization, team and
    membership tables; no customer onboarding, no console, no request-access form;
  - after sign-in, the existing Pixel page and demo mode inside it, with demo instances owned by the
    user (section 9);
  - read-only authentication middleware, membership removal honoured, visitor and member tokens
    separated by fixed issuer and algorithm;
  - Phase 2 later moves the same adapter and records to PostgreSQL; nothing built here is thrown
    away.

  It adds one reviewed step to Milestone 3 and moves the provider decision earlier.

**Recommendation: Option B.** It keeps the owner's recorded sequence, gives invited people a real
signed-in Pixel sooner, and puts identity behind the same adapter Phase 2 uses, so the early step is
reused rather than replaced. Option A is the lower-effort choice if early sign-in is not needed.

## 16. Current Production Transition

Current production remains the reference deployment while Milestone 3 finishes: one Railway API
process, SQLite on its volume, the `pixel-dev` demonstration organization and private visitor demo
instances.

Transition sequence:

1. Freeze and back up the current SQLite database; prove restore before migration.
2. Build a PostgreSQL staging environment and migrate organization, team, product, definition,
   session-policy and usage metadata with reconciled row counts and checksums.
3. Keep public visitor instances synthetic and disposable; do not migrate expired instances.
4. Run the reference product and second product on staging through the generic 3.8 core.
5. Rehearse cutover and rollback using a read-only window and an explicit write freeze.
6. Move the public Test Drive to the standard shared runtime partition. It remains logically
   isolated; a physically separate demo deployment is a later operational choice.
7. Preserve the SQLite backup read-only through the rollback window, then destroy it under the
   documented retention policy.

No customer production data exists in today's reference deployment, so this transition is not used
as evidence that a future customer-data migration is safe.

## 17. SaaS Delivery Phases After Milestone 3.8

### Phase 0 - Architecture Freeze

Deliverables:

- control-plane/data-plane architecture decision;
- tenant and data classification inventory;
- standard, dedicated and sovereign deployment contracts;
- threat model and authorization matrix;
- accepted provider evaluation criteria.

Exit evidence: every existing table and runtime component has an owner, data class, tenant boundary
and destination architecture.

### Phase 1 - PostgreSQL Platform Foundation

Deliverables:

- control-plane PostgreSQL schema and migration tooling;
- RLS policies and least-privileged request role;
- migration path from existing metadata;
- webhook inbox, audit ledger and asynchronous job foundation;
- backup, restore and expand/contract migration procedures.

Exit evidence: isolation tests run through the deployed request role; restore succeeds; adding an
unclassified tenant table fails CI.

### Phase 2 - Identity and Organization Boundary

If decision A1 chose Option B, the provider proof and the email-code sign-in already exist; this
phase moves them to PostgreSQL and adds everything below that is new.

Deliverables:

- provider proof and final provider decision (unless already made under A1 Option B);
- external identity adapter;
- explicit organization onboarding;
- invitation and organization switching;
- webhook synchronization and reconciliation;
- permission middleware and API-key lifecycle.

Exit evidence: cross-organization matrix is entirely denied; revocation, duplicate webhooks,
out-of-order events and suspended organizations are proven.

### Phase 3 - Product and Environment Lifecycle

Deliverables:

- products and development/staging/production environments;
- draft editing with optimistic revisions;
- validation findings and review workflow;
- immutable release publication, signing, deployment and rollback;
- complete audit events.

Exit evidence: a release can be reproduced from its manifest; a draft cannot reach production;
rollback requires no database rewrite.

### Phase 4 - Knowledge Ingestion

Deliverables:

- safe document upload and URL ingestion;
- isolated ingestion workers;
- versioned chunks and embeddings;
- citation evaluation and deletion propagation;
- ingestion status and failure recovery.

Exit evidence: retrieval never crosses organization/product/environment/version, and deleting a
source removes every derived online copy within the declared SLA.

### Phase 5 - Runtime Deployment and Integration SDK

Deliverables:

- shared-runtime provisioning contract and later dedicated-deployment compatibility contract;
- PostgreSQL implementation of the generic records, sessions, execution ledger and usage ledger,
  with tenant/product/environment policies and a rehearsed transition from the reference SQLite
  runtime;
- signed release distribution;
- scoped runtime and embed tokens;
- reference UI adapter, iframe bridge and customer SDK;
- provider-secret and budget injection;
- deployment health and redacted telemetry export.

Exit evidence: no organization can use a credential, query, cache key, queue message, object path or
retrieval filter to reach another organization; origin and message-protocol attacks fail. The same
release can be assigned to a dedicated test deployment without changing product semantics.

### Phase 6 - Console Design System and Shell

Deliverables:

- visual tokens and component primitives;
- responsive authenticated shell;
- organization, product and environment switching;
- command palette backed by authorized server search;
- accessibility and screenshot baselines.

Exit evidence: desktop and mobile visual regression, keyboard journey and WCAG checks pass; no
generic demo or marketing UI remains inside the console.

### Phase 7 - Build, Test and Deploy Console

Deliverables:

- definition workbench and diff;
- knowledge manager;
- action/policy matrix;
- authenticated playground;
- evaluations and redacted trace inspector;
- release review, deployment and rollback screens.

Exit evidence: an authorized customer can onboard a synthetic second product, test it, publish it,
deploy it and roll it back without direct database or command-line access.

### Phase 8 - Operate, Usage and Commercial Controls

Deliverables:

- OpenTelemetry instrumentation;
- provider latency, error and budget views;
- audit UI and exports;
- billing entitlements and metering integration;
- alerts, kill switches and incident controls.

Exit evidence: duplicate billing events do not double count; hard limits block paid work before a
provider call; dashboards contain no forbidden customer content.

### Phase 9 - Pilot Hardening

Deliverables:

- independent security review;
- load, concurrency, restart and failure testing;
- disaster recovery exercise;
- data export/deletion exercise;
- operational runbooks and support policy;
- pilot readiness report.

Exit evidence: all severity-one and severity-two findings are closed; recovery objectives are met;
tenant-isolation and restore evidence is attached to the release.

### Phase 10 - Public Site and Controlled Access Funnel

Deliverables:

- factual product site and pricing;
- public Test Drive entry;
- request-access form, review queue, invitation and one-time-code onboarding;
- documentation and SDK quickstart.

Exit evidence: the public funnel cannot reach customer runtime data, and every advertised capability
is available in the shipped product. Requesting access never creates an account, membership or
organization until an authorized administrator issues an invitation.

## 18. Quality Gates Applied to Every Phase

Each phase requires:

1. reviewed plan and threat-boundary delta;
2. migration and rollback plan;
3. unit, contract, API and browser tests proportional to risk;
4. negative tenant and permission tests;
5. Linux CI green before merge;
6. no paid-provider test unless separately authorized and budgeted;
7. documentation and operator runbook updated;
8. production evidence before sign-off;
9. no unresolved critical or high-severity finding;
10. one branch for the active phase, following the repository's standing Git rule.

## 19. Readiness Targets

Initial pilot targets:

- zero known cross-tenant access paths;
- 100% classified tenant-owned tables and object prefixes;
- 100% privileged mutations audited;
- no raw customer content in default telemetry;
- control-plane API p95 under 300 ms excluding asynchronous jobs;
- deterministic runtime overhead p95 under 75 ms excluding model, retrieval-provider and speech
  network time, and no more than 20% worse than the signed-off pre-SaaS production baseline;
- hard budget rejection before a paid provider call;
- successful encrypted backup restore;
- documented recovery point and recovery time objectives accepted for the pilot;
- WCAG 2.2 AA automated checks plus manual keyboard and screen-reader journeys;
- product onboarding demonstrated with at least two materially different product definitions.

These are release gates, not marketing claims. Targets may tighten after measured pilot traffic.

## 20. Explicitly Deferred

The first SaaS release does not include:

- unrestricted browser control;
- executing uploaded customer code;
- production writes without registered actions;
- cross-customer analytics containing customer content;
- autonomous publication or deployment by a model;
- a marketplace for third-party adapters;
- customer-defined arbitrary code inside the Pixel runtime;
- sovereign deployment before the dedicated-deployment model is proven;
- enterprise SSO, SCIM and directory sync unless required by an approved pilot customer.

## 21. Proposed Decisions Requiring Approval

Proposed direction:

- Pixel is a platform, not the reference issue tracker.
- SaaS control plane and customer runtime data plane are separate.
- the pilot uses the approved shared multi-tenant deployment with strict logical isolation;
  dedicated deployments remain a later enterprise option.
- PostgreSQL precedes customer-facing console launch.
- the identity provider is chosen by the proof gate between WorkOS AuthKit and Clerk, on the pilot
  requirements in section 5.1; Pixel remains provider neutral;
- request access is Pixel-owned, verified, rate-limited and retention-bounded (section 5.6);
- decision A1 (section 15.1): sign-in with Phase 2 after 3.8, or a narrow invited-member sign-in
  step right after 5d. Recommended: the narrow step.
- published releases are immutable and signed.
- the console uses an instrument-grade Pixel visual system, not gradient/glass AI styling.
- the public demo remains synthetic, disposable and logically separate from customer runtimes;
  physical separation is a later deployment decision.

Implementation starts only after Milestone 3.8 is signed off and SaaS Phase 0 is approved.

## 22. Research Basis

Sources reviewed 2026-09-21. Provider features, pricing, subprocessors and data-location terms must
be rechecked at the Phase 2 decision because they are living commercial documentation.

- OWASP Multi-Tenant Security Cheat Sheet:
  https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html
- PostgreSQL Row Security Policies:
  https://www.postgresql.org/docs/17/ddl-rowsecurity.html
- WorkOS AuthKit and Python SDK:
  https://workos.com/docs/authkit/overview
  https://workos.com/docs/sdks/python
- Clerk multi-tenant architecture and session tokens:
  https://clerk.com/docs/guides/how-clerk-works/multi-tenant-architecture
  https://clerk.com/docs/guides/sessions/session-tokens
- MDN iframe, CSP and postMessage security:
  https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe
  https://developer.mozilla.org/en-US/docs/Web/API/Window/postMessage
  https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy
- OpenTelemetry signals and semantic conventions:
  https://opentelemetry.io/docs/concepts/signals/
  https://opentelemetry.io/docs/concepts/semantic-conventions/
- WCAG 2.2:
  https://www.w3.org/TR/wcag/
