# Product Onboarding Lifecycle (prototype)

Prototype: `saas/console/app/console/products/new`, state machine in
`saas/console/lib/onboarding.ts`, tested in `onboarding.test.ts`. Analysis is mocked.

```
Product details -> Approved sources -> Analyze -> Review understanding
  -> Configure actions -> Validation -> Ready to publish -> Published (release v1)
```

## Rules the prototype enforces

1. **No skipping.** Each step has entry requirements (`canEnter`); a step is unreachable until its
   prerequisites hold.
2. **Only approved sources.** Executable, script and archive uploads and non-https links are
   refused with a reason. Analysis needs at least one accepted source.
3. **Analysis is a proposal.** Nothing it produces is used until a person accepts it.
4. **Actions start off.** Every generated action is disabled until someone enables it.
5. **Mutations always ask.** An action that changes data cannot be configured to run without
   confirmation.
6. **Changes invalidate downstream work.** Editing sources discards the analysis and everything
   after it; changing actions invalidates the last validation.
7. **Publishing needs a clean validation of exactly the current configuration.** Errors block,
   warnings do not.
8. **Publishing is not deploying.** It creates immutable release v1; deployment is a separate,
   explicit step per environment.
9. **Unsaved drafts are protected.** Switching product or leaving the page asks first, and
   "discard" really clears the draft.

## What the real flow adds (Phases 3, 4 and 7)

Persistent drafts with optimistic revisions, real ingestion (`INGESTION.md`), model-assisted
analysis with citations back to the sources, the full publication gate (plan section 6.3), release
signing, and audit events for every step.
