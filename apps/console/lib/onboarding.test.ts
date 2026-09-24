import { describe, expect, it } from "vitest";
import {
  acceptUnderstanding, addSource, addThing, approveGeneratedUnderstanding, canEnter,
  completeAnalysis, goTo, initialOnboarding,
  keyForName, publish, removeSource, setConfirmation, setDetails, toggleAction, updateField, updateThing,
  understood, validate,
} from "./onboarding";

const started = () => setDetails(initialOnboarding("billing"), "Ledger", "ledger");
/** A product described the way somebody adding one describes it. */
const described = () => {
  let state = goTo(started(), "sources");
  state = addThing(state);
  state = updateThing(state, 0, { id: "invoice", label: "Invoice", plural: "Invoices" });
  state = updateField(state, 0, 0, { name: "reference", type: "text", required: true });
  return state;
};
const analyzed = () => completeAnalysis(described());

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

  it("publishes the reviewed server-generated definition without fake browser action switches", () => {
    const reviewed = understood(described(), {
      definition: "approved-definition",
      things: ["Invoices"],
      screens: ["Invoices"],
      canDo: ["Open the list of invoices.", "Add an invoice."],
    });
    const approved = approveGeneratedUnderstanding(reviewed);
    expect(approved.step).toBe("ready");
    expect(approved.actions.every((action) => action.enabled)).toBe(true);
    expect(approved.actions.find((action) => action.description === "Add an invoice.")?.requiresConfirmation).toBe(true);
    expect(canEnter(approved, "ready")).toBe(true);
  });

  it("changing the description after analysis invalidates everything downstream", () => {
    let state = acceptUnderstanding(analyzed());
    state = toggleAction(state, "open_invoices", true);
    state = updateThing(state, 0, { label: "Bill", plural: "Bills" });
    expect(state.step).toBe("sources");
    expect(state.actions).toEqual([]);
    expect(state.understanding).toBeNull();
    expect(canEnter(state, "review")).toBe(false);
  });

  it("a description with nothing to keep cannot be written into a product", () => {
    let state = goTo(started(), "sources");
    expect(canEnter(state, "analyzing")).toBe(false);
    state = addThing(state);
    state = updateThing(state, 0, { id: "rep", label: "Rep", plural: "Reps", people: true });
    expect(canEnter(state, "analyzing")).toBe(false);
    state = addThing(state);
    state = updateThing(state, 1, { id: "deal", label: "Deal", plural: "Deals" });
    expect(canEnter(state, "analyzing")).toBe(true);
  });

  it("turns plain user labels into safe internal keys", () => {
    expect(keyForName("Due date")).toBe("due_date");
    expect(keyForName("24 hour SLA")).toBe("item_24_hour_sla");
    let state = goTo(started(), "sources");
    state = addThing(state);
    state = updateThing(state, 0, { id: "", label: "Customer Tickets", plural: "Customer Tickets" });
    expect(canEnter(state, "analyzing")).toBe(true);
  });

  it("only one kind of record can be the people", () => {
    let state = described();
    state = addThing(state);
    state = updateThing(state, 1, { id: "rep", label: "Rep", plural: "Reps", people: true });
    state = updateThing(state, 0, { people: true });
    expect(state.things.filter((thing) => thing.people)).toHaveLength(1);
    expect(state.things[0].people).toBe(true);
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
