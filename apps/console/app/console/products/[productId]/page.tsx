"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { Pencil, Plus, RotateCcw } from "lucide-react";
import { RecordCards } from "@pixel-console/components/record-cards";
import { useConsole } from "@pixel-console/components/console-context";
import { ProductKnowledge } from "@pixel-console/components/product-knowledge";
import { RecordFormDialog } from "@pixel-console/components/record-form";
import { Dialog } from "@pixel-console/components/overlays";
import { useToast } from "@pixel-console/components/toast";
import { Alert, Badge, Button, EmptyState, ErrorState, LoadingRows, PageHead, Panel, PermissionDenied, StatusBadge } from "@pixel-console/components/ui";
import { DEPLOYMENTS, RELEASES, productById, teamName } from "@pixel-console/lib/mock-data";
import { productRecords, productShape, storedSession,
  type ApiActionShape, type ApiProductShape, type ApiRecord, type ApiRecords, type ApiSession,
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
  // The record being written on a form: a new one of this kind, or one being changed. Nothing
  // proposed either, so neither carries an execution key; the server decides both on their own
  // merits.
  const [writing, setWriting] = useState<{ entity: string; record: ApiRecord | null } | null>(null);
  const surfaceCurrentPage = shape
    ? activeView ?? shape.views.find((view) => view.navigable)?.name ?? shape.views[0]?.name ?? null
    : null;

  async function load(currentSession = session) {
    if (!currentSession) return;
    const [nextShape, nextRecords] = await Promise.all([
      productShape(currentSession, productId),
      productRecords(currentSession, productId),
    ]);
    setShape(nextShape);
    setRecords(nextRecords);
    // Open on a screen that has something on it. Landing on an empty dashboard is the first
    // thing somebody sees of a product they just added, and it tells them it is empty when it
    // is not.
    setActiveView((current) => current ?? firstWorthOpening(nextShape, nextRecords) ?? null);
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
    c.setProductSurface({
      productId, shape, currentPage: surfaceCurrentPage, selectedRecordId: recordSelection?.id ?? null,
      reloadRecords: () => load(session), showAction,
    });
    return () => c.setProductSurface(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [productId, shape, session, surfaceCurrentPage, recordSelection?.id]);

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

  /**
   * Whether this record can be changed on a form here.
   *
   * A form edit says which version of the record it was made against, so an edit made against a
   * version that has moved on can be refused rather than overwriting it. A record from a source
   * that keeps no versions reports 0, and there is no honest way to offer an edit for it: that
   * product is changed where its records actually live.
   */
  function editable(record: ApiRecord | undefined | null): boolean {
    return Number(record?.revision ?? 0) >= 1;
  }

  function showAction(action: ApiActionShape, payload: Record<string, unknown>) {
    const view = action.view ?? shape?.views.find((candidate) => candidate.entity === action.entity && candidate.navigable)?.name;
    if (view) setActiveView(view);
    setRecordSelection(action.entity && typeof payload.record_id === "string" ? { entity: action.entity, id: payload.record_id } : null);
    setRecordFilter(action.capability === "FILTER_RECORDS" && action.entity && action.by ? { entity: action.entity, field: action.by, value: payload[action.by] } : null);
  }

  return (
    <>
      <PageHead title={shape.product_name}
        description={`${countOf(shape.entities.length, "kind of record", "kinds of record")}, `
          + `${countOf(shape.views.length, "screen", "screens")}. Version ${shape.definition_version}.`}
        actions={<StatusBadge status="active" />} />
      <div className="px-product-workspace">
        <div className="px-stack">
          <ProductCommandCenter shape={shape} records={records}
            onOpen={(view) => { setActiveView(view); setRecordSelection(null); setRecordFilter(null); }}
            onAdd={(entity) => setWriting({ entity, record: null })} />
          <Panel title="Screens" actions={<Badge>{records.scope}</Badge>}>
            <div className="px-row" role="tablist" aria-label="Product screens">
              {shape.views.filter((view) => view.navigable).map((view) => (
                <Button key={view.name} size="sm" variant={view.name === activeView ? "primary" : "default"}
                  onClick={() => { setActiveView(view.name); setRecordSelection(null); setRecordFilter(null); }}>{view.label}</Button>
              ))}
            </div>
          </Panel>
          {currentEntity ? (
            <Panel title={currentView.label} actions={<span className="px-row">
              <Badge>{currentRecords.length} {currentRecords.length === 1 ? currentEntity.label : currentEntity.plural}</Badge>
              <Button size="sm" variant="primary"
                onClick={() => setWriting({ entity: currentEntity.name, record: null })}>
                <Plus aria-hidden />New {currentEntity.label.toLowerCase()}</Button>
            </span>}>
              {recordFilter ? <Button size="sm" onClick={() => setRecordFilter(null)}>Clear filter</Button> : null}
              {selectedRecord ? <section aria-label="Selected record" className="px-stack">
                <h2>{String(selectedRecord[currentEntity.title_field] ?? selectedRecord.id)}</h2>
                <dl className="px-record-fields">{currentEntity.fields.filter((field) => field.display).map((field) => (
                  <div key={field.name}><dt>{field.label}</dt>
                    <dd>{renderValue(named(shape, records, currentEntity.fields, field.name, selectedRecord[field.name]))}</dd>
                  </div>
                ))}</dl>
                <div className="px-row">
                  <Button size="sm" onClick={() => setRecordSelection(null)}>Back to records</Button>
                  {editable(selectedRecord) ? (
                    <Button size="sm" variant="primary"
                      onClick={() => setWriting({ entity: currentEntity.name, record: selectedRecord })}>
                      <Pencil aria-hidden />Edit</Button>
                  ) : null}
                </div>
              </section> : <RecordCards shape={shape} records={records} entity={currentEntity.name} rows={filteredRecords}
                columns={currentView.columns.length ? currentView.columns : currentEntity.summary_fields}
                onAdd={() => setWriting({ entity: currentEntity!.name, record: null })}
                onOpen={(row) => setRecordSelection({ entity: currentEntity!.name, id: row.id })}
                onEdit={(row) => setWriting({ entity: currentEntity!.name, record: row })} />
              }
            </Panel>
          ) : (
            <Panel title={currentView?.label ?? "Screen"}>
              <ProductSummary shape={shape} records={records} onOpen={(view) => {
                setActiveView(view); setRecordSelection(null); setRecordFilter(null);
              }} />
            </Panel>
          )}
          {writing ? (
            <RecordFormDialog open session={session} productId={productId}
              entity={shape.entities.find((entity) => entity.name === writing.entity)!}
              records={records.records} editing={writing.record}
              onOpenChange={(open) => { if (!open) setWriting(null); }}
              onSaved={async (record, created) => {
                await load(session);
                setRecordSelection({ entity: writing.entity, id: record.id });
                toast("ok", created ? `${record.title || record.id} created.` : `${record.title || record.id} saved.`);
              }} />
          ) : null}
          <ProductKnowledge session={session} productId={productId} onPublished={() => {
            setConversationEpoch((value) => value + 1);
            toast("ok", "Source published. A new conversation will use this version.");
          }} />
        </div>
      </div>
    </>
  );
}

/** "1 screen" and "7 screens", without a stray "(s)". */
function countOf(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/**
 * The screen to open a product on: the first navigable one that actually has records, and
 * otherwise the first navigable one at all.
 */
function firstWorthOpening(shape: ApiProductShape, records: ApiRecords): string | null {
  const navigable = shape.views.filter((view) => view.navigable);
  const withRecords = navigable.find((view) => view.entity && (records.records[view.entity]?.length ?? 0) > 0);
  return (withRecords ?? navigable[0] ?? shape.views[0])?.name ?? null;
}

function ProductCommandCenter({ shape, records, onOpen, onAdd }: {
  shape: ApiProductShape;
  records: ApiRecords;
  onOpen: (view: string) => void;
  onAdd: (entity: string) => void;
}) {
  const metrics = shape.entities.slice(0, 4).map((entity) => ({
    entity,
    count: records.records[entity.name]?.length ?? 0,
    view: shape.views.find((view) => view.entity === entity.name && view.navigable)?.name ?? null,
  }));
  const firstRecordEntity = shape.entities.find((entity) => shape.views.some((view) => view.entity === entity.name && view.navigable));
  return (
    <Panel title="Product command center" actions={<Badge>{shape.assistant_name} is scoped here</Badge>}>
      <div className="px-stack">
        <div className="px-stats" aria-label="Product record counts">
          {metrics.length ? metrics.map(({ entity, count, view }) => (
            <button key={entity.name} type="button" className="px-stat px-stat-button"
              disabled={!view} onClick={() => view ? onOpen(view) : undefined}>
              <span className="px-stat-label">{entity.plural}</span>
              <strong className="px-stat-value">{count}</strong>
            </button>
          )) : <p className="px-muted">This product has no record types yet.</p>}
        </div>
        <div className="px-row">
          {shape.views.filter((view) => view.navigable).slice(0, 3).map((view) => (
            <Button key={view.name} size="sm" onClick={() => onOpen(view.name)}>Open {view.label}</Button>
          ))}
          {firstRecordEntity ? (
            <Button size="sm" variant="primary" onClick={() => onAdd(firstRecordEntity.name)}>
              <Plus aria-hidden />Add {firstRecordEntity.label.toLowerCase()}
            </Button>
          ) : null}
        </div>
        <p className="px-small px-muted">
          Try asking Edith: "how many {metrics[0]?.entity.plural.toLowerCase() ?? "records"} are there",
          "create a {firstRecordEntity?.label.toLowerCase() ?? "record"}", or "show me around".
        </p>
      </div>
    </Panel>
  );
}

/**
 * What a product holds, for a screen that holds nothing itself.
 *
 * A dashboard a definition declares without an entity has nothing of its own to show. Saying so
 * is accurate and useless; this says what the product does have and offers a way into each of
 * it, which is what somebody arriving on their own product needs.
 */
function ProductSummary({ shape, records, onOpen }: {
  shape: ApiProductShape;
  records: ApiRecords;
  onOpen: (view: string) => void;
}) {
  const kinds = shape.entities.map((entity) => ({
    entity,
    count: records.records[entity.name]?.length ?? 0,
    view: shape.views.find((view) => view.entity === entity.name && view.navigable)?.name ?? null,
  }));
  if (kinds.length === 0) {
    return <EmptyState title="Nothing on this screen yet">This product keeps no records.</EmptyState>;
  }
  return (
    <div className="px-stack">
      <p className="px-muted">What this product holds right now.</p>
      <div className="px-table-wrap">
        <table className="px-table">
          <caption className="px-sr-only">Records this product holds</caption>
          <thead><tr><th scope="col">Records</th><th scope="col" className="px-num">How many</th><th scope="col"><span className="px-sr-only">Open</span></th></tr></thead>
          <tbody>
            {kinds.map(({ entity, count, view }) => (
              <tr key={entity.name}>
                <td>{entity.plural}</td>
                <td className="px-num">{count}</td>
                <td style={{ textAlign: "right" }}>
                  {view ? <Button size="sm" onClick={() => onOpen(view)}>Open</Button> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function labelFor(fields: ApiProductShape["entities"][number]["fields"], column: string): string {
  if (column === "id") return "ID";
  const label = fields.find((field) => field.name === column)?.label || column.replace(/_/g, " ");
  // A definition that names its fields but does not label them gets a readable heading anyway.
  return label.charAt(0).toUpperCase() + label.slice(1);
}

/**
 * A value as the product would say it.
 *
 * A reference is stored as the identifier of the record it points at, which is the right thing to
 * store and the wrong thing to read: a column of assignees should say who they are. The record it
 * points at is already on this screen, so its title is here to be used.
 */
function named(shape: ApiProductShape, records: ApiRecords,
               fields: ApiProductShape["entities"][number]["fields"],
               column: string, value: unknown): unknown {
  const target = fields.find((field) => field.name === column)?.target;
  if (!target) return value;
  const title = (id: unknown) => {
    const found = (records.records[target] ?? []).find((record) => record.id === id);
    return found ? String(found.title || found.id) : String(id);
  };
  return Array.isArray(value) ? value.map(title) : (value === null || value === undefined ? value : title(value));
}

function renderValue(value: unknown) {
  // "None" read as a value somebody had typed. An empty field says it has not been filled in.
  const empty = <span className="px-muted">Not set</span>;
  if (value === undefined || value === null || value === "") return empty;
  if (Array.isArray(value)) return value.length ? value.join(", ") : empty;
  if (typeof value === "boolean") return value ? "Yes" : "No";
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
