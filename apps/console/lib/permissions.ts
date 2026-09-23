/**
 * The authorization model, ported from `saas/platform/pixel_saas/authorization.py` so the mocked
 * console hides and disables exactly what the server would refuse. The server remains the
 * authority; this port is checked against the same reviewed policy fixture in `permissions.test.ts`.
 */
import type { Environment, Membership, OrganizationState, Permission, Role } from "./contracts";

export const ALL_PERMISSIONS: Permission[] = [
  "organization.manage", "members.read", "members.manage", "products.read", "products.manage",
  "definitions.read", "definitions.edit", "definitions.publish", "knowledge.read", "knowledge.manage",
  "policies.read", "policies.manage", "deployments.read", "deployments.manage", "playground.use",
  "traces.read_redacted", "usage.read", "budgets.manage", "credentials.manage", "audit.read",
  "billing.manage",
];

export const ORGANIZATION_ONLY = new Set<Permission>([
  "organization.manage", "budgets.manage", "credentials.manage", "audit.read", "billing.manage",
]);

export const ROLE_PERMISSIONS: Record<Role, Set<Permission>> = {
  org_admin: new Set(ALL_PERMISSIONS),
  team_admin: new Set<Permission>([
    "members.read", "members.manage", "products.read", "products.manage", "definitions.read",
    "definitions.edit", "definitions.publish", "knowledge.read", "knowledge.manage", "policies.read",
    "policies.manage", "deployments.read", "deployments.manage", "playground.use",
    "traces.read_redacted", "usage.read",
  ]),
  team_member: new Set<Permission>([
    "members.read", "products.read", "definitions.read", "knowledge.read", "policies.read",
    "deployments.read", "playground.use", "usage.read",
  ]),
};

const ARCHIVED_READS = new Set<Permission>([
  "products.read", "definitions.read", "knowledge.read", "policies.read", "deployments.read",
  "usage.read", "audit.read", "products.manage",
]);

export interface Resource {
  organizationId: string;
  teamId?: string | null;
  productId?: string | null;
  environment?: Environment | null;
}

export interface Directory {
  organizations: Record<string, OrganizationState>;
  suspendedTeams: Array<[string, string]>;
  archivedProducts: Array<[string, string]>;
}

export interface Decision { allowed: boolean; reason: string }

const deny = (reason: string): Decision => ({ allowed: false, reason });
const has = (pairs: Array<[string, string]>, a: string, b: string) => pairs.some(([x, y]) => x === a && y === b);

function productUsable(permission: Permission, resource: Resource, directory: Directory): boolean {
  if (!resource.productId) return true;
  if (!has(directory.archivedProducts, resource.organizationId, resource.productId)) return true;
  return ARCHIVED_READS.has(permission);
}

export function authorize(
  userId: string, memberships: Membership[], permission: Permission, resource: Resource, directory: Directory,
): Decision {
  if (!ALL_PERMISSIONS.includes(permission)) return deny("unknown_permission");
  const state = directory.organizations[resource.organizationId];
  if (state === undefined) return deny("no_access");
  const membership = memberships.find((m) => m.organizationId === resource.organizationId);
  if (!membership || membership.userId !== userId) return deny("no_access");
  if (membership.state !== "active") return deny("membership_inactive");
  if (state !== "active") return deny("organization_suspended");
  const granted = new Set([...ROLE_PERMISSIONS[membership.role], ...(membership.extra ?? [])]);

  if (membership.role === "org_admin") {
    if (resource.teamId && has(directory.suspendedTeams, resource.organizationId, resource.teamId)) return deny("team_suspended");
    if (!productUsable(permission, resource, directory)) return deny("product_archived");
    return granted.has(permission) ? { allowed: true, reason: "allowed" } : deny("permission_missing");
  }
  if (ORGANIZATION_ONLY.has(permission)) return deny("organization_permission_required");
  if (!resource.teamId) {
    return permission === "members.read" ? { allowed: true, reason: "allowed" } : deny("team_required");
  }
  if (resource.teamId !== membership.teamId) return deny("no_access");
  if (has(directory.suspendedTeams, resource.organizationId, resource.teamId)) return deny("team_suspended");
  if (!productUsable(permission, resource, directory)) return deny("product_archived");
  if (!granted.has(permission)) return deny("permission_missing");
  return { allowed: true, reason: "allowed" };
}
