"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { AuthCard } from "@pixel-console/components/auth-card";
import { Alert, Button, Field, Input } from "@pixel-console/components/ui";
import { requestEmailCode, verifyEmailCode } from "@pixel-console/lib/pixel-api";

export default function VerifyPage() {
  const router = useRouter();
  const [challenge, setChallenge] = useState<{ email: string; id: string } | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    try {
      const value = JSON.parse(sessionStorage.getItem("pixel.email.challenge") ?? "null");
      if (value && typeof value.email === "string" && typeof value.id === "string") setChallenge(value);
    } catch { setError("Start sign-in again."); }
  }, []);

  async function verify(event: React.FormEvent) {
    event.preventDefault();
    if (!challenge || busy) return;
    setBusy(true); setError(null);
    try {
      await verifyEmailCode(challenge.id, code);
      sessionStorage.removeItem("pixel.email.challenge");
      router.replace("/console");
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Sign-in failed."); }
    finally { setBusy(false); }
  }

  async function resend() {
    if (!challenge || busy) return;
    setBusy(true); setError(null);
    try {
      const result = await requestEmailCode(challenge.email);
      const next = { email: challenge.email, id: result.challenge_id };
      sessionStorage.setItem("pixel.email.challenge", JSON.stringify(next));
      setChallenge(next); setCode("");
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Code could not be sent."); }
    finally { setBusy(false); }
  }

  return <AuthCard title="Check your email" description={challenge ? `Enter the 8-digit code sent to ${challenge.email}. It expires in 10 minutes.` : "Start sign-in to receive a code."}
    footer={<Link href="/sign-in">Use a different email</Link>}>
    {error ? <Alert tone="warn">{error}</Alert> : null}
    {challenge ? <form className="px-stack" onSubmit={verify}>
      <Field label="One-time code">{(f) => <Input id={f.id} autoComplete="one-time-code" inputMode="numeric"
        maxLength={8} value={code} autoFocus onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />}</Field>
      <Button type="submit" variant="primary" loading={busy} disabled={code.length !== 8}>Sign in</Button>
      <Button type="button" onClick={() => void resend()} disabled={busy}>Resend code</Button>
    </form> : null}
  </AuthCard>;
}
