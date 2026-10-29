"""`ops post send` builds the metadata the persona gate requires."""

import unittest

from ctn.ops import post_plan


class FakePersona:
    persona_id = 'yes_051'
    account_id = 'smallshop'
    version = 3


class FakeRegistry:
    catalog_hash = 'abc123'
    def get(self, identifier):
        return FakePersona() if identifier in ('yes_051', 'smallshop') else None


class PostPlanTests(unittest.TestCase):
    def test_carries_catalog_persona_and_version(self):
        plan = post_plan(FakeRegistry(), 'yes_051', '  a question about the queue  ')
        self.assertEqual(plan['account'], 'smallshop')
        self.assertEqual(plan['text'], 'a question about the queue')
        meta = plan['metadata']
        self.assertEqual(meta['persona_id'], 'yes_051')
        self.assertEqual(meta['persona_version'], 3)
        self.assertEqual(meta['catalog_hash'], 'abc123')
        self.assertEqual(meta['purpose'], 'P01')
        self.assertEqual(meta['engine'], 'operator')

    def test_unknown_persona_is_rejected(self):
        with self.assertRaises(ValueError):
            post_plan(FakeRegistry(), 'yes_999', 'hello')

    def test_empty_text_is_rejected(self):
        with self.assertRaises(ValueError):
            post_plan(FakeRegistry(), 'yes_051', '   ')

    def test_decision_id_is_stable_when_given(self):
        plan = post_plan(FakeRegistry(), 'yes_051', 'x', decision_id='ops-20261029-2000')
        self.assertEqual(plan['decision_id'], 'ops-20261029-2000')


if __name__ == '__main__':
    unittest.main()
