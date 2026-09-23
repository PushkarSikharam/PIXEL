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
  test: "/console/test",
  deploy: "/console/deploy",
  operate: "/console/operate",
  audit: "/console/audit",
};

/** The places a definition declares that this client could not actually open. */
export function unroutableViews(views: readonly string[]): string[] {
  return views.filter((view) => !(view in CONSOLE_ROUTES));
}
