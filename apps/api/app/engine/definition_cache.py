"""Parsed definitions for the shadow engine, behind fresh gates (5a plan, section 4.2).

The cache holds parsed definition content only, keyed by (definition ID, version, checksum). It
never stores approval. Every use goes through `approve`, which runs the same checks a live turn
runs, against the database, right now: the caller's product access, the binding's state and
team, the session's pin, the version's lifecycle state, its recorded checksum and the file's
current checksum. Only the `Approval` that `approve` returns can read the cache, and only for the
exact key it approved. A failed gate raises `Gated`; nothing is ever served from an older or
different version instead.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime

from app.definitions.access import AccessDenied, Principal, ProductAccess, authorize_product
from app.definitions.contract import ProductDefinition
from app.definitions.organizations import OrganizationDirectory
from app.definitions.sessions import SessionEnded, SessionPin, check_pinned_session

CACHE_LIMIT = 32

# Only `approve` holds this, so an `Approval` cannot be assembled anywhere else.
_GATE = object()


class Gated(Exception):
    """A gate refused this turn; `reason` is the same UI-safe reason a live turn would give."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class DefinitionKey:
    definition_id: str
    version: int
    checksum: str


@dataclass(frozen=True)
class Approval:
    """Proof that every gate passed for one key, just now."""

    key: DefinitionKey
    pin: SessionPin
    access: ProductAccess
    issued_by: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self.issued_by is not _GATE:
            raise TypeError("an Approval is issued by approve() only")


def approve(
    principal: Principal,
    product_id: str,
    pin: SessionPin | None,
    directory: OrganizationDirectory,
    now: datetime | None = None,
) -> Approval:
    try:
        access = authorize_product(principal, product_id, directory)
    except AccessDenied as denied:
        raise Gated(denied.reason) from None
    try:
        pin = check_pinned_session(pin, directory, now)
    except SessionEnded as ended:
        raise Gated(ended.reason) from None
    if (pin.tenant_id, pin.product_id) != (principal.tenant_id, product_id):
        raise Gated("session_product_mismatch")
    key = DefinitionKey(pin.definition_id, pin.definition_version, pin.definition_checksum)
    return Approval(key, pin, access, issued_by=_GATE)


class DefinitionCache:
    """At most `limit` parsed definitions, least recently used evicted. Safe across threads."""

    def __init__(self, directory: OrganizationDirectory, limit: int = CACHE_LIMIT) -> None:
        self._directory = directory
        self._limit = limit
        self._entries: OrderedDict[DefinitionKey, ProductDefinition] = OrderedDict()
        self._lock = threading.Lock()

    def definition(self, approval: Approval) -> ProductDefinition:
        if not isinstance(approval, Approval):
            raise TypeError("the cache is read with an Approval only")
        key = approval.key
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached
        loaded = self._directory.definitions.load(key.definition_id, key.version)
        if loaded.checksum != key.checksum:
            # The registry verified the file against its record; the pin must agree with both.
            raise Gated("definition_checksum_mismatch")
        with self._lock:
            self._entries[key] = loaded.definition
            self._entries.move_to_end(key)
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)
        return loaded.definition

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def keys(self) -> tuple[DefinitionKey, ...]:
        with self._lock:
            return tuple(self._entries)
