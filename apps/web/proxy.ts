import { NextResponse, type NextRequest } from "next/server";

/**
 * Security headers for every page this application serves.
 *
 * They used to be set in the console's own Next config, which applies only when the console runs
 * on its own in development. The deployment serves the console from this application, so until
 * now it went out with none: nothing restricting where a script could come from, nothing stopping
 * the pages being framed, and nothing preventing a response being treated as a type it is not.
 *
 * This is Next 16's `proxy.ts` - the renamed `middleware.ts` - which runs before a page renders
 * and is the only place a per-request nonce can be minted and handed to the framework.
 *
 * **Two script policies, and why.** Where somebody is signed in, scripts are allowed by a nonce:
 * an injected script has no nonce, so it does not run, which is exactly what `unsafe-inline`
 * gives away. A nonce only works on a page rendered per request, because Next puts it there while
 * rendering; a page built ahead of time has no request to take one from. The signed-in pages
 * therefore render per request (see the `(system)` layout), and the rest of the site - the demo
 * and the pages around it, which hold no session - stays prerendered and keeps `unsafe-inline`.
 * Rendering every public page per request to tighten a policy on pages that carry nothing is a
 * cost without a matching gain.
 *
 * Inline *styles* are allowed everywhere. Style attributes are used throughout these pages, an
 * attribute cannot carry a nonce, and a stylesheet cannot exfiltrate anything by itself. That is
 * a different trade from scripts, made deliberately rather than by leaving both open.
 *
 * Development loads code differently and needs `unsafe-eval`, and `strict-dynamic` would make a
 * browser ignore the rest of the list, so development takes the permissive path either way.
 */

/** Everything reached after signing in. These render per request, so a nonce reaches the page. */
const SIGNED_IN = ["/console", "/preview", "/invite", "/session-expired"];

export function proxy(request: NextRequest) {
  const development = process.env.NODE_ENV !== "production";
  const path = request.nextUrl.pathname;
  const nonced = !development && SIGNED_IN.some(
    (prefix) => path === prefix || path.startsWith(`${prefix}/`),
  );
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const scripts = nonced
    ? `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`
    : `script-src 'self' 'unsafe-inline'${development ? " 'unsafe-eval'" : ""}`;

  const policy = [
    "default-src 'self'",
    scripts,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "media-src 'self' blob:",
    `connect-src 'self'${development ? " ws: wss:" : ""}`,
    "worker-src 'self' blob:",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    ...(development ? [] : ["upgrade-insecure-requests"]),
  ].join("; ");

  const headers = new Headers(request.headers);
  // Next reads the nonce back out of this header while rendering and puts it on its own scripts.
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", policy);

  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", policy);
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("Referrer-Policy", "no-referrer");
  response.headers.set("Permissions-Policy", "camera=(), geolocation=(), payment=()");
  return response;
}

export const config = {
  // Pages only. The API proxy answers with the backend's own headers, and a static asset or a
  // prefetch carries nothing a policy protects.
  matcher: [
    {
      source: "/((?!api/|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
