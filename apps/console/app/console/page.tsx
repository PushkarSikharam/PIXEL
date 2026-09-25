"use client";

import Link from "next/link";
import { Network, PlayCircle, Plus, Users } from "lucide-react";
import { useConsole } from "@pixel-console/components/console-context";
import { Alert, EmptyState, PageHead, Panel, StatusBadge } from "@pixel-console/components/ui";

/**
 * What somebody sees when they arrive.
 *
 * A first screen that says only "no products yet" leaves a person with nothing to act on. This
 * one says what Pixel is for, what to do first, and what the assistant beside it can be asked,
 * and it changes once there is something to come back to.
 */
function LiveOverview() {
  const c = useConsole();
  const products = c.visibleProducts;
  const first = products.length === 0;

  return <>
    <PageHead
      title={first ? "Welcome to Pixel" : (c.account?.organization_name ?? "Your workspace")}
      description={first
        ? "Pixel runs your products. Describe one, and Edith can answer about it, open its screens, and create and assign its records."
        : "Your products, and where to go next."}
      actions={<Link className="px-button" data-variant="primary" href="/console/products/new">
        <Plus aria-hidden />Add a product</Link>} />

    {c.account?.role === "org_admin" && c.account.organization_name === "My organization" ? (
      <Alert title="Give your organization a name">
        It is called &quot;My organization&quot; for now. <Link href="/console/settings">Name it in Settings</Link>,
        and everyone you add will see it at the top of every page.
      </Alert>
    ) : null}

    {first ? (
      <Panel title="Start here">
        <ol className="px-stack" style={{ margin: 0, paddingLeft: 20, gap: 10 }}>
          <li>
            <strong>Describe your product.</strong> Tell Pixel what it keeps - deals, tickets,
            customers - and Pixel writes its definition for you to read before anything runs on it.
            <div className="px-row" style={{ marginTop: 6 }}>
              <Link className="px-button" data-variant="primary" href="/console/products/new">
                <Plus aria-hidden />Add your first product</Link>
            </div>
          </li>
          <li>
            <strong>See one that already works.</strong> The guided demo is a finished product
            running on the same Pixel you are using.
            <div className="px-row" style={{ marginTop: 6 }}>
              <Link className="px-button" href="/demo"><PlayCircle aria-hidden />Explore the demo</Link>
              <Link className="px-button" href="/architecture"><Network aria-hidden />How Pixel works</Link>
            </div>
          </li>
          <li>
            <strong>Ask Edith.</strong> She is beside every screen. Try "add a product",
            "show me the demo", or "how many products do I have".
          </li>
        </ol>
      </Panel>
    ) : null}

    <Panel title="Your products" actions={products.length
      ? <Link href="/console/products">All products</Link> : undefined}>
      {first ? (
        <EmptyState title="No products yet"
          action={<Link className="px-button" data-variant="primary" href="/console/products/new">
            <Plus aria-hidden />Add a product</Link>}>
          Once you add one, it appears here and Edith can answer about it.
        </EmptyState>
      ) : <div className="px-table-wrap"><table className="px-table">
        <thead><tr><th>Product</th><th>What it keeps</th><th>Version</th><th>Status</th></tr></thead>
        <tbody>{products.map((product) => (
          <tr key={product.id}>
            <td><Link href={`/console/products/${encodeURIComponent(product.id)}`}>{product.name}</Link></td>
            <td className="px-muted">{product.description}</td>
            <td>{product.revision}</td>
            <td><StatusBadge status={product.state} /></td>
          </tr>
        ))}</tbody>
      </table></div>}
    </Panel>

    {first ? null : (
      <Panel title="Elsewhere in Pixel">
        <div className="px-row" style={{ flexWrap: "wrap" }}>
          <Link className="px-button" href="/console/products/new"><Plus aria-hidden />Add a product</Link>
          <Link className="px-button" href="/console/organization"><Users aria-hidden />People and teams</Link>
          <Link className="px-button" href="/demo"><PlayCircle aria-hidden />Explore the demo</Link>
          <Link className="px-button" href="/architecture"><Network aria-hidden />How Pixel works</Link>
        </div>
      </Panel>
    )}
  </>;
}

export default function Overview() {
  return <LiveOverview />;
}
