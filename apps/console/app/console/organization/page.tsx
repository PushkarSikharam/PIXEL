"use client";

import { useEffect, useState } from "react";
import { MoreHorizontal, UserPlus } from "lucide-react";
import { useConsole } from "@pixel-console/components/console-context";
import { Dialog, Menu } from "@pixel-console/components/overlays";
import { useToast } from "@pixel-console/components/toast";
import { Button, EmptyState, ErrorState, Field, Input, LoadingRows, PageHead, Panel, PermissionDenied, StatusBadge } from "@pixel-console/components/ui";
import { addPerson, listMembers, removePerson, storedSession, type ApiMember, type PersonRole } from "@pixel-console/lib/pixel-api";
import { INVITATIONS, MEMBERS, TEAMS, teamName } from "@pixel-console/lib/mock-data";
import type { Member, Role } from "@pixel-console/lib/contracts";
import { normalizeEmail } from "@pixel-console/lib/mock-identity";

const ROLE_LABEL: Record<Role, string> = { org_admin: "Organization admin", team_admin: "Team admin", team_member: "Team member" };

const ROLE_WORDS: Record<string, string> = {
  org_admin: "Organization admin", team_admin: "Team admin", team_member: "Team member",
};

/**
 * The people who are really in this organization, and adding or removing them.
 *
 * Adding somebody gives their email address a place here. Nothing is emailed: they sign in with
 * that address and a code, as everyone does, and arrive in this organization with its products.
 */
function LivePeople() {
  const c = useConsole();
  const toast = useToast();
  const [people, setPeople] = useState<ApiMember[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [reloads, setReloads] = useState(0);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<PersonRole>("team_member");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [removing, setRemoving] = useState<ApiMember | null>(null);
  const isAdmin = c.account?.role === "org_admin";

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const session = storedSession();
        if (!session) throw new Error("Sign in to see who is in your organization.");
        const found = await listMembers(session);
        if (!cancelled) { setPeople(found); setProblem(null); }
      } catch (error) {
        if (!cancelled) setProblem(error instanceof Error ? error.message : "These could not be loaded.");
      }
    })();
    return () => { cancelled = true; };
  }, [c.organizationId, reloads]);

  async function add(event: React.FormEvent) {
    event.preventDefault();
    const address = normalizeEmail(email);
    if (!address) { setAddError("Enter a valid email address."); return; }
    const session = storedSession();
    if (!session) return;
    setAdding(true); setAddError(null);
    try {
      await addPerson(session, address, role);
      toast("ok", `${address} was added. They can sign in with that email now.`);
      setEmail(""); setRole("team_member"); setReloads((n) => n + 1);
    } catch (error) {
      setAddError(error instanceof Error ? error.message : "That person could not be added.");
    } finally {
      setAdding(false);
    }
  }

  async function remove() {
    const person = removing;
    const session = storedSession();
    if (!person || !session) return;
    try {
      await removePerson(session, person.user_id);
      toast("ok", `${person.email ?? "That person"} was removed and signed out.`);
      setReloads((n) => n + 1);
    } catch (error) {
      toast("danger", error instanceof Error ? error.message : "That person could not be removed.");
    } finally {
      setRemoving(null);
    }
  }

  if (problem) return <><PageHead title="People" /><ErrorState title="People could not be loaded">{problem}</ErrorState></>;
  if (people === null) return <><PageHead title="People" description="Loading." /><LoadingRows rows={4} /></>;

  return (
    <>
      <PageHead title="People"
        description={`Everyone in ${c.account?.organization_name ?? "this organization"}, and what each of them can do.`} />
      {isAdmin ? (
        <Panel title="Add a person">
          <form className="px-stack" onSubmit={add} noValidate>
            <p className="px-muted" style={{ margin: 0 }}>
              Enter their work email. They sign in with it and a code we email them, and they
              arrive here with access to your products.
            </p>
            <div className="px-row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
              <div style={{ flex: "1 1 260px" }}>
                <Field label="Email" error={addError}>{(f) => (
                  <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} type="email" value={email}
                    autoComplete="off" placeholder="name@company.com"
                    onChange={(e) => { setEmail(e.target.value); setAddError(null); }} />
                )}</Field>
              </div>
              <div style={{ flex: "0 1 220px" }}>
                <Field label="What they can do">{(f) => (
                  <select id={f.id} className="px-select" value={role}
                    onChange={(e) => setRole(e.target.value as PersonRole)}>
                    <option value="team_member">Member: use the products</option>
                    <option value="team_admin">Team admin: also manage products</option>
                    <option value="org_admin">Admin: everything, including people</option>
                  </select>
                )}</Field>
              </div>
              <Button type="submit" variant="primary" loading={adding}><UserPlus aria-hidden />Add person</Button>
            </div>
          </form>
        </Panel>
      ) : null}
      <Panel title={`People (${people.length})`}>
        <div className="px-table-wrap">
          <table className="px-table">
            <caption className="px-sr-only">People in this organization</caption>
            <thead><tr><th scope="col">Person</th><th scope="col">Role</th><th scope="col">Team</th>
              {isAdmin ? <th scope="col"><span className="px-sr-only">Actions</span></th> : null}</tr></thead>
            <tbody>
              {people.map((person) => {
                const you = person.user_id === c.account?.user_id;
                return (
                  <tr key={person.user_id}>
                    <td>{person.email ?? "Invited person"}{you ? <span className="px-small px-muted"> (you)</span> : null}</td>
                    <td>{ROLE_WORDS[person.role] ?? person.role}</td>
                    <td>{person.team_name ?? <span className="px-muted">All teams</span>}</td>
                    {isAdmin ? (
                      <td style={{ textAlign: "right" }}>
                        {you ? null : <Button size="sm" variant="ghost"
                          aria-label={`Remove ${person.email ?? "this person"}`}
                          onClick={() => setRemoving(person)}>Remove</Button>}
                      </td>
                    ) : null}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>
      <Dialog open={removing !== null} onOpenChange={(open) => { if (!open) setRemoving(null); }}
        title="Remove this person?"
        description={`${removing?.email ?? "They"} will be signed out straight away and lose access to every product here.`}
        actions={<>
          <Button onClick={() => setRemoving(null)}>Keep</Button>
          <Button variant="danger" onClick={() => void remove()}>Remove</Button>
        </>} />
    </>
  );
}

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

  if (c.live) return <LivePeople />;
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
