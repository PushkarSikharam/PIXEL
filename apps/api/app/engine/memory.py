"""Explicit per-session conversation state (3.2 plan, section 6).

Memory never authorizes anything: every remembered reference is re-resolved through the
session's `RecordLookup` on the turn that uses it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from app.engine.actions import ConfirmationReason, GenericAction, RecordRef, frozen_value

# A pending question or confirmation is answered on the next turn or not at all.
PENDING_TURNS = 1
# A question is asked again at most once after an answer that does not fit.
MAX_REPEATS = 1
# What a pending question waits for.
EXPECTED_SLOTS = frozenset({"person", "record", "choice"})


@dataclass(frozen=True)
class PendingClarification:
    key: str  # the response key that asked the question
    action_key: str | None  # the action waiting for the answer, if any
    expected: str  # one of EXPECTED_SLOTS
    candidates: tuple[RecordRef, ...] = ()
    rejected: tuple[RecordRef, ...] = ()
    # The one candidate the assistant's last reply named, if it named exactly one.
    singled_out: RecordRef | None = None
    # A target already resolved before the question was asked (for example, the record to update).
    target: RecordRef | None = None
    # Field values already resolved before the question was asked (an immutable snapshot).
    fields: Mapping[str, Any] | None = None
    # The normalized request that led to the question, so a short answer can be read in context.
    context: str | None = None
    turn: int = 0
    # How often the question was repeated after an unrelated answer.
    repeats: int = 0

    def __post_init__(self) -> None:
        if self.fields is not None:
            object.__setattr__(self, "fields", frozen_value(self.fields))
        if self.expected not in EXPECTED_SLOTS:
            raise ValueError(f"unknown expected slot {self.expected}")
        if len(self.candidates) > 3:
            raise ValueError("a clarification offers at most three candidates")
        if self.singled_out is not None and self.singled_out not in self.candidates:
            raise ValueError("the singled-out candidate must be one of the candidates")

    def reject(self, refs: tuple[RecordRef, ...]) -> "PendingClarification":
        """Remove rejected candidates. Nothing is selected in their place."""
        remaining = tuple(ref for ref in self.candidates if ref not in refs)
        return replace(self, candidates=remaining, rejected=(*self.rejected, *refs), singled_out=None)

    def reject_singled_out(self) -> "PendingClarification":
        """Remove the candidate a correction refers to. Never selects another one."""
        if self.singled_out is None:
            raise ValueError("no candidate was singled out, so a correction cannot remove one")
        return self.reject((self.singled_out,))

    def repeated(self, turn: int) -> "PendingClarification | None":
        """The same question asked again, or None once it has been repeated enough."""
        if self.repeats >= MAX_REPEATS:
            return None
        return replace(self, repeats=self.repeats + 1, turn=turn)


@dataclass(frozen=True)
class PendingConfirmation:
    action: GenericAction
    reason: ConfirmationReason
    turn: int


@dataclass(frozen=True)
class PersonFollowUp:
    """The request the last accepted turn served (5a plan, section 4.5).

    A bare "what about <person>" on the very next turn re-applies it to that person: the same request when it
    was about a person, otherwise its subject filtered by that person.
    """

    action_key: str
    turn: int


@dataclass(frozen=True)
class ConversationMemory:
    pending_clarification: PendingClarification | None = None
    pending_confirmation: PendingConfirmation | None = None
    focus: RecordRef | None = None
    last_person: RecordRef | None = None
    last_view: str | None = None
    last_change: str | None = None  # an executed ledger entry's key
    turn: int = 0
    # Added in 5a, optional: nothing that predates it is affected.
    person_follow_up: PersonFollowUp | None = None

    def next_turn(self, turn: int) -> "ConversationMemory":
        """Advance to a new turn, expiring pending state that was not answered in time."""
        clarification = self.pending_clarification
        if clarification is not None and turn - clarification.turn > PENDING_TURNS:
            clarification = None
        confirmation = self.pending_confirmation
        if confirmation is not None and turn - confirmation.turn > PENDING_TURNS:
            confirmation = None
        return replace(self, pending_clarification=clarification, pending_confirmation=confirmation, turn=turn)

    def discard_pending(self) -> "ConversationMemory":
        """A refusal, scope change or cancellation clears anything waiting for an answer.

        It also ends a person follow-up: after a refusal, "what about <person>" is a new request.
        """
        return replace(self, pending_clarification=None, pending_confirmation=None,
                       person_follow_up=None)

    def change_scope(self) -> "ConversationMemory":
        """References made in another scope are never carried across."""
        return ConversationMemory(last_change=self.last_change, turn=self.turn)
