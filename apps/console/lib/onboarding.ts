/**
 * Product onboarding as a state machine (prototype; analysis is mocked).
 *
 *   details -> sources -> analyzing -> review -> actions -> validation -> ready -> published
 *
 * Rules the prototype enforces, so the real flow inherits them:
 * - Each step has entry requirements; a step cannot be reached before its prerequisites hold.
 * - Changing sources after analysis invalidates the analysis and everything after it.
 * - Analysis is a proposal: nothing it produced is used until a person reviews and accepts it.
 * - Every generated action starts disabled, and mutating actions always require confirmation.
 * - Publishing requires a clean validation of exactly the reviewed configuration.
 */

export type Step = "details" | "sources" | "analyzing" | "review" | "actions" | "validation" | "ready" | "published";
export const STEPS: Step[] = ["details", "sources", "analyzing", "review", "actions", "validation", "ready", "published"];

/**
 * What each step is called, in the words of the person doing it.
 *
 * A step's name has to say what happens on it. "Approved sources" described an earlier design
 * where somebody uploaded documents; the step now asks what the product keeps, and a rail that
 * disagrees with the panel beside it reads as a bug even when nothing is broken.
 */
export const STEP_LABELS: Record<Step, string> = {
  details: "Start", sources: "Describe it", analyzing: "Pixel is writing it", review: "Review",
  actions: "Choose what it can do", validation: "Check it", ready: "Launch", published: "Live",
};

export interface Source { id: string; name: string; kind: "document" | "openapi" | "url"; status: "accepted" | "refused"; reason?: string }
export interface ProposedAction { key: string; description: string; mutating: boolean; enabled: boolean; requiresConfirmation: boolean }
export interface Finding { id: string; severity: "error" | "warning"; message: string }

export interface DescribedField {
  name: string;
  type: "text" | "integer" | "enum" | "date" | "boolean";
  required: boolean;
  values: string;
}

export interface DescribedThing {
  id: string;
  label: string;
  plural: string;
  people: boolean;
  fields: DescribedField[];
}

/** What Pixel wrote from the description, and has not yet been published. */
export interface Understanding {
  definition: string;
  things: string[];
  screens: string[];
  canDo: string[];
}

export interface OnboardingState {
  /** Whether somebody edited the address themselves, so their name no longer rewrites it. */
  slugChosen?: boolean;
  step: Step;
  name: string;
  slug: string;
  teamId: string;
  sources: Source[];
  things: DescribedThing[];
  understanding: Understanding | null;
  analysisRevision: number | null; // the source revision the analysis was built from
  sourceRevision: number;
  understandingAccepted: boolean;
  actions: ProposedAction[];
  findings: Finding[];
  validatedRevision: number | null; // the configuration revision validation passed on
  configRevision: number;
}

export const initialOnboarding = (teamId: string): OnboardingState => ({
  step: "details", name: "", slug: "", teamId, sources: [], things: [], understanding: null,
  analysisRevision: null, sourceRevision: 0,
  understandingAccepted: false, actions: [], findings: [], validatedRevision: null, configRevision: 0,
});

const SLUG = /^[a-z0-9][a-z0-9-]{1,62}$/;
const THING_NAME = /^[a-z][a-z0-9_]{0,47}$/;

export function keyForName(name: string): string {
  const key = name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return (/^[a-z]/.test(key) ? key : `item_${key || "record"}`).slice(0, 48);
}

export function canEnter(state: OnboardingState, step: Step): boolean {
  const accepted = state.sources.filter((s) => s.status === "accepted");
  switch (step) {
    case "details": return true;
    case "sources": return state.name.trim().length > 0 && SLUG.test(state.slug);
    // Something to keep, and something to call each one: a product nobody can describe is not a
    // product Pixel can write.
    case "analyzing": return canEnter(state, "sources") && state.things.some((thing) => !thing.people)
      && state.things.every((thing) => THING_NAME.test(keyForName(thing.label || thing.id))
        && thing.label.trim() && thing.plural.trim());
    case "review": return state.analysisRevision === state.sourceRevision && state.analysisRevision !== null;
    case "actions": return canEnter(state, "review") && state.understandingAccepted;
    case "validation": return canEnter(state, "actions") && state.actions.length > 0;
    case "ready": return canEnter(state, "validation") && state.validatedRevision === state.configRevision
      && !state.findings.some((f) => f.severity === "error");
    case "published": return canEnter(state, "ready");
  }
}

export function goTo(state: OnboardingState, step: Step): OnboardingState {
  if (!canEnter(state, step)) throw new Error(`cannot_enter:${step}`);
  return { ...state, step };
}

/**
 * A product's address, from its name.
 *
 * Asking somebody to invent one is asking them to do a job the computer can do: they came here to
 * add their product, not to choose an identifier. It stays editable, and once they have edited it
 * their choice is theirs to keep - typing the name again does not overwrite it.
 */
export function addressFor(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
}

export function setDetails(state: OnboardingState, name: string, slug: string,
                           options: { derived?: boolean } = {}): OnboardingState {
  return { ...state, name, slug, slugChosen: options.derived ? false : state.slugChosen || slug !== "" };
}

const REFUSED_EXTENSIONS = [".exe", ".sh", ".js", ".py", ".bat", ".ps1", ".jar", ".zip"];

export function addSource(state: OnboardingState, name: string, kind: Source["kind"]): OnboardingState {
  const lower = name.toLowerCase();
  let status: Source["status"] = "accepted";
  let reason: string | undefined;
  if (kind === "url" && !lower.startsWith("https://")) { status = "refused"; reason = "Only https:// links to public pages are accepted."; }
  else if (REFUSED_EXTENSIONS.some((ext) => lower.endsWith(ext))) { status = "refused"; reason = "Executable, script and archive files are not accepted in this prototype."; }
  const source: Source = { id: `src-${state.sources.length + 1}-${lower.replace(/[^a-z0-9]+/g, "-")}`, name, kind, status, reason };
  return invalidateFromSources({ ...state, sources: [...state.sources, source] });
}

export function removeSource(state: OnboardingState, id: string): OnboardingState {
  return invalidateFromSources({ ...state, sources: state.sources.filter((s) => s.id !== id) });
}

function invalidateFromSources(state: OnboardingState): OnboardingState {
  // New sources mean the earlier analysis no longer describes the product.
  return { ...state, sourceRevision: state.sourceRevision + 1, understandingAccepted: false, actions: [],
    findings: [], validatedRevision: null, configRevision: state.configRevision + 1,
    step: STEPS.indexOf(state.step) > STEPS.indexOf("sources") ? "sources" : state.step };
}

/** Mocked analysis: a fixed, synthetic proposal. Nothing is sent anywhere. */
export function completeAnalysis(state: OnboardingState): OnboardingState {
  if (!canEnter(state, "analyzing")) throw new Error("cannot_analyze");
  const actions: ProposedAction[] = [
    { key: "open_invoices", description: "Open the invoice list", mutating: false, enabled: false, requiresConfirmation: false },
    { key: "filter_invoices_by_customer", description: "Filter invoices by customer", mutating: false, enabled: false, requiresConfirmation: false },
    { key: "issue_refund", description: "Issue a refund on an invoice", mutating: true, enabled: false, requiresConfirmation: true },
    { key: "update_subscription_plan", description: "Change a customer's subscription plan", mutating: true, enabled: false, requiresConfirmation: true },
  ];
  return { ...state, analysisRevision: state.sourceRevision, actions, understandingAccepted: false, step: "review" };
}

export function acceptUnderstanding(state: OnboardingState): OnboardingState {
  if (!canEnter(state, "review")) throw new Error("no_analysis");
  return { ...state, understandingAccepted: true, step: "actions" };
}

/**
 * A server-generated definition has already passed the product contract. The guided live flow
 * publishes that exact reviewed definition, so it does not show switches that would falsely
 * imply the browser can rewrite individual actions inside the signed definition.
 */
export function approveGeneratedUnderstanding(state: OnboardingState): OnboardingState {
  if (!canEnter(state, "review") || !state.understanding) throw new Error("no_analysis");
  const actions = state.understanding.canDo.map((description, index) => ({
    key: `generated-${index + 1}`,
    description,
    mutating: /^(add|change)\b/i.test(description),
    enabled: true,
    requiresConfirmation: /^(add|change)\b/i.test(description),
  }));
  return {
    ...state,
    understandingAccepted: true,
    actions,
    findings: [],
    validatedRevision: state.configRevision,
    step: "ready",
  };
}

export function toggleAction(state: OnboardingState, key: string, enabled: boolean): OnboardingState {
  const actions = state.actions.map((a) => (a.key === key ? { ...a, enabled } : a));
  return { ...state, actions, validatedRevision: null, configRevision: state.configRevision + 1 };
}

export function setConfirmation(state: OnboardingState, key: string, required: boolean): OnboardingState {
  const actions = state.actions.map((a) => {
    if (a.key !== key) return a;
    // A mutating action can never be configured to run without confirmation.
    return { ...a, requiresConfirmation: a.mutating ? true : required };
  });
  return { ...state, actions, validatedRevision: null, configRevision: state.configRevision + 1 };
}

/** Mocked validation with the real rules' shape: errors block publishing, warnings do not. */
export function validate(state: OnboardingState): OnboardingState {
  if (!canEnter(state, "validation")) throw new Error("cannot_validate");
  const findings: Finding[] = [];
  const enabled = state.actions.filter((a) => a.enabled);
  if (enabled.length === 0) findings.push({ id: "no-actions", severity: "error", message: "Enable at least one action for Edith to offer." });
  for (const a of enabled) {
    if (a.mutating && !a.requiresConfirmation) findings.push({ id: `confirm-${a.key}`, severity: "error", message: `${a.description} changes data and must ask for confirmation.` });
  }
  if (state.sources.some((s) => s.status === "refused")) findings.push({ id: "refused-sources", severity: "warning", message: "Some sources were refused and are not part of this product's knowledge." });
  if (!state.actions.some((a) => a.enabled && !a.mutating)) findings.push({ id: "no-read", severity: "warning", message: "No read-only action is enabled; visitors can only be guided to changes." });
  return { ...state, findings, validatedRevision: state.configRevision, step: "validation" };
}

export function publish(state: OnboardingState): OnboardingState {
  if (!canEnter(state, "published")) throw new Error("cannot_publish");
  return { ...state, step: "published" };
}

export function definitionIdFor(state: Pick<OnboardingState, "slug">): string {
  return `${state.slug.replace(/-/g, "_")}_product`;
}

export function starterDefinitionText(state: Pick<OnboardingState, "name" | "slug">): string {
  const definitionId = definitionIdFor(state);
  const productName = state.name.trim() || "Added Product";
  return JSON.stringify({
    definition: { definition_id: definitionId, version: 1, ownership: "platform_shared" },
    identity: {
      product_name: productName,
      assistant_name: "Edith",
      persona: `A concise guide for ${productName}.`,
      voice_style: "Calm, clear and professional.",
      greeting: "Welcome to {product}. I'm {assistant}.",
    },
    vocabulary: {
      terms: ["book", "books", "catalogue", "loan", "loans", "librarian", "librarians", "shelf", "borrowed"],
      corrections: { boook: "book", libarian: "librarian" },
      correction_markers: ["actually", "instead"],
      negatable_terms: ["book", "books", "librarian", "librarians"],
    },
    entities: {
      book: {
        label: "Book", plural: "Books",
        id: { strategy: "prefix", prefix: "BK" },
        title_field: "title",
        summary_fields: ["status"],
        fields: {
          title: { type: "text", required: true, max: 200 },
          status: { type: "enum", required: true, values: ["On shelf", "On loan"], default: "On shelf" },
          keeper: { type: "ref", target: "librarian", required: true },
        },
      },
      librarian: {
        label: "Librarian", plural: "Librarians",
        id: { strategy: "slug", from_field: "name" },
        title_field: "name",
        fields: {
          name: { type: "text", required: true, editable: false },
          books: { type: "refs", target: "book" },
        },
      },
    },
    people: { entity: "librarian", assigned_by: ["book.keeper"], match_on: ["name"] },
    scope: { anchor: "book", paths: { book: [], librarian: ["books"] } },
    views: {
      catalogue: { label: "Catalogue", kind: "list", entity: "book", columns: ["title", "status"] },
      librarians: { label: "Librarians", kind: "list", entity: "librarian", columns: ["name"] },
    },
    actions: {
      open_catalogue: { capability: "NAVIGATE_VIEW", view: "catalogue", description: "Open the catalogue." },
      open_librarians: { capability: "NAVIGATE_VIEW", view: "librarians", description: "Open the librarian directory." },
      open_book: { capability: "OPEN_RECORD", entity: "book", description: "Open one book." },
      books_by_keeper: { capability: "FILTER_RECORDS", entity: "book", by: "keeper", description: "Show every book one librarian keeps." },
      add_book: { capability: "CREATE_RECORD", entity: "book", fields: ["title", "status", "keeper"], description: "Add a book." },
      reassign_book: { capability: "UPDATE_RECORD", entity: "book", fields: ["keeper", "status"], description: "Change who keeps a book, or whether it is out." },
      add_librarian: { capability: "CREATE_RECORD", entity: "librarian", fields: ["name"], description: "Add a librarian." },
    },
    intents: [
      { action: "open_catalogue", response: "anchor_count", match: [["how many", "count", "number of"], ["book", "books"]] },
      { action: "open_catalogue", response: "view_opened", match: [["book", "books", "catalogue", "shelf"]] },
      { action: "open_librarians", response: "view_opened", match: [["librarian", "librarians"]] },
      { action: "books_by_keeper", requires: ["person"], response: "records_filtered", match: [["kept by", "for"], ["books"]] },
      { action: "open_book", requires: ["record"], response: "record_opened", match: [["open", "show", "pull up"]] },
      { action: "add_book", requires: ["person"], response: "record_created", match: [["add", "create", "new"], ["book"]] },
      { action: "add_librarian", response: "record_created", match: [["add", "create", "new"], ["librarian"]] },
      { action: "reassign_book", requires: ["record"], response: "record_updated", match: [["give", "hand", "keeper", "assign", "on loan", "on shelf"]] },
    ],
    guardrails: [
      { topic: "destructive_change", response: "destructive_refused", match: [["delete", "erase", "wipe", "remove all"]] },
    ],
    responses: {
      view_opened: "I'll open {view}.",
      anchor_count: "{scope} has {count} visible books: {records}.",
      record_opened: "I'll open {record_id}.",
      records_filtered: "I found {count} books kept by {person}.",
      destructive_refused: "I can't delete or erase anything here.",
      record_created: "I'll add the book.",
      record_updated: "I'll update {record_id}: {changes}.",
      clarify_create: "What would you like to add?",
    },
  }, null, 2);
}


export function addThing(state: OnboardingState): OnboardingState {
  const thing: DescribedThing = {
    id: "", label: "", plural: "", people: false,
    fields: [{ name: "name", type: "text", required: true, values: "" }],
  };
  return invalidateAnalysis({ ...state, things: [...state.things, thing] });
}

export function updateThing(state: OnboardingState, index: number,
                            changes: Partial<DescribedThing>): OnboardingState {
  const things = state.things.map((thing, at) => (at === index ? { ...thing, ...changes } : thing));
  // Only one kind of record can be the people; marking another unmarks the first.
  const people = changes.people ? things.map((thing, at) => (at === index ? thing : { ...thing, people: false })) : things;
  return invalidateAnalysis({ ...state, things: people });
}

export function removeThing(state: OnboardingState, index: number): OnboardingState {
  return invalidateAnalysis({ ...state, things: state.things.filter((_, at) => at !== index) });
}

export function updateField(state: OnboardingState, thingIndex: number, fieldIndex: number,
                            changes: Partial<DescribedField>): OnboardingState {
  return updateThing(state, thingIndex, {
    fields: state.things[thingIndex].fields.map((field, at) => (at === fieldIndex ? { ...field, ...changes } : field)),
  });
}

export function addField(state: OnboardingState, thingIndex: number): OnboardingState {
  return updateThing(state, thingIndex, {
    fields: [...state.things[thingIndex].fields, { name: "", type: "text", required: false, values: "" }],
  });
}

export function removeField(state: OnboardingState, thingIndex: number, fieldIndex: number): OnboardingState {
  return updateThing(state, thingIndex, {
    fields: state.things[thingIndex].fields.filter((_, at) => at !== fieldIndex),
  });
}

/** Changing the description throws away what Pixel made of the old one, and everything after. */
function invalidateAnalysis(state: OnboardingState): OnboardingState {
  return invalidateFromSources({ ...state, understanding: null });
}

/** What Pixel understood, recorded against the description it was written from. */
export function understood(state: OnboardingState, understanding: Understanding): OnboardingState {
  return { ...state, understanding, analysisRevision: state.sourceRevision, step: "review" };
}
