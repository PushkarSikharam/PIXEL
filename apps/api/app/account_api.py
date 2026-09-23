"""Email possession verification, separate from synthetic identities."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthUser, create_token, require_auth, require_member
from app.db import get_connection
from app.services.env import env_bool, env_value

router = APIRouter(prefix="/api/account", tags=["account"])


class EmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)


class CodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(min_length=32, max_length=64)
    code: str = Field(pattern=r"^[0-9]{8}$")


def _settings() -> tuple[str, ...]:
    values = tuple(env_value(name) or "" for name in (
        "PIXEL_AUTH_SECRET", "PIXEL_SMTP_HOST", "PIXEL_SMTP_USER",
        "PIXEL_SMTP_PASSWORD", "PIXEL_EMAIL_FROM",
    ))
    if not env_bool("PIXEL_EMAIL_LOGIN_ENABLED", default=False) or not all(values):
        raise HTTPException(503, "Email sign-in is not configured. Shared access is disabled.")
    if env_value("PIXEL_ENGINE_MODE") != "definition":
        raise HTTPException(503, "Customer workspaces require the definition engine before sign-in can be enabled.")
    return values


def _digest(secret: str, text: str) -> str:
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()


def send_code(email: str, code: str, settings: tuple[str, ...]) -> None:
    if env_bool("PIXEL_BLOCK_EXTERNAL_HTTP", default=False):
        raise OSError("External delivery is disabled in this environment")
    _, host, username, password, sender = settings
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, email, "Your Pixel sign-in code"
    message.set_content(f"Your Pixel code is {code}. It expires in 10 minutes. "
                        "Do not share this code. If you did not request it, ignore this email.")
    with smtplib.SMTP_SSL(host, 465, timeout=10, context=ssl.create_default_context()) as smtp:
        smtp.login(username, password)
        smtp.send_message(message)


@router.post("/email-code")
def request_code(body: EmailRequest, response: Response) -> dict:
    settings = _settings()
    email = body.email.strip().lower()
    if not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", email):
        raise HTTPException(422, "Enter a valid email address.")
    now = time.time()
    challenge_id, code = secrets.token_hex(24), f"{secrets.randbelow(100000000):08d}"
    limited = False
    with get_connection() as connection:
        connection.execute("begin immediate")
        connection.execute("delete from email_challenges where expires_at < ?", (now,))
        connection.execute("delete from email_login_limits where starts_at < ?", (now - 3600,))
        # A deployment-wide ceiling also limits rotating-address abuse behind proxies.
        for bucket, maximum in (("global", 100), (_digest(settings[0], email), 5)):
            row = connection.execute("select attempts from email_login_limits where bucket=?", (bucket,)).fetchone()
            if row and row[0] >= maximum:
                limited = True
        if not limited:
            for bucket in ("global", _digest(settings[0], email)):
                connection.execute("insert into email_login_limits values (?, ?, 1) "
                                   "on conflict(bucket) do update set attempts=attempts+1", (bucket, now))
            connection.execute("update email_challenges set consumed=1 where email=?", (email,))
            connection.execute("insert into email_challenges values (?, ?, ?, ?, 0, 0)",
                               (challenge_id, email, _digest(settings[0], challenge_id + code), now + 600))
    if limited:
        raise HTTPException(429, "Too many code requests. Please try again later.", headers={"Retry-After": "3600"})
    try:
        send_code(email, code, settings)
    except (OSError, smtplib.SMTPException):
        with get_connection() as connection:
            connection.execute("update email_challenges set consumed=1 where challenge_id=?", (challenge_id,))
        raise HTTPException(503, "Email could not be delivered. Please try again later.") from None
    response.headers["Cache-Control"] = "no-store"
    return {"challenge_id": challenge_id, "expires_in": 600}


@router.post("/verify-code")
def verify_code(body: CodeRequest, response: Response) -> dict:
    settings = _settings()
    now = time.time()
    account = None
    with get_connection() as connection:
        connection.execute("begin immediate")
        row = connection.execute("select * from email_challenges where challenge_id=?", (body.challenge_id,)).fetchone()
        if row and not row["consumed"] and row["expires_at"] > now and row["attempts"] < 5:
            connection.execute("update email_challenges set attempts=attempts+1 where challenge_id=?", (body.challenge_id,))
            if hmac.compare_digest(row["code_digest"], _digest(settings[0], body.challenge_id + body.code)):
                connection.execute("update email_challenges set consumed=1 where challenge_id=?", (body.challenge_id,))
                account = connection.execute("select * from email_accounts where email=?", (row["email"],)).fetchone()
                if account is None and env_bool("PIXEL_SELF_SIGNUP_ENABLED", default=False):
                    user_id, tenant_id = f"user-{uuid4().hex}", f"org-{uuid4().hex}"
                    created = datetime.now(timezone.utc).isoformat()
                    connection.execute("insert into organizations values (?, ?, 'active', ?)", (tenant_id, "My organization", created))
                    connection.execute("insert into teams values (?, 'default', 'My team', 'active', ?)", (tenant_id, created))
                    connection.execute("insert into memberships values (?, ?, 'org_admin', null)", (tenant_id, user_id))
                    connection.execute("insert into email_accounts values (?, ?, ?, ?)", (row["email"], user_id, tenant_id, created))
                    account = {"user_id": user_id, "tenant_id": tenant_id}
    if account is None:
        raise HTTPException(401, "The code is invalid, expired, or this account has no access.")
    with get_connection() as connection:
        organization = connection.execute("select state from organizations where tenant_id=?", (account["tenant_id"],)).fetchone()
    if not organization or organization[0] != "active":
        raise HTTPException(403, "This account is unavailable.")
    response.headers["Cache-Control"] = "no-store"
    return {"token": create_token(account["user_id"], account["tenant_id"]),
            "user_id": account["user_id"], "tenant_id": account["tenant_id"]}


@router.get("/session")
def account_session(response: Response, user: AuthUser = Depends(require_auth)) -> dict:
    require_member(user)
    with get_connection() as connection:
        account = connection.execute("select email from email_accounts where user_id=?", (user.user_id,)).fetchone()
        organization = connection.execute("select name from organizations where tenant_id=?", (user.tenant_id,)).fetchone()
        teams = connection.execute("select team_id, name from teams where tenant_id=? and state='active' "
                                   "and (?='org_admin' or team_id=?)", (user.tenant_id, user.role, user.team_id)).fetchall()
    response.headers["Cache-Control"] = "no-store"
    return {"user_id": user.user_id, "email": account[0] if account else None,
            "tenant_id": user.tenant_id, "organization_name": organization[0],
            "role": user.role, "team_id": user.team_id,
            "teams": [dict(team) for team in teams]}


@router.post("/logout", status_code=204)
def logout(user: AuthUser = Depends(require_auth), authorization: str = Header()) -> Response:
    require_member(user)
    token = authorization.removeprefix("Bearer ").strip()
    with get_connection() as connection:
        connection.execute("delete from login_sessions where token_hash=? and user_id=?",
                           (hashlib.sha256(token.encode()).hexdigest(), user.user_id))
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
