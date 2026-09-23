# Pixel SaaS Threat Model (foundation draft)

Scope: the control plane and console described in `docs/PIXEL_SAAS_PLATFORM_PLAN.md`. This draft is
the Phase 0 starting point; each later phase adds its own threat-boundary delta.

## Trust boundaries

| Boundary | Untrusted side | Rule |
| --- | --- | --- |
| Browser → console API | Everything the browser sends, including the organization and product in the URL | The server derives organization, team and permissions from the session; URL values are selectors only |
| Identity provider → Pixel | Tokens and webhooks | Verify signature, issuer, audience, expiry and type; webhooks through a signed, idempotent inbox; authentication never creates access |
| Customer uploads → ingestion | Files, archives, URLs, OpenAPI specs | Size, format, archive and SSRF checks before storage (`pixel_saas.ingestion`); no customer code runs |
| Knowledge → model | Retrieved text | Untrusted data, delimited in prompts, never an instruction and never authorization |
| Model → platform | Proposed actions | Validated against the definition and caller's scope; mutations require confirmation (existing Milestone 3 rule) |
| Control plane → runtime | Releases | Signed, immutable, checksum-verified before activation |
| Public test drive → everything else | Anonymous visitors | Separate partition, synthetic data, private disposable instances, spending caps |

## Tenant-isolation matrix

Every store must answer "deny" for every row below. Rows marked **tested** are covered by
`platform/tests/test_authorization.py` today; the rest become tests when the store exists.

| Attempt | Expected | State |
| --- | --- | --- |
| Any role, any permission, another organization | Denied, `no_access` | tested (matrix) |
| Same IDs reused in another organization | Denied | tested (matrix) |
| Unknown organization versus foreign organization | Same answer | tested |
| Team role, another team's product or environment | Denied | tested (matrix) |
| Team role, organization-only permission | Denied | tested |
| Suspended or removed membership | Denied | tested |
| Suspended organization or team | Denied | tested |
| Archived product, any write | Denied (reads allowed) | tested |
| A membership object naming a different user | Ignored | tested |
| Cache key, queue message, object path or retrieval filter without organization and product | Refused at construction | Phase 1 and 4 |
| Query through the deployed database role without tenant context | Returns nothing (row-level security) | Phase 1 |

## Abuse cases

| Case | Mitigation | Evidence |
| --- | --- | --- |
| Enumerate accounts through sign-in | Every address gets the same response and a delivery attempt | `test_identity.EmailCodeTest.test_every_address_gets_the_same_answer` |
| Brute-force a code | 6 digits, 10-minute expiry, 5 attempts, single use | `test_identity.EmailCodeTest` |
| Codes or tokens leaked from storage | Stored as keyed hashes only | `test_codes_are_not_stored_in_clear`, `test_tokens_are_not_stored_in_clear` |
| Forwarded or intercepted invitation link | Accepting requires signing in as the invited address | `test_a_forwarded_link_is_useless_to_another_address` |
| Replay an accepted invitation | One use only | `test_an_invitation_is_accepted_once` |
| Stale invitation | 7-day expiry; revocation | `test_expired_revoked_and_unknown_invitations_are_refused` |
| Sign in to regain access after suspension | Suspension survives sign-in; sessions revoked | `test_suspension_is_not_undone_by_signing_in_again` |
| Removed member keeps a live session | Removal and suspension end sessions at once | `test_removal_and_suspension_end_sessions_immediately` |
| Authenticated stranger | No account, organization or session is created | `test_authenticating_creates_nothing` |
| Flood the request-access form | Per-address, per-network and global daily limits; one open request per address | `test_access_requests` |
| Request access as a backdoor | A request never grants access; only an administrator's invitation does | `test_a_request_grants_nothing` |
| Malicious archive (traversal, links, bombs, nesting) | Refused from the directory before unpacking | `test_ingestion.ArchiveTest` |
| SSRF through URL ingestion | https only, no credentials, every resolved address public, pinned and rechecked on redirect | `test_ingestion.UrlTest` |
| Executable upload | Refused by extension even inside archives; no code runs | `test_ingestion` |
| Personal data in audit logs | Audit schema accepts identifiers and codes only | `test_audit` |
| Account takeover through email change | Email is profile data, not identity; the provider's immutable subject is the key | Design rule (`identity.py`) |
| Privilege escalation by grant | Organization-only permissions cannot be granted to team roles | `test_team_roles_never_hold_organization_permissions` |

## Audit events

Schema: `pixel_saas/audit.py`. Every privileged mutation writes one append-only event with the actor
ID, action, target type and ID, result, and allowlisted metadata (role, team, environment, release
version, checksum prefix, reason code, request ID, credential prefix, count). Emails, names, record
bodies, prompts and tokens are rejected by construction.

## Retention and deletion

| Data | Retention |
| --- | --- |
| Access requests | Rejected or withdrawn: 90 days after decision; unreviewed: 90 days; approved: until the invitation settles; deleted on request |
| Abuse counters | 30 days, keyed hashes only |
| Sign-in codes | Until used or 10 minutes |
| Invitations | Token hash until accepted, revoked or expired |
| Sessions | 12 hours, revoked on suspension or removal |
| Audit events | Contractual retention (plan section 4.4) |
| Backups | 30 days; deletion tombstones replayed before any restore serves traffic |
