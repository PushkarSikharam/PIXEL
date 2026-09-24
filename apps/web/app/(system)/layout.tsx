import type { ReactNode } from "react";
import { connection } from "next/server";
import ConsoleLayout, { metadata as consoleMetadata } from "@pixel-console/app/layout";

export const metadata = consoleMetadata;

/**
 * Everything reached after signing in, rendered per request.
 *
 * These pages are allowed to run scripts by a nonce rather than by `unsafe-inline`, and a nonce
 * can only be put on a page while it is being rendered for a request. Built ahead of time there
 * is no request to take one from, so the policy would block the page's own scripts. Awaiting the
 * connection is what tells Next these pages are rendered per request.
 *
 * Nothing here is static anyway: every one of these screens reads the signed-in organization
 * before it can show anything.
 */
export default async function SystemLayout({ children }: { children: ReactNode }) {
  await connection();
  return <ConsoleLayout>{children}</ConsoleLayout>;
}
