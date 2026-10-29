"""Phrasing variety: per-draft variation, stance stripping, the team repetition gate and its wiring."""
import json
import time
import unittest

from ctn import phrasing
from ctn.llm.models import DraftOptions
from ctn.llm.prompts import build_messages
from ctn.persona import get_default_registry
from ctn.storage import StateStore
from tests.test_persona_voices import make_sample_material


def sent(store, ident, text, purpose='P03', at=None):
    ts = time.time() if at is None else at
    with store.batch_transaction():
        store._conn.execute("INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,created_at,updated_at,metadata_json) "
                            "VALUES(?,?,'reply','a','t',?,'succeeded','',0,?,?,?)", (ident, ident, text, ts, ts, json.dumps({'purpose': purpose})))


class VariationTests(unittest.TestCase):
    def test_stable_per_seed_and_vote_declared_rarely(self):
        self.assertEqual(phrasing.variation('p1:yes_001:P03'), phrasing.variation('p1:yes_001:P03'))
        rolls = [phrasing.variation(f'p{i}:yes_001:P03') for i in range(1000)]
        self.assertTrue(0.10 < sum(v['allow_stance'] for v in rolls) / 1000 < 0.20)
        self.assertEqual({v['opening'] for v in rolls}, set(phrasing.POLICY['opening_styles']))

    def test_stance_sentences_are_removed_from_style_examples(self):
        text = "I'm voting YES. I also want an answer for people facing travel barriers."
        self.assertEqual(phrasing.strip_stance(text), 'I also want an answer for people facing travel barriers.')
        self.assertEqual(phrasing.strip_stance('Shared places, shared responsibilities.'), 'Shared places, shared responsibilities.')


class GateTests(unittest.TestCase):
    def test_stock_openings_are_always_rejected(self):
        self.assertIn('fair point', phrasing.repetition_reason('Fair point about the ferry ramp, it is tight.', []))
        self.assertIn("you're right", phrasing.repetition_reason('You’re right that the bus is crowded.', []))
        # Whole words only: "I hear your concern" is not the stock "I hear you".
        self.assertEqual(phrasing.repetition_reason('I hear your concern about the clinic queue.', []), '')

    def test_repeated_opening_and_closing_across_the_team(self):
        recent = ['The ferry ramp at six is packed every day.', 'The ferry ramp gets busy after school too.']
        self.assertIn('opened with "the ferry ramp"', phrasing.repetition_reason('The ferry ramp needs a plan.', recent))
        ends = ['Our clinic has the same queue, and that is why it matters to me.',
                'Shops on my street close early now, and that is why it matters to me.']
        self.assertIn('ended with', phrasing.repetition_reason('My mum waits an hour for the bus, so that is why it matters to me.', ends))

    def test_stock_phrase_only_when_the_team_used_it_recently(self):
        draft = "Our depot lost three drivers last winter. I'm voting YES because of it."
        self.assertEqual(phrasing.repetition_reason(draft, ['Something else entirely.']), '')
        self.assertIn("i'm voting yes", phrasing.repetition_reason(draft, ["Clinics fill fast. I'm voting YES."]))

    def test_ordinary_draft_passes(self):
        self.assertEqual(phrasing.repetition_reason('My nan takes the 7am bus to dialysis twice a week.', ['A different post.']), '')

    def test_recent_texts_skip_first_posts_and_stats(self):
        store = StateStore(); self.addCleanup(store.close)
        sent(store, 'a', 'Fair point, the queue is long.', at=1)
        sent(store, 'b', "I'm voting YES. Clinics are small.", at=2)
        sent(store, 'c', 'Morning fog on the harbour.', purpose='initialise', at=3)
        self.assertEqual(phrasing.recent_texts(store), ["I'm voting YES. Clinics are small.", 'Fair point, the queue is long.'])
        s = phrasing.stats(store)
        self.assertEqual((s['sample'], s['stock_opening_share'], s['vote_declaration_share']), (2, 0.5, 0.5))


class PromptTests(unittest.TestCase):
    def test_reply_prompt_carries_a_variation_and_stripped_style(self):
        persona = get_default_registry().list_by_side('yes')[0]
        rag = {'corpus_hash': 'h', 'corpus_version': 'v', 'retrieval_version': 'r',
               'styles': [{'text': "I'm voting YES. Shared places need shared rules.", 'purposes': ['P08']}]}
        messages = build_messages(make_sample_material('P08'), DraftOptions(language='follow_post', max_reply_chars=280),
                                  max_chars=280, persona=persona, rag=rag)
        self.assertIn('Phrasing:', messages[0]['content'])
        self.assertIn('Never open with a stock acknowledgement', messages[0]['content'])
        style = json.loads(messages[1]['content'])['style_reference'][0]['text']
        self.assertEqual(style, 'Shared places need shared rules.')


class OveruseTests(unittest.TestCase):
    """2026-10-27: 5,877 replies in 3 days converged on a few constructions; cap their share of the window."""

    def test_phrase_capped_once_the_team_leans_on_it(self):
        recent = ["I'd want the list first." if i < 9 else f'Post {i} about the ferry.' for i in range(40)]
        self.assertIn("overused_phrase", phrasing.overuse_reason("I'd want the venue list in writing.", recent))
        self.assertEqual(phrasing.overuse_reason('The ferry queue at six is the whole story.', recent), '')

    def test_occasional_use_is_fine(self):
        recent = ["I'd want it spelled out."] + [f'Post {i} about the depot.' for i in range(39)]
        self.assertEqual(phrasing.overuse_reason("I'd want the list in writing.", recent), '')

    def test_opening_pattern_capped(self):
        recent = ['What would the list cover?', 'What would a list change?'] + [f'Post {i} about the bus.' for i in range(13)]
        self.assertIn('overused_opening', phrasing.overuse_reason('What would the list look like?', recent))
        self.assertEqual(phrasing.overuse_reason('My nan queues at six for dialysis.', recent), '')

    def test_gate_rejects_through_repetition_reason(self):
        recent = ['who checks at the gate' + f' in post {i}' for i in range(10)] + [f'Post {i} about the ferry.' for i in range(10)]
        self.assertIn('overused_phrase', phrasing.repetition_reason('Fine in principle, but who checks at the gate?', recent))

    def test_prompt_tells_the_model_what_to_avoid(self):
        text = phrasing.instructions('seed')
        self.assertIn('avoid them', text)
        self.assertIn('who checks', text)

    def test_stats_report_the_capped_shares(self):
        store = StateStore(); self.addCleanup(store.close)
        for i in range(6):
            sent(store, f's{i}', f"Sentence {i}. I'd want the list in writing.", at=i + 1)
        stats = phrasing.stats(store, n=10)
        self.assertIn("i'd want", stats['overused'])
        self.assertGreaterEqual(stats['overused']["i'd want"], 0.5)


class UnansweredDoubtTests(unittest.TestCase):
    """2026-10-29: towards-neither outran towards-YES 29:25, mostly replies that asked back instead of answering."""

    def test_a_restated_doubt_gets_one_reason_clause(self):
        target = 'Fine, but what about the family that cannot get the paperwork in time?'
        self.assertTrue(phrasing.unanswered_doubt(target, 'Would you accept a paper route at the door?'))
        self.assertEqual(
            phrasing.unanswered_doubt(target, 'Because the paper route exists on the same day, the queue is the barrier.'),
            '')

    def test_reason_clause_is_recognised_in_its_common_shapes(self):
        target = 'Still not convinced.'
        for reply in ('Which is why the list has to be published first.',
                      "That's why the paper option matters.",
                      'It comes down to who checks the code at the door.'):
            self.assertEqual(phrasing.unanswered_doubt(target, reply), '', reply)

    def test_posts_without_a_doubt_are_left_alone(self):
        self.assertEqual(phrasing.unanswered_doubt('The hospital numbers were bad last time.', 'What do you think?'), '')
        self.assertEqual(phrasing.unanswered_doubt('', ''), '')

    def test_a_plain_statement_is_not_treated_as_a_doubt(self):
        self.assertEqual(phrasing.unanswered_doubt('I voted yes last time too.', 'Same here, that is why I am a yes again.'), '')


if __name__ == '__main__':
    unittest.main()


class LengthLimitTests(unittest.TestCase):
    def test_prompts_aim_for_280_but_only_500_is_a_hard_stop(self):
        from ctn.config import PLATFORM_MAX_CHARS, LLMConfig
        from ctn.jev.postflight import validate_text_constraints
        from ctn.storage import StateStore
        self.assertEqual((PLATFORM_MAX_CHARS, LLMConfig.max_reply_chars), (500, 280))
        text = 'Our ferry ramp gets crowded at six. ' * 11  # ~400 characters
        self.assertTrue(280 < len(text.strip()) <= 500)
        self.assertTrue(validate_text_constraints(text.strip())[0])
        self.assertFalse(validate_text_constraints('x' * 501)[0])
        store = StateStore(); self.addCleanup(store.close)
        self.assertEqual(store.publication_gate(text.strip(), {})[1], 'ready')
        self.assertEqual(store.publication_gate('y' * 501, {})[1], 'text_constraint')
