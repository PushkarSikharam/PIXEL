"use client";

import Link from "next/link";
import { useState } from "react";
import { AuthCard, PrototypeNote } from "@/components/auth-card";
import { Alert, Button, Field, Input } from "@/components/ui";
import { demoCodeFor, normalizeEmail } from "@/lib/mock-identity";

type Stage = "form" | "verify" | "done";

export default function RequestAccess() {
  const [stage, setStage] = useState<Stage>("form");
  const [email, setEmail] = useState("");
  const [organization, setOrganization] = useState("");
  const [reason, setReason] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (stage === "done") {
    return (
      <AuthCard title="Request received" description="An administrator will review it. If it's approved, you'll get an invitation by email.">
        <Alert>Requesting access doesn&apos;t create an account. We keep your request for at most 90 days, and you can ask us to delete it at any time.</Alert>
        <Link className="px-button" href="/sign-in">Back to sign in</Link>
      </AuthCard>
    );
  }
  if (stage === "verify") {
    const address = normalizeEmail(email)!;
    return (
      <AuthCard title="Confirm your email" description={<>Enter the code we sent to <strong>{address}</strong>. Your request is only stored after this step.</>}>
        <form className="px-stack" noValidate onSubmit={(e) => {
          e.preventDefault();
          if (code !== demoCodeFor(address)) { setError("That code is not correct."); return; }
          setStage("done");
        }}>
          <Field label="One-time code" error={error}>{(f) => (
            <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} className="px-input px-code-input" inputMode="numeric" autoComplete="one-time-code"
              maxLength={6} autoFocus value={code} onChange={(e) => { setCode(e.target.value.replace(/\D/g, "")); setError(null); }} />
          )}</Field>
          <Button type="submit" variant="primary" disabled={code.length !== 6}>Submit request</Button>
        </form>
        <PrototypeNote>the code is <code>{demoCodeFor(address)}</code>. Nothing is sent or stored.</PrototypeNote>
      </AuthCard>
    );
  }
  return (
    <AuthCard title="Request access" description="Pixel is invitation-only. Tell us who you are and we'll review your request."
      footer={<>Already invited? <Link href="/sign-in">Sign in</Link></>}>
      <form className="px-stack" noValidate onSubmit={(e) => {
        e.preventDefault();
        if (!normalizeEmail(email)) { setError("Enter a valid email address."); return; }
        setError(null);
        setStage("verify");
      }}>
        <Field label="Work email" error={error}>{(f) => (
          <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        )}</Field>
        <Field label="Organization (optional)">{(f) => (
          <Input id={f.id} describedBy={f.describedBy} maxLength={120} autoComplete="organization" value={organization} onChange={(e) => setOrganization(e.target.value)} />
        )}</Field>
        <Field label="What would you like to use Pixel for? (optional)" hint={`${reason.length}/500`}>{(f) => (
          <textarea id={f.id} aria-describedby={f.describedBy} className="px-textarea" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
        )}</Field>
        <p className="px-small px-muted">We ask only for these fields. The request is stored after you confirm your email, kept for at most 90 days, and never creates an account by itself.</p>
        <Button type="submit" variant="primary">Continue</Button>
      </form>
    </AuthCard>
  );
}
