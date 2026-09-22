from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from app.db import get_connection, use_connection
from app.definitions.sessions import SessionPin
from app.engine.execution import ExecutionLedger, ExecutionOwner
from app.schemas import SessionSummary, Signal
from app.services.demo_instances import DemoContext


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    def ensure_session(
        self,
        session_id: str,
        product_id: str,
        user_id: str | None = None,
        tenant_id: str | None = None,
        scope_id: str | None = None,
        pin: SessionPin | None = None,
        demo_context: DemoContext | None = None,
    ) -> bool:
        """Create or reuse a session. Returns False if it belongs to someone else.

        The conversation_owners.customer_id column holds the owning tenant ID.

        Callers without a user (internal tools, unit tests) skip the ownership check;
        API routes always pass the authenticated user. A new session stores its definition
        pin; an existing session keeps the pin it started with.
        """
        with get_connection() as connection:
            connection.execute("begin immediate")
            existing = connection.execute(
                "select product_id from sessions where id = ?",
                (session_id,),
            ).fetchone()
            if existing:
                if existing["product_id"] != product_id:
                    return False
                if user_id is None:
                    return True
                owner = connection.execute(
                    "select user_id, customer_id, instance_id, instance_generation "
                    "from conversation_owners where session_id = ?",
                    (session_id,),
                ).fetchone()
                expected_instance = (
                    (demo_context.instance_id, demo_context.generation)
                    if demo_context else (None, None)
                )
                if owner is None or (
                    owner["user_id"], owner["customer_id"],
                    owner["instance_id"], owner["instance_generation"],
                ) != (user_id, tenant_id, *expected_instance):
                    return False
                # An existing session's workspace moves only with an accepted, monotonic turn
                # activation (5b plan, section 6.1 step 4): a stale or delayed request never moves it.
                return True

            connection.execute(
                """
                insert into sessions(
                  id, product_id, active_turn_id, latest_turn_id, started_at,
                  tenant_id, team_id, definition_id, definition_version, definition_checksum,
                  knowledge_version, expires_at
                )
                values (?, ?, null, null, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, product_id, utc_now(),
                    *(
                        (pin.tenant_id, pin.team_id, pin.definition_id, pin.definition_version,
                         pin.definition_checksum, pin.knowledge_version, pin.expires_at.isoformat())
                        if pin else (None,) * 7
                    ),
                ),
            )
            connection.execute(
                "insert into visitor_context(session_id) values (?)",
                (session_id,),
            )
            if user_id is not None:
                connection.execute(
                    """
                    insert into conversation_owners(
                      session_id, user_id, customer_id, product_id, scope_id,
                      instance_id, instance_generation
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, user_id, tenant_id or "", product_id, scope_id or "",
                     demo_context.instance_id if demo_context else None,
                     demo_context.generation if demo_context else None),
                )
            return True

    def exists(self, session_id: str) -> bool:
        with get_connection() as connection:
            return connection.execute("select 1 from sessions where id = ?", (session_id,)).fetchone() is not None

    def pin_for(self, session_id: str, connection=None) -> SessionPin | None:
        """The product and definition pin a session started with, or None if unknown or unpinned."""
        with use_connection(connection) as connection:
            row = connection.execute(
                """
                select product_id, tenant_id, team_id, definition_id, definition_version,
                       definition_checksum, knowledge_version, expires_at
                from sessions where id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None or row["definition_version"] is None:
            return None
        return SessionPin(
            tenant_id=row["tenant_id"],
            team_id=row["team_id"],
            product_id=row["product_id"],
            definition_id=row["definition_id"],
            definition_version=row["definition_version"],
            definition_checksum=row["definition_checksum"],
            knowledge_version=row["knowledge_version"],
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    def owns_session(self, session_id: str, user_id: str, tenant_id: str, connection=None,
                     demo_context: DemoContext | None = None) -> bool:
        with use_connection(connection) as connection:
            owner = connection.execute(
                "select user_id, customer_id, instance_id, instance_generation "
                "from conversation_owners where session_id = ?",
                (session_id,),
            ).fetchone()
        expected_instance = (
            (demo_context.instance_id, demo_context.generation) if demo_context else (None, None)
        )
        return bool(owner and (
            owner["user_id"], owner["customer_id"], owner["instance_id"], owner["instance_generation"]
        ) == (user_id, tenant_id, *expected_instance))

    def activate_turn(
        self, session_id: str, turn_id: int, *, owner: ExecutionOwner | None = None,
        scope_id: str | None = None,
    ) -> bool:
        """Activate a newer turn, select its workspace and supersede older keys, atomically.

        One transaction (5b plan, section 6.1 step 4): the session's recorded owner is read and,
        when the caller names an owner, must match it; the turn becomes active only if it is newer
        than every earlier turn; only then is the workspace recorded and are this owner's unused
        keys from older turns cancelled (`superseded`), whatever workspace they were issued in.
        Returns False, writing nothing, for a stale or foreign turn.
        """
        with get_connection() as connection:
            connection.execute("begin immediate")
            recorded = connection.execute(
                "select user_id, customer_id, product_id, instance_id, instance_generation "
                "from conversation_owners where session_id = ?",
                (session_id,),
            ).fetchone()
            session_owner = None if recorded is None else ExecutionOwner(
                recorded["customer_id"], recorded["product_id"], recorded["user_id"],
                recorded["instance_id"], recorded["instance_generation"],
            )
            if owner is not None and session_owner != owner:
                return False
            cursor = connection.execute(
                """
                update sessions
                set active_turn_id = ?, latest_turn_id = ?
                where id = ?
                  and (latest_turn_id is null or latest_turn_id < ?)
                """,
                (turn_id, turn_id, session_id, turn_id),
            )
            if cursor.rowcount != 1:
                return False
            if session_owner is not None:
                if scope_id is not None:
                    connection.execute(
                        "update conversation_owners set scope_id = ? where session_id = ?",
                        (scope_id, session_id),
                    )
                ExecutionLedger().supersede(connection, session_owner, session_id, turn_id)
            return True

    def is_active_turn(self, session_id: str, turn_id: int) -> bool:
        with get_connection() as connection:
            row = connection.execute(
                "select active_turn_id from sessions where id = ?",
                (session_id,),
            ).fetchone()
        return bool(row and row["active_turn_id"] == turn_id)

    def cancel_turn(self, session_id: str, turn_id: int, *, owner: ExecutionOwner | None = None) -> bool:
        """Cancel one turn and its unused keys, in one owner-bound transaction (section 6.3).

        The active turn is cleared only if it is this turn. Keys are cancelled only for exactly
        this turn and only for the session's recorded owner (`user_cancelled`), in whichever
        workspaces they were issued; no workspace is taken from the client and no record is read.
        Returns True when the turn was the active one.
        """
        with get_connection() as connection:
            connection.execute("begin immediate")
            recorded = connection.execute(
                "select user_id, customer_id, product_id, instance_id, instance_generation "
                "from conversation_owners where session_id = ?",
                (session_id,),
            ).fetchone()
            session_owner = None if recorded is None else ExecutionOwner(
                recorded["customer_id"], recorded["product_id"], recorded["user_id"],
                recorded["instance_id"], recorded["instance_generation"],
            )
            if owner is not None and session_owner != owner:
                return False
            cursor = connection.execute(
                "update sessions set active_turn_id = null where id = ? and active_turn_id = ?",
                (session_id, turn_id),
            )
            if session_owner is not None:
                ExecutionLedger().cancel_turn(connection, session_owner, session_id, turn_id)
            return cursor.rowcount == 1

    def complete_turn(self, session_id: str, turn_id: int) -> None:
        with get_connection() as connection:
            connection.execute(
                """
                update sessions
                set active_turn_id = null
                where id = ? and active_turn_id = ?
                """,
                (session_id, turn_id),
            )

    def store_message(self, session_id: str, turn_id: int, role: str, content: str,
                       scope_id: str | None = None) -> None:
        with get_connection() as connection:
            connection.execute(
                """
                insert into messages(id, session_id, turn_id, role, content, created_at, scope_id)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), session_id, turn_id, role, content, utc_now(), scope_id),
            )

    def recent_user_messages(self, session_id: str, limit: int = 20) -> list[str]:
        """This session's own recent visitor messages, newest first (already stored; nothing copied)."""
        with get_connection() as connection:
            rows = connection.execute(
                "select content from messages where session_id = ? and role = 'user' "
                "order by turn_id desc, created_at desc limit ?",
                (session_id, limit),
            ).fetchall()
        return [row["content"] for row in rows]

    def store_signals(self, session_id: str, turn_id: int, signals: list[Signal],
                       scope_id: str | None = None) -> None:
        if not signals:
            return

        with get_connection() as connection:
            for signal in signals:
                connection.execute(
                    """
                    insert into signals(id, session_id, turn_id, type, value, confidence, created_at, scope_id)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        session_id,
                        turn_id,
                        signal.type,
                        signal.value,
                        signal.confidence,
                        utc_now(),
                        scope_id,
                    ),
                )

    def remember_session_context(
        self,
        session_id: str,
        signals: list[Signal],
        clarification_pending: str | None = None,
    ) -> None:
        with get_connection() as connection:
            row = connection.execute(
                """
                select goals, pain_points, features_interested_in
                from visitor_context
                where session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return

            goals = _json_list(row["goals"])
            pain_points = _json_list(row["pain_points"])
            features = _json_list(row["features_interested_in"])

            for signal in signals:
                if signal.type == "feature_interest":
                    _append_unique(features, signal.value)
                if signal.type == "pain_point":
                    _append_unique(pain_points, signal.value)
                if signal.type == "goal":
                    _append_unique(goals, signal.value)

            connection.execute(
                """
                update visitor_context
                set goals = ?,
                    pain_points = ?,
                    features_interested_in = ?
                where session_id = ?
                """,
                (
                    json.dumps(goals),
                    json.dumps(pain_points),
                    json.dumps(features),
                    session_id,
                ),
            )

    def latest_signal_value(self, session_id: str, signal_type: str) -> str | None:
        with get_connection() as connection:
            row = connection.execute(
                """
                select value
                from signals
                where session_id = ? and type = ?
                order by created_at desc
                limit 1
                """,
                (session_id, signal_type),
            ).fetchone()
        return row["value"] if row else None

    def session_summary(
        self,
        session_id: str,
        clarification_pending: str | None = None,
    ) -> SessionSummary:
        with get_connection() as connection:
            row = connection.execute(
                """
                select goals, pain_points, features_interested_in
                from visitor_context
                where session_id = ?
                """,
                (session_id,),
            ).fetchone()

        return SessionSummary(
            interests=_json_list(row["features_interested_in"]) if row else [],
            pain_points=_json_list(row["pain_points"]) if row else [],
            last_person=self.latest_signal_value(session_id, "person_interest"),
            last_feature=self.latest_signal_value(session_id, "feature_interest"),
            clarification_pending=clarification_pending,
        )


def _json_list(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    value = json.loads(raw_value)
    return value if isinstance(value, list) else []


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
