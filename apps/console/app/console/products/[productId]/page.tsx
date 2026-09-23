"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { RotateCcw } from "lucide-react";
import { useConsole } from "@pixel-console/components/console-context";
import { ProductKnowledge } from "@pixel-console/components/product-knowledge";
import { Dialog } from "@pixel-console/components/overlays";
import { useToast } from "@pixel-console/components/toast";
import { Alert, Badge, Button, EmptyState, ErrorState, LoadingRows, PageHead, Panel, PermissionDenied, StatusBadge } from "@pixel-console/components/ui";
import { DEPLOYMENTS, RELEASES, productById, teamName } from "@pixel-console/lib/mock-data";
import { productRecords, productShape, storedSession,
  type ApiActionShape, type ApiProductShape, type ApiRecords, type ApiSession,
} from "@pixel-console/lib/pixel-api";
import type { Environment, Release } from "@pixel-console/lib/contracts";

const ENVS: Environment[] = ["development", "staging", "production"];

export default function ProductDetail() {
  const { productId } = useParams<{ productId: string }>();
  const c = useConsole();
  useEffect(() => { c.selectProduct(productId); }, [productId, c.selectProduct]);
  const liveProduct = c.live ? c.visibleProducts.find((p) => p.id === productId) : null;

  if (c.live) {
    if (!liveProduct) return <PermissionDenied what="this product" />;
    return <LiveProductWorkspace key={`${c.organizationId}:${productId}`} productId={productId} />;
  }

  const product = productById(productId);
  if (!product || !c.can("products.read", { productId })) return <PermissionDenied what="this product" />;
  return <MockProductDetail productId={productId} />;
}

function LiveProductWorkspace({ productId }: { productId: string }) {
  const c = useConsole();
  const toast = useToast();
  const [session, setSession] = useState<ApiSession | null>(null);
  const [shape, setShape] = useState<ApiProductShape | null>(null);
  const [records, setRecords] = useState<ApiRecords | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeView, setActiveView] = useState<string | null>(null);
  const [conversationEpoch, setConversationEpoch] = useState(0);
  const [recordSelection, setRecordSelection] = useState<{ entity: string; id: string } | null>(null);
  const [recordFilter, setRecordFilter] = useState<{ entity: string; field: string; value: unknown } | null>(null);

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
        const nextSession = storedSession();
        if (!nextSession) throw new Error("Sign in to open this product.");
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

  // Hand this product to the shell's assistant, and take it back on the way out so the next
  // screen does not inherit it. Registered before any early return, so the same hooks run on
  // every render of this screen.
  useEffect(() => {
    if (!shape || !session) return;
    c.setProductSurface({ productId, shape, reloadRecords: () => load(session), showAction });
    return () => c.setProductSurface(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [productId, shape, session]);

  if (loading) return <><PageHead title="Product" description="Loading live product." /><LoadingRows rows={6} /></>;

  if (error || !shape || !records || !session) {
    return <><PageHead title="Product unavailable" /><ErrorState title="Product could not be loaded">{error}</ErrorState></>;
  }

  const currentView = shape.views.find((view) => view.name === activeView) ?? shape.views[0];
  const currentEntity = currentView?.entity ? shape.entities.find((entity) => entity.name === currentView.entity) : null;
  const currentRecords = currentEntity ? records.records[currentEntity.name] ?? [] : [];
  const filteredRecords = recordFilter && recordFilter.entity === currentEntity?.name
    ? currentRecords.filter((row) => row[recordFilter.field] === recordFilter.value) : currentRecords;
  const selectedRecord = recordSelection && recordSelection.entity === currentEntity?.name ? currentRecords.find((row) => row.id === recordSelection.id) : null;

  function showAction(action: ApiActionShape, payload: Record<string, unknown>) {
    const view = action.view ?? shape?.views.find((candidate) => candidate.entity === action.entity && candidate.navigable)?.name;
    if (view) setActiveView(view);
    setRecordSelection(action.entity && typeof payload.record_id === "string" ? { entity: action.entity, id: payload.record_id } : null);
    setRecordFilter(action.capability === "FILTER_RECORDS" && action.entity && action.by ? { entity: action.entity, field: action.by, value: payload[action.by] } : null);
  }

  return (
    <>
      <PageHead title={shape.product_name}
        description={`Definition ${shape.definition_id} v${shape.definition_version}. ${shape.entities.length} record types, ${shape.views.length} screens.`}
        actions={<StatusBadge status="active" />} />
      <div className="px-product-workspace">
        <div className="px-stack">
          <Panel title="Screens" actions={<Badge>{records.scope}</Badge>}>
            <div className="px-row" role="tablist" aria-label="Product screens">
              {shape.views.filter((view) => view.navigable).map((view) => (
                <Button key={view.name} size="sm" variant={view.name === activeView ? "primary" : "default"}
                  onClick={() => { setActiveView(view.name); setRecordSelection(null); setRecordFilter(null); }}>{view.label}</Button>
              ))}
            </div>
          </Panel>
          {currentEntity ? (
            <Panel title={currentView.label} actions={<Badge>{currentRecords.length} {currentRecords.length === 1 ? currentEntity.label : currentEntity.plural}</Badge>}>
              {recordFilter ? <Button size="sm" onClick={() => setRecordFilter(null)}>Clear filter</Button> : null}
              {selectedRecord ? <section aria-label="Selected record" className="px-stack">
                <h2>{String(selectedRecord[currentEntity.title_field] ?? selectedRecord.id)}</h2>
                <dl>{currentEntity.fields.filter((field) => field.display).map((field) => <div key={field.name}><dt>{field.label}</dt><dd>{renderValue(selectedRecord[field.name])}</dd></div>)}</dl>
                <Button size="sm" onClick={() => setRecordSelection(null)}>Back to records</Button>
              </section> : <GenericRecordTable shape={shape} entity={currentEntity.name} rows={filteredRecords}
                columns={currentView.columns.length ? currentView.columns : currentEntity.summary_fields} />
              }
            </Panel>
          ) : (
            <Panel title={currentView?.label ?? "Screen"}>
              <EmptyState title="No records on this screen">This view has no entity attached yet.</EmptyState>
            </Panel>
          )}
          <ProductKnowledge session={session} productId={productId} onPublished={() => {
            setConversationEpoch((value) => value + 1);
            toast("ok", "Source published. A new conversation will use this version.");
          }} />
        </div>
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
              {visibleColumns.map((column) => <td key={column}>{renderValue(row[column])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
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
