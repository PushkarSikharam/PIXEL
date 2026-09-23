import Link from "next/link";
import { AuthCard } from "@/components/auth-card";
import { Alert } from "@/components/ui";

export const metadata = { title: "Session ended" };

export default function SessionExpired() {
  return (
    <AuthCard title="Your session ended" description="Sessions end after 12 hours, when you sign out elsewhere, or when an administrator changes your access.">
      <Alert>Anything you hadn&apos;t saved wasn&apos;t kept. Sign in to continue.</Alert>
      <Link className="px-button" data-variant="primary" href="/sign-in">Sign in</Link>
    </AuthCard>
  );
}
