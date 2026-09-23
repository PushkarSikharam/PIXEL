const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL
  ?? (typeof window === "undefined" ? "http://127.0.0.1:8001/api" : "/api/agent");

const TOKEN_KEY = "pixel_system_console_token";
const USER_KEY = "pixel_system_console_user";
const TENANT_KEY = "pixel_system_console_tenant";
const DEFAULT_CONSOLE_USER = process.env.NEXT_PUBLIC_PIXEL_CONSOLE_USER ?? "demo-product-eng";

export type ConsoleSession = {
  token: string;
  userId: string;
  tenantId: string;
};

export type PixelProduct = {
  product_id: string;
  name: string;
  definition_id: string;
  definition_version: number;
  state: string;
  visitor_access: boolean;
  entities: string[];
  views: string[];
};

export type ProductShape = {
  product_id: string;
  product_name: string;
  assistant_name: string;
  definition_id: string;
  definition_version: number;
  views: Array<{ name: string; label: string; kind: string; entity: string | null; columns: string[]; navigable: boolean }>;
  entities: Array<{
    name: string;
    label: string;
    plural: string;
    title_field: string;
    summary_fields: string[];
    fields: Array<{ name: string; label: string; type: string; target: string | null; display: boolean }>;
  }>;
  actions: Array<{ name: string; client_type: string; capability: string; description: string; entity: string | null; fields: string[] }>;
};

export type ProductRecords = {
  product_id: string;
  scope: string;
  records: Record<string, Array<Record<string, unknown> & { id: string; title?: string }>>;
};

export type TurnResponse = {
  session_id: string;
  turn_id: number;
  status: "completed" | "cancelled" | "stale" | "denied";
  speech: string;
  validated_action: { type: string; payload: Record<string, unknown> } | null;
  execution: { key: string; session_id: string; turn_id: number; expires_at: string } | null;
};

export type ExecutionReceipt = {
  outcome: "executed" | "failed" | "refused";
  code: string;
  speech: string;
  record: Record<string, unknown> | null;
};

export function storedConsoleSession(): ConsoleSession | null {
  if (typeof window === "undefined") return null;
  const token = window.sessionStorage.getItem(TOKEN_KEY);
  const userId = window.sessionStorage.getItem(USER_KEY);
  const tenantId = window.sessionStorage.getItem(TENANT_KEY);
  return token && userId && tenantId ? { token, userId, tenantId } : null;
}

function storeConsoleSession(session: ConsoleSession): ConsoleSession {
  window.sessionStorage.setItem(TOKEN_KEY, session.token);
  window.sessionStorage.setItem(USER_KEY, session.userId);
  window.sessionStorage.setItem(TENANT_KEY, session.tenantId);
  return session;
}

export function clearConsoleSession(): void {
  window.sessionStorage.removeItem(TOKEN_KEY);
  window.sessionStorage.removeItem(USER_KEY);
  window.sessionStorage.removeItem(TENANT_KEY);
}

export async function consoleSignIn(userId = DEFAULT_CONSOLE_USER): Promise<ConsoleSession> {
  const body = await api<{ token: string; user_id: string; tenant_id: string }>("/auth/demo-login", {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
  return storeConsoleSession({ token: body.token, userId: body.user_id, tenantId: body.tenant_id });
}

export async function listProducts(session: ConsoleSession): Promise<PixelProduct[]> {
  const body = await api<{ products: PixelProduct[] }>(
    `/organizations/${encodeURIComponent(session.tenantId)}/products`,
    {},
    session,
  );
  return body.products;
}

export async function addStarterProduct(session: ConsoleSession, input: { productId: string; name: string }): Promise<PixelProduct> {
  const definitionId = `${input.productId.replace(/-/g, "_")}_product`;
  return api<PixelProduct>(
    `/organizations/${encodeURIComponent(session.tenantId)}/products`,
    {
      method: "POST",
      body: JSON.stringify({
        product_id: input.productId,
        team_id: "planning-team",
        definition_id: definitionId,
        definition_version: 1,
        definition: starterDefinition(definitionId, input.name),
      }),
    },
    session,
  );
}

export async function getProductShape(session: ConsoleSession, productId: string): Promise<ProductShape> {
  return api<ProductShape>(`/products/${encodeURIComponent(productId)}/shape`, {}, session);
}

export async function getProductRecords(session: ConsoleSession, productId: string): Promise<ProductRecords> {
  return api<ProductRecords>(`/products/${encodeURIComponent(productId)}/records`, {}, session);
}

export async function sendProductTurn(session: ConsoleSession, input: {
  sessionId: string;
  turnId: number;
  productId: string;
  message: string;
}): Promise<TurnResponse> {
  return api<TurnResponse>("/turn", {
    method: "POST",
    body: JSON.stringify({
      session_id: input.sessionId,
      turn_id: input.turnId,
      product_id: input.productId,
      message: input.message,
      input_mode: "text",
      current_page: "pixel_system_console",
      workspace_scope_id: "primary",
    }),
  }, session);
}

export async function executeAction(session: ConsoleSession, productId: string, response: TurnResponse, shape: ProductShape): Promise<ExecutionReceipt | null> {
  if (!response.execution || !response.validated_action) return null;
  const action = shape.actions.find((candidate) => candidate.client_type === response.validated_action?.type);
  if (!action?.entity) return null;
  if (action.capability !== "CREATE_RECORD" && action.capability !== "UPDATE_RECORD") return null;
  const payload = response.validated_action.payload;
  const recordId = typeof payload.record_id === "string" ? payload.record_id : null;
  const endpoint = recordId
    ? `/products/${encodeURIComponent(productId)}/records/${encodeURIComponent(action.entity)}/${encodeURIComponent(recordId)}`
    : `/products/${encodeURIComponent(productId)}/records/${encodeURIComponent(action.entity)}`;
  const body = recordId
    ? { action: action.name, changes: withoutKey(payload, "record_id") }
    : { action: action.name, fields: payload };
  return api<ExecutionReceipt>(endpoint, {
    method: recordId ? "PATCH" : "POST",
    headers: {
      "X-Execution-Key": response.execution.key,
      "X-Session-Id": response.execution.session_id,
    },
    body: JSON.stringify(body),
  }, session);
}

async function api<T>(path: string, init: RequestInit = {}, session?: ConsoleSession): Promise<T> {
  const response = await fetch(`${API_BASE_URL.replace(/\/$/, "")}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(session ? { Authorization: `Bearer ${session.token}` } : {}),
      ...(init.headers ?? {}),
    },
  });
  const text = await response.text();
  if (!response.ok) {
    let detail = text || `Request failed (${response.status})`;
    try {
      detail = (JSON.parse(text) as { detail?: string }).detail ?? detail;
    } catch {
      /* not json */
    }
    throw new Error(detail);
  }
  return text ? JSON.parse(text) as T : ({} as T);
}

function withoutKey(source: Record<string, unknown>, key: string): Record<string, unknown> {
  const next = { ...source };
  delete next[key];
  return next;
}

function starterDefinition(definitionId: string, name: string): string {
  return JSON.stringify({
    definition: { definition_id: definitionId, version: 1, ownership: "platform_shared" },
    identity: {
      product_name: name,
      assistant_name: "Edith",
      persona: `A precise product operator for ${name}.`,
      voice_style: "Calm, clear and professional.",
      greeting: "Welcome to {product}. I'm {assistant}.",
    },
    vocabulary: {
      terms: ["ticket", "tickets", "issue", "issues", "member", "members", "owner", "owners", "status"],
      corrections: { tikcet: "ticket" },
      correction_markers: ["actually", "instead"],
      negatable_terms: ["ticket", "tickets", "member", "members"],
    },
    entities: {
      ticket: {
        label: "Ticket", plural: "Tickets",
        id: { strategy: "prefix", prefix: "TKT" },
        title_field: "title",
        summary_fields: ["status", "owner"],
        fields: {
          title: { type: "text", required: true, max: 160 },
          status: { type: "enum", required: true, values: ["Todo", "In progress", "Done"], default: "Todo" },
          owner: { type: "ref", target: "member", required: true },
        },
      },
      member: {
        label: "Member", plural: "Members",
        id: { strategy: "slug", from_field: "name" },
        title_field: "name",
        fields: {
          name: { type: "text", required: true, editable: false },
          tickets: { type: "refs", target: "ticket" },
        },
      },
    },
    people: { entity: "member", assigned_by: ["ticket.owner"], match_on: ["name"] },
    scope: { anchor: "ticket", paths: { ticket: [], member: ["tickets"] } },
    views: {
      tickets: { label: "Tickets", kind: "list", entity: "ticket", columns: ["title", "status", "owner"] },
      members: { label: "Members", kind: "list", entity: "member", columns: ["name"] },
    },
    actions: {
      open_tickets: { capability: "NAVIGATE_VIEW", view: "tickets", description: "Open the ticket list." },
      open_members: { capability: "NAVIGATE_VIEW", view: "members", description: "Open the team directory." },
      open_ticket: { capability: "OPEN_RECORD", entity: "ticket", description: "Open one ticket." },
      tickets_by_owner: { capability: "FILTER_RECORDS", entity: "ticket", by: "owner", description: "Show tickets assigned to one member." },
      create_ticket: { capability: "CREATE_RECORD", entity: "ticket", fields: ["title", "status", "owner"], description: "Create a ticket." },
      update_ticket: { capability: "UPDATE_RECORD", entity: "ticket", fields: ["status", "owner"], description: "Update a ticket." },
      add_member: { capability: "CREATE_RECORD", entity: "member", fields: ["name"], description: "Add a team member." },
    },
    intents: [
      { action: "open_tickets", response: "anchor_count", match: [["how many", "count", "number of"], ["ticket", "tickets"]] },
      { action: "open_tickets", response: "view_opened", match: [["ticket", "tickets", "issue", "issues"]] },
      { action: "open_members", response: "view_opened", match: [["member", "members", "team"]] },
      { action: "tickets_by_owner", requires: ["person"], response: "records_filtered", match: [["assigned to", "for"], ["ticket", "tickets"]] },
      { action: "open_ticket", requires: ["record"], response: "record_opened", match: [["open", "show"]] },
      { action: "create_ticket", requires: ["person"], response: "record_created", match: [["add", "create", "new"], ["ticket", "issue"]] },
      { action: "add_member", response: "record_created", match: [["add", "create", "new"], ["member", "person"]] },
      { action: "update_ticket", requires: ["record"], response: "record_updated", match: [["assign", "owner", "status", "done", "todo"]] },
    ],
    guardrails: [
      { topic: "destructive_change", response: "destructive_refused", match: [["delete", "erase", "wipe", "remove all"]] },
    ],
    responses: {
      view_opened: "I'll open {view}.",
      anchor_count: "{scope} has {count} visible tickets: {records}.",
      record_opened: "I'll open {record_id}.",
      records_filtered: "I found {count} tickets for {person}.",
      destructive_refused: "I can't delete or erase anything here.",
      record_created: "I'll create the record.",
      record_updated: "I'll update {record_id}: {changes}.",
      clarify_create: "What title should I use?",
    },
  }, null, 2);
}
