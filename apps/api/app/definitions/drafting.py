"""Turning a description of a product into a definition the platform can run.

Somebody adding a product to Pixel knows what their product is made of - the kinds of record it
keeps, what each one carries, and who works on them - but not how this platform's definitions are
written. This module takes that description and produces a definition: the entities, the views,
the actions, the intents and the words, all derived from what they said. Nothing in this file
knows the vocabulary of any particular product; every noun in the result came from the caller.

What comes out is a draft, not an authority. It is parsed and validated by the same contract
every definition passes, and it is meant to be read and approved by the person who described it
before their product runs on it. Nothing here is a shortcut past that contract: a description
that cannot produce a valid definition produces an error instead of a half-built product.

Two things are deliberately not invented. Scopes are not generated, because a scope decides who
may see what and guessing that would widen somebody's access. Guardrails beyond refusing
destruction are not generated, because a refusal nobody asked for is as wrong as an action
nobody asked for.
"""
from __future__ import annotations

from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.definitions.safety import check_key, check_term, check_text, check_value

# The field types somebody can ask for without knowing the contract. References between their
# things are worked out from the description rather than asked for.
DraftFieldType = Literal["text", "integer", "enum", "date", "boolean"]
MAX_THINGS = 8
MAX_FIELDS = 12


class DraftField(BaseModel):
    """One piece of information a thing carries."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=48)]
    label: Annotated[str, Field(min_length=1, max_length=60)] | None = None
    type: DraftFieldType = "text"
    required: bool = False
    values: Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]],
                      Field(max_length=20)] = []

    @model_validator(mode="after")
    def _sound(self) -> "DraftField":
        check_key(self.name)
        if self.label is not None:
            check_text(self.label)
        if self.type == "enum" and len(self.values) < 2:
            raise ValueError(f"{self.name} is a choice, so it needs at least two values")
        if self.type != "enum" and self.values:
            raise ValueError(f"{self.name} only takes values when it is a choice")
        for value in self.values:
            check_value(value)
        return self


class DraftThing(BaseModel):
    """One kind of record the product keeps."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=48)]
    label: Annotated[str, Field(min_length=1, max_length=60)]
    plural: Annotated[str, Field(min_length=1, max_length=60)]
    # The people who work on everything else. Exactly one thing may be marked this way.
    people: bool = False
    fields: Annotated[list[DraftField], Field(max_length=MAX_FIELDS)] = []

    @model_validator(mode="after")
    def _sound(self) -> "DraftThing":
        check_key(self.name)
        check_text(self.label)
        check_text(self.plural)
        check_term(self.label.lower())
        check_term(self.plural.lower())
        names = [field.name for field in self.fields]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.name} names a field twice")
        return self


def _checked(field: str, label: str, rule, value: str) -> None:
    """Apply one rule, and say which field failed it in words from that field's own label."""
    try:
        rule(value)
    except ValueError as refused:
        raise ValueError(f"{field}: {label} {refused}") from refused


class ProductDraft(BaseModel):
    """A product as the person adding it describes it."""

    model_config = ConfigDict(extra="forbid")

    product_name: Annotated[str, Field(min_length=1, max_length=60)]
    # Who guides this product. The platform's own assistant has a name, but core does not hold
    # it: the caller says who is speaking, so nothing here is tied to one deployment's branding.
    assistant_name: Annotated[str, Field(min_length=1, max_length=40)]
    definition_id: Annotated[str, Field(min_length=1, max_length=48)]
    things: Annotated[list[DraftThing], Field(min_length=1, max_length=MAX_THINGS)]

    @model_validator(mode="after")
    def _sound(self) -> "ProductDraft":
        # Attributed as it is checked. A description is written by a person filling in boxes, so
        # a refusal has to say which box; an unattributed "must not contain '<'" leaves somebody
        # rereading three screens to find what we meant.
        _checked("product_name", "The product name", check_text, self.product_name)
        _checked("assistant_name", "The assistant's name", check_text, self.assistant_name)
        _checked("definition_id", "The product's address", check_key, self.definition_id)
        names = [thing.name for thing in self.things]
        if len(set(names)) != len(names):
            raise ValueError("two things share a name")
        if sum(1 for thing in self.things if thing.people) > 1:
            raise ValueError("only one kind of record can be the people")
        if not [thing for thing in self.things if not thing.people]:
            raise ValueError("a product needs something for its people to work on")
        return self

    @property
    def people_thing(self) -> DraftThing | None:
        return next((thing for thing in self.things if thing.people), None)

    @property
    def main_thing(self) -> DraftThing:
        """What the product is mostly about: the first thing that is not its people."""
        return next(thing for thing in self.things if not thing.people)


def _prefix(name: str, taken: set[str]) -> str:
    """A short identifier prefix somebody can read out and type back.

    One word gives its first letters (a deal becomes DEAL-1); several words give their initials
    (a support case becomes SC-1). A prefix already used gains a digit rather than colliding.
    """
    parts = [part for part in name.split("_") if part]
    letters = ("".join(part[0] for part in parts) if len(parts) > 1 else parts[0][:4]).upper()
    candidate = letters.ljust(2, "X")[:6]
    suffix = 2
    while candidate in taken:
        candidate = f"{letters[:4]}{suffix}".upper()[:6]
        suffix += 1
    taken.add(candidate)
    return candidate


def _a(noun: str) -> str:
    """"an agent", "a deal": the article a person would write."""
    return f"{'an' if noun[:1] in 'aeiou' else 'a'} {noun}"


def _label(field: DraftField) -> str:
    return field.label or field.name.replace("_", " ")


def _title_field(thing: DraftThing) -> DraftField:
    """What one of these is called. A description with no text field gets one."""
    for field in thing.fields:
        if field.type == "text":
            return field
    return DraftField(name="name", label="Name", type="text", required=True)


def _words(thing: DraftThing) -> list[str]:
    """The words somebody would use for this kind of record."""
    seen: list[str] = []
    for word in (thing.label.lower(), thing.plural.lower(), thing.name.replace("_", " ")):
        if word and word not in seen:
            seen.append(word)
    return seen


def draft_definition(draft: ProductDraft, owner_organization: str | None = None) -> dict:
    """The definition this description asks for, ready to be validated and reviewed."""
    main = draft.main_thing
    people = draft.people_thing
    link = f"{main.name}"  # the reference every other thing carries to the main one
    people_link = f"{main.name}s_worked_on" if people else None

    entities: dict[str, dict] = {}
    prefixes: set[str] = set()
    titles: dict[str, str] = {}
    for thing in draft.things:
        title = _title_field(thing)
        titles[thing.name] = title.name
        fields: dict[str, dict] = {}
        for field in [title, *[f for f in thing.fields if f.name != title.name]]:
            spec: dict = {"type": field.type, "label": _label(field).title(),
                          "required": field.required}
            if field.type == "enum":
                spec["values"] = list(field.values)
                spec["default"] = field.values[0]
            if field.type == "text":
                spec["max"] = 300
            fields[field.name] = spec
        # Somebody's name is what identifies them, so it is not renamed afterwards.
        if thing.people:
            fields[title.name]["editable"] = False
            fields[title.name]["required"] = True
        if thing is not main:
            # Every other thing points at what the product is mostly about, so the platform can
            # tell which part of the product a record belongs to.
            reference = people_link if thing.people else link
            fields[reference] = {
                "type": "refs" if thing.people else "ref", "target": main.name,
                "label": main.plural if thing.people else main.label,
            }
        if people and thing is main:
            fields["owner"] = {"type": "ref", "target": people.name,
                               "label": f"{people.label}", "required": False}
        entities[thing.name] = {
            "label": thing.label, "plural": thing.plural,
            "id": ({"strategy": "slug", "from_field": title.name} if thing.people
                   else {"strategy": "prefix", "prefix": _prefix(thing.name, prefixes)}),
            "title_field": title.name,
            "summary_fields": [f.name for f in thing.fields if f.type == "enum"][:2],
            "fields": fields,
        }

    views: dict[str, dict] = {}
    actions: dict[str, dict] = {}
    intents: list[dict] = []
    terms: list[str] = []
    for thing in draft.things:
        view_key = thing.name if thing.name != "overview" else f"{thing.name}_list"
        columns = [titles[thing.name]] + [f.name for f in thing.fields
                                          if f.name != titles[thing.name]][:3]
        views[view_key] = {"label": thing.plural, "kind": "list", "entity": thing.name,
                           "columns": columns}
        words = _words(thing)
        terms.extend(words)

        open_view, open_one = f"open_{thing.name}s", f"open_{thing.name}"
        actions[open_view] = {"capability": "NAVIGATE_VIEW", "view": view_key,
                              "description": f"Open the list of {thing.plural.lower()}."}
        actions[open_one] = {"capability": "OPEN_RECORD", "entity": thing.name,
                             "description": f"Open one {thing.label.lower()}."}
        intents.append({"action": open_view, "response": "anchor_count",
                        "match": [["how many", "count", "number of"], words]})
        intents.append({"action": open_view, "response": "view_opened", "match": [words]})
        # Only the singular here: "show me the deals" asks for the list, and an intent that also
        # matched the plural would answer it by asking which one deal was meant.
        singular = sorted({thing.label.lower(), thing.name.replace("_", " ")})
        intents.append({"action": open_one, "requires": ["record"], "response": "record_opened",
                        "match": [["open", "pull up"], singular]})

        # A field that may not be changed afterwards may still be set when the record is made:
        # somebody's name identifies them, so it is given once and not renamed.
        settable = [name for name, spec in entities[thing.name]["fields"].items()
                    if spec["type"] != "refs"
                    # Who owns it is decided after it exists: a create that demanded a person
                    # would refuse to start until one was named.
                    and not (people is not None and spec.get("target") == people.name)]
        if settable:
            create = f"create_{thing.name}"
            actions[create] = {"capability": "CREATE_RECORD", "entity": thing.name,
                               "fields": settable,
                               "description": f"Add {_a(thing.label.lower())}.",
                               "confirm": True}
            intents.append({"action": create, "response": "record_created",
                            "match": [["add", "create", "new", "raise", "log"], words]})
        # Who owns it is not asked for when it is made, but it is very much changeable
        # afterwards: assigning work is a change of owner.
        owner_field = [name for name, spec in entities[thing.name]["fields"].items()
                       if people is not None and spec.get("target") == people.name
                       and spec["type"] == "ref"]
        changeable = [name for name in [*settable, *owner_field]
                      if name != titles[thing.name]
                      and entities[thing.name]["fields"][name].get("editable", True)]
        if changeable:
            change = f"change_{thing.name}"
            actions[change] = {"capability": "UPDATE_RECORD", "entity": thing.name,
                               "fields": changeable,
                               "description": f"Change {_a(thing.label.lower())}.",
                               "confirm": True}
            change_words = ["change", "set", "update", "move"]
            if people and "owner" in changeable:
                change_words += ["assign", "give", "hand", "reassign", "owner"]
            for spec in entities[thing.name]["fields"].values():
                change_words.extend(value.lower() for value in spec.get("values", []))
            intents.append({"action": change, "requires": ["record"], "response": "record_updated",
                            "match": [sorted(set(change_words))]})

    if people:
        actions[f"{main.name}s_by_owner"] = {
            "capability": "FILTER_RECORDS", "entity": main.name, "by": "owner",
            "description": f"Show every {main.label.lower()} one {people.label.lower()} owns.",
        }
        intents.append({"action": f"{main.name}s_by_owner", "requires": ["person"],
                        "response": "records_filtered",
                        "match": [["for", "owned by", "working on", "assigned to", "looking after"],
                                  _words(main)]})

    scope_paths = {main.name: []}
    for thing in draft.things:
        if thing is main:
            continue
        scope_paths[thing.name] = [people_link if thing.people else link]

    definition: dict = {
        "definition": {
            "definition_id": draft.definition_id,
            "version": 1,
            "ownership": "organization_private" if owner_organization else "platform_shared",
            **({"owner_organization": owner_organization} if owner_organization else {}),
        },
        "identity": {
            "product_name": draft.product_name,
            "assistant_name": draft.assistant_name,
            "persona": f"A concise, plain-spoken guide to {draft.product_name}.",
            "voice_style": (f"You are {draft.assistant_name}, a calm, warm and natural "
                            f"conversational assistant for {draft.product_name}. Speak clearly "
                            "and unhurriedly."),
            "greeting": "Welcome to {product}. I'm {assistant}.",
        },
        "vocabulary": {
            "terms": sorted(set(terms)),
            "correction_markers": ["actually", "instead", "rather", "i mean", "sorry"],
            "negatable_terms": sorted(set(terms)),
        },
        "entities": entities,
        "scope": {"anchor": main.name, "paths": scope_paths},
        "views": views,
        "actions": actions,
        "intents": intents,
        "guardrails": [
            {"topic": "destructive_change", "response": "destructive_refused",
             "match": [["delete", "erase", "wipe", "clear", "remove all", "delete all"]]},
        ],
        "responses": {
            "view_opened": "I'll open {view}.",
            "record_opened": "I'll open {record_id}.",
            "record_updated": "I'll update {record_id}: {changes}.",
            "destructive_refused": "I can't delete or erase anything here.",
            "anchor_count": "{scope} has {count} of those: {records}.",
            "records_filtered": "I found {count} for {person}.",
        },
    }
    if people:
        definition["people"] = {"entity": people.name,
                                "assigned_by": [f"{main.name}.owner"],
                                "match_on": [titles[people.name]]}
    return definition


class _PlainDumper(yaml.SafeDumper):
    """Writes every value out in full.

    A definition may not contain YAML aliases, and a drafted one repeats the same list of words
    in several intents, which an ordinary dumper would collapse into an alias.
    """

    def ignore_aliases(self, data) -> bool:
        return True


def draft_text(draft: ProductDraft, owner_organization: str | None = None) -> str:
    """The drafted definition as the text that is stored, reviewed and published."""
    return yaml.dump(draft_definition(draft, owner_organization), Dumper=_PlainDumper, sort_keys=False,
                     allow_unicode=True, default_flow_style=False, width=100)
