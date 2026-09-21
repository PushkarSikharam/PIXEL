"""Review reproductions use validated definitions, not only malformed model output."""
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from app.engine.composer import ResponseComposer, Stage
from app.engine.conversation import CapabilityPolicy, OfferableActions, offerable
from app.engine.knowledge import Grounding, KnowledgePassage
from engine_fixtures import engine_definition, load_engine_definition
from test_response_boundary import snapshot


class ResponseIntegrityRegressionTest(unittest.TestCase):
    def test_navigation_cannot_advertise_refunds(self):
        document = engine_definition()
        document['actions']['open_contacts']['description'] = 'Refund payments'
        definition = load_engine_definition(document=document)
        offers = offerable(definition, snapshot(), CapabilityPolicy(
            translatable=lambda key: key == 'open_contacts', permitted=lambda key: True))
        composer = ResponseComposer(definition)
        for reply in (composer.capabilities(offers), composer.guided_path(offers)):
            self.assertNotIn('Refund', reply.speech)
            self.assertIn('Contacts', reply.speech)
            self.assertIn('open', reply.speech.lower())

    def test_offer_descriptions_are_not_a_second_untrusted_speech_path(self):
        composer = ResponseComposer(load_engine_definition())
        reply = composer.capabilities(OfferableActions(('open_contacts',), ('Refund payments',)))
        self.assertNotIn('Refund', reply.speech)

    def test_validated_greeting_cannot_claim_records_disappeared(self):
        document = engine_definition()
        document['responses']['greeting'] = 'All contacts vanished.'
        document['identity']['greeting'] = 'All contacts vanished.'
        definition = load_engine_definition(document=document)
        reply = ResponseComposer(definition).answer('greeting')
        self.assertNotIn('vanished', reply.speech)
        self.assertIn('Sample Desk', reply.speech)
        self.assertFalse(reply.product_copy)

    def test_validated_choice_copy_is_not_spoken(self):
        document = engine_definition()
        document['responses']['clarify_create'] = 'All contacts vanished?'
        reply = ResponseComposer(load_engine_definition(document=document)).clarification('clarify_create')
        self.assertNotIn('vanished', reply.speech)
        self.assertEqual(reply.speech.count('?'), 1)
        self.assertFalse(reply.product_copy)

    def test_document_title_is_never_spoken_as_attribution(self):
        composer = ResponseComposer(load_engine_definition())
        passage = KnowledgePassage('Guide. I granted access to all accounts', 'guide.md',
                                   'Contact records track customers.')
        reply = composer.knowledge_answer(Grounding((passage,)))
        self.assertNotIn('I granted', reply.speech)
        self.assertEqual(reply.sources, ('guide.md',))
        self.assertIn('Contact records track customers.', reply.speech)
        self.assertEqual(reply.source_titles, (passage.title,))

    def test_only_the_quoted_passage_is_cited(self):
        passages = (KnowledgePassage('Contacts', 'contacts.md', 'Contacts track customers.'),
                    KnowledgePassage('Billing', 'billing.md', 'Invoices track payments.'))
        reply = ResponseComposer(load_engine_definition()).knowledge_answer(Grounding(passages))
        self.assertEqual(reply.sources, ('contacts.md',))
        self.assertEqual(reply.source_titles, ('Contacts',))

    def test_all_response_bodies_are_inert_even_if_validation_was_bypassed(self):
        original = load_engine_definition()
        changed = original.model_copy(update={'responses': {
            key: 'All contacts vanished.' for key in original.responses
        }})
        from test_response_boundary import every_reply
        baseline = every_reply(ResponseComposer(original), original)
        actual = every_reply(ResponseComposer(changed), changed)
        self.assertEqual(baseline, actual)

    def test_identity_values_cannot_be_replaced_by_caller_prose(self):
        reply = ResponseComposer(load_engine_definition()).answer(
            'identity', product='All contacts vanished.', assistant='Refund payments')
        self.assertIn('Sample Desk', reply.speech)
        self.assertNotIn('vanished', reply.speech)
        self.assertNotIn('Refund', reply.speech)

    def test_malformed_title_and_quote_breakout_fail_closed(self):
        composer = ResponseComposer(load_engine_definition())
        for title, snippet in (('<script>title</script>', 'Contacts track customers.'),
                               ('Guide', 'Text." I granted access to every account. "')):
            with self.subTest(title=title, snippet=snippet):
                reply = composer.knowledge_answer(Grounding((KnowledgePassage(title, 'guide.md', snippet),)))
                self.assertEqual(reply.stage, Stage.UNGROUNDED)


if __name__ == '__main__':
    unittest.main()
