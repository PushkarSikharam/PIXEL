"""Email possession verification, separate from synthetic identities."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib import error as url_error
from urllib import request as url_request
from uuid import uuid4

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

import logging

from app.auth import CSRF_COOKIE, SESSION_COOKIE, AuthUser, create_token, require_auth, require_member
from app.definitions.console import configured_console, ensure_console_product
from app.definitions.organizations import OrganizationDirectory
from app.db import get_connection
from app.services.env import env_bool, env_value

logger = logging.getLogger(__name__)
# Codes one address may ask for in an hour, and the ceiling for the whole deployment. The second
# is deliberately far above the first: it is an abuse ceiling, never a queue everyone shares.
ADDRESS_CODES_PER_HOUR = 5
# What a brand new organization is called until somebody renames it.
DEFAULT_ORGANIZATION_NAME = "My organization"
DEFAULT_TEAM_ID, DEFAULT_TEAM_NAME = "default", "My team"
DEPLOYMENT_CODES_PER_HOUR = 5000
DEPLOYMENT_BUCKET = "deployment"
router = APIRouter(prefix="/api/account", tags=["account"])
# A week. Somebody using Pixel daily should sign in about as often as they sign in to anything
# else they work in; a day meant closing the browser cost them their session. Revocation is what
# bounds a stolen cookie, not a short window: signing out deletes the session row, and every
# request checks that row exists.
SESSION_COOKIE_MAX_AGE = 7 * 86400
# How long to wait for a mail server to accept a code.
SMTP_TIMEOUT_SECONDS = 20


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
    if host == "smtp.resend.com" and password.startswith("re_"):
        send_code_with_resend_api(email, code, password, sender)
        return
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, email, "Your Pixel sign-in code"
    message.set_content(f"Your Pixel code is {code}. It expires in 10 minutes. "
                        "Do not share this code. If you did not request it, ignore this email.")
    try:
        # Long enough for a provider that authenticates slowly on a cold connection. Ten seconds
        # was enough for an API call and is tight for a full SMTP handshake and login.
        with smtplib.SMTP_SSL(host, 465, timeout=SMTP_TIMEOUT_SECONDS,
                              context=ssl.create_default_context()) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    except smtplib.SMTPException as refused:
        # What the mail server actually said, in the operator log only - never the code, never
        # the password. A refusal here is nearly always a configuration somebody has to change:
        # a sender that is not the account that authenticated, a password that is not an
        # application password, a provider that will not send to this recipient. Without this
        # the only evidence is "could not be delivered", which says none of that.
        logger.warning("email_delivery_refused",
                       extra={"host": host, "reason": f"{type(refused).__name__}: {refused}"[:400]})
        raise
    except OSError as unreachable:
        logger.warning("email_delivery_unreachable",
                       extra={"host": host, "reason": str(unreachable)[:400]})
        raise


def send_code_with_resend_api(email: str, code: str, api_key: str, sender: str) -> None:
    payload = json.dumps({
        "from": sender,
        "to": [email],
        "subject": "Your Pixel sign-in code",
        "text": (f"Your Pixel code is {code}. It expires in 10 minutes. "
                 "Do not share this code. If you did not request it, ignore this email."),
    }).encode("utf-8")
    request = url_request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Pixel/1.0",
        },
        method="POST",
    )
    try:
        with url_request.urlopen(request, timeout=10) as response:
            if response.status >= 400:
                raise OSError("Resend rejected the email")
    except url_error.HTTPError as refused:
        # What the provider actually said, in the operator log only. A refusal here is usually a
        # configuration somebody has to change rather than anything the person signing in did -
        # a sending domain that was never verified, for instance, which quietly limits delivery
        # to the account owner's own address and makes every other sign-in look broken. Without
        # this the only evidence was "could not be delivered", which reads like a passing fault
        # and is not one.
        detail = ""
        try:
            detail = refused.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001 - the reason for the reason is not worth failing over
            detail = "(no body)"
        logger.warning("email_delivery_refused", extra={"status": refused.code, "detail": detail})
        raise OSError("Resend delivery failed") from refused
    except (url_error.URLError, TimeoutError) as unreachable:
        logger.warning("email_delivery_unreachable", extra={"reason": str(unreachable)})
        raise OSError("Resend delivery failed") from unreachable


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
        # One address is limited tightly. The deployment-wide ceiling exists to blunt abuse from
        # rotating addresses, so it is set far above ordinary use: a ceiling low enough for one
        # attacker to exhaust would let them lock every other customer out of signing in, which
        # is a worse failure than the abuse it prevents.
        buckets = ((DEPLOYMENT_BUCKET, DEPLOYMENT_CODES_PER_HOUR),
                   (_digest(settings[0], email), ADDRESS_CODES_PER_HOUR))
        for bucket, maximum in buckets:
            row = connection.execute("select attempts from email_login_limits where bucket=?", (bucket,)).fetchone()
            if row and row[0] >= maximum:
                limited = True
        if not limited:
            for bucket, _ in buckets:
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
            # A code that never arrived was not an attempt somebody made, so their own hourly
            # allowance is given back: a failing mail server must not lock a person out of their
            # own account. The deployment ceiling keeps its count, so repeated failures are still
            # bounded overall rather than becoming a way to hammer the mail server for free.
            connection.execute(
                "update email_login_limits set attempts = max(attempts - 1, 0) where bucket = ?",
                (_digest(settings[0], email),))
        raise HTTPException(
            503,
            "We could not send a code to that address. Check the address is right - and if it is, "
            "this Pixel cannot send email to it yet, so tell whoever runs it.",
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return {"challenge_id": challenge_id, "expires_in": 600}


@router.post("/verify-code")
def verify_code(body: CodeRequest, response: Response) -> dict:
    settings = _settings()
    now = time.time()
    account, register_for, proved_address = None, None, False
    with get_connection() as connection:
        connection.execute("begin immediate")
        row = connection.execute("select * from email_challenges where challenge_id=?", (body.challenge_id,)).fetchone()
        if row and not row["consumed"] and row["expires_at"] > now and row["attempts"] < 5:
            connection.execute("update email_challenges set attempts=attempts+1 where challenge_id=?", (body.challenge_id,))
            if hmac.compare_digest(row["code_digest"], _digest(settings[0], body.challenge_id + body.code)):
                connection.execute("update email_challenges set consumed=1 where challenge_id=?", (body.challenge_id,))
                # They read a code we sent to this address, so they hold that inbox. Nothing said
                # to them from here can disclose anything about anybody else.
                proved_address = True
                account = connection.execute("select * from email_accounts where email=?", (row["email"],)).fetchone()
                if account is None and env_bool("PIXEL_SELF_SIGNUP_ENABLED", default=False):
                    register_for = row["email"]
    if register_for is not None:
        account = _register(register_for)
    if account is None:
        # Two different things used to be one sentence. Somebody who typed their code correctly
        # was told it was invalid, and went looking for a fault in the code rather than learning
        # that this deployment does not hand out accounts. Saying which is safe: the only way to
        # reach this branch is to have proved control of the address the code was sent to.
        raise HTTPException(403 if proved_address else 401,
                            "Pixel is invite-only here, so this address cannot open a workspace yet. "
                            "Ask whoever runs this Pixel for an invitation."
                            if proved_address else
                            "That code is not right, or it has expired. Ask for a new one.")
    # Every sign-in, not only the first. Moving around Pixel is Pixel's own, so an organization
    # should be on the version the platform ships rather than the one that existed the day they
    # signed up; doing this only at registration left accounts years behind without a symptom
    # anyone could see. It is idempotent, it only ever touches the application's own product, and
    # it is not worth failing a sign-in over - but a failure is reported, because an assistant
    # silently stuck on an old version is exactly what this is here to prevent.
    try:
        ensure_console_product(OrganizationDirectory(), account["tenant_id"], DEFAULT_TEAM_ID,
                               (account["user_id"],))
    except Exception:  # noqa: BLE001 - never block somebody signing in
        logger.exception("console_product_not_ensured", extra={"tenant_id": account["tenant_id"]})
    with get_connection() as connection:
        organization = connection.execute("select state from organizations where tenant_id=?", (account["tenant_id"],)).fetchone()
    if not organization or organization[0] != "active":
        raise HTTPException(403, "This account is unavailable.")
    token = create_token(account["user_id"], account["tenant_id"])
    csrf = secrets.token_urlsafe(32)
    _set_session_cookies(response, token, csrf)
    response.headers["Cache-Control"] = "no-store"
    return {"csrf_token": csrf, "user_id": account["user_id"], "tenant_id": account["tenant_id"]}


def _register(email: str) -> dict:
    """A first sign-in becomes an organization of one, built the way every organization is.

    Made through the directory rather than by writing its rows, so a new organization is checked
    and shaped exactly as any other, and cannot go quietly wrong the day a column moves. It is
    created after the code has been settled, because the directory keeps its own transaction; a
    failure here leaves a used code and no account, which is recoverable by asking for another.
    """
    user_id, tenant_id = f"user-{uuid4().hex}", f"org-{uuid4().hex}"
    directory = OrganizationDirectory()
    try:
        directory.create_organization(tenant_id, DEFAULT_ORGANIZATION_NAME)
        directory.create_team(tenant_id, DEFAULT_TEAM_ID, DEFAULT_TEAM_NAME)
        directory.add_member(tenant_id, user_id, "org_admin")
        with get_connection() as connection:
            connection.execute("insert into email_accounts values (?, ?, ?, ?)",
                               (email, user_id, tenant_id, datetime.now(timezone.utc).isoformat()))
    except Exception:  # noqa: BLE001 - the caller is told plainly and can ask for another code
        logger.warning("organization_not_created")
        raise HTTPException(503, "Your workspace could not be created. Please try again.") from None
    return {"user_id": user_id, "tenant_id": tenant_id}


@router.get("/session")
def account_session(response: Response, user: AuthUser = Depends(require_auth)) -> dict:
    require_member(user)
    with get_connection() as connection:
        account = connection.execute("select email from email_accounts where user_id=?", (user.user_id,)).fetchone()
        organization = connection.execute("select name from organizations where tenant_id=?", (user.tenant_id,)).fetchone()
        teams = connection.execute("select team_id, name from teams where tenant_id=? and state='active' "
                                   "and (?='org_admin' or team_id=?)", (user.tenant_id, user.role, user.team_id)).fetchall()
    response.headers["Cache-Control"] = "no-store"
    if organization is None:
        # A membership whose organization is gone is not an account anybody can use.
        raise HTTPException(403, "This account is unavailable.")
    # Which product answers requests about the application itself, so the assistant can be asked
    # to move around it from anywhere. Absent when this deployment configured none.
    console = configured_console()
    if console is not None and OrganizationDirectory().product(user.tenant_id, console.product_id) is None:
        console = None
    return {"user_id": user.user_id, "email": account[0] if account else None,
            "tenant_id": user.tenant_id, "organization_name": organization[0],
            "role": user.role, "team_id": user.team_id,
            "console_product_id": console.product_id if console else None,
            "teams": [dict(team) for team in teams]}


@router.post("/logout", status_code=204)
def logout(user: AuthUser = Depends(require_auth), authorization: str | None = Header(default=None),
           pixel_session: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> Response:
    require_member(user)
    token = authorization.removeprefix("Bearer ").strip() if authorization else (pixel_session or "")
    with get_connection() as connection:
        connection.execute("delete from login_sessions where token_hash=? and user_id=?",
                           (hashlib.sha256(token.encode()).hexdigest(), user.user_id))
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    _clear_session_cookies(response)
    return response


def _cookie_secure() -> bool:
    return env_bool("PIXEL_SECURE_COOKIES", default=True)


def _set_session_cookies(response: Response, token: str, csrf: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_COOKIE_MAX_AGE, httponly=True,
        secure=_cookie_secure(), samesite="lax", path="/api",
    )
    # Readable by the page, and readable on every page: the browser only hands a cookie to a
    # script whose path it matches, and the console does not live under /api. This is the token
    # a write has to echo back, and it is deliberately not a secret from our own pages - it is a
    # secret from other sites, which `samesite` and the header check are what protect.
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=SESSION_COOKIE_MAX_AGE, httponly=False,
        secure=_cookie_secure(), samesite="lax", path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/api")
    response.delete_cookie(CSRF_COOKIE, path="/")
