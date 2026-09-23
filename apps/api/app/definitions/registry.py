"""Definition version lifecycle.

A definition version may be bound by many products, including products of different
organizations when the definition is platform-shared, so its lifecycle is global: revoking a
version ends sessions for every product using it. Lifecycle state lives here, never in the
immutable definition file.

Ownership (platform-shared, or private to one organization) belongs to the definition's
identity. The first registered version fixes it, and registration and publication both check
it inside their write transaction, so versions arriving in any order or concurrently can never
disagree about who may use the definition.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.db import get_connection, use_connection
from app.definitions.compatibility import ChangeClass, classify
from app.definitions.loader import DefinitionSource, LoadedDefinition, load_definition

TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"validated"}),
    "validated": frozenset({"published"}),
    "published": frozenset({"retired", "revoked"}),
    "retired": frozenset({"revoked"}),
    "revoked": frozenset(),
}
EVER_PUBLISHED = ("published", "retired", "revoked")


class RegistryError(ValueError):
    pass


class DefinitionMigration(Protocol):
    """Reviewed platform code that moves records to a breaking definition version."""

    name: str

    def apply(self, connection) -> None: ...


@dataclass(frozen=True)
class DefinitionVersion:
    definition_id: str
    version: int
    checksum: str
    ownership: str
    owner_organization: str | None
    state: str

    def bindable_by(self, tenant_id: str) -> bool:
        return self.ownership == "platform_shared" or self.owner_organization == tenant_id


def default_source() -> DefinitionSource:
    """Where this deployment's definitions live: the files this repository ships, and the ones
    people added to Pixel. Files win, so a stored definition can never shadow a shipped one."""
    from app.definitions.authoring import StoredDefinitionSource

    return StoredDefinitionSource()


class DefinitionRegistry:
    def __init__(self, source: DefinitionSource | None = None, connection=None) -> None:
        self.source = source if source is not None else default_source()
        # With a connection, lifecycle reads join the caller's transaction.
        self._connection = connection

    def register(self, definition_id: str, version: int) -> DefinitionVersion:
        """Record a version as a draft. Re-registering identical content is a no-op.

        The first registered version establishes the definition's ownership; any version that
        declares different ownership is rejected.
        """
        loaded = load_definition(self.source, definition_id, version)
        identity = loaded.definition.definition
        with get_connection() as connection:
            connection.execute("begin immediate")
            existing = connection.execute(
                "select checksum from definition_versions where definition_id = ? and version = ?",
                (definition_id, version),
            ).fetchone()
            if existing is not None:
                if existing["checksum"] != loaded.checksum:
                    raise RegistryError(
                        f"{definition_id} v{version} is already registered with different content; "
                        "publish a new version instead"
                    )
            else:
                established = _identity(connection, definition_id)
                if established is None:
                    connection.execute(
                        "insert into definitions(definition_id, ownership, owner_tenant_id, created_at) "
                        "values (?, ?, ?, ?)",
                        (definition_id, identity.ownership, identity.owner_organization, _now()),
                    )
                elif established != (identity.ownership, identity.owner_organization):
                    raise _ownership_conflict(definition_id, version)
                connection.execute(
                    """
                    insert into definition_versions(
                      definition_id, version, checksum, ownership, owner_tenant_id, state, registered_at
                    )
                    values (?, ?, ?, ?, ?, 'draft', ?)
                    """,
                    (definition_id, version, loaded.checksum, identity.ownership,
                     identity.owner_organization, _now()),
                )
        return self.get(definition_id, version)

    def validate(self, definition_id: str, version: int) -> DefinitionVersion:
        self.load(definition_id, version, allowed_states=("draft",))
        return self._transition(definition_id, version, "validated")

    def publish(
        self,
        definition_id: str,
        version: int,
        migration: DefinitionMigration | None = None,
    ) -> DefinitionVersion:
        current = self.load(definition_id, version, allowed_states=("validated",))
        previous = self._latest_ever_published(definition_id, below=version)
        breaking = False
        if previous is not None:
            earlier = self.load(definition_id, previous.version)
            compatibility = classify(earlier.definition, current.definition)
            breaking = compatibility.change == ChangeClass.BREAKING_REQUIRES_MIGRATION
            if breaking and migration is None:
                raise RegistryError(
                    f"{definition_id} v{version} makes breaking entity changes and needs a reviewed "
                    f"migration: {'; '.join(compatibility.breaking_changes)}"
                )
        declared = current.definition.definition
        with get_connection() as connection:
            connection.execute("begin immediate")
            recorded = connection.execute(
                "select ownership, owner_tenant_id from definition_versions where definition_id = ? and version = ?",
                (definition_id, version),
            ).fetchone()
            owners = {
                _identity(connection, definition_id),
                (declared.ownership, declared.owner_organization),
                (recorded["ownership"], recorded["owner_tenant_id"]) if recorded else None,
            }
            if len(owners) != 1:
                raise _ownership_conflict(definition_id, version)
            if breaking and migration is not None:
                migration.apply(connection)
                # Older sessions would read records in a shape their definition no longer matches.
                connection.execute(
                    "update sessions set expires_at = ? where definition_id = ? and definition_version < ?",
                    (_now(), definition_id, version),
                )
            self._transition_in(connection, definition_id, version, "published")
        return self.get(definition_id, version)

    def retire(self, definition_id: str, version: int) -> DefinitionVersion:
        return self._transition(definition_id, version, "retired")

    def revoke(self, definition_id: str, version: int) -> DefinitionVersion:
        return self._transition(definition_id, version, "revoked")

    def get(self, definition_id: str, version: int) -> DefinitionVersion | None:
        with use_connection(self._connection) as connection:
            row = connection.execute(
                "select * from definition_versions where definition_id = ? and version = ?",
                (definition_id, version),
            ).fetchone()
        return _version(row) if row else None

    def load(
        self,
        definition_id: str,
        version: int,
        allowed_states: tuple[str, ...] = EVER_PUBLISHED,
    ) -> LoadedDefinition:
        """Load a registered version, verifying the file still matches the registered checksum."""
        registered = self.get(definition_id, version)
        if registered is None:
            raise RegistryError(f"{definition_id} v{version} is not registered")
        if registered.state not in allowed_states:
            raise RegistryError(f"{definition_id} v{version} is {registered.state}")
        return load_definition(self.source, definition_id, version, expected_checksum=registered.checksum)

    def ensure_published(self, definition_id: str, version: int) -> DefinitionVersion:
        """Development bootstrap: take a version through draft and validated to published."""
        registered = self.register(definition_id, version)
        if registered.state == "draft":
            registered = self.validate(definition_id, version)
        if registered.state == "validated":
            registered = self.publish(definition_id, version)
        if registered.state != "published":
            raise RegistryError(f"{definition_id} v{version} is {registered.state} and cannot be used")
        return registered

    def _latest_ever_published(self, definition_id: str, below: int) -> DefinitionVersion | None:
        with get_connection() as connection:
            row = connection.execute(
                f"""
                select * from definition_versions
                where definition_id = ? and version < ? and state in ({",".join("?" * len(EVER_PUBLISHED))})
                order by version desc limit 1
                """,
                (definition_id, below, *EVER_PUBLISHED),
            ).fetchone()
        return _version(row) if row else None

    def _transition(self, definition_id: str, version: int, target: str) -> DefinitionVersion:
        with get_connection() as connection:
            connection.execute("begin immediate")
            self._transition_in(connection, definition_id, version, target)
        return self.get(definition_id, version)

    def _transition_in(self, connection, definition_id: str, version: int, target: str) -> None:
        row = connection.execute(
            "select state from definition_versions where definition_id = ? and version = ?",
            (definition_id, version),
        ).fetchone()
        if row is None:
            raise RegistryError(f"{definition_id} v{version} is not registered")
        if target not in TRANSITIONS[row["state"]]:
            raise RegistryError(f"{definition_id} v{version} cannot move from {row['state']} to {target}")
        connection.execute(
            f"update definition_versions set state = ?, {target}_at = ? "
            "where definition_id = ? and version = ? and state = ?",
            (target, _now(), definition_id, version, row["state"]),
        )


def _identity(connection, definition_id: str) -> tuple[str, str | None] | None:
    row = connection.execute(
        "select ownership, owner_tenant_id from definitions where definition_id = ?", (definition_id,)
    ).fetchone()
    return (row["ownership"], row["owner_tenant_id"]) if row else None


def _ownership_conflict(definition_id: str, version: int) -> RegistryError:
    return RegistryError(
        f"{definition_id} v{version} declares different ownership from the definition; ownership is permanent"
    )


def _version(row) -> DefinitionVersion:
    return DefinitionVersion(
        definition_id=row["definition_id"],
        version=row["version"],
        checksum=row["checksum"],
        ownership=row["ownership"],
        owner_organization=row["owner_tenant_id"],
        state=row["state"],
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
