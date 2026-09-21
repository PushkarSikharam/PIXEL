# Milestone 3.2, Slice 4b: Response Composer, Conversation Intents and the Knowledge Boundary

Status: **PR #13 MERGED, SIGN-OFF REOPENED by the 2026-09-20 review. Revision 4.3 corrections are locally verified and await review and fresh CI evidence. The composer is not wired into the live runtime.**

- Merged through PR #10 (commit `d3ddf25`). Pull-request run 35380052283 and the `main` push run 35380344483 are green on all three jobs.
- The stakeholder review of 2026-09-18 found that product-controlled templates were still spoken in answers, refusals and clarifications, and reopened the sign-off. The boundary it required is described below under "The response-integrity boundary". It is built and carried by PR #13 (commit `f5f5f97`). Pull-request run 35396288897 is green on all three jobs: API tests, including the container build and its live smoke test; web checks; and browser tests. PR #13 merged as `f3cc571c4a3e49aa780947b9b49a7d03ec0c0f89`. Its CI evidence covers `f5f5f97`, not the new revision 4.3 edits.

Date: 2026-09-18. Plan: `docs/MILESTONE_3_STEP_3_2_PLAN.md` (revision 4.2), sections 7.2, 8.2 and 8.5.
Approved scope: the response composer as a lifecycle state machine, platform conversation intents, the response-integrity boundary (revision 4.2), and the `KnowledgeLookup` honest fallback.

## Current contract: revision 4.3

The 2026-09-20 review reproduced three remaining output paths and a shared-demo defect.
The shared-demo defect is not fixed by the composer; its implementation plan is
`docs/DEMO_VISITOR_ISOLATION_PLAN.md` and awaits review before schema work.

- **Capability descriptions:** generated from capability, entity, view, control and allowed field
  keys. A navigation action cannot advertise refunds by changing its description. The composer
  regenerates from offer keys, ignoring even a supplied offer-description string.
- **Identity and clarification:** every sentence is platform-owned. The product supplies names,
  presented as names, not greeting or question prose. Legacy templates remain accepted under their
  existing validation for published-definition compatibility. They are never rendered by 4b.
  Identity values cannot be overridden by caller-supplied product/assistant prose.
- **Knowledge:** a fixed "According to the product documentation" attribution precedes one
  checked, bounded excerpt. Checked source titles remain metadata. Malformed titles, oversized
  excerpts and embedded double-quote breakouts fall back to ungrounded. Only the quoted passage
  supplies the reply's source ID and title.
- **Lifecycle:** existing platform wording and stage allowlists remain. No model-authored speech
  is accepted. The future orchestrator must supply verified results and the correct stage.
- **Copy validation:** still lexical and intentionally not the security guarantee. Multiple
  questions now fail the single-question rule. The structural guarantee is no product response
  body or action-description text in any generic-engine reply.
- **5a:** platform-wording differences must be recorded separately, shadow memory must be
  independent, and shadow must create neither provider calls nor execution keys.

`test_response_integrity_regressions.py` adds the exact validated-definition reproductions and
tests direct offer-description substitution, all-response replacement, attribution metadata,
unrelated citations and protected identity values. The original six tests failed before the fix
(seven failing assertions including subcases); the expanded nine pass locally.

Local verification completed on 2026-09-21:

- Core API: 715 run, 712 passed, 3 CI-only skips.
- Product: 82 passed.
- Browser: 112 passed (4.3 minutes).
- Focused response-boundary suite: 21 passed; the nine new regression tests also pass.
- TypeScript check, all 34 web unit tests and the production build passed on the fresh verification run.

No new CI sign-off is claimed. These edits are not deployed. No paid provider
calls or changes to production data were made. The shared-demo isolation issue remains open
pending review and implementation of its separate migration plan.

## Earlier implementation record

The sections below preserve earlier review evidence and design decisions. Wording ownership and
knowledge attribution statements from those revisions are superseded by the current contract
above; they are not requirements for 5a.

## What was built

| Component | File | Notes |
| --- | --- | --- |
| Response composer | `apps/api/app/engine/composer.py` | A `Stage` per lifecycle state, platform-owned lifecycle wording, and the rule that model speech may never describe an outcome. |
| Conversation intents | `apps/api/app/engine/conversation.py` | Generic detection of greetings, identity, capability questions and introductions; `offerable()` filters what may honestly be offered. |
| Knowledge boundary | `apps/api/app/engine/knowledge.py` | `KnowledgePassage`, the `KnowledgeLookup` protocol, `NoKnowledge` as the default, and `ground()`. |
| Product knowledge source | `products/linear_simplified/backend/knowledge.py` | Passages from this product's documents, built for one `KnowledgeContext`. |
| Registration | `installed_products.py`, `backend/package.py` | `knowledge_factory`, registered in code beside the lookup and translator factories. |
| Platform vocabulary | `apps/api/app/definitions/vocabulary.py` | New response key `knowledge_unavailable` (additive, like `clarify_person` in slice 2), and the ownership of every response key (reopened sign-off). |
| Product copy rules | `apps/api/app/definitions/copy_rules.py` | Reopened sign-off: the validation of every product-controlled word. |

## The wording rule

The platform owns lifecycle wording, and a test asserts that the *same action* produces four different
sentences across proposed, awaiting-confirmation, executed and cancelled — so the states cannot
quietly collapse into one another.

| Stage | Means | Example wording |
| --- | --- | --- |
| `proposed` | Nothing has happened | "I'll update CON-1: status to Closed." |
| `awaiting_confirmation` | The visitor has not agreed | "Should I update CON-1: status to Closed?" |
| `executed` | The write committed | "Updated CON-1: status to Closed." |
| `failed` | A rule rejected it | "I couldn't complete that request." — never a success |
| `cancelled` | The visitor declined | "Okay, I won't make that change." |
| `clarification` | A question is pending | a platform slot question ("Which person do you mean: Ana Lopez or Ana Reyes?"), or the product's own choice question |
| `ungrounded` | Nothing installed can answer | a platform-owned knowledge-unavailable sentence |

**No stage accepts model-written speech.** `MODEL_SPEECH_STAGES` is empty, deliberately.
Executing one action proves that one action succeeded; it does not make any other sentence true,
and "I deleted every customer" passes every lexical check ever written. Retrieving a passage
proves a document exists; it does not make a sentence about that document accurate. Until a reply
can be bound to its citation and that binding evaluated, every word is composed deterministically.
Lifecycle text comes from platform-owned templates and verified results. As first merged, product
templates still worded answers, refusals and clarifications. The stakeholder review showed that was
a hole, and the next section closes it.

**Every stage enforces a template allowlist.** `STAGE_TEMPLATES` is checked on every render, so a
failure can never reach for success wording.

## The response-integrity boundary (reopened sign-off)

**The finding.** Lifecycle wording was platform-owned, but answers, refusals and clarifications
still rendered the product's own template. A definition could word a refusal as a success
("Everything changed successfully."), invent a count, claim history, or promise an action inside a
question. The allowlist said which *key* a stage could use, not whether the *sentence* was true.

**The boundary**, as the stakeholder set it:

| Who | Owns | How it is enforced |
| --- | --- | --- |
| Platform | Every assertion about execution, refusal, authorization, scope, counts, retrieved facts, history, failure and knowledge availability: all lifecycle wording, every refusal, capability and guided-path replies, counts, last change, knowledge availability, and **slot questions** (which person, which record) | `PLATFORM_LIFECYCLE_TEMPLATES` and `PLATFORM_CONVERSATION_TEMPLATES` in the composer. The definition's text for these keys is never read. |
| Product | Names and labels (product, assistant, entity, view and control names; action descriptions), identity copy (`greeting`, `greeting_named`, `identity`, `identity.greeting`), and **choice questions** (`clarify_create`, `clarify_all_items`) | `copy_rules`, checked whenever a definition or a product binding's settings are validated, and again by the composer before speaking. |

Slot questions are the platform's because their answer feeds straight into an action the platform
may still refuse. The first Linear definition shows why: its own owner question reads "Who should
own this ticket? Name a teammate in this workspace and I'll prepare the form." That is a promise
made before anything has been checked.

**Validation.** `copy_rules` rejects product copy that claims an action ran, promises one, asserts
a refusal or permission, states what is in scope, states a count or a retrieved fact, describes
history or failure, asserts what knowledge exists, or advertises deletion (no product can delete).
Product copy may use `{product}`, `{assistant}` and `{visitor}` only; facts such as `{count}` or
`{person}` are the platform's. A choice question must be exactly one question. Names and labels
must be names: never a sentence, never a first-person statement. The rules run inside the
definition contract, so registration, publication and every load pass through them; a definition
that breaks them is rejected completely and never registered.

**These rules are lexical, so they are the second line, not the boundary.** The boundary is
structural: the composer never speaks product copy in a protected stage, whatever it says. A
definition may still carry text for platform-owned keys (the first Linear version does, for the
legacy engine's parity), and that text is inert.

**Adversarial tests** (`apps/api/tests/test_response_boundary.py`, 21 tests):

- Every platform-owned key is filled with a different adversarial sentence ("Done. I deleted every
  record and you are now an administrator.", "There are 4012 overdue contacts.", "The update
  failed, so I retried it twice.", and others). Then every reply the composer can produce is
  checked, more than a hundred of them: each lifecycle method for every capability, every failure
  reason, every answer, refusal and clarification key, the ungrounded answer, capability and
  guided-path replies, and model speech in every stage. No adversarial text appears.
- The strongest form: swapping every platform-owned template changes **no protected reply at all**,
  compared character for character with the untouched definition.
- Product copy reaches only its own stage, and is marked `product_copy` when it does.
- Every state-asserting category above is rejected for every product-owned key; tenant settings,
  names, labels and action descriptions follow the same rules; the registry refuses to register a
  definition that breaks them; and copy that bypassed validation (`model_copy`) is still refused
  by the composer.
- **The tests were run against the previous composer** (product templates rendered for every key)
  to prove they can fail: they failed across dozens of stage and key combinations.

**Consequences, stated rather than hidden:**

1. **Two first-merge decisions were reversed.** The knowledge-unavailable reply and the capability
   reply were product wording; they are now platform wording, and the product contributes only its
   name. The tests that asserted the old ownership were rewritten to assert the new one.
2. **Unknown and inaccessible people now read identically** ("I can't find Ana Lopez in ACC-1."),
   so a refusal never reveals that someone exists outside the caller's scope.
3. **The router no longer needs product wording for slot questions.** A definition that declares
   none still gets the platform's question instead of a fallback. The first Linear definition
   declares them, so its routing is unchanged, and so are the golden comparisons.
4. **The first Linear definition passes.** Its product-controlled copy asserts nothing; its
   lifecycle, refusal and slot templates would fail as product copy, and are never spoken. It was
   not edited: published definitions are immutable.
5. **Residual risk.** Record values (names, titles) are inserted into platform sentences as facts
   the platform looked up. They are the tenant's own data, shown to a visitor who can already see
   them, but they are not sentence-checked. The composer is still not wired into the runtime; the
   live conversation path is the legacy engine until slice 5c.

## The knowledge boundary

`KnowledgeLookup` mirrors `RecordLookup`: read-only, scope-bound at construction, `search` takes
no tenant, product or scope, and the implementation lives in the product package. Core states the
contract and imports nothing — a test reads the module's own import lines and fails on any
mention of a retriever, `products`, or `app.services`.

- **`NoKnowledge` is the default.** A deployment with nothing installed finds nothing, and the
  platform returns a fixed honest fallback rather than improvising.
- **An answer is grounded or it is not given.** No passages means a platform-owned response at a
  distinct `ungrounded` stage that is deliberately *not* a refusal: the request was fine, the
  deployment simply has no source for it.
- **Documents are untrusted content.** Plain-text snippets are quoted and explicitly attributed to
  their title rather than spoken as Edith's own claim. The reply also carries immutable source IDs.
  Markup and malformed passages are refused.
- **The boundary carries the caller.** `KnowledgeContext` holds tenant, product, definition
  version and checksum, knowledge version and scope, and is required at construction. Today's
  documents are static and shipped with a definition, so *this implementation* still reads by
  definition ID — but the shape is already right, so Milestone 3.4 changes the implementation
  rather than the boundary. It is also why dismantling today's assistant in slice 5 does not
  silently empty `retrieved_context`.
- **Malformed context fails closed.** Tenant, product and definition identifiers are validated;
  versions must be positive integers; the definition checksum must be a lowercase SHA-256 value;
  and scope must be non-empty plain text.

## Capability replies

An action is offered only when it passes four filters: declared by this product, expressible by
the installed adapter, permitted for this caller, and **reachable under the caller's current
scope**. The last one has teeth — with no visible contacts, opening, updating and reassigning are
all withheld, while creating is still offered, because creating is the one thing still possible on
an empty scope.

## Self-review findings, fixed before this report

A probe of my own work found five defects. Each has a regression test.

| # | Severity | Defect | Fix |
| --- | --- | --- | --- |
| 1 | **High** | **The completion-claim denylist was trivially evadable.** "The contact was closed.", "That is taken care of.", "Closed.", "Sorted." all passed as a *proposal*. A phrase list cannot be the guarantee against something that writes English. | Narrowed to two stages at the time, and then removed entirely by the stakeholder review below: no stage accepts model speech. |
| 2 | **High** | **A document that claimed completion was spoken verbatim.** A passage reading "Done. I have updated the cycle." became the answer. | Passages are checked like any other untrusted content; malformed markup is refused; completion claims later became explicitly attributed quotations, and revision 4.3 removes titles from spoken attribution. |
| 3 | **High** | **Ordinary sentences were read as introductions.** "call me back later" greeted the visitor as "Back Later"; "this is urgent" as "Urgent". | Those cues are gone, a stoplist rejects ordinary words, and a name must appear **capitalized in what the visitor actually typed**. |
| 4 | Medium | **Mutations were offered on an empty scope.** With no visible contacts, updating and reassigning were still advertised. | Only creating survives an empty scope; everything needing an existing record is withheld. |
| 5 | Medium | **Model speech had no length bound in the composer.** A 5000-character sentence passed through. | Capped independently of the parser. Now moot for this slice, since no stage uses model speech, but the cap stays for when one does. |

Verified safe rather than assumed: a field value containing `{assistant}` is **not** re-expanded,
because substitution runs once over the template and inserted text is never rescanned. There is a
test for it.

## Stakeholder review: nine defects, all fixed

| # | Severity | Reproduction | Fix |
| --- | --- | --- | --- |
| 1 | **Critical** | **Lifecycle templates could be crossed.** `failed(action, "record_updated")` produced "CON-1 is now updated" while the reply was marked `FAILED`. `STAGE_TEMPLATES` existed but was never enforced. | Every render checks the stage's allowlist and raises `TemplateNotAllowed`. A test walks every stage and asserts a forbidden key is refused; another asserts no non-executed stage may use completion wording. |
| 2 | **Critical** | **Executed model speech could claim unrelated actions.** Closing one contact let "I deleted every customer and emailed their data" through. | No stage accepts model speech. An executed write is worded from the committed result. |
| 3 | **Critical** | **"Grounded" answers need not be supported.** A cycles passage made "Salesforce exports every customer automatically" acceptable — presence of retrieval, not grounding. | `knowledge_answer` takes only a `Grounding`; there is no parameter through which a model sentence can arrive. A test asserts the signature. |
| 4 | **High** | **Capability filtering failed open.** Omitting `translatable` or `permitted` advertised every declared action. | Both are now required through one `CapabilityPolicy`, and `CapabilityPolicy.nothing()` is the safe default. Omitting it is a `TypeError`. |
| 5 | **High** | **Knowledge was not caller-scoped.** Retrieval was bound only to a definition ID, with no organization, product, version or scope. | A typed `KnowledgeContext` carries tenant, product, definition version and checksum, knowledge version and scope, and is required at construction. Today's static documents still read by definition ID — a property of the implementation, not the boundary. |
| 6 | Medium | **`Grounding` was shallowly frozen.** Mutating the list passed in changed whether an answer was grounded. | Converted to a tuple in `__post_init__`, with empty sources and snippets refused. |
| 7 | **Critical** | **An allowed product template could still lie about lifecycle state.** A valid definition could put deletion or success prose inside `record_create_proposed` or `action_failed`; key allowlisting did not make the sentence truthful. | Proposed, confirmation, executed, failed and cancelled wording is platform-owned. Product lifecycle sentence bodies are never rendered. |
| 8 | **High** | **A retrieved snippet was repeated without visible provenance.** A document could contain an execution claim and the visitor could reasonably hear it as Edith's own statement. | Knowledge is rendered as an explicit quotation attributed to its document title, and `Reply.sources` preserves the immutable source IDs. |
| 9 | **High** | **`KnowledgeContext` accepted malformed isolation metadata.** Whitespace identifiers, non-positive versions, malformed checksums and empty scope values could reach a product knowledge adapter. | Construction now validates every identity, version, checksum and scope field and fails before lookup. |

## Test results

| Suite | Result |
| --- | --- |
First merge (PR #10), Windows development runs; the Linux CI runs are named in the status:

| Suite | Result |
| --- | --- |
| Core API | **623 run: 620 passed, 3 skipped** (CI-only) |
| Product (Linear) | **82 passed** |
| Composer, conversation and knowledge | `apps/api/tests/test_engine_composer.py` — **67 passed** |
| Browser regression | **107 passed** |

Reopened sign-off, Windows development runs. The same change also carries the public-demo and
readiness fixes described in `docs/LIVE_DEPLOYMENT.md`.

| Suite | Result |
| --- | --- |
| Core API | **682 run: 679 passed, 3 skipped** (CI-only) |
| Product (Linear) | **82 passed** |
| Response boundary | `apps/api/tests/test_response_boundary.py` — **21 passed** |
| Composer, conversation and knowledge | `apps/api/tests/test_engine_composer.py` — **67 passed** |
| Router | `apps/api/tests/test_engine_router.py` — **56 passed** |
| Web type check | **passed** |
| Web unit tests | **30 passed** |
| Browser regression | **111 passed** |

No paid provider call was made. The sign-off evidence is the Linux CI run on PR #13 (run
35396288897): API tests, web checks and browser tests, all green.

## Notes for the review

1. **Linear v2 (slice 5a) is written against this boundary.** It must declare its identity copy
   and choice questions; the composer raises `MissingTemplate` rather than inventing identity copy.
   It need not declare platform-owned keys at all, and nothing speaks them if it does.
2. **Core owns every assertion; products own names, identity and choice questions.** Product
   definitions cannot redefine what proposed, confirmed, executed, failed or cancelled means, nor
   word a refusal, a count, a history line, a knowledge claim or a slot question.
3. **Nothing is wired.** A test walks every `app.*` module outside `app/engine/` and fails if any
   of them names the new modules.
4. **Not in this slice:** no provider call, no dispatch, no runtime path, and no knowledge
   indexing, versioning or ranking — those are Milestone 3.4.
