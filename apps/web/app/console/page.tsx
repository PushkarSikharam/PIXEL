"use client";

import Link from "next/link";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  addStarterProduct,
  clearConsoleSession,
  consoleSignIn,
  executeAction,
  getProductRecords,
  getProductShape,
  listProducts,
  sendProductTurn,
  storedConsoleSession,
  type ConsoleSession,
  type PixelProduct,
  type ProductRecords,
  type ProductShape,
} from "@/lib/pixel-console-api";

type ChatMessage = { role: "visitor" | "edith"; text: string };

export default function PixelConsolePage() {
  const [session, setSession] = useState<ConsoleSession | null>(null);
  const [products, setProducts] = useState<PixelProduct[]>([]);
  const [selectedProductId, setSelectedProductId] = useState<string | null>(null);
  const [shape, setShape] = useState<ProductShape | null>(null);
  const [records, setRecords] = useState<ProductRecords | null>(null);
  const [activeView, setActiveView] = useState<string | null>(null);
  const [newName, setNewName] = useState("Customer Portal");
  const [newSlug, setNewSlug] = useState("customer-portal");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const turn = useRef(0);
  const sessionId = useRef(`pixel-system-${Date.now()}`);

  async function signInAndLoad() {
    setBusy(true);
    setError(null);
    try {
      const next = storedConsoleSession() ?? await consoleSignIn();
      setSession(next);
      const loaded = await listProducts(next);
      setProducts(loaded);
      setSelectedProductId((current) => current ?? loaded[0]?.product_id ?? null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Pixel System could not sign in.");
    } finally {
      setBusy(false);
    }
  }

  async function refreshProduct(productId: string, currentSession = session) {
    if (!currentSession) return;
    const [nextShape, nextRecords] = await Promise.all([
      getProductShape(currentSession, productId),
      getProductRecords(currentSession, productId),
    ]);
    setShape(nextShape);
    setRecords(nextRecords);
    setActiveView((current) => current ?? nextShape.views.find((view) => view.navigable)?.name ?? nextShape.views[0]?.name ?? null);
    setMessages((current) => current.length ? current : [
      { role: "edith", text: `Welcome to ${nextShape.product_name}. I'm ${nextShape.assistant_name}.` },
    ]);
  }

  useEffect(() => {
    void signInAndLoad();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!session || !selectedProductId) return;
    setShape(null);
    setRecords(null);
    setActiveView(null);
    setMessages([]);
    void refreshProduct(selectedProductId, session);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, selectedProductId]);

  async function addProduct(event: FormEvent) {
    event.preventDefault();
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      const created = await addStarterProduct(session, { productId: newSlug, name: newName });
      const loaded = await listProducts(session);
      setProducts(loaded);
      setSelectedProductId(created.product_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Product could not be added.");
    } finally {
      setBusy(false);
    }
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    if (!session || !shape || !selectedProductId || !input.trim()) return;
    const text = input.trim();
    setInput("");
    setBusy(true);
    setMessages((current) => [...current, { role: "visitor", text }]);
    try {
      turn.current += 1;
      const response = await sendProductTurn(session, {
        sessionId: sessionId.current,
        turnId: turn.current,
        productId: selectedProductId,
        message: text,
      });
      setMessages((current) => [...current, { role: "edith", text: response.speech }]);
      const receipt = await executeAction(session, selectedProductId, response, shape);
      if (receipt) {
        setMessages((current) => [...current, { role: "edith", text: receipt.speech }]);
        await refreshProduct(selectedProductId, session);
      }
    } catch (caught) {
      setMessages((current) => [...current, { role: "edith", text: caught instanceof Error ? caught.message : "That request failed." }]);
    } finally {
      setBusy(false);
    }
  }

  const currentView = shape?.views.find((view) => view.name === activeView) ?? shape?.views[0] ?? null;
  const currentEntity = currentView?.entity ? shape?.entities.find((entity) => entity.name === currentView.entity) ?? null : null;
  const rows = currentEntity && records ? records.records[currentEntity.name] ?? [] : [];
  const actionNames = useMemo(() => shape?.actions.map((action) => action.description).slice(0, 5) ?? [], [shape]);

  return (
    <main className="system-console-page">
      <header className="console-header">
        <Link className="system-text-link" href="/">Pixel System</Link>
        <div>
          <h1>Pixel Console</h1>
          <p>Add a product, open its generated screens, and ask Edith inside that product scope.</p>
        </div>
        <div className="console-header-actions">
          <Link className="secondary-button compact" href="/">Home</Link>
          <button className="secondary-button compact" type="button" onClick={() => {
            clearConsoleSession();
            setSession(null);
            setProducts([]);
            void signInAndLoad();
          }}>Restart session</button>
        </div>
      </header>

      {error && <div className="console-error" role="alert">{error}</div>}

      <section className="console-layout">
        <aside className="console-sidebar">
          <section className="console-card">
            <h2>Products</h2>
            {products.length === 0 ? <p>No products loaded yet.</p> : (
              <div className="console-product-list">
                {products.map((product) => (
                  <button className={product.product_id === selectedProductId ? "selected" : ""}
                    key={product.product_id} onClick={() => setSelectedProductId(product.product_id)} type="button">
                    <strong>{product.name}</strong>
                    <span>{product.entities.length} records / {product.views.length} screens</span>
                  </button>
                ))}
              </div>
            )}
          </section>

          <section className="console-card">
            <h2>Add product</h2>
            <form className="console-form" onSubmit={addProduct}>
              <label>Product name<input value={newName} onChange={(event) => setNewName(event.target.value)} /></label>
              <label>Product ID<input value={newSlug} onChange={(event) => setNewSlug(event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))} /></label>
              <button className="primary-system-button" disabled={busy || !session} type="submit">Add product</button>
            </form>
          </section>
        </aside>

        <section className="console-main">
          {!shape || !records ? (
            <div className="console-card"><p>{busy ? "Loading product..." : "Select a product to open it."}</p></div>
          ) : (
            <>
              <section className="console-card">
                <div className="console-product-head">
                  <div>
                    <p className="section-kicker">Active product</p>
                    <h2>{shape.product_name}</h2>
                    <p>{shape.definition_id} v{shape.definition_version} / {records.scope}</p>
                  </div>
                  <div className="console-action-strip">
                    {shape.views.filter((view) => view.navigable).map((view) => (
                      <button className={view.name === activeView ? "selected" : ""} key={view.name}
                        onClick={() => setActiveView(view.name)} type="button">{view.label}</button>
                    ))}
                  </div>
                </div>
                {actionNames.length ? <p className="console-capabilities">Edith can: {actionNames.join("; ")}.</p> : null}
              </section>

              <section className="console-card">
                <h2>{currentView?.label ?? "Records"}</h2>
                <GenericTable entity={currentEntity} rows={rows} />
              </section>
            </>
          )}
        </section>

        <aside className="console-chat">
          <section className="console-card">
            <h2>{shape?.assistant_name ?? "Edith"}</h2>
            <div className="console-chat-log" aria-live="polite">
              {messages.map((message, index) => (
                <div className="console-chat-message" data-role={message.role} key={`${message.role}-${index}`}>
                  <strong>{message.role === "visitor" ? "You" : shape?.assistant_name ?? "Edith"}</strong>
                  <p>{message.text}</p>
                </div>
              ))}
            </div>
            <form className="console-chat-form" onSubmit={sendMessage}>
              <input value={input} onChange={(event) => setInput(event.target.value)}
                placeholder={shape ? `Ask ${shape.assistant_name}` : "Open a product first"} />
              <button className="primary-system-button" disabled={busy || !shape} type="submit">Send</button>
            </form>
          </section>
        </aside>
      </section>
    </main>
  );
}

function GenericTable({ entity, rows }: {
  entity: ProductShape["entities"][number] | null;
  rows: Array<Record<string, unknown> & { id: string; title?: string }>;
}) {
  if (!entity) return <p>No entity is attached to this screen.</p>;
  if (!rows.length) return <p>No {entity.plural.toLowerCase()} yet. Ask Edith to create one.</p>;
  const fields = ["id", entity.title_field, ...entity.summary_fields].filter((field, index, all) => all.indexOf(field) === index);
  return (
    <div className="console-table-wrap">
      <table>
        <thead><tr>{fields.map((field) => <th key={field}>{label(entity, field)}</th>)}</tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>{fields.map((field) => <td key={field}>{format(row[field] ?? row.title)}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function label(entity: ProductShape["entities"][number], field: string): string {
  if (field === "id") return "ID";
  return entity.fields.find((candidate) => candidate.name === field)?.label ?? field.replace(/_/g, " ");
}

function format(value: unknown): string {
  if (value === null || value === undefined || value === "") return "None";
  return Array.isArray(value) ? value.join(", ") : String(value);
}
