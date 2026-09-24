"use client";

import { useEffect, useState } from "react";
import { Upload } from "lucide-react";
import { useConsole } from "./console-context";
import { Alert, Button, Field, Input, Panel } from "./ui";
import { approveKnowledge, productKnowledge, type ApiKnowledge, type ApiSession } from "@pixel-console/lib/pixel-api";

export function ProductKnowledge({ session, productId, onPublished }: {
  session: ApiSession; productId: string; onPublished: () => void;
}) {
  const c = useConsole();
  const [knowledge, setKnowledge] = useState<ApiKnowledge | null>(null);
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [approved, setApproved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canPublish = c.account?.role === "org_admin" || c.account?.role === "team_admin";
  useEffect(() => {
    let active = true;
    void productKnowledge(session, productId).then((value) => { if (active) setKnowledge(value); })
      .catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "Sources unavailable."); });
    return () => { active = false; };
  }, [session, productId]);
  async function publish(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !approved) return;
    setBusy(true); setError(null);
    try {
      await approveKnowledge(session, productId, title, text);
      setKnowledge(await productKnowledge(session, productId));
      setText(""); setTitle(""); setApproved(false); onPublished();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Source could not be published."); }
    finally { setBusy(false); }
  }
  return <Panel title="What Edith can answer from">
    {error ? <Alert tone="warn">{error}</Alert> : null}
    {knowledge ? <ul>{knowledge.documents.map((doc) => <li key={doc.document_id}>{doc.title} <span className="px-small px-muted">{doc.characters.toLocaleString()} characters</span></li>)}</ul> : null}
    {knowledge?.documents.length === 0 ? <p className="px-muted">Nothing yet. Until you add something here, Edith answers questions about this product by saying she has nothing to answer from.</p> : null}
    {canPublish ? <details><summary>Add something she can answer from</summary><form className="px-stack" onSubmit={publish}>
      <Field label="Title">{(f) => <Input id={f.id} value={title} maxLength={120} required onChange={(e) => setTitle(e.target.value)} />}</Field>
      <Field label="Text or Markdown file">{(f) => <Input id={f.id} type="file" accept=".txt,.md" onChange={async (e) => {
        const file = e.target.files?.[0];
        if (!file) return;
        if (file.size > 32000) { setError("Source exceeds 32 KB."); return; }
        try { setText(await file.text()); setTitle(file.name.slice(0, 120)); setApproved(false); }
        catch { setError("The file could not be read."); }
      }} />}</Field>
      <Field label="Source text">{(f) => <textarea id={f.id} className="px-input" rows={8} maxLength={32000} required value={text} onChange={(e) => { setText(e.target.value); setApproved(false); }} />}</Field>
      <label className="px-row"><input type="checkbox" checked={approved} onChange={(e) => setApproved(e.target.checked)} />Approved for everyone with access to this product</label>
      <Button type="submit" variant="primary" loading={busy} disabled={!approved || !text.trim() || !title.trim()}><Upload aria-hidden />Publish source</Button>
    </form></details> : null}
  </Panel>;
}
