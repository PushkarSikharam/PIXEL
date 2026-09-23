import Link from "next/link";
import { AuthCard } from "@/components/auth-card";

export const metadata = { title: "Signed out" };

export default function SignedOut() {
  return (
    <AuthCard title="You're signed out" description="Your session on this device has ended.">
      <Link className="px-button" data-variant="primary" href="/sign-in">Sign in again</Link>
    </AuthCard>
  );
}
