import { describe, expect, it } from "vitest";
import {
  PRODUCT_TEMPLATES, summariseAbilities, acceptUnderstanding, addSource, addThing, applyTemplate, approveGeneratedUnderstanding, canEnter,
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

describe("product templates", () => {
  it.each(PRODUCT_TEMPLATES.map((template) => [template.id]))("%s can be written into a product as it is", (id) => {
    const state = applyTemplate(goTo(started(), "sources"), id);
    expect(state.things.length).toBeGreaterThan(0);
    expect(state.things.filter((thing) => thing.people).length).toBeLessThanOrEqual(1);
    expect(canEnter(state, "analyzing")).toBe(true);
    for (const thing of state.things) {
      for (const field of thing.fields) {
        if (field.type === "enum") expect(field.values.split(",").filter((v) => v.trim()).length).toBeGreaterThan(1);
      }
    }
  });

  it("choosing a template replaces the description and invalidates what was written from it", () => {
    let state = acceptUnderstanding(analyzed());
    state = applyTemplate(state, "support-desk");
    expect(state.step).toBe("sources");
    expect(state.understanding).toBeNull();
    expect(state.things.map((thing) => thing.plural)).toEqual(["Tickets", "Customers", "Agents"]);
  });

  it("editing a template's records never changes the template itself", () => {
    const state = updateThing(applyTemplate(goTo(started(), "sources"), "sales-crm"), 0, { label: "Opportunity" });
    expect(state.things[0].label).toBe("Opportunity");
    expect(PRODUCT_TEMPLATES.find((template) => template.id === "sales-crm")!.things[0].label).toBe("Deal");
  });

  it("an unknown template changes nothing", () => {
    const state = described();
    expect(applyTemplate(state, "missing")).toBe(state);
  });
});

describe("what the assistant will be able to do", () => {
  it("says the everyday abilities once and keeps what is particular", () => {
    const canDo = ["Add a ticket.", "Add an agent.", "Change a ticket.", "Open one ticket.", "Open one agent.",
      "Open the list of tickets.", "Open the list of agents.", "Show every ticket one agent owns."];
    expect(summariseAbilities(canDo, ["Tickets", "Agents"])).toEqual([
      "Add, change and open tickets and agents, and show the list of each.",
      "Show every ticket one agent owns.",
    ]);
  });

  it("leaves a list it does not recognise as it is", () => {
    expect(summariseAbilities(["Refund an order."], ["Orders"])).toEqual(["Refund an order."]);
  });
});
