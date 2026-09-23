"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { Bot, RotateCcw, SendHorizonal } from "lucide-react";
import { useConsole } from "@pixel-console/components/console-context";
import { Dialog } from "@pixel-console/components/overlays";
import { useToast } from "@pixel-console/components/toast";
import { Alert, Badge, Button, EmptyState, ErrorState, Input, LoadingRows, PageHead, Panel, PermissionDenied, StatusBadge } from "@pixel-console/components/ui";
import { DEPLOYMENTS, RELEASES, productById, teamName } from "@pixel-console/lib/mock-data";
import {
  executeProductAction, productRecords, productShape, sendProductTurn, signIn, storedSession,
  type ApiActionShape, type ApiProductShape, type ApiRecords, type ApiSession, type ApiTurnResponse,
} from "@pixel-console/lib/pixel-api";
import type { Environment, Release } from "@pixel-console/lib/contracts";

const ENVS: Environment[] = ["development", "staging", "production"];

export default function ProductDetail() {
  const { productId } = useParams<{ productId: string }>();
  const c = useConsole();
  const liveProduct = c.live ? c.visibleProducts.find((p) => p.id === productId) : null;

  if (c.live) {
    if (!liveProduct) return <PermissionDenied what="this product" />;
    return <LiveProductWorkspace productId={productId} />;
  }

  const product = productById(productId);
  if (!product || !c.can("products.read", { productId })) return <PermissionDenied what="this product" />;
  return <MockProductDetail productId={productId} />;
}

function LiveProductWorkspace({ productId }: { productId: string }) {
  const toast = useToast();
  const [session, setSession] = useState<ApiSession | null>(null);
  const [shape, setShape] = useState<ApiProductShape | null>(null);
  const [records, setRecords] = useState<ApiRecords | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeView, setActiveView] = useState<string | null>(null);

  async function load(currentSession = session) {
    if (!currentSession) return;
    const [nextShape, nextRecords] = await Promise.all([
      productShape(currentSession, productId),
      productRecords(currentSession, productId),
    ]);
    setShape(nextShape);
    setRecords(nextRecords);
    setActiveView((current) => current ?? nextShape.views.find((view) => view.navigable)?.name ?? nextShape.views[0]?.name ?? null);
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const nextSession = storedSession() ?? await signIn("demo-admin");
        if (cancelled) return;
        setSession(nextSession);
        await load(nextSession);
        if (!cancelled) setError(null);
      } catch (caught) {
        if (!cancelled) setError(caught instanceof Error ? caught.message : "Product could not be loaded.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [productId]);

  if (loading) return <><PageHead title="Product" description="Loading live product." /><LoadingRows rows={6} /></>;
  if (error || !shape || !records || !session) {
    return <><PageHead title="Product unavailable" /><ErrorState title="Product could not be loaded">{error}</ErrorState></>;
  }

  const currentView = shape.views.find((view) => view.name === activeView) ?? shape.views[0];
  const currentEntity = currentView?.entity ? shape.entities.find((entity) => entity.name === currentView.entity) : null;
  const currentRecords = currentEntity ? records.records[currentEntity.name] ?? [] : [];

  return (
    <>
      <PageHead title={shape.product_name}
        description={`Definition ${shape.definition_id} v${shape.definition_version}. ${shape.entities.length} record types, ${shape.views.length} screens.`}
        actions={<StatusBadge status="active" />} />
      <div className="px-grid-two">
        <div className="px-stack">
          <Panel title="Screens" actions={<Badge>{records.scope}</Badge>}>
            <div className="px-row" role="tablist" aria-label="Product screens">
              {shape.views.filter((view) => view.navigable).map((view) => (
                <Button key={view.name} size="sm" variant={view.name === activeView ? "primary" : "default"}
                  onClick={() => setActiveView(view.name)}>{view.label}</Button>
              ))}
            </div>
          </Panel>
          {currentEntity ? (
            <Panel title={currentView.label} actions={<Badge>{currentRecords.length} {currentRecords.length === 1 ? currentEntity.label : currentEntity.plural}</Badge>}>
              <GenericRecordTable shape={shape} entity={currentEntity.name} rows={currentRecords}
                columns={currentView.columns.length ? currentView.columns : currentEntity.summary_fields} />
            </Panel>
          ) : (
            <Panel title={currentView?.label ?? "Screen"}>
              <EmptyState title="No records on this screen">This view has no entity attached yet.</EmptyState>
            </Panel>
          )}
        </div>
        <LiveProductChat session={session} shape={shape} productId={productId}
          onRecordsChanged={async () => {
            await load(session);
            toast("ok", "Records refreshed.");
          }} />
      </div>
    </>
  );
}

function GenericRecordTable({ shape, entity, rows, columns }: {
  shape: ApiProductShape;
  entity: string;
  rows: Array<Record<string, unknown> & { id: string; title?: string }>;
  columns: string[];
}) {
  const entityShape = shape.entities.find((candidate) => candidate.name === entity);
  const fields = entityShape?.fields ?? [];
  const visibleColumns = ["id", ...(columns.length ? columns : fields.filter((field) => field.display).map((field) => field.name))];
  if (rows.length === 0) {
    return <EmptyState title={`No ${entityShape?.plural.toLowerCase() ?? "records"} yet`}>Use Edith to create the first one.</EmptyState>;
  }
  return (
    <div className="px-table-wrap">
      <table className="px-table">
        <thead><tr>{visibleColumns.map((column) => <th key={column} scope="col">{labelFor(fields, column)}</th>)}</tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              {visibleColumns.map((column) => <td key={column}>{renderValue(row[column] ?? row.title)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LiveProductChat({ session, shape, productId, onRecordsChanged }: {
  session: ApiSession;
  shape: ApiProductShape;
  productId: string;
  onRecordsChanged: () => Promise<void>;
}) {
  const [messages, setMessages] = useState<Array<{ role: "visitor" | "agent"; text: string }>>([
    { role: "agent", text: `Welcome to ${shape.product_name}. I'm ${shape.assistant_name}.` },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const turn = useRef(0);
  const sessionId = useRef(`console-${productId}-${Date.now()}`);
  const actions = useMemo(() => Object.fromEntries(shape.actions.map((action) => [action.client_type, action])), [shape.actions]);

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    setMessages((all) => [...all, { role: "visitor", text: message }]);
    try {
      turn.current += 1;
      const response = await sendProductTurn(session, {
        sessionId: sessionId.current,
        turnId: turn.current,
        productId,
        message,
      });
      setMessages((all) => [...all, { role: "agent", text: response.speech }]);
      const receipt = await maybeExecute(session, productId, response, actions);
      if (receipt) {
        setMessages((all) => [...all, { role: "agent", text: receipt.speech }]);
        await onRecordsChanged();
      }
    } catch (caught) {
      setMessages((all) => [...all, { role: "agent", text: caught instanceof Error ? caught.message : "That request failed." }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title={<span className="px-row"><Bot aria-hidden size={16} />{shape.assistant_name}</span>}>
      <div className="px-stack">
        <div className="px-chat-log" aria-live="polite">
          {messages.map((message, index) => (
            <div key={index} className="px-console-message" data-role={message.role}>
              <strong>{message.role === "visitor" ? "You" : shape.assistant_name}</strong>
              <p>{message.text}</p>
            </div>
          ))}
        </div>
        <form className="px-row" onSubmit={(event) => { event.preventDefault(); void send(); }}>
          <Input value={input} onChange={(event) => setInput(event.target.value)}
            placeholder={`Ask ${shape.assistant_name}`} aria-label={`Ask ${shape.assistant_name}`} />
          <Button type="submit" variant="primary" loading={busy}><SendHorizonal aria-hidden />Send</Button>
        </form>
        <Alert>Mutations are committed only when the backend returns an execution key for this product and turn.</Alert>
      </div>
    </Panel>
  );
}

async function maybeExecute(
  session: ApiSession,
  productId: string,
  response: ApiTurnResponse,
  actions: Record<string, ApiActionShape>,
) {
  if (!response.execution || !response.validated_action) return null;
  const action = actions[response.validated_action.type];
  if (!action?.entity) return null;
  if (action.capability !== "CREATE_RECORD" && action.capability !== "UPDATE_RECORD") return null;
  return executeProductAction(session, {
    productId,
    entity: action.entity,
    action: action.name,
    payload: response.validated_action.payload,
    executionKey: response.execution.key,
    sessionId: response.execution.session_id,
  });
}

function labelFor(fields: ApiProductShape["entities"][number]["fields"], column: string): string {
  if (column === "id") return "ID";
  return fields.find((field) => field.name === column)?.label ?? column.replace(/_/g, " ");
}

function renderValue(value: unknown) {
  if (value === undefined || value === null || value === "") return <span className="px-muted">None</span>;
  if (Array.isArray(value)) return value.length ? value.join(", ") : <span className="px-muted">None</span>;
  return String(value);
}

function MockProductDetail({ productId }: { productId: string }) {
  const toast = useToast();
  const [rollback, setRollback] = useState<{ env: Environment; release: Release } | null>(null);
  const product = productById(productId)!;
  const releases = RELEASES.filter((r) => r.productId === product.id).sort((a, b) => b.version - a.version);
  const canDeploy = true;

  return (
    <>
      <PageHead title={product.name} description={`${product.description} Owned by ${teamName(product.teamId)}.`}
        actions={<StatusBadge status={product.state} />} />
      {product.state === "archived" ? <Alert tone="warn" title="Archived.">History stays readable; nothing can be deployed or changed until it is restored.</Alert> : null}
      <Panel title="Environments">
        <div className="px-table-wrap">
          <table className="px-table">
            <caption className="px-sr-only">Deployment per environment</caption>
            <thead><tr><th scope="col">Environment</th><th scope="col">Release</th><th scope="col">Deployment</th><th scope="col"><span className="px-sr-only">Actions</span></th></tr></thead>
            <tbody>
              {ENVS.map((env) => {
                const d = DEPLOYMENTS.find((x) => x.productId === product.id && x.environment === env);
                const release = d ? RELEASES.find((r) => r.id === d.releaseId) : undefined;
                const previous = releases.find((r) => r.state === "published" && r.id !== release?.id);
                return (
                  <tr key={env}>
                    <td style={{ textTransform: "capitalize" }}>{env}</td>
                    <td>{release ? <span className="px-mono">v{release.version} / {release.checksum.slice(0, 8)}</span> : <span className="px-muted">Nothing deployed</span>}</td>
                    <td>{d ? <StatusBadge status={d.state} /> : "-"}</td>
                    <td style={{ textAlign: "right" }}>
                      {d && previous && canDeploy && product.state === "active" ? (
                        <Button size="sm" onClick={() => setRollback({ env, release: previous })}><RotateCcw aria-hidden />Roll back to v{previous.version}</Button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel title="Releases" actions={<Link href="/console/build">Open in Build</Link>}>
        <div className="px-table-wrap">
          <table className="px-table">
            <caption className="px-sr-only">Releases, newest first</caption>
            <thead><tr><th scope="col">Version</th><th scope="col">Checksum</th><th scope="col">State</th><th scope="col">Created</th></tr></thead>
            <tbody>
              {releases.map((r) => (
                <tr key={r.id}>
                  <td className="px-mono">v{r.version}</td>
                  <td className="px-mono">{r.checksum}</td>
                  <td><StatusBadge status={r.state} /></td>
                  <td>{r.createdAt}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Dialog open={rollback !== null} onOpenChange={(open) => { if (!open) setRollback(null); }}
        title={`Roll back ${rollback?.env ?? ""}?`}
        description={rollback ? `This starts a new deployment of v${rollback.release.version} (${rollback.release.checksum.slice(0, 8)}) to ${rollback.env}. No release is edited, and open sessions finish on the release they started with.` : undefined}
        actions={<>
          <Button onClick={() => setRollback(null)}>Cancel</Button>
          <Button variant="primary" onClick={() => { toast("ok", `Rollback to v${rollback?.release.version} started (prototype: nothing was deployed).`); setRollback(null); }}>Start rollback</Button>
        </>} />
    </>
  );
}
