import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// Reads the real stylesheet, so a token change that breaks contrast fails here.
const css = readFileSync(fileURLToPath(new URL("../app/globals.css", import.meta.url)), "utf8");

function block(selector: string): Record<string, string> {
  const start = css.indexOf(selector);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  const vars: Record<string, string> = {};
  for (const m of css.slice(open + 1, close).matchAll(/(--px-[a-z0-9-]+):\s*(#[0-9a-f]{6})/gi)) vars[m[1]] = m[2];
  return vars;
}

function luminance(hex: string): number {
  const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

const ratio = (a: string, b: string) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

// Text on its surfaces must reach 4.5:1; UI state colours on their surfaces 3:1.
const TEXT_PAIRS: Array<[string, string]> = [
  ["--px-text", "--px-bg"], ["--px-text", "--px-surface"], ["--px-text", "--px-surface-2"],
  ["--px-text-2", "--px-bg"], ["--px-text-2", "--px-surface"], ["--px-text-2", "--px-surface-2"],
  ["--px-text-3", "--px-bg"], ["--px-text-3", "--px-surface"],
  ["--px-text", "--px-page"], ["--px-text-2", "--px-page"], ["--px-text-3", "--px-page"], ["--px-accent", "--px-page"],
  ["--px-accent", "--px-bg"], ["--px-accent", "--px-accent-soft"], ["--px-on-accent", "--px-accent"],
  ["--px-ok", "--px-ok-soft"], ["--px-warn", "--px-warn-soft"], ["--px-danger", "--px-danger-soft"],
  ["--px-text", "--px-warn-soft"], ["--px-danger", "--px-bg"],
];
const UI_PAIRS: Array<[string, string]> = [
  ["--px-border-strong", "--px-bg"], ["--px-focus", "--px-bg"], ["--px-ok", "--px-bg"], ["--px-warn", "--px-bg"],
];

for (const [theme, selector] of [["light", ":root {"], ["dark", ':root[data-theme="dark"] {']] as const) {
  describe(`${theme} theme contrast`, () => {
    const vars = block(selector);
    for (const [fg, bg] of TEXT_PAIRS) {
      it(`${fg} on ${bg} >= 4.5`, () => expect(ratio(vars[fg], vars[bg])).toBeGreaterThanOrEqual(4.5));
    }
    for (const [fg, bg] of UI_PAIRS) {
      it(`${fg} on ${bg} >= 3`, () => expect(ratio(vars[fg], vars[bg])).toBeGreaterThanOrEqual(3));
    }
  });
}
