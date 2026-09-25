"use client";

import { useEffect, useState } from "react";
import { MoreHorizontal, Plus, UserPlus } from "lucide-react";
import { useConsole } from "@pixel-console/components/console-context";
import { Dialog, Menu } from "@pixel-console/components/overlays";
import { useToast } from "@pixel-console/components/toast";
import { Alert, Button, EmptyState, ErrorState, Field, Input, LoadingRows, PageHead, Panel, PermissionDenied, StatusBadge } from "@pixel-console/components/ui";
import {
  addPerson, changePerson, createTeam, listMembers, listTeams, moveProductToTeam, removePerson, storedSession,
  type ApiMember, type ApiTeam, type PersonRole,
} from "@pixel-console/lib/pixel-api";
import { INVITATIONS, MEMBERS, TEAMS, teamName } from "@pixel-console/lib/mock-data";
import type { Member, Role } from "@pixel-console/lib/contracts";
import { normalizeEmail } from "@pixel-console/lib/mock-identity";

const ROLE_LABEL: Record<Role, string> = { org_admin: "Organization admin", team_admin: "Team admin", team_member: "Team member" };

const ROLE_WORDS: Record<string, string> = {
  org_admin: "Organization admin", team_admin: "Team admin", team_member: "Team member",
};

/**
 * The people who are really in this organization, the teams they work in, and managing both.
 *
 * Adding somebody gives their email address a place here. Nothing is emailed: they sign in with
 * that address and a code, as everyone does, and arrive in their team with its products. A team
 * member opens the products their team runs; an admin opens every product.
 */
function LivePeople() {
  const c = useConsole();
  const toast = useToast();
  const [people, setPeople] = useState<ApiMember[] | null>(null);
  const [teams, setTeams] = useState<ApiTeam[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [reloads, setReloads] = useState(0);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<PersonRole>("team_member");
  const [teamId, setTeamId] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [removing, setRemoving] = useState<ApiMember | null>(null);
  const [changing, setChanging] = useState<ApiMember | null>(null);
  const [newTeam, setNewTeam] = useState("");
  const [teamError, setTeamError] = useState<string | null>(null);
  const [makingTeam, setMakingTeam] = useState(false);
  const isAdmin = c.account?.role === "org_admin";
  const productName = (id: string) => c.visibleProducts.find((product) => product.id === id)?.name ?? id;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const session = storedSession();
        if (!session) throw new Error("Sign in to see who is in your organization.");
        const [found, foundTeams] = await Promise.all([listMembers(session), listTeams(session)]);
        if (!cancelled) { setPeople(found); setTeams(foundTeams); setProblem(null); }
      } catch (error) {
        if (!cancelled) setProblem(error instanceof Error ? error.message : "These could not be loaded.");
      }
    })();
    return () => { cancelled = true; };
  }, [c.organizationId, reloads]);

  // The team a new person joins starts as the one running the most products.
  const busiest = [...teams].sort((a, b) => b.products.length - a.products.length)[0]?.team_id ?? "";
  const chosenTeam = teams.some((team) => team.team_id === teamId) ? teamId : busiest;

  async function add(event: React.FormEvent) {
    event.preventDefault();
    const address = normalizeEmail(email);
    if (!address) { setAddError("Enter a valid email address."); return; }
    const session = storedSession();
    if (!session) return;
    setAdding(true); setAddError(null);
    try {
      await addPerson(session, address, role, chosenTeam || null);
      const where = role === "org_admin" ? "" : ` to ${teams.find((team) => team.team_id === chosenTeam)?.name ?? "their team"}`;
      toast("ok", `${address} was added${where}. They can sign in with that email now.`);
      setEmail(""); setRole("team_member"); setReloads((n) => n + 1);
    } catch (error) {
      setAddError(error instanceof Error ? error.message : "That person could not be added.");
    } finally {
      setAdding(false);
    }
  }

  async function makeTeam(event: React.FormEvent) {
    event.preventDefault();
    const name = newTeam.trim();
    if (!name) { setTeamError("Give the team a name."); return; }
    const session = storedSession();
    if (!session) return;
    setMakingTeam(true); setTeamError(null);
    try {
      const made = await createTeam(session, name);
      toast("ok", `${made.name} was created. Add people to it, or give it a product to run.`);
      setNewTeam(""); setReloads((n) => n + 1); c.reloadProducts();
    } catch (error) {
      setTeamError(error instanceof Error ? error.message : "The team could not be created.");
    } finally {
      setMakingTeam(false);
    }
  }

  async function moveProduct(productId: string, toTeam: string) {
    const session = storedSession();
    if (!session) return;
    try {
      await moveProductToTeam(session, productId, toTeam);
      toast("ok", `${productName(productId)} is now run by ${teams.find((team) => team.team_id === toTeam)?.name ?? "that team"}.`);
      setReloads((n) => n + 1); c.reloadProducts();
    } catch (error) {
      toast("danger", error instanceof Error ? error.message : "The product could not be moved.");
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

  if (problem) return <><PageHead title="People and teams" /><ErrorState title="People could not be loaded">{problem}</ErrorState></>;
  if (people === null) return <><PageHead title="People and teams" description="Loading." /><LoadingRows rows={4} /></>;

  const products = teams.flatMap((team) => team.products.map((product) => ({ product, team: team.team_id })));

  return (
    <>
      <PageHead title="People and teams"
        description={`Everyone in ${c.account?.organization_name ?? "this organization"}, the team each works in, and the products each team runs.`} />
      {isAdmin ? (
        <Panel title="Add a person">
          <form className="px-stack" onSubmit={add} noValidate>
            <p className="px-muted" style={{ margin: 0 }}>
              Enter their work email. They sign in with it and a code we email them, and they
              arrive in their team with its products.
            </p>
            <div className="px-row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
              <div style={{ flex: "1 1 240px" }}>
                <Field label="Email" error={addError}>{(f) => (
                  <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} type="email" value={email}
                    autoComplete="off" placeholder="name@company.com"
                    onChange={(e) => { setEmail(e.target.value); setAddError(null); }} />
                )}</Field>
              </div>
              <div style={{ flex: "0 1 170px" }}>
                <Field label="What they can do">{(f) => (
                  <select id={f.id} className="px-select" value={role} title={ROLE_HINT}
                    onChange={(e) => setRole(e.target.value as PersonRole)}>
                    {ROLE_CHOICES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                  </select>
                )}</Field>
              </div>
              {role !== "org_admin" && teams.length > 1 ? (
                <div style={{ flex: "0 1 180px" }}>
                  <Field label="Team">{(f) => (
                    <select id={f.id} className="px-select" value={chosenTeam}
                      onChange={(e) => setTeamId(e.target.value)}>
                      {teams.map((team) => <option key={team.team_id} value={team.team_id}>{team.name}</option>)}
                    </select>
                  )}</Field>
                </div>
              ) : null}
              <Button type="submit" variant="primary" loading={adding}><UserPlus aria-hidden />Add person</Button>
            </div>
            <p className="px-small px-muted" style={{ margin: 0 }}>{ROLE_HINT}</p>
          </form>
        </Panel>
      ) : null}
      <Panel title={`Teams (${teams.length})`}>
        <div className="px-stack">
          <ul className="px-team-cards" aria-label="Teams">
            {teams.map((team) => (
              <li key={team.team_id} className="px-team-card">
                <span className="px-avatar" aria-hidden>{initials(team.name)}</span>
                <span className="px-person-text">
                  <strong>{team.name}</strong>
                  <span>{team.people === 1 ? "1 person" : `${team.people} people`}
                    {" / "}{team.products.length ? `runs ${team.products.map(productName).join(", ")}` : "no products yet"}</span>
                </span>
              </li>
            ))}
          </ul>
          {isAdmin ? (
            <form className="px-row" style={{ alignItems: "flex-end", flexWrap: "wrap" }} onSubmit={makeTeam} noValidate>
              <div style={{ flex: "1 1 240px" }}>
                <Field label="New team" error={teamError} hint="For example Mobile, Design or Support.">{(f) => (
                  <Input id={f.id} describedBy={f.describedBy} invalid={f.invalid} value={newTeam} maxLength={60}
                    placeholder="Team name" onChange={(e) => { setNewTeam(e.target.value); setTeamError(null); }} />
                )}</Field>
              </div>
              <Button type="submit" loading={makingTeam}><Plus aria-hidden />Create team</Button>
            </form>
          ) : null}
          {isAdmin && teams.length > 1 && products.length ? (
            <div className="px-stack">
              <h4 className="px-subhead">Which team runs each product</h4>
              <p className="px-small px-muted" style={{ margin: 0 }}>
                People in that team can open it. Admins can open every product.
              </p>
              {products.map(({ product, team }) => (
                <div key={product} className="px-row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
                  <label htmlFor={`runs-${product}`}><strong>{productName(product)}</strong></label>
                  <select id={`runs-${product}`} className="px-select" style={{ maxWidth: 220 }} value={team}
                    onChange={(e) => void moveProduct(product, e.target.value)}>
                    {teams.map((option) => <option key={option.team_id} value={option.team_id}>{option.name}</option>)}
                  </select>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </Panel>
      <Panel title={`People (${people.length})`}>
        <ul className="px-people-cards" aria-label="People">
          {people.map((person) => {
            const you = person.user_id === c.account?.user_id;
            const who = person.email ?? "Invited person";
            return (
              <li key={person.user_id} className="px-person-card">
                <span className="px-avatar" aria-hidden>{initials(who.split("@")[0].replace(/[._-]+/g, " "))}</span>
                <span className="px-person-text">
                  <strong>{who}{you ? <span className="px-small px-muted"> (you)</span> : null}</strong>
                  <span>{ROLE_WORDS[person.role] ?? person.role} · {person.team_name ?? "All teams"}</span>
                </span>
                {isAdmin && !you ? (
                  <span className="px-row" style={{ gap: 4 }}>
                    <Button size="sm" variant="ghost" aria-label={`Change ${who}`}
                      onClick={() => setChanging(person)}>Change</Button>
                    <Button size="sm" variant="ghost" aria-label={`Remove ${who}`}
                      onClick={() => setRemoving(person)}>Remove</Button>
                  </span>
                ) : null}
              </li>
            );
          })}
        </ul>
      </Panel>
      <ChangePersonDialog person={changing} teams={teams} onClose={() => setChanging(null)}
        onSaved={(message) => { toast("ok", message); setChanging(null); setReloads((n) => n + 1); }} />
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

const ROLE_CHOICES: Array<[PersonRole, string]> = [
  ["team_member", "Member"],
  ["team_admin", "Team admin"],
  ["org_admin", "Admin"],
];
const ROLE_HINT = "Members use their team's products. Team admins also manage them. Admins manage everything, including people.";

/** Change what one person can do and which team they work in. */
function ChangePersonDialog({ person, teams, onClose, onSaved }: {
  person: ApiMember | null; teams: ApiTeam[]; onClose: () => void; onSaved: (message: string) => void;
}) {
  const [role, setRole] = useState<PersonRole>("team_member");
  const [team, setTeam] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!person) return;
    setRole(person.role as PersonRole);
    setTeam(person.team_id ?? teams[0]?.team_id ?? "");
    setError(null);
  }, [person, teams]);

  async function save() {
    const session = storedSession();
    if (!person || !session) return;
    setSaving(true); setError(null);
    try {
      await changePerson(session, person.user_id, role, role === "org_admin" ? null : team);
      const teamName = teams.find((option) => option.team_id === team)?.name;
      onSaved(role === "org_admin"
        ? `${person.email ?? "They"} can now manage everything.`
        : `${person.email ?? "They"} is now ${ROLE_WORDS[role].toLowerCase()} in ${teamName ?? "their team"}.`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "This could not be changed.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={person !== null} onOpenChange={(open) => { if (!open) onClose(); }}
      title={`Change ${person?.email ?? "this person"}`}
      description="It applies the next time they do anything; they don't need to sign in again."
      actions={<>
        <Button onClick={onClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" loading={saving} onClick={() => void save()}>Save</Button>
      </>}>
      <div className="px-stack">
        {error ? <Alert tone="danger" title="This could not be changed">{error}</Alert> : null}
        <Field label="What they can do" hint={ROLE_HINT}>{(f) => (
          <select id={f.id} aria-describedby={f.describedBy} className="px-select" value={role} onChange={(e) => setRole(e.target.value as PersonRole)}>
            {ROLE_CHOICES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        )}</Field>
        {role !== "org_admin" ? (
          <Field label="Team">{(f) => (
            <select id={f.id} className="px-select" value={team} onChange={(e) => setTeam(e.target.value)}>
              {teams.map((option) => <option key={option.team_id} value={option.team_id}>{option.name}</option>)}
            </select>
          )}</Field>
        ) : <p className="px-small px-muted">Admins belong to no single team and can open every product.</p>}
      </div>
    </Dialog>
  );
}

function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? "") + (words.length > 1 ? words[words.length - 1][0] : words[0]?.[1] ?? "")).toUpperCase() || "?";
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
