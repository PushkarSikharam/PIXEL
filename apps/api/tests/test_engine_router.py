"""Milestone 3.2 slice 2: routing precedence, memory, corrections and confirmation.

Conversations are replayed turn by turn with their own memory. Router output is a proposal;
where a scenario needs the next turn to see an accepted proposal, the test says so explicitly
by calling `remember_accepted`, standing in for the slice 3 validator.
"""
from __future__ import annotations

import copy
from dataclasses import fields
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.definitions.vocabulary import Capability
from app.engine.actions import ConfirmationReason, RecordRef
from app.engine.memory import ConversationMemory, PendingClarification
from app.engine.router import IntentRouter, TurnContext, remember_accepted
from app.engine.routing import RouteKind, RouteResult, RouteStage
from engine_fixtures import InMemoryLookup, SampleDesk, engine_definition, load_engine_definition

ANA_LOPEZ = RecordRef("agent", "ana-lopez")
ANA_REYES = RecordRef("agent", "ana-reyes")
ANA_SINGH = RecordRef("agent", "ana-singh")
CON_2 = RecordRef("contact", "CON-2")


class Conversation:
    """One visitor's conversation: sequential turns sharing memory."""

    def __init__(self, router: IntentRouter) -> None:
        self.router = router
        self.memory = ConversationMemory()
        self.turn = 0

    def say(self, message: str, *, assume_validated: bool = True) -> RouteResult:
        self.turn += 1
        routed = self.router.route(message, self.memory, TurnContext(turn=self.turn))
        self.memory = routed.memory
        if assume_validated and routed.result.kind in (RouteKind.PROPOSE, RouteKind.CONFIRM):
            # Stands in for the slice 3 validator accepting the proposal.
            self.memory = remember_accepted(self.memory, routed.result)
        return routed.result


class RouterFixture(unittest.TestCase):
    def setUp(self):
        self.lookup = InMemoryLookup(SampleDesk(), frozenset({"ACC-1"}))
        self.router = IntentRouter(load_engine_definition(), self.lookup)

    def chat(self) -> Conversation:
        return Conversation(self.router)

    def open_bens_contact(self, chat: Conversation) -> None:
        result = chat.say("open Ben's contact")
        self.assertEqual((result.kind, result.proposal.target), (RouteKind.PROPOSE, CON_2))


class PrecedenceTest(RouterFixture):
    def test_router_output_is_only_a_proposal(self):
        result = self.chat().say("show me the contacts", assume_validated=False)
        self.assertEqual((result.kind, result.proposal.action_key), (RouteKind.PROPOSE, "open_contacts"))
        self.assertFalse(result.confirmed)
        # A routing result cannot carry validation, authorization or execution state at all.
        names = {f.name for f in fields(result)} | {f.name for f in fields(result.proposal)}
        self.assertEqual(names & {"validated", "authorized", "dispatched", "executed", "execution_key"}, set())

    def test_exact_phrases_beat_match_groups(self):
        result = self.chat().say("everything please")
        self.assertEqual((result.stage, result.proposal.action_key), (RouteStage.EXACT_PHRASES, "open_contacts"))

    def test_more_specific_intents_win(self):
        chat = self.chat()
        filtered = chat.say("contacts for Ben")
        self.assertEqual(filtered.proposal.action_key, "contacts_by_owner")
        self.assertEqual(filtered.proposal.filter.value, "ben-okafor")
        opened = self.chat().say("open Ben's contact")
        self.assertEqual(opened.proposal.action_key, "open_contact", "a satisfied requirement outranks a bare view")

    def test_ties_never_fall_back_to_file_order(self):
        result = self.chat().say("show the overview")
        self.assertEqual((result.kind, result.stage), (RouteKind.FALLBACK, RouteStage.FALLBACK))
        reordered = engine_definition()
        reordered["intents"].reverse()
        router = IntentRouter(load_engine_definition(document=reordered), self.lookup)
        self.assertEqual(Conversation(router).say("show the overview").kind, RouteKind.FALLBACK)

    def test_clarification_rules_apply_only_when_no_intent_matches(self):
        result = self.chat().say("create something new")
        self.assertEqual((result.kind, result.response_key), (RouteKind.CLARIFY, "clarify_create"))
        self.assertEqual(self.chat().say("create a new contacts list").proposal.action_key, "open_contacts")

    def test_unmatched_messages_fall_back(self):
        result = self.chat().say("tell me a joke")
        self.assertEqual((result.kind, result.response_key), (RouteKind.FALLBACK, "fallback"))


class NormalizationSafetyTest(RouterFixture):
    def test_a_product_word_never_respells_a_phrase_the_platform_reads(self):
        """A product that declares a near-neighbour of a platform phrase must not silently
        rewrite it: spelling correction may not make the platform deaf to its own questions."""
        document = engine_definition()
        document["intents"][0].setdefault("exclude", []).append("change")
        router = IntentRouter(load_engine_definition(document=document), self.lookup)
        for message in ("what changed", "what did you change", "thanks", "what next"):
            with self.subTest(message=message):
                normalized = router.normalizer.normalize(message)
                self.assertEqual(normalized.full, message)
                self.assertEqual(normalized.focused, message)

    def test_correction_markers_select_what_was_finally_asked(self):
        result = self.chat().say("reopen it, actually show me the contacts")
        self.assertEqual(result.proposal.action_key, "open_contacts")

    def test_correction_markers_never_hide_a_refusal(self):
        for message in ("delete everything, actually show me the contacts",
                        "show contacts instead of the invoice",
                        "not the invoice, just contacts"):
            with self.subTest(message=message):
                result = self.chat().say(message)
                self.assertEqual(result.kind, RouteKind.REFUSE)

    def test_the_original_message_is_kept(self):
        normalized = self.router.normalizer.normalize("Show Ben's CONTACTS, actually Ana's")
        self.assertEqual(normalized.original, "Show Ben's CONTACTS, actually Ana's")
        self.assertEqual(normalized.full, "show ben contacts actually ana")
        self.assertEqual(normalized.focused, "ana")

    def test_negated_terms_are_dropped_only_from_the_focused_form(self):
        normalized = self.router.normalizer.normalize("not contacts, show the overview")
        self.assertIn("contacts", normalized.full)
        self.assertNotIn("contacts", normalized.focused)

    def test_spelling_is_corrected_from_the_vocabulary(self):
        self.assertEqual(self.router.normalizer.normalize("show the acount").full, "show the account")


class ClarificationMemoryTest(RouterFixture):
    def test_ambiguous_people_are_asked_about_with_visible_candidates_only(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        asked = chat.say("give it to Ana")
        self.assertEqual((asked.kind, asked.response_key), (RouteKind.CLARIFY, "clarify_assign"))
        pending = chat.memory.pending_clarification
        self.assertEqual(set(pending.candidates), {ANA_LOPEZ, ANA_REYES, ANA_SINGH})
        self.assertNotIn(RecordRef("agent", "ana-kim"), pending.candidates, "hidden people are never offered")
        self.assertEqual((pending.action_key, pending.target), ("reassign_contact", CON_2))

    def test_an_answer_naming_one_candidate_completes_the_request(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.say("give it to Ana")
        result = chat.say("Ana Reyes")
        self.assertEqual(result.kind, RouteKind.PROPOSE)
        self.assertEqual((result.proposal.target, dict(result.proposal.fields)), (CON_2, {"owner": "ana-reyes"}))
        self.assertIsNone(chat.memory.pending_clarification)

    def test_ordinals_select_from_the_offered_list(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.say("give it to Ana")
        offered = chat.memory.pending_clarification.candidates
        result = chat.say("the second one")
        self.assertEqual(result.proposal.fields["owner"], offered[1].id)

    def test_a_reply_can_reject_a_named_candidate(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.say("give it to Ana")
        result = chat.say("not Ana Lopez")
        self.assertEqual(result.kind, RouteKind.CLARIFY)
        pending = chat.memory.pending_clarification
        self.assertEqual((pending.candidates, pending.rejected), ((ANA_REYES, ANA_SINGH), (ANA_LOPEZ,)))

    def test_a_reply_that_rejects_and_names_selects_the_named_one(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.say("give it to Ana")
        result = chat.say("no, Ana Reyes")
        self.assertEqual(result.kind, RouteKind.PROPOSE)
        self.assertEqual(result.proposal.fields["owner"], "ana-reyes")

    def test_a_rejection_that_fits_every_candidate_selects_nothing(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.say("give it to Ana")
        before = chat.memory.pending_clarification.candidates
        result = chat.say("not Ana")
        self.assertEqual(result.kind, RouteKind.CLARIFY)
        self.assertEqual(chat.memory.pending_clarification.candidates, before)

    def test_a_correction_after_a_list_asks_again_without_removing_anything(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.say("give it to Ana")
        before = chat.memory.pending_clarification.candidates
        result = chat.say("no")
        self.assertEqual((result.kind, result.response_key), (RouteKind.CLARIFY, "clarify_assign"))
        self.assertEqual(chat.memory.pending_clarification.candidates, before)
        self.assertEqual(chat.memory.pending_clarification.rejected, ())

    def test_a_refusal_discards_the_pending_question_and_resolves_nothing(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.say("give it to Ana")
        refused = chat.say("no, delete everything instead")
        self.assertEqual((refused.kind, refused.topic), (RouteKind.REFUSE, "destructive_change"))
        self.assertIsNone(refused.proposal)
        self.assertIsNone(chat.memory.pending_clarification)
        after = chat.say("the second one")
        self.assertEqual(after.kind, RouteKind.FALLBACK, "the old question is gone")

    def test_a_new_request_replaces_the_pending_question(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.say("give it to Ana")
        result = chat.say("show me the contacts")
        self.assertEqual(result.proposal.action_key, "open_contacts")
        self.assertIsNone(chat.memory.pending_clarification)

    def test_an_unrelated_answer_repeats_the_question_once_then_expires(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.say("give it to Ana")
        self.assertEqual(chat.say("hmm").kind, RouteKind.CLARIFY)
        self.assertEqual(chat.say("hmm").kind, RouteKind.FALLBACK)
        self.assertIsNone(chat.memory.pending_clarification)

    def test_a_skipped_turn_expires_the_question(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.say("give it to Ana")
        chat.turn += 1  # the client sends the next message two turns later
        result = chat.say("Ana Reyes")
        self.assertNotEqual(result.kind, RouteKind.PROPOSE)

    def test_a_missing_record_is_asked_for(self):
        result = self.chat().say("give it to Ben")
        self.assertEqual((result.kind, result.response_key), (RouteKind.CLARIFY, "clarify_update_target"))


class AmbiguityQuestionTest(RouterFixture):
    def test_the_question_matches_the_action(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        assignment = chat.say("give it to Ana")
        self.assertEqual(assignment.response_key, "clarify_assign")
        listing = self.chat().say("contacts for Ana")
        self.assertEqual(listing.response_key, "clarify_person", "a listing is not an assignment")

    def test_a_slot_question_does_not_depend_on_the_definition_wording_it(self):
        """Slot questions are platform wording (the 4b response boundary), so a definition that
        declares none still gets the question, and the router still does not act on a guess."""
        document = engine_definition()
        del document["responses"]["clarify_person"]
        router = IntentRouter(load_engine_definition(document=document), self.lookup)
        result = Conversation(router).say("contacts for Ana")
        self.assertEqual((result.kind, result.response_key, result.proposal),
                         (RouteKind.CLARIFY, "clarify_person", None))


class CorrectionTest(RouterFixture):
    def singled_out_chat(self, candidates, singled_out) -> Conversation:
        """A conversation whose previous reply named one candidate (as a slice 4 reply would)."""
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.memory = ConversationMemory(
            focus=CON_2, turn=chat.turn,
            pending_clarification=PendingClarification(
                "clarify_assign", "reassign_contact", "choice", candidates=candidates,
                singled_out=singled_out, target=CON_2, turn=chat.turn,
            ),
        )
        return chat

    def test_correction_phrases_inside_a_reply_are_understood(self):
        for phrase in ("no, the other one", "not that one please", "hmm, wrong one"):
            with self.subTest(phrase=phrase):
                chat = self.singled_out_chat((ANA_LOPEZ, ANA_REYES, ANA_SINGH), ANA_LOPEZ)
                result = chat.say(phrase)
                self.assertEqual(result.kind, RouteKind.CLARIFY)
                pending = chat.memory.pending_clarification
                self.assertEqual((pending.candidates, pending.rejected), ((ANA_REYES, ANA_SINGH), (ANA_LOPEZ,)))

    def test_not_that_one_removes_only_the_named_candidate_and_asks_again(self):
        chat = self.singled_out_chat((ANA_LOPEZ, ANA_REYES, ANA_SINGH), ANA_LOPEZ)
        result = chat.say("not that one")
        self.assertEqual((result.kind, result.proposal), (RouteKind.CLARIFY, None))
        pending = chat.memory.pending_clarification
        self.assertEqual((pending.candidates, pending.rejected), ((ANA_REYES, ANA_SINGH), (ANA_LOPEZ,)))
        self.assertIsNone(pending.singled_out, "nothing is picked on the visitor's behalf")

    def test_a_correction_leaving_one_candidate_asks_for_confirmation(self):
        chat = self.singled_out_chat((ANA_LOPEZ, ANA_REYES), ANA_LOPEZ)
        spec = self.router._definition.actions["reassign_contact"]
        self.assertFalse(spec.confirm, "the definition does not ask for confirmation")
        result = chat.say("the other one")
        self.assertEqual((result.kind, result.confirmation_reason), (RouteKind.CONFIRM, ConfirmationReason.CORRECTION))
        self.assertEqual(result.proposal.fields["owner"], "ana-reyes")
        self.assertIsNotNone(chat.memory.pending_confirmation, "held until the visitor says yes")
        confirmed = chat.say("yes")
        self.assertEqual((confirmed.kind, confirmed.confirmed), (RouteKind.PROPOSE, True))
        self.assertEqual(confirmed.proposal.fields["owner"], "ana-reyes")

    def test_a_confirmation_from_a_correction_can_be_declined(self):
        chat = self.singled_out_chat((ANA_LOPEZ, ANA_REYES), ANA_LOPEZ)
        chat.say("wrong one")
        result = chat.say("no")
        self.assertEqual((result.kind, result.response_key), (RouteKind.CANCELLED, "action_cancelled"))
        self.assertIsNone(chat.memory.pending_confirmation)

    def test_hidden_candidates_are_dropped_before_a_correction_decides(self):
        hidden = RecordRef("agent", "ana-kim")
        chat = self.singled_out_chat((ANA_LOPEZ, hidden), ANA_LOPEZ)
        result = chat.say("not that one")
        self.assertEqual(result.kind, RouteKind.CLARIFY)
        self.assertEqual(chat.memory.pending_clarification.candidates, ())


class ConfirmationTest(RouterFixture):
    def ask_to_close(self) -> Conversation:
        chat = self.chat()
        self.open_bens_contact(chat)
        result = chat.say("set the status to closed")
        self.assertEqual((result.kind, result.confirmation_reason), (RouteKind.CONFIRM, ConfirmationReason.DEFINITION))
        self.assertEqual((result.proposal.target, dict(result.proposal.fields)), (CON_2, {"status": "Closed"}))
        return chat

    def test_only_an_explicit_yes_confirms(self):
        chat = self.ask_to_close()
        result = chat.say("yes please")
        self.assertEqual((result.kind, result.confirmed, result.stage),
                         (RouteKind.PROPOSE, True, RouteStage.PENDING_CONFIRMATION))
        self.assertIsNone(chat.memory.pending_confirmation)

    def test_anything_else_cancels(self):
        for message in ("sure thing maybe", "ok", "hmm"):
            with self.subTest(message=message):
                chat = self.ask_to_close()
                result = chat.say(message)
                self.assertEqual(result.kind, RouteKind.CANCELLED)
                self.assertIsNone(chat.memory.pending_confirmation)

    def test_a_new_request_cancels_and_is_routed(self):
        chat = self.ask_to_close()
        result = chat.say("show me the contacts")
        self.assertEqual((result.proposal.action_key, result.cancelled_confirmation), ("open_contacts", True))

    def test_a_refusal_cancels_without_confirming(self):
        chat = self.ask_to_close()
        result = chat.say("yes, delete it")
        self.assertEqual(result.kind, RouteKind.REFUSE)
        self.assertIsNone(chat.memory.pending_confirmation)
        self.assertNotEqual(chat.say("yes").kind, RouteKind.PROPOSE)

    def test_an_unanswered_confirmation_expires(self):
        chat = self.ask_to_close()
        chat.turn += 1
        self.assertNotEqual(chat.say("yes").kind, RouteKind.PROPOSE)

    def test_a_confirmation_needs_an_accepted_proposal(self):
        chat = self.chat()
        self.open_bens_contact(chat)
        chat.say("set the status to closed", assume_validated=False)
        self.assertIsNone(chat.memory.pending_confirmation, "the router alone never stores a confirmation")
        self.assertNotEqual(chat.say("yes").kind, RouteKind.PROPOSE)


class VisibilityTest(RouterFixture):
    def test_unknown_and_hidden_people_give_identical_results(self):
        def outcome(name):
            chat = self.chat()
            self.open_bens_contact(chat)
            result = chat.say(f"give it to {name}")
            return result.kind, result.stage, result.response_key, result.proposal, dict(result.placeholders)

        hidden = outcome("Cara")
        unknown = outcome("Priya")
        self.assertEqual(hidden[:4], unknown[:4])
        self.assertEqual((hidden[2], hidden[4], unknown[4]),
                         ("unknown_person", {"person": "Cara"}, {"person": "Priya"}))

    def test_hidden_records_cannot_be_targeted_by_id_or_memory(self):
        chat = self.chat()
        chat.memory = ConversationMemory(focus=RecordRef("contact", "CON-3"))
        result = chat.say("set the status to closed")
        self.assertEqual((result.kind, result.response_key), (RouteKind.CLARIFY, "clarify_update_target"))
        self.assertEqual(self.chat().say("set CON-3 status to closed").kind, RouteKind.CLARIFY)

    def test_the_router_can_only_use_the_scoped_lookup_interface(self):
        """The router cannot reach past the protocol, so it cannot widen the caller's scope."""

        class Fenced:
            """Allows the five protocol methods and records what was asked for."""

            def __init__(self, inner):
                self._inner = inner
                self.asked: list[str] = []

            def __getattr__(self, name):
                if name not in {"get", "search", "by_person", "people", "count"}:
                    raise AssertionError(f"the router reached for {name!r} outside the lookup interface")
                self.asked.append(name)
                return getattr(self._inner, name)

        fenced = Fenced(InMemoryLookup(SampleDesk(), frozenset({"ACC-1"})))
        chat = Conversation(IntentRouter(load_engine_definition(), fenced))
        chat.say("contacts for Cara")
        chat.say("open Ben's contact")
        chat.say("give it to Ana")
        self.assertTrue({"people", "get"} <= set(fenced.asked))
        # Every call carries only an entity, a text and a limit: there is no scope argument to widen.
        for name in set(fenced.asked):
            with self.subTest(method=name):
                self.assertNotIn("scope", getattr(fenced, name).__code__.co_varnames)

    def test_guardrails_cannot_grant_access(self):
        without_guardrails = engine_definition()
        without_guardrails["guardrails"] = []
        router = IntentRouter(load_engine_definition(document=without_guardrails), self.lookup)
        chat = Conversation(router)
        chat.memory = ConversationMemory(focus=RecordRef("contact", "CON-3"))
        result = chat.say("set the status to open")
        self.assertIsNone(result.proposal, "removing refusals never exposes hidden records")
        for rule_result in (Conversation(self.router).say("delete the contact"),):
            self.assertEqual((rule_result.kind, rule_result.proposal), (RouteKind.REFUSE, None))


class PersonFollowUpTest(RouterFixture):
    """"What about <person>" on the next turn re-applies the last request (5a plan, section 4.5)."""

    def test_a_list_view_is_followed_by_its_person_filter(self):
        chat = self.chat()
        chat.say("show me the contacts")
        result = chat.say("what about Ben")
        self.assertEqual((result.kind, result.proposal.action_key, result.proposal.filter.value),
                         (RouteKind.PROPOSE, "contacts_by_owner", "ben-okafor"))

    def test_a_message_that_merely_contains_a_name_is_not_a_follow_up(self):
        """Right after a person request, "what's the weather in Paris?" was answered as if Paris
        were someone to look up, and got "I can't find Paris"."""
        for message in ("what's the weather in Paris?", "tell me about Rome", "Zed"):
            with self.subTest(message=message):
                chat = self.chat()
                chat.say("show me the contacts")
                self.assertNotEqual(chat.say(message).response_key, "unknown_person")

    def test_follow_ups_can_be_phrased_several_ways(self):
        for message in ("what about Ben", "how about Ben?", "and Ben", "same for Ben", "Ben too"):
            with self.subTest(message=message):
                chat = self.chat()
                chat.say("show me the contacts")
                result = chat.say(message)
                self.assertEqual((result.kind, result.proposal.action_key),
                                 (RouteKind.PROPOSE, "contacts_by_owner"))

    def test_a_person_based_request_is_reapplied_to_the_new_person(self):
        chat = self.chat()
        chat.say("contacts for Cara")
        self.open_bens_contact(chat)
        chat.say("contacts for Ben")
        again = self.chat()
        again.say("open Ben's contact")
        self.assertEqual(again.say("what about Ben").proposal.action_key, "open_contact")

    def test_unknown_and_hidden_people_are_answered_alike(self):
        def outcome(name):
            chat = self.chat()
            chat.say("show me the contacts")
            result = chat.say(f"what about {name}")
            return result.kind, result.response_key, result.proposal, dict(result.placeholders)

        self.assertEqual(outcome("Cara"), (RouteKind.ANSWER, "unknown_person", None, {"person": "Cara"}))
        self.assertEqual(outcome("Priya"), (RouteKind.ANSWER, "unknown_person", None, {"person": "Priya"}))

    def test_the_follow_up_lasts_one_turn_only(self):
        chat = self.chat()
        chat.say("show me the contacts")
        chat.say("tell me a joke")
        self.assertEqual(chat.say("what about Ben").kind, RouteKind.FALLBACK)

    def test_a_refusal_ends_the_follow_up(self):
        chat = self.chat()
        chat.say("show me the contacts")
        self.assertEqual(chat.say("delete the contact").kind, RouteKind.REFUSE)
        self.assertEqual(chat.say("what about Ben").kind, RouteKind.FALLBACK)

    def test_an_unaccepted_proposal_starts_no_follow_up(self):
        chat = self.chat()
        chat.say("show me the contacts", assume_validated=False)
        self.assertEqual(chat.say("what about Ben").kind, RouteKind.FALLBACK)

    def test_two_people_are_never_guessed_between(self):
        chat = self.chat()
        chat.say("show me the contacts")
        self.assertEqual(chat.say("what about Ben and Cara").kind, RouteKind.FALLBACK)

    def test_two_person_filters_on_one_entity_are_never_guessed_between(self):
        document = engine_definition()
        document["actions"]["contacts_by_owner_too"] = dict(document["actions"]["contacts_by_owner"])
        document["intents"].append({"action": "contacts_by_owner_too", "requires": ["person"],
                                    "match": [["managed by"], ["contacts"]], "response": "records_filtered"})
        chat = Conversation(IntentRouter(load_engine_definition(document=document), self.lookup))
        chat.say("show me the contacts")
        self.assertEqual(chat.say("what about Ben").kind, RouteKind.FALLBACK)


class SyntheticDefinitionTest(unittest.TestCase):
    """Changing the definition changes behaviour, with no core edits."""

    def router(self, document) -> IntentRouter:
        lookup = InMemoryLookup(SampleDesk(), frozenset({"ACC-1"}))
        return IntentRouter(load_engine_definition(document=document), lookup)

    def test_vocabulary_intents_actions_and_responses_drive_routing(self):
        base = engine_definition()
        self.assertEqual(Conversation(self.router(base)).say("show the rolodex").kind, RouteKind.FALLBACK)

        changed = copy.deepcopy(base)
        changed["vocabulary"]["corrections"]["rolodex"] = "contacts"
        self.assertEqual(Conversation(self.router(changed)).say("show the rolodex").proposal.action_key, "open_contacts")

        renamed = copy.deepcopy(base)
        renamed["intents"][0]["match"] = [["people list"]]
        self.assertEqual(Conversation(self.router(renamed)).say("show the people list").proposal.action_key,
                         "open_contacts")

        retargeted = copy.deepcopy(base)
        retargeted["intents"][0]["action"] = "highlight_mail"
        result = Conversation(self.router(retargeted)).say("contacts")
        self.assertEqual(result.proposal.capability, Capability.HIGHLIGHT_CONTROL)

        reworded = copy.deepcopy(base)
        reworded["intents"][0]["response"] = "record_opened"
        self.assertEqual(Conversation(self.router(reworded)).say("contacts").response_key, "record_opened")

    def test_two_definitions_route_independently_in_one_process(self):
        first = self.router(engine_definition())
        other = engine_definition()
        other["intents"] = [{"action": "highlight_mail", "match": [["contacts"]]}]
        second = self.router(other)
        self.assertEqual(Conversation(first).say("contacts").proposal.action_key, "open_contacts")
        self.assertEqual(Conversation(second).say("contacts").proposal.action_key, "highlight_mail")


class ReproducedDefectTest(RouterFixture):
    """The stakeholder's reproductions from the slice 2 and 3 review, one test each.

    Each of these proposed the wrong thing before the fix, so each fails on the old behaviour.
    """

    def route(self, message: str, *, memory: ConversationMemory | None = None,
              selected: RecordRef | None = None, document: dict | None = None, turn: int = 1):
        router = IntentRouter(load_engine_definition(document=document), self.lookup) if document else self.router
        return router.route(message, memory or ConversationMemory(),
                            TurnContext(turn=turn, selected=selected))

    def test_a_correction_decides_which_record_is_changed(self):
        """"status CON-1 Closed, actually status CON-2 Closed" is a request about CON-2."""
        result = self.route("status CON-1 Closed, actually status CON-2 Closed").result
        self.assertEqual(result.kind, RouteKind.CONFIRM)
        self.assertEqual(result.proposal.target, RecordRef("contact", "CON-2"))

    def test_the_first_mentioned_record_no_longer_wins_by_position(self):
        result = self.route("status CON-2 Closed, actually status CON-1 Closed").result
        self.assertEqual(result.proposal.target, RecordRef("contact", "CON-1"))

    def test_a_named_record_that_is_unavailable_is_never_swapped_for_the_remembered_one(self):
        memory = ConversationMemory(focus=RecordRef("contact", "CON-1"))
        result = self.route("status CON-999 Closed", memory=memory).result
        self.assertEqual(result.kind, RouteKind.CLARIFY)
        self.assertIsNone(result.proposal)

    def test_a_named_record_that_is_unavailable_is_never_swapped_for_the_selected_one(self):
        result = self.route("status CON-999 Closed", selected=RecordRef("contact", "CON-1")).result
        self.assertEqual(result.kind, RouteKind.CLARIFY)
        self.assertIsNone(result.proposal)

    def test_a_record_in_another_scope_is_treated_exactly_like_one_that_does_not_exist(self):
        """CON-3 exists, but not for this caller. Both must give the same answer."""
        memory = ConversationMemory(focus=RecordRef("contact", "CON-1"))
        hidden = self.route("status CON-3 Closed", memory=memory).result
        missing = self.route("status CON-999 Closed", memory=memory).result
        self.assertEqual((hidden.kind, hidden.response_key), (missing.kind, missing.response_key))
        self.assertIsNone(hidden.proposal)

    def test_naming_no_record_still_uses_the_remembered_one(self):
        """The fix must not break the ordinary follow-up: "close it" after opening CON-1."""
        memory = ConversationMemory(focus=RecordRef("contact", "CON-1"))
        result = self.route("status Closed", memory=memory).result
        self.assertEqual(result.proposal.target, RecordRef("contact", "CON-1"))

    def test_an_ambiguous_ordinal_answer_asks_again_instead_of_choosing(self):
        asked = self.route("reassign CON-1 to Ana")
        self.assertEqual(asked.result.kind, RouteKind.CLARIFY)
        answered = self.route("first or second", memory=asked.memory, turn=2).result
        self.assertEqual(answered.kind, RouteKind.CLARIFY, "two ordinals select nobody")
        self.assertIsNone(answered.proposal)

    def test_an_ambiguous_ordinal_answer_does_not_consume_the_question(self):
        asked = self.route("reassign CON-1 to Ana")
        answered = self.route("first or second", memory=asked.memory, turn=2)
        self.assertIsNotNone(answered.memory.pending_clarification, "the question must still stand")
        chosen = self.route("the second one", memory=answered.memory, turn=3).result
        self.assertEqual(chosen.kind, RouteKind.PROPOSE)
        self.assertEqual(dict(chosen.proposal.fields)["owner"], "ana-reyes")

    def test_a_single_ordinal_answer_still_selects(self):
        asked = self.route("reassign CON-1 to Ana")
        answered = self.route("the first one", memory=asked.memory, turn=2).result
        self.assertEqual(answered.kind, RouteKind.PROPOSE)
        self.assertEqual(dict(answered.proposal.fields)["owner"], "ana-lopez")

    def test_repeating_one_ordinal_is_not_ambiguous(self):
        asked = self.route("reassign CON-1 to Ana")
        answered = self.route("first, yes the first", memory=asked.memory, turn=2).result
        self.assertEqual(answered.kind, RouteKind.PROPOSE)

    def test_an_exact_clarification_beats_a_grouped_intent(self):
        document = engine_definition()
        document["clarifications"].append({"response": "clarify_create", "exact": ["show contacts"]})
        result = self.route("show contacts", document=document).result
        self.assertEqual(result.kind, RouteKind.CLARIFY)
        self.assertEqual(result.stage, RouteStage.EXACT_PHRASES)
        self.assertEqual(result.response_key, "clarify_create")

    def test_a_grouped_intent_still_wins_when_no_exact_rule_matches(self):
        document = engine_definition()
        document["clarifications"].append({"response": "clarify_create", "exact": ["show contacts"]})
        result = self.route("please show me the contacts", document=document).result
        self.assertEqual(result.kind, RouteKind.PROPOSE)
        self.assertEqual(result.proposal.action_key, "open_contacts")

    def test_two_competing_exact_rules_are_not_resolved_by_file_order(self):
        document = engine_definition()
        document["clarifications"].append({"response": "clarify_create", "exact": ["everything please"]})
        result = self.route("everything please", document=document).result
        self.assertEqual(result.kind, RouteKind.FALLBACK)

    def test_two_exact_clarifications_with_different_answers_fall_back(self):
        document = engine_definition()
        document["clarifications"].append({"response": "clarify_create", "exact": ["what now"]})
        document["clarifications"].append({"response": "clarify_update_target", "exact": ["what now"]})
        result = self.route("what now", document=document).result
        self.assertEqual(result.kind, RouteKind.FALLBACK)


if __name__ == "__main__":
    unittest.main()


class RecordNamedByTitleTest(RouterFixture):
    """People name what they made by what they called it, not by the identifier Pixel gave it."""

    def test_a_visible_record_opens_by_its_whole_title(self):
        for message in ("open Eli Moss", "show me eli moss", "Eli Moss"):
            with self.subTest(message=message):
                result = self.chat().say(message)
                self.assertEqual(result.kind, RouteKind.PROPOSE, result)
                self.assertEqual(result.proposal.target, RecordRef("contact", "CON-2"))

    def test_a_record_the_caller_cannot_see_is_never_opened(self):
        result = self.chat().say("open Fay Chu")
        self.assertTrue(result.proposal is None or result.proposal.target != RecordRef("contact", "CON-3"))

    def test_part_of_a_title_names_nothing(self):
        result = self.chat().say("open Moss")
        self.assertTrue(result.proposal is None or result.proposal.target is None)


class TitleDetailsTest(unittest.TestCase):
    """Choices named after a new record's title set those fields instead of joining the title."""

    def setUp(self):
        from app.definitions.contract import EntitySpec
        self.entity = EntitySpec.model_validate({
            "label": "Case", "plural": "Cases", "id": {"strategy": "prefix", "prefix": "CASE"}, "title_field": "title",
            "fields": {
                "title": {"type": "text", "label": "Title"},
                "priority": {"type": "enum", "label": "Priority", "values": ["Low", "High"]},
                "status": {"type": "enum", "label": "Status", "values": ["New", "In progress", "Done"]},
            },
        })

    def split(self, message: str):
        from app.engine.mentions import title_and_details
        return title_and_details(message, self.entity, ["title", "priority", "status"])

    def test_a_trailing_choice_is_a_detail(self):
        self.assertEqual(self.split("create a case called Checkout crash with high priority"),
                         ("Checkout crash", {"priority": ["High"]}))
        self.assertEqual(self.split("add a case called Printer jam, high priority and status in progress"),
                         ("Printer jam", {"priority": ["High"], "status": ["In progress"]}))

    def test_a_title_that_only_mentions_a_word_keeps_it(self):
        self.assertEqual(self.split("create a case called Coffee with the new team"),
                         ("Coffee with the new team", {}))
        self.assertEqual(self.split("create a case about login errors"), ("Login errors", {}))

    def test_a_choice_before_the_title_counts_too(self):
        self.assertEqual(self.split("create a high priority case called Outage"),
                         ("Outage", {"priority": ["High"]}))
