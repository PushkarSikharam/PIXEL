/**
 * Synthetic data for the console prototype. Every organization, person and product here is made
 * up; nothing is read from the running application or any customer.
 */
import type { Deployment, Invitation, Member, Membership, Organization, Product, Release, Team, UsageDay } from "./contracts";
import type { Directory } from "./permissions";

export const ORGANIZATIONS: Organization[] = [
  { id: "northwind", name: "Northwind Labs", state: "active" },
  { id: "contoso", name: "Contoso Health", state: "active" },
];

export const TEAMS: Team[] = [
  { id: "billing", organizationId: "northwind", name: "Billing" },
  { id: "support", organizationId: "northwind", name: "Support tools" },
  { id: "platform", organizationId: "northwind", name: "Platform", suspended: true },
  { id: "clinical", organizationId: "contoso", name: "Clinical apps" },
];

export const PRODUCTS: Product[] = [
  { id: "ledger", organizationId: "northwind", teamId: "billing", name: "Ledger", slug: "ledger", description: "Invoices, subscriptions and refunds for finance teams.", state: "active", revision: 7 },
  { id: "helpdesk", organizationId: "northwind", teamId: "support", name: "Helpdesk", slug: "helpdesk", description: "Ticket triage and customer replies.", state: "active", revision: 3 },
  { id: "legacy-portal", organizationId: "northwind", teamId: "billing", name: "Legacy portal", slug: "legacy-portal", description: "Retired customer portal, kept for its history.", state: "archived", revision: 12 },
  { id: "intake", organizationId: "contoso", teamId: "clinical", name: "Patient intake", slug: "intake", description: "Scheduling and intake forms.", state: "active", revision: 2 },
];

export const RELEASES: Release[] = [
  { id: "rel-ledger-4", productId: "ledger", version: 4, checksum: "9f2c41ab0d77e3c1", state: "published", createdAt: "2026-09-18" },
  { id: "rel-ledger-5", productId: "ledger", version: 5, checksum: "c01d9e44a8b2f610", state: "published", createdAt: "2026-09-20" },
  { id: "rel-ledger-6", productId: "ledger", version: 6, checksum: "71ab30ce95d24f08", state: "validated", createdAt: "2026-09-21" },
  { id: "rel-ledger-3", productId: "ledger", version: 3, checksum: "4be07a21c9f3d556", state: "revoked", createdAt: "2026-09-10" },
  { id: "rel-helpdesk-2", productId: "helpdesk", version: 2, checksum: "e3a9f01c77b24d12", state: "published", createdAt: "2026-09-19" },
  { id: "rel-helpdesk-3", productId: "helpdesk", version: 3, checksum: "08c4d2e19ab7f3e5", state: "draft", createdAt: "2026-09-21" },
];

export const DEPLOYMENTS: Deployment[] = [
  { id: "dep-1", productId: "ledger", environment: "production", releaseId: "rel-ledger-5", state: "active" },
  { id: "dep-2", productId: "ledger", environment: "staging", releaseId: "rel-ledger-5", state: "active" },
  { id: "dep-3", productId: "ledger", environment: "development", releaseId: "rel-ledger-6", state: "deploying" },
  { id: "dep-4", productId: "helpdesk", environment: "production", releaseId: "rel-helpdesk-2", state: "active" },
  { id: "dep-5", productId: "helpdesk", environment: "staging", releaseId: "rel-helpdesk-2", state: "failed" },
];

export const MEMBERS: Member[] = [
  { userId: "usr-avery", name: "Avery Stone", email: "avery@northwind.example", role: "org_admin", teamId: null, state: "active", lastActive: "Today" },
  { userId: "usr-bo", name: "Bo Lindqvist", email: "bo@northwind.example", role: "team_admin", teamId: "billing", state: "active", lastActive: "Today" },
  { userId: "usr-chen", name: "Chen Wei", email: "chen@northwind.example", role: "team_member", teamId: "billing", state: "active", lastActive: "Yesterday" },
  { userId: "usr-dana", name: "Dana Okafor", email: "dana@northwind.example", role: "team_member", teamId: "support", state: "active", lastActive: "3 days ago" },
  { userId: "usr-eli", name: "Eli Marsh", email: "eli@northwind.example", role: "team_member", teamId: "support", state: "suspended", lastActive: "2 weeks ago" },
];

export const INVITATIONS: Invitation[] = [
  { id: "inv-1", email: "farah@northwind.example", role: "team_member", teamId: "billing", expiresAt: "in 6 days", state: "pending" },
  { id: "inv-2", email: "gus@northwind.example", role: "team_admin", teamId: "support", expiresAt: "expired", state: "expired" },
];

export const USAGE: UsageDay[] = [
  { day: "Sep 15", turns: 820, units: 41 }, { day: "Sep 16", turns: 910, units: 44 },
  { day: "Sep 17", turns: 760, units: 38 }, { day: "Sep 18", turns: 1040, units: 52 },
  { day: "Sep 19", turns: 1190, units: 57 }, { day: "Sep 20", turns: 640, units: 30 },
  { day: "Sep 21", turns: 980, units: 47 },
];

/** Signed-in people the prototype can act as, to show what each role sees. */
export const PERSONAS: Array<{ id: string; label: string; memberships: Membership[] }> = [
  { id: "usr-avery", label: "Avery Stone (organization admin)", memberships: [
    { userId: "usr-avery", organizationId: "northwind", role: "org_admin", teamId: null, state: "active" },
  ] },
  { id: "usr-bo", label: "Bo Lindqvist (Billing team admin)", memberships: [
    { userId: "usr-bo", organizationId: "northwind", role: "team_admin", teamId: "billing", state: "active" },
  ] },
  { id: "usr-dana", label: "Dana Okafor (Support team member)", memberships: [
    { userId: "usr-dana", organizationId: "northwind", role: "team_member", teamId: "support", state: "active" },
  ] },
];

export const DIRECTORY: Directory = {
  organizations: Object.fromEntries(ORGANIZATIONS.map((o) => [o.id, o.state])),
  suspendedTeams: TEAMS.filter((t) => t.suspended).map((t) => [t.organizationId, t.id]),
  archivedProducts: PRODUCTS.filter((p) => p.state === "archived").map((p) => [p.organizationId, p.id]),
};

export const teamName = (id: string | null) => TEAMS.find((t) => t.id === id)?.name ?? "Organization";
export const productById = (id: string) => PRODUCTS.find((p) => p.id === id);
