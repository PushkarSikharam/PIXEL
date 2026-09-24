"""Token authentication for organization members and product visitors.

Two security domains:
- Members belong to an organization (organization admin, team admin or team member). Their
  organization comes from their membership, never from deployment configuration.
- Visitors are not organization members. A visitor token is issued for exactly one product and
  grants nothing else.

Tokens are signed with itsdangerous and must also match an active login row. In production the
member login can come from verified email codes; the passwordless demo login exists only for
isolated synthetic demos.

Record grants (workspace scopes and record administration inside a product's data) are not
part of the principal. They are resolved per organization and product for each request; see
`app/record_access.py`.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from uuid import uuid4

from fastapi import Cookie, Depends, Header, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.db import get_connection
from app.definitions.access import AccessDenied, authorize_product
from app.definitions.organizations import OrganizationDirectory
from app.definitions.sessions import DefinitionUnavailable, pin_new_session
from app.record_access import RecordGrant, legacy_record_owner, record_grant
from app.services.env import env_bool, env_value
from app.services.demo_instances import (
    DemoContext,
    DemoInstanceStore,
    InstanceUnavailable,
    SeedNotApproved,
)
from app.installed_products import PackageMissing, package_for

# The secret rotates per process; tokens don't survive a server restart,
# which is fine for the demo.  Set PIXEL_AUTH_SECRET for stable tokens.
_SECRET = env_value("PIXEL_AUTH_SECRET") or os.urandom(32).hex()
_SERIALIZER = URLSafeTimedSerializer(_SECRET)
# As long as the session cookie may live, so a cookie the browser still holds is still honoured.
# A shorter window here would sign people out while their browser believed they were signed in.
_TOKEN_MAX_AGE_SECONDS = int(os.environ.get("PIXEL_TOKEN_MAX_AGE", str(7 * 86400)))
SESSION_COOKIE = "pixel_session"
CSRF_COOKIE = "pixel_csrf"
CSRF_HEADER = "X-Pixel-CSRF"


@dataclass(frozen=True)
class AuthUser:
    kind: str  # "member" or "visitor"
    user_id: str
    tenant_id: str
    role: str | None = None
    team_id: str | None = None
    product_id: str | None = None
    instance_id: str | None = None
    instance_generation: int | None = None

    @property
    def demo_context(self) -> DemoContext | None:
        if self.kind != "visitor" or not self.product_id or not self.instance_id or not self.instance_generation:
            return None
        return DemoContext(
            self.tenant_id, self.product_id, self.user_id,
            self.instance_id, self.instance_generation,
        )


def demo_login_enabled() -> bool:
    """Passwordless demo login is only for deployments explicitly marked as isolated synthetic
    demos. It is never customer access."""
    return env_bool("PIXEL_SYNTHETIC_DEMO", default=False)


def demo_identity_allowed(user_id: str, tenant_id: str) -> bool:
    """Whether the public demo login may sign this identity in.

    The rule is structural: the public demo never hands out an administrator's token. Anyone can
    call the demo login with any user ID, so a restriction that lived in the web app (which user
    it happens to send) would restrict nothing. An administrator here means an organization admin,
    or a member holding administration over the product's records (who can reset everyone's data).

    `PIXEL_DEMO_ADMIN_LOGIN=true` lifts the administrator rule. It exists only for isolated test
    harnesses that start their own backend and database; it defaults off and a deployment must
    never set it. `PIXEL_DEMO_LOGIN_USERS`, when set, narrows the demo further to a fixed list.
    """
    listed = env_value("PIXEL_DEMO_LOGIN_USERS")
    if listed:
        allowed = {name.strip() for name in listed.split(",") if name.strip()}
        if user_id not in allowed:
            return False
    if env_bool("PIXEL_DEMO_ADMIN_LOGIN", default=False):
        return True
    membership = OrganizationDirectory().membership(tenant_id, user_id)
    if membership is None or membership.role == "org_admin":
        return False
    owner = legacy_record_owner()
    if owner is not None and owner.tenant_id == tenant_id:
        grant = record_grant(tenant_id, owner.product_id, user_id)
        if grant is not None and grant.is_admin:
            return False
    return True


def create_token(user_id: str, tenant_id: str | None = None) -> str:
    """Create a member token for one organization the user belongs to.

    Without an explicit organization, the user must belong to exactly one.
    """
    directory = OrganizationDirectory()
    if tenant_id is None:
        organizations = directory.organizations_of(user_id)
        if len(organizations) != 1:
            raise ValueError(f"{user_id} must name one of their organizations")
        tenant_id = organizations[0]
    if directory.membership(tenant_id, user_id) is None:
        raise ValueError(f"{user_id} is not a member of {tenant_id}")

    # The nonce keeps two logins in the same second from producing identical tokens.
    token = _SERIALIZER.dumps({"k": "member", "uid": user_id, "tid": tenant_id, "n": secrets.token_hex(8)})
    with get_connection() as connection:
        # login_sessions references the legacy access_grants table, which grants nothing any more.
        connection.execute(
            "insert or ignore into access_grants(user_id, scope_ids, is_admin) values (?, '[]', 0)",
            (user_id,),
        )
        connection.execute("delete from login_sessions where user_id = ? and expires_at < ?", (user_id, time.time()))
        # The login_sessions.customer_id column holds the organization (tenant) ID.
        connection.execute(
            "insert into login_sessions(token_hash, user_id, customer_id, expires_at) values (?, ?, ?, ?)",
            (_hash(token), user_id, tenant_id, time.time() + _TOKEN_MAX_AGE_SECONDS),
        )
    return token


def create_visitor_token(tenant_id: str, product_id: str) -> tuple[str, str]:
    """Start a visitor session for one product. Returns (token, visitor_id).

    Raises AccessDenied unless the product exists, is active, and accepts visitors.
    """
    visitor_id = f"visitor-{uuid4()}"
    directory = OrganizationDirectory()
    access = authorize_product(_visitor(visitor_id, tenant_id, product_id), product_id, directory)
    try:
        # Allocation is useful only when this product can start a conversation. Check before the
        # write transaction so a broken or revoked definition cannot consume visitor capacity.
        pin_new_session(access, directory.definitions)
    except DefinitionUnavailable as unavailable:
        raise AccessDenied("definition_unavailable") from unavailable
    try:
        package = package_for(access.binding.definition_id)
    except PackageMissing as missing:
        raise AccessDenied("demo_package_unavailable") from missing
    if package.demo_seed_factory is None:
        raise AccessDenied("demo_seed_unavailable")
    try:
        seed = package.demo_seed_factory()
    except SeedNotApproved as unapproved:
        # An unreviewed seed change must not become what a new visitor sees.
        raise AccessDenied("demo_seed_not_approved") from unapproved
    instances = DemoInstanceStore()
    # The login and every seeded record commit together. A valid token can never point at a
    # partial instance, and a failed login leaves no orphan consuming capacity.
    with get_connection() as connection:
        connection.execute("begin immediate")
        context = instances.allocate(tenant_id, product_id, visitor_id, seed, connection=connection)
        # Sign the final server-allocated instance, never an ID supplied by the browser.
        token = _SERIALIZER.dumps(
            {"k": "visitor", "vid": visitor_id, "tid": tenant_id, "pid": product_id,
             "iid": context.instance_id, "gen": context.generation, "n": secrets.token_hex(8)}
        )
        connection.execute("delete from visitor_logins where expires_at < ?", (time.time(),))
        connection.execute(
            "insert into visitor_logins(token_hash, visitor_id, tenant_id, product_id, expires_at, "
            "instance_id, instance_generation, seed_version) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (_hash(token), visitor_id, tenant_id, product_id, time.time() + _TOKEN_MAX_AGE_SECONDS,
             context.instance_id, context.generation, seed.version),
        )
    return token, visitor_id


def visitor_from_token(token: str) -> AuthUser:
    """Resolve a token the server just issued through the persisted visitor-login boundary."""
    payload, encoded = _decode(f"Bearer {token}")
    if payload.get("k") != "visitor":
        raise HTTPException(status_code=401, detail="Invalid visitor token.")
    return _visitor_from(payload, encoded)


def replace_visitor_token(user: AuthUser, context: DemoContext, seed_version: str,
                          connection) -> str:
    """Rotate a visitor token after reset. The old generation and token stop working at commit."""
    if user.demo_context is None or (
        user.tenant_id, user.product_id, user.user_id, user.instance_id
    ) != (context.tenant_id, context.product_id, context.visitor_id, context.instance_id):
        raise HTTPException(status_code=403, detail="This demo cannot be reset.")
    token = _SERIALIZER.dumps(
        {"k": "visitor", "vid": user.user_id, "tid": user.tenant_id, "pid": user.product_id,
         "iid": context.instance_id, "gen": context.generation, "n": secrets.token_hex(8)}
    )
    connection.execute(
        "delete from visitor_logins where visitor_id=? and tenant_id=? and product_id=?",
        (user.user_id, user.tenant_id, user.product_id),
    )
    connection.execute(
        "insert into visitor_logins(token_hash, visitor_id, tenant_id, product_id, expires_at, "
        "instance_id, instance_generation, seed_version) values (?, ?, ?, ?, ?, ?, ?, ?)",
        (_hash(token), user.user_id, user.tenant_id, user.product_id,
         time.time() + _TOKEN_MAX_AGE_SECONDS, context.instance_id, context.generation, seed_version),
    )
    return token


def require_auth(request: Request, authorization: str | None = Header(default=None),
                 pixel_session: str | None = Cookie(default=None),
                 x_pixel_csrf: str | None = Header(default=None, alias=CSRF_HEADER),
                 pixel_csrf: str | None = Cookie(default=None)) -> AuthUser:
    """FastAPI dependency: validate a member or visitor bearer token.

    Raises 401 for missing, malformed, expired or revoked tokens, and 403 when the
    organization is suspended.
    """
    if authorization:
        payload, token = _decode(authorization)
    else:
        if not pixel_session:
            raise HTTPException(status_code=401, detail="Authorization header is required.")
        if _unsafe_method(request) and (not pixel_csrf or not x_pixel_csrf
                                       or not secrets.compare_digest(pixel_csrf, x_pixel_csrf)):
            raise HTTPException(status_code=403, detail="CSRF token is missing or invalid.")
        payload, token = _decode_cookie(pixel_session)
    kind = payload.get("k", "member")
    if kind == "visitor":
        user = _visitor_from(payload, token)
    elif kind == "member":
        user = _member_from(payload, token)
    else:
        raise HTTPException(status_code=401, detail="Invalid token payload.")

    organization = OrganizationDirectory().organization(user.tenant_id)
    if organization is None:
        raise HTTPException(status_code=401, detail="Organization no longer exists.")
    if organization.state != "active":
        raise HTTPException(status_code=403, detail="This organization is suspended.")
    return user


def _unsafe_method(request: Request) -> bool:
    return request.method.upper() not in {"GET", "HEAD", "OPTIONS"}


def require_member(user: AuthUser = Depends(require_auth)) -> AuthUser:
    """FastAPI dependency: organization members only; visitors are refused."""
    if user.kind != "member":
        raise HTTPException(status_code=403, detail="This operation requires an organization member.")
    return user


def require_org_admin(user: AuthUser) -> None:
    if user.kind != "member" or user.role != "org_admin":
        raise HTTPException(status_code=403, detail="This operation requires an organization administrator.")


def require_record_access(user: AuthUser = Depends(require_auth)) -> RecordGrant:
    """FastAPI dependency for the legacy record endpoints.

    The caller must belong to the organization of the designated record-owning product, be
    allowed to use that product (active organization, team and binding), and hold a record
    grant for it. Anything else gets the same 403.
    """
    owner = legacy_record_owner()
    if owner is None or owner.tenant_id != user.tenant_id:
        raise _no_record_access()
    return product_record_grant(user, owner.product_id)


def product_record_grant(user: AuthUser, product_id: str) -> RecordGrant:
    """The caller's record grant for one product of their organization, or 403."""
    try:
        authorize_product(user, product_id)
    except AccessDenied:
        raise _no_record_access()
    if user.kind == "visitor":
        context = user.demo_context
        if context is None:
            raise _no_record_access()
        from app.services.product_data_store import ProductDataStore
        scopes = ProductDataStore(context).load()["workspaceScopes"]
        return RecordGrant(
            scope_ids=frozenset(scope["id"] for scope in scopes),
            is_admin=False,
            demo_context=context,
        )
    grant = record_grant(user.tenant_id, product_id, user.user_id)
    if grant is None:
        raise _no_record_access()
    return grant


def require_any_scope(scope_ids: set[str], grant: RecordGrant) -> None:
    """Raise 403 unless the grant covers at least one of the record's scopes."""
    if not grant.may_use_any(scope_ids):
        raise HTTPException(status_code=403, detail="You do not have access to this workspace.")


def require_scope(scope_id: str, grant: RecordGrant) -> None:
    if not grant.may_use(scope_id):
        raise HTTPException(status_code=403, detail="You do not have access to this workspace.")


def require_record_admin(grant: RecordGrant) -> None:
    if not grant.is_admin:
        raise HTTPException(status_code=403, detail="This operation requires administrator access.")


def _no_record_access() -> HTTPException:
    return HTTPException(status_code=403, detail="You do not have access to these records.")


def _decode(authorization: str | None) -> tuple[dict, str]:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header is required.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing or malformed bearer token.")
    try:
        payload = _SERIALIZER.loads(token, max_age=_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired:
        raise HTTPException(status_code=401, detail="Token has expired.")
    except BadSignature:
        raise HTTPException(status_code=401, detail="Invalid token.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail="Invalid token payload.")
    return payload, token


def _decode_cookie(token: str | None) -> tuple[dict, str]:
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header is required.")
    return _loads_token(token)


def _loads_token(token: str) -> tuple[dict, str]:
    try:
        payload = _SERIALIZER.loads(token, max_age=_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired:
        raise HTTPException(status_code=401, detail="Token has expired.")
    except BadSignature:
        raise HTTPException(status_code=401, detail="Invalid token.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail="Invalid token payload.")
    return payload, token


def _member_from(payload: dict, token: str) -> AuthUser:
    user_id, tenant_id = payload.get("uid"), payload.get("tid")
    if not user_id or not tenant_id:
        raise HTTPException(status_code=401, detail="Invalid token payload.")
    with get_connection() as connection:
        active = connection.execute(
            "select 1 from login_sessions where token_hash = ? and customer_id = ? and expires_at > ?",
            (_hash(token), tenant_id, time.time()),
        ).fetchone()
    if active is None:
        raise HTTPException(status_code=401, detail="Token is not active.")
    membership = OrganizationDirectory().membership(tenant_id, user_id)
    if membership is None:
        raise HTTPException(status_code=401, detail="User is no longer a member of this organization.")
    return AuthUser(
        kind="member",
        user_id=user_id,
        tenant_id=tenant_id,
        role=membership.role,
        team_id=membership.team_id,
    )


def _visitor_from(payload: dict, token: str) -> AuthUser:
    visitor_id, tenant_id, product_id = payload.get("vid"), payload.get("tid"), payload.get("pid")
    instance_id, generation = payload.get("iid"), payload.get("gen")
    if not visitor_id or not tenant_id or not product_id or not instance_id or type(generation) is not int:
        raise HTTPException(status_code=401, detail="Invalid token payload.")
    with get_connection() as connection:
        active = connection.execute(
            "select 1 from visitor_logins where token_hash = ? and visitor_id = ? and tenant_id = ? "
            "and product_id = ? and instance_id = ? and instance_generation = ? and expires_at > ?",
            (_hash(token), visitor_id, tenant_id, product_id, instance_id, generation, time.time()),
        ).fetchone()
    if active is None:
        raise HTTPException(status_code=401, detail="Visitor session is not active.")
    user = _visitor(visitor_id, tenant_id, product_id, instance_id, generation)
    try:
        # Successful visitor activity extends only the idle deadline. The absolute lifetime is
        # fixed when the instance is allocated and cannot be extended by repeated requests.
        DemoInstanceStore().touch(user.demo_context)
    except InstanceUnavailable:
        raise HTTPException(status_code=401, detail="Visitor session is not active.")
    return user


def _visitor(visitor_id: str, tenant_id: str, product_id: str,
             instance_id: str | None = None, generation: int | None = None) -> AuthUser:
    return AuthUser(
        kind="visitor", user_id=visitor_id, tenant_id=tenant_id, product_id=product_id,
        instance_id=instance_id, instance_generation=generation,
    )


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

