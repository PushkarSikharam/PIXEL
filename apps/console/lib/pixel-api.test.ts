import { afterEach, describe, expect, it, vi } from "vitest";
import { currentAccount, sendProductTurn, storedSession, verifyEmailCode } from "./pixel-api";

function installBrowser(cookie: string) {
  const storage = new Map<string, string>();
  vi.stubGlobal("window", {
    sessionStorage: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => { storage.set(key, value); },
      removeItem: (key: string) => { storage.delete(key); },
    },
  });
  vi.stubGlobal("document", { cookie });
  return storage;
}

describe("Pixel API CSRF handling", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps using the verified CSRF token when the readable cookie is unavailable", async () => {
    installBrowser("");
    const requests: Array<{ path: string; headers: Headers }> = [];
    vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit = {}) => {
      requests.push({ path, headers: new Headers(init.headers) });
      if (path.endsWith("/account/verify-code")) {
        return new Response(JSON.stringify({ csrf_token: "verified-token", user_id: "user-1", tenant_id: "tenant-1" }), { status: 200 });
      }
      if (path.endsWith("/account/session")) {
        return new Response(JSON.stringify({
          user_id: "user-1", email: "user@example.com", tenant_id: "tenant-1",
          organization_name: "My organization", role: "org_admin", team_id: "team-1",
          console_product_id: "pixel-console", teams: [{ team_id: "team-1", name: "My team" }],
        }), { status: 200 });
      }
      if (path.endsWith("/turn")) {
        return new Response(JSON.stringify({
          session_id: "session-1", turn_id: 1, status: "completed", speech: "Done.",
          validated_action: null, execution: null,
        }), { status: 200 });
      }
      return new Response("not found", { status: 404 });
    }));

    const session = await verifyEmailCode("challenge", "123456");
    expect(session.csrfToken).toBe("verified-token");
    await currentAccount();
    const kept = storedSession();
    expect(kept?.csrfToken).toBe("verified-token");

    await sendProductTurn(kept!, {
      sessionId: "session-1", turnId: 1, productId: "pixel-console", message: "hi",
    });
    expect(requests.at(-1)?.headers.get("X-Pixel-CSRF")).toBe("verified-token");
  });

  it("prefers the current cookie token when the server rotates it", async () => {
    installBrowser("pixel_csrf=fresh-token");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      session_id: "session-1", turn_id: 1, status: "completed", speech: "Done.",
      validated_action: null, execution: null,
    }), { status: 200 })));

    await sendProductTurn({ csrfToken: "old-token", userId: "user-1", tenantId: "tenant-1" }, {
      sessionId: "session-1", turnId: 1, productId: "pixel-console", message: "hi",
    });

    const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>;
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("X-Pixel-CSRF")).toBe("fresh-token");
  });
});
