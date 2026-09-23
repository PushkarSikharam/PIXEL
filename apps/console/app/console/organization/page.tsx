"use client";

import { useState } from "react";
import { MoreHorizontal, UserPlus } from "lucide-react";
import { useConsole } from "@/components/console-context";
import { Dialog, Menu } from "@/components/overlays";
import { useToast } from "@/components/toast";
import { Button, Field, Input, PageHead, Panel, PermissionDenied, StatusBadge } from "@/components/ui";
import { INVITATIONS, MEMBERS, TEAMS, teamName } from "@/lib/mock-data";
import type { Member, Role } from "@/lib/contracts";
import { normalizeEmail } from "@/lib/mock-identity";

const ROLE_LABEL: Record<Role, string> = { org_admin: "Organization admin", team_admin: "Team admin", team_member: "Team member" };

export default function Members() {
  const c = useConsole();
  const toast = useToast();
  const [members, setMembers] = useState<Member[]>(MEMBERS);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [confirm, setConfirm] = useState<{ member: Member; action: "suspend" | "remove" } | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("team_member");
  const [team, setTeam] = useState("billing");
  const [emailError, setEmailError] = useState<string | null>(null);

  if (!c.can("members.read")) return <PermissionDenied what="this organization's members" />;
  const isOrgAdmin = c.persona.memberships.some((m) => m.role === "org_admin");
  const manageableTeams = TEAMS.filter((t) => t.organizationId === c.organizationId && !t.suspended
    && c.can("members.manage", { teamId: t.id }));
  const canManage = (m: Member) => m.userId !== c.persona.id
    && (isOrgAdmin || (m.teamId !== null && manageableTeams.some((t) => t.id === m.teamId)));

  function sendInvite() {
    if (!normalizeEmail(email)) { setEmailError("Enter a valid email address."); return; }
    toast("ok", `Invitation for ${email.trim()} created. It expires in 7 days and works once (prototype: nothing was sent).`);
    setInviteOpen(false); setEmail(""); setEmailError(null);
  }

  function apply() {
    if (!confirm) return;
    const { member, action } = confirm;
    setMembers((all) => action === "remove" ? all.filter((m) => m.userId !== member.userId)
      : all.map((m) => (m.userId === member.userId ? { ...m, state: "suspended" } : m)));
    toast("ok", `${member.name} ${action === "remove" ? "removed" : "suspended"}. Their sessions ended immediately.`);
    setConfirm(null);
  }

  return (
    <>
      <PageHead title="Members" description="People in this organization and the team each one works in."
        actions={manageableTeams.length ? <Button variant="primary" onClick={() => setInviteOpen(true)}><UserPlus aria-hidden />Invite</Button> : null} />
      <Panel title={`Members (${members.length})`}>
        <div className="px-table-wrap">
          <table className="px-table">
            <caption className="px-sr-only">Organization members</caption>
            <thead><tr><th scope="col">Name</th><th scope="col">Role</th><th scope="col">Team</th><th scope="col">State</th><th scope="col">Last active</th><th scope="col"><span className="px-sr-only">Actions</span></th></tr></thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.userId}>
                  <td>{m.name}<div className="px-small px-muted">{m.email}</div></td>
                  <td>{ROLE_LABEL[m.role]}</td>
                  <td>{m.teamId ? teamName(m.teamId) : <span className="px-muted">All teams</span>}</td>
                  <td><StatusBadge status={m.state} /></td>
                  <td>{m.lastActive}</td>
                  <td style={{ textAlign: "right" }}>
                    {canManage(m) ? (
                      <Menu align="end" label={`Actions for ${m.name}`} trigger={<Button size="sm" variant="ghost" aria-label={`Actions for ${m.name}`}><MoreHorizontal aria-hidden /></Button>}
                        sections={[{ items: [
                          { key: "suspend", label: "Suspend", disabled: m.state !== "active", onSelect: () => setConfirm({ member: m, action: "suspend" }) },
                          { key: "remove", label: "Remove from organization", onSelect: () => setConfirm({ member: m, action: "remove" }) },
                        ] }]} />
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel title="Invitations">
        <div className="px-table-wrap">
          <table className="px-table">
            <caption className="px-sr-only">Pending and expired invitations</caption>
            <thead><tr><th scope="col">Email</th><th scope="col">Role</th><th scope="col">Team</th><th scope="col">Expires</th><th scope="col">State</th></tr></thead>
            <tbody>
              {INVITATIONS.map((i) => (
                <tr key={i.id}><td>{i.email}</td><td>{ROLE_LABEL[i.role]}</td><td>{teamName(i.teamId)}</td><td>{i.expiresAt}</td><td><StatusBadge status={i.state} /></td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Dialog open={inviteOpen} onOpenChange={setInviteOpen} title="Invite a member"
        description="The invitation works once, only for this address, and expires in 7 days. The person signs in with a one-time email code."
        actions={<><Button onClick={() => setInviteOpen(false)}>Cancel</Button><Button variant="primary" onClick={sendInvite}>Create invitation</Button></>}>
        <div className="px-stack">
          <Field label="Email address" error={emailError}>{(f) => (
            <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} type="email" autoComplete="off" value={email} onChange={(e) => setEmail(e.target.value)} />
          )}</Field>
          <Field label="Role">{(f) => (
            <select id={f.id} className="px-select" value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="team_member">Team member</option>
              <option value="team_admin">Team admin</option>
              {isOrgAdmin ? <option value="org_admin">Organization admin</option> : null}
            </select>
          )}</Field>
          {role !== "org_admin" ? (
            <Field label="Team">{(f) => (
              <select id={f.id} className="px-select" value={team} onChange={(e) => setTeam(e.target.value)}>
                {manageableTeams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            )}</Field>
          ) : <p className="px-small px-muted">Organization admins belong to no single team.</p>}
        </div>
      </Dialog>

      <Dialog open={confirm !== null} onOpenChange={(open) => { if (!open) setConfirm(null); }}
        title={confirm?.action === "remove" ? `Remove ${confirm?.member.name}?` : `Suspend ${confirm?.member.name}?`}
        description={confirm?.action === "remove"
          ? "They lose access now and their sessions end immediately. They can only return through a new invitation."
          : "They lose access now and their sessions end immediately. Signing in again does not restore access."}
        actions={<><Button onClick={() => setConfirm(null)}>Cancel</Button><Button variant="danger" onClick={apply}>{confirm?.action === "remove" ? "Remove" : "Suspend"}</Button></>} />
    </>
  );
}
