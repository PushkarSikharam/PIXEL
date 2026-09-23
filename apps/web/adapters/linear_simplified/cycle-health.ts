import type { DemoCycle } from "@/types/demo";

/**
 * A cycle's progress and the time left in it are counted from the cycle itself.
 *
 * Both were stored numbers that nothing kept in step with the work or the calendar, so a screen
 * could show 68% beside 18 of 38 items done, or "8 days left" on a cycle that ended a fortnight
 * ago. Counting them here means every screen shows the same number and none of them can disagree.
 */
export function cycleProgress(cycle: DemoCycle): number {
  const planned = cycle.completed + cycle.inProgress + cycle.remaining;
  return planned > 0 ? Math.round((cycle.completed / planned) * 100) : 0;
}

export function cycleDaysLeft(cycle: DemoCycle, today: Date = new Date()): number {
  const end = Date.parse(`${cycle.endDate}T00:00:00Z`);
  if (Number.isNaN(end)) {
    return Math.max(0, cycle.daysLeft);
  }
  // Whole days between two calendar dates, so the count does not change as the clock moves
  // through the day and never disagrees with the window the cycle was seeded with.
  const start = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  return Math.max(0, Math.round((end - start) / (1000 * 60 * 60 * 24)));
}
