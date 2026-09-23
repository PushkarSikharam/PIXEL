"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { AuthCard, PrototypeNote } from "@pixel-console/components/auth-card";
import { Alert } from "@pixel-console/components/ui";
import { DEMO_INVITATIONS, invitationStatus } from "@pixel-console/lib/mock-identity";

function Invitation() {
  const { token } = useParams<{ token: string }>();
  const signedInAs = useSearchParams().get("as");
  const status = invitationStatus(token, signedInAs);
  const invitation = DEMO_INVITATIONS[token];
  const note = <PrototypeNote>tokens <code>demo-valid</code>, <code>demo-expired</code>, <code>demo-used</code>, <code>demo-revoked</code>; add <code>?as=someone@else.example</code> to see a wrong-account sign-in.</PrototypeNote>;

  if (status === "valid") {
    return (
      <AuthCard title={`Join ${invitation.organization}`} description={<>You&apos;ve been invited as <strong>{invitation.role}</strong> in <strong>{invitation.team}</strong>.</>}>
        <p>This invitation is for <strong>{invitation.email}</strong>. Sign in with that address to accept it; it works once.</p>
        <Link className="px-button" data-variant="primary" href={`/sign-in/verify?email=${encodeURIComponent(invitation.email)}`}>Continue as {invitation.email}</Link>
        {note}
      </AuthCard>
    );
  }
  const copy = {
    expired: ["This invitation has expired", "Invitations last 7 days. Ask the person who invited you to send a new one."],
    used: ["This invitation was already used", "Each invitation works once. If that was you, sign in."],
    revoked: ["This invitation was withdrawn", "An administrator cancelled it. Ask them if you still need access."],
    invalid: ["This invitation link isn't valid", "Check that you opened the full link from the email, or ask for a new invitation."],
    wrong_account: ["This invitation is for a different address", `You're signed in as ${signedInAs}. Sign out and sign in as the invited address to accept it.`],
  }[status];
  return (
    <AuthCard title={copy[0]}>
      <Alert tone={status === "wrong_account" ? "warn" : "danger"}>{copy[1]}</Alert>
      <Link className="px-button" href={status === "used" ? "/sign-in" : "/request-access"}>{status === "used" ? "Sign in" : "Request access instead"}</Link>
      {note}
    </AuthCard>
  );
}

export default function InvitePage() {
  return <AuthCard title="Invitation unavailable" description="Invitation acceptance is not enabled. No membership has been changed.">
    <Link className="px-button" href="/sign-in">Sign in</Link>
  </AuthCard>;
}
