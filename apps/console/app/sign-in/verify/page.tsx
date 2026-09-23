"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { AuthCard, PrototypeNote } from "@/components/auth-card";
import { Alert, Button, Field, Input } from "@/components/ui";
import {
  CODE_LENGTH, SIGN_IN_MESSAGES, demoCodeFor, normalizeEmail, startChallenge, verifyChallenge,
  type Challenge, type SignInError,
} from "@/lib/mock-identity";

function Verify() {
  const router = useRouter();
  const email = normalizeEmail(useSearchParams().get("email") ?? "");
  const [challenge, setChallenge] = useState<Challenge | null>(() => {
    const started = email ? startChallenge(email, Date.now()) : "invalid_email";
    return typeof started === "string" ? null : started;
  });
  const [code, setCode] = useState("");
  const [error, setError] = useState<SignInError | null>(null);
  const [resent, setResent] = useState(false);

  if (!email || !challenge) {
    return (
      <AuthCard title="Start again" description="This sign-in link is incomplete.">
        <Link className="px-button" data-variant="primary" href="/sign-in">Back to sign in</Link>
      </AuthCard>
    );
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!challenge) return;
    const outcome = verifyChallenge(challenge, code, Date.now());
    setChallenge(outcome.challenge);
    if (outcome.result === "ok") { router.push("/console"); return; }
    setError(outcome.result);
  }

  function resend() {
    const started = startChallenge(email!, Date.now());
    if (typeof started !== "string") { setChallenge(started); setCode(""); setError(null); setResent(true); }
  }

  const locked = error === "too_many_attempts" || error === "code_expired";
  return (
    <AuthCard title="Check your email" description={<>We sent a {CODE_LENGTH}-digit code to <strong>{email}</strong>. It expires in 10 minutes.</>}
      footer={<Link href="/sign-in">Use a different email</Link>}>
      {resent ? <Alert tone="ok">A new code is on its way. Earlier codes no longer work.</Alert> : null}
      {error === "no_access" ? (
        <div className="px-stack">
          <Alert tone="warn">{SIGN_IN_MESSAGES.no_access}</Alert>
          <Link className="px-button" href="/request-access">Request access</Link>
        </div>
      ) : (
        <form className="px-stack" onSubmit={submit} noValidate>
          <Field label="One-time code" error={error ? SIGN_IN_MESSAGES[error] : null}>{(f) => (
            <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} className="px-input px-code-input" inputMode="numeric"
              autoComplete="one-time-code" maxLength={CODE_LENGTH} autoFocus value={code} disabled={locked}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
          )}</Field>
          {locked ? <Button variant="primary" onClick={resend}>Send a new code</Button> : (
            <>
              <Button type="submit" variant="primary" disabled={code.length !== CODE_LENGTH}>Sign in</Button>
              <Button variant="ghost" onClick={resend}>Resend code</Button>
            </>
          )}
        </form>
      )}
      <PrototypeNote>the code for this address is <code>{demoCodeFor(email)}</code>. A real sign-in emails it.</PrototypeNote>
    </AuthCard>
  );
}

export default function VerifyPage() {
  return <Suspense fallback={null}><Verify /></Suspense>;
}
