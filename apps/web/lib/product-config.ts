import type { DemoActionType, NavigableDemoPage } from "@/types/demo";

export type ProductConfig = {
  tenantId: string;
  id: string;
  name: string;
  docsPath: string;
  pages: Array<{
    id: NavigableDemoPage;
    label: string;
    shortcut: string;
  }>;
  allowedActions: DemoActionType[];
};

export const productConfig: ProductConfig = {
  // Server-owned identifiers for the public product demo, not a Product Definition ID.
  tenantId: "pixel-dev",
  id: "linear-demo",
  name: "Pixel",
  docsPath: "docs/product",
  pages: [
    { id: "dashboard", label: "Dashboard", shortcut: "D" },
    { id: "issues", label: "Issues", shortcut: "I" },
    { id: "projects", label: "Projects", shortcut: "P" },
    { id: "cycles", label: "Cycles", shortcut: "C" },
    { id: "teams", label: "Teams", shortcut: "T" },
    { id: "integrations", label: "Integrations", shortcut: "G" }
  ],
  allowedActions: [
    "OPEN_DASHBOARD",
    "OPEN_ISSUES",
    "OPEN_PROJECTS",
    "OPEN_CYCLES",
    "OPEN_TEAMS",
    "OPEN_INTEGRATIONS",
    "OPEN_SYSTEM_ARCHITECTURE",
    "OPEN_DEMO_ISSUE",
    "CREATE_DEMO_ISSUE",
    "UPDATE_DEMO_ISSUE",
    "FILTER_ISSUES_BY_ASSIGNEE",
    "HIGHLIGHT_ASSIGNMENT_CONTROL",
    "HIGHLIGHT_CREATE_TICKET_BUTTON",
    "HIGHLIGHT_ADD_MEMBER_BUTTON",
    "HIGHLIGHT_CYCLE_PROGRESS",
    "OPEN_GITHUB_SETUP",
    "HIGHLIGHT_GITHUB_CARD",
    "HIGHLIGHT_SLACK_CARD"
  ]
};
