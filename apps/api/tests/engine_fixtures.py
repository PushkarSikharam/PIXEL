"""A neutral, non-project-management product for engine tests.

The sample desk (accounts, contacts, notes) from `definition_fixtures` is extended with a
confirm-free mutation, a record update that needs confirmation, a destructive guardrail and the
replies the engine uses. `InMemoryLookup` is a scope-bound record lookup: records and people
outside the caller's accounts behave exactly as if they did not exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.definitions.contract import ProductDefinition
from app.engine.lookup import PeopleMatch, PersonView, RecordView
from definition_fixtures import sample_definition


def engine_definition(version: int = 1) -> dict:
    document = sample_definition(version)
    document["vocabulary"]["correction_markers"] = ["actually", "instead"]
    document["vocabulary"]["negatable_terms"] = ["contact", "contacts"]
    document["actions"]["reassign_contact"] = {
        "capability": "UPDATE_RECORD", "entity": "contact", "fields": ["owner"],
        "description": "Give a contact to another agent.",
    }
    document["intents"] = [
        {"action": "open_contacts", "match": [["contact", "contacts"]], "response": "view_opened"},
        {"action": "open_contacts", "exact": ["everything please"], "response": "view_opened"},
        {"action": "open_contact", "requires": ["person"], "match": [["contact"]], "response": "record_opened"},
        {"action": "contacts_by_owner", "requires": ["person"], "match": [["owned by", "for"], ["contacts"]],
         "response": "records_filtered"},
        {"action": "update_contact", "requires": ["record"], "match": [["close", "reopen", "status"]],
         "response": "record_update_proposed"},
        {"action": "reassign_contact", "requires": ["record"], "match": [["reassign", "give"]],
         "response": "record_update_proposed"},
        # Two equally specific intents for different actions: a deliberate tie.
        {"action": "open_contacts", "match": [["overview"]]},
        {"action": "highlight_mail", "match": [["overview"]]},
    ]
    document["clarifications"] = [
        {"response": "clarify_create", "match": [["create", "new"]], "exclude": ["contact"]},
    ]
    document["guardrails"] = [
        {"topic": "external_billing", "response": "out_of_scope", "match": [["invoice"]]},
        {"topic": "destructive_change", "response": "destructive_refused", "match": [["delete", "erase"]]},
    ]
    document["responses"].update({
        "record_opened": "I'll open {record_id}.",
        "records_filtered": "Here are the contacts for {person}.",
        "record_update_proposed": "I'll update {record_id}: {changes}.",
        "confirm_action": "Should I go ahead and update {record_id}?",
        "action_cancelled": "Okay, I won't change anything.",
        "clarify_assign": "Who should get {record_id}?",
        "clarify_person": "Which person do you mean: {records}?",
        "clarify_update_target": "Which contact should I update?",
        "unknown_person": "I could not find {person} here.",
        "destructive_refused": "I can't delete anything here.",
        "fallback": "I can help with {product}.",
        # Slice 4b: a full lifecycle needs wording for every stage, and they must read differently.
        "greeting_named": "Hey {visitor}, welcome to {product}.",
        "record_create_proposed": "I'll create a contact with {changes}.",
        "record_created": "That contact is created with {changes}.",
        "record_updated": "{record_id} is now updated: {changes}.",
        "control_highlighted": "I'll point at that control in {view}.",
        "knowledge_unavailable": "I have nothing to answer that from in this {product} demo, so I would rather not guess.",
    })
    return document


def load_engine_definition(version: int = 1, document: dict | None = None) -> ProductDefinition:
    return ProductDefinition.model_validate(document or engine_definition(version))


@dataclass
class SampleDesk:
    """Two accounts; the caller may see only ACC-1 and what belongs to it."""

    accounts: dict[str, RecordView] = field(default_factory=lambda: {
        "ACC-1": RecordView("account", "ACC-1", "Northwind", {"tier": "Pro"}),
        "ACC-2": RecordView("account", "ACC-2", "Contoso", {"tier": "Free"}),
    })
    agents: dict[str, tuple[str, str]] = field(default_factory=lambda: {
        # agent id -> (name, account id)
        "ana-lopez": ("Ana Lopez", "ACC-1"),
        "ana-reyes": ("Ana Reyes", "ACC-1"),
        "ana-singh": ("Ana Singh", "ACC-1"),
        "ben-okafor": ("Ben Okafor", "ACC-1"),
        "cara-singh": ("Cara Singh", "ACC-2"),
        "ana-kim": ("Ana Kim", "ACC-2"),
    })
    contacts: dict[str, tuple[RecordView, str]] = field(default_factory=lambda: {
        "CON-1": (RecordView("contact", "CON-1", "Dana Reyes", {"owner": "ana-lopez", "status": "Open"}), "ACC-1"),
        "CON-2": (RecordView("contact", "CON-2", "Eli Moss", {"owner": "ben-okafor", "status": "Open"}), "ACC-1"),
        "CON-3": (RecordView("contact", "CON-3", "Fay Chu", {"owner": "cara-singh", "status": "Closed"}), "ACC-2"),
    })


class InMemoryLookup:
    """Scope is applied first; matching, counting and choices only ever see visible records."""

    def __init__(self, desk: SampleDesk, visible_accounts: frozenset[str]) -> None:
        self._desk = desk
        self._visible = visible_accounts
        self.calls: list[tuple[str, ...]] = []

    def _records(self, entity: str) -> list[RecordView]:
        if entity == "account":
            return [record for key, record in self._desk.accounts.items() if key in self._visible]
        if entity == "contact":
            return [record for record, account in self._desk.contacts.values() if account in self._visible]
        if entity == "agent":
            return [
                RecordView("agent", agent_id, name, {"account": account})
                for agent_id, (name, account) in self._desk.agents.items() if account in self._visible
            ]
        return []

    def get(self, entity: str, record_id: str) -> RecordView | None:
        self.calls.append(("get", entity, record_id))
        return next((record for record in self._records(entity) if record.id == record_id), None)

    def search(self, entity: str, text: str, limit: int) -> list[RecordView]:
        needle = text.lower()
        return [record for record in self._records(entity) if needle in record.title.lower()][:limit]

    def by_person(self, entity: str, person_id: str, limit: int) -> list[RecordView]:
        return [record for record in self._records(entity) if record.fields.get("owner") == person_id][:limit]

    def people(self, text: str, limit: int) -> PeopleMatch:
        self.calls.append(("people", text))
        wanted = text.lower().split()
        matches = tuple(
            PersonView(record.id, record.title)
            for record in self._records("agent")
            if wanted and (record.title.lower().split() == wanted
                           or (len(wanted) == 1 and wanted[0] in record.title.lower().split()))
        )
        return PeopleMatch(matches[:limit])

    def count(self, entity: str) -> int:
        return len(self._records(entity))

    @property
    def scope_label(self) -> str:
        return ", ".join(sorted(self._visible))
