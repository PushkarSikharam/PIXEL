"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import {
  Activity, Boxes, Building2, ChevronsUpDown, ClipboardList, FlaskConical, Gauge, Hammer, LogOut,
  Monitor, Moon, Network, PanelLeftClose, PanelLeftOpen, PlayCircle, Rocket, Sun, UserCog, Users,
} from "lucide-react";
import { useConsole } from "./console-context";
import { Edith } from "./edith-shell";
import { Dialog } from "./overlays";
import { Menu } from "./overlays";
import { Alert, Button, LoadingRows } from "./ui";
import { endSession } from "@pixel-console/lib/pixel-api";
import { ORGANIZATIONS, PERSONAS, productById, teamName } from "@pixel-console/lib/mock-data";
import type { Environment } from "@pixel-console/lib/contracts";

/**
 * Where somebody can go, named for what they would find there.
 *
 * "Build", "Operate" and "Test" tell a first-time visitor nothing about what is behind them, and
 * an item that leads to a screen saying "not connected yet" is worse than one that is not there.
 * Anything still unconnected is marked `connected: false` and only shown while the console is
 * running on its own sample data, where it is a design preview rather than a promise.
 */
const NAV = [
  { section: "Your workspace", items: [
    { href: "/console", label: "Overview", icon: Gauge },
    { href: "/console/products", label: "Products", icon: Boxes },
    { href: "/console/products/new", label: "Add a product", icon: Hammer },
  ] },
  { section: "Your organization", items: [
    { href: "/console/organization", label: "People and teams", icon: Users },
    { href: "/console/settings", label: "Settings", icon: Building2 },
  ] },
  { section: "Pixel", items: [
    { href: "/demo", label: "Explore the demo", icon: PlayCircle, external: true },
    { href: "/architecture", label: "How Pixel works", icon: Network, external: true },
  ] },
  { section: "Not connected yet", connected: false, items: [
    { href: "/console/test", label: "Test", icon: FlaskConical },
    { href: "/console/deploy", label: "Deploy", icon: Rocket },
    { href: "/console/operate", label: "Operate", icon: Activity },
    { href: "/console/audit", label: "Audit", icon: ClipboardList },
  ] },
];

const ENVIRONMENTS: Environment[] = ["development", "staging", "production"];

// The parts of Pixel that really answer for a signed-in organization. Anywhere else is still a
// design, and says so rather than showing somebody another organization's sample data.
const CONNECTED_AREAS = ["/console", "/console/products", "/console/organization", "/console/settings"];

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const c = useConsole();
  const [pending, setPending] = useState<string | null | undefined>(undefined);
  const [pendingHref, setPendingHref] = useState<string | null>(null);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const organization = c.live ? { name: c.account?.organization_name ?? "Your organization" } : ORGANIZATIONS.find((o) => o.id === c.organizationId);
  const product = c.visibleProducts.find((p) => p.id === c.productId);
  // The column is always there once somebody is signed in: either the assistant, or a note saying
  // why she is not, so a missing assistant is never just a gap on the page.
  const hasAssistant = Boolean(c.live && c.account);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  // Only the most specific item is current: "Add a product" lives under "Products", and marking
  // both reads as two places at once.
  const currentHref = NAV.flatMap((group) => group.items).map((item) => item.href)
    .filter((href) => pathname === href || (href !== "/console" && pathname.startsWith(`${href}/`)))
    .sort((a, b) => b.length - a.length)[0] ?? null;

  function requestProduct(id: string | null) {
    if (id === c.productId) return;
    if (c.activeWork) setPending(id); // ask first: switching resets the conversation and drafts
    else {
      c.selectProduct(id);
      if (id) router.push(`/console/products/${encodeURIComponent(id)}`);
    }
  }

  return (
    <div className="px-shell">
      <a className="px-skip" href="#main">Skip to content</a>
      <aside className="px-sidebar" aria-label="Console" data-mobile-open={mobileNavOpen}>
        <div className="px-brand">
          <span className="px-brand-mark" aria-hidden><span /><span /><span /><span /></span>
          <span className="px-brand-text">Pixel<small>{c.live ? c.account?.organization_name ?? "Console" : "Console"}</small></span>
          <button type="button" className="px-mobile-nav-toggle"
            aria-expanded={mobileNavOpen} aria-controls="pixel-primary-navigation"
            aria-label={mobileNavOpen ? "Close navigation" : "Open navigation"}
            onClick={() => setMobileNavOpen((open) => !open)}>
            {mobileNavOpen ? <PanelLeftClose aria-hidden /> : <PanelLeftOpen aria-hidden />}
          </button>
        </div>
        <nav id="pixel-primary-navigation" aria-label="Primary">
          <div className="px-nav">
            <a href="/demo"><Monitor aria-hidden />Visit demo</a>
          </div>
          {NAV.filter((group) => !c.live || group.connected !== false).map((group) => (
            <div key={group.section} className="px-nav">
              <div className="px-nav-section">{group.section}</div>
              {group.items.map((item) => {
                const current = item.href === currentHref;
                return (
                  <Link key={item.href} href={item.href} aria-current={current ? "page" : undefined}
                    onClick={(event) => {
                      // Leaving a page with unsaved work asks first, exactly like switching product.
                      if (c.activeWork && !current) { event.preventDefault(); setPendingHref(item.href); }
                      else setMobileNavOpen(false);
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
        {logoutError ? <Alert tone="warn">{logoutError}</Alert> : null}
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
            } sections={[{ label: "Your organizations", items: (c.live ? [{ id: c.organizationId, name: organization?.name ?? "Your organization" }] : ORGANIZATIONS).map((o) => ({
              key: o.id, label: o.name,
              disabled: c.live || !c.persona.memberships.some((m) => m.organizationId === o.id),
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
            {product && !c.live ? (
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
              <Button variant="ghost" size="sm" aria-label="Account"><UserCog aria-hidden />{c.live ? c.account?.email ?? "Account" : c.persona.label.split(" (")[0]}</Button>
            } sections={[
              ...(!c.live ? [{ label: "View the prototype as", items: PERSONAS.map((p) => ({ key: p.id, label: p.label, onSelect: () => c.setPersona(p.id) })) }] : []),
              { items: [{ key: "out", label: <span className="px-row"><LogOut aria-hidden size={14} />Sign out</span>, onSelect: () => {
                void endSession().then(() => router.push("/signed-out")).catch(() => setLogoutError("Sign-out could not be confirmed. Please retry."));
              } }] },
            ]} />
          </div>
        </header>
        <main id="main" className={`px-main${hasAssistant ? " px-with-assistant" : ""}`} tabIndex={-1}>
          {hasAssistant ? <Edith /> : null}
          {/* The page is one column beside the assistant. Without this wrapper each thing on the
              page is a row of the same grid as the assistant, so the first row is as tall as the
              assistant and everything after the heading starts below the fold. */}
          <div className="px-main-column">
            {c.live && c.loading ? <LoadingRows rows={4} /> : c.live && !c.account && c.liveUnavailable ? <div className="px-stack">
              <h1>Your Pixel workspace</h1>
              <Alert tone="warn" title="Pixel is temporarily unavailable">
                Pixel&apos;s server is not responding, so your workspace and assistant cannot load
                right now. You are not signed out, and nothing has been lost.
              </Alert>
              <div className="px-row"><Button variant="primary" onClick={c.reloadProducts}>Try again</Button></div>
            </div> : c.live && !c.account ? <div className="px-stack">
              <h1>Your Pixel workspace</h1>
              {c.liveError
                ? <Alert tone="warn">{c.liveError}</Alert>
                : <p className="px-muted" style={{ margin: 0 }}>Sign in to see your products and ask Edith about them, or explore the demo first.</p>}
              <div className="px-row">
                <Link className="px-button" data-variant="primary" href="/sign-in">Sign in</Link>
                <a className="px-button" href="/demo">Explore the demo</a>
              </div>
            </div> : c.live && !CONNECTED_AREAS.some((area) =>
              area === pathname || (area !== "/console" && pathname.startsWith(area + "/"))) ?
              <Alert title="Not connected yet">
                This part of Pixel is designed but not yet connected to your workspace, so nothing
                here would be yours. <Link href="/console">Back to your workspace</Link>.
              </Alert> : children}
          </div>
        </main>
      </div>
      <Dialog open={pending !== undefined} onOpenChange={(open) => { if (!open) setPending(undefined); }}
        title="Switch product?"
        description={`You have unsaved work: ${c.activeWork}. Switching to ${pending ? productById(pending)?.name : "another product"} discards it and starts a new Edith conversation. Nothing from this product carries over.`}
        actions={<>
          <Button onClick={() => setPending(undefined)}>Stay</Button>
          <Button variant="danger" onClick={() => { c.selectProduct(pending ?? null); if (pending) router.push(`/console/products/${encodeURIComponent(pending)}`); setPending(undefined); }}>Discard and switch</Button>
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
