"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Environment, Permission } from "@pixel-console/lib/contracts";
import { DIRECTORY, ORGANIZATIONS, PERSONAS, PRODUCTS, productById } from "@pixel-console/lib/mock-data";
import { authorize, type Resource } from "@pixel-console/lib/permissions";
import { isLive, listProducts, currentAccount, serverUnavailable, storedSession, type ApiAccount, type ApiActionShape,
  type ApiProduct, type ApiProductShape } from "@pixel-console/lib/pixel-api";

/**
 * The signed-in context of the mocked console: who is acting, in which organization, on which
 * product and environment, and whether unsaved or in-progress work would be lost by switching.
 * In the real console every one of these is derived again by the server from the session; the
 * browser's copy is only for display.
 */
interface ConsoleState {
  personaId: string;
  organizationId: string;
  productId: string | null;
  environment: Environment;
  activeWork: string | null; // e.g. "an onboarding draft" or "a playground conversation"
  // Increases each time unsaved work is discarded; the page that owns the work resets on it.
  discardEpoch: number;
  theme: "system" | "light" | "dark";
}

export interface ProductSurface {
  productId: string;
  shape: ApiProductShape;
  currentPage: string | null;
  selectedRecordId: string | null;
  reloadRecords: () => Promise<void>;
  showAction: (action: ApiActionShape, payload: Record<string, unknown>) => void;
}

interface ConsoleApi extends ConsoleState {
  persona: (typeof PERSONAS)[number];
  can: (permission: Permission, resource?: Partial<Resource>) => boolean;
  visibleProducts: typeof PRODUCTS;
  /** Set when this console is showing a running Pixel rather than its own sample data. */
  live: boolean;
  liveError: string | null;
  /** Set when the last attempt failed because Pixel's server did not answer, not because of who asked. */
  liveUnavailable: boolean;
  account: ApiAccount | null;
  loading: boolean;
  reloadProducts: () => void;
  /**
   * What the open product screen wants the assistant to be able to do: reload its records and act
   * on its screens. Absent when nobody is inside a product, and replaced whole when another is
   * opened, so one product's callbacks can never run against another's.
   */
  productSurface: ProductSurface | null;
  setProductSurface: (surface: ProductSurface | null) => void;
  setPersona: (id: string) => void;
  selectProduct: (id: string | null) => void;
  setEnvironment: (env: Environment) => void;
  setActiveWork: (what: string | null) => void;
  setTheme: (theme: ConsoleState["theme"]) => void;
}

const Context = createContext<ConsoleApi | null>(null);

export function ConsoleProvider({ children }: { children: ReactNode }) {
  const [account, setAccount] = useState<ApiAccount | null>(null);
  const [state, setState] = useState<ConsoleState>({
    personaId: PERSONAS[0].id, organizationId: ORGANIZATIONS[0].id, productId: "ledger",
    environment: "staging", activeWork: null, discardEpoch: 0, theme: "system",
  });
  const persona = PERSONAS.find((p) => p.id === state.personaId) ?? PERSONAS[0];

  const can = useCallback((permission: Permission, resource: Partial<Resource> = {}) => {
    if (isLive()) {
      if (!account || (resource.organizationId && resource.organizationId !== account.tenant_id)) return false;
      if (account.role === "org_admin") return true;
      if (permission === "products.read" || permission === "definitions.read" || permission === "playground.use") return true;
      return account.role === "team_admin" && resource.teamId === account.team_id && (permission === "products.manage" || permission === "definitions.edit");
    }
    const product = resource.productId ? productById(resource.productId) : undefined;
    const full: Resource = {
      organizationId: resource.organizationId ?? state.organizationId,
      teamId: resource.teamId ?? product?.teamId ?? null,
      productId: resource.productId ?? null,
      environment: resource.environment ?? null,
    };
    return authorize(persona.id, persona.memberships, permission, full, DIRECTORY).allowed;
  }, [persona, state.organizationId, account]);

  // Products from a running Pixel, when this console is connected to one. Until the first
  // answer arrives the sample catalogue is shown, so the console never renders half a page.
  const [live, setLive] = useState<ApiProduct[] | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [liveUnavailable, setLiveUnavailable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reloads, setReloads] = useState(0);
  const connected = isLive();

  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    // A retry after an outage shows that it is trying again rather than the old failure.
    if (reloads > 0) setLoading(true);
    (async () => {
      try {
        // Ask the server, rather than looking for something this tab happens to remember. A new
        // tab, a refresh and a reopened browser all still carry the session cookie, and each of
        // them used to look signed out because this tab's own memory was empty.
        const identity = await currentAccount();
        const products = await listProducts({ csrfToken: "", userId: identity.user_id, tenantId: identity.tenant_id });
        if (!cancelled) {
          setAccount(identity);
          setState((current) => ({ ...current, organizationId: identity.tenant_id,
            productId: products.some((p) => p.product_id === current.productId) ? current.productId : null }));
          setLive(products);
          setLiveError(null);
          setLiveUnavailable(false);
        }
      } catch (error) {
        if (!cancelled) {
          setLiveError(error instanceof Error ? error.message : "Products could not be loaded.");
          setLiveUnavailable(serverUnavailable(error));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [connected, reloads]);

  const visibleProducts = useMemo(() => {
    if (connected && live === null) return [];
    if (connected && live !== null) {
      return live.map((product) => ({
        id: product.product_id,
        organizationId: state.organizationId,
        teamId: product.team_id ?? "",
        name: product.name,
        slug: product.product_id,
        description: `${product.entities.length} kinds of record, ${product.views.length} screens.`,
        state: (product.state === "active" ? "active" : "archived") as "active" | "archived",
        revision: product.definition_version,
      }));
    }
    return PRODUCTS.filter((p) => p.organizationId === state.organizationId
      && can("products.read", { productId: p.id }));
  }, [can, connected, live, state.organizationId]);

  const reloadProducts = useCallback(() => setReloads((count) => count + 1), []);
  const [productSurface, setProductSurface] = useState<ProductSurface | null>(null);

  // Actions are stable across renders, and a setter that changes nothing keeps the same state
  // object, so effects that depend on them can never loop.
  const setPersona = useCallback((id: string) => setState((s) => {
    if (s.personaId === id) return s;
    const next = PERSONAS.find((p) => p.id === id);
    const org = next?.memberships[0]?.organizationId ?? s.organizationId;
    return { ...s, personaId: id, organizationId: org, productId: null, activeWork: null,
      discardEpoch: s.activeWork ? s.discardEpoch + 1 : s.discardEpoch };
  }), []);
  const selectProduct = useCallback((id: string | null) => setState((s) => {
    if (s.productId === id && s.activeWork === null) return s;
    // Switching away from unsaved work discards it, so the owning page must reset, not keep it.
    return { ...s, productId: id, activeWork: null, discardEpoch: s.activeWork ? s.discardEpoch + 1 : s.discardEpoch };
  }), []);
  const setEnvironment = useCallback((environment: Environment) => setState((s) => (
    s.environment === environment ? s : { ...s, environment })), []);
  const setActiveWork = useCallback((activeWork: string | null) => setState((s) => (
    s.activeWork === activeWork ? s : { ...s, activeWork })), []);
  const setTheme = useCallback((theme: ConsoleState["theme"]) => {
    if (theme === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    setState((s) => (s.theme === theme ? s : { ...s, theme }));
  }, []);

  const api = useMemo<ConsoleApi>(() => ({
    ...state, persona, can, visibleProducts, live: connected, liveError, liveUnavailable, account, loading, reloadProducts,
    productSurface, setProductSurface,
    setPersona, selectProduct, setEnvironment, setActiveWork, setTheme,
  }), [state, persona, can, visibleProducts, connected, liveError, liveUnavailable, account, loading, reloadProducts,
       productSurface,
       setPersona, selectProduct, setEnvironment, setActiveWork, setTheme]);
  return <Context.Provider value={api}>{children}</Context.Provider>;
}

export function useConsole(): ConsoleApi {
  const value = useContext(Context);
  if (!value) throw new Error("useConsole outside ConsoleProvider");
  return value;
}
