"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import {
  Activity, Boxes, Building2, ChevronsUpDown, ClipboardList, FlaskConical, Gauge, Hammer, LogOut,
  Monitor, Moon, Rocket, Sun, UserCog, Users,
} from "lucide-react";
import { useConsole } from "./console-context";
import { Dialog } from "./overlays";
import { Menu } from "./overlays";
import { Button } from "./ui";
import { ORGANIZATIONS, PERSONAS, productById, teamName } from "@/lib/mock-data";
import type { Environment } from "@/lib/contracts";

const NAV = [
  { section: "Workspace", items: [
    { href: "/console", label: "Overview", icon: Gauge },
    { href: "/console/products", label: "Products", icon: Boxes },
  ] },
  { section: "Product", items: [
    { href: "/console/build", label: "Build", icon: Hammer },
    { href: "/console/test", label: "Test", icon: FlaskConical },
    { href: "/console/deploy", label: "Deploy", icon: Rocket },
  ] },
  { section: "Organization", items: [
    { href: "/console/operate", label: "Operate", icon: Activity },
    { href: "/console/audit", label: "Audit", icon: ClipboardList },
    { href: "/console/organization", label: "Members", icon: Users },
    { href: "/console/settings", label: "Settings", icon: Building2 },
  ] },
];

const ENVIRONMENTS: Environment[] = ["development", "staging", "production"];

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const c = useConsole();
  const [pending, setPending] = useState<string | null | undefined>(undefined);
  const [pendingHref, setPendingHref] = useState<string | null>(null);
  const organization = ORGANIZATIONS.find((o) => o.id === c.organizationId);
  const product = c.productId ? productById(c.productId) : undefined;

  function requestProduct(id: string | null) {
    if (id === c.productId) return;
    if (c.activeWork) setPending(id); // ask first: switching resets the conversation and drafts
    else c.selectProduct(id);
  }

  return (
    <div className="px-shell">
      <a className="px-skip" href="#main">Skip to content</a>
      <aside className="px-sidebar" aria-label="Console">
        <div className="px-brand">
          <span className="px-brand-mark" aria-hidden><span /><span /><span /><span /></span>
          Pixel
        </div>
        <nav aria-label="Primary">
          {NAV.map((group) => (
            <div key={group.section} className="px-nav">
              <div className="px-nav-section">{group.section}</div>
              {group.items.map((item) => {
                const current = item.href === "/console" ? pathname === "/console" : pathname.startsWith(item.href);
                return (
                  <Link key={item.href} href={item.href} aria-current={current ? "page" : undefined}
                    onClick={(event) => {
                      // Leaving a page with unsaved work asks first, exactly like switching product.
                      if (c.activeWork && !current) { event.preventDefault(); setPendingHref(item.href); }
                    }}>
                    <item.icon aria-hidden />{item.label}
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>
      </aside>
      <div>
        {c.activeWork ? (
          <div className="px-mode-banner" role="status">
            Unsaved work: {c.activeWork}. Switching product will discard it and reset the Edith conversation.
          </div>
        ) : null}
        <header className="px-topbar">
          <div className="px-crumbs">
            <Menu label="Organization" trigger={
              <button type="button" className="px-switcher-trigger" aria-label={`Organization: ${organization?.name}. Change organization`}>
                <Building2 aria-hidden /><span>{organization?.name}</span><ChevronsUpDown aria-hidden />
              </button>
            } sections={[{ label: "Your organizations", items: ORGANIZATIONS.map((o) => ({
              key: o.id, label: o.name,
              disabled: !c.persona.memberships.some((m) => m.organizationId === o.id),
              hint: c.persona.memberships.some((m) => m.organizationId === o.id) ? undefined : <span className="px-small px-muted">No access</span>,
            })) }]} />
            <span aria-hidden className="px-muted">/</span>
            <Menu label="Product" trigger={
              <button type="button" className="px-switcher-trigger" aria-label={`Product: ${product?.name ?? "none selected"}. Change product`}>
                <Boxes aria-hidden /><span>{product?.name ?? "Select a product"}</span><ChevronsUpDown aria-hidden />
              </button>
            } sections={[
              { label: "Products you can see", items: c.visibleProducts.map((p) => ({
                key: p.id, label: p.name, onSelect: () => requestProduct(p.id),
                hint: <span className="px-small px-muted">{p.state === "archived" ? "Archived" : teamName(p.teamId)}</span>,
              })) },
              { items: [{ key: "all", label: "All products", onSelect: () => router.push("/console/products") }] },
            ]} />
            {product ? (
              <>
                <span aria-hidden className="px-muted">/</span>
                <label className="px-sr-only" htmlFor="env">Environment</label>
                <select id="env" className="px-select" style={{ width: "auto", minHeight: 28 }} value={c.environment}
                  onChange={(e) => c.setEnvironment(e.target.value as Environment)}>
                  {ENVIRONMENTS.map((env) => <option key={env} value={env}>{env[0].toUpperCase() + env.slice(1)}</option>)}
                </select>
              </>
            ) : null}
          </div>
          <div className="px-row">
            <Menu label="Theme" align="end" trigger={
              <Button variant="ghost" size="sm" aria-label={`Theme: ${c.theme}`}>
                {c.theme === "dark" ? <Moon aria-hidden /> : c.theme === "light" ? <Sun aria-hidden /> : <Monitor aria-hidden />}
              </Button>
            } sections={[{ label: "Theme", items: (["system", "light", "dark"] as const).map((t) => ({
              key: t, label: t[0].toUpperCase() + t.slice(1), onSelect: () => c.setTheme(t),
            })) }]} />
            <Menu label="Account" align="end" trigger={
              <Button variant="ghost" size="sm" aria-label="Account and prototype persona"><UserCog aria-hidden />{c.persona.label.split(" (")[0]}</Button>
            } sections={[
              { label: "View the prototype as", items: PERSONAS.map((p) => ({ key: p.id, label: p.label, onSelect: () => c.setPersona(p.id) })) },
              { items: [{ key: "out", label: <span className="px-row"><LogOut aria-hidden size={14} />Sign out</span>, onSelect: () => router.push("/signed-out") }] },
            ]} />
          </div>
        </header>
        <main id="main" className="px-main" tabIndex={-1}>{children}</main>
      </div>
      <Dialog open={pending !== undefined} onOpenChange={(open) => { if (!open) setPending(undefined); }}
        title="Switch product?"
        description={`You have unsaved work: ${c.activeWork}. Switching to ${pending ? productById(pending)?.name : "another product"} discards it and starts a new Edith conversation. Nothing from this product carries over.`}
        actions={<>
          <Button onClick={() => setPending(undefined)}>Stay</Button>
          <Button variant="danger" onClick={() => { c.selectProduct(pending ?? null); setPending(undefined); }}>Discard and switch</Button>
        </>} />
      <Dialog open={pendingHref !== null} onOpenChange={(open) => { if (!open) setPendingHref(null); }}
        title="Leave this page?"
        description={`You have unsaved work: ${c.activeWork}. Leaving discards it.`}
        actions={<>
          <Button onClick={() => setPendingHref(null)}>Stay</Button>
          <Button variant="danger" onClick={() => { const href = pendingHref!; setPendingHref(null); c.setActiveWork(null); router.push(href); }}>Discard and leave</Button>
        </>} />
    </div>
  );
}
