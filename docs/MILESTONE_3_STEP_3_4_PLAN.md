# Milestone 3.4, Knowledge Store - Implementation Plan

Status: **draft, awaiting owner approval. No 3.4 code has changed.**
Date: 2026-09-22. Parent plan: `docs/MILESTONE_3_STEP_3_2_PLAN.md` revision 4.3, which assigns
"generic knowledge work - indexing, versioning, ranking" to this milestone. Platform rules:
`docs/PIXEL_SAAS_PLATFORM_PLAN.md`, section 7.

Prerequisite: slice 5c is accepted in production. 5d may run before or after this work; the only
overlap is that 5d deletes the last legacy caller of today's retriever, so the file itself can only
be removed here once 5d has landed.

## 1. Outcome

Replace the shipped-file keyword retriever with a knowledge store the platform owns: versioned,
scoped to one organization and one product, ranked, attributed, and deletable. After 3.4 a reply
that quotes a document quotes a passage from a **published knowledge version the session is pinned
to**, and a question with no such passage is answered honestly instead of with the nearest file.

Done means:

1. `apps/api/app/services/retriever.py` and `products/linear_simplified/backend/knowledge.py` are
   deleted, and the 3.4 entry leaves the core-purity allowlist.
2. A session's `knowledge_version` is verified the way its definition version already is: it must
   resolve to a published version whose checksum matches the binding, or the session does not start.
3. Retrieval cannot be widened by anything a visitor says, and cannot reach another organization's
   knowledge under any input.
4. Re-ingestion creates a new version; sessions already pinned keep reading the old one.
5. Deleting a source removes its rows and its derived rows everywhere the application can read.

## 2. What exists today, honestly

- **Documents:** five Markdown files in `docs/product/`, shipped in the repository and shared by
  every organization. There is no per-organization knowledge at all.
- **Retrieval:** `ProductRetriever` scores a document by counting overlapping words, including
  "who", "is" and "the", and returns anything scoring above zero. The 5c production run found this:
  "who build pixel?" was answered from the integrations passage.
- **The seam:** `app/engine/knowledge.py` already defines `KnowledgeContext` (tenant, product,
  definition id, definition version and checksum, knowledge version, scope label),
  `KnowledgeLookup`, `KnowledgePassage`, `Grounding`, `ground()` and `answerable()`. The engine is
  built against this seam and imports no product code, so 3.4 changes an implementation, not a shape.
- **Versioning:** `knowledge_version` is stored on the product binding and copied into the session
  pin. Nothing verifies it: there is no knowledge registry, and `knowledge_checksum` is written but
  never read. A session can be pinned to a knowledge version that does not exist.
- **The stop-gap from 5c:** the product adapter marks a passage `grounds_answer` when a second
  retrieval pass scores it at or above 3. It is a guard over a weak scorer, and it is deleted here.

## 3. Invariants (normative)

Every one of these is a test, not a comment.

1. **Constructed scope.** A retrieval API is built from a `KnowledgeContext` and takes only a
   question. No caller may pass an organization, product or version into a query.
2. **Pinned version.** A session reads exactly the knowledge version its pin names. A newer
   published version never reaches an open session.
3. **Verified version.** A pin is refused when its knowledge version is missing, unpublished, or
   its checksum does not match the binding, exactly as a definition version is refused today.
4. **Nothing by default.** A product with no published knowledge version retrieves nothing, and the
   platform says so in its own words.
5. **Grounded or not answered.** A passage may be quoted only when it passes the store's relevance
   rule. "No passage" is an answer the platform gives honestly.
6. **Attribution.** Every quoted passage carries its source title, source identifier and knowledge
   version, and the reply reports them as evidence.
7. **Untrusted text.** Retrieved text is data. It can never authorize an action, change platform
   wording, or alter instructions, and it passes the same safety checks as any other text the
   platform speaks.
8. **Isolation.** One organization's private knowledge is unreachable from another, under every
   input, including a question that quotes another organization's name or file path.
9. **Deletion.** Deleting a source deletes its chunks and index rows in the same transaction.
   Backup copies expire with the backup, within 30 days.
10. **No new paid dependency.** Ranking is local. Embeddings stay off and unbuilt in 3.4.

## 4. Design

### 4.1 Ownership, mirroring definitions

Knowledge is owned the way a definition is: **platform-shared** (shipped with a product, readable
by every organization bound to it) or **organization-private** (ingested for one organization).
Retrieval for a session reads the union of the shared set for its pinned knowledge version and that
organization's private set, and nothing else.

### 4.2 Tables

Additive; no existing table changes.

- `knowledge_versions`: `(definition_id, version)` primary key, `ownership`, `owner_tenant_id`,
  `state` (draft, published, retired, revoked), `checksum` over the ingested source set,
  `source_count`, `chunk_count`, `published_at`. The lifecycle and the transition rules are the
  ones `DefinitionRegistry` already enforces, reused rather than re-invented.
- `knowledge_sources`: `id`, `definition_id`, `version`, `ownership`, `owner_tenant_id`, `title`,
  `origin` (the file path or upload reference), `media_type`, `content_hash`, `bytes`, `created_at`.
- `knowledge_chunks`: `id`, `source_id`, `ordinal`, `text`, `term_count`, `heading_path`.
- `knowledge_terms`: `(chunk_id, term)` with `frequency`, plus a per-version document-frequency
  row set. This is the whole index: a local inverted index, no external service.

Every table carries `definition_id` and `version`; every query filters on both, plus ownership.

### 4.3 Ingestion

One operator command, `ops knowledge publish --product <id> --version <n>`, which:

1. reads the product package's declared knowledge directory (today `docs/product/`);
2. splits each file into chunks on heading boundaries, with a size cap and overlap;
3. validates every chunk with the same safety checks the composer applies to anything spoken;
4. writes sources, chunks and index rows inside one transaction;
5. computes the version checksum and publishes the version;
6. prints the counts and the checksum, and exits non-zero on any validation failure.

Re-running for an existing published version is refused. A new version is a new number, which is
what keeps open sessions stable. Binding a product to it stays the existing
`ops move-product-version` path, extended with `--knowledge-version`.

### 4.4 Ranking and the relevance rule

Local BM25 over the inverted index, with:

- stop words removed at ingestion and at query time;
- a term that appears in most chunks of a version contributing almost nothing, which is what makes
  the product's own name stop dominating;
- a **relevance floor**: a chunk may be quoted only when its score clears the floor *and* it shares
  at least one non-stop term with the question. Below the floor a passage may still be attached as
  supporting evidence, never quoted.

That replaces `grounds_answer` as a product-side flag with a store-side decision the platform can
test. The production questions from 5c become the first test cases: "who build pixel?" and "what
does pixel cost?" must find nothing quotable, while "how does github work with tickets?" and the
five product areas must.

### 4.5 What the engine sees

`KnowledgeLookup` keeps its shape. `KnowledgePassage` gains `version` and keeps `grounds_answer`,
now set by the store. `ground()` and `answerable()` are unchanged, so the engine and its tests do
not move. The product package keeps one line: which directory its shipped knowledge lives in.

## 5. Slices

Each slice is separately mergeable, and leaves the product working.

- **3.4a - registry and pinning.** `knowledge_versions`, the lifecycle, checksum verification at
  session pin, and readiness reporting a product whose knowledge version is missing. Ingestion still
  writes nothing; retrieval still uses today's path. Closes the unverified-pin gap first, because it
  is the only correctness hole in what is already deployed.
- **3.4b - store and ingestion.** Sources, chunks, the index, the `ops knowledge publish` command,
  and the golden ingestion output for the Linear documents.
- **3.4c - retrieval and cutover.** The BM25 lookup behind `KnowledgeLookup`, the relevance floor,
  attribution through to the response, and a switch (`PIXEL_KNOWLEDGE_SOURCE`, defaulting to the
  old path) so the new store can be proven in shadow before it answers. Same pattern as 5a-5c:
  compare, review every difference, then cut over.
- **3.4d - removal.** Delete `retriever.py`, the product adapter and the switch; remove the 3.4
  entry from the purity allowlist; update the architecture page.

## 6. Test plan

- **Isolation:** two organizations, private sources in each, every question from one; no row, title
  or snippet from the other, including questions naming the other organization's file.
- **Pinning:** a session pinned to version 1 keeps reading it after version 2 is published; a
  binding pointing at a missing or unpublished version refuses to start a session.
- **Relevance:** a recorded question set with the expected quotable and non-quotable outcome for
  each, including the 5c production questions. Regenerating it to make a test pass is not allowed.
- **Attribution:** every quoted answer carries source title, identifier and version.
- **Untrusted text:** a source containing instructions ("ignore previous instructions", a fake
  action, a fake refusal) changes no behaviour and no wording.
- **Deletion:** deleting a source removes its chunks and index rows; a question that previously
  quoted it no longer finds it.
- **Cost:** with paid providers disabled, every knowledge path works; no test needs a provider.
- **Purity:** no product term in core knowledge code.

## 7. Deliberate limits

- No embeddings, no external index, no re-ranking model. The seam allows them later; 3.4 ships
  local lexical ranking only, so knowledge costs nothing per query.
- No upload interface. Sources come from the product package directory; operator uploads are a
  separate change once the control plane exists.
- No answer synthesis. The platform still quotes a passage rather than writing prose about it, as
  4b decided. Synthesis needs citation binding and a grounding evaluation.

## 8. Open questions for the owner

1. **Private knowledge now or later?** The tables carry ownership from the start, but ingesting
   organization-private sources needs the upload path. Ship 3.4 shared-only, with private sources
   arriving with the control plane?
2. **Relevance floor tuning.** A stricter floor means more honest "I don't have that" answers and
   fewer weak quotes. Where should it sit for the demo: strict, or lenient with attribution?
3. **Should 3.4 precede 3.5?** This plan assumes yes, because the knowledge gap is visible to
   visitors today while the record store is not.
