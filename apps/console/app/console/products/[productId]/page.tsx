"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { RotateCcw } from "lucide-react";
import { useConsole } from "@/components/console-context";
import { Dialog } from "@/components/overlays";
import { useToast } from "@/components/toast";
import { Alert, Button, PageHead, Panel, PermissionDenied, StatusBadge } from "@/components/ui";
import { DEPLOYMENTS, RELEASES, productById, teamName } from "@/lib/mock-data";
import type { Environment, Release } from "@/lib/contracts";

const ENVS: Environment[] = ["development", "staging", "production"];

export default function ProductDetail() {
  const { productId } = useParams<{ productId: string }>();
  const c = useConsole();
  const toast = useToast();
  const [rollback, setRollback] = useState<{ env: Environment; release: Release } | null>(null);
  const product = productById(productId);
  // Unknown and inaccessible products read the same.
  if (!product || !c.can("products.read", { productId })) return <PermissionDenied what="this product" />;
  const releases = RELEASES.filter((r) => r.productId === product.id).sort((a, b) => b.version - a.version);
  const canDeploy = c.can("deployments.manage", { productId: product.id });

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
                    <td>{release ? <span className="px-mono">v{release.version} · {release.checksum.slice(0, 8)}</span> : <span className="px-muted">Nothing deployed</span>}</td>
                    <td>{d ? <StatusBadge status={d.state} /> : "—"}</td>
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
