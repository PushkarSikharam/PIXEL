"""Required-field completion for creating a record (5c plan, section 7.1).

A create names only what the visitor said. The validator applies defaults the pinned definition
declares; anything still required is asked for, one field at a time, in definition order. Each
answer is read against that field's declared type and the caller's visible records, and the
completed create is validated again before anything is proposed. Nothing is inferred from demo
data, the page on screen or another engine's habits.

The product supplies labels and allowed values; the platform supplies the question structure.
"""
from __future__ import annotations

import re
from typing import Any

from app.definitions.contract import EntitySpec, FieldSpec
from app.engine.snapshot import TurnSnapshot

# At most this many visible records are named in a question; more are asked for without a list.
MAX_LISTED = 5

_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_YES = frozenset({"yes", "y", "yeah", "yep", "true"})
_NO = frozenset({"no", "n", "nope", "false"})
_FILLER = frozenset({"the", "a", "an", "it", "in", "to", "for", "please", "use", "make", "set"})


def first_missing(entity: EntitySpec, fields: dict[str, Any]) -> str | None:
    """The first required field, in definition order, that is neither given nor defaulted."""
    for name, spec in entity.fields.items():
        if spec.required and name not in fields and spec.default is None:
            return name
    return None


def field_label(name: str, spec: FieldSpec) -> str:
    return (spec.label or name.replace("_", " ")).lower()


def question_values(entity: EntitySpec, name: str, snapshot: TurnSnapshot) -> tuple[str, dict[str, str]]:
    """The platform question for one missing field, and the values it names."""
    spec = entity.fields[name]
    values = {"label": entity.label.lower(), "field": field_label(name, spec)}
    if spec.type == "enum":
        values["options"] = _either(list(spec.values))
        return "What {field} should the new {label} have: {options}?", values
    if spec.is_reference:
        visible = snapshot.records.get(spec.target or "", ())
        if 0 < len(visible) <= MAX_LISTED:
            values["options"] = _either([record.title or record.id for record in visible])
            return "Which {field} should the new {label} have: {options}?", values
        return "Which {field} should the new {label} have?", values
    if spec.type == "integer":
        return "What number should the {field} of the new {label} be?", values
    if spec.type == "boolean":
        return "Should the new {label} be {field}? Please answer yes or no.", values
    if spec.type == "date":
        return "What date should the {field} of the new {label} be? Please use YYYY-MM-DD.", values
    return "What should the {field} of the new {label} be?", values


def read_answer(spec: FieldSpec, message: str, snapshot: TurnSnapshot) -> Any | None:
    """The value an answer gives this field, or None when it does not clearly give one.

    An ambiguous, unknown or inaccessible reference all read as no answer: none says whether a
    record exists somewhere the caller cannot see.
    """
    text = message.strip().strip("\"'").rstrip(".!?").strip()
    lowered = text.lower()
    if not text:
        return None
    if spec.type == "enum":
        matches = [value for value in spec.values if _mentions(lowered, value.lower())]
        return matches[0] if len(matches) == 1 else None
    if spec.is_reference:
        if spec.type != "ref":
            return None
        return _one_reference(spec.target or "", lowered, snapshot)
    if spec.type == "integer":
        match = re.fullmatch(r"-?\d+", lowered)
        return int(match.group(0)) if match else None
    if spec.type == "boolean":
        word = lowered.split()[0] if lowered.split() else ""
        return True if word in _YES else False if word in _NO else None
    if spec.type == "date":
        match = _DATE.search(lowered)
        return match.group(1) if match else None
    if spec.type == "text":
        return text
    return None


def _one_reference(target: str, lowered: str, snapshot: TurnSnapshot) -> str | None:
    exact = snapshot.get(target, lowered)
    if exact is not None:
        return exact.id
    # The target entity's own key ("the Atlas <entity>") is not part of any record's name.
    ignored = _FILLER | {target.lower(), f"{target.lower()}s"}
    words = [word for word in re.findall(r"[a-z0-9-]+", lowered) if word not in ignored]
    if not words:
        return None
    candidates = snapshot.records.get(target, ())
    matched = [
        record for record in candidates
        if (record.title or "").lower() == " ".join(words)
        or all(word in (record.title or "").lower().split() for word in words)
    ]
    return matched[0].id if len(matched) == 1 else None


def _mentions(text: str, value: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", text) is not None


def _either(options: list[str]) -> str:
    if len(options) <= 1:
        return "".join(options)
    return f"{', '.join(options[:-1])} or {options[-1]}"
