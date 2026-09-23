"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Check, FileText, Globe, Link2, Loader2, Trash2 } from "lucide-react";
import { useConsole } from "@/components/console-context";
import { useToast } from "@/components/toast";
import { Alert, Badge, Button, Field, Input, PageHead, Panel, PermissionDenied } from "@/components/ui";
import { TEAMS } from "@/lib/mock-data";
import {
  STEP_LABELS, STEPS, acceptUnderstanding, addSource, canEnter, completeAnalysis, definitionIdFor, goTo,
  initialOnboarding, publish, removeSource, setConfirmation, setDetails, starterDefinitionText,
  toggleAction, validate, type OnboardingState, type Step,
} from "@/lib/onboarding";
import { addProduct, signIn, storedSession } from "@/lib/pixel-api";

const VISIBLE_STEPS: Step[] = STEPS.filter((s) => s !== "published");

export default function NewProduct() {
  const c = useConsole();
  const toast = useToast();
  const router = useRouter();
  const teams = TEAMS.filter((t) => t.organizationId === c.organizationId && !t.suspended
    && c.can("products.manage", { teamId: t.id, productId: null }));
  const [state, setState] = useState<OnboardingState>(() => initialOnboarding(teams[0]?.id ?? ""));
  const [sourceName, setSourceName] = useState("");
  const [sourceKind, setSourceKind] = useState<"document" | "openapi" | "url">("document");
  const [publishing, setPublishing] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const { setActiveWork } = c;
  const liveTeamId = c.live ? "planning-team" : null;

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

  function runAnalysis() {
    setState((s) => goTo(s, "analyzing"));
    // Mocked analysis: nothing is sent anywhere.
    window.setTimeout(() => setState((s) => completeAnalysis(s)), 1200);
  }

  return (
    <>
      <PageHead title="New product" description="Give Pixel approved material about your product, review what it understood, and choose what Edith may do." />
      <div className="px-onboarding">
        <nav aria-label="Onboarding steps">
          <ol className="px-steps">
            {VISIBLE_STEPS.map((step, index) => {
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
                <p className="px-muted">Add documentation, an OpenAPI specification or public https pages. Executable and script files are never accepted, and nothing you upload is run.</p>
                <form className="px-row" style={{ alignItems: "flex-end" }} onSubmit={(e) => {
                  e.preventDefault();
                  if (!sourceName.trim()) return;
                  setState((s) => addSource(s, sourceName.trim(), sourceKind));
                  setSourceName("");
                }}>
                  <div style={{ flex: "1 1 260px" }}>
                    <Field label={sourceKind === "url" ? "Page address" : "File name"} hint="Prototype: type a name; no file is read.">{(f) => (
                      <Input id={f.id} describedBy={f.describedBy} value={sourceName} onChange={(e) => setSourceName(e.target.value)}
                        placeholder={sourceKind === "url" ? "https://docs.example.com/billing" : "billing-guide.pdf"} />
                    )}</Field>
                  </div>
                  <Field label="Kind">{(f) => (
                    <select id={f.id} className="px-select" value={sourceKind} onChange={(e) => setSourceKind(e.target.value as typeof sourceKind)}>
                      <option value="document">Document</option><option value="openapi">OpenAPI</option><option value="url">Web page</option>
                    </select>
                  )}</Field>
                  <Button type="submit">Add source</Button>
                </form>
                {state.sources.length === 0 ? <p className="px-muted">No sources yet.</p> : (
                  <ul className="px-stack" style={{ listStyle: "none", padding: 0, margin: 0, gap: 8 }}>
                    {state.sources.map((s) => (
                      <li key={s.id} className="px-row" style={{ justifyContent: "space-between", border: "1px solid var(--px-border)", borderRadius: 6, padding: "8px 12px" }}>
                        <span className="px-row">
                          {s.kind === "url" ? <Globe aria-hidden size={16} /> : s.kind === "openapi" ? <Link2 aria-hidden size={16} /> : <FileText aria-hidden size={16} />}
                          <span>{s.name}</span>
                          {s.status === "accepted" ? <Badge tone="ok">Accepted</Badge> : <Badge tone="danger">Refused</Badge>}
                          {s.reason ? <span className="px-small px-muted">{s.reason}</span> : null}
                        </span>
                        <Button size="sm" variant="ghost" aria-label={`Remove ${s.name}`} onClick={() => setState((st) => removeSource(st, s.id))}><Trash2 aria-hidden /></Button>
                      </li>
                    ))}
                  </ul>
                )}
                <div className="px-row">
                  <Button onClick={() => setState((s) => goTo(s, "details"))}>Back</Button>
                  <Button variant="primary" disabled={!canEnter(state, "analyzing")} onClick={runAnalysis}>Analyze sources</Button>
                </div>
              </div>
            </Panel>
          )}

          {state.step === "analyzing" && (
            <Panel>
              <div className="px-row" role="status" aria-live="polite"><Loader2 aria-hidden className="px-spin" />Reading approved sources (mocked)…</div>
            </Panel>
          )}

          {state.step === "review" && (
            <Panel>
              <div className="px-stack">
                <Alert>This is Pixel&apos;s proposal. Nothing is used until you accept it, and every action starts turned off.</Alert>
                <dl className="px-stack" style={{ gap: 8 }}>
                  <div><dt className="px-label">What the product does</dt><dd className="px-muted" style={{ margin: 0 }}>Finance teams manage invoices, subscriptions and refunds.</dd></div>
                  <div><dt className="px-label">Main records</dt><dd style={{ margin: 0 }} className="px-row"><Badge>Invoice</Badge><Badge>Customer</Badge><Badge>Subscription</Badge></dd></div>
                  <div><dt className="px-label">Proposed actions</dt><dd style={{ margin: 0 }} className="px-muted">{state.actions.length} ({state.actions.filter((a) => a.mutating).length} change data)</dd></div>
                </dl>
                <div className="px-row">
                  <Button onClick={() => setState((s) => goTo(s, "sources"))}>Change sources</Button>
                  <Button variant="primary" onClick={() => setState((s) => acceptUnderstanding(s))}>Accept and configure actions</Button>
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
                        const session = storedSession() ?? await signIn("demo-admin");
                        const definitionId = definitionIdFor(state);
                        const created = await addProduct(session, {
                          productId: state.slug,
                          teamId: liveTeamId ?? state.teamId,
                          definitionId,
                          definition: starterDefinitionText(state),
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
