"""Re-checking a keyed write inside its own transaction (3.2 plan, section 5.1).

This is platform enforcement, not engine code: it reads the deployment's organizations, products,
definitions and sessions. The engine defines what an execution is; this decides whether one may
still happen.

The checks a request passed when the action was proposed are not enough: an organization can
be suspended, a product disabled or transferred, a definition revoked, a session ended or a
record grant withdrawn between the proposal and the write. So every keyed write repeats them,
reading through the connection that is about to make the change, and refuses if anything moved.

The refusal reason is a stable code. It never distinguishes "not allowed" from "does not exist".
"""
from __future__ import annotations

from dataclasses import dataclass

from app.definitions.access import AccessDenied, Principal, ProductAccess, authorize_product
from app.definitions.organizations import OrganizationDirectory
from app.definitions.registry import DefinitionRegistry
from app.definitions.sessions import DefinitionUnavailable, SessionEnded, check_pinned_session
from app.engine.execution import ExecutionRefused
from app.record_access import RecordGrant, record_grant
from app.services.session_manager import SessionManager
from app.services.demo_instances import DemoInstanceStore, InstanceUnavailable
from app.services.product_data_store import ProductDataStore


@dataclass(frozen=True)
class WriteAuthority:
    """What the caller is allowed to do at the moment of the write."""

    access: ProductAccess
    grant: RecordGrant


class ExecutionGuard:
    """Repeats access, product, definition and session checks on the caller's connection."""

    def __init__(self, sessions: SessionManager | None = None) -> None:
        self._sessions = sessions or SessionManager()

    def recheck(
        self, connection, principal: Principal, *, product_id: str, session_id: str | None,
    ) -> WriteAuthority:
        """Raise `ExecutionRefused` unless this caller may still write to this product now."""
        directory = OrganizationDirectory(
            DefinitionRegistry(connection=connection), connection=connection
        )
        try:
            access = authorize_product(principal, product_id, directory)
        except AccessDenied as denied:
            raise ExecutionRefused(f"access_{denied.reason}", conflict=False) from denied

        if principal.kind == "member":
            grant = record_grant(
                principal.tenant_id, product_id, principal.user_id, connection=connection
            )
        else:
            context = getattr(principal, "demo_context", None)
            try:
                DemoInstanceStore().assert_available(context, connection)
            except (InstanceUnavailable, AttributeError):
                grant = None
            else:
                scopes = ProductDataStore(context).load(connection=connection)["workspaceScopes"]
                grant = RecordGrant(
                    frozenset(scope["id"] for scope in scopes), False, demo_context=context
                )
        if grant is None:
            raise ExecutionRefused("record_access_withdrawn", conflict=False)

        if session_id is not None:
            pin = self._sessions.pin_for(session_id, connection=connection)
            owned = self._sessions.owns_session(
                session_id, principal.user_id, principal.tenant_id, connection=connection,
                demo_context=getattr(principal, "demo_context", None),
            )
            if pin is None or not owned or pin.product_id != product_id:
                # Another caller's session, an unknown one, one from before a private reset, or
                # one for a different product: the same plain not-found as an unknown key (5b plan,
                # section 8.1), so nothing about the session or the key is revealed.
                raise ExecutionRefused("session_not_usable", conflict=False, not_found=True)
            try:
                check_pinned_session(pin, directory)
            except (SessionEnded, DefinitionUnavailable) as ended:
                raise ExecutionRefused(f"session_{ended.reason}", conflict=False) from ended
        return WriteAuthority(access, grant)
