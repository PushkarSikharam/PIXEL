"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { actionByPage, executeDemoAction } from "@/lib/action-executor";
import { cancelAgentTurn, type AgentTurnResponse, sendAgentTurn } from "@/lib/agent-api";
import {
  demoIssues,
  demoProjects,
  demoCycle,
  demoCycles,
  demoTeam,
  demoWorkspaceScope,
  demoWorkspaceScopes,
  integrations
} from "@/lib/demo-data";
import { cycleDaysLeft, cycleProgress } from "@/adapters/linear_simplified/cycle-health";
import {
  ensureDemoLogin,
  applyKeyedChange,
  loadDemoData,
  resetPrivateDemo,
  RateLimitedError,
  RecordSaveError,
  type ExecutionEnvelope,
  type ExecutionReceipt,
  saveStoredCycle,
  saveStoredIssue,
  saveStoredProject,
  saveStoredTeamMember,
  updateStoredIssue
} from "@/lib/product-data-api";
import { productConfig } from "@/lib/product-config";
import { checkAgentService, type AgentServiceStatus } from "@/lib/service-health";
import { useRecordSubmit, type CreateRecord } from "@/lib/use-record-submit";
import { HybridVoiceEngine, VoiceEngineMode, VoiceEngineStatus } from "@/lib/hybrid-voice-engine";
import type { SpectrumData } from "@/lib/voice-analyzer";
import { isEchoOfAgent } from "@/lib/voice-echo";
import { voiceModeLabel } from "@/lib/voice-labels";
import type {
  DemoAction,
  DemoCycle,
  DemoIssue,
  DemoPage,
  DemoProject,
  DemoTeamMember,
  DemoWorkspaceScope,
  IntentTrace,
  SessionUiState,
  UiEvent
} from "@/types/demo";

const navItems = productConfig.pages;

const initialTrace: IntentTrace = {
  role: "Unknown",
  current_tool: "Not detected",
  goal: "Waiting for visitor",
  pain_point: "None yet",
  current_intent: "Discovery",
  relevant_feature: "Dashboard",
  reason: "The demo is ready for the visitor's first request.",
  confidence: 0,
  status: "active"
};

type TranscriptMessage = {
  speaker: "Agent" | "Visitor";
  text: string;
  // A note the page adds about the conversation itself, never speech: the assistant's words are
  // always the backend's (5c plan, section 2.3).
  note?: string;
};

type SessionSummary = AgentTurnResponse["session_summary"];
type InputMode = "text" | "voice";
type VoiceStatus = "Idle" | "Listening" | "Processing" | "Preparing" | "Speaking" | "Unavailable";
const DEFAULT_VOICE_SILENCE_TIMEOUT_MS = 3500;

type DraftPrefill = {
  issue?: {
    assignee?: string;
    priority?: DemoIssue["priority"];
    title?: string;
  };
  teamMember?: {
    name?: string;
  };
  issueUpdate?: {
    issueId: string;
    assignee?: string;
    priority?: DemoIssue["priority"];
    status?: string;
  };
};

type SpeechRecognitionResultLike = {
  readonly isFinal: boolean;
  readonly 0: {
    readonly transcript: string;
  };
};

type SpeechRecognitionEventLike = {
  readonly resultIndex: number;
  readonly results: {
    readonly length: number;
    readonly [index: number]: SpeechRecognitionResultLike;
  };
};

type BrowserSpeechRecognition = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onend: (() => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onstart: (() => void) | null;
  abort: () => void;
  start: () => void;
  stop: () => void;
};

type SpeechRecognitionConstructor = new () => BrowserSpeechRecognition;

type SpeechRecognitionWindow = Window &
  typeof globalThis & {
    SpeechRecognition?: SpeechRecognitionConstructor;
    webkitSpeechRecognition?: SpeechRecognitionConstructor;
    __demoVoiceSilenceTimeoutMs?: number;
    __emitVoiceTranscript?: (transcript: string) => void;
    __spokenAgentReplies?: string[];
  };

const initialTranscript: TranscriptMessage[] = [
  {
    speaker: "Agent",
    text: "Welcome to Pixel. I'm Edith, your guide to planning work, tracking tickets, and connecting your team's tools. What brought you to check us out today?"
  }
];

const initialSessionSummary: SessionSummary = {
  interests: [],
  pain_points: [],
  last_person: null,
  last_feature: null,
  clarification_pending: null
};

const demoPrompts = [
  "Back to GitHub integrations",
  "Show me issue assignment",
  "What can Pixel do with Slack?"
];

const demoPathPrompts = [
  "Show sprint planning",
  "Open Maya's ticket",
  "Assign it to Noah",
  "Create a ticket for Lucifer",
  "Open Salesforce"
];

const connectedIntegrationCount = integrations.filter((integration) => integration.connected).length;
type TurnStatus = "Ready" | "Thinking" | "Interrupted" | "Action blocked" | "Service unavailable";
const issueStatuses = ["Todo", "In progress", "Review", "Done"] as const;
const priorities = ["Low", "Medium", "High"] as const;

export default function Home() {
  const router = useRouter();
  const [experience, setExperience] = useState<"system" | "demo">("system");
  const [sessionId, setSessionId] = useState(() => crypto.randomUUID());
  const activeTurnIdRef = useRef<number | null>(null);
  const nextTurnIdRef = useRef(1);
  const [uiState, setUiState] = useState<SessionUiState>({
    current_page: "dashboard",
    active_turn_id: null
  });
  const [uiEvents, setUiEvents] = useState<UiEvent[]>([]);
  const [intentTrace, setIntentTrace] = useState<IntentTrace>(initialTrace);
  const [sessionSummary, setSessionSummary] = useState<SessionSummary>(initialSessionSummary);
  const [messages, setMessages] = useState<TranscriptMessage[]>(initialTranscript);
  const [issues, setIssues] = useState<DemoIssue[]>(demoIssues);
  const [projects, setProjects] = useState<DemoProject[]>(demoProjects);
  const [cycles, setCycles] = useState<DemoCycle[]>(demoCycles);
  const [team, setTeam] = useState<DemoTeamMember[]>(demoTeam);
  const [workspaceScopes, setWorkspaceScopes] = useState<DemoWorkspaceScope[]>(demoWorkspaceScopes);
  const [workspaceScope, setWorkspaceScope] = useState<DemoWorkspaceScope>(demoWorkspaceScope);
  const workspaceScopeRef = useRef<DemoWorkspaceScope>(demoWorkspaceScope);
  const [draftPrefill, setDraftPrefill] = useState<DraftPrefill>({});
  const [isAssistantCollapsed, setIsAssistantCollapsed] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [turnStatus, setTurnStatus] = useState<TurnStatus>("Ready");
  const currentPage = uiState.current_page;
  const activeNavItem = useMemo(
    () => navItems.find((item) => item.id === currentPage),
    [currentPage]
  );
  const scopedProjects = useMemo(
    () => projects.filter((project) => workspaceScope.allowedProjectIds.includes(project.id)),
    [projects, workspaceScope]
  );
  const scopedIssues = useMemo(
    () => filterIssuesByScope(issues, workspaceScope),
    [issues, workspaceScope]
  );
  const scopedTeam = useMemo(
    () => filterTeamByScope(team, workspaceScope, scopedIssues),
    [team, workspaceScope, scopedIssues]
  );
  const scopedCycles = useMemo(
    () => cycles.filter((cycle) => isCycleInScope(cycle, workspaceScope)),
    [cycles, workspaceScope]
  );
  const latestEvent = uiEvents[0];
  const [dismissedEventId, setDismissedEventId] = useState<string | null>(null);
  const [dataError, setDataError] = useState<string | null>(null);
  const [serviceError, setServiceError] = useState<string | null>(null);
  const [agentServiceStatus, setAgentServiceStatus] = useState<AgentServiceStatus>("checking");

  useEffect(() => {
    if (!latestEvent) return;
    setDismissedEventId(null);
    const timeout = window.setTimeout(() => setDismissedEventId(latestEvent.id), 3200);
    return () => window.clearTimeout(timeout);
  }, [latestEvent]);

  async function loadStoredData(isCurrent: () => boolean = () => true) {
    setServiceError(null);
    setAgentServiceStatus("checking");
    const isHealthy = await checkAgentService();
    if (!isCurrent()) return;
    if (!isHealthy) {
      setAgentServiceStatus("unavailable");
      setTurnStatus("Service unavailable");
      setServiceError("Chat, voice, and saved changes are temporarily disabled.");
      return;
    }

    try {
      await ensureDemoLogin();
      const demoData = await loadDemoData();
      if (!isCurrent()) return;
      setIssues(demoData.issues);
      setProjects(demoData.projects);
      setCycles(demoData.cycles);
      setTeam(demoData.team);
      setWorkspaceScopes(demoData.workspaceScopes);
      setWorkspaceScope((currentScope) => {
        const nextScope = demoData.workspaceScopes.find((scope) => scope.id === currentScope.id)
          ?? demoData.workspaceScopes[0]
          ?? demoWorkspaceScope;
        workspaceScopeRef.current = nextScope;
        return nextScope;
      });
      setAgentServiceStatus("ready");
      setTurnStatus("Ready");
      setServiceError(null);
    } catch (error) {
      if (!isCurrent()) return;
      setAgentServiceStatus("unavailable");
      setTurnStatus("Service unavailable");
      setServiceError(error instanceof RateLimitedError
        ? "Too many visitors are starting sessions right now. Please wait a moment, then reconnect."
        : "The secure demo session could not start. Chat, voice, and saved changes are temporarily disabled.");
    }
  }

  useEffect(() => {
    let isMounted = true;
    void loadStoredData(() => isMounted);
    return () => {
      isMounted = false;
    };
  }, []);

  function switchWorkspaceScope(scopeId: string) {
    const nextScope = workspaceScopes.find((scope) => scope.id === scopeId);
    if (!nextScope || nextScope.id === workspaceScope.id) return;
    const activeTurnId = activeTurnIdRef.current;
    if (activeTurnId !== null) {
      void cancelAgentTurn({ sessionId, turnId: activeTurnId }).catch(() => undefined);
      activeTurnIdRef.current = null;
    }

    workspaceScopeRef.current = nextScope;
    setWorkspaceScope(nextScope);
    setDraftPrefill({});
    setIntentTrace({
      ...initialTrace,
      relevant_feature: "Dashboard",
      reason: `${nextScope.name} is now the active project scope.`
    });
    setSessionSummary((currentSummary) => ({
      ...currentSummary,
      last_person: null,
      last_feature: nextScope.name,
      clarification_pending: null
    }));
    setUiState((currentState) => ({
      ...currentState,
      current_page: currentState.current_page === "issue_detail" ? "dashboard" : currentState.current_page,
      highlighted_target: undefined,
      selected_issue_id: undefined,
      issue_filter_assignee: undefined
    }));
    setTurnStatus("Ready");
    setIsSending(false);
    recordUiEvent({
      id: crypto.randomUUID(),
      action_type: "SWITCH_WORKSPACE_SCOPE",
      status: "executed",
      description: `Switched active workspace to ${nextScope.name}.`,
      created_at: new Date().toISOString()
    });
  }

  function applyAction(action: DemoAction) {
    if (action.type === "OPEN_SYSTEM_ARCHITECTURE") {
      recordUiEvent({
        id: crypto.randomUUID(),
        action_type: action.type,
        status: "executed",
        description: "Opened the system architecture view.",
        created_at: new Date().toISOString()
      });
      router.push("/architecture");
      return;
    }

    const nextIssues =
        action.type === "CREATE_DEMO_ISSUE" && "id" in action.payload
          ? upsertIssue(issues, withScopedIssue(action.payload, scopedProjects, workspaceScope))
          : action.type === "UPDATE_DEMO_ISSUE"
            ? applyIssueUpdate(issues, action.payload)
          : issues;
    const scopedNextIssues = filterIssuesByScope(nextIssues, workspaceScope);
    const result = executeDemoAction(uiState, action, { issues: scopedNextIssues });
      if (
        (action.type === "CREATE_DEMO_ISSUE" || action.type === "UPDATE_DEMO_ISSUE")
        && result.event.status === "executed"
      ) {
        setIssues(nextIssues);
      }
      const visibleEvent = savedWorkspaceEvent(result.event, workspaceScope.name, action, nextIssues);
      setUiEvents((events) => [visibleEvent, ...events].slice(0, 6));
    setUiState(result.nextState);
  }

  async function runAction(action: DemoAction, envelope?: ExecutionEnvelope | null): Promise<ExecutionReceipt | null> {
    if (action.type === "CREATE_DEMO_ISSUE") {
      if (envelope) {
        const receipt = await applyKeyedChange(action, envelope);
        if (receipt?.record) {
          const saved = receipt.record;
          setIssues((currentIssues) => upsertIssue(currentIssues, saved));
          applyAction({ ...action, payload: saved });
        }
        return receipt;
      }
      // Without a key only a complete record from a form is saved; assistant fields never are.
      if (!("id" in action.payload)) throw new RecordSaveError("This change was not found, so nothing was applied.");
      const saved = await saveStoredIssue(
        withScopedIssue(action.payload, scopedProjects, workspaceScope), crypto.randomUUID()
      );
      applyAction({ ...action, payload: saved });
      return null;
    }
    if (action.type === "UPDATE_DEMO_ISSUE") {
      if (envelope) {
        const receipt = await applyKeyedChange(action, envelope);
        if (receipt?.record) {
          const saved = receipt.record;
          applyAction(action);
          setIssues((currentIssues) => upsertIssue(currentIssues, saved));
        }
        return receipt;
      }
      const changed = applyIssueUpdate(issues, action.payload).find((issue) => issue.id === action.payload.issue_id);
      if (!changed) throw new Error("This ticket is no longer available.");
      const saved = await updateStoredIssue(changed);
      applyAction(action);
      // The server increments the optimistic revision. Keep it in browser state so a second
      // natural-language edit does not submit the stale version that preceded this write.
      setIssues((currentIssues) => upsertIssue(currentIssues, saved));
      return null;
    }
    applyAction(action);
    return null;
  }

  function recordUiEvent(event: UiEvent) {
    setUiEvents((events) => [event, ...events].slice(0, 6));
  }

  function resetDemoSession() {
    setDataError(null);
    // Reset restores only this visitor's private seed and rotates its generation. Clear nothing
    // until that transaction commits; a failed reset keeps the current conversation and data.
    void resetPrivateDemo()
      .then((demoData) => {
        const activeTurnId = activeTurnIdRef.current;
        if (activeTurnId !== null) {
          void cancelAgentTurn({ sessionId, turnId: activeTurnId }).catch(() => undefined);
        }

        activeTurnIdRef.current = null;
        nextTurnIdRef.current = 1;
        setSessionId(crypto.randomUUID());
        setUiState({
          current_page: "dashboard",
          active_turn_id: null
        });
        setUiEvents([]);
        setIntentTrace(initialTrace);
        setSessionSummary(initialSessionSummary);
        setMessages(initialTranscript);
        setIssues(demoData.issues);
        setProjects(demoData.projects);
        setCycles(demoData.cycles);
        setTeam(demoData.team);
        setWorkspaceScopes(demoData.workspaceScopes);
        // A fresh session starts where a fresh page load does: in the default workspace, when this
        // visitor can see it. The first listed scope is only an order, not a starting point.
        const nextScope = demoData.workspaceScopes.find((scope) => scope.id === demoWorkspaceScope.id)
          ?? demoData.workspaceScopes[0]
          ?? demoWorkspaceScope;
        workspaceScopeRef.current = nextScope;
        setWorkspaceScope(nextScope);
        setDraftPrefill({});
        setIsSending(false);
        setTurnStatus("Ready");
      })
      .catch(() => {
        setDataError("Reset failed. Nothing was changed: your current session is unchanged.");
      });
  }

  function restartConversation() {
    const activeTurnId = activeTurnIdRef.current;
    if (activeTurnId !== null) {
      void cancelAgentTurn({ sessionId, turnId: activeTurnId }).catch(() => undefined);
    }
    activeTurnIdRef.current = null;
    nextTurnIdRef.current = 1;
    setSessionId(crypto.randomUUID());
    setUiState({ current_page: "dashboard", active_turn_id: null });
    setUiEvents([]);
    setIntentTrace(initialTrace);
    setSessionSummary(initialSessionSummary);
    setMessages(initialTranscript);
    setDraftPrefill({});
    setIsSending(false);
    setTurnStatus("Ready");
  }

  async function createIssue(issue: DemoIssue, requestKey: string) {
    const scopedIssue = await saveStoredIssue(withScopedIssue(issue, scopedProjects, workspaceScope), requestKey);
    const nextIssues = upsertIssue(issues, scopedIssue);
    setIssues(nextIssues);
    setDraftPrefill((currentPrefill) => ({
      ...currentPrefill,
      issue: undefined,
      issueUpdate: undefined
    }));
    applyAction({ type: "CREATE_DEMO_ISSUE", payload: scopedIssue });
  }

  async function createTeamMember(member: DemoTeamMember, requestKey: string) {
    const pendingIssueDraft = draftPrefill.issue;
    const pendingIssueUpdate = draftPrefill.issueUpdate;
    const scopedMember = await saveStoredTeamMember({
      ...member,
      projectIds: member.projectIds ?? [...workspaceScope.allowedProjectIds]
    }, workspaceScope.id, requestKey);
    setTeam((currentTeam) => [...currentTeam, scopedMember]);
    setDraftPrefill((currentPrefill) => ({
      ...currentPrefill,
      teamMember: undefined,
      issue: currentPrefill.issue
        ? {
          ...currentPrefill.issue,
            assignee: scopedMember.name
          }
        : undefined
    }));

    if (pendingIssueUpdate) {
      const issue = issues.find((item) => item.id === pendingIssueUpdate.issueId);
      if (issue) {
        const updatedIssue = {
          ...issue,
          assignee: pendingIssueUpdate.assignee ?? scopedMember.name,
          priority: pendingIssueUpdate.priority ?? issue.priority,
          status: pendingIssueUpdate.status ?? issue.status
        };
        await updateIssue(updatedIssue);
        setDraftPrefill({});
        setUiState((currentState) => ({
          ...currentState,
          current_page: "issue_detail",
          selected_issue_id: issue.id,
          highlighted_target: "updated_issue",
          issue_filter_assignee: undefined
        }));
        setMessages((currentMessages) => [
          ...currentMessages,
          {
            speaker: "Agent",
            text: `${scopedMember.name} has been added. I assigned ${issue.id} to ${scopedMember.name}.`
          }
        ]);
      }
    } else if (pendingIssueDraft) {
      setUiState((currentState) => ({
        ...currentState,
        current_page: "issues",
        highlighted_target: "create_ticket_button",
        issue_filter_assignee: undefined
      }));
      setMessages((currentMessages) => [
        ...currentMessages,
        {
          speaker: "Agent",
          text: `${scopedMember.name} has been added. I'll open the ticket form with ${scopedMember.name} selected.`
        }
      ]);
    }
    recordUiEvent({
      id: crypto.randomUUID(),
      action_type: "CREATE_DEMO_TEAM_MEMBER",
      status: "executed",
      description: `Saved to ${workspaceScope.name}: added ${scopedMember.name} (${scopedMember.role}).`,
      created_at: new Date().toISOString()
    });
  }

  async function updateIssue(updatedIssue: DemoIssue) {
    updatedIssue = await updateStoredIssue(updatedIssue);
    setIssues((currentIssues) =>
      currentIssues.map((item) => (item.id === updatedIssue.id ? updatedIssue : item))
    );
    recordUiEvent({
      id: crypto.randomUUID(),
      action_type: "UPDATE_DEMO_ISSUE",
      status: "executed",
      description: `Saved to ${workspaceScope.name}: updated ${updatedIssue.id} (Assignee: ${updatedIssue.assignee}, Priority: ${updatedIssue.priority}, Status: ${updatedIssue.status}).`,
      created_at: new Date().toISOString()
    });
  }

  async function createProject(project: DemoProject, requestKey: string) {
    project = await saveStoredProject(project, workspaceScope.id, requestKey);
    setProjects((currentProjects) => [project, ...currentProjects]);
    const nextScope = addProjectToScope(workspaceScope, project);
    workspaceScopeRef.current = nextScope;
    setWorkspaceScope(nextScope);
    setWorkspaceScopes((currentScopes) =>
      currentScopes.map((scope) => (scope.id === workspaceScope.id ? nextScope : scope))
    );
    setUiState((currentState) => ({
      ...currentState,
      current_page: "projects",
      highlighted_target: `project_${project.id}`,
      issue_filter_assignee: undefined
    }));
    recordUiEvent({
      id: crypto.randomUUID(),
      action_type: "CREATE_DEMO_PROJECT",
      status: "executed",
      description: `Saved to ${workspaceScope.name}: created project ${project.name}.`,
      created_at: new Date().toISOString()
    });
  }

  async function createCycle(cycle: DemoCycle, requestKey: string) {
    const scopedCycle = await saveStoredCycle({
      ...cycle,
      projectId: cycle.projectId ?? workspaceScope.allowedProjectIds[0]
    }, requestKey);
    setCycles((currentCycles) => [scopedCycle, ...currentCycles]);
    setUiState((currentState) => ({
      ...currentState,
      current_page: "cycles",
      highlighted_target: `cycle_${scopedCycle.id}`,
      issue_filter_assignee: undefined
    }));
    recordUiEvent({
      id: crypto.randomUUID(),
      action_type: "CREATE_DEMO_CYCLE",
      status: "executed",
      description: `Saved to ${workspaceScope.name}: created cycle ${scopedCycle.name}.`,
      created_at: new Date().toISOString()
    });
  }

  async function sendMessage(message: string, inputMode: InputMode = "text") {
    const trimmedMessage = message.trim();
    if (!trimmedMessage || agentServiceStatus !== "ready") return null;
    if (!(await checkAgentService())) {
      setAgentServiceStatus("unavailable");
      setTurnStatus("Service unavailable");
      setServiceError("The demo service disconnected. Reconnect before continuing.");
      return null;
    }

    const previousTurnId = activeTurnIdRef.current;
    if (previousTurnId !== null) {
      void cancelAgentTurn({ sessionId, turnId: previousTurnId }).catch(() => undefined);
      activeTurnIdRef.current = null;
      setIsSending(false);
      // The unfinished reply is dropped on purpose. The page notes that on the message it
      // answered, so the visitor is not left waiting for an answer that will never arrive.
      setMessages((currentMessages) => {
        const last = currentMessages.length - 1;
        if (last < 0 || currentMessages[last].speaker !== "Visitor") return currentMessages;
        const noted = [...currentMessages];
        noted[last] = { ...noted[last], note: "Reply stopped when you sent a new message." };
        return noted;
      });
      setUiState((currentState) => ({
        ...currentState,
        active_turn_id: null
      }));
    }

    const turnId = nextTurnIdRef.current;
    nextTurnIdRef.current += 1;
    activeTurnIdRef.current = turnId;
    setIsSending(true);
    setTurnStatus("Thinking");
    setUiState((currentState) => ({
      ...currentState,
      active_turn_id: turnId
    }));
    setMessages((currentMessages) => [
      ...currentMessages,
      { speaker: "Visitor", text: trimmedMessage }
    ]);

    try {
      const result = await sendAgentTurn({
        sessionId,
        turnId,
        productId: productConfig.id,
        message: trimmedMessage,
        inputMode,
        currentPage,
        workspaceScopeId: workspaceScopeRef.current.id,
        selectedIssueId: uiState.selected_issue_id
      });

      if (activeTurnIdRef.current !== turnId) {
        return null;
      }

      if (result.status === "stale" || result.status === "cancelled") {
        setTurnStatus("Interrupted");
        setIntentTrace(result.intent_trace);
        setSessionSummary(result.session_summary);
        return null;
      }

      setIntentTrace(result.intent_trace);
      setSessionSummary(result.session_summary);
      const validatedAction = result.validated_action;
      if (validatedAction) {
        if (validatedAction.type === "HIGHLIGHT_ADD_MEMBER_BUTTON") {
          const requestedName = validatedAction.payload?.name;
          setDraftPrefill((currentPrefill) => ({
            ...currentPrefill,
            teamMember: {
              name: requestedName
            },
            issue: asksForTicketCreation(normalizeText(trimmedMessage)) && requestedName
              ? {
                assignee: requestedName,
                priority: extractPriority(normalizeText(trimmedMessage)),
                title: extractIssueTitle(trimmedMessage)
              }
              : currentPrefill.issue
          }));
        }
        if (validatedAction.type === "HIGHLIGHT_CREATE_TICKET_BUTTON") {
          const { assignee, priority, title } = validatedAction.payload ?? {};
          if (assignee || priority || title) {
            setDraftPrefill((currentPrefill) => ({ ...currentPrefill, issue: { assignee, priority, title } }));
          }
        }
        const mutation =
          validatedAction.type === "CREATE_DEMO_ISSUE" || validatedAction.type === "UPDATE_DEMO_ISSUE";
        if (mutation && !result.execution) {
          throw new RecordSaveError("Edith could not verify this change, so nothing was applied.");
        }
        const receipt = await runAction(validatedAction, result.execution);
        if (receipt) {
          setMessages((currentMessages) => [
            ...currentMessages,
            { speaker: "Agent", text: receipt.speech }
          ]);
          setTurnStatus(receipt.outcome === "executed" ? "Ready" : "Action blocked");
          return result;
        }
        if (mutation) {
          throw new RecordSaveError("This change was not found, so nothing was applied.");
        }
      } else if (result.proposed_action) {
        recordUiEvent({
          id: crypto.randomUUID(),
          action_type: result.proposed_action.type,
          status: "rejected",
          description: result.intent_trace.reason ?? "The backend did not validate this action.",
          created_at: new Date().toISOString()
        });
      }

      if (activeTurnIdRef.current !== turnId) return null;
      setMessages((currentMessages) => [
        ...currentMessages,
        { speaker: "Agent", text: result.speech }
      ]);
      setTurnStatus(result.status === "denied" ? "Action blocked" : "Ready");
      return result;
    } catch (error) {
      if (activeTurnIdRef.current !== turnId) {
        return null;
      }

      // Too many requests: the service is fine and nothing ran, so say so and stay connected.
      if (error instanceof RateLimitedError) {
        setTurnStatus("Ready");
        setMessages((currentMessages) => [
          ...currentMessages,
          { speaker: "Agent", text: error.message }
        ]);
        recordUiEvent({
          id: crypto.randomUUID(),
          action_type: "AGENT_SERVICE",
          status: "rejected",
          description: "Edith paused because requests arrived too quickly. Nothing was changed.",
          created_at: new Date().toISOString()
        });
        return null;
      }

      // A failed record write reports why it failed; anything else is a transport failure.
      const saveMessage = error instanceof RecordSaveError ? error.message : null;
      if (!saveMessage) {
        setAgentServiceStatus("unavailable");
        setTurnStatus("Service unavailable");
        setServiceError("The demo service disconnected. Reconnect before continuing.");
        recordUiEvent({
          id: crypto.randomUUID(),
          action_type: "AGENT_SERVICE",
          status: "rejected",
          description: "Edith lost the secure demo service connection.",
          created_at: new Date().toISOString()
        });
        return null;
      }
      setTurnStatus("Action blocked");
      setIntentTrace((currentTrace) => ({
        ...currentTrace,
        status: "denied",
        reason: saveMessage ?? "The frontend could not reach the backend turn API."
      }));
      setMessages((currentMessages) => [
        ...currentMessages,
        {
          speaker: "Agent",
          text: saveMessage
        }
      ]);
      recordUiEvent({
        id: crypto.randomUUID(),
        action_type: "AGENT_SERVICE",
        status: "rejected",
        description: "Edith could not save the requested change.",
        created_at: new Date().toISOString()
      });
      return null;
    } finally {
      if (activeTurnIdRef.current === turnId) {
        activeTurnIdRef.current = null;
        setIsSending(false);
        setUiState((currentState) => ({
          ...currentState,
          active_turn_id: null
        }));
      }
    }
  }

  if (experience === "system") {
    return <PixelSystemHome onEnterDemo={() => setExperience("demo")} />;
  }

  return (
    <main className={isAssistantCollapsed ? "app-shell assistant-collapsed" : "app-shell"}>
      {dataError && (
        <div className="data-error-banner" data-testid="data-error" role="alert">
          <span>{dataError}</span>
          <button className="secondary-button compact" onClick={() => void loadStoredData()} type="button">
            Retry
          </button>
        </div>
      )}
      {latestEvent && dismissedEventId !== latestEvent.id && (
        <ActivityPopup
          event={latestEvent}
          onDismiss={() => setDismissedEventId(latestEvent.id)}
        />
      )}
      <aside className="sidebar" aria-label="Product navigation">
        <div className="brand-block">
          <div className="brand-mark">P</div>
          <div>
            <p className="brand-name">{productConfig.name}</p>
            <p className="brand-meta">Adaptive Demo</p>
          </div>
        </div>

        <nav className="nav-list">
          <button
            className="nav-item system-nav-item"
            onClick={() => setExperience("system")}
            type="button"
          >
            <span className="nav-shortcut">P</span>
            <span>Pixel System</span>
          </button>
          {navItems.map((item) => (
            <button
              className={item.id === currentPage ? "nav-item active" : "nav-item"}
              data-testid={`nav-${item.id}`}
              key={item.id}
              onClick={() => runAction(actionByPage[item.id])}
              type="button"
            >
              <span className="nav-shortcut">{item.shortcut}</span>
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <Link className="architecture-nav-link" href="/architecture">
          <span className="nav-shortcut">A</span>
          <span>System architecture</span>
        </Link>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="section-kicker">Current View</p>
            <h1 data-testid="current-view-title">{activeNavItem?.label ?? "Issue Detail"}</h1>
            <p className="scope-caption">
              {workspaceScope.name}: only scoped projects, tickets, cycles, and team members are visible.
            </p>
          </div>
          <div className="status-strip">
            <span className="status-dot" />
            <select
              aria-label="Workspace scope"
              className="workspace-switcher"
              data-testid="workspace-switcher"
              onChange={(event) => switchWorkspaceScope(event.target.value)}
              value={workspaceScope.id}
            >
              {workspaceScopes.map((scope) => (
                <option key={scope.id} value={scope.id}>
                  {scope.name}
                </option>
              ))}
            </select>
          </div>
        </header>

        <ProductSurface
          createCycle={createCycle}
          createIssue={createIssue}
          createProject={createProject}
          createTeamMember={createTeamMember}
          cycles={scopedCycles}
          draftPrefill={draftPrefill}
          issues={scopedIssues}
          projects={scopedProjects}
          runAction={runAction}
          team={scopedTeam}
          uiState={uiState}
          updateIssue={updateIssue}
          workspaceScope={workspaceScope}
        />
      </section>

      <aside
        className={isAssistantCollapsed ? "assistant-panel collapsed" : "assistant-panel"}
        aria-label="Assistant"
      >
        {isAssistantCollapsed ? (
          <button
            className="assistant-rail"
            data-testid="assistant-expand"
            onClick={() => setIsAssistantCollapsed(false)}
            type="button"
          >
            Ask Edith
          </button>
        ) : (
          <ConversationCard
            agentServiceStatus={agentServiceStatus}
            demoPathPrompts={demoPathPrompts}
            demoPrompts={demoPrompts}
            isSending={isSending}
            messages={messages}
            onCollapse={() => setIsAssistantCollapsed(true)}
            onRestart={restartConversation}
            onReset={resetDemoSession}
            onRetryService={() => void loadStoredData()}
            onSend={sendMessage}
            serviceError={serviceError}
            sessionId={sessionId}
            turnStatus={turnStatus}
          />
        )}
      </aside>
    </main>
  );
}

function PixelSystemHome({ onEnterDemo }: { onEnterDemo: () => void }) {
  const platformPillars = [
    {
      title: "Product definitions",
      body: "Each product declares its records, screens, actions, vocabulary and guardrails. Pixel runs from that contract."
    },
    {
      title: "Scoped workspaces",
      body: "Organizations, products and record scopes stay isolated. Edith answers inside the selected product only."
    },
    {
      title: "Safe execution",
      body: "Creates and updates require backend-issued execution keys, committed receipts and no duplicate writes."
    },
    {
      title: "Guided demos",
      body: "A customer can still enter a controlled demo to understand how Pixel behaves before configuring a product."
    }
  ];
  const flow = ["Sign in", "Add product", "Review definition", "Open product", "Ask Edith", "Commit safe changes"];

  return (
    <main className="pixel-system">
      <section className="system-hero" aria-labelledby="pixel-system-title">
        <nav className="system-topbar" aria-label="Pixel system navigation">
          <div className="system-brand">
            <span className="system-mark">P</span>
            <span>Pixel</span>
          </div>
          <div className="system-actions">
            <Link href="/architecture">Architecture</Link>
            <button className="secondary-button compact" onClick={onEnterDemo} type="button">
              Visit demo
            </button>
          </div>
        </nav>

        <div className="system-hero-grid">
          <div className="system-hero-copy">
            <p className="section-kicker">Pixel System</p>
            <h1 id="pixel-system-title">A product-specific AI workspace for demos, records and safe actions.</h1>
            <p>
              Pixel lets a team add a product, define what Edith is allowed to know and do,
              and operate inside that product without leaking across tenants, products or scopes.
            </p>
            <div className="system-cta-row">
              <button className="primary-system-button" onClick={onEnterDemo} type="button">
                Visit live demo
              </button>
              <Link className="system-text-link" href="/architecture">
                View system architecture
              </Link>
            </div>
          </div>

          <div className="system-console-preview" aria-label="Pixel product flow preview">
            <div className="preview-header">
              <span className="status-dot" />
              <strong>Product runtime</strong>
              <span>Definition driven</span>
            </div>
            <ol className="system-flow">
              {flow.map((step, index) => (
                <li key={step}>
                  <span>{index + 1}</span>
                  <strong>{step}</strong>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </section>

      <section className="system-section" aria-labelledby="system-capabilities">
        <div className="system-section-head">
          <p className="section-kicker">Current platform</p>
          <h2 id="system-capabilities">What Pixel is built to prove</h2>
        </div>
        <div className="system-pillar-grid">
          {platformPillars.map((pillar) => (
            <article className="system-pillar" key={pillar.title}>
              <h3>{pillar.title}</h3>
              <p>{pillar.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="system-demo-band" aria-labelledby="demo-entry-title">
        <div>
          <p className="section-kicker">Interactive proof</p>
          <h2 id="demo-entry-title">Open the guided demo</h2>
          <p>
            The demo shows Edith navigating a product workspace, creating records, assigning work,
            enforcing boundaries and explaining what happened.
          </p>
        </div>
        <button className="primary-system-button" onClick={onEnterDemo} type="button">
          Enter demo workspace
        </button>
      </section>
    </main>
  );
}

function ProductSurface({
  createCycle,
  createIssue,
  createProject,
  createTeamMember,
  cycles,
  draftPrefill,
  issues,
  projects,
  team,
  uiState,
  runAction,
  updateIssue,
  workspaceScope
}: {
  createCycle: CreateRecord<DemoCycle>;
  createIssue: CreateRecord<DemoIssue>;
  createProject: CreateRecord<DemoProject>;
  createTeamMember: CreateRecord<DemoTeamMember>;
  cycles: DemoCycle[];
  draftPrefill: DraftPrefill;
  issues: DemoIssue[];
  projects: DemoProject[];
  team: DemoTeamMember[];
  uiState: SessionUiState;
  runAction: (action: DemoAction) => void;
  updateIssue: (issue: DemoIssue) => Promise<void>;
  workspaceScope: DemoWorkspaceScope;
}) {
  if (uiState.current_page === "issues") {
    return (
      <IssuesView
        assigneeFilter={uiState.issue_filter_assignee}
        createIssue={createIssue}
        cycles={cycles}
        draftPrefill={draftPrefill.issue}
        issues={issues}
        projects={projects}
        runAction={runAction}
        team={team}
        highlightedTarget={uiState.highlighted_target}
      />
    );
  }
  if (uiState.current_page === "issue_detail") {
    return (
      <IssueDetailView
        issues={issues}
        onUpdateIssue={updateIssue}
        runAction={runAction}
        team={team}
        uiState={uiState}
      />
    );
  }
  if (uiState.current_page === "projects") {
    return (
      <ProjectsView
        createProject={createProject}
        highlightedTarget={uiState.highlighted_target}
        projects={projects}
        team={team}
        workspaceScope={workspaceScope}
      />
    );
  }
  if (uiState.current_page === "cycles") {
    return (
      <CyclesView
        createCycle={createCycle}
        cycles={cycles}
        highlightedTarget={uiState.highlighted_target}
        workspaceScope={workspaceScope}
      />
    );
  }
  if (uiState.current_page === "teams") {
    return (
      <TeamsView
        createTeamMember={createTeamMember}
        draftPrefill={draftPrefill.teamMember}
        highlightedTarget={uiState.highlighted_target}
        team={team}
        workspaceScope={workspaceScope}
      />
    );
  }
  if (uiState.current_page === "integrations") {
    return <IntegrationsView highlightedTarget={uiState.highlighted_target} />;
  }
  return (
    <DashboardView
      cycles={cycles}
      issues={issues}
      projects={projects}
      runAction={runAction}
      team={team}
      workspaceScope={workspaceScope}
    />
  );
}

function DashboardView({
  cycles,
  issues,
  projects,
  runAction,
  team,
  workspaceScope
}: {
  cycles: DemoCycle[];
  issues: DemoIssue[];
  projects: DemoProject[];
  runAction: (action: DemoAction) => void;
  team: DemoTeamMember[];
  workspaceScope: DemoWorkspaceScope;
}) {
  const activeCycle = cycles[0] ?? demoCycle;
  const atRiskProjects = projects.filter((project) => project.status === "At risk").length;
  const urgentIssues = issues.filter((issue) => issue.priority === "High").length;
  const activeProject = projects.find((project) => project.status === "Active") ?? projects[0];
  const nextMilestone = activeProject?.targetDate ?? "No milestone set";
  const averageLoad =
    team.length > 0
      ? Math.round(team.reduce((total, member) => total + member.load, 0) / team.length)
      : 0;
  const reviewIssues = issues.filter((issue) => issue.status === "Review").length;
  const inProgressIssues = issues.filter((issue) => issue.status === "In progress").length;
  const availableCapacity = Math.max(0, 100 - averageLoad);
  const cycleCompletion = activeCycle.completed + activeCycle.inProgress + activeCycle.remaining;
  const activeCycleProgress = cycleProgress(activeCycle);
  const deliveryConfidence =
    activeCycleProgress >= 65 && atRiskProjects === 0
      ? "On track"
      : activeCycleProgress >= 40
        ? "Watch closely"
        : "Needs attention";

  return (
    <div className="content-grid">
      <section className="panel wide dashboard-hero">
        <div className="panel-header">
          <div>
            <p className="section-kicker">Scoped Workspace</p>
            <h2>{workspaceScope.name}</h2>
            <p className="body-copy">{workspaceScope.description}</p>
          </div>
          <span className="quiet-badge" data-testid="workspace-scope-badge">
            {projects.length} scoped projects
          </span>
        </div>
        <div className="metric-grid">
          <Metric label="Scoped issues" value={`${issues.length}`} delta="visible to this team" />
          <Metric
            label="Cycle progress"
            value={`${activeCycleProgress}%`}
            delta={`${cycleDaysLeft(activeCycle)} days left`}
          />
          <Metric label="Active projects" value={`${projects.length}`} delta={`${atRiskProjects} at risk`} />
          <Metric label="Team workload" value={`${averageLoad}%`} delta={`${team.length} scoped members`} />
        </div>
        <div className="dashboard-scope-insights">
          <article>
            <span>Primary focus</span>
            <strong>{activeProject?.name ?? "No active project"}</strong>
            <p>{activeProject?.description ?? "Create a project to start tracking scoped work."}</p>
          </article>
          <article>
            <span>Risk signal</span>
            <strong>{urgentIssues} high priority</strong>
            <p>{atRiskProjects > 0 ? "Review at-risk project scope before the next sync." : "No scoped projects are marked at risk."}</p>
          </article>
          <article>
            <span>Next milestone</span>
            <strong>{nextMilestone}</strong>
            <p>{activeCycle.name} is the current planning window for this workspace.</p>
          </article>
        </div>
        <div className="scope-lockup" data-testid="scope-lockup">
          <div>
            <span>Project boundary</span>
            <strong>{workspaceScope.name}</strong>
          </div>
          <p>
            Edith answers and acts only inside {workspaceScope.allowedIssueProjects.join(" and ")}.
            Requests for another workspace are blocked instead of shown.
          </p>
        </div>
      </section>

      <section className="panel wide command-center">
        <div className="panel-header">
          <div>
            <p className="section-kicker">Operating View</p>
            <h2>{workspaceScope.name.replace(" Workspace", "")} command center</h2>
          </div>
          <span className="quiet-badge">{deliveryConfidence}</span>
        </div>
        <div className="command-center-grid">
          <article className="delivery-card">
            <span>Delivery confidence</span>
            <strong>{deliveryConfidence}</strong>
            <p>
              {activeCycleProgress}% cycle progress across {cycleCompletion} planned work items.
            </p>
          </article>
          <article className="delivery-card">
            <span>Execution queue</span>
            <strong>{inProgressIssues} in progress</strong>
            <p>{reviewIssues} waiting for review, {urgentIssues} high priority.</p>
          </article>
          <article className="delivery-card">
            <span>Capacity buffer</span>
            <strong>{availableCapacity}% open</strong>
            <p>{averageLoad}% average scoped workload across {team.length} members.</p>
          </article>
        </div>
        <div className="workspace-lanes">
          {projects.slice(0, 3).map((project) => (
            <article className="workspace-lane" key={project.id}>
              <div>
                <span>{project.id}</span>
                <strong>{project.name}</strong>
              </div>
              <div className="progress-bar">
                <span style={{ width: `${project.progress}%` }} />
              </div>
              <em>{project.progress}%</em>
            </article>
          ))}
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>Priority work</h2>
        </div>
        <IssueList issues={issues} limit={4} runAction={runAction} />
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>Current cycle</h2>
        </div>
        <CycleProgress compact cycle={activeCycle} />
      </section>
    </div>
  );
}

function IssuesView({
  assigneeFilter,
  createIssue,
  cycles,
  draftPrefill,
  highlightedTarget,
  issues,
  projects,
  runAction,
  team
}: {
  assigneeFilter?: string;
  createIssue: CreateRecord<DemoIssue>;
  cycles: DemoCycle[];
  draftPrefill?: DraftPrefill["issue"];
  highlightedTarget?: string;
  issues: DemoIssue[];
  projects: DemoProject[];
  runAction: (action: DemoAction) => void;
  team: DemoTeamMember[];
}) {
  const [isCreating, setIsCreating] = useState(false);
  const visibleIssueCount = assigneeFilter
    ? issues.filter((issue) => issue.assignee === assigneeFilter).length
    : issues.length;

  useEffect(() => {
    if (draftPrefill) {
      setIsCreating(true);
    }
  }, [draftPrefill]);

  return (
    <div className="surface-stack">
      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">Issue Tracking</p>
            <h2>Open issues</h2>
          </div>
          <div className="panel-actions">
            <button
              className={
                highlightedTarget === "create_ticket_button"
                  ? "primary-button highlighted-action"
                  : "primary-button"
              }
              data-testid="create-ticket-button"
              onClick={() => setIsCreating((isOpen) => !isOpen)}
              type="button"
            >
              Create ticket
            </button>
            <span className="quiet-badge" data-testid="issue-count-badge">
              {visibleIssueCount} open
            </span>
          </div>
        </div>
        {assigneeFilter && (
          <div className="filter-bar" data-testid="issue-filter">
            <span>Assignee</span>
            <strong>{assigneeFilter}</strong>
          </div>
        )}
        {isCreating && (
          <IssueCreatePanel
            cycles={cycles}
            issues={issues}
            onCancel={() => setIsCreating(false)}
            onCreate={async (issue, requestKey) => {
              await createIssue(issue, requestKey);
              setIsCreating(false);
            }}
            projects={projects}
            prefill={draftPrefill}
            team={team}
          />
        )}
        <IssueList assigneeFilter={assigneeFilter} issues={issues} runAction={runAction} />
      </section>
    </div>
  );
}

function IssueCreatePanel({
  cycles,
  issues,
  onCancel,
  onCreate,
  prefill,
  projects,
  team
}: {
  cycles: DemoCycle[];
  issues: DemoIssue[];
  onCancel: () => void;
  onCreate: CreateRecord<DemoIssue>;
  prefill?: DraftPrefill["issue"];
  projects: DemoProject[];
  team: DemoTeamMember[];
}) {
  const submission = useRecordSubmit<DemoIssue>();
  const [title, setTitle] = useState(prefill?.title ?? "Investigate customer onboarding issue");
  const [assignee, setAssignee] = useState(prefill?.assignee ?? team[0]?.name ?? "Maya Chen");
  const [priority, setPriority] = useState<DemoIssue["priority"]>(prefill?.priority ?? "Medium");
  const [status, setStatus] = useState("Todo");
  const [project, setProject] = useState(projects[0]?.name ?? "Issue Triage");
  const [cycle, setCycle] = useState(cycles[0]?.name ?? demoCycle.name);
  const [estimate, setEstimate] = useState("2 pts");
  const [label, setLabel] = useState("Customer");
  const [description, setDescription] = useState(
    "Capture the request, assign an owner, and track it through the current cycle."
  );

  useEffect(() => {
    if (!prefill) return;
    if (prefill.title) setTitle(prefill.title);
    if (prefill.assignee) setAssignee(prefill.assignee);
    if (prefill.priority) setPriority(prefill.priority);
  }, [prefill]);

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedTitle = title.trim();
    if (!trimmedTitle) return;

    void submission.submit({
      id: "",
      title: trimmedTitle,
      priority,
      assignee,
      project,
      status,
      cycle,
      estimate,
      label,
      description: description.trim()
    }, onCreate);
  }

  return (
    <form className="creation-panel" data-testid="issue-create-panel" onSubmit={handleSubmit}>
      {submission.error && <p className="form-error" role="alert">{submission.error}</p>}
      <fieldset className="creation-fields" disabled={submission.saving}>
      <div className="creation-header">
        <div>
          <p className="section-kicker">New Ticket</p>
          <h3>Create issue</h3>
        </div>
        <div className="panel-actions">
          <button className="secondary-button compact" onClick={onCancel} type="button">
            Cancel
          </button>
          <button className="primary-button" data-testid="submit-create-ticket" type="submit">
            Create
          </button>
        </div>
      </div>
      <div className="form-grid">
        <label className="field wide">
          <span>Title</span>
          <input
            data-testid="ticket-title-input"
            onChange={(event) => setTitle(event.target.value)}
            value={title}
          />
        </label>
        <label className="field">
          <span>Assignee</span>
          <select
            data-testid="ticket-assignee-select"
            onChange={(event) => setAssignee(event.target.value)}
            value={assignee}
          >
            {team.map((member) => (
              <option key={member.name} value={member.name}>
                {member.name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Priority</span>
          <select onChange={(event) => setPriority(event.target.value as DemoIssue["priority"])} value={priority}>
            {priorities.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Status</span>
          <select onChange={(event) => setStatus(event.target.value)} value={status}>
            {issueStatuses.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Project</span>
          <select onChange={(event) => setProject(event.target.value)} value={project}>
            {projects.map((item) => (
              <option key={item.id} value={item.name}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Cycle</span>
          <select onChange={(event) => setCycle(event.target.value)} value={cycle}>
            {cycles.map((item) => (
              <option key={item.id} value={item.name}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Estimate</span>
          <select onChange={(event) => setEstimate(event.target.value)} value={estimate}>
            {["1 pt", "2 pts", "3 pts", "5 pts", "8 pts"].map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Label</span>
          <select onChange={(event) => setLabel(event.target.value)} value={label}>
            {["Customer", "Bug", "Improvement", "Integration", "Planning"].map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label className="field wide">
          <span>Description</span>
          <textarea onChange={(event) => setDescription(event.target.value)} value={description} />
        </label>
      </div>
      </fieldset>
    </form>
  );
}

function IssueDetailView({
  issues,
  onUpdateIssue,
  runAction,
  team,
  uiState
}: {
  issues: DemoIssue[];
  onUpdateIssue: (issue: DemoIssue) => Promise<void>;
  runAction: (action: DemoAction) => void;
  team: DemoTeamMember[];
  uiState: SessionUiState;
}) {
  const selectedIssueId = uiState.selected_issue_id ?? "LIN-142";
  const edit = useRecordSubmit<DemoIssue>();
  const saveEdit = (issue: DemoIssue) => edit.submit(issue, onUpdateIssue);
  const issue = issues.find((item) => item.id === selectedIssueId) ?? issues[0] ?? demoIssues[0];
  const isAssignmentHighlighted =
    uiState.highlighted_target === "assignment_control" ||
    uiState.highlighted_target === `assignee_${issue.id}`;
  const isCreatedIssue = uiState.highlighted_target === "created_issue";
  const isUpdatedIssue = uiState.highlighted_target === "updated_issue";

  return (
    <div className="surface-stack">
      <article className="panel" data-testid="issue-detail-panel">
        {edit.error && <p className="form-error" role="alert">{edit.error}</p>}
        <div className="panel-header">
          <div>
            <p className="section-kicker">Issue Detail</p>
            <h2>{issue.title}</h2>
          </div>
          <div className="panel-actions">
            <button
              className="secondary-button compact"
              onClick={() => runAction({ type: "OPEN_ISSUES" })}
              type="button"
            >
              Back to issues
            </button>
            {isCreatedIssue && <span className="quiet-badge active-status">Created now</span>}
            {isUpdatedIssue && <span className="quiet-badge active-status">Updated now</span>}
            <span className="quiet-badge" data-testid="selected-issue-id">
              {issue.id}
            </span>
          </div>
        </div>
        <div className="detail-layout">
          <div className="detail-main">
            <div className="detail-description">
              <h3>Description</h3>
              <p>
                {issue.description ||
                  "Capture the request, assign an owner, and track it through the current cycle."}
              </p>
            </div>
            <div className="detail-meta-grid">
              <div className="meta-item">
                <span>Project</span>
                <strong>{issue.project}</strong>
              </div>
              <div className="meta-item">
                <span>Cycle</span>
                <strong>{issue.cycle || "Unassigned"}</strong>
              </div>
              <div className="meta-item">
                <span>Estimate</span>
                <strong>{issue.estimate || "2 pts"}</strong>
              </div>
              <div className="meta-item">
                <span>Label</span>
                <strong>{issue.label || "Customer"}</strong>
              </div>
            </div>
          </div>

          <aside className="detail-sidebar">
            <div
              className={isAssignmentHighlighted ? "property-row highlighted" : "property-row"}
              data-testid="assignee-control"
            >
              <span>Assignee</span>
              <select
                className="property-select"
                data-testid="assignee-select"
                disabled={edit.saving}
                onChange={(event) => void saveEdit({ ...issue, assignee: event.target.value })}
                value={issue.assignee}
              >
                {team.map((member) => (
                  <option key={member.name} value={member.name}>
                    {member.name} ({member.role})
                  </option>
                ))}
                {!team.some((m) => m.name === issue.assignee) && (
                  <option value={issue.assignee}>
                    {issue.assignee} (External)
                  </option>
                )}
              </select>
            </div>
            <div className="property-row">
              <span>Priority</span>
              <select
                className="property-select"
                onChange={(event) =>
                  void saveEdit({ ...issue, priority: event.target.value as DemoIssue["priority"] })
                }
                disabled={edit.saving}
                value={issue.priority}
              >
                {priorities.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </div>
            <div className="property-row">
              <span>Status</span>
              <select
                className="property-select"
                onChange={(event) => void saveEdit({ ...issue, status: event.target.value })}
                disabled={edit.saving}
                value={issue.status}
              >
                {issueStatuses.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </div>
            <button
              className="secondary-button"
              data-testid="highlight-assignee-button"
              onClick={() =>
                runAction({
                  type: "HIGHLIGHT_ASSIGNMENT_CONTROL",
                  payload: { issue_id: issue.id }
                })
              }
              type="button"
            >
              Highlight assignee
            </button>
          </aside>
        </div>
      </article>
    </div>
  );
}

function ProjectsView({
  createProject,
  highlightedTarget,
  projects,
  team,
  workspaceScope
}: {
  createProject: CreateRecord<DemoProject>;
  highlightedTarget?: string;
  projects: DemoProject[];
  team: DemoTeamMember[];
  workspaceScope: DemoWorkspaceScope;
}) {
  const [isCreating, setIsCreating] = useState(false);

  return (
    <div className="surface-stack">
      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">Scoped Roadmap</p>
            <h2>{workspaceScope.name}</h2>
          </div>
          <div className="panel-actions">
            <button
              className={
                highlightedTarget === "create_project_button"
                  ? "primary-button highlighted-action"
                  : "primary-button"
              }
              data-testid="create-project-button"
              onClick={() => setIsCreating((isOpen) => !isOpen)}
              type="button"
            >
              Create project
            </button>
            <span className="quiet-badge">{projects.length} active</span>
          </div>
        </div>

        {isCreating && (
          <ProjectCreatePanel
            onCancel={() => setIsCreating(false)}
            onCreate={async (project, requestKey) => {
              await createProject(project, requestKey);
              setIsCreating(false);
            }}
            members={team}
            projects={projects}
          />
        )}

        {projects.length === 0 ? (
          <div className="empty-panel" data-testid="projects-empty-state">
            <strong>No scoped projects yet</strong>
            <p>Create a project to give this workspace its own roadmap, lead, target date, and progress signal.</p>
          </div>
        ) : (
          <div className="project-list">
            {projects.map((project) => (
              <article className="project-row" key={project.id || project.name}>
              <div>
                <div className="project-header-line">
                  <h3>{project.name}</h3>
                  <span className={`status-tag status-${project.status.toLowerCase().replace(/\s+/g, "-")}`}>
                    {project.status}
                  </span>
                </div>
                <p>{project.description}</p>
                <div className="project-meta-line">
                  <span>Lead: <strong>{project.lead || "Avery Brooks"}</strong></span>
                  <span>Team: <strong>{project.team || "Engineering"}</strong></span>
                  {project.targetDate && <span>Target: <strong>{project.targetDate}</strong></span>}
                </div>
              </div>
              <div className="progress-cell">
                <div className="progress-bar">
                  <span style={{ width: `${project.progress}%` }} />
                </div>
                <strong>{project.progress}%</strong>
              </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function ProjectCreatePanel({
  onCancel,
  onCreate,
  projects,
  members
}: {
  onCancel: () => void;
  onCreate: CreateRecord<DemoProject>;
  projects: DemoProject[];
  members: DemoTeamMember[];
}) {
  const submission = useRecordSubmit<DemoProject>();
  const [name, setName] = useState("Design System V2");
  const [description, setDescription] = useState(
    "Standardize dynamic tokens, accessible dark mode components, and responsive grid patterns across the product."
  );
  const [status, setStatus] = useState<DemoProject["status"]>("Active");
  const [lead, setLead] = useState(members[0]?.name ?? "Maya Chen");
  const [teamName, setTeamName] = useState("Product Engineering");
  const [targetDate, setTargetDate] = useState("2026-11-15");
  const [progress, setProgress] = useState(25);

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedName = name.trim();
    if (!trimmedName) return;

    void submission.submit({
      id: "",
      name: trimmedName,
      description: description.trim() || "No description provided.",
      progress,
      status,
      lead,
      team: teamName,
      targetDate
    }, onCreate);
  }

  return (
    <form className="creation-panel" data-testid="project-create-panel" onSubmit={handleSubmit}>
      {submission.error && <p className="form-error" role="alert">{submission.error}</p>}
      <fieldset className="creation-fields" disabled={submission.saving}>
      <div className="creation-header">
        <div>
          <p className="section-kicker">New Project</p>
          <h3>Create project</h3>
        </div>
        <div className="panel-actions">
          <button className="secondary-button compact" onClick={onCancel} type="button">
            Cancel
          </button>
          <button className="primary-button" data-testid="submit-create-project" type="submit">
            Create
          </button>
        </div>
      </div>
      <div className="form-grid">
        <label className="field wide">
          <span>Project Name</span>
          <input
            data-testid="project-name-input"
            onChange={(event) => setName(event.target.value)}
            placeholder="e.g. Design System V2"
            required
            value={name}
          />
        </label>
        <label className="field wide">
          <span>Description</span>
          <textarea
            onChange={(event) => setDescription(event.target.value)}
            placeholder="What is the goal of this project?"
            value={description}
          />
        </label>
        <label className="field">
          <span>Lead</span>
          <select onChange={(event) => setLead(event.target.value)} value={lead}>
            {members.map((member) => (
              <option key={member.name} value={member.name}>
                {member.name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Status</span>
          <select onChange={(event) => setStatus(event.target.value as DemoProject["status"])} value={status}>
            <option value="Planned">Planned</option>
            <option value="Active">Active</option>
            <option value="At risk">At risk</option>
            <option value="Completed">Completed</option>
          </select>
        </label>
        <label className="field">
          <span>Team</span>
          <select onChange={(event) => setTeamName(event.target.value)} value={teamName}>
            <option value="Product Engineering">Product Engineering</option>
            <option value="Platform">Platform</option>
            <option value="Design">Design</option>
          </select>
        </label>
        <label className="field">
          <span>Target Date</span>
          <input
            onChange={(event) => setTargetDate(event.target.value)}
            type="date"
            value={targetDate}
          />
        </label>
        <label className="field wide">
          <span>Initial Progress ({progress}%)</span>
          <input
            max="100"
            min="0"
            onChange={(event) => setProgress(Number(event.target.value))}
            type="range"
            value={progress}
          />
        </label>
      </div>
      </fieldset>
    </form>
  );
}

function CyclesView({
  createCycle,
  cycles,
  highlightedTarget,
  workspaceScope
}: {
  createCycle: CreateRecord<DemoCycle>;
  cycles: DemoCycle[];
  highlightedTarget?: string;
  workspaceScope: DemoWorkspaceScope;
}) {
  const [isCreating, setIsCreating] = useState(false);

  return (
    <div className="surface-stack">
      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">Scoped Sprint Planning</p>
            <h2>{workspaceScope.name} cycles</h2>
          </div>
          <div className="panel-actions">
            <button
              className={
                highlightedTarget === "create_cycle_button"
                  ? "primary-button highlighted-action"
                  : "primary-button"
              }
              data-testid="create-cycle-button"
              onClick={() => setIsCreating((isOpen) => !isOpen)}
              type="button"
            >
              Create cycle
            </button>
            <span className="quiet-badge">{cycles.length} active</span>
          </div>
        </div>

        {isCreating && (
          <CycleCreatePanel
            cycles={cycles}
            onCancel={() => setIsCreating(false)}
            onCreate={async (cycle, requestKey) => {
              await createCycle(cycle, requestKey);
              setIsCreating(false);
            }}
          />
        )}

        {cycles.length === 0 ? (
          <div className="empty-panel" data-testid="cycles-empty-state">
            <strong>No scoped cycles yet</strong>
            <p>Create a cycle to plan the next delivery window for this workspace.</p>
          </div>
        ) : (
          <div className="cycles-stack">
            {cycles.map((cycle) => (
              <article className="cycle-card-item" key={cycle.id || cycle.name}>
              <div className="cycle-card-top">
                <div>
                  <div className="cycle-title-group">
                    <h3>{cycle.name}</h3>
                    <span className={`status-tag status-${cycle.status.toLowerCase()}`}>
                      {cycle.status}
                    </span>
                  </div>
                  <p className="cycle-meta-text">
                    {cycle.startDate} to {cycle.endDate} - <strong>{cycleDaysLeft(cycle)} days left</strong>
                  </p>
                </div>
                <span className="quiet-badge">{cycle.team}</span>
              </div>
              <CycleProgress compact cycle={cycle} highlighted={highlightedTarget === "cycle_progress"} />
              <div className="focus-section">
                <span className="focus-label">Cycle focus:</span>
                <div className="focus-list">
                  {cycle.focus.map((item) => (
                    <span key={item}>{item}</span>
                  ))}
                </div>
              </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function CycleCreatePanel({
  cycles,
  onCancel,
  onCreate
}: {
  cycles: DemoCycle[];
  onCancel: () => void;
  onCreate: CreateRecord<DemoCycle>;
}) {
  const submission = useRecordSubmit<DemoCycle>();
  const [name, setName] = useState("Frontend Cycle 15");
  const [status, setStatus] = useState<DemoCycle["status"]>("Active");
  const [team, setTeam] = useState("Product Engineering");
  const [startDate, setStartDate] = useState("2026-09-09");
  const [endDate, setEndDate] = useState("2026-09-23");
  const [focusInput, setFocusInput] = useState("");
  const [focusTags, setFocusTags] = useState<string[]>([
    "Assignee Workflow",
    "Real-time Voice",
    "Ticket Reassignment"
  ]);

  function addFocusTag() {
    const trimmed = focusInput.trim();
    if (trimmed && !focusTags.includes(trimmed)) {
      setFocusTags([...focusTags, trimmed]);
      setFocusInput("");
    }
  }

  function removeFocusTag(tagToRemove: string) {
    setFocusTags(focusTags.filter((t) => t !== tagToRemove));
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedName = name.trim();
    if (!trimmedName) return;

    const endMs = new Date(endDate).getTime();
    const nowMs = Date.now();
    const daysLeft = Math.max(0, Math.ceil((endMs - nowMs) / (1000 * 60 * 60 * 24)));

    void submission.submit({
      id: "",
      name: trimmedName,
      daysLeft,
      progress: 0,
      completed: 0,
      inProgress: 0,
      remaining: 0,
      focus: focusTags.length > 0 ? focusTags : ["General development"],
      status,
      team,
      startDate,
      endDate
    }, onCreate);
  }

  return (
    <form className="creation-panel" data-testid="cycle-create-panel" onSubmit={handleSubmit}>
      {submission.error && <p className="form-error" role="alert">{submission.error}</p>}
      <fieldset className="creation-fields" disabled={submission.saving}>
      <div className="creation-header">
        <div>
          <p className="section-kicker">New Sprint / Cycle</p>
          <h3>Create cycle</h3>
        </div>
        <div className="panel-actions">
          <button className="secondary-button compact" onClick={onCancel} type="button">
            Cancel
          </button>
          <button className="primary-button" data-testid="submit-create-cycle" type="submit">
            Create
          </button>
        </div>
      </div>
      <div className="form-grid">
        <label className="field wide">
          <span>Cycle Name</span>
          <input
            data-testid="cycle-name-input"
            onChange={(event) => setName(event.target.value)}
            placeholder="e.g. Frontend Cycle 15"
            required
            value={name}
          />
        </label>
        <label className="field">
          <span>Status</span>
          <select onChange={(event) => setStatus(event.target.value as DemoCycle["status"])} value={status}>
            <option value="Planned">Planned</option>
            <option value="Active">Active</option>
            <option value="Completed">Completed</option>
          </select>
        </label>
        <label className="field">
          <span>Team</span>
          <select onChange={(event) => setTeam(event.target.value)} value={team}>
            <option value="Product Engineering">Product Engineering</option>
            <option value="Platform">Platform</option>
            <option value="Design">Design</option>
          </select>
        </label>
        <label className="field">
          <span>Start Date</span>
          <input
            onChange={(event) => setStartDate(event.target.value)}
            type="date"
            value={startDate}
          />
        </label>
        <label className="field">
          <span>End Date</span>
          <input
            onChange={(event) => setEndDate(event.target.value)}
            type="date"
            value={endDate}
          />
        </label>
        <div className="field wide">
          <span>Focus Areas (Tags)</span>
          <div className="tag-input-row">
            <input
              onChange={(e) => setFocusInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addFocusTag();
                }
              }}
              placeholder="Type a focus area and press Enter or Add..."
              value={focusInput}
            />
            <button
              className="secondary-button compact"
              onClick={(e) => {
                e.preventDefault();
                addFocusTag();
              }}
              type="button"
            >
              Add Tag
            </button>
          </div>
          <div className="tag-pill-cloud">
            {focusTags.map((tag) => (
              <span className="tag-pill" key={tag}>
                {tag}
                <button onClick={() => removeFocusTag(tag)} type="button">
                  x
                </button>
              </span>
            ))}
          </div>
        </div>
      </div>
      </fieldset>
    </form>
  );
}

function TeamsView({
  createTeamMember,
  draftPrefill,
  highlightedTarget,
  team,
  workspaceScope
}: {
  createTeamMember: CreateRecord<DemoTeamMember>;
  draftPrefill?: DraftPrefill["teamMember"];
  highlightedTarget?: string;
  team: DemoTeamMember[];
  workspaceScope: DemoWorkspaceScope;
}) {
  const [isCreating, setIsCreating] = useState(
    highlightedTarget === "add_member_button" || Boolean(draftPrefill)
  );

  useEffect(() => {
    if (highlightedTarget === "add_member_button" || draftPrefill) {
      setIsCreating(true);
    }
  }, [draftPrefill, highlightedTarget]);

  return (
    <div className="surface-stack">
      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">Scoped Team</p>
            <h2>{workspaceScope.name} capacity</h2>
          </div>
          <div className="panel-actions">
            <button
              className={
                highlightedTarget === "add_member_button" || isCreating
                  ? "primary-button highlighted-action"
                  : "primary-button"
              }
              data-testid="add-team-member-button"
              onClick={() => setIsCreating((isOpen) => !isOpen)}
              type="button"
            >
              + Add Member
            </button>
            <span className="quiet-badge" data-testid="team-member-count">
              {team.length} members
            </span>
          </div>
        </div>
        {isCreating && (
          <TeamMemberCreatePanel
            onCancel={() => setIsCreating(false)}
            onCreate={async (member, requestKey) => {
              await createTeamMember(member, requestKey);
              setIsCreating(false);
            }}
            prefill={draftPrefill}
          />
        )}
        {team.length === 0 ? (
          <div className="empty-panel" data-testid="team-empty-state">
            <strong>No scoped members yet</strong>
            <p>Add a team member before assigning tickets or creating capacity plans in this workspace.</p>
          </div>
        ) : (
          <div className="team-grid">
            {team.map((member) => (
              <article className="member-card" key={member.name}>
              <div className="avatar">{member.initials}</div>
              <div>
                <h3>{member.name}</h3>
                <p>{member.role}</p>
                {member.email && <p>{member.email}</p>}
              </div>
              <strong>{member.load}%</strong>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function TeamMemberCreatePanel({
  onCancel,
  onCreate,
  prefill
}: {
  onCancel: () => void;
  onCreate: CreateRecord<DemoTeamMember>;
  prefill?: DraftPrefill["teamMember"];
}) {
  const submission = useRecordSubmit<DemoTeamMember>();
  const [name, setName] = useState(prefill?.name ?? "");
  const [initials, setInitials] = useState(initialsForName(prefill?.name ?? ""));
  const [email, setEmail] = useState(emailForName(prefill?.name ?? ""));
  const [role, setRole] = useState("Product Engineer");
  const [load, setLoad] = useState(50);

  useEffect(() => {
    if (!prefill?.name) return;
    setName(prefill.name);
    setInitials(initialsForName(prefill.name));
    setEmail(emailForName(prefill.name));
  }, [prefill]);

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedName = name.trim();
    if (!trimmedName) return;

    void submission.submit({
      name: trimmedName,
      initials: initials.trim().toUpperCase() || initialsForName(trimmedName),
      role,
      load: Number(load),
      email: email.trim() || emailForName(trimmedName)
    }, onCreate);
  }

  return (
    <form className="creation-panel" data-testid="team-member-create-panel" onSubmit={handleSubmit}>
      {submission.error && <p className="form-error" role="alert">{submission.error}</p>}
      <fieldset className="creation-fields" disabled={submission.saving}>
      <div className="creation-header">
        <div>
          <p className="section-kicker">New Employee</p>
          <h3>Add team member</h3>
        </div>
        <div className="panel-actions">
          <button className="secondary-button compact" onClick={onCancel} type="button">
            Cancel
          </button>
          <button className="primary-button" data-testid="submit-create-member" type="submit">
            Add Member
          </button>
        </div>
      </div>
      <div className="form-grid">
        <label className="field wide">
          <span>Full Name</span>
          <input
            data-testid="member-name-input"
            onChange={(event) => {
              setName(event.target.value);
              setInitials(initialsForName(event.target.value));
              setEmail(emailForName(event.target.value));
            }}
            placeholder="e.g. Lucifer Morningstar"
            required
            value={name}
          />
        </label>
        <label className="field">
          <span>Initials</span>
          <input
            data-testid="member-initials-input"
            maxLength={3}
            onChange={(event) => setInitials(event.target.value)}
            placeholder="LM"
            value={initials}
          />
        </label>
        <label className="field">
          <span>Email</span>
          <input
            data-testid="member-email-input"
            onChange={(event) => setEmail(event.target.value)}
            placeholder="lucifer@pixel.demo"
            type="email"
            value={email}
          />
        </label>
        <label className="field">
          <span>Role</span>
          <select onChange={(event) => setRole(event.target.value)} value={role}>
            <option value="Product Engineer">Product Engineer</option>
            <option value="Frontend Lead">Frontend Lead</option>
            <option value="Backend Engineer">Backend Engineer</option>
            <option value="Engineering Manager">Engineering Manager</option>
            <option value="Product Designer">Product Designer</option>
            <option value="DevOps / Platform">DevOps / Platform</option>
          </select>
        </label>
        <label className="field">
          <span>Current Workload (%)</span>
          <input
            max="100"
            min="0"
            onChange={(event) => setLoad(Number(event.target.value))}
            type="number"
            value={load}
          />
        </label>
      </div>
      </fieldset>
    </form>
  );
}

function IntegrationsView({ highlightedTarget }: { highlightedTarget?: string }) {
  const showGithubSetup = highlightedTarget === "github_setup";
  const highlightGithub = showGithubSetup || highlightedTarget === "github_card";
  const highlightSlack = highlightedTarget === "slack_card";
  const githubHealth = [
    ["Connection", "Connected"],
    ["Repositories", "3 selected"],
    ["PR sync", "Every 5 minutes"],
    ["Last event", "2 minutes ago"]
  ];

  return (
    <div className="surface-stack">
      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">Connected Workflow</p>
            <h2>Integrations</h2>
          </div>
          <span className="quiet-badge">{connectedIntegrationCount} connected</span>
        </div>
        <div className="integration-grid">
          {integrations.map((integration) => (
            <article
              className={
                integration.name === "GitHub" && highlightGithub
                  ? "integration-card highlighted-card"
                  : integration.name === "Slack" && highlightSlack
                    ? "integration-card highlighted-card"
                  : "integration-card"
              }
              data-testid={
                integration.name === "GitHub"
                  ? "github-integration-card"
                  : integration.name === "Slack"
                    ? "slack-integration-card"
                    : undefined
              }
              key={integration.name}
            >
              <div>
                <h3>{integration.name}</h3>
                <p>{integration.description}</p>
              </div>
              <span className={integration.connected ? "connected" : "available"}>
                {integration.connected ? "Connected" : "Available"}
              </span>
            </article>
          ))}
        </div>
        {showGithubSetup && (
          <div className="setup-panel" data-testid="github-setup-panel">
            <div className="setup-header">
              <div>
                <p className="section-kicker">GitHub Setup</p>
                <h3>Repository activity is mapped into scoped Pixel work.</h3>
              </div>
              <span className="connected">Demo connected</span>
            </div>
            <div className="github-health-grid">
              {githubHealth.map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
            <div className="github-setup-grid">
              <article>
                <span>Selected repositories</span>
                <strong>pixel/web, pixel/api, pixel/integrations</strong>
                <p>Only repositories mapped to the active workspace appear in this demo.</p>
              </article>
              <article>
                <span>Automation rules</span>
                <strong>PR mentions attach to tickets</strong>
                <p>Commits, pull requests, branch names, and review status become ticket context.</p>
              </article>
              <article>
                <span>Recent sync</span>
                <strong>PR #184 linked to PIX-143</strong>
                <p>Edith can explain the link and open the related work without leaving Pixel.</p>
              </article>
            </div>
            <div className="setup-steps">
              <span>Authorize workspace</span>
              <span>Select repositories</span>
              <span>Map branches to projects</span>
              <span>Sync PR and commit activity</span>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

function Metric({ label, value, delta }: { label: string; value: string; delta: string }) {
  return (
    <article className="metric-card">
      <p>{label}</p>
      <strong>{value}</strong>
      <span>{delta}</span>
    </article>
  );
}

function ActivityPopup({ event, onDismiss }: { event: UiEvent; onDismiss: () => void }) {
  return (
    <aside className={`activity-popup ${event.status}`} data-testid="activity-popup" role="status">
      <div>
        <span>{event.status === "executed" ? "Workspace updated" : "Action blocked"}</span>
        <strong>{event.description}</strong>
      </div>
      <button aria-label="Dismiss activity update" onClick={onDismiss} type="button">
        x
      </button>
    </aside>
  );
}

function IssueList({
  assigneeFilter,
  issues,
  limit,
  runAction
}: {
  assigneeFilter?: string;
  issues: DemoIssue[];
  limit?: number;
  runAction: (action: DemoAction) => void;
}) {
  const filteredIssues = assigneeFilter
    ? issues.filter((issue) => issue.assignee === assigneeFilter)
    : issues;
  const visibleIssues = typeof limit === "number" ? filteredIssues.slice(0, limit) : filteredIssues;

  if (visibleIssues.length === 0) {
    return (
      <div className="empty-panel" data-testid="issues-empty-state">
        <strong>No scoped tickets found</strong>
        <p>
          This workspace has no tickets matching the current view. Create a ticket or clear the
          assignee filter to continue the demo.
        </p>
      </div>
    );
  }

  return (
    <div className="issue-list">
      {visibleIssues.map((issue) => (
        <article className="issue-row" key={issue.id}>
          <div className="issue-main">
            <h3>{issue.title}</h3>
            <p>
              {issue.id} - {issue.assignee} - {issue.project}
            </p>
          </div>
          <div className="issue-controls">
            <span className={`priority ${issue.priority.toLowerCase()}`}>{issue.priority}</span>
            <span className="issue-status">{issue.status}</span>
            <button
              aria-label={`Open ${issue.id}`}
              className="row-button"
              onClick={() => runAction({ type: "OPEN_DEMO_ISSUE", payload: { issue_id: issue.id } })}
              type="button"
            >
              Open
            </button>
          </div>
        </article>
      ))}
    </div>
  );
}

function CycleProgress({
  cycle = demoCycle,
  compact = false,
  highlighted = false
}: {
  cycle?: DemoCycle;
  compact?: boolean;
  highlighted?: boolean;
}) {
  return (
    <div className={compact ? "cycle compact" : "cycle"}>
      <div className="cycle-summary">
        <div>
          <p>Completed</p>
          <strong>{cycle.completed}</strong>
        </div>
        <div>
          <p>In progress</p>
          <strong>{cycle.inProgress}</strong>
        </div>
        <div>
          <p>Remaining</p>
          <strong>{cycle.remaining}</strong>
        </div>
      </div>
      <div
        className={highlighted ? "progress-bar large highlighted-bar" : "progress-bar large"}
        data-testid="cycle-progress-bar"
      >
        <span style={{ width: `${cycleProgress(cycle)}%` }} />
      </div>
      {!compact && (
        <p className="body-copy">
          This screen is where the agent will land when a visitor asks about sprint planning,
          time-boxed work, cycle health, or replacing Jira sprint workflows.
        </p>
      )}
    </div>
  );
}

function IntentTraceCard({ trace }: { trace: IntentTrace }) {
  return (
    <section className="panel trace-card">
      <div className="panel-header">
        <div>
          <p className="section-kicker">Intent Trace</p>
          <h2>Understanding panel</h2>
        </div>
        <span className="quiet-badge" data-testid="trace-status">
          {trace.status}
        </span>
      </div>
      <dl className="trace-list">
        <TraceItem label="Role" testId="trace-role" value={trace.role} />
        <TraceItem label="Current tool" testId="trace-current-tool" value={trace.current_tool} />
        <TraceItem label="Goal" testId="trace-goal" value={trace.goal} />
        <TraceItem label="Pain point" testId="trace-pain-point" value={trace.pain_point} />
        <TraceItem
          label="Relevant feature"
          testId="trace-relevant-feature"
          value={trace.relevant_feature}
        />
        <TraceItem label="Why this view" testId="trace-reason" value={trace.reason} />
      </dl>
      <div className="confidence">
        <span>Confidence</span>
        <div className="progress-bar">
          <span style={{ width: `${Math.round((trace.confidence ?? 0) * 100)}%` }} />
        </div>
      </div>
    </section>
  );
}

function SessionSummaryCard({ summary }: { summary: SessionSummary }) {
  const interests = summary.interests.length > 0 ? summary.interests : ["None yet"];
  const painPoints = summary.pain_points.length > 0 ? summary.pain_points : ["None yet"];

  return (
    <section className="panel session-card" data-testid="session-summary">
      <div className="panel-header">
        <div>
          <p className="section-kicker">Session Intelligence</p>
          <h2>Visitor context</h2>
        </div>
      </div>
      <div className="session-grid">
        <SummaryGroup label="Interests" values={interests} />
        <SummaryGroup label="Pain points" values={painPoints} />
        <div className="summary-pair" data-testid="session-last-person">
          <span>Last person</span>
          <strong>{summary.last_person ?? "None"}</strong>
        </div>
        <div className="summary-pair" data-testid="session-last-feature">
          <span>Last feature</span>
          <strong>{summary.last_feature ?? "None"}</strong>
        </div>
        {summary.clarification_pending && (
          <div className="summary-pair wide" data-testid="session-clarification">
            <span>Clarifying</span>
            <strong>{summary.clarification_pending}</strong>
          </div>
        )}
      </div>
    </section>
  );
}

function SummaryGroup({ label, values }: { label: string; values: string[] }) {
  return (
    <div className="summary-pair">
      <span>{label}</span>
      <strong>{values.join(", ")}</strong>
    </div>
  );
}

function TraceItem({
  label,
  testId,
  value
}: {
  label: string;
  testId: string;
  value?: string;
}) {
  return (
    <div className="trace-item" data-testid={testId}>
      <dt>{label}</dt>
      <dd>{value ?? "Not detected"}</dd>
    </div>
  );
}

function ActionConsole({ runAction }: { runAction: (action: DemoAction) => void }) {
  return (
    <div className="action-console">
      <p className="section-kicker">Action Registry</p>
      <div className="action-grid">
        <button
          data-testid="action-open-cycles"
          onClick={() => runAction({ type: "OPEN_CYCLES" })}
          type="button"
        >
          Open Cycles
        </button>
        <button
          data-testid="action-open-issues"
          onClick={() => runAction({ type: "OPEN_ISSUES" })}
          type="button"
        >
          Open Issues
        </button>
        <button
          data-testid="action-open-demo-issue"
          onClick={() => runAction({ type: "OPEN_DEMO_ISSUE", payload: { issue_id: "LIN-142" } })}
          type="button"
        >
          Open Demo Issue
        </button>
        <button
          data-testid="action-create-demo-issue"
          onClick={() =>
            runAction({
              type: "CREATE_DEMO_ISSUE",
              payload: {
                id: "PIX-143",
                title: "Investigate login issue",
                priority: "Medium",
                assignee: "Maya Chen",
                project: "Issue Triage",
                status: "Todo"
              }
            })
          }
          type="button"
        >
          Create Demo Issue
        </button>
        <button
          data-testid="action-open-github-setup"
          onClick={() => runAction({ type: "OPEN_GITHUB_SETUP" })}
          type="button"
        >
          GitHub Setup
        </button>
        <button
          data-testid="action-highlight-assignee"
          onClick={() =>
            runAction({ type: "HIGHLIGHT_ASSIGNMENT_CONTROL", payload: { issue_id: "LIN-142" } })
          }
          type="button"
        >
          Highlight Assignee
        </button>
        <button
          data-testid="action-highlight-cycle"
          onClick={() => runAction({ type: "HIGHLIGHT_CYCLE_PROGRESS" })}
          type="button"
        >
          Highlight Cycle
        </button>
      </div>
    </div>
  );
}

function UiEventLog({ events }: { events: UiEvent[] }) {
  return (
    <div className="event-log-card" data-testid="event-log">
      <p className="section-kicker">UI Events</p>
      {events.length === 0 ? (
        <p className="empty-state">No actions executed yet.</p>
      ) : (
        <div className="event-list">
          {events.map((event) => (
            <article className="event-row" key={event.id}>
              <span className={event.status}>{event.status}</span>
              <div>
                <strong>{event.action_type}</strong>
                <p>{event.description}</p>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

function DiagnosticsPanel({
  events,
  runAction
}: {
  events: UiEvent[];
  runAction: (action: DemoAction) => void;
}) {
  return (
    <details className="diagnostics-panel" data-testid="diagnostics-panel">
      <summary>Developer diagnostics</summary>
      <ActionConsole runAction={runAction} />
      <UiEventLog events={events} />
    </details>
  );
}

function ConversationCard({
  agentServiceStatus,
  demoPathPrompts,
  demoPrompts,
  isSending,
  messages,
  onCollapse,
  onRestart,
  onReset,
  onRetryService,
  onSend,
  serviceError,
  sessionId,
  turnStatus
}: {
  agentServiceStatus: AgentServiceStatus;
  demoPathPrompts: string[];
  demoPrompts: string[];
  isSending: boolean;
  messages: TranscriptMessage[];
  onCollapse: () => void;
  onRestart: () => void;
  onReset: () => void;
  onRetryService: () => void;
  onSend: (message: string, inputMode?: InputMode) => Promise<AgentTurnResponse | null>;
  serviceError: string | null;
  sessionId: string;
  turnStatus: TurnStatus;
}) {
  const [draft, setDraft] = useState("");
  const [voiceEngineStatus, setVoiceEngineStatus] = useState<VoiceEngineStatus>("Idle");
  const [voiceEngineMode, setVoiceEngineMode] = useState<VoiceEngineMode>("connecting");
  const [spectrum, setSpectrum] = useState<SpectrumData>([15, 20, 15, 18, 12]);
  const [liveTranscript, setLiveTranscript] = useState("");
  const [voiceError, setVoiceError] = useState("");
  const [voiceFallbackNotice, setVoiceFallbackNotice] = useState("");
  const [isResetConfirmOpen, setIsResetConfirmOpen] = useState(false);
  // Replies are spoken only after the visitor turns voice on; neural speech is a paid call.
  const [isTTSEnabled, setIsTTSEnabled] = useState(false);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const voiceEngineRef = useRef<HybridVoiceEngine | null>(null);
  const onSendRef = useRef(onSend);
  const sessionIdRef = useRef(sessionId);
  const isTTSEnabledRef = useRef(isTTSEnabled);
  const mockVoiceTranscriptRef = useRef("");
  const mockVoiceTimerRef = useRef<number | null>(null);
  const isSubmittingVoiceRef = useRef(false);
  const isAgentSpeakingRef = useRef(false);
  const recentAgentRepliesRef = useRef<string[]>([]);
  const isVoiceActive = voiceEngineStatus !== "Idle" && voiceEngineStatus !== "Error";
  const isServiceReady = agentServiceStatus === "ready";

  useEffect(() => {
    onSendRef.current = onSend;
  }, [onSend]);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    isTTSEnabledRef.current = isTTSEnabled;
  }, [isTTSEnabled]);

  useEffect(() => {
    transcriptRef.current?.scrollTo({
      top: transcriptRef.current.scrollHeight,
      behavior: "smooth"
    });
  }, [messages, liveTranscript]);

  useEffect(() => {
    // Initialize Hybrid Voice Engine
    const engine = new HybridVoiceEngine({
      onStatusChange: (status, mode) => {
        setVoiceEngineStatus(status);
        setVoiceEngineMode((currentMode) => {
          if (mode !== "local") return mode;
          if (status === "Speaking" || status === "Error") return "local";
          return currentMode === "local" ? "azure" : currentMode;
        });
        if (status !== "Error") {
          setVoiceError("");
        }
        if (status === "Idle") {
          setLiveTranscript("");
        }
      },
      onSpectrumChange: (newSpectrum) => {
        setSpectrum(newSpectrum);
      },
      onUserTranscript: (text, isFinal) => {
        if (isAgentSpeakingRef.current || isSubmittingVoiceRef.current) return;

        // Discard a transcript only when it repeats what Edith just said: her voice coming back
        // through the microphone. Real questions that share her words must still go through.
        if (isEchoOfAgent(text, recentAgentRepliesRef.current)) {
          return;
        }

        setLiveTranscript(text);
        if (isFinal && text.trim()) {
          isSubmittingVoiceRef.current = true;
          setLiveTranscript("");
          engine.pauseListening();
          void (async () => {
            try {
              const result = await onSendRef.current(text, "voice");
              if (result?.speech) {
                isAgentSpeakingRef.current = true;
                await speakAgentReply(result.speech, "voice", () => {
                  isAgentSpeakingRef.current = false;
                  engine.stop();
                });
              } else {
                engine.stop();
              }
            } catch {
              engine.stop();
            } finally {
              isSubmittingVoiceRef.current = false;
              setLiveTranscript("");
            }
          })();
        }
      },
      onAgentSpeech: () => {
        isAgentSpeakingRef.current = true;
      },
      onError: (err) => {
        setVoiceError(err);
      },
      onCloudFallback: setVoiceFallbackNotice,
      productId: productConfig.id,
      getSessionId: () => sessionIdRef.current
    });

    voiceEngineRef.current = engine;
    if (isVoiceTestMode()) {
      const speechWindow = window as SpeechRecognitionWindow;
      speechWindow.__emitVoiceTranscript = (transcript: string) => {
        mockVoiceTranscriptRef.current = `${mockVoiceTranscriptRef.current} ${transcript}`.trim();
        setLiveTranscript(mockVoiceTranscriptRef.current);

        if (mockVoiceTimerRef.current !== null) {
          window.clearTimeout(mockVoiceTimerRef.current);
        }

        mockVoiceTimerRef.current = window.setTimeout(() => {
          const finalTranscript = mockVoiceTranscriptRef.current.trim();
          mockVoiceTranscriptRef.current = "";
          setLiveTranscript("");
          if (!finalTranscript) return;

          setVoiceEngineStatus("Thinking");
          void (async () => {
            const result = await onSendRef.current(finalTranscript, "voice");
            setVoiceEngineStatus(result?.speech ? "Speaking" : "Idle");
            if (result?.speech) {
              void speakAgentReply(result.speech, "voice", () => {
                setVoiceEngineStatus("Idle");
              });
            }
          })();
        }, getVoiceSilenceTimeoutMs());
      };
    }

    return () => {
      if (mockVoiceTimerRef.current !== null) {
        window.clearTimeout(mockVoiceTimerRef.current);
      }
      if (typeof window !== "undefined") {
        delete (window as SpeechRecognitionWindow).__emitVoiceTranscript;
      }
      engine.stop();
    };
  }, []);

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = draft;
    setDraft("");
    setLiveTranscript("");
    voiceEngineRef.current?.cancelSpeech();
    void (async () => {
      const result = await onSend(message, "text");
      if (result?.speech && isTTSEnabledRef.current) {
        void speakAgentReply(result.speech, "text");
      }
    })();
  }

  function speakAgentReply(
    speech: string,
    source: InputMode,
    onEnded?: () => void
  ): Promise<void> {
    // The two most recent replies are what the microphone could still be hearing.
    recentAgentRepliesRef.current = [speech, ...recentAgentRepliesRef.current].slice(0, 2);
    if (typeof window !== "undefined") {
      const speechWindow = window as SpeechRecognitionWindow;
      speechWindow.__spokenAgentReplies = [
        ...(speechWindow.__spokenAgentReplies ?? []),
        speech
      ];
    }

    if (source === "voice") {
      return voiceEngineRef.current?.speakLocalResponse(speech, onEnded, false) ?? Promise.resolve();
    }

    voiceEngineRef.current?.speakOnly(speech, onEnded);
    return Promise.resolve();
  }

  function toggleVoiceInput() {
    if (isVoiceTestMode()) {
      if (voiceEngineStatus === "Listening") {
        if (mockVoiceTimerRef.current !== null) {
          window.clearTimeout(mockVoiceTimerRef.current);
        }
        mockVoiceTranscriptRef.current = "";
        setLiveTranscript("");
        setVoiceEngineStatus("Idle");
        return;
      }

      setVoiceError("");
      setVoiceEngineStatus("Listening");
      setVoiceEngineMode("azure");
      return;
    }

    if (isVoiceActive) {
      voiceEngineRef.current?.stop();
    } else {
      isAgentSpeakingRef.current = false;
      voiceEngineRef.current?.cancelSpeech();
      setVoiceError("");
      void voiceEngineRef.current?.start();
    }
  }

  return (
    <section className="panel conversation-card" aria-busy={isSending}>
      <div className="conversation-topbar">
        <div className="guide-brand">
          <span className="guide-mark" aria-hidden="true">P</span>
          <strong>Pixel</strong>
        </div>
        <div className="conversation-actions">
          <button
            className="reset-button"
            data-testid="assistant-collapse"
            onClick={onCollapse}
            type="button"
          >
            Hide
          </button>
          <button
            className="reset-button"
            data-testid="restart-chat"
            disabled={!isServiceReady}
            onClick={onRestart}
            type="button"
          >
            Restart
          </button>
          <button
            className="reset-button"
            data-testid="reset-demo"
            disabled={!isServiceReady}
            onClick={() => setIsResetConfirmOpen(true)}
            type="button"
          >
            Reset
          </button>
          <span
            className={turnStatus === "Ready" ? "quiet-badge" : "quiet-badge active-status"}
            data-testid="turn-status"
          >
            {turnStatus}
          </span>
        </div>
      </div>

      {isResetConfirmOpen ? (
        <div className="demo-reset-backdrop" role="presentation">
          <div
            aria-describedby="demo-reset-description"
            aria-labelledby="demo-reset-title"
            aria-modal="true"
            className="demo-reset-dialog"
            data-testid="reset-demo-dialog"
            role="dialog"
          >
            <p className="section-kicker">Private demo</p>
            <h3 id="demo-reset-title">Restore the starting data?</h3>
            <p id="demo-reset-description">
              This clears tickets, projects, cycles, and members created in this demo. Other
              visitors are not affected.
            </p>
            <div className="demo-reset-actions">
              <button
                className="secondary-button compact"
                onClick={() => setIsResetConfirmOpen(false)}
                type="button"
              >
                Cancel
              </button>
              <button
                className="danger-button compact"
                data-testid="reset-demo-confirm"
                onClick={() => {
                  voiceEngineRef.current?.stop();
                  voiceEngineRef.current?.cancelSpeech();
                  setIsResetConfirmOpen(false);
                  onReset();
                }}
                type="button"
              >
                Reset demo
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <div className="guide-intro">
        <div className="guide-title-row">
          <div>
            <p className="section-kicker">Live Demo Guide</p>
            <h2>Edith</h2>
          </div>
          <span className={`voice-mode-badge ${voiceEngineMode}`}>
            {voiceModeLabel(voiceEngineMode)}
          </span>
        </div>
      </div>

      {!isServiceReady ? (
        <div className="service-readiness" data-testid="service-readiness" role="status">
          <strong>{agentServiceStatus === "checking" ? "Connecting" : "Service unavailable"}</strong>
          <span>
            {agentServiceStatus === "checking"
              ? "Checking the secure demo service."
              : serviceError ?? "Demo actions are paused until the service reconnects."}
          </span>
          {agentServiceStatus === "unavailable" ? (
            <button className="secondary-button compact" onClick={onRetryService} type="button">
              Retry
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="transcript" data-testid="transcript" ref={transcriptRef}>
        {messages.map((message, index) => (
          <article
            className={message.speaker === "Visitor" ? "message visitor-message" : "message"}
            key={`${message.speaker}-${index}`}
          >
            <span>{message.speaker}</span>
            <p>{message.text}</p>
            {message.note ? <p className="message-note">{message.note}</p> : null}
          </article>
        ))}
        {liveTranscript && (
          <article className="message visitor-message live-transcript-message">
            <span>Visitor (Speaking...)</span>
            <p>{liveTranscript}</p>
          </article>
        )}
      </div>

      <div className="demo-path" data-testid="demo-path" aria-label="Guided demo path">
        <div className="demo-path-header">
          <span>Guided demo path</span>
          <strong>{demoPathPrompts.length} steps</strong>
        </div>
        <div className="demo-path-list">
          {demoPathPrompts.map((prompt, index) => (
            <button
              data-testid={`demo-path-${index + 1}`}
              disabled={!isServiceReady}
              key={prompt}
              onClick={() => {
                setLiveTranscript("");
                voiceEngineRef.current?.cancelSpeech();
                void (async () => {
                  const result = await onSend(prompt, "text");
                  if (result?.speech && isTTSEnabledRef.current) {
                    void speakAgentReply(result.speech, "text");
                  }
                })();
              }}
              type="button"
            >
              <span>{index + 1}</span>
              {prompt}
            </button>
          ))}
        </div>
      </div>

      <div className="demo-prompt-strip" aria-label="Suggested demo turns">
        {demoPrompts.map((prompt) => (
          <button
            data-testid={`demo-prompt-${prompt.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}`}
            disabled={!isServiceReady}
            key={prompt}
            onClick={() => {
              setLiveTranscript("");
              voiceEngineRef.current?.cancelSpeech();
              void (async () => {
                const result = await onSend(prompt, "text");
                if (result?.speech && isTTSEnabledRef.current) {
                  void speakAgentReply(result.speech, "text");
                }
              })();
            }}
            type="button"
          >
            {prompt}
          </button>
        ))}
      </div>

      <form className="chat-form" onSubmit={handleSubmit}>
        <span className="input-mark" aria-hidden="true">P</span>
        <input
          data-testid="chat-input"
          disabled={!isServiceReady}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={isServiceReady ? "Ask Edith" : "Demo service unavailable"}
          suppressHydrationWarning
          value={draft}
        />
        <button data-testid="chat-send" disabled={!isServiceReady || !draft.trim()} type="submit">
          Send
        </button>
      </form>

      {/* Voice Control Card */}
      <div className={`voice-control-card ${isVoiceActive ? "active" : ""}`}>
        <div className="voice-control-top">
          <button
            aria-pressed={isVoiceActive}
            className={`voice-toggle-btn ${isVoiceActive ? "active" : ""}`}
            data-testid="voice-toggle"
            disabled={!isServiceReady}
            onClick={toggleVoiceInput}
            type="button"
          >
            <span className={`voice-dot ${voiceEngineStatus.toLowerCase()}`} />
            {isVoiceActive ? "End Voice" : "Start Voice"}
          </button>

          <button
            aria-pressed={isTTSEnabled}
            className={`tts-toggle-btn ${isTTSEnabled ? "active" : ""}`}
            data-testid="tts-toggle"
            disabled={!isServiceReady}
            onClick={() => {
              const nextState = !isTTSEnabled;
              setIsTTSEnabled(nextState);
              if (!nextState) {
                voiceEngineRef.current?.cancelSpeech();
                setVoiceFallbackNotice("");
              }
            }}
            title={isTTSEnabled ? "Mute agent voice" : "Unmute agent voice"}
            type="button"
          >
            {isTTSEnabled ? "Voice On" : "Voice Off"}
          </button>
        </div>

        <div className="voice-status-line">
          <span className="voice-status-label" data-testid="voice-status">
            Voice: <strong>{voiceStatusLabel(voiceEngineStatus)}</strong>
          </span>

          {/* 5-Bar Audio Equalizer Spectrum Visualizer */}
          <div className="voice-spectrum-bar" aria-label="Audio Spectrum Visualizer">
            {spectrum.map((height, i) => (
              <span
                className={`spectrum-bar ${isVoiceActive ? "animating" : ""}`}
                key={i}
                style={{ height: `${isVoiceActive ? height : 15}%` }}
              />
            ))}
          </div>
        </div>
        {voiceFallbackNotice ? (
          <p className="voice-fallback-text" data-testid="voice-fallback" role="status">
            {voiceFallbackNotice}
          </p>
        ) : null}
        {voiceError ? (
          <p className="voice-error-text" role="alert">{voiceError}</p>
        ) : null}
      </div>
    </section>
  );
}

function getSpeechRecognitionConstructor(): SpeechRecognitionConstructor | null {
  if (typeof window === "undefined") return null;
  const speechWindow = window as SpeechRecognitionWindow;
  return speechWindow.SpeechRecognition ?? speechWindow.webkitSpeechRecognition ?? null;
}

function getVoiceSilenceTimeoutMs(): number {
  if (typeof window === "undefined") return DEFAULT_VOICE_SILENCE_TIMEOUT_MS;
  const configuredTimeout = (window as SpeechRecognitionWindow).__demoVoiceSilenceTimeoutMs;
  return typeof configuredTimeout === "number"
    ? configuredTimeout
    : DEFAULT_VOICE_SILENCE_TIMEOUT_MS;
}

function isVoiceTestMode(): boolean {
  return (
    typeof window !== "undefined"
    && typeof (window as SpeechRecognitionWindow).__demoVoiceSilenceTimeoutMs === "number"
  );
}


function savedWorkspaceEvent(
  event: UiEvent,
  workspaceName: string,
  action: DemoAction,
  issues: DemoIssue[]
): UiEvent {
  if (event.status !== "executed") return event;

  if (action.type === "CREATE_DEMO_ISSUE") {
    return {
      ...event,
      description: `Saved to ${workspaceName}: created ${"id" in action.payload ? action.payload.id : "a ticket"} for ${action.payload.assignee}.`
    };
  }

  if (action.type === "UPDATE_DEMO_ISSUE") {
    const issue = issues.find((item) => item.id === action.payload.issue_id);
    return {
      ...event,
      description: issue
        ? `Saved to ${workspaceName}: updated issue ${issue.id} (${issue.assignee}, ${issue.priority}, ${issue.status}).`
        : `Saved to ${workspaceName}: updated issue ${action.payload.issue_id}.`
    };
  }

  return event;
}

function voiceStatusLabel(status: VoiceEngineStatus): string {
  if (status === "Idle") return "Ready";
  if (status === "Preparing") return "Preparing voice";
  if (status === "Error") return "Unavailable";
  return status;
}

function voiceControlTitle(status: VoiceStatus): string {
  if (status === "Listening") return "Stop listening";
  if (status === "Speaking") return "Stop agent voice";
  if (status === "Preparing") return "Preparing voice";
  if (status === "Processing") return "Processing voice input";
  return "Start voice input";
}

function upsertIssue(issues: DemoIssue[], issue: DemoIssue): DemoIssue[] {
  const existingIssueIndex = issues.findIndex((item) => item.id === issue.id);
  if (existingIssueIndex === -1) {
    return [issue, ...issues];
  }

  return issues.map((item, index) => (index === existingIssueIndex ? issue : item));
}

function applyIssueUpdate(
  issues: DemoIssue[],
  update: {
    issue_id: string;
    assignee?: string;
    priority?: DemoIssue["priority"];
    status?: string;
  }
): DemoIssue[] {
  return issues.map((issue) =>
    issue.id === update.issue_id
      ? {
          ...issue,
          assignee: update.assignee ?? issue.assignee,
          priority: update.priority ?? issue.priority,
          status: update.status ?? issue.status
        }
      : issue
  );
}

function filterIssuesByScope(
  issues: DemoIssue[],
  workspaceScope: DemoWorkspaceScope
): DemoIssue[] {
  return issues.filter((issue) => isIssueInScope(issue, workspaceScope));
}

function filterTeamByScope(
  team: DemoTeamMember[],
  workspaceScope: DemoWorkspaceScope,
  scopedIssues: DemoIssue[]
): DemoTeamMember[] {
  const scopedAssignees = new Set(scopedIssues.map((issue) => issue.assignee));
  return team.filter((member) => {
    const projectIds = member.projectIds ?? [];
    return (
      projectIds.some((projectId) => workspaceScope.allowedProjectIds.includes(projectId))
      || scopedAssignees.has(member.name)
    );
  });
}

function isIssueInScope(issue: DemoIssue, workspaceScope: DemoWorkspaceScope): boolean {
  if (issue.projectId) {
    return workspaceScope.allowedProjectIds.includes(issue.projectId);
  }

  return workspaceScope.allowedIssueProjects.includes(issue.project);
}

function isCycleInScope(cycle: DemoCycle, workspaceScope: DemoWorkspaceScope): boolean {
  return !cycle.projectId || workspaceScope.allowedProjectIds.includes(cycle.projectId);
}

function withScopedIssue(
  issue: DemoIssue,
  scopedProjects: DemoProject[],
  workspaceScope: DemoWorkspaceScope
): DemoIssue {
  const matchingProject = scopedProjects.find((project) => project.name === issue.project);
  return {
    ...issue,
    projectId: issue.projectId ?? matchingProject?.id ?? workspaceScope.allowedProjectIds[0],
    project: issue.project || workspaceScope.allowedIssueProjects[0]
  };
}

function addProjectToScope(
  workspaceScope: DemoWorkspaceScope,
  project: DemoProject
): DemoWorkspaceScope {
  return {
    ...workspaceScope,
    allowedProjectIds: workspaceScope.allowedProjectIds.includes(project.id)
      ? workspaceScope.allowedProjectIds
      : [...workspaceScope.allowedProjectIds, project.id],
    allowedIssueProjects: workspaceScope.allowedIssueProjects.includes(project.name)
      ? workspaceScope.allowedIssueProjects
      : [...workspaceScope.allowedIssueProjects, project.name]
  };
}

function asksForTicketCreation(text: string): boolean {
  const mentionsWorkItem = /\b(ticket|issue|bug)\b/.test(text);
  if (!mentionsWorkItem) return false;
  return (
    /\b(create|make|add|raise|file)\b/.test(text)
    || /\b(open|start|draft)\b/.test(text) && /\b(new|fresh)\b/.test(text)
  );
}

function extractPriority(normalizedMessage: string): DemoIssue["priority"] {
  if (/\b(urgent|critical|high)\b/.test(normalizedMessage)) return "High";
  if (/\blow\b/.test(normalizedMessage)) return "Low";
  return "Medium";
}

function extractIssueTitle(message: string): string {
  const explicitTitle = message.match(/\b(?:about|regarding|named|called)\s+(.+)$/i);
  if (explicitTitle?.[1]) {
    return titleCase(explicitTitle[1].trim());
  }

  const normalizedMessage = normalizeText(message);
  if (normalizedMessage.includes("login") || normalizedMessage.includes("sign in")) {
    return "Investigate login issue";
  }
  if (normalizedMessage.includes("github")) return "Review GitHub sync issue";
  if (normalizedMessage.includes("webhook")) return "Investigate webhook issue";
  if (normalizedMessage.includes("bug")) return "Investigate reported bug";
  return "Investigate customer onboarding issue";
}

function initialsForName(name: string): string {
  return (
    name
      .trim()
      .split(/\s+/)
      .map((part) => part[0])
      .join("")
      .toUpperCase()
      .slice(0, 2) || ""
  );
}

function emailForName(name: string): string {
  const handle = normalizeText(name).replace(/\s+/g, ".");
  return handle ? `${handle}@pixel.demo` : "";
}

function normalizeText(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
}

function titleCase(value: string): string {
  return value
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => `${word.charAt(0).toUpperCase()}${word.slice(1).toLowerCase()}`)
    .join(" ");
}

function nextDemoIssueId(issues: DemoIssue[]): string {
  const nextNumber =
    Math.max(
      ...issues
        .map((issue) => Number(issue.id.match(/\d+/)?.[0]))
        .filter(Number.isFinite),
      142
    ) + 1;
  return `PIX-${nextNumber}`;
}

function nextDemoIssue(
  issues: DemoIssue[],
  input: { assignee: string; title: string; priority?: DemoIssue["priority"] }
): DemoIssue {
  const nextNumber =
    Math.max(
      ...issues
        .map((issue) => Number(issue.id.match(/\d+/)?.[0]))
        .filter(Number.isFinite),
      142
    ) + 1;

  return {
    id: `PIX-${nextNumber}`,
    title: input.title,
    priority: input.priority ?? "Medium",
    assignee: input.assignee,
    project: "Issue Triage",
    status: "Todo"
  };
}
