/**
 * Where each place in Pixel lives.
 *
 * Which URL shows which screen is the client's own business; whether somebody may go there is
 * decided by the backend, which only names a place the caller is allowed to reach. Every place
 * Pixel's definition can be asked to open must appear here, because saying "I'll open
 * Architecture" and then not moving is worse than declining.
 */
export const CONSOLE_ROUTES: Record<string, string> = {
  build: "/console/products/new",
  products: "/console/products",
  demo: "/demo",
  architecture: "/architecture",
  overview: "/console",
  members: "/console/organization",
  settings: "/console/settings",
};

export function viewForRoute(pathname: string | null | undefined): string | null {
  const path = pathname?.replace(/\/$/, "") || "/console";
  const found = Object.entries(CONSOLE_ROUTES).find(([, route]) => route.replace(/\/$/, "") === path);
  return found?.[0] ?? null;
}

/**
 * Where one record of Pixel's own product lives.
 *
 * Pixel keeps products and people, and only a product has a screen of its own. Anything else is
 * shown on the list it belongs to, so this says so rather than inventing an address that would
 * answer with nothing.
 */
export function consoleRecordRoute(entity: string, recordId: string): string | null {
  if (entity !== "product" || !recordId) return null;
  return `/console/products/${encodeURIComponent(recordId)}`;
}

/** The places a definition declares that this client could not actually open. */
export function unroutableViews(views: readonly string[]): string[] {
  return views.filter((view) => !(view in CONSOLE_ROUTES));
}
