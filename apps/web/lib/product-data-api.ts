import type {
  DemoAction,
  DemoCycle,
  DemoDataResponse,
  DemoIssue,
  DemoProject,
  DemoTeamMember
} from "@/types/demo";
import { productConfig } from "@/lib/product-config";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL
  ?? (typeof window === "undefined" ? "http://127.0.0.1:8001/api" : "/api/agent");

const AUTH_STORAGE_KEY = `pixel_demo_auth:${productConfig.tenantId}:${productConfig.id}:v2`;

/** A record write the server rejected. Its message is safe to show to the user. */
export class RecordSaveError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RecordSaveError";
  }
}

// --- Demo auth token management ---

let _authToken: string | null = null;
let _loginPromise: Promise<void> | null = null;

export function getAuthToken(): string | null {
  if (!_authToken && typeof window !== "undefined") {
    try {
      _authToken = window.sessionStorage?.getItem(AUTH_STORAGE_KEY);
    } catch {
      // sessionStorage not accessible
    }
  }
  return _authToken;
}

export function setAuthToken(token: string | null): void {
  _authToken = token;
  if (typeof window !== "undefined") {
    try {
      if (token) {
        window.sessionStorage?.setItem(AUTH_STORAGE_KEY, token);
      } else {
        window.sessionStorage?.removeItem(AUTH_STORAGE_KEY);
      }
    } catch {
      // sessionStorage not accessible
    }
  }
}

/**
 * The API refused a request because too many arrived too quickly. Nothing was done, and the
 * service is healthy: the visitor only needs to wait, so this must never read as an outage.
 */
export class RateLimitedError extends Error {
  constructor() {
    super("Edith is receiving a lot of requests right now. Please wait a moment and try again.");
    this.name = "RateLimitedError";
  }
}

export async function ensureDemoLogin(): Promise<void> {
  if (getAuthToken()) return;
  if (_loginPromise) return _loginPromise;
  _loginPromise = (async () => {
    try {
      const response = await fetch(apiUrl(
        `/organizations/${encodeURIComponent(productConfig.tenantId)}`
        + `/products/${encodeURIComponent(productConfig.id)}/visitor-sessions`
      ), {
        method: "POST",
        headers: { "Content-Type": "application/json" }
      });
      if (response.status === 429) throw new RateLimitedError();
      if (!response.ok) {
        throw new Error("Demo login failed. Is the backend running?");
      }
      const body = (await response.json()) as { token: string };
      setAuthToken(body.token);
    } finally {
      _loginPromise = null;
    }
  })();
  return _loginPromise;
}

/**
 * fetch with the current bearer token. A 401 means the API restarted or the token
 * expired, so sign in again once and retry; idempotency keys make retried writes safe.
 */
export async function authorizedFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const send = () => fetch(url, {
    ...init,
    headers: { ...(init.headers as Record<string, string> | undefined), ...authHeaders() }
  });
  // Sign in before the first request, so calls made during startup are not rejected.
  if (!getAuthToken()) await ensureDemoLogin();
  const response = await send();
  if (response.status !== 401) return response;
  setAuthToken(null);
  await ensureDemoLogin();
  return send();
}

export function authHeaders(): Record<string, string> {
  const token = getAuthToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// --- Data API ---

export async function loadDemoData(): Promise<DemoDataResponse> {
  const response = await authorizedFetch(apiUrl("/demo-data"), {
    cache: "no-store"
  });
  return parseJsonResponse<DemoDataResponse>(response);
}

export type ExecutionEnvelope = {
  key: string;
  session_id: string;
  turn_id: number;
  expires_at: string;
};

export type ExecutionReceipt = {
  outcome: "executed" | "refused";
  code: string;
  speech: string;
  record: DemoIssue | null;
};

type PrivateDemoResetResponse = {
  token: string;
  instance_id: string;
  generation: number;
  data: DemoDataResponse;
};

export async function resetPrivateDemo(): Promise<DemoDataResponse> {
  const response = await authorizedFetch(apiUrl("/demo-data/reset-mine"), {
    method: "POST",
    headers: jsonHeaders()
  });
  const body = await parseJsonResponse<PrivateDemoResetResponse>(response);
  // Reset increments the private generation. Store its rotated token before any later request.
  setAuthToken(body.token);
  return body.data;
}

export async function saveStoredIssue(issue: DemoIssue, requestKey?: string): Promise<DemoIssue> {
  const response = await authorizedFetch(apiUrl("/demo-data/issues"), {
    method: "POST",
    headers: jsonHeaders(requestKey),
    body: JSON.stringify(issue)
  });
  return parseJsonResponse<DemoIssue>(response);
}

export async function updateStoredIssue(issue: DemoIssue): Promise<DemoIssue> {
  const response = await authorizedFetch(apiUrl(`/demo-data/issues/${encodeURIComponent(issue.id)}`), {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify(issue)
  });
  return parseJsonResponse<DemoIssue>(response);
}

export async function applyKeyedChange(
  action: Extract<DemoAction, { type: "UPDATE_DEMO_ISSUE" | "CREATE_DEMO_ISSUE" }>,
  envelope: ExecutionEnvelope
): Promise<ExecutionReceipt | null> {
  const request = keyedIssueRequest(action);
  const response = await authorizedFetch(request.url, {
    method: request.method,
    headers: {
      "Content-Type": "application/json",
      "X-Execution-Key": envelope.key,
      "X-Session-Id": envelope.session_id
    },
    body: JSON.stringify(request.body)
  });

  if (response.status === 404) return null;
  if (response.status === 409) return response.json() as Promise<ExecutionReceipt>;
  return parseJsonResponse<ExecutionReceipt>(response);
}

export async function saveStoredProject(
  project: DemoProject,
  workspaceScopeId: string,
  requestKey?: string
): Promise<DemoProject> {
  const response = await authorizedFetch(
    apiUrl(`/demo-data/projects?workspace_scope_id=${encodeURIComponent(workspaceScopeId)}`),
    {
      method: "POST",
      headers: jsonHeaders(requestKey),
      body: JSON.stringify(project)
    }
  );
  return parseJsonResponse<DemoProject>(response);
}

export async function saveStoredCycle(cycle: DemoCycle, requestKey?: string): Promise<DemoCycle> {
  const response = await authorizedFetch(apiUrl("/demo-data/cycles"), {
    method: "POST",
    headers: jsonHeaders(requestKey),
    body: JSON.stringify(cycle)
  });
  return parseJsonResponse<DemoCycle>(response);
}

export async function saveStoredTeamMember(
  member: DemoTeamMember,
  workspaceScopeId: string,
  requestKey?: string
): Promise<DemoTeamMember> {
  const response = await authorizedFetch(
    apiUrl(`/demo-data/team-members?workspace_scope_id=${encodeURIComponent(workspaceScopeId)}`),
    {
      method: "POST",
      headers: jsonHeaders(requestKey),
      body: JSON.stringify(member)
    }
  );
  return parseJsonResponse<DemoTeamMember>(response);
}

export function apiUrl(path: string): string {
  const normalizedBase = API_BASE_URL.replace(/\/$/, "");
  return `${normalizedBase}${path}`;
}

function jsonHeaders(requestKey?: string): Record<string, string> {
  return {
    "Content-Type": "application/json",
    ...(requestKey ? { "Idempotency-Key": requestKey } : {})
  };
}

function keyedIssueRequest(
  action: Extract<DemoAction, { type: "UPDATE_DEMO_ISSUE" | "CREATE_DEMO_ISSUE" }>
): { url: string; method: "PATCH" | "POST"; body: unknown } {
  if (action.type === "CREATE_DEMO_ISSUE") {
    return {
      url: apiUrl("/demo-data/issues"),
      method: "POST",
      body: { fields: action.payload }
    };
  }
  const { issue_id: issueId, ...changes } = action.payload;
  const boundChanges = Object.fromEntries(
    Object.entries(changes).filter(([, value]) => value !== undefined)
  );
  return {
    url: apiUrl(`/demo-data/issues/${encodeURIComponent(issueId)}`),
    method: "PATCH",
    body: { changes: boundChanges }
  };
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === "string" ? body.detail
      : response.status === 422 ? "Check the required fields and dates, then try again."
      : "Could not save your changes. Your draft is still here; please try again.";
    throw new RecordSaveError(detail);
  }
  return response.json() as Promise<T>;
}
