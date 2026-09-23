import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { Membership, Permission, Role } from "./contracts";
import { authorize, type Directory, type Resource } from "./permissions";

// The reviewed table of who may do what. The console checks itself against it so a screen
// never offers an action the caller cannot take; the API decides for real, and the browser's
// answer is only for display.
const fixture = JSON.parse(readFileSync(
  fileURLToPath(new URL("./policy-cases.json", import.meta.url)), "utf8",
));

const directory: Directory = {
  organizations: fixture.directory.organizations,
  suspendedTeams: fixture.directory.suspended_teams,
  archivedProducts: fixture.directory.archived_products,
};

function membershipsOf(name: string): Membership[] {
  return fixture.principals[name].map((m: Record<string, unknown>) => ({
    userId: name,
    organizationId: m.organization_id as string,
    role: m.role as Role,
    teamId: (m.team_id as string | undefined) ?? null,
    state: (m.state as Membership["state"] | undefined) ?? "active",
    extra: (m.extra as Permission[] | undefined) ?? [],
  }));
}

function resourceOf(name: string): Resource {
  const r = fixture.resources[name];
  return { organizationId: r.organization_id, teamId: r.team_id ?? null, productId: r.product_id ?? null, environment: r.environment ?? null };
}

describe("permission model parity with the platform", () => {
  for (const c of fixture.cases) {
    it(`${c.who} ${c.permission} on ${c.on}`, () => {
      const decision = authorize(c.who, membershipsOf(c.who), c.permission, resourceOf(c.on), directory);
      expect(decision).toEqual({ allowed: c.allowed, reason: c.reason });
    });
  }

  it("denies a permission it does not know", () => {
    expect(authorize("ada-admin", membershipsOf("ada-admin"), "everything" as Permission, resourceOf("acme-org"), directory).allowed).toBe(false);
  });
});
