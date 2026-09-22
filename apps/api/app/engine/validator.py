"""The action-contract validator (3.2 plan, section 4).

A proposal is only a request. Nothing may act on it until this validator has checked it against
the pinned definition and the caller's scope-bound lookup:

- the action exists, and its capability comes from the definition;
- parameters are exactly those the capability allows (section 4.2);
- every value fits its field's declared type, bounds and allowed values;
- every record and person referenced is visible to this caller;
- values that reach replies and prompts are plain text.

A refusal names a stable code and never says whether a record exists somewhere else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import replace

from app.definitions.contract import EntitySpec, FieldSpec, ProductDefinition
from app.definitions.safety import check_text
from app.definitions.vocabulary import MUTATING_CAPABILITIES, PLATFORM_VIEWS, Capability
from app.engine.actions import ConfirmationReason, GenericAction, shape_errors
from app.engine.lookup import RecordLookup

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_TEXT = 10_000


@dataclass(frozen=True)
class Refusal:
    code: str
    detail: str


@dataclass(frozen=True)
class ValidatedAction:
    """A proposal the validator has approved, with the values it will be executed with."""

    action: GenericAction
    definition_id: str
    definition_version: int
    confirmation: ConfirmationReason | None = None

    @property
    def is_mutation(self) -> bool:
        return self.action.capability in MUTATING_CAPABILITIES


class ActionContractValidator:
    def __init__(
        self, definition: ProductDefinition, lookup: RecordLookup, *, definition_version: int = 1,
    ) -> None:
        self._definition = definition
        self._lookup = lookup
        self._version = definition_version

    def validate(
        self, proposal: GenericAction, *, confirmation: ConfirmationReason | None = None,
    ) -> ValidatedAction | Refusal:
        spec = self._definition.actions.get(proposal.action_key)
        if spec is None:
            return Refusal("unknown_action", f"{proposal.action_key} is not an action of this product")
        if errors := shape_errors(proposal, self._definition):
            code = "capability_mismatch" if "capability" in errors[0] else "bad_parameters"
            return Refusal(code, errors[0])
        entity = self._definition.entities.get(spec.entity) if spec.entity else None

        # A view or control that does not exist, and a field the entity marks uneditable, cannot
        # reach this point: the shape check ties every parameter to what the action declares, and
        # the contract refuses a definition whose action names an unknown view, a control outside
        # that view or an uneditable field. Navigability is not checked there, so it is checked here.
        if proposal.view is not None and proposal.view not in PLATFORM_VIEWS:
            view = self._definition.views[proposal.view]
            if proposal.capability == Capability.NAVIGATE_VIEW and not view.navigable:
                return Refusal("view_not_navigable", f"{proposal.view} cannot be opened directly")

        if proposal.target is not None:
            if self._lookup.get(proposal.target.entity, proposal.target.id) is None:
                # Unknown and inaccessible are the same answer.
                return Refusal("record_not_found", f"no {proposal.target.entity} {proposal.target.id} is available")

        if proposal.filter is not None and entity is not None:
            if refusal := self._check_value(entity, proposal.filter.field, proposal.filter.value):
                return refusal

        fields = proposal.fields
        if fields is not None and entity is not None:
            fields = self._with_defaults(entity, fields) if proposal.capability == Capability.CREATE_RECORD else fields
            for name, value in fields.items():
                if refusal := self._check_value(entity, name, value):
                    return refusal
            if proposal.capability == Capability.CREATE_RECORD:
                if refusal := self._check_required(entity, fields):
                    return refusal

        if proposal.prefill is not None and entity is not None:
            for name, value in proposal.prefill.items():
                if refusal := self._check_value(entity, name, value):
                    return refusal

        if fields is not proposal.fields:
            proposal = replace(proposal, fields=fields)
        return ValidatedAction(proposal, self._definition.definition.definition_id, self._version, confirmation)

    def _with_defaults(self, entity: EntitySpec, values: dict) -> dict:
        completed = dict(values)
        for name, spec in entity.fields.items():
            if spec.required and name not in completed and spec.default is not None:
                completed[name] = spec.default
        return completed

    def _check_required(self, entity: EntitySpec, values: dict) -> Refusal | None:
        """Every required field of the entity must be given, unless the definition defaults it.

        This covers fields the action does not declare: the contract refuses such a definition,
        so reaching one here means the definition and the entity disagree.
        """
        for name, spec in entity.fields.items():
            if not spec.required or name in values or spec.default is not None:
                continue
            return Refusal("missing_required_field", f"{name} is required to create a {entity.label.lower()}")
        return None

    def _check_value(self, entity: EntitySpec, name: str, value: object) -> Refusal | None:
        spec = entity.fields.get(name)
        if spec is None:
            return Refusal("unknown_field", f"{name} is not a field of {entity.label.lower()}")
        return self._check_typed(spec, name, value)

    def _check_typed(self, spec: FieldSpec, name: str, value: object) -> Refusal | None:
        invalid = Refusal("invalid_value", f"{value!r} is not a valid {name}")
        if spec.is_reference:
            return self._check_reference(spec, name, value)
        if spec.type == "enum":
            return None if value in spec.values else invalid
        if spec.type == "boolean":
            return None if isinstance(value, bool) else invalid
        if spec.type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return invalid
            if (spec.min is not None and value < spec.min) or (spec.max is not None and value > spec.max):
                return Refusal("value_out_of_range", f"{name} must be between {spec.min} and {spec.max}")
            return None
        if spec.type == "date":
            return None if isinstance(value, str) and _DATE.match(value) else invalid
        if spec.type == "text_list":
            items = value if isinstance(value, tuple) else None
            if items is None:
                return invalid
            return next((refusal for item in items if (refusal := self._check_plain_text(spec, name, item))), None)
        return self._check_plain_text(spec, name, value)

    def _check_reference(self, spec: FieldSpec, name: str, value: object) -> Refusal | None:
        """Shape before visibility: one record for `ref`, a collection for `refs`.

        A single-owner field given two owners, or none, is not a visibility question at all; it
        does not fit the field, and accepting it would mean executing something the definition
        cannot express.
        """
        collection = isinstance(value, (tuple, list))
        if spec.type == "ref":
            if collection:
                return Refusal("invalid_value", f"{name} takes one {spec.target}, not a list")
            ids: tuple = (value,)
        else:
            if not collection:
                return Refusal("invalid_value", f"{name} takes a list of {spec.target} records")
            if not value and spec.required:
                return Refusal("missing_required_field", f"{name} needs at least one {spec.target}")
            ids = tuple(value)
        for item in ids:
            if not isinstance(item, str) or not item:
                return Refusal("invalid_value", f"{name} must name a {spec.target} record")
            if self._lookup.get(spec.target or "", item) is None:
                # Hidden and nonexistent are the same answer.
                return Refusal("reference_not_found", f"no {spec.target} {item!r} is available")
        return None

    def _check_plain_text(self, spec: FieldSpec, name: str, value: object) -> Refusal | None:
        if not isinstance(value, str):
            return Refusal("invalid_value", f"{name} must be text")
        if len(value) > min(spec.max or MAX_TEXT, MAX_TEXT):
            return Refusal("value_too_long", f"{name} is longer than {spec.max} characters")
        if spec.min is not None and len(value) < spec.min:
            return Refusal("value_too_short", f"{name} is shorter than {spec.min} characters")
        try:
            # Values reach replies and prompts, so only plain text is accepted.
            check_text(value)
        except ValueError as error:
            return Refusal("unsafe_value", f"{name} {error}")
        return None
