"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { AuthCard, PrototypeNote } from "@pixel-console/components/auth-card";
import { Button, Field, Input } from "@pixel-console/components/ui";
import { normalizeEmail, SIGN_IN_MESSAGES } from "@pixel-console/lib/mock-identity";

export default function SignIn() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const address = normalizeEmail(email);
    if (!address) { setError(SIGN_IN_MESSAGES.invalid_email); return; }
    setSending(true);
    router.push(`/sign-in/verify?email=${encodeURIComponent(address)}`);
  }

  return (
    <AuthCard title="Sign in to Pixel" description="We'll email you a one-time code. There is no password."
      footer={<>No account yet? Pixel is invitation-only. <Link href="/request-access">Request access</Link></>}>
      <form className="px-stack" onSubmit={submit} noValidate>
        <Field label="Work email" error={error}>{(f) => (
          <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} type="email" autoComplete="email" inputMode="email"
            autoFocus value={email} onChange={(e) => { setEmail(e.target.value); setError(null); }} />
        )}</Field>
        <Button type="submit" variant="primary" loading={sending}>Email me a code</Button>
      </form>
      <PrototypeNote>try <code>avery@northwind.example</code> (invited) or any other address (verifies, but has no access). No email is sent.</PrototypeNote>
    </AuthCard>
  );
}
