"""The turn snapshot (3.2 plan, section 7.1).

One turn reads one snapshot. Routing, the lookup, validation, clarification candidates, the
prompt and the reply all see the same records, so two reads inside a turn can never disagree and
a clarification list cannot offer a record that validation then rejects.

Two properties matter more than the convenience:

- **It is materialized in one short read transaction, which is then closed.** Reading each entity
  on first access would not be a snapshot at all: one entity read at one moment and a related
  entity read at another can contradict each other. No database transaction is ever held open
  while a model call is in flight.
- **It decides nothing about permission.** The scope is fixed when the snapshot is taken, and a
  keyed write re-checks access and records live inside its own transaction (section 5.1). A
  snapshot makes a conversation coherent; it must never be what allows a change.

A snapshot lives for exactly one turn. Anything remembered across turns, including pending
clarification candidates, is re-resolved against the next turn's snapshot.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from app.db import get_connection
from app.engine.lookup import PeopleMatch, PersonView, RecordView


@runtime_checkable
class SnapshotSource(Protocol):
    """A product package's one chance to read records for a turn.

    `materialize` is called with an open read transaction and must return every record this
    caller may see, by entity. It is called once per turn and may not keep the connection.
    """

    def materialize(self, connection) -> Mapping[str, tuple[RecordView, ...]]: ...

    @property
    def scope_label(self) -> str:
        """What the snapshot is bound to, for evidence only. Never used to widen anything."""
        ...


@runtime_checkable
class LoadedRecordSource(Protocol):
    """A product package that can convert records its caller already loaded (5a plan, 5.1).

    The shadow engine builds its snapshot from the same records the live turn received, so both
    engines see one moment and there is no second read. The conversion produces immutable views
    and never keeps a reference to the loaded data.
    """

    def records_from(self, data: Mapping[str, Any]) -> Mapping[str, tuple[RecordView, ...]]: ...

    @property
    def scope_label(self) -> str: ...


@dataclass(frozen=True)
class TurnSnapshot:
    """Frozen records for one turn. Satisfies `RecordLookup`, so the engine needs no other path."""

    records: Mapping[str, tuple[RecordView, ...]]
    scope_label: str
    definition_checksum: str
    taken_at: float
    people_entity: str | None = None
    person_fields: Mapping[str, str] = MappingProxyType({})
    snapshot_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        # Product adapters and tests may construct this type directly, so immutability cannot
        # depend on callers going through take_snapshot().
        object.__setattr__(self, "records", MappingProxyType({
            entity: tuple(records) for entity, records in self.records.items()
        }))
        object.__setattr__(self, "person_fields", MappingProxyType(dict(self.person_fields)))

    # --- RecordLookup ---

    def get(self, entity: str, record_id: str) -> RecordView | None:
        wanted = record_id.lower()
        return next((r for r in self.records.get(entity, ()) if r.id.lower() == wanted), None)

    def search(self, entity: str, text: str, limit: int) -> list[RecordView]:
        needle = text.lower().strip()
        if not needle:
            return []
        return [r for r in self.records.get(entity, ()) if needle in r.title.lower()][:limit]

    def by_person(self, entity: str, person_id: str, limit: int) -> list[RecordView]:
        field = self.person_fields.get(entity)
        if field is None:
            return []
        return [r for r in self.records.get(entity, ()) if r.fields.get(field) == person_id][:limit]

    def people(self, text: str, limit: int) -> PeopleMatch:
        if self.people_entity is None:
            return PeopleMatch()
        wanted = text.lower().split()
        if not wanted:
            return PeopleMatch()
        matches = tuple(
            PersonView(r.id, r.title) for r in self.records.get(self.people_entity, ())
            if r.title.lower().split() == wanted
            or (len(wanted) == 1 and wanted[0] in r.title.lower().split())
        )
        return PeopleMatch(matches[:limit])

    def count(self, entity: str) -> int:
        return len(self.records.get(entity, ()))

    # --- Evidence ---

    def entities(self) -> tuple[str, ...]:
        return tuple(sorted(self.records))

    def ids(self, entity: str) -> frozenset[str]:
        return frozenset(r.id for r in self.records.get(entity, ()))


def take_snapshot(
    source: SnapshotSource, *, definition_checksum: str, people_entity: str | None = None,
    person_fields: Mapping[str, str] | None = None, connection=None,
) -> TurnSnapshot:
    """Read everything this turn may see, in one short read transaction, and freeze it.

    Every read happens inside **one** read transaction, so two entities cannot come from two
    different moments. Without a connection, one is opened, a read transaction is begun, the
    records are read and the connection is **closed** before this function returns, so no
    transaction can outlive the snapshot or span a model call. A supplied connection must already
    hold a transaction, for the same reason.
    """
    if connection is not None:
        if not connection.in_transaction:
            raise RuntimeError("a supplied connection must already hold a read transaction")
        materialized = source.materialize(connection)
    else:
        with get_connection() as own_connection:
            # One moment for every entity: without this, each select could see its own.
            own_connection.execute("begin")
            try:
                materialized = source.materialize(own_connection)
            finally:
                own_connection.rollback()
    frozen = MappingProxyType({
        entity: tuple(records) for entity, records in sorted(materialized.items())
    })
    return TurnSnapshot(
        records=frozen,
        scope_label=source.scope_label,
        definition_checksum=definition_checksum,
        taken_at=time.time(),
        people_entity=people_entity,
        person_fields=MappingProxyType(dict(person_fields or {})),
    )
