import { describe, expect, it } from "vitest";
import {
  acceptUnderstanding, addSource, canEnter, completeAnalysis, goTo, initialOnboarding, publish,
  removeSource, setConfirmation, setDetails, toggleAction, validate,
} from "./onboarding";

const started = () => setDetails(initialOnboarding("billing"), "Ledger", "ledger");
const analyzed = () => completeAnalysis(addSource(goTo(started(), "sources"), "billing-guide.pdf", "document"));

describe("onboarding flow", () => {
  it("cannot skip ahead", () => {
    const state = initialOnboarding("billing");
    expect(canEnter(state, "sources")).toBe(false);
    expect(() => goTo(state, "review")).toThrow();
    expect(() => publish(state)).toThrow();
  });

  it("refuses unsafe sources and never analyzes refused ones alone", () => {
    let state = goTo(started(), "sources");
    state = addSource(state, "installer.exe", "document");
    state = addSource(state, "http://intranet/wiki", "url");
    expect(state.sources.map((s) => s.status)).toEqual(["refused", "refused"]);
    expect(canEnter(state, "analyzing")).toBe(false);
  });

  it("analysis is a proposal: every generated action starts disabled and mutations ask first", () => {
    const state = analyzed();
    expect(state.step).toBe("review");
    expect(state.actions.every((a) => !a.enabled)).toBe(true);
    expect(state.actions.filter((a) => a.mutating).every((a) => a.requiresConfirmation)).toBe(true);
    expect(canEnter(state, "actions")).toBe(false);
  });

  it("a mutating action cannot be set to run without confirmation", () => {
    let state = acceptUnderstanding(analyzed());
    state = setConfirmation(state, "issue_refund", false);
    expect(state.actions.find((a) => a.key === "issue_refund")?.requiresConfirmation).toBe(true);
  });

  it("changing sources after analysis invalidates everything downstream", () => {
    let state = acceptUnderstanding(analyzed());
    state = toggleAction(state, "open_invoices", true);
    const firstSource = state.sources[0].id;
    state = removeSource(state, firstSource);
    expect(state.step).toBe("sources");
    expect(state.actions).toEqual([]);
    expect(canEnter(state, "review")).toBe(false);
  });

  it("publishing needs a clean validation of exactly the current configuration", () => {
    let state = acceptUnderstanding(analyzed());
    state = validate(state);
    expect(state.findings.some((f) => f.severity === "error")).toBe(true);
    expect(canEnter(state, "ready")).toBe(false);
    state = toggleAction(state, "open_invoices", true);
    state = validate(state);
    expect(canEnter(state, "ready")).toBe(true);
    state = toggleAction(state, "issue_refund", true); // configuration changed after validation
    expect(canEnter(state, "ready")).toBe(false);
    state = validate(state);
    expect(publish(goTo(state, "ready")).step).toBe("published");
  });
});
