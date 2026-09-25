"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { LogOut } from "lucide-react";
import { useConsole } from "@pixel-console/components/console-context";
import { SectionPlaceholder } from "@pixel-console/components/placeholder";
import { useToast } from "@pixel-console/components/toast";
import { Button, Field, Input, PageHead, Panel } from "@pixel-console/components/ui";
import { endSession, renameOrganization, storedSession } from "@pixel-console/lib/pixel-api";

const ROLE_WORDS: Record<string, string> = {
  org_admin: "Organization admin", team_admin: "Team admin", team_member: "Team member",
};

/** What a signed-in organization can really set today: its name, and who you are signed in as. */
function LiveSettings() {
  const c = useConsole();
  const toast = useToast();
  const router = useRouter();
  const current = c.account?.organization_name ?? "";
  const [name, setName] = useState(current);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isAdmin = c.account?.role === "org_admin";

  useEffect(() => { setName(current); }, [current]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    const session = storedSession();
    const wanted = name.trim();
    if (!wanted) { setError("Give your organization a name."); return; }
    if (!session) return;
    setSaving(true); setError(null);
    try {
      const saved = await renameOrganization(session, wanted);
      toast("ok", `Your organization is now called ${saved.name}.`);
      c.reloadProducts();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The name could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <PageHead title="Settings" description="Your organization's name and the account you are signed in with." />
      <Panel title="Organization">
        <form className="px-stack" onSubmit={save} noValidate>
          <Field label="Organization name" error={error}
            hint={isAdmin ? "Shown to everyone in your organization and at the top of every page." : "Only an organization admin can change this."}>{(f) => (
            <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} value={name} maxLength={80}
              disabled={!isAdmin} onChange={(e) => { setName(e.target.value); setError(null); }} />
          )}</Field>
          {isAdmin ? (
            <div className="px-row">
              <Button type="submit" variant="primary" loading={saving}
                disabled={!name.trim() || name.trim() === current}>Save name</Button>
            </div>
          ) : null}
        </form>
      </Panel>
      <Panel title="Your account">
        <dl className="px-record-fields">
          <div><dt>Email</dt><dd>{c.account?.email ?? "Not set"}</dd></div>
          <div><dt>Role</dt><dd>{ROLE_WORDS[c.account?.role ?? ""] ?? c.account?.role}</dd></div>
          <div><dt>Sign-in</dt><dd>A one-time code sent to your email. There is no password to manage.</dd></div>
        </dl>
        <div className="px-row" style={{ marginTop: "var(--px-space-4)" }}>
          <Button onClick={() => { void endSession().then(() => router.push("/signed-out")); }}>
            <LogOut aria-hidden />Sign out</Button>
        </div>
      </Panel>
    </>
  );
}

export default function Page() {
  const c = useConsole();
  if (c.live) return <LiveSettings />;
  return <SectionPlaceholder title="Settings" permission="organization.manage"
    description="Organization profile, login policy, credentials, billing and retention." />;
}
