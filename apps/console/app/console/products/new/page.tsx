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
  STEP_LABELS, STEPS, acceptUnderstanding, addField, addThing, approveGeneratedUnderstanding,
  canEnter, completeAnalysis,
  definitionIdFor, goTo, initialOnboarding, publish, removeField, removeThing, setConfirmation,
  setDetails, starterDefinitionText, understood, updateField, updateThing,
  toggleAction, validate, type OnboardingState, type Step,
} from "@pixel-console/lib/onboarding";
import { addProduct, productDraft, storedSession } from "@pixel-console/lib/pixel-api";
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
  const [state, setState] = useState<OnboardingState>(() => initialOnboarding(liveTeamId ?? teams[0]?.id ?? ""));
  const [sourceName, setSourceName] = useState("");
  const [sourceKind, setSourceKind] = useState<"document" | "openapi" | "url">("document");
  const [publishing, setPublishing] = useState(false);
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);
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
    setSourceName("");
    toast("warn", "The onboarding draft was discarded.");
  }, [c.discardEpoch, teams, toast]);

  if (teams.length === 0 && !c.live) return <PermissionDenied what="product creation in any team" />;

  const slugError = state.slug && !/^[a-z0-9][a-z0-9-]{1,62}$/.test(state.slug)
    ? "Use 2 to 63 lowercase letters, digits or hyphens, starting with a letter or digit." : null;

  async function runAnalysis() {
    setDraftError(null);
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
          name: thing.id, label: thing.label.trim(), plural: thing.plural.trim(), people: thing.people,
          fields: thing.fields.filter((field) => field.name.trim()).map((field) => ({
            name: field.name.trim(), type: field.type, required: field.required,
            values: field.values.split(",").map((value) => value.trim()).filter(Boolean),
          })),
        })),
      });
      setState((s) => understood(s, {
        definition: drafted.definition, things: drafted.things,
        screens: drafted.screens, canDo: drafted.can_do,
      }));
    } catch (caught) {
      setDraftError(caught instanceof Error ? caught.message : "Pixel could not write that product.");
      setState((s) => goTo(s, "sources"));
    } finally {
      setDrafting(false);
    }
  }

  return (
    <>
      <PageHead title="New product" description="Describe what your product keeps, review Pixel's understanding, and publish it for your organization."
        actions={onAdvanced ? <Button onClick={onAdvanced}>Upload a definition</Button> : undefined} />
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
                <Field label="Product name">{(f) => (
                  <Input id={f.id} describedBy={f.describedBy} value={state.name} maxLength={80} required
                    onChange={(e) => setState((s) => setDetails(s, e.target.value, s.slug || ""))} />
                )}</Field>
                <Field label="Slug" hint="Used in addresses and API calls. It cannot be changed later." error={slugError}>{(f) => (
                  <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} value={state.slug} required
                    onChange={(e) => setState((s) => setDetails(s, s.name, e.target.value.toLowerCase()))} />
                )}</Field>
                <Field label="Owning team">{(f) => (
                  <select id={f.id} className="px-select" value={liveTeamId ?? state.teamId} disabled={Boolean(liveTeamId)}
                    onChange={(e) => setState((s) => ({ ...s, teamId: e.target.value }))}>
                    {liveTeamId ? <option value={liveTeamId}>Planning team</option>
                      : teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                  </select>
                )}</Field>
                <div className="px-row"><Button type="submit" variant="primary" disabled={!canEnter(state, "sources")}>Continue</Button></div>
              </form>
            </Panel>
          )}

          {state.step === "sources" && (
            <Panel>
              <div className="px-stack">
                <p className="px-muted">
                  Tell Pixel what your product keeps. Each kind of record becomes a screen you can
                  open, ask about and change. Mark the one that is your people, and Edith will be
                  able to assign work to them.
                </p>
                {state.things.map((thing, index) => (
                  <fieldset key={index} className="px-stack"
                    style={{ border: "1px solid var(--px-border)", borderRadius: 8, padding: 12, gap: 10 }}>
                    <legend className="px-label">{thing.label.trim() || `Record ${index + 1}`}</legend>
                    <div className="px-row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
                      <Field label="One of them is called" hint="For example: Deal">{(f) => (
                        <Input id={f.id} describedBy={f.describedBy} value={thing.label}
                          onChange={(e) => setState((st) => updateThing(st, index, {
                            label: e.target.value,
                            id: thing.id || e.target.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, ""),
                          }))} />
                      )}</Field>
                      <Field label="Many of them are called" hint="For example: Deals">{(f) => (
                        <Input id={f.id} describedBy={f.describedBy} value={thing.plural}
                          onChange={(e) => setState((st) => updateThing(st, index, { plural: e.target.value }))} />
                      )}</Field>
                      <label className="px-row px-small">
                        <input type="checkbox" checked={thing.people}
                          onChange={(e) => setState((st) => updateThing(st, index, { people: e.target.checked }))} />
                        These are my people
                      </label>
                      <Button size="sm" variant="ghost" aria-label={`Remove ${thing.label || "record"}`}
                        onClick={() => setState((st) => removeThing(st, index))}><Trash2 aria-hidden /></Button>
                    </div>
                    {thing.fields.map((field, fieldIndex) => (
                      <div key={fieldIndex} className="px-row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
                        <Field label="Carries">{(f) => (
                          <Input id={f.id} describedBy={f.describedBy} value={field.name} placeholder="title"
                            onChange={(e) => setState((st) => updateField(st, index, fieldIndex, { name: e.target.value }))} />
                        )}</Field>
                        <Field label="Which is">{(f) => (
                          <select id={f.id} className="px-select" value={field.type}
                            onChange={(e) => setState((st) => updateField(st, index, fieldIndex, { type: e.target.value as typeof field.type }))}>
                            <option value="text">Text</option><option value="enum">One of a few choices</option>
                            <option value="integer">A whole number</option><option value="date">A date</option>
                            <option value="boolean">Yes or no</option>
                          </select>
                        )}</Field>
                        {field.type === "enum" ? (
                          <Field label="Choices" hint="Separated by commas">{(f) => (
                            <Input id={f.id} describedBy={f.describedBy} value={field.values} placeholder="New, Won, Lost"
                              onChange={(e) => setState((st) => updateField(st, index, fieldIndex, { values: e.target.value }))} />
                          )}</Field>
                        ) : null}
                        <label className="px-row px-small">
                          <input type="checkbox" checked={field.required}
                            onChange={(e) => setState((st) => updateField(st, index, fieldIndex, { required: e.target.checked }))} />
                          Always needed
                        </label>
                        <Button size="sm" variant="ghost" aria-label={`Remove ${field.name || "field"}`}
                          disabled={thing.fields.length === 1}
                          onClick={() => setState((st) => removeField(st, index, fieldIndex))}><Trash2 aria-hidden /></Button>
                      </div>
                    ))}
                    <div><Button size="sm" onClick={() => setState((st) => addField(st, index))}>Add something it carries</Button></div>
                  </fieldset>
                ))}
                <div><Button onClick={() => setState((st) => addThing(st))}><Plus aria-hidden />Add a kind of record</Button></div>
                {draftError ? <Alert tone="danger">{draftError}</Alert> : null}
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
                <Alert>This is what Pixel understood. Nothing runs on it until you accept it.</Alert>
                <dl className="px-stack" style={{ gap: 8 }}>
                  <div><dt className="px-label">It keeps</dt><dd style={{ margin: 0 }} className="px-row">
                    {(state.understanding?.things ?? []).map((thing) => <Badge key={thing}>{thing}</Badge>)}
                  </dd></div>
                  <div><dt className="px-label">Its screens</dt><dd style={{ margin: 0 }} className="px-row">
                    {(state.understanding?.screens ?? []).map((screen) => <Badge key={screen}>{screen}</Badge>)}
                  </dd></div>
                  <div><dt className="px-label">Edith will be able to</dt><dd style={{ margin: 0 }}>
                    <ul className="px-stack" style={{ margin: 0, paddingLeft: 18, gap: 4 }}>
                      {(state.understanding?.canDo ?? []).map((can) => <li key={can} className="px-small">{can}</li>)}
                    </ul>
                  </dd></div>
                </dl>
                <details>
                  <summary className="px-small">Read the definition Pixel wrote</summary>
                  <pre className="px-small" style={{ maxHeight: 280, overflow: "auto", whiteSpace: "pre-wrap" }}>
                    {state.understanding?.definition}
                  </pre>
                </details>
                <div className="px-row">
                  <Button onClick={() => setState((s) => goTo(s, "sources"))}>Change the description</Button>
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
                <p>Publishing creates immutable release v1 of <strong>{state.name}</strong>. It is not deployed anywhere until you deploy it to an environment.</p>
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
                          teamId: liveTeamId ?? state.teamId,
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
                  }}>Publish v1</Button>
                </div>
              </div>
            </Panel>
          )}
        </div>
      </div>
    </>
  );
}
