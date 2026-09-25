import { describe, expect, it } from "vitest";
import { normalizeEmail } from "./email";

describe("an email address as Pixel keeps it", () => {
  it("is trimmed and lower-cased", () => {
    expect(normalizeEmail("  Nora@Example.COM ")).toBe("nora@example.com");
  });

  it("is refused when it cannot be one", () => {
    for (const bad of ["", "nora", "nora@", "@example.com", "nora@example", "a@b@c.com", "no ra@example.com"]) {
      expect(normalizeEmail(bad)).toBeNull();
    }
  });
});
