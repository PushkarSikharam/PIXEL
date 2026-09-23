# Pixel System Delivery Status

This is an implementation inventory, not a claim that the SaaS product is finished.

## Connected Work

- Email-code sign-in endpoints: eight digits, ten-minute expiry, one use, five guesses,
  persistent per-address and deployment-wide delivery limits. Codes are HMAC digests at rest.
- Verified self-signup creates a private organization and its default team. Self-signup and
  email delivery are separately disabled by default. Existing accounts return to their own organization.
- Customer sign-in no longer calls synthetic login. Logout revokes the server session.
- The console loads the authenticated organization and real accessible products. It does not
  substitute sample products or simulated deployments when a request fails.
- Product import accepts an approved definition, validates its contract and requires ownership
  by the caller's organization. Source storage claims definition ownership atomically.
- The product workspace has a collapsible assistant on the left, fresh conversation IDs on
  product changes, backend-keyed writes, receipts, navigation and opt-in cloud audio playback.
- Browser-supported microphone input can submit a spoken turn and request its spoken reply.
  Microphone permission is requested only after a click. Unsupported browsers keep typed input.
- Product speech reads the pinned definition's voice style rather than a demo-only lookup.
- Approved plain-text/Markdown sources are versioned per product and definition checksum.
  Retrieval is bounded keyword matching, not embedding search or automatic deep understanding.
  Source publication starts a new console conversation; earlier sessions retain their version.
- Approved sources are product-wide: they must be suitable for everyone allowed into that
  product. They are not confidential record-scoped documents. The upload control says this.
- Knowledge capacity is bounded: sixteen documents, 128,000 characters and twenty publication
  increments per product. There is no pruning of historical sources while sessions may use them.
- The separate guided demo remains at `/demo`.

## Required Configuration

Do not enable the email flow without all of these server-side settings:

- `PIXEL_AUTH_SECRET`: a stable random secret, not a browser environment variable.
- `PIXEL_SMTP_HOST`, `PIXEL_SMTP_USER`, `PIXEL_SMTP_PASSWORD`, `PIXEL_EMAIL_FROM`:
  an authenticated sender. Resend is delivered through its HTTPS API when the host is
  `smtp.resend.com`; other SMTP hosts use TLS on port 465. Verify delivery with the mailbox
  provider.
- `PIXEL_EMAIL_LOGIN_ENABLED=true` after configuration and delivery validation.
- `PIXEL_ENGINE_MODE=definition` after its cutover checks; customer sign-in refuses legacy mode.
- `PIXEL_SELF_SIGNUP_ENABLED=true` only when new verified users may create organizations.

No SMTP credentials have been provisioned, no real email has been sent in these tests, and no
payment account has been provisioned. Synthetic administrator access remains blocked.
The account currently uses the existing bearer-token transport and sessionStorage. A
same-origin HttpOnly-cookie session with CSRF protection remains an authentication hardening task.

## Not Complete

- Customer invitations, multi-organization switching, team/role administration and recovery.
- Schema-driven manual record forms, revision-conflict UX and relationship selection.
- Audio streaming, physical-device microphone verification and production voice-latency measurements.
- Automatic conversion of arbitrary source code or documents into an approved product definition.
- Semantic retrieval, document replacement/deletion, scoped document permissions and citation evaluation.
- Customer product lifecycle, deployment management, audit and usage dashboards.
- Billing, checkout, webhooks, entitlements and billing-provider configuration.
- Backup automation, off-volume encrypted retention, restore monitoring, shared rate-limit storage.
- Full accessibility review, production security review and a real customer onboarding pilot.

Unconnected management screens are not shown as functioning customer tools. The designed
prototype code remains in the repository, but its simulated data is not exposed as live data.
Do not call this list complete, production-ready, or a replacement for the remaining roadmap.
