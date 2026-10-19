"""Hashtags for originals: fixed tags per purpose, stable per slot, appended by code, ignored by the phrasing gate."""
import collections
import unittest

from ctn import hashtags, phrasing


class AssignTests(unittest.TestCase):
    def test_p01_mostly_carries_our_tag_and_often_pairs_it_with_theirs(self):
        picks = [tuple(hashtags.assign(f'macro:live:2026-10-20:12:{i}', 'P01')) for i in range(2000)]
        tagged = [p for p in picks if p]
        self.assertTrue(0.85 < len(tagged) / 2000 < 0.95)
        counts = collections.Counter(tagged)
        self.assertGreater(counts[('#ProofKeepsUsOpen', '#KeepKingstoniaOpen')], counts[('#ProofNotLockdown',)])
        self.assertTrue(all(len(p) <= 2 for p in tagged))
        self.assertEqual(hashtags.assign('slot-1', 'P01'), hashtags.assign('slot-1', 'P01'))

    def test_neutral_questions_get_only_shared_tags(self):
        tags = {t for i in range(500) for t in hashtags.assign(f's{i}', 'P00')}
        self.assertEqual(tags, {'#KeepKingstoniaOpen', '#h2d2'})
        self.assertEqual(hashtags.assign('s', 'P07'), [])

    def test_model_hashtags_are_removed(self):
        self.assertEqual(hashtags.strip('Clinics stay open when we plan ahead. #VoteYes #h2d2'), 'Clinics stay open when we plan ahead.')
        self.assertEqual(hashtags.strip('No tags here.'), 'No tags here.')


class GateTests(unittest.TestCase):
    def test_shared_tags_are_not_a_repeated_ending(self):
        recent = ['Buses kept running last winter. #ProofKeepsUsOpen #KeepKingstoniaOpen',
                  'My stall needs the market open. #ProofKeepsUsOpen #KeepKingstoniaOpen']
        draft = 'Our school gate queue is short anyway. #ProofKeepsUsOpen #KeepKingstoniaOpen'
        self.assertEqual(phrasing.repetition_reason(draft, recent), '')


if __name__ == '__main__':
    unittest.main()
