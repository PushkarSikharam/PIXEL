/**
 * An email address as Pixel keeps it: trimmed and lower-cased, or nothing when it cannot be one.
 * The server checks again; this only lets a form say so before anything is sent.
 */
export function normalizeEmail(email: string): string | null {
  const value = email.trim().toLowerCase();
  const [local, domain, ...rest] = value.split("@");
  if (rest.length || !local || !domain || !domain.includes(".") || /\s/.test(value) || value.length > 254) return null;
  return value;
}

export const INVALID_EMAIL = "Enter a valid email address.";
