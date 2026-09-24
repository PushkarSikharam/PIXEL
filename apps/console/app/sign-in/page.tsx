"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { AuthCard } from "@pixel-console/components/auth-card";
import { Button, Field, Input } from "@pixel-console/components/ui";
import { normalizeEmail, SIGN_IN_MESSAGES } from "@pixel-console/lib/mock-identity";
import { requestEmailCode } from "@pixel-console/lib/pixel-api";

export default function SignIn() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const address = normalizeEmail(email);
    if (!address) { setError(SIGN_IN_MESSAGES.invalid_email); return; }
    setSending(true);
    try {
      const challenge = await requestEmailCode(address);
      sessionStorage.setItem("pixel.email.challenge", JSON.stringify({ email: address, id: challenge.challenge_id }));
      router.push("/sign-in/verify");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Sign-in could not start.");
    } finally { setSending(false); }
  }

  return (
    <AuthCard title="Sign in to Pixel"
      description="We'll email you a one-time code. There is no password, and if you have not been here before this makes you a workspace of your own."
      footer={<Link href="/demo">Explore the demo</Link>}>
      <form className="px-stack" onSubmit={submit} noValidate>
        <Field label="Your email" error={error}>{(f) => (
          <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} type="email" autoComplete="email" inputMode="email"
            autoFocus value={email} onChange={(e) => { setEmail(e.target.value); setError(null); }} />
        )}</Field>
        <Button type="submit" variant="primary" loading={sending}>Email me a code</Button>
      </form>
    </AuthCard>
  );
}
