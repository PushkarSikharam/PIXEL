import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { unroutableViews } from "./console-routes";

/**
 * Pixel's own definition names the places somebody can ask to be taken to. Every one of them
 * must have a route, or the assistant promises a move it cannot make.
 */
const definition = readFileSync(
  fileURLToPath(new URL("../../../products/pixel_console/definition/v5.yaml", import.meta.url)),
  "utf8",
);

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
});
