import { describe, expect, it } from "vitest";

import { cycleDaysLeft, cycleProgress } from "@/adapters/linear_simplified/cycle-health";
import { demoCycle, demoCycles } from "@/lib/demo-data";
import type { DemoCycle } from "@/types/demo";

const cycle = (fields: Partial<DemoCycle>): DemoCycle => ({ ...demoCycle, ...fields });

describe("cycle health is counted, never taken on trust", () => {
  it("reports the share of planned work that is done", () => {
    expect(cycleProgress(cycle({ completed: 18, inProgress: 9, remaining: 11 }))).toBe(47);
    expect(cycleProgress(cycle({ completed: 0, inProgress: 0, remaining: 0 }))).toBe(0);
    expect(cycleProgress(cycle({ completed: 4, inProgress: 0, remaining: 0 }))).toBe(100);
  });

  it("ignores a stored progress number that disagrees with the work", () => {
    // The live dashboard showed 68% beside 18 of 38 items done.
    expect(cycleProgress(cycle({ progress: 68, completed: 18, inProgress: 9, remaining: 11 }))).toBe(47);
  });

  it("counts the days left from the end date, and never below zero", () => {
    const ended = cycle({ endDate: "2026-09-08", daysLeft: 8 });
    expect(cycleDaysLeft(ended, new Date("2026-09-22T12:00:00Z"))).toBe(0);
    expect(cycleDaysLeft(ended, new Date("2026-09-04T12:00:00Z"))).toBe(4);
    expect(cycleDaysLeft(ended, new Date("2026-09-04T23:30:00Z"))).toBe(4);
  });

  it("keeps every seeded cycle consistent with what it claims", () => {
    for (const seeded of demoCycles) {
      expect(cycleProgress(seeded)).toBe(seeded.progress);
      if (seeded.status === "Active") {
        expect(cycleDaysLeft(seeded)).toBe(seeded.daysLeft);
      }
    }
  });
});
