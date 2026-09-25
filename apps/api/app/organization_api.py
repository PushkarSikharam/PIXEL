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
from app.definitions.organizations import OrganizationDirectory
from app.record_access import grant_records
from app.services.record_store import PRIMARY

router = APIRouter(prefix="/api/organizations", tags=["organization"])

_EMAIL = re.compile(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+")


class NewPerson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    role: Literal["team_member", "team_admin", "org_admin"] = "team_member"


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
    team = _home_team(tenant_id, directory)
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
