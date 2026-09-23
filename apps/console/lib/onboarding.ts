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

export const STEP_LABELS: Record<Step, string> = {
  details: "Product details", sources: "Approved sources", analyzing: "Analyze", review: "Review understanding",
  actions: "Configure actions", validation: "Validation", ready: "Ready to publish", published: "Published",
};

export interface Source { id: string; name: string; kind: "document" | "openapi" | "url"; status: "accepted" | "refused"; reason?: string }
export interface ProposedAction { key: string; description: string; mutating: boolean; enabled: boolean; requiresConfirmation: boolean }
export interface Finding { id: string; severity: "error" | "warning"; message: string }

export interface OnboardingState {
  step: Step;
  name: string;
  slug: string;
  teamId: string;
  sources: Source[];
  analysisRevision: number | null; // the source revision the analysis was built from
  sourceRevision: number;
  understandingAccepted: boolean;
  actions: ProposedAction[];
  findings: Finding[];
  validatedRevision: number | null; // the configuration revision validation passed on
  configRevision: number;
}

export const initialOnboarding = (teamId: string): OnboardingState => ({
  step: "details", name: "", slug: "", teamId, sources: [], analysisRevision: null, sourceRevision: 0,
  understandingAccepted: false, actions: [], findings: [], validatedRevision: null, configRevision: 0,
});

const SLUG = /^[a-z0-9][a-z0-9-]{1,62}$/;

export function canEnter(state: OnboardingState, step: Step): boolean {
  const accepted = state.sources.filter((s) => s.status === "accepted");
  switch (step) {
    case "details": return true;
    case "sources": return state.name.trim().length > 0 && SLUG.test(state.slug);
    case "analyzing": return canEnter(state, "sources") && accepted.length > 0;
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

export function setDetails(state: OnboardingState, name: string, slug: string): OnboardingState {
  return { ...state, name, slug };
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
