"""Definitions that were added to Pixel rather than shipped as files (3.5).

A product somebody adds cannot be a file in this repository, so its definition is kept in the
database and read from there. Everything else about it is unchanged: the same strict contract
validates it, the same registry publishes it, the same checksum makes a published version
immutable, and the same binding points a product at one version of it.

A definition added this way is untrusted input, however it was produced and whoever produced it.
It earns nothing by being stored: it is parsed and validated before it is written, and a caller
that cannot produce a valid definition gets an error rather than a partly-working product.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.db import get_connection, use_connection
from app.definitions.loader import (
    DefinitionError,
    DefinitionSource,
    _checksum,
    parse_definition,
)
from app.definitions.safety import check_key


@dataclass(frozen=True)
class StoredDefinitionSource(DefinitionSource):
    """Definition files, plus the definitions people added to Pixel.

    Files are read first, so a stored definition can never shadow one this repository ships.
    """

    def read(self, definition_id: str, version: int) -> bytes:
        if super().has(definition_id, version):
            return super().read(definition_id, version)
        stored = read_stored(definition_id, version)
        if stored is None:
            raise DefinitionError(f"no definition for {definition_id} v{version}")
        return stored

    def has(self, definition_id: str, version: int) -> bool:
        return super().has(definition_id, version) or read_stored(definition_id, version) is not None


def read_stored(definition_id: str, version: int, connection=None) -> bytes | None:
    with use_connection(connection) as connection:
        row = connection.execute(
            "select source from definition_sources where definition_id = ? and version = ?",
            (definition_id, version),
        ).fetchone()
    return row["source"].encode("utf-8") if row else None


def stored_versions(definition_id: str, connection=None) -> list[int]:
    with use_connection(connection) as connection:
        rows = connection.execute(
            "select version from definition_sources where definition_id = ? order by version",
            (definition_id,),
        ).fetchall()
    return [row["version"] for row in rows]


def store_definition(text: str, *, definition_id: str, version: int) -> str:
    """Write one version's text after checking it is a definition this platform will run.

    Returns its checksum. A version that already exists is never overwritten: published
    definitions are immutable, so a change is a new version.
    """
    check_key(definition_id)
    if definition_id in DefinitionSource().packages():
        raise DefinitionError("This definition identifier is reserved by an installed package.")
    if not isinstance(version, int) or version < 1:
        raise DefinitionError("a definition version is a positive whole number")
    raw = text.encode("utf-8")
    definition = parse_definition(raw)
    declared = definition.definition
    if (declared.definition_id, declared.version) != (definition_id, version):
        raise DefinitionError(
            f"this definition declares {declared.definition_id} v{declared.version}, "
            f"not {definition_id} v{version}"
        )
    checksum = _checksum(raw)
    with get_connection() as connection:
        connection.execute("begin immediate")
        owner = connection.execute(
            "select ownership, owner_tenant_id from definitions where definition_id=?", (definition_id,),
        ).fetchone()
        expected_owner = (declared.ownership, declared.owner_organization)
        if owner is not None and tuple(owner) != expected_owner:
            raise DefinitionError("This definition identifier belongs to another owner.")
        # Claim the identity with the source so a concurrent upload cannot reserve another
        # owner's next version before lifecycle registration catches the mismatch.
        if owner is None:
            connection.execute("insert into definitions values (?, ?, ?, ?)",
                               (definition_id, *expected_owner, datetime.now(UTC).isoformat()))
        existing = connection.execute(
            "select checksum from definition_sources where definition_id = ? and version = ?",
            (definition_id, version),
        ).fetchone()
        if existing is not None:
            if existing["checksum"] != checksum:
                raise DefinitionError(
                    f"{definition_id} v{version} already exists; publish a new version instead"
                )
            return checksum
        connection.execute(
            "insert into definition_sources values (?, ?, ?, ?, ?)",
            (definition_id, version, text, checksum, datetime.now(UTC).isoformat()),
        )
    return checksum
