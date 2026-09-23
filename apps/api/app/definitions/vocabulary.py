"""Closed platform vocabularies.

A product definition may only *request* what is listed here. Adding a capability, platform
view, response key, placeholder or tenant setting is a reviewed platform code change;
no definition can create one by declaring it.
"""
from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    NAVIGATE_VIEW = "NAVIGATE_VIEW"
    OPEN_RECORD = "OPEN_RECORD"
    FILTER_RECORDS = "FILTER_RECORDS"
    CREATE_RECORD = "CREATE_RECORD"
    UPDATE_RECORD = "UPDATE_RECORD"
    HIGHLIGHT_CONTROL = "HIGHLIGHT_CONTROL"


# Record deletion is deliberately absent.
MUTATING_CAPABILITIES = frozenset({Capability.CREATE_RECORD, Capability.UPDATE_RECORD})

# Views owned by Pixel itself and available to every product.
PLATFORM_VIEWS = frozenset({"architecture"})

# Placeholders a response template may use. Values are always inserted as plain text.
RESPONSE_PLACEHOLDERS = frozenset({
    "product", "assistant", "scope", "view", "visitor",
    "person", "record_id", "record_title", "records",
    # What the thing being counted is called, singular or plural to match the count. It comes
    # from the definition's own labels, so a product can word its counts in its own terms.
    "label",
    "field", "value", "changes", "count",
})

# Response templates the platform knows how to use.
RESPONSE_KEYS = frozenset({
    "greeting", "greeting_named", "identity", "capabilities", "fallback",
    "guided_path", "next_step", "nothing_changed", "last_change", "correction",
    "view_opened", "record_opened", "records_filtered", "record_created", "record_updated",
    "control_highlighted", "member_missing", "unknown_person",
    "out_of_scope", "destructive_refused", "person_outside_scope", "work_outside_scope",
    "broad_scope_refused", "clarify_create", "clarify_assign", "clarify_owner",
    "clarify_all_items", "clarify_update_target", "people_count", "anchor_count",
    "conversation_ended",
    # Added in 3.2: a mutation is described as proposed until it has executed, and actions that
    # need an explicit yes are confirmed or cancelled.
    "record_create_proposed", "record_update_proposed", "confirm_action", "action_cancelled",
    # Added in 3.2: the question asked when a request names more than one visible person.
    "clarify_person",
    # Added in 3.2 slice 4b: what to say when no knowledge source can answer a question.
    "knowledge_unavailable",
    # Added in 5c: a change request that names a record but no field to change.
    "clarify_change",
})

# All generic-engine speech is platform-owned. Legacy copy fields remain accepted and
# validated so published definitions do not have to be rewritten.
PRODUCT_IDENTITY_KEYS = frozenset({"greeting", "greeting_named", "identity"})
PRODUCT_CHOICE_KEYS = frozenset({"clarify_create", "clarify_all_items"})
LEGACY_PRODUCT_COPY_KEYS = PRODUCT_IDENTITY_KEYS | PRODUCT_CHOICE_KEYS
PRODUCT_VOICE_KEYS = frozenset()
PLATFORM_RESPONSE_KEYS = RESPONSE_KEYS
PRODUCT_COPY_PLACEHOLDERS = frozenset({"product", "assistant", "visitor"})

# Whole-message replies that confirm a pending action. Anything else cancels it.
AFFIRMATIONS = frozenset({"yes", "yes please", "confirm", "go ahead", "do it"})

# Replies that reject the candidate the assistant singled out. They never select another candidate.
CORRECTION_CUES = frozenset({"no", "not that one", "the other one", "wrong one"})

# Parameters an intent may ask the router to extract from the visitor's message.
INTENT_REQUIREMENTS = frozenset({"person", "record", "selected_record", "unknown_person"})

# Product settings a binding may override. Everything else is rejected.
TENANT_SETTING_KEYS = frozenset({"display_name", "assistant_name", "greeting"})

# Who owns a definition. There is no team-private ownership in Milestone 3.
DEFINITION_OWNERSHIP = ("platform_shared", "organization_private")

# Roles an organization user can hold. Visitors are a separate kind of principal.
MEMBER_ROLES = ("org_admin", "team_admin", "team_member")
TEAM_ROLES = ("team_admin", "team_member")

ORGANIZATION_STATES = ("active", "suspended")
TEAM_STATES = ("active", "disabled")
PRODUCT_STATES = ("active", "disabled")

# Field types an entity may declare.
SCALAR_FIELD_TYPES = frozenset({"text", "integer", "enum", "date", "boolean", "text_list"})
REFERENCE_FIELD_TYPES = frozenset({"ref", "refs"})

# The longest path from a record to its scope anchor (record -> related record -> anchor).
MAX_SCOPE_HOPS = 2
