/**
 * The console's connection to the running Pixel API.
 *
 * The console was built on synthetic data so it could be designed before there was anything to
 * connect it to. It still runs that way: without `NEXT_PUBLIC_PIXEL_API_BASE_URL` set, nothing
 * here makes a request and the console shows its own sample organization exactly as before.
 * With it set, the products the console lists are the products the organization really has.
 *
 * Nothing in this file decides what anybody may see. The API answers for the signed-in caller
 * and refuses what they may not reach; the console only displays what comes back.
 */

export interface ApiProduct {
  product_id: string;
  name: string;
  definition_id: string;
  definition_version: number;
  state: string;
  visitor_access: boolean;
  entities: string[];
  views: string[];
}

export interface ApiFieldShape {
  name: string;
  label: string;
  type: string;
  required: boolean;
  editable: boolean;
  display: boolean;
  values: string[];
  target: string | null;
}

export interface ApiEntityShape {
  name: string;
  label: string;
  plural: string;
  title_field: string;
  summary_fields: string[];
  fields: ApiFieldShape[];
}

export interface ApiViewShape {
  name: string;
  label: string;
  kind: string;
  entity: string | null;
  shortcut: string | null;
  navigable: boolean;
  columns: string[];
  controls: Array<{ name: string; label: string }>;
}

export interface ApiActionShape {
  name: string;
  client_type: string;
  capability: string;
  description: string;
  entity: string | null;
  view: string | null;
  fields: string[];
}

export interface ApiProductShape {
  product_id: string;
  product_name: string;
  assistant_name: string;
  definition_id: string;
  definition_version: number;
  views: ApiViewShape[];
  entities: ApiEntityShape[];
  actions: ApiActionShape[];
}

export interface ApiRecords {
  product_id: string;
  scope: string;
  records: Record<string, Array<Record<string, unknown> & { id: string; title?: string }>>;
}

export interface ApiTurnResponse {
  session_id: string;
  turn_id: number;
  status: "completed" | "cancelled" | "stale" | "denied";
  speech: string;
  validated_action: { type: string; payload: Record<string, unknown> } | null;
  execution: { key: string; session_id: string; turn_id: number; expires_at: string } | null;
}

export interface ApiExecutionReceipt {
  outcome: "executed" | "failed" | "refused";
  code: string;
  speech: string;
  record: Record<string, unknown> | null;
}

export interface ApiSession {
  token: string;
  userId: string;
  tenantId: string;
}

export class ApiError extends Error {}

const TOKEN_KEY = "pixel.console.session";

export function apiBaseUrl(): string | null {
  const configured = process.env.NEXT_PUBLIC_PIXEL_API_BASE_URL?.trim();
  return configured ? configured.replace(/\/$/, "") : null;
}

/** Whether this console is pointed at a running Pixel. */
export function isLive(): boolean {
  return apiBaseUrl() !== null;
}

export function storedSession(): ApiSession | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(TOKEN_KEY);
    return raw ? (JSON.parse(raw) as ApiSession) : null;
  } catch {
    // A browser that refuses storage simply signs in again; it is never a reason to fail.
    return null;
  }
}

function remember(session: ApiSession | null): void {
  try {
    if (session) window.sessionStorage.setItem(TOKEN_KEY, JSON.stringify(session));
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* nothing kept; the session lasts as long as the page does */
  }
}

async function call<T>(path: string, init: RequestInit = {}, session?: ApiSession | null): Promise<T> {
  const base = apiBaseUrl();
  if (!base) throw new ApiError("This console is not connected to a Pixel API.");
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(session ? { Authorization: `Bearer ${session.token}` } : {}),
      ...(init.headers ?? {}),
    },
  });
  const body = await response.text();
  if (!response.ok) {
    let detail = body;
    try {
      detail = (JSON.parse(body) as { detail?: string }).detail ?? body;
    } catch {
      /* the body was not JSON; show it as it came */
    }
    throw new ApiError(detail || `Request failed (${response.status})`);
  }
  return body ? (JSON.parse(body) as T) : ({} as T);
}

/**
 * Sign in for development. This is the API's own demo login, which is off unless a deployment
 * turns it on, and it is not how people will sign in to Pixel: real accounts arrive with the
 * identity work. It exists so the console can be driven against a running API today.
 */
export async function signIn(userId: string): Promise<ApiSession> {
  const answer = await call<{ token: string; user_id: string; tenant_id: string }>(
    "/auth/demo-login", { method: "POST", body: JSON.stringify({ user_id: userId }) },
  );
  const session = { token: answer.token, userId: answer.user_id, tenantId: answer.tenant_id };
  remember(session);
  return session;
}

export function signOut(): void {
  remember(null);
}

export async function listProducts(session: ApiSession): Promise<ApiProduct[]> {
  const answer = await call<{ products: ApiProduct[] }>(
    `/organizations/${encodeURIComponent(session.tenantId)}/products`, {}, session,
  );
  return answer.products;
}

export async function addProduct(session: ApiSession, product: {
  productId: string; teamId: string; definitionId: string; definition: string; version?: number;
}): Promise<ApiProduct> {
  return call<ApiProduct>(
    `/organizations/${encodeURIComponent(session.tenantId)}/products`,
    {
      method: "POST",
      body: JSON.stringify({
        product_id: product.productId,
        team_id: product.teamId,
        definition_id: product.definitionId,
        definition: product.definition,
        definition_version: product.version ?? 1,
      }),
    },
    session,
  );
}

export async function productShape(session: ApiSession, productId: string): Promise<ApiProductShape> {
  return call<ApiProductShape>(`/products/${encodeURIComponent(productId)}/shape`, {}, session);
}

export async function productRecords(session: ApiSession, productId: string): Promise<ApiRecords> {
  return call<ApiRecords>(`/products/${encodeURIComponent(productId)}/records`, {}, session);
}

export async function sendProductTurn(session: ApiSession, input: {
  sessionId: string;
  turnId: number;
  productId: string;
  message: string;
}): Promise<ApiTurnResponse> {
  return call<ApiTurnResponse>("/turn", {
    method: "POST",
    body: JSON.stringify({
      session_id: input.sessionId,
      turn_id: input.turnId,
      product_id: input.productId,
      message: input.message,
      input_mode: "text",
      current_page: "console",
      workspace_scope_id: "primary",
    }),
  }, session);
}

export async function executeProductAction(session: ApiSession, input: {
  productId: string;
  entity: string;
  action: string;
  payload: Record<string, unknown>;
  executionKey: string;
  sessionId: string;
}): Promise<ApiExecutionReceipt> {
  const headers = { "X-Execution-Key": input.executionKey, "X-Session-Id": input.sessionId };
  const recordId = typeof input.payload.record_id === "string" ? input.payload.record_id : null;
  const body = recordId
    ? { action: input.action, changes: withoutKey(input.payload, "record_id") }
    : { action: input.action, fields: input.payload };
  const path = recordId
    ? `/products/${encodeURIComponent(input.productId)}/records/${encodeURIComponent(input.entity)}/${encodeURIComponent(recordId)}`
    : `/products/${encodeURIComponent(input.productId)}/records/${encodeURIComponent(input.entity)}`;
  return call<ApiExecutionReceipt>(path, {
    method: recordId ? "PATCH" : "POST",
    headers,
    body: JSON.stringify(body),
  }, session);
}

function withoutKey(source: Record<string, unknown>, key: string): Record<string, unknown> {
  const result = { ...source };
  delete result[key];
  return result;
}
