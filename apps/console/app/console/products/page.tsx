"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { Plus } from "lucide-react";
import { useConsole } from "@pixel-console/components/console-context";
import { Button, EmptyState, ErrorState, Input, LoadingRows, PageHead, PermissionDenied, StatusBadge } from "@pixel-console/components/ui";
import { teamName } from "@pixel-console/lib/mock-data";

/** `?state=loading|empty|error|denied` shows each non-happy state of the catalogue. */
function Catalogue() {
  const c = useConsole();
  const queryState = useSearchParams().get("state");
  const demo = c.live ? null : queryState;
  const [query, setQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const canCreate = c.live ? c.account?.role === "org_admin" || c.account?.role === "team_admin" : c.persona.memberships.some((m) => m.role !== "team_member");
  const products = useMemo(() => c.visibleProducts
    .filter((p) => showArchived || p.state === "active")
    .filter((p) => p.name.toLowerCase().includes(query.trim().toLowerCase())), [c.visibleProducts, query, showArchived]);

  const head = (
    <PageHead title="Products" description="Every product your teams own in this organization."
      actions={canCreate ? <Link className="px-button" data-variant="primary" href="/console/products/new"><Plus aria-hidden />Add a product</Link> : null} />
  );
  if (demo === "denied") return <>{head}<PermissionDenied what="this organization's products" /></>;
  if (demo === "loading") return <>{head}<LoadingRows rows={5} label="Loading products" /></>;
  if (demo === "error") return <>{head}<ErrorState title="Products could not be loaded" action={<Button onClick={() => location.assign("/console/products")}>Try again</Button>}>Nothing was changed. The request can be retried safely.</ErrorState></>;

  return (
    <>
      {head}
      <div className="px-row">
        <div style={{ width: 280 }}>
          <label className="px-sr-only" htmlFor="q">Search products</label>
          <Input id="q" type="search" placeholder="Search products" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <label className="px-row px-small"><input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} /> Show archived</label>
      </div>
      {demo === "empty" || c.visibleProducts.length === 0 ? (
        <EmptyState title="No products yet" action={canCreate ? <Link className="px-button" data-variant="primary" href="/console/products/new"><Plus aria-hidden />Create the first product</Link> : undefined}>
          {canCreate ? "Create a product to give it its own Edith." : "No product in your team is visible to you yet."}
        </EmptyState>
      ) : products.length === 0 ? (
        <EmptyState title="No products match">Try a different search, or include archived products.</EmptyState>
      ) : (
        <div className="px-table-wrap">
          <table className="px-table">
            <caption className="px-sr-only">Products</caption>
            <thead><tr><th scope="col">Product</th><th scope="col">Team</th><th scope="col">Status</th><th scope="col" className="px-num">Version</th></tr></thead>
            <tbody>
              {products.map((p) => (
                <tr key={p.id}>
                  <td><Link href={`/console/products/${encodeURIComponent(p.id)}`}>{p.name}</Link><div className="px-small px-muted">{p.description}</div></td>
                  <td>{c.live ? c.account?.teams.find((t) => t.team_id === p.teamId)?.name ?? p.teamId : teamName(p.teamId)}</td>
                  <td><StatusBadge status={p.state} /></td>
                  <td className="px-num">{p.revision}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export default function ProductsPage() {
  return <Suspense fallback={<LoadingRows />}><Catalogue /></Suspense>;
}
