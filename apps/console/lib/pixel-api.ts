/**
 * The console's connection to the running Pixel API.
 *
 * The console talks to Pixel through its same-origin `/api/agent` proxy. That keeps the browser
 * session in HttpOnly cookies owned by this app instead of exposing a bearer token to JavaScript
 * or depending on third-party cookies.
 *
 * Nothing in this file decides what anybody may see. The API answers for the signed-in caller
 * and refuses what they may not reach; the console only displays what comes back.
 */

export interface ApiProduct {
  team_id?: string;
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
  by?: string | null;
  control?: string | null;
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

/** One record as a screen has it, including the version an edit must be made against. */
export type ApiRecord = Record<string, unknown> & { id: string; title?: string; revision: number };

export interface ApiRecords {
  product_id: string;
  scope: string;
  records: Record<string, ApiRecord[]>;
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
  csrfToken: string;
  userId: string;
  tenantId: string;
}

export class ApiError extends Error {
  /** The refusal as the server sent it, when it was more than a sentence. */
  readonly detail: unknown;

  constructor(message: string, detail?: unknown) {
    super(message);
    this.detail = detail;
  }
}

/**
 * Somebody else changed the record first.
 *
 * The record as it stands now comes back with the refusal, so the person can be shown what
 * changed and decide, rather than being told only that they were too late.
 */
export class RecordConflictError extends ApiError {
  constructor(message: string, readonly current: ApiRecord | null) {
    super(message);
  }
}

export interface ApiAccount {
  user_id: string; email: string | null; tenant_id: string; organization_name: string;
  role: "org_admin" | "team_admin" | "team_member"; team_id: string | null;
  /** The product that answers requests about Pixel itself; null when none is configured. */
  console_product_id: string | null;
  teams: Array<{ team_id: string; name: string }>;
}

export async function requestEmailCode(email: string): Promise<{ challenge_id: string }> {
  return call("/account/email-code", { method: "POST", body: JSON.stringify({ email }) });
}

export async function verifyEmailCode(challengeId: string, code: string): Promise<ApiSession> {
  const answer = await call<{ csrf_token: string; user_id: string; tenant_id: string }>(
    "/account/verify-code", { method: "POST", body: JSON.stringify({ challenge_id: challengeId, code }) },
  );
  const session = { csrfToken: answer.csrf_token, userId: answer.user_id, tenantId: answer.tenant_id };
  remember(session);
  return session;
}

export async function currentAccount(session: ApiSession): Promise<ApiAccount> {
  return call("/account/session", {}, session);
}

export interface ApiKnowledge { version: number; documents: Array<{ document_id: string; title: string; characters: number }> }
export async function productKnowledge(session: ApiSession, productId: string): Promise<ApiKnowledge> {
  return call(`/products/${encodeURIComponent(productId)}/knowledge`, {}, session);
}
export async function approveKnowledge(session: ApiSession, productId: string, title: string, text: string): Promise<void> {
  await call(`/products/${encodeURIComponent(productId)}/knowledge`, {
    method: "POST", body: JSON.stringify({ title, text, approved: true }),
  }, session);
}

export async function endSession(): Promise<void> {
  const session = storedSession();
  if (session) await call("/account/logout", { method: "POST" }, session);
  remember(null);
}

const TOKEN_KEY = "pixel.console.session";

export function apiBaseUrl(): string | null {
  return "/api/agent";
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
  const method = (init.method ?? "GET").toUpperCase();
  const response = await fetch(`${base}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(session && !["GET", "HEAD", "OPTIONS"].includes(method) ? { "X-Pixel-CSRF": session.csrfToken } : {}),
      ...(init.headers ?? {}),
    },
  });
  const body = await response.text();
  if (!response.ok) {
    let detail: unknown = body;
    try {
      detail = (JSON.parse(body) as { detail?: unknown }).detail ?? body;
    } catch {
      /* the body was not JSON; show it as it came */
    }
    if (detail && typeof detail === "object" && "message" in detail) {
      const structured = detail as { message: string; record?: ApiRecord | null };
      if (response.status === 409) throw new RecordConflictError(structured.message, structured.record ?? null);
      throw new ApiError(structured.message, detail);
    }
    throw new ApiError(typeof detail === "string" && detail ? detail : `Request failed (${response.status})`, detail);
  }
  return body ? (JSON.parse(body) as T) : ({} as T);
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

/**
 * Create one record from values the person typed.
 *
 * No execution key: nothing proposed this, so there is nothing to bind it to. The server decides
 * whether they may put a record where this one lands, and the product's own definition decides
 * whether the values are allowed.
 */
export async function createProductRecord(session: ApiSession, productId: string, entity: string,
                                          fields: Record<string, unknown>): Promise<ApiRecord> {
  return call<ApiRecord>(
    `/products/${encodeURIComponent(productId)}/records/${encodeURIComponent(entity)}`,
    { method: "POST", body: JSON.stringify({ fields }) }, session,
  );
}

/**
 * Change one record from values the person typed, against the version they were shown.
 *
 * Throws `RecordConflictError` when that version has moved on, carrying the record as it is now.
 */
export async function editProductRecord(session: ApiSession, productId: string, entity: string,
                                        recordId: string, changes: Record<string, unknown>,
                                        revision: number): Promise<ApiRecord> {
  return call<ApiRecord>(
    `/products/${encodeURIComponent(productId)}/records/${encodeURIComponent(entity)}/${encodeURIComponent(recordId)}`,
    { method: "PUT", body: JSON.stringify({ changes, revision }) }, session,
  );
}

export async function sendProductTurn(session: ApiSession, input: {
  sessionId: string;
  turnId: number;
  productId: string;
  message: string;
  currentPage?: string | null;
  selectedRecordId?: string | null;
  signal?: AbortSignal;
}): Promise<ApiTurnResponse> {
  return call<ApiTurnResponse>("/turn", {
    method: "POST",
    signal: input.signal,
    body: JSON.stringify({
      session_id: input.sessionId,
      turn_id: input.turnId,
      product_id: input.productId,
      message: input.message,
      input_mode: "text",
      ...(input.currentPage ? { current_page: input.currentPage } : {}),
      ...(input.selectedRecordId ? { selected_issue_id: input.selectedRecordId } : {}),
      workspace_scope_id: "primary",
    }),
  }, session);
}

export async function productSpeech(session: ApiSession, productId: string, sessionId: string, text: string, signal: AbortSignal) {
  const response = await fetch(`${apiBaseUrl()}/speech`, {
    method: "POST", signal,
    credentials: "include",
    headers: { "Content-Type": "application/json", "X-Pixel-CSRF": session.csrfToken },
    body: JSON.stringify({ product_id: productId, session_id: sessionId, text }),
  });
  if (!response.ok) throw new ApiError(response.status === 429 ? "Voice limit reached. Text remains available." : "Voice is unavailable. Text remains available.");
  return { audio: await response.blob(), provider: response.headers.get("X-TTS-Engine") ?? "Cloud voice" };
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

export interface DraftThingField {
  name: string;
  type: "text" | "integer" | "enum" | "date" | "boolean";
  required?: boolean;
  values?: string[];
}

export interface DraftThing {
  name: string;
  label: string;
  plural: string;
  people?: boolean;
  fields: DraftThingField[];
}

export interface ApiDraftedDefinition {
  definition_id: string;
  product_name: string;
  definition: string;
  things: string[];
  screens: string[];
  can_do: string[];
}

/**
 * Ask Pixel to write a definition from a description of a product. Nothing is stored and no
 * product is created: this is what Pixel understood, for the person who described it to read
 * before anything runs on it.
 */
export async function productDraft(session: ApiSession, draft: {
  productName: string; assistantName: string; definitionId: string; things: DraftThing[];
}): Promise<ApiDraftedDefinition> {
  return call<ApiDraftedDefinition>("/product-drafts", {
    method: "POST",
    body: JSON.stringify({
      product_name: draft.productName,
      assistant_name: draft.assistantName,
      definition_id: draft.definitionId,
      things: draft.things.map((thing) => ({
        name: thing.name, label: thing.label, plural: thing.plural,
        people: thing.people ?? false,
        fields: thing.fields.map((field) => ({
          name: field.name, type: field.type, required: field.required ?? false,
          values: field.type === "enum" ? field.values ?? [] : [],
        })),
      })),
    }),
  }, session);
}

/**
 * End a conversation and withdraw anything it proposed but nobody carried out.
 *
 * Called when the assistant panel goes away - leaving a product, or switching to another - so a
 * proposal made about one product cannot be presented after somebody has moved on from it.
 */
export async function closeConversation(session: ApiSession, sessionId: string): Promise<void> {
  try {
    await call(`/conversations/${encodeURIComponent(sessionId)}/close`, { method: "POST" }, session);
  } catch {
    /* The keys expire on their own; failing to tidy up is never worth an error on the way out. */
  }
}
