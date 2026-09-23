/**
 * Product-management contracts, TypeScript side. These mirror `saas/platform/pixel_saas` and
 * `saas/contracts/openapi.yaml`; there are no live handlers behind them yet.
 */

export type Role = "org_admin" | "team_admin" | "team_member";
export type MembershipState = "active" | "suspended" | "removed";
export type OrganizationState = "active" | "suspended";
export type Environment = "development" | "staging" | "production";

export type Permission =
  | "organization.manage"
  | "members.read" | "members.manage"
  | "products.read" | "products.manage"
  | "definitions.read" | "definitions.edit" | "definitions.publish"
  | "knowledge.read" | "knowledge.manage"
  | "policies.read" | "policies.manage"
  | "deployments.read" | "deployments.manage"
  | "playground.use" | "traces.read_redacted"
  | "usage.read" | "budgets.manage"
  | "credentials.manage"
  | "audit.read"
  | "billing.manage";

export interface Membership {
  userId: string;
  organizationId: string;
  role: Role;
  teamId: string | null;
  state: MembershipState;
  extra?: Permission[];
}

export interface Organization { id: string; name: string; state: OrganizationState }
export interface Team { id: string; organizationId: string; name: string; suspended?: boolean }

export type ProductState = "active" | "archived";
export interface Product {
  id: string;
  organizationId: string;
  teamId: string;
  name: string;
  slug: string;
  description: string;
  state: ProductState;
  revision: number;
}

export type VersionState = "draft" | "validated" | "published" | "retired" | "revoked";
export type DeploymentState = "pending" | "deploying" | "active" | "superseded" | "failed" | "disabled";

export interface Release {
  id: string;
  productId: string;
  version: number;
  checksum: string;
  state: VersionState;
  createdAt: string;
}

export interface Deployment {
  id: string;
  productId: string;
  environment: Environment;
  releaseId: string;
  state: DeploymentState;
}

export interface Member {
  userId: string;
  name: string;
  email: string;
  role: Role;
  teamId: string | null;
  state: MembershipState;
  lastActive: string;
}

export interface Invitation {
  id: string;
  email: string;
  role: Role;
  teamId: string | null;
  expiresAt: string;
  state: "pending" | "accepted" | "revoked" | "expired";
}

export interface UsageDay { day: string; turns: number; units: number }

/** Requests (no handlers yet). */
export interface CreateProductRequest { organizationId: string; teamId: string; name: string; slug: string; description?: string }
export interface PublishRequest { productId: string; releaseId: string; expectedRevision: number }
export interface DeployRequest { productId: string; environment: Environment; releaseId: string }
export interface RollbackRequest { productId: string; environment: Environment; toReleaseId: string }
