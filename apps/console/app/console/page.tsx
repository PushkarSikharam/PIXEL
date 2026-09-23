"use client";

import Link from "next/link";
import { AlertTriangle } from "lucide-react";
import { useConsole } from "@/components/console-context";
import { Alert, Badge, PageHead, Panel, StatusBadge, StatusRaster, type RasterState } from "@/components/ui";
import { DEPLOYMENTS, RELEASES, USAGE, teamName } from "@/lib/mock-data";
import type { Environment } from "@/lib/contracts";

const ENVS: Environment[] = ["development", "staging", "production"];

function envCells(productId: string): Array<{ state: RasterState; name: string }> {
  return ENVS.map((env) => {
    const d = DEPLOYMENTS.find((x) => x.productId === productId && x.environment === env);
    const state: RasterState = !d ? "idle" : d.state === "active" ? "done" : d.state === "failed" ? "failed"
      : d.state === "deploying" || d.state === "pending" ? "active" : "warn";
    return { state, name: env };
  });
}

export default function Overview() {
  const c = useConsole();
  const total = USAGE.reduce((sum, d) => sum + d.units, 0);
  const failed = DEPLOYMENTS.filter((d) => d.state === "failed" && c.visibleProducts.some((p) => p.id === d.productId));
  return (
    <>
      <PageHead title="Overview" description="Deployment health, releases and budget for the products you can see." />
      {failed.length ? (
        <Alert tone="danger" title="Action needed.">
          {failed.length} deployment{failed.length > 1 ? "s" : ""} failed.{" "}
          <Link href="/console/deploy">Review deployments</Link>
        </Alert>
      ) : null}
      <dl className="px-stats">
        <div className="px-stat"><dt>Products</dt><dd>{c.visibleProducts.filter((p) => p.state === "active").length}</dd></div>
        <div className="px-stat"><dt>Live in production</dt><dd>{DEPLOYMENTS.filter((d) => d.environment === "production" && d.state === "active" && c.visibleProducts.some((p) => p.id === d.productId)).length}</dd></div>
        <div className="px-stat"><dt>Turns, last 7 days</dt><dd>{USAGE.reduce((s, d) => s + d.turns, 0).toLocaleString("en-US")}</dd></div>
        <div className="px-stat"><dt>Provider units, last 7 days</dt><dd>{total} <span className="px-small px-muted">of 600</span></dd></div>
      </dl>
      <Panel title="Products" actions={<Link href="/console/products">All products</Link>}>
        {c.visibleProducts.length === 0 ? <p className="px-muted">No products are visible to you in this organization.</p> : (
          <div className="px-table-wrap">
            <table className="px-table">
              <caption className="px-sr-only">Products and deployment state per environment</caption>
              <thead><tr><th scope="col">Product</th><th scope="col">Team</th><th scope="col">Environments</th><th scope="col">Latest release</th><th scope="col">State</th></tr></thead>
              <tbody>
                {c.visibleProducts.map((p) => {
                  const latest = RELEASES.filter((r) => r.productId === p.id).sort((a, b) => b.version - a.version)[0];
                  return (
                    <tr key={p.id}>
                      <td><Link href={`/console/products/${p.id}`}>{p.name}</Link></td>
                      <td>{teamName(p.teamId)}</td>
                      <td><StatusRaster label={`${p.name} environments`} cells={envCells(p.id)} /></td>
                      <td>{latest ? <span className="px-row"><span className="px-mono">v{latest.version}</span><StatusBadge status={latest.state} /></span> : <span className="px-muted">None</span>}</td>
                      <td><StatusBadge status={p.state} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      <Panel title="Budget">
        <div className="px-stack">
          <p className="px-muted">Paid provider use is reserved before each call and stops at the hard ceiling. Numbers here are synthetic.</p>
          <div className="px-row"><Badge tone="warn" icon={<AlertTriangle aria-hidden />}>Warning at 80%</Badge><Badge>Hard ceiling 600 units</Badge></div>
        </div>
      </Panel>
    </>
  );
}
