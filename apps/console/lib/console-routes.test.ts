import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { CONSOLE_ROUTES, unroutableViews } from "./console-routes";

/**
 * Pixel's own definition names the places somebody can ask to be taken to. Every one of them
 * must have a route, or the assistant promises a move it cannot make.
 */
const folder = fileURLToPath(new URL("../../../products/pixel_console/definition/", import.meta.url));
// The newest published version is the one every organization is moved to.
const newest = readdirSync(folder).filter((name) => /^v\d+\.yaml$/.test(name))
  .sort((a, b) => Number(a.slice(1, -5)) - Number(b.slice(1, -5))).at(-1)!;
const definition = readFileSync(`${folder}${newest}`, "utf8");

function declaredViews(): string[] {
  const actions = definition.slice(definition.indexOf("\nactions:"), definition.indexOf("\nintents:"));
  return [...actions.matchAll(/view:\s*([a-z_]+)/g)].map((match) => match[1]);
}

describe("everywhere Pixel can be asked to open", () => {
  it("names at least the places a first-time user needs", () => {
    const views = declaredViews();
    for (const needed of ["build", "products", "demo", "architecture", "overview"]) {
      expect(views).toContain(needed);
    }
  });

  it("has a route for every place it declares", () => {
    expect(unroutableViews(declaredViews())).toEqual([]);
  });

  it("offers no screen that was only ever a design", () => {
    for (const unbuilt of ["test", "deploy", "operate", "audit"]) {
      expect(declaredViews()).not.toContain(unbuilt);
      expect(CONSOLE_ROUTES).not.toHaveProperty(unbuilt);
    }
  });
});
