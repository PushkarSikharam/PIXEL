"use client";

import Link from "next/link";
import { Suspense, useMemo, useState } from "react";
import { Plus } from "lucide-react";
import { useConsole } from "@pixel-console/components/console-context";
import { EmptyState, Input, LoadingRows, PageHead, StatusBadge } from "@pixel-console/components/ui";

/** Every product of the organization the signed-in person can see. */
function Catalogue() {
  const c = useConsole();
  const [query, setQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const canCreate = c.account?.role === "org_admin" || c.account?.role === "team_admin";
  const products = useMemo(() => c.visibleProducts
    .filter((p) => showArchived || p.state === "active")
    .filter((p) => p.name.toLowerCase().includes(query.trim().toLowerCase())), [c.visibleProducts, query, showArchived]);

  const head = (
    <PageHead title="Products" description="Every product your teams own in this organization."
      actions={canCreate ? <Link className="px-button" data-variant="primary" href="/console/products/new"><Plus aria-hidden />Add a product</Link> : null} />
  );

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
      {c.visibleProducts.length === 0 ? (
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
                  <td>{c.account?.teams.find((t) => t.team_id === p.teamId)?.name ?? p.teamId}</td>
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
