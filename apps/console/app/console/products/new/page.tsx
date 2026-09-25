"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Check, Loader2, Plus, Trash2 } from "lucide-react";
import { useConsole } from "@pixel-console/components/console-context";
import { useToast } from "@pixel-console/components/toast";
import { Alert, Badge, Button, Field, Input, PageHead, Panel, PermissionDenied } from "@pixel-console/components/ui";
import { TEAMS } from "@pixel-console/lib/mock-data";

// Pixel's own guide, named here because the console may name her and core may not.
const ASSISTANT_NAME = "Edith";
import {
  PRODUCT_TEMPLATES, STEP_LABELS, STEPS, acceptUnderstanding, addField, addThing, addressFor, applyTemplate,
  approveGeneratedUnderstanding,
  canEnter, completeAnalysis,
  definitionIdFor, goTo, initialOnboarding, keyForName, publish, removeField, removeThing, setConfirmation,
  setDetails, starterDefinitionText, summariseAbilities, understood, updateField, updateThing,
  toggleAction, validate, type OnboardingState, type Step,
} from "@pixel-console/lib/onboarding";
import { FieldError, addProduct, productDraft, storedSession } from "@pixel-console/lib/pixel-api";
import { ProductImport } from "@pixel-console/components/product-import";

export default function NewProduct() {
  const c = useConsole();
  const [advanced, setAdvanced] = useState(false);
  if (!c.live) return <PrototypeNewProduct />;
  if (advanced) return <>
    <div className="px-row" style={{ justifyContent: "flex-end" }}>
      <Button onClick={() => setAdvanced(false)}>Describe a product instead</Button>
    </div>
    <ProductImport />
  </>;
  return <PrototypeNewProduct onAdvanced={() => setAdvanced(true)} />;
}

function PrototypeNewProduct({ onAdvanced }: { onAdvanced?: () => void }) {
  const c = useConsole();
  const toast = useToast();
  const router = useRouter();
  const teams = TEAMS.filter((t) => t.organizationId === c.organizationId && !t.suspended
    && c.can("products.manage", { teamId: t.id, productId: null }));
  const liveTeamId = c.account?.team_id ?? c.account?.teams[0]?.team_id ?? null;
  const liveTeams = c.account?.teams ?? [];
  // An admin of an organization with several teams chooses which one runs the product.
  const chooseLiveTeam = c.live && !c.account?.team_id && liveTeams.length > 1;
  const [liveChosen, setLiveChosen] = useState<string | null>(null);
  const liveTeam = chooseLiveTeam && liveTeams.some((team) => team.team_id === liveChosen) ? liveChosen : liveTeamId;
  const liveTeamName = c.account?.teams.find((team) => team.team_id === liveTeam)?.name ?? "your team";
  const [state, setState] = useState<OnboardingState>(() => initialOnboarding(liveTeamId ?? teams[0]?.id ?? ""));
  const [sourceName, setSourceName] = useState("");
  const [sourceKind, setSourceKind] = useState<"document" | "openapi" | "url">("document");
  const [publishing, setPublishing] = useState(false);
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);
  // Which box a refusal was about, so it can be shown there instead of as a notice at the foot
  // of a step the person may not even be on any more.
  const [draftField, setDraftField] = useState<string | null>(null);
  // The template the current description started from, if any, so its card shows as chosen.
  const [templateId, setTemplateId] = useState<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const { setActiveWork } = c;
  const visibleSteps: Step[] = STEPS.filter((step) => step !== "published"
    && (!c.live || (step !== "actions" && step !== "validation")));

  // A started draft is unsaved work: switching product would discard it.
  useEffect(() => {
    setActiveWork(state.name || state.sources.length ? "a product onboarding draft" : null);
  }, [state.name, state.sources.length, setActiveWork]);
  useEffect(() => () => setActiveWork(null), [setActiveWork]);
  useEffect(() => { heading.current?.focus(); }, [state.step]);
  // "Discard and switch" means discard: the draft is cleared, not left on screen untracked.
  const epoch = useRef(c.discardEpoch);
  useEffect(() => {
    if (c.discardEpoch === epoch.current) return;
    epoch.current = c.discardEpoch;
    setState(initialOnboarding(teams[0]?.id ?? ""));
    setTemplateId(null);
    setSourceName("");
    toast("warn", "The onboarding draft was discarded.");
  }, [c.discardEpoch, teams, toast]);

  if (teams.length === 0 && !c.live) return <PermissionDenied what="product creation in any team" />;

  const slugError = state.slug && !/^[a-z0-9][a-z0-9-]{1,62}$/.test(state.slug)
    ? "Use 2 to 63 lowercase letters, digits or hyphens, starting with a letter or digit." : null;

  async function runAnalysis() {
    setDraftError(null);
    setDraftField(null);
    if (!c.live) {
      setState((s) => goTo(s, "analyzing"));
      window.setTimeout(() => setState((s) => completeAnalysis(s)), 1200);
      return;
    }
    const session = storedSession();
    if (!session) { setDraftError("Sign in to add a product."); return; }
    setDrafting(true);
    setState((s) => goTo(s, "analyzing"));
    try {
      // Pixel writes the definition from the description; nothing is stored until it is accepted
      // and published, so this can be read and changed first.
      const drafted = await productDraft(session, {
        productName: state.name.trim(),
        assistantName: ASSISTANT_NAME,
        definitionId: definitionIdFor(state),
        things: state.things.map((thing) => ({
          name: keyForName(thing.label.trim() || thing.id),
          label: thing.label.trim(),
          plural: thing.plural.trim(),
          people: thing.people,
          fields: thing.fields.filter((field) => field.name.trim()).map((field) => ({
            name: keyForName(field.name.trim()),
            type: field.type,
            required: field.required,
            values: field.values.split(",").map((value) => value.trim()).filter(Boolean),
          })),
        })),
      });
      setState((s) => understood(s, {
        definition: drafted.definition, things: drafted.things,
        screens: drafted.screens, canDo: drafted.can_do,
      }));
    } catch (caught) {
      setDraftError(friendlyDraftError(caught));
      // A refusal that named a box goes back to the step that box is on, and is shown beside it.
      // Anything else stays with the description, which is where it was most likely written.
      const named = caught instanceof FieldError ? caught.field : null;
      setDraftField(named);
      setState((s) => goTo(s, named === "product_name" || named === "definition_id" ? "details" : "sources"));
    } finally {
      setDrafting(false);
    }
  }

  return (
    <>
      <PageHead title="Add a product" description="Start from a template or describe what your product keeps. Pixel turns it into screens, records and an assistant you can review before launch."
        actions={onAdvanced ? <Button onClick={onAdvanced}>Advanced import</Button> : undefined} />
      <div className="px-onboarding">
        <nav aria-label="Onboarding steps">
          <ol className="px-steps">
            {visibleSteps.map((step, index) => {
              const done = STEPS.indexOf(state.step) > STEPS.indexOf(step);
              return (
                <li key={step} aria-current={state.step === step ? "step" : undefined} data-state={done ? "done" : undefined}>
                  <span className="px-step-dot" aria-hidden>{done ? <Check size={12} /> : index + 1}</span>
                  {STEP_LABELS[step]}{done ? <span className="px-sr-only"> (complete)</span> : null}
                </li>
              );
            })}
          </ol>
        </nav>

        <div className="px-stack">
          <h2 ref={heading} tabIndex={-1}>{STEP_LABELS[state.step]}</h2>

          {state.step === "details" && (
            <Panel>
              <form className="px-stack" onSubmit={(e) => { e.preventDefault(); setState((s) => goTo(s, "sources")); }}>
                <Field label="What is your product called?" hint="For example: Acme Sales or Support Desk"
                  error={draftField === "product_name" ? draftError : null}>{(f) => (
                  <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} value={state.name}
                    maxLength={80} required
                    onChange={(e) => {
                      if (draftField === "product_name") { setDraftError(null); setDraftField(null); }
                      setState((s) => setDetails(s, e.target.value,
                        s.slugChosen ? s.slug : addressFor(e.target.value), { derived: !s.slugChosen }));
                    }} />
                )}</Field>
                {chooseLiveTeam ? (
                  <Field label="Which team runs it" hint="People in that team can use it. Admins can use every product.">{(f) => (
                    <select id={f.id} aria-describedby={f.describedBy} className="px-select" value={liveTeam ?? ""}
                      onChange={(e) => setLiveChosen(e.target.value)}>
                      {liveTeams.map((team) => <option key={team.team_id} value={team.team_id}>{team.name}</option>)}
                    </select>
                  )}</Field>
                ) : liveTeamId ? (
                  <p className="px-small px-muted" style={{ margin: 0 }}>
                    It will belong to <strong>{liveTeamName}</strong>, so everyone there can use it.
                  </p>
                ) : (
                  <Field label="Which team runs it">{(f) => (
                    <select id={f.id} className="px-select" value={state.teamId}
                      onChange={(e) => setState((s) => ({ ...s, teamId: e.target.value }))}>
                      {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                    </select>
                  )}</Field>
                )}
                <details open={Boolean(slugError)}>
                  <summary className="px-small">Advanced: change its web address</summary>
                  <div style={{ marginTop: 8 }}>
                    <Field label="Web address" hint={`Pixel makes this from the name: /console/products/${state.slug || "your-product"}`}
                      error={slugError}>{(f) => (
                      <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} value={state.slug} required
                        onChange={(e) => setState((s) => setDetails(s, s.name, e.target.value.toLowerCase()))} />
                    )}</Field>
                  </div>
                </details>
                <div className="px-row"><Button type="submit" variant="primary" disabled={!canEnter(state, "sources")}>Continue</Button></div>
              </form>
            </Panel>
          )}

          {state.step === "sources" && (
            <Panel>
              <div className="px-stack">
                <div className="px-stack" style={{ gap: 6 }}>
                  <h3 style={{ margin: 0 }}>Start from a template</h3>
                  <p className="px-muted" style={{ margin: 0 }}>
                    Pick the closest match and Pixel fills in the rest. You can launch it as it is
                    or change anything below first.
                    {state.things.length && !templateId ? " Choosing one replaces what you have described." : null}
                  </p>
                </div>
                <div className="px-template-grid" role="group" aria-label="Product templates">
                  {PRODUCT_TEMPLATES.map((template) => (
                    <button key={template.id} type="button" className="px-template-card"
                      aria-pressed={templateId === template.id}
                      onClick={() => {
                        setDraftError(null); setDraftField(null);
                        setTemplateId(template.id);
                        setState((st) => applyTemplate(st, template.id));
                      }}>
                      <strong>{template.name}</strong>
                      <span>{template.summary}</span>
                      <span className="px-template-things">
                        {template.things.map((described) => described.plural).join(" · ")}
                      </span>
                    </button>
                  ))}
                </div>
                <h3 style={{ margin: "8px 0 0" }}>{templateId ? "Adjust what it keeps" : "Or describe it yourself"}</h3>
                <p className="px-muted" style={{ margin: 0 }}>
                  Tell Pixel what your product keeps track of, like deals or tickets, and what you
                  want to know about each one. Each becomes a screen, and {ASSISTANT_NAME} can open,
                  count, add and change them for you.
                </p>
                {state.things.map((thing, index) => (
                  <fieldset key={index} className="px-stack"
                    style={{ border: "1px solid var(--px-border)", borderRadius: 8, padding: 12, gap: 10 }}>
                    <legend className="px-label">{thing.plural.trim() || thing.label.trim() || `Thing ${index + 1}`}</legend>
                    <div className="px-row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
                      <Field label="What do you call one?" hint="For example: Deal">{(f) => (
                        <Input id={f.id} describedBy={f.describedBy} value={thing.label}
                          onChange={(e) => setState((st) => updateThing(st, index, {
                            label: e.target.value,
                            id: thing.id || e.target.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, ""),
                          }))} />
                      )}</Field>
                      <Field label="And more than one?" hint="For example: Deals">{(f) => (
                        <Input id={f.id} describedBy={f.describedBy} value={thing.plural}
                          onChange={(e) => setState((st) => updateThing(st, index, { plural: e.target.value }))} />
                      )}</Field>
                      <label className="px-row px-small">
                        <input type="checkbox" checked={thing.people}
                          onChange={(e) => setState((st) => updateThing(st, index, { people: e.target.checked }))} />
                        These are people (like agents or salespeople)
                      </label>
                      <Button size="sm" variant="ghost" aria-label={`Remove ${thing.label || "record"}`}
                        onClick={() => setState((st) => removeThing(st, index))}><Trash2 aria-hidden /></Button>
                    </div>
                    {thing.fields.map((field, fieldIndex) => (
                      <div key={fieldIndex} className="px-row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
                        <Field label="Detail to keep" hint={fieldIndex === 0 ? "For example: Title, Status or Due date" : undefined}>{(f) => (
                          <Input id={f.id} describedBy={f.describedBy} value={field.name} placeholder="Title"
                            onChange={(e) => setState((st) => updateField(st, index, fieldIndex, { name: e.target.value }))} />
                        )}</Field>
                        <Field label="What kind of answer?">{(f) => (
                          <select id={f.id} className="px-select" value={field.type}
                            onChange={(e) => setState((st) => updateField(st, index, fieldIndex, { type: e.target.value as typeof field.type }))}>
                            <option value="text">Words</option><option value="enum">Pick from a list</option>
                            <option value="integer">A number</option><option value="date">A date</option>
                            <option value="boolean">Yes or no</option>
                          </select>
                        )}</Field>
                        {field.type === "enum" ? (
                          <Field label="The choices" hint="Separate them with commas">{(f) => (
                            <Input id={f.id} describedBy={f.describedBy} value={field.values} placeholder="New, Won, Lost"
                              onChange={(e) => setState((st) => updateField(st, index, fieldIndex, { values: e.target.value }))} />
                          )}</Field>
                        ) : null}
                        <label className="px-row px-small">
                          <input type="checkbox" checked={field.required}
                            onChange={(e) => setState((st) => updateField(st, index, fieldIndex, { required: e.target.checked }))} />
                          Must be filled in
                        </label>
                        <Button size="sm" variant="ghost" aria-label={`Remove ${field.name || "field"}`}
                          disabled={thing.fields.length === 1}
                          onClick={() => setState((st) => removeField(st, index, fieldIndex))}><Trash2 aria-hidden /></Button>
                      </div>
                    ))}
                    <div><Button size="sm" onClick={() => setState((st) => addField(st, index))}><Plus aria-hidden />Add a detail</Button></div>
                  </fieldset>
                ))}
                <div><Button onClick={() => setState((st) => addThing(st))}><Plus aria-hidden />{state.things.length ? "Add something else it keeps track of" : "Describe my own instead"}</Button></div>
                {draftError && !draftField ? <Alert tone="danger">{draftError}</Alert> : null}
                <div className="px-row">
                  <Button onClick={() => setState((s) => goTo(s, "details"))}>Back</Button>
                  <Button variant="primary" loading={drafting} disabled={!canEnter(state, "analyzing")}
                    onClick={runAnalysis}>Let Pixel write it</Button>
                </div>
              </div>
            </Panel>
          )}

          {state.step === "analyzing" && (
            <Panel>
              <div className="px-row" role="status" aria-live="polite">
                <Loader2 aria-hidden className="px-spin" />Writing your product&apos;s definition...
              </div>
            </Panel>
          )}

          {state.step === "review" && (
            <Panel>
              <div className="px-stack">
                <Alert>This is what Pixel understood. Nothing is live until you accept it.</Alert>
                <dl className="px-stack" style={{ gap: 8 }}>
                  <div><dt className="px-label">It keeps</dt><dd style={{ margin: 0 }} className="px-row">
                    {(state.understanding?.things ?? []).map((thing) => <Badge key={thing}>{thing}</Badge>)}
                  </dd></div>
                  <div><dt className="px-label">Its screens</dt><dd style={{ margin: 0 }} className="px-row">
                    {(state.understanding?.screens ?? []).map((screen) => <Badge key={screen}>{screen}</Badge>)}
                  </dd></div>
                  <div><dt className="px-label">Edith will be able to</dt><dd style={{ margin: 0 }}>
                    <ul className="px-stack" style={{ margin: 0, paddingLeft: 18, gap: 4 }}>
                      {summariseAbilities(state.understanding?.canDo ?? [], state.understanding?.things ?? [])
                        .map((can) => <li key={can} className="px-small">{can}</li>)}
                    </ul>
                  </dd></div>
                </dl>
                <details>
                  <summary className="px-small">Advanced: see the generated product contract</summary>
                  <pre className="px-small" style={{ maxHeight: 280, overflow: "auto", whiteSpace: "pre-wrap" }}>
                    {state.understanding?.definition}
                  </pre>
                </details>
                <div className="px-row">
                  <Button onClick={() => setState((s) => goTo(s, "sources"))}>Change what you described</Button>
                  <Button variant="primary" onClick={() => setState((s) => c.live
                    ? approveGeneratedUnderstanding(s) : acceptUnderstanding(s))}>
                    {c.live ? "Accept and continue" : "Accept and configure actions"}
                  </Button>
                </div>
              </div>
            </Panel>
          )}

          {state.step === "actions" && (
            <Panel>
              <div className="px-stack">
                <div className="px-table-wrap">
                  <table className="px-table">
                    <caption className="px-sr-only">Actions Edith may offer</caption>
                    <thead><tr><th scope="col">Action</th><th scope="col">Kind</th><th scope="col">Enabled</th><th scope="col">Ask before running</th></tr></thead>
                    <tbody>
                      {state.actions.map((a) => (
                        <tr key={a.key}>
                          <td>{a.description}<div className="px-small px-mono px-muted">{a.key}</div></td>
                          <td>{a.mutating ? <Badge tone="warn">Changes data</Badge> : <Badge>Read only</Badge>}</td>
                          <td><input type="checkbox" aria-label={`Enable ${a.description}`} checked={a.enabled} onChange={(e) => setState((s) => toggleAction(s, a.key, e.target.checked))} /></td>
                          <td>
                            <input type="checkbox" aria-label={`Ask before ${a.description}`} checked={a.requiresConfirmation} disabled={a.mutating}
                              onChange={(e) => setState((s) => setConfirmation(s, a.key, e.target.checked))} />
                            {a.mutating ? <span className="px-small px-muted"> Always</span> : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="px-row">
                  <Button onClick={() => setState((s) => goTo(s, "review"))}>Back</Button>
                  <Button variant="primary" onClick={() => setState((s) => validate(s))}>Run validation</Button>
                </div>
              </div>
            </Panel>
          )}

          {state.step === "validation" && (
            <Panel>
              <div className="px-stack">
                {state.findings.length === 0 ? <Alert tone="ok" title="All checks passed.">This configuration is ready to publish.</Alert> : state.findings.map((f) => (
                  <Alert key={f.id} tone={f.severity === "error" ? "danger" : "warn"} title={f.severity === "error" ? "Blocks publishing." : "Warning."}>{f.message}</Alert>
                ))}
                <div className="px-row">
                  <Button onClick={() => setState((s) => goTo(s, "actions"))}>Change actions</Button>
                  <Button variant="primary" disabled={!canEnter(state, "ready")} onClick={() => setState((s) => goTo(s, "ready"))}>Continue</Button>
                </div>
              </div>
            </Panel>
          )}

          {state.step === "ready" && (
            <Panel>
              <div className="px-stack">
                <p>Launching creates version 1 of <strong>{state.name}</strong> for your organization. You can open it right away and keep improving it later.</p>
                <div className="px-row">
                  <Button onClick={() => setState((s) => goTo(s, "actions"))}>Back</Button>
                  <Button variant="primary" loading={publishing} onClick={async () => {
                    setPublishing(true);
                    try {
                      if (c.live) {
                        const session = storedSession();
                        if (!session) throw new Error("Sign in to add a product.");
                        const definitionId = definitionIdFor(state);
                        const created = await addProduct(session, {
                          productId: state.slug,
                          teamId: liveTeam ?? state.teamId,
                          definitionId,
                          definition: state.understanding?.definition ?? starterDefinitionText(state),
                          version: 1,
                        });
                        c.reloadProducts();
                        setState((s) => publish(s));
                        setActiveWork(null);
                        toast("ok", `${created.name} v1 published.`);
                        router.push(`/console/products/${created.product_id}`);
                        return;
                      }
                      setState((s) => publish(s));
                      setActiveWork(null);
                      toast("ok", `${state.name} v1 published (prototype: nothing was stored).`);
                      router.push("/console/products");
                    } catch (error) {
                      toast("danger", error instanceof Error ? error.message : "Product could not be published.");
                    } finally {
                      setPublishing(false);
                    }
                  }}>Launch product</Button>
                </div>
              </div>
            </Panel>
          )}
        </div>
      </div>
    </>
  );
}

function friendlyDraftError(caught: unknown): string {
  if (caught instanceof FieldError) {
    if (caught.field === "product_name") return "Use a plain product name, like Customer Portal.";
    if (caught.field === "definition_id") return "Use a simple short link with letters, numbers and hyphens.";
  }
  const message = caught instanceof Error ? caught.message : "";
  const lower = message.toLowerCase();
  if (lower.includes("enum") || lower.includes("choices")) {
    return "Choice fields need choices such as New, Active, Closed.";
  }
  if (lower.includes("duplicate") || lower.includes("twice")) {
    return "Use a different name for each thing and each detail.";
  }
  if (lower.includes("validation error") || lower.includes("pydantic") || lower.includes("definition is invalid")) {
    return "Pixel could not understand one of the names. Use simple labels like Customer, Customers, Status or Due date.";
  }
  return message || "Pixel could not write that product.";
}
