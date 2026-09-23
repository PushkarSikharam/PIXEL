# Knowledge Ingestion Design (foundation draft)

Executable contracts: `saas/platform/pixel_saas/ingestion.py`, tested in `test_ingestion.py`.

## Accepted input

| Kind | Formats | Limit |
| --- | --- | --- |
| Documents | `.md`, `.txt`, `.html`, `.pdf`, `.docx` | 25 MB each, 200 per upload |
| API descriptions | `.json`, `.yaml`, `.yml` (OpenAPI) | 25 MB |
| Images | `.png`, `.jpg`, `.jpeg` (screenshots) | 25 MB |
| Archives | `.zip`, not nested | 100 MB packed, 500 MB unpacked, 1,000 entries, ratio 100:1, depth 8 |
| URLs | Public `https` only | Fetched by an isolated worker |

The declared media type must match the extension, and the scanner re-checks the bytes. Executable
and script formats are refused everywhere, including inside archives.

## Pipeline

```
queued -> scanning -> parsing -> chunking -> indexing -> ready
            |           |           |           |
            +-----------+-----------+-----------+--> queued (retry, at most 3 attempts)
            |                                   +--> failed
            +--> quarantined (malware or a secret found; never retried)
any working state --> cancelled
```

- **Scanning** is the malware and secret-scanning boundary. Nothing reaches the parser unscanned. A
  found secret quarantines the upload and notifies an administrator; the secret is never logged.
- **Workers** run without network access (except the URL fetcher, which is constrained as below),
  without customer credentials, with CPU, memory and time limits, and each job in a fresh sandbox.
- **Retries** use exponential backoff for transient failures only; three attempts, then `failed`.
- **Cancellation** stops the job at its next step boundary. Nothing from a cancelled, failed or
  quarantined job is ever retrievable (`visible_to_sessions`).

## URL ingestion (SSRF)

`check_url` refuses non-https URLs, embedded credentials, non-default ports, and any host that
resolves to a loopback, private, link-local, carrier-grade NAT, multicast or metadata address,
including IPv4-mapped IPv6. The fetcher connects only to the address it checked (no second DNS
lookup), re-runs the check on every redirect, and caps redirects, response size and time.

## Versioning and deletion

- Each ingestion builds a new knowledge version beside the active one:
  `building -> ready -> active -> superseded`. An active version is never edited.
- Sessions pin their knowledge version; switching to a new version affects new sessions only.
- Deleting a source removes its files, chunks, embeddings and caches from every active system
  within the deletion SLA. Backup copies are unreachable by the application and expire within 30
  days; deletion tombstones are replayed before any restore serves traffic.

## Queue and worker contract

| Message field | Notes |
| --- | --- |
| `job_id`, `organization_id`, `product_id`, `environment`, `knowledge_version` | Always present; a worker refuses a message without them |
| `source_ref` | Object-storage key under `org/product/environment/version/`; never a path supplied by a person |
| `attempt` | Incremented by the queue; the worker never trusts a client value |

Workers are idempotent per `(job_id, step)`; a redelivered message repeats no completed step.

## Synthetic fixtures

Tests use only synthetic entries (`ArchiveEntry`, `UploadItem`) and an injected resolver, so no real
file, archive or network request is needed.
