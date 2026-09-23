"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Upload } from "lucide-react";
import { useConsole } from "./console-context";
import { Alert, Button, Field, Input, PageHead } from "./ui";
import { addProduct, storedSession } from "@pixel-console/lib/pixel-api";

export function ProductImport() {
  const c = useConsole();
  const router = useRouter();
  const [productId, setProductId] = useState("");
  const [definitionId, setDefinitionId] = useState("");
  const [version, setVersion] = useState(1);
  const [source, setSource] = useState("");
  const [team, setTeam] = useState(c.account?.teams[0]?.team_id ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const allowed = c.account?.role === "org_admin" || c.account?.role === "team_admin";

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError(null); setBusy(true);
    try {
      const session = storedSession();
      if (!session) throw new Error("Sign in to continue.");
      const product = await addProduct(session, { productId, definitionId, teamId: team, definition: source, version });
      c.reloadProducts(); c.selectProduct(product.product_id);
      router.push(`/console/products/${encodeURIComponent(product.product_id)}`);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Product could not be added."); }
    finally { setBusy(false); }
  }

  return <>
    <PageHead title="Add product" />
    {!allowed ? <Alert>Only an organization or team administrator can add products.</Alert> : <form className="px-stack" onSubmit={submit} style={{ maxWidth: 760 }}>
      {error ? <Alert tone="danger">{error}</Alert> : null}
      <Field label="Product ID">{(f) => <Input id={f.id} required pattern="[a-z0-9][a-z0-9_-]{0,63}" value={productId} onChange={(e) => setProductId(e.target.value)} />}</Field>
      <Field label="Team">{(f) => <select id={f.id} className="px-select" required value={team} onChange={(e) => setTeam(e.target.value)}>
        {c.account?.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}</option>)}
      </select>}</Field>
      <Field label="Definition ID">{(f) => <Input id={f.id} required maxLength={64} value={definitionId} onChange={(e) => setDefinitionId(e.target.value)} />}</Field>
      <Field label="Definition version">{(f) => <Input id={f.id} required type="number" min={1} value={version} onChange={(e) => setVersion(Number(e.target.value))} />}</Field>
      <Field label="Approved definition (.json, .yaml)">{(f) => <Input id={f.id} type="file" accept=".json,.yaml,.yml" onChange={async (event) => {
        const file = event.target.files?.[0];
        if (!file) return;
        if (file.size > 256000) { setError("Definition exceeds 250 KB."); setSource(""); return; }
        try { setSource(await file.text()); setError(null); }
        catch { setError("The file could not be read."); }
      }} />}</Field>
      <Field label="Definition">{(f) => <textarea id={f.id} className="px-input" rows={14} required value={source} onChange={(e) => setSource(e.target.value)} />}</Field>
      <p className="px-small px-muted">Organization: <code>{c.account?.tenant_id}</code></p>
      <Button type="submit" variant="primary" loading={busy} disabled={!source || !team}><Upload aria-hidden />Validate and add product</Button>
    </form>}
  </>;
}
