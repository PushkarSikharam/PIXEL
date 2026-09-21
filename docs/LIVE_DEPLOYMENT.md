# Pixel Live Deployment

The live application has two services. Vercel serves the Next.js interface. A long-running API
service runs FastAPI and owns authentication, records, conversation state, usage accounting and
paid-provider calls. The browser reaches the API only through the same-origin `/api/agent/*`
rewrite.

## API service

Deploy the repository with `Dockerfile.api`. `railway.json` configures the container and the
`/health` readiness check.

Attach one persistent volume at `/data`, then set:

```text
PIXEL_DB_PATH=/data/pixel-live.sqlite3
PIXEL_DEPLOYMENT_ID=live-demo
PIXEL_AUTH_SECRET=<long random value>
PIXEL_SYNTHETIC_DEMO=false
PIXEL_DEMO_SEEDS=true
PIXEL_SESSION_MAX_AGE_SECONDS=86400
PIXEL_DEMO_INSTANCE_ACTIVE_LIMIT=1000
PIXEL_DEMO_INSTANCE_RECORD_LIMIT=1000
PIXEL_DEMO_INSTANCE_TTL_SECONDS=86400
PIXEL_DEMO_INSTANCE_IDLE_SECONDS=7200
PIXEL_DEMO_INSTANCE_RECLAIM_SECONDS=900
```

Never set `PIXEL_DEMO_ADMIN_LOGIN` or `PIXEL_RATE_LIMITS=off` on a deployment. Both exist only for
isolated test harnesses.

`PIXEL_SYNTHETIC_DEMO` controls only the legacy member demo login, which signs callers into the
shared member records. The public web app no longer uses it, so production keeps it off: with it
on, anyone calling the API directly could still read and change the shared member records.
`PIXEL_DEMO_LOGIN_USERS` and `PIXEL_DEMO_IDLE_RESET_MINUTES` only matter while it is on.

Provider credentials and budgets belong only to the API service. Copy the paid-provider settings
from `.env.example`; never add them to Vercel or expose them as `NEXT_PUBLIC_*` variables.

Do not deploy this SQLite configuration without the mounted volume. Container-local storage is
temporary and would invalidate logins, usage history and saved demo records whenever the service
restarts.

### Readiness means a conversation can start

`GET /health` and `GET /api/health` return `200 {"status": "ok"}` only when every active product
can start a conversation. The check is the one a real turn uses: the bound definition version is
registered, published, and its file still matches the registered checksum. Otherwise they return
`503 {"status": "unhealthy", "reason": "sessions_cannot_start", "products": <count>}`. The public
body names no tenant, product or definition. The result is cached for 30 seconds.

Operators get the detail in two places: the server log line `session_start_unavailable` (logger
`pixel.readiness`), and the readiness command below. Because Railway only routes traffic to a
deployment whose health check passes, a deployment that could not start conversations no longer
goes live looking healthy.

This check exists because of the 2026-09-18 outage. The production database had registered the
Linear definition with a checksum over Windows (CRLF) line endings, while the Linux image carries
the same file with LF endings. Checksums are over exact bytes, so every new conversation failed
with `definition_invalid`, voice with it, while `/health` still said `ok`. `.gitattributes` now
pins product definition and adapter files to LF on every checkout.

### Operator commands

Run these from a shell in the API container (`/app`), or from the repository root locally:

```text
PYTHONPATH=apps/api python -m app.ops check-readiness
PYTHONPATH=apps/api python -m app.ops reset-demo-data
```

`check-readiness` prints every active product that cannot start a conversation and why, and exits
non-zero if there is one. `reset-demo-data` restores the legacy member-owned demo records to the
seed; it does not alter private visitor instances. Neither command is reachable through the
public API.

### Private public demos

- The web app requests a product-scoped visitor session. The API allocates a unique visitor,
  instance and generation, then binds all three to the signed token.
- Every instance starts from the same approved immutable product seed. Tickets, projects, cycles,
  members, conversations and execution receipts are private to that instance.
- Refresh and **Restart** retain the visitor's records. **Reset** asks for confirmation, restores
  only that visitor's seed, rotates the generation and token, and invalidates old sessions and
  pending actions.
- `POST /api/demo-data/reset` remains administrator-only and affects only legacy member-owned demo
  records. Public visitors use `POST /api/demo-data/reset-mine` and cannot reset another visitor.
- Expired instances fail closed and are pruned in bounded batches. A missing private context never
  falls back to member records or product seed files: record lookups take the caller's records
  explicitly and refuse to run without them.
- **Capacity cannot be held by allocating once.** When all `PIXEL_DEMO_INSTANCE_ACTIVE_LIMIT`
  slots are taken, a new visitor takes over the instance unused for longest, provided it has been
  unused for `PIXEL_DEMO_INSTANCE_RECLAIM_SECONDS` (15 minutes). An instance used within that window
  is never evicted; if every instance is in use, allocation answers `429`. The server logs
  `demo_instance_reclaimed` and `demo_capacity_reached` (logger `pixel.demo`).
- **The seed is pinned.** The product package records the approved seed version and checksum. If
  the seed files change without an updated pin, new visitors and private resets are refused
  instead of starting from an unreviewed seed, and a test fails in CI.
- `PIXEL_DEMO_LOGIN_USERS` and `PIXEL_DEMO_IDLE_RESET_MINUTES` apply only to the legacy member demo
  login. The public web app does not use that login.

### Abuse limits

Limits apply per client address, authenticated identity and deployment. A refused request is not
counted, and the response is `429` with `Retry-After`.

| Route | Per client, per minute | Per identity, per minute | Whole deployment, per minute |
| --- | --- | --- | --- |
| Visitor allocation and legacy demo login | 30 | — | 120 |
| Conversation turn | 120 | 60 | 600 |
| Speech | 60 | 60 | 300 |
| Record write | 30 | 60 | 180 |
| Private demo reset and global data reset | 10 | 5 | 120 |

Every public visitor now has their own identity, so the per-identity limit applies to one visitor;
the deployment column is the ceiling that neither forged addresses nor fresh identities bypass.

- **The client address is best effort.** It is the first `X-Forwarded-For` entry. A caller can
  forge it, so visitor allocation also has a deployment-wide ceiling that fresh identities and
  changing addresses cannot bypass.
- **Unverified assumption:** that the Vercel rewrite forwards the visitor's address. If it does
  not, every visitor shares one client bucket. Check it after each deployment (release smoke test
  below).
- **Limits are held in memory, per replica.** Run one API replica until a shared store exists.

### Spending

Every anonymous visitor can start paid provider calls until the deployment's server-side budget
is exhausted. The daily budgets in `.env.example` bound the cost, and the limits above bound the
rate. Concurrency limits and spending alerts are **not built yet**; until they are, this
deployment is a demo, not a production service.

### Voice

Edith speaks with Azure Speech (`en-US-AvaMultilingualNeural`, chat style) and nothing else:
`PIXEL_SPEECH_PROVIDERS` defaults to `azure`. The Gemini key stays configured for reasoning but is
never used as a voice unless that setting lists it. When Azure cannot answer (for example, the
free tier's monthly allowance is used up), the page falls back to the browser's own voice, which
costs nothing. The Azure resource is on the free (F0) tier.

### Backups: not in place

The SQLite database on the volume has no backups. Before any real pilot we need scheduled,
encrypted snapshots, a retention policy, copies outside this Railway volume, one documented
restore test, and monitoring of disk use and backup failures.

## Vercel web service

Set these variables for Production:

```text
PIXEL_AGENT_API_BASE_URL=https://<api-host>/api
NEXT_PUBLIC_API_BASE_URL=/api/agent
```

`PIXEL_AGENT_API_BASE_URL` is server-only. Production builds fail when it is missing, preventing a
deployment that silently rewrites API requests to localhost.

The web app requests a private session for the tenant and product declared in its product
configuration. It does not send an employee identity. The API decides whether that product is
publicly available and returns a token scoped to the allocated visitor instance.

The web interface checks readiness before login. When the API is unavailable, it disables chat,
guided prompts and voice and shows one retry control; it does not run browser-local demo behavior.
A `429` is reported as "please wait a moment", never as a lost connection.

## Release smoke test

Run the automated smoke test against the deployed web origin:

```text
PIXEL_LIVE_URL=https://linear-simplified-web.vercel.app node scripts/smoke-live.mjs
```

It fails unless: the API reports ready; two visitors receive different private instances with the
same seed; a write by A is invisible to B; "Show sprint planning" completes with `OPEN_CYCLES`;
"Open Salesforce" is refused for the guardrail's own reason; global reset is denied; and A's
private reset restores its seed, rotates its generation and invalidates the old token.
CI runs the same script against the freshly built container (`PIXEL_SMOKE_API_URL`).
`PIXEL_SMOKE_SPEECH=true` adds one paid speech call and needs explicit spending approval.

Then check by hand:

1. Load the dashboard with no service warning.
2. Show sprint planning, open Maya's ticket, and ask to open Salesforce (refused).
3. In browser A, assign Maya's ticket to Noah and reload: A still sees Noah. Open browser B in an
   independent private context: B still sees Maya.
4. Create a ticket for an unknown teammate and verify the Teams handoff.
5. Press **Restart**: the conversation restarts and A still sees Noah. Then press **Reset**, accept
   the confirmation, and verify A returns to Maya while B remains unchanged.
6. Rate limits: from one network, the 31st new visitor session within a minute returns `429`.
   From a different network straight afterwards, a new session succeeds. If it does not, the proxy is not
   forwarding client addresses and every visitor shares one limit.
7. Enable voice and speak one turn (paid: needs approval), or confirm the browser-voice fallback.
8. Restart the API service and verify the saved data remains.

## Recovery: conversations cannot start

Symptoms: `/health` returns `503`, and the chat answers "This product is not available right now."

1. Run `check-readiness` in the API container and read the `reason`.
2. `definition_invalid` means the registered checksum does not match the file the image carries.
   Published definitions are immutable, so the registered row is never rewritten. For the
   synthetic demo, point `PIXEL_DB_PATH` at a new file (for example `/data/pixel-live-2.sqlite3`)
   and redeploy: the seed registers the image's own bytes. The old file stays on the volume as a
   rollback until it is deleted.
3. Anything else (`definition_revoked`, `definition_retired`, a missing binding) is a lifecycle
   decision; fix the binding or the definition's state rather than the database file.

Changing `PIXEL_DB_PATH` or `PIXEL_AUTH_SECRET` signs every visitor out; the web app signs them in
again automatically.
