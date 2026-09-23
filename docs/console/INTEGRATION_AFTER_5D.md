# Connecting the Foundation After 5d

Nothing here is connected to the running application. This is how each part connects later,
through reviewed phases, and what must not happen before then.

## Connection points

| Foundation part | Connects to | When | How |
| --- | --- | --- | --- |
| `IdentityAdapter` | The chosen provider (WorkOS AuthKit or Clerk, decided by the proof gate) | Plan decision A1 (early invited-member sign-in) or SaaS Phase 2 | A provider class implementing the same protocol; `InMemoryIdentity` stays as the test double; the existing tests run against both |
| `authorize()` | API middleware for console routes | Phase 2 | Called with a `Principal` built from the verified session and a `Directory` loaded per request; never from request bodies |
| Role model | Today's `memberships` table | Phase 1 migration | Roles map one-to-one (`org_admin`, `team_admin`, `team_member`); additional grants become rows |
| `AccessRequests` | Public request-access endpoint | Phase 10 (or A1 option B without the form) | Persisted with the same retention; `purge` becomes a scheduled job |
| Product contracts and `openapi.yaml` | Control-plane API | Phase 3 | Handlers implement the contract; the state machines move behind PostgreSQL with optimistic revisions |
| Release and deployment rules | Today's definition registry lifecycle | Phase 3 | The lifecycle already matches `definition_versions` (draft, validated, published, retired, revoked) |
| Ingestion checks | Upload endpoint and workers | Phase 4 | Called before storage and before unpacking; the URL fetcher pins the checked address |
| Audit schema | Audit ledger | Phase 1 | Events validated by `AuditEvent` before insert |
| Console prototype | Real console | Phase 6 | Components and tokens move over; mock data is replaced by the API; the permission port keeps its parity test |

## Domain ownership

| Domain | Owner module (later) | Stores |
| --- | --- | --- |
| Identity and sessions | identity service behind `IdentityAdapter` | Provider plus `users`, `external_identities` |
| Organizations, teams, memberships, invitations, access requests | control plane | Control PostgreSQL |
| Products, releases, deployments | control plane | Control PostgreSQL, signed artifacts in object storage |
| Knowledge | ingestion workers and runtime retrieval | Tenant-prefixed object storage and a tenant-filtered index |
| Conversations, records, execution ledger | runtime (today's engine after 5d) | Runtime database |
| Audit | control plane | Append-only audit store |

## Deployment model

Shared multi-tenant by default, with strict logical isolation; dedicated deployments are a later
enterprise option (plan section 3.2). The console is a separate web app from the public demo.

## Incident and recovery (initial)

- **Suspected cross-tenant access:** disable the affected route or feature flag, revoke sessions for
  the affected organizations, preserve audit and access logs, notify per the incident procedure.
- **Identity provider outage:** existing sessions continue until expiry; no new sign-ins; no local
  fallback that bypasses the provider.
- **Leaked signing key or credential:** freeze publication, revoke the key, redeploy known-good
  releases signed by a trusted key (plan section 6.4).
- **Bad release:** revoke it; deployments using it are disabled until an operator selects another.
- **Data restore:** restore to staging first, replay deletion tombstones, verify, then promote.

## Must not happen before its phase

Importing anything from `saas/` into `apps/` or `products/`; adding `saas/` to the root workspaces,
Dockerfile, Railway or CI deploy steps; real identity, database or provider integration.
