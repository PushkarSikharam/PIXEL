# Acceptance Test Definitions

Each test has an ID used in pull requests and phase reports. **Implemented** tests run today;
**Defined** tests become executable when their phase builds the store or endpoint.

| ID | Scenario | Expected | State |
| --- | --- | --- | --- |
| AT-01 | A member of organization A requests any product, release, member or usage of organization B, by guessed or reused ID | 404, identical to a missing resource; nothing about B in logs | Implemented for authorization (`IsolationMatrixTest`); Defined for endpoints (Phase 2) |
| AT-02 | Retrieval for product P returns a passage of product Q, same or other organization | Never; retrieval is constructed with scoped context | Defined (Phase 4) |
| AT-03 | Switching product mid-conversation | Confirmation first; the new product starts a new conversation; no memory, draft or selection carries over | Implemented in the console prototype (switch and leave dialogs, discard resets the draft); Defined for the runtime (Phase 5) |
| AT-04 | A session, cache entry or trace of product P is readable after switching to Q | Never | Defined (Phase 5) |
| AT-05 | A member is removed or suspended while signed in | Their sessions end at once; the next request is refused; signing in again restores nothing | Implemented (`SessionTest`) |
| AT-06 | A removed member's pending invitation or old link | Unusable; only a new invitation restores access | Implemented (`InvitationTest`) |
| AT-07 | Two products with the same name in one organization, differing only in case or spacing | Refused `name_taken`; allowed across organizations | Implemented (`test_products`) |
| AT-08 | Malicious uploads: traversal, absolute paths, links, bombs, nested archives, executables, SSRF URLs | Refused before storage or unpacking | Implemented (`test_ingestion`) |
| AT-09 | Analysis proposes an action that changes data | It starts disabled and cannot run without confirmation | Implemented (`onboarding.test.ts`) |
| AT-10 | Generated action that no adapter can express, or with an unknown capability | Publication blocked | Defined (Phase 7) |
| AT-11 | A draft is deployed to any environment | Refused `release_not_published` | Implemented (`test_products`) |
| AT-12 | Rollback | A new deployment of an earlier published release; no release edited; open sessions keep their pin | Implemented for contracts (`test_products`); Defined for runtime (Phase 5) |
| AT-13 | A release is revoked while deployed | Its deployments are disabled; sessions end on their next request | Implemented for contracts; Defined for runtime |
| AT-14 | The authenticated stranger | Verifies an email code but gets no account, organization or session | Implemented (`NoImplicitAccessTest`, `mock-identity.test.ts`) |
| AT-15 | Request-access flood and duplicate | Rate limited; one open request per address; a request is never access | Implemented (`test_access_requests`) |
| AT-16 | Audit event carrying an email or record body | Rejected before writing | Implemented (`test_audit`) |
| AT-17 | Console and platform parity | TypeScript and Python permission models agree on every reviewed policy case | Implemented (`permissions.test.ts` reads the Python fixture) |
| AT-18 | Colour contrast | Every text pairing ≥ 4.5:1 and UI boundary ≥ 3:1 in both themes | Implemented (`contrast.test.ts`) |
| AT-19 | Deleting `saas/` | The application, its build, container and CI are unaffected | Implemented (`test_boundaries`) |
