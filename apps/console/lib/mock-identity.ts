/**
 * A mocked identity provider for the authentication screens. It follows the platform contract
 * (`saas/platform/pixel_saas/identity.py`) closely enough to show every state, entirely in the
 * browser: no request leaves the page and no real email is sent. The code for the demo is shown on
 * screen and is always derived from the address, so the flow can be walked through by anyone.
 */

export type SignInError = "invalid_email" | "code_invalid" | "code_expired" | "too_many_attempts" | "no_access";
export type InvitationStatus = "valid" | "expired" | "used" | "revoked" | "invalid" | "wrong_account";

export const CODE_LENGTH = 6;
export const MAX_ATTEMPTS = 5;

export function normalizeEmail(email: string): string | null {
  const value = email.trim().toLowerCase();
  const [local, domain, ...rest] = value.split("@");
  if (rest.length || !local || !domain || !domain.includes(".") || /\s/.test(value) || value.length > 254) return null;
  return value;
}

/** The demo code for an address. A real provider emails a random code; the mock makes it visible. */
export function demoCodeFor(email: string): string {
  let h = 2166136261;
  for (const ch of email) h = Math.imul(h ^ ch.charCodeAt(0), 16777619) >>> 0;
  return String(h % 1_000_000).padStart(CODE_LENGTH, "0");
}

/** Addresses the mock treats as invited members; everyone else verifies but has no access. */
export const INVITED = new Set(["avery@northwind.example", "bo@northwind.example", "dana@northwind.example"]);

export interface Challenge { email: string; attempts: number; expiresAt: number; used: boolean }

export function startChallenge(email: string, now: number): Challenge | SignInError {
  const address = normalizeEmail(email);
  if (!address) return "invalid_email";
  // Same answer for every valid address: the form cannot reveal who has an account.
  return { email: address, attempts: 0, expiresAt: now + 10 * 60_000, used: false };
}

export function verifyChallenge(challenge: Challenge, code: string, now: number):
  { challenge: Challenge; result: "ok" | SignInError } {
  if (challenge.used) return { challenge, result: "code_invalid" };
  if (now >= challenge.expiresAt) return { challenge, result: "code_expired" };
  if (challenge.attempts >= MAX_ATTEMPTS) return { challenge, result: "too_many_attempts" };
  const next = { ...challenge, attempts: challenge.attempts + 1 };
  if (code.trim() !== demoCodeFor(challenge.email)) {
    return { challenge: next, result: next.attempts >= MAX_ATTEMPTS ? "too_many_attempts" : "code_invalid" };
  }
  const done = { ...next, used: true };
  return { challenge: done, result: INVITED.has(challenge.email) ? "ok" : "no_access" };
}

/** Demo invitation tokens, one per state the acceptance screen must handle. */
export const DEMO_INVITATIONS: Record<string, { status: InvitationStatus; organization: string; email: string; role: string; team: string }> = {
  "demo-valid": { status: "valid", organization: "Northwind Labs", email: "farah@northwind.example", role: "Team member", team: "Billing" },
  "demo-expired": { status: "expired", organization: "Northwind Labs", email: "gus@northwind.example", role: "Team admin", team: "Support tools" },
  "demo-used": { status: "used", organization: "Northwind Labs", email: "chen@northwind.example", role: "Team member", team: "Billing" },
  "demo-revoked": { status: "revoked", organization: "Northwind Labs", email: "hana@northwind.example", role: "Team member", team: "Support tools" },
};

export function invitationStatus(token: string, signedInEmail: string | null): InvitationStatus {
  const invitation = DEMO_INVITATIONS[token];
  if (!invitation) return "invalid";
  if (invitation.status !== "valid") return invitation.status;
  if (signedInEmail && signedInEmail !== invitation.email) return "wrong_account";
  return "valid";
}

export const SIGN_IN_MESSAGES: Record<SignInError, string> = {
  invalid_email: "Enter a valid email address.",
  code_invalid: "That code is not correct. Check the latest email and try again.",
  code_expired: "That code has expired. Request a new one.",
  too_many_attempts: "Too many attempts. Request a new code to try again.",
  no_access: "You're signed in, but this address has no Pixel access yet. Ask an administrator for an invitation, or request access.",
};
