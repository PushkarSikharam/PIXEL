import demoIssuesJson from "../../../packages/shared/demo-data/issues.json";
import type { DemoCycle, DemoIssue, DemoProject, DemoWorkspaceScope } from "@/types/demo";

export const demoIssues = demoIssuesJson as DemoIssue[];

export const demoWorkspaceScopes: DemoWorkspaceScope[] = [
  {
    id: "workspace-product-eng",
    name: "Product Engineering Workspace",
    description: "Scoped to Integrations and Issue Triage project work.",
    allowedProjectIds: ["PRJ-101", "PRJ-102"],
    allowedIssueProjects: ["Integrations", "Issue Triage"]
  },
  {
    id: "workspace-platform",
    name: "Platform Workspace",
    description: "Scoped to planning insights, migration work, and platform reliability.",
    allowedProjectIds: ["PRJ-103", "PRJ-104"],
    allowedIssueProjects: ["Planning", "Migration"]
  }
];

export const demoWorkspaceScope = demoWorkspaceScopes[0];

export const demoProjects: DemoProject[] = [
  {
    id: "PRJ-101",
    name: "GitHub Integration Hardening",
    description: "Improve PR sync reliability, webhook recovery, and commit-to-issue visibility.",
    progress: 68,
    status: "Active",
    lead: "Maya Chen",
    team: "Product Engineering",
    targetDate: "2026-09-28"
  },
  {
    id: "PRJ-102",
    name: "Issue Triage Workflow",
    description: "Reduce duplicate reports and speed up assignment, priority, and status updates.",
    progress: 54,
    status: "Active",
    lead: "Noah Patel",
    team: "Product Engineering",
    targetDate: "2026-10-12"
  },
  {
    id: "PRJ-103",
    name: "Cycle Planning Insights",
    description: "Forecast sprint capacity, burndown risks, and planning accuracy.",
    progress: 42,
    status: "At risk",
    lead: "Avery Brooks",
    team: "Platform",
    targetDate: "2026-09-18"
  },
  {
    id: "PRJ-104",
    name: "Workspace Migration",
    description: "Move archived project history into the new operating model.",
    progress: 31,
    status: "Planned",
    lead: "Iris Morgan",
    team: "Platform",
    targetDate: "2026-11-04"
  }
];

// The placeholder shown before the API answers. Its dates are anchored to today for the same
// reason the seeded ones are: a cycle called active must still be running when someone reads it.
function anchored(startsIn: number, endsIn: number): { startDate: string; endDate: string } {
  const day = (offset: number) => {
    const when = new Date();
    when.setDate(when.getDate() + offset);
    return when.toISOString().slice(0, 10);
  };
  return { startDate: day(startsIn), endDate: day(endsIn) };
}

export const demoCycle: DemoCycle = {
  id: "CYC-14",
  name: "Product Engineering Cycle 14",
  projectId: "PRJ-101",
  daysLeft: 8,
  progress: 47,
  completed: 18,
  inProgress: 9,
  remaining: 11,
  focus: ["Bug triage", "Cycle planning", "GitHub sync", "Assignment flow"],
  status: "Active",
  team: "Product Engineering",
  ...anchored(-7, 8)
};

export const demoCycles: DemoCycle[] = [
  demoCycle,
  {
    id: "CYC-21",
    name: "Platform Cycle 21",
    projectId: "PRJ-103",
    daysLeft: 6,
    progress: 32,
    completed: 7,
    inProgress: 6,
    remaining: 9,
    focus: ["Capacity forecast", "Migration readiness", "Planning accuracy"],
    status: "Active",
    team: "Platform",
    ...anchored(-9, 6)
  }
];

export const demoTeam = [
  {
    name: "Maya Chen",
    initials: "MC",
    role: "Frontend Lead",
    load: 84,
    projectIds: ["PRJ-101"]
  },
  {
    name: "Noah Patel",
    initials: "NP",
    role: "Product Engineer",
    load: 71,
    projectIds: ["PRJ-102"]
  },
  {
    name: "Avery Brooks",
    initials: "AB",
    role: "Engineering Manager",
    load: 63,
    projectIds: ["PRJ-103"]
  },
  {
    name: "Iris Morgan",
    initials: "IM",
    role: "Platform Engineer",
    load: 77,
    projectIds: ["PRJ-104"]
  }
];

export const integrations = [
  {
    name: "GitHub",
    description: "Sync pull requests, commits, and issue references.",
    connected: true
  },
  {
    name: "Slack",
    description: "Create issues from messages and receive project updates.",
    connected: true
  },
  {
    name: "Jira Import",
    description: "Bring active Jira projects into a simplified workspace.",
    connected: true
  },
  {
    name: "Sentry",
    description: "Create bug reports from production exceptions.",
    connected: false
  }
];
