"""An organization's own people and name, managed by its administrators.

Adding somebody is giving an email address a place in this organization. Nothing is sent: the
person signs in with that address and a code, as everyone does, and arrives here rather than in a
new organization of their own. An address that already belongs to somebody in Pixel is refused,
because a person belongs to exactly one organization and moving them silently would take them out
of the one they are in.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthUser, require_member, require_org_admin
from app.db import get_connection
from app.definitions.console import configured_console
from app.definitions.organizations import OrganizationDirectory
from app.record_access import grant_records
from app.services.record_store import PRIMARY

router = APIRouter(prefix="/api/organizations", tags=["organization"])

_EMAIL = re.compile(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+")


class NewPerson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    role: Literal["team_member", "team_admin", "org_admin"] = "team_member"
    team_id: str | None = Field(default=None, max_length=64)


class PersonChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["team_member", "team_admin", "org_admin"]
    team_id: str | None = Field(default=None, max_length=64)


class NewTeam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)


class ProductTeam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_id: str = Field(min_length=1, max_length=64)


class Rename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)


def _own_organization(tenant_id: str, user: AuthUser) -> None:
    if tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="That organization is not available to you.")
    require_org_admin(user)


def grant_products_to(tenant_id: str, user_id: str, role: str,
                      directory: OrganizationDirectory | None = None) -> None:
    """Give one person access to the records of every product their organization runs.

    An administrator may use every record; anyone else works in the product's main workspace.
    """
    directory = directory or OrganizationDirectory()
    with get_connection() as connection:
        products = [row["product_id"] for row in connection.execute(
            "select product_id from product_bindings where tenant_id = ?", (tenant_id,)).fetchall()]
    for product_id in products:
        admin = role == "org_admin"
        grant_records(tenant_id, product_id, user_id, [] if admin else [PRIMARY], admin)


def grant_product_to_everyone(tenant_id: str, product_id: str,
                              directory: OrganizationDirectory | None = None) -> None:
    """A newly added product is open to everyone already in the organization, not only its maker."""
    directory = directory or OrganizationDirectory()
    for member in directory.members(tenant_id):
        admin = member.role == "org_admin"
        grant_records(tenant_id, product_id, member.user_id, [] if admin else [PRIMARY], admin)


def _home_team(tenant_id: str, directory: OrganizationDirectory) -> str | None:
    """The active team that runs the most of this organization's products.

    A team member can open only their own team's products, so somebody added is put where the
    products are. An organization of one team - every new one - has only that choice.
    """
    active = [team.team_id for team in directory.teams(tenant_id) if team.state == "active"]
    if not active:
        return None
    with get_connection() as connection:
        counts = {row["team_id"]: row["products"] for row in connection.execute(
            "select team_id, count(*) as products from product_bindings where tenant_id = ? "
            "group by team_id", (tenant_id,)).fetchall()}
    return max(active, key=lambda team: (counts.get(team, 0), -active.index(team)))


@router.post("/{tenant_id}/people", status_code=201)
def add_person(tenant_id: str, body: NewPerson, user: AuthUser = Depends(require_member)) -> dict:
    _own_organization(tenant_id, user)
    email = body.email.strip().lower()
    if not _EMAIL.fullmatch(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    directory = OrganizationDirectory()
    team = _active_team(tenant_id, body.team_id, directory) if body.team_id else _home_team(tenant_id, directory)
    if body.role != "org_admin" and team is None:
        raise HTTPException(status_code=409, detail="This organization has no team to add them to.")
    with get_connection() as connection:
        taken = connection.execute("select 1 from email_accounts where email = ?", (email,)).fetchone()
    if taken:
        raise HTTPException(status_code=409, detail="That address already has a Pixel account.")
    user_id = f"user-{uuid4().hex}"
    directory.add_member(tenant_id, user_id, body.role, None if body.role == "org_admin" else team)
    with get_connection() as connection:
        connection.execute("insert into email_accounts values (?, ?, ?, ?)",
                           (email, user_id, tenant_id, datetime.now(timezone.utc).isoformat()))
    grant_products_to(tenant_id, user_id, body.role, directory)
    return {"user_id": user_id, "email": email, "role": body.role}


def _active_team(tenant_id: str, team_id: str, directory: OrganizationDirectory) -> str:
    team = directory.team(tenant_id, team_id)
    if team is None or team.state != "active":
        raise HTTPException(status_code=404, detail="That team is not in this organization.")
    return team.team_id


@router.patch("/{tenant_id}/people/{user_id}")
def change_person(tenant_id: str, user_id: str, body: PersonChange,
                  user: AuthUser = Depends(require_member)) -> dict:
    """Change what somebody can do, and which team they work in. It applies from their next request.

    Their record access is set again for the new role, so a former admin keeps no admin reach.
    """
    _own_organization(tenant_id, user)
    if user_id == user.user_id:
        raise HTTPException(status_code=409, detail="You can't change your own role. Ask another admin.")
    directory = OrganizationDirectory()
    if directory.membership(tenant_id, user_id) is None:
        raise HTTPException(status_code=404, detail="That person is not in this organization.")
    team = None
    if body.role != "org_admin":
        team = _active_team(tenant_id, body.team_id, directory) if body.team_id else _home_team(tenant_id, directory)
        if team is None:
            raise HTTPException(status_code=409, detail="This organization has no team to put them in.")
    directory.set_member_role(tenant_id, user_id, body.role, team)
    with get_connection() as connection:
        connection.execute("delete from record_grants where tenant_id = ? and user_id = ?", (tenant_id, user_id))
    grant_products_to(tenant_id, user_id, body.role, directory)
    return {"user_id": user_id, "role": body.role, "team_id": team}


def _team_slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "team"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


@router.get("/{tenant_id}/teams")
def list_teams(tenant_id: str, user: AuthUser = Depends(require_member)) -> dict:
    """Every active team, with how many people work in it and which products it runs."""
    if tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="That organization is not available to you.")
    directory = OrganizationDirectory()
    people: dict[str, int] = {}
    for member in directory.members(tenant_id):
        if member.team_id:
            people[member.team_id] = people.get(member.team_id, 0) + 1
    with get_connection() as connection:
        bound = connection.execute(
            "select product_id, team_id from product_bindings where tenant_id = ? and state = 'active' "
            "order by product_id", (tenant_id,)).fetchall()
    console = _console_product_id()
    products: dict[str, list[str]] = {}
    for row in bound:
        if row["product_id"] != console:
            products.setdefault(row["team_id"], []).append(row["product_id"])
    return {"tenant_id": tenant_id, "teams": [
        {"team_id": team.team_id, "name": team.name, "people": people.get(team.team_id, 0),
         "products": products.get(team.team_id, [])}
        for team in directory.teams(tenant_id) if team.state == "active"
    ]}


@router.post("/{tenant_id}/teams", status_code=201)
def create_team(tenant_id: str, body: NewTeam, user: AuthUser = Depends(require_member)) -> dict:
    _own_organization(tenant_id, user)
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(status_code=422, detail="Give the team a name.")
    directory = OrganizationDirectory()
    existing = directory.teams(tenant_id)
    if any(team.name.lower() == name.lower() for team in existing):
        raise HTTPException(status_code=409, detail=f"There is already a team called {name}.")
    team = directory.create_team(tenant_id, _team_slug(name, {team.team_id for team in existing}), name)
    return {"team_id": team.team_id, "name": team.name, "people": 0, "products": []}


@router.put("/{tenant_id}/products/{product_id}/team")
def move_product(tenant_id: str, product_id: str, body: ProductTeam,
                 user: AuthUser = Depends(require_member)) -> dict:
    """Choose which team runs a product. Its members can open it; the old team's can no longer."""
    _own_organization(tenant_id, user)
    directory = OrganizationDirectory()
    binding = directory.product(tenant_id, product_id)
    if binding is None or product_id == _console_product_id():
        raise HTTPException(status_code=404, detail="That product is not in this organization.")
    team = _active_team(tenant_id, body.team_id, directory)
    if binding.team_id != team:
        directory.transfer_product(tenant_id, product_id, team)
    return {"product_id": product_id, "team_id": team}


def _console_product_id() -> str | None:
    console = configured_console()
    return console.product_id if console else None


@router.delete("/{tenant_id}/people/{user_id}", status_code=204)
def remove_person(tenant_id: str, user_id: str, user: AuthUser = Depends(require_member)) -> None:
    _own_organization(tenant_id, user)
    if user_id == user.user_id:
        raise HTTPException(status_code=409, detail="You can't remove yourself.")
    directory = OrganizationDirectory()
    if directory.membership(tenant_id, user_id) is None:
        raise HTTPException(status_code=404, detail="That person is not in this organization.")
    with get_connection() as connection:
        connection.execute("begin immediate")
        # Their sessions end now, not when they next expire.
        connection.execute("delete from login_sessions where user_id = ?", (user_id,))
        connection.execute("delete from record_grants where tenant_id = ? and user_id = ?", (tenant_id, user_id))
        connection.execute("delete from memberships where tenant_id = ? and user_id = ?", (tenant_id, user_id))
        connection.execute("delete from email_accounts where tenant_id = ? and user_id = ?", (tenant_id, user_id))


@router.patch("/{tenant_id}")
def rename_organization(tenant_id: str, body: Rename, user: AuthUser = Depends(require_member)) -> dict:
    _own_organization(tenant_id, user)
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(status_code=422, detail="Give the organization a name.")
    with get_connection() as connection:
        connection.execute("update organizations set name = ? where tenant_id = ?", (name, tenant_id))
    return {"tenant_id": tenant_id, "name": name}
