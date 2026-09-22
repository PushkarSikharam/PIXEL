export type DemoPage =
  | "dashboard"
  | "issues"
  | "issue_detail"
  | "projects"
  | "cycles"
  | "teams"
  | "integrations";

export type NavigableDemoPage = Exclude<DemoPage, "issue_detail">;

export type IntentTrace = {
  role?: string;
  team_size?: number;
  current_tool?: string;
  goal?: string;
  pain_point?: string;
  current_intent?: string;
  relevant_feature?: string;
  reason?: string;
  confidence?: number;
  status?: "active" | "interrupted" | "denied";
};

export type SessionUiState = {
  current_page: DemoPage;
  active_turn_id: number | null;
  highlighted_target?: string;
  selected_issue_id?: string;
  issue_filter_assignee?: string;
};

export type DemoTeamMember = {
  revision?: number;
  name: string;
  initials: string;
  role: string;
  load: number;
  email?: string;
  projectIds?: string[];
};

export type IssueFields = Omit<DemoIssue, "id" | "revision">;

export type DemoIssue = {
  revision?: number;
  id: string;
  title: string;
  priority: "Low" | "Medium" | "High";
  assignee: string;
  project: string;
  projectId?: string;
  status: string;
  cycle?: string;
  estimate?: string;
  label?: string;
  description?: string;
};

export type DemoProject = {
  revision?: number;
  id: string;
  name: string;
  description: string;
  progress: number;
  status: "Planned" | "Active" | "At risk" | "Completed";
  lead: string;
  team: string;
  targetDate: string;
};

export type DemoCycle = {
  revision?: number;
  id: string;
  name: string;
  projectId?: string;
  daysLeft: number;
  progress: number;
  completed: number;
  inProgress: number;
  remaining: number;
  focus: string[];
  status: "Planned" | "Active" | "Completed";
  team: string;
  startDate: string;
  endDate: string;
};

export type DemoWorkspaceScope = {
  revision?: number;
  id: string;
  name: string;
  description: string;
  allowedProjectIds: string[];
  allowedIssueProjects: string[];
};

export type DemoDataResponse = {
  workspaceScopes: DemoWorkspaceScope[];
  projects: DemoProject[];
  team: DemoTeamMember[];
  cycles: DemoCycle[];
  issues: DemoIssue[];
};

export type DemoActionType =
  | "OPEN_DASHBOARD"
  | "OPEN_ISSUES"
  | "OPEN_PROJECTS"
  | "OPEN_CYCLES"
  | "OPEN_TEAMS"
  | "OPEN_INTEGRATIONS"
  | "OPEN_SYSTEM_ARCHITECTURE"
  | "OPEN_DEMO_ISSUE"
  | "CREATE_DEMO_ISSUE"
  | "CREATE_DEMO_TEAM_MEMBER"
  | "UPDATE_DEMO_ISSUE"
  | "FILTER_ISSUES_BY_ASSIGNEE"
  | "HIGHLIGHT_ASSIGNMENT_CONTROL"
  | "HIGHLIGHT_CREATE_TICKET_BUTTON"
  | "HIGHLIGHT_ADD_MEMBER_BUTTON"
  | "HIGHLIGHT_CYCLE_PROGRESS"
  | "OPEN_GITHUB_SETUP"
  | "HIGHLIGHT_GITHUB_CARD"
  | "HIGHLIGHT_SLACK_CARD";

export type DemoAction =
  | { type: "OPEN_DASHBOARD"; payload?: Record<string, never> }
  | { type: "OPEN_ISSUES"; payload?: Record<string, never> }
  | { type: "OPEN_PROJECTS"; payload?: Record<string, never> }
  | { type: "OPEN_CYCLES"; payload?: Record<string, never> }
  | { type: "OPEN_TEAMS"; payload?: Record<string, never> }
  | { type: "OPEN_INTEGRATIONS"; payload?: Record<string, never> }
  | { type: "OPEN_SYSTEM_ARCHITECTURE"; payload?: Record<string, never> }
  | { type: "OPEN_DEMO_ISSUE"; payload: { issue_id: string } }
  // An assistant create carries only the fields its execution key binds; the server assigns the
  // ID when it commits. A form create carries the whole record.
  | { type: "CREATE_DEMO_ISSUE"; payload: DemoIssue | IssueFields }
  | { type: "CREATE_DEMO_TEAM_MEMBER"; payload: DemoTeamMember }
  | {
      type: "UPDATE_DEMO_ISSUE";
      payload: {
        issue_id: string;
        assignee?: string;
        priority?: DemoIssue["priority"];
        status?: string;
      };
    }
  | { type: "FILTER_ISSUES_BY_ASSIGNEE"; payload: { assignee: string } }
  | { type: "HIGHLIGHT_ASSIGNMENT_CONTROL"; payload: { issue_id?: string } }
  // The backend may prefill the ticket form with what the visitor said; the form still asks.
  | { type: "HIGHLIGHT_CREATE_TICKET_BUTTON"; payload?: { assignee?: string; priority?: DemoIssue["priority"]; title?: string } }
  | { type: "HIGHLIGHT_ADD_MEMBER_BUTTON"; payload?: { name?: string } }
  | { type: "HIGHLIGHT_CYCLE_PROGRESS"; payload?: Record<string, never> }
  | { type: "OPEN_GITHUB_SETUP"; payload?: Record<string, never> }
  | { type: "HIGHLIGHT_GITHUB_CARD"; payload?: Record<string, never> }
  | { type: "HIGHLIGHT_SLACK_CARD"; payload?: Record<string, never> };

export type UiEvent = {
  id: string;
  action_type: string;
  status: "executed" | "rejected";
  description: string;
  created_at: string;
};
