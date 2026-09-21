"""Compatibility checks for legacy product copy and names.

Published definitions retain their existing copy fields and validation. These lexical checks
catch common authoring errors but cannot prove a sentence makes no false claim. The generic
composer therefore never speaks product response bodies or action descriptions. It generates
platform wording and inserts product names as names.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.definitions.vocabulary import (
    PRODUCT_CHOICE_KEYS,
    PRODUCT_COPY_PLACEHOLDERS,
    PRODUCT_IDENTITY_KEYS,
)

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_MUTATING_VERBS = (
    r"(?:create|update|change|assign|reassign|delete|remove|close|move|edit|modify|reset|erase"
    r"|archive|send|email|approve|grant|export|import|publish|pay|refund|cancel)\w*"
)


@dataclass(frozen=True)
class Rule:
    category: str
    pattern: re.Pattern[str]


def _rules(category: str, *patterns: str) -> tuple[Rule, ...]:
    return tuple(Rule(category, re.compile(pattern)) for pattern in patterns)


# Checked against lowercased text with straight apostrophes and placeholders removed.
EXECUTION_WORDS = _rules(
    "claims an action ran",
    r"\bdone\b", r"\ball set\b", r"\bsuccessful(?:ly)?\b", r"\bcompleted\b", r"\bfinished\b",
)
EXECUTION = _rules(
    "claims an action ran",
    r"\bi(?:'ve| have)\b", r"\b(?:has|have|had) been\b", r"\b(?:is|are|was|were) now\b",
    r"\bi (?:just |already )?(?:updated|created|assigned|reassigned|changed|closed|moved|deleted"
    r"|removed|added|saved|sent|opened|filtered|applied|reset|archived|made|set|prepared"
    r"|highlighted|switched|erased|approved|granted|fixed|resolved|started|scheduled)\b",
    r"\b(?:updated|created|assigned|reassigned|changed|closed|moved|deleted|removed|added|saved"
    r"|erased|archived) (?:it|them|that|this|those|these)\b",
)
PROMISE = _rules(
    "promises an action",
    r"\bi(?:'ll| will| shall|'m going to| am going to)\b", r"\bwe(?:'ll| will)\b",
    r"\blet me\b(?! know)",
)
AUTHORIZATION = _rules(
    "asserts a refusal or permission",
    r"\bcan(?:'t|not)\b", r"\bcan not\b", r"\bwon't\b", r"\bwill not\b", r"\bunable\b",
    r"\bnot (?:allowed|permitted|authori[sz]ed)\b", r"\ballowed to\b", r"\bforbidden\b",
    r"\bunauthori[sz]ed\b", r"\bauthori[sz]ed\b", r"\bdenied\b", r"\brefus(?:e|ed|es|ing)\b",
    r"\bblocked\b", r"\bno access\b", r"\bpermissions?\b", r"\badministrators?\b",
    r"\bprivileges?\b",
)
CAPABILITY_CLAIM = _rules(
    "asserts a refusal or permission",
    rf"\b(?:i|you|we)(?: can| could| may|'re able to| are able to| am able to)\b[^.?!]{{0,40}}?"
    rf"\b{_MUTATING_VERBS}",
)
FAILURE = _rules(
    "asserts a failure",
    r"\bfail(?:ed|s|ure|ing)?\b", r"\bcouldn't\b", r"\bcould not\b", r"\berrors?\b",
    r"\bwent wrong\b", r"\bbroken\b", r"\bunavailable\b",
)
SCOPE = _rules(
    "asserts what is in scope",
    r"\boutside (?:of|this|that|your|the|its|their)\b", r"\bout of scope\b", r"\bin scope\b",
    r"\bnot (?:in|within|part of) (?:your|this|the|that)\b", r"\bbeyond (?:your|this|the)\b",
    r"\bvisible to\b", r"\bonly see\b",
)
COUNT = _rules(
    "states a count",
    r"\d",
    r"\b(?:zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty"
    r"|hundreds?|thousands?|millions?|dozens?|none|nobody|no one|nothing|several|many|few"
    r"|single)\b",
)
FACT = _rules(
    "states a retrieved fact",
    r"\bfound\b", r"\bi (?:can )?see\b", r"\bthere(?:'s| is| are| was| were)\b",
    r"\bcurrently (?:has|have|holds?|contains?|owns?)\b", r"\bbelongs? to\b",
    r"\b(?:is|are|was|were) (?:assigned|owned|due|overdue|open|closed)\b", r"\bowned by\b",
)
HISTORY = _rules(
    "describes history",
    r"\balready\b", r"\bpreviously\b", r"\bearlier\b", r"\blast time\b", r"\brecent(?:ly)?\b",
    r"\bso far\b", r"\bno changes?\b", r"\bnothing (?:has )?changed\b",
    r"\bha(?:ve|s)(?:n't| not) changed\b", r"\bhistory\b", r"\byou (?:asked|said|told)\b",
    r"\bwe (?:discussed|talked)\b",
)
KNOWLEDGE = _rules(
    "asserts what knowledge is available",
    r"\bi (?:don't|do not) know\b", r"\bno (?:approved )?information\b",
    r"\b(?:don't|do not) have (?:any |approved )*(?:information|details|data|docs|documentation)\b",
    r"\bnot documented\b", r"\baccording to\b", r"\b(?:documentation|the docs) (?:says?|shows?|states?)\b",
    r"\bapproved (?:information|sources?|docs|documentation)\b", r"\bguess\b",
)
DESTRUCTIVE = _rules(
    "advertises deletion, which no product can do",
    r"\b(?:delete[sd]?|deleting|erase[sd]?|erasing|wipe[sd]?|wiping|purge[sd]?|purging"
    r"|destroy(?:s|ed|ing)?)\b",
)
FIRST_PERSON_CLAIM = _rules(
    "makes a statement",
    r"\bi\b", r"\bi'", r"\bwe\b", r"\bwe'", r"\byou\b", r"\byou'",
)

# Sentences a product may write: identity copy and choice questions.
COPY_RULES = (*EXECUTION_WORDS, *EXECUTION, *PROMISE, *AUTHORIZATION, *CAPABILITY_CLAIM,
              *FAILURE, *SCOPE, *COUNT, *FACT, *HISTORY, *KNOWLEDGE, *DESTRUCTIVE)
# Names and labels are inserted into platform sentences, so they must not be sentences at all.
# Single words such as "Completed" or "Failed" are legitimate names for a view or a status.
NAME_RULES = (*EXECUTION, *PROMISE, *FIRST_PERSON_CLAIM)
LABEL_RULES = (*EXECUTION, *PROMISE, *FIRST_PERSON_CLAIM, *DESTRUCTIVE)

MAX_LABEL = 120
_SENTENCE_BREAK = re.compile(r"[.;:!?]\s+\S")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 &'.()-]*")


def _normalized(text: str) -> str:
    plain = _PLACEHOLDER.sub(" ", text.replace("’", "'").replace("‘", "'"))
    return " ".join(plain.lower().split())


def _violations(text: str, rules: Iterable[Rule]) -> list[str]:
    normalized = _normalized(text)
    found: dict[str, str] = {}
    for rule in rules:
        match = rule.pattern.search(normalized)
        if match and rule.category not in found:
            found[rule.category] = f"{rule.category} ({match.group(0).strip()!r})"
    return list(found.values())


def _placeholders(text: str) -> list[str]:
    unknown = sorted(set(_PLACEHOLDER.findall(text)) - PRODUCT_COPY_PLACEHOLDERS)
    return [f"uses facts only the platform may state ({', '.join('{' + u + '}' for u in unknown)})"] \
        if unknown else []


def identity_copy_problems(text: str) -> list[str]:
    """A greeting or introduction: names and tone, asserting nothing."""
    return _placeholders(text) + _violations(text, COPY_RULES)


def choice_question_problems(text: str) -> list[str]:
    """A menu the product asks to tell its own requests apart. One question; no statements."""
    problems = _placeholders(text) + _violations(text, COPY_RULES)
    stripped = text.strip()
    if stripped.count("?") != 1 or not stripped.endswith("?") or re.search(r"[.!](?:\s|$)", stripped):
        problems.append("is not a single question")
    return problems


def name_problems(text: str) -> list[str]:
    """A product, assistant, entity or view name: never a sentence."""
    problems = _violations(text, NAME_RULES)
    if not _NAME.fullmatch(text.strip()) or _SENTENCE_BREAK.search(text):
        problems.append("is not a plain name")
    return problems


def label_problems(text: str) -> list[str]:
    """An action description: one short phrase, listed by the platform as something it can do."""
    problems = _violations(text, LABEL_RULES)
    body = text.strip().removesuffix(".")
    if len(text) > MAX_LABEL or _SENTENCE_BREAK.search(body) or re.search(r"[!?:;]", body):
        problems.append("is not a single short phrase")
    return problems


def definition_copy_problems(definition) -> list[str]:
    """Every rule broken by a definition's product-controlled text, as `where: problem` lines."""
    checks: list[tuple[str, str, object]] = [
        ("identity.product_name", definition.identity.product_name, name_problems),
        ("identity.assistant_name", definition.identity.assistant_name, name_problems),
        ("identity.greeting", definition.identity.greeting, identity_copy_problems),
    ]
    checks += [(f"responses.{key}", definition.responses[key], identity_copy_problems)
               for key in sorted(PRODUCT_IDENTITY_KEYS & definition.responses.keys())]
    checks += [(f"responses.{key}", definition.responses[key], choice_question_problems)
               for key in sorted(PRODUCT_CHOICE_KEYS & definition.responses.keys())]
    for name, entity in sorted(definition.entities.items()):
        checks += [(f"entities.{name}.label", entity.label, name_problems),
                   (f"entities.{name}.plural", entity.plural, name_problems)]
    for name, view in sorted(definition.views.items()):
        checks.append((f"views.{name}.label", view.label, name_problems))
        checks += [(f"views.{name}.controls.{control}.label", spec.label, name_problems)
                   for control, spec in sorted(view.controls.items())]
    checks += [(f"actions.{name}.description", action.description, label_problems)
               for name, action in sorted(definition.actions.items())]
    return [f"{where}: {problem}" for where, text, check in checks for problem in check(text)]


def settings_copy_problems(settings) -> list[str]:
    """A product binding's overrides are product-controlled copy too."""
    checks = [
        ("display_name", settings.display_name, name_problems),
        ("assistant_name", settings.assistant_name, name_problems),
        ("greeting", settings.greeting, identity_copy_problems),
    ]
    return [f"{where}: {problem}" for where, text, check in checks if text is not None
            for problem in check(text)]
