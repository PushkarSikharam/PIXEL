"use client";

import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { Dialog, Menu } from "@/components/overlays";
import { useToast } from "@/components/toast";
import {
  Alert, Badge, Button, EmptyState, ErrorState, Field, Input, LoadingRows, PageHead, Panel,
  PermissionDenied, StatusBadge, StatusRaster,
} from "@/components/ui";

const TOKENS = ["--px-bg", "--px-surface", "--px-surface-2", "--px-border", "--px-border-strong", "--px-text",
  "--px-text-2", "--px-text-3", "--px-accent", "--px-accent-soft", "--px-ok", "--px-ok-soft", "--px-warn",
  "--px-warn-soft", "--px-danger", "--px-danger-soft"];

/** Every design-system part and state in one place, for review in light and dark themes. */
export default function Preview() {
  const toast = useToast();
  const [dialog, setDialog] = useState(false);
  const [theme, setTheme] = useState<"system" | "light" | "dark">("system");

  function applyTheme(next: typeof theme) {
    if (next === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", next);
    setTheme(next);
  }

  return (
    <main className="px-main" style={{ margin: "0 auto" }}>
      <PageHead title="Pixel design system" description="Components and states for review. Mock content only."
        actions={<div className="px-row" role="group" aria-label="Theme">
          {(["system", "light", "dark"] as const).map((t) => (
            <Button key={t} size="sm" variant={theme === t ? "primary" : "default"} aria-pressed={theme === t} onClick={() => applyTheme(t)}>{t}</Button>
          ))}
        </div>} />

      <Panel title="Colour tokens">
        <div className="px-stats">
          {TOKENS.map((t) => (
            <div key={t} className="px-row">
              <span aria-hidden style={{ width: 28, height: 28, borderRadius: 4, background: `var(${t})`, border: "1px solid var(--px-border-strong)" }} />
              <code>{t}</code>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Typography">
        <div className="px-stack" style={{ gap: 8 }}>
          <h1>Heading 1 · 24px</h1><h2>Heading 2 · 20px</h2><h3>Heading 3 · 16px</h3>
          <p>Body 14px. Pixel reads like an instrument: short sentences, neutral surfaces, colour only where it means something.</p>
          <p className="px-small px-muted">Small secondary 13px</p>
          <code>definition_checksum 9f2c41ab0d77e3c1</code>
        </div>
      </Panel>

      <Panel title="Buttons">
        <div className="px-row">
          <Button variant="primary"><Plus aria-hidden />Primary</Button>
          <Button>Default</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="danger"><Trash2 aria-hidden />Destructive</Button>
          <Button disabled>Disabled</Button>
          <Button loading>Saving</Button>
          <Button size="sm">Small</Button>
        </div>
      </Panel>

      <Panel title="Fields">
        <div className="px-grid-2">
          <Field label="Product name" hint="Up to 80 characters.">{(f) => <Input id={f.id} describedBy={f.describedBy} defaultValue="Ledger" />}</Field>
          <Field label="Slug" error="Use lowercase letters, digits or hyphens.">{(f) => <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} defaultValue="Ledger App" />}</Field>
          <Field label="Environment">{(f) => <select id={f.id} className="px-select"><option>Staging</option><option>Production</option></select>}</Field>
          <Field label="Disabled">{(f) => <Input id={f.id} disabled defaultValue="Read only" />}</Field>
        </div>
      </Panel>

      <Panel title="Status">
        <div className="px-stack">
          <div className="px-row">
            {["active", "published", "validated", "draft", "deploying", "failed", "revoked", "disabled", "archived", "suspended", "expired"].map((s) => <StatusBadge key={s} status={s} />)}
          </div>
          <div className="px-row">
            <Badge tone="accent">Accent</Badge>
            <StatusRaster label="Release pipeline" cells={[
              { name: "validate", state: "done" }, { name: "sign", state: "done" }, { name: "deploy staging", state: "active" },
              { name: "deploy production", state: "idle" },
            ]} />
            <StatusRaster label="Failed pipeline" cells={[{ name: "validate", state: "done" }, { name: "sign", state: "failed" }, { name: "deploy", state: "idle" }]} />
          </div>
        </div>
      </Panel>

      <Panel title="Alerts and notifications">
        <div className="px-stack">
          <Alert>Neutral information.</Alert>
          <Alert tone="ok" title="Published.">Release v6 is immutable.</Alert>
          <Alert tone="warn" title="Warning.">Some sources were refused.</Alert>
          <Alert tone="danger" title="Blocks publishing.">A mutating action must ask for confirmation.</Alert>
          <div className="px-row">
            <Button onClick={() => toast("ok", "Saved.")}>Success toast</Button>
            <Button onClick={() => toast("warn", "Approaching the budget warning.")}>Warning toast</Button>
            <Button onClick={() => toast("danger", "Deployment failed. Nothing changed in production.")}>Error toast</Button>
          </div>
        </div>
      </Panel>

      <Panel title="Dialog and menu">
        <div className="px-row">
          <Button onClick={() => setDialog(true)}>Open dialog</Button>
          <Menu label="Example menu" trigger={<Button>Open menu</Button>} sections={[
            { label: "Actions", items: [{ key: "a", label: "Rename" }, { key: "b", label: "Duplicate" }, { key: "c", label: "Unavailable", disabled: true }] },
            { items: [{ key: "d", label: "Archive" }] },
          ]} />
        </div>
        <Dialog open={dialog} onOpenChange={setDialog} title="Archive product?" description="History stays readable. Nothing can be deployed until it is restored."
          actions={<><Button onClick={() => setDialog(false)}>Cancel</Button><Button variant="danger" onClick={() => setDialog(false)}>Archive</Button></>} />
      </Panel>

      <Panel title="Table">
        <div className="px-table-wrap">
          <table className="px-table">
            <caption className="px-sr-only">Example releases</caption>
            <thead><tr><th scope="col">Version</th><th scope="col">State</th><th scope="col" className="px-num">Turns</th></tr></thead>
            <tbody>
              <tr><td className="px-mono">v6</td><td><StatusBadge status="validated" /></td><td className="px-num">0</td></tr>
              <tr><td className="px-mono">v5</td><td><StatusBadge status="published" /></td><td className="px-num">12,480</td></tr>
            </tbody>
          </table>
        </div>
      </Panel>

      <div className="px-grid-2">
        <Panel title="Loading"><LoadingRows /></Panel>
        <Panel title="Empty"><EmptyState title="No products yet">Create a product to give it its own Edith.</EmptyState></Panel>
        <Panel title="Permission denied"><PermissionDenied what="this product" /></Panel>
        <Panel title="Failure"><ErrorState title="Could not load releases" action={<Button>Try again</Button>}>Nothing was changed.</ErrorState></Panel>
      </div>
    </main>
  );
}
