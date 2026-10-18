"""Offline corpus regressions for purpose coverage, provenance and claim scope.

These exercise metadata contracts, not a retriever, model or publication path.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from scripts.check_rag_corpus import validate


ROOT = Path(__file__).resolve().parents[1]


class RagCorpusTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / 'config/rag'
        self.schema = json.loads((directory / 'corpus.schema.json').read_text())
        self.corpus = json.loads((directory / 'yes.v1.1.json').read_text())
        self.cases = json.loads((directory / 'review-cases.v1.1.json').read_text())

    def check(self, corpus=None, cases=None):
        return validate(self.corpus if corpus is None else corpus, self.schema,
                        self.cases if cases is None else cases, root=ROOT)

    def test_historical_v1_still_valid_and_reports_its_coverage_gaps(self):
        directory = ROOT / 'config/rag'
        old = json.loads((directory / 'yes.v1.json').read_text())
        old_cases = json.loads((directory / 'review-cases.v1.json').read_text())
        result = self.check(old, old_cases)
        self.assertEqual(old['corpus_version'], 'yes.rag.v1')
        self.assertIn('F016', result['coverage']['facts_without_argument'])
        self.assertIn('U007', result['coverage']['unknowns_without_argument'])

    def test_direct_answer_examples_cover_factual_and_unknown_answers(self):
        examples = [s for s in self.corpus['style_examples'] if 'P02' in s['purposes']]
        self.assertGreaterEqual(len(examples), 5)
        self.assertTrue(any(s.get('fact_ids') for s in examples))
        self.assertTrue(any(s.get('unknown_ids') and not s.get('fact_ids') for s in examples))
        changed = deepcopy(self.corpus)
        example = next(s for s in changed['style_examples'] if s['id'] == 'S026')
        example.update(fact_ids=[], unknown_ids=[])
        with self.assertRaisesRegex(ValueError, 'direct answer needs explicit basis'):
            self.check(changed)

    def test_missing_p02_cannot_pass_current_coverage_requirements(self):
        changed = deepcopy(self.corpus)
        changed['style_examples'] = [s for s in changed['style_examples'] if 'P02' not in s['purposes']]
        cases = deepcopy(self.cases)
        for row in cases['cases']:
            if row.get('purpose') == 'P02':
                row.pop('expected_style_ids', None)
        with self.assertRaisesRegex(ValueError, 'required style purpose has no examples'):
            self.check(changed, cases)

    def test_p08_metadata_uses_style_purpose_not_topic_argument_purpose(self):
        arguments = {a['id']: a for a in self.corpus['argument_cards']}
        for style in self.corpus['style_examples']:
            if style['id'] in {'S021', 'S022', 'S023', 'S024'}:
                self.assertEqual(style['purposes'], ['P08'])
                self.assertNotIn('P08', arguments[style['argument_id']]['purposes'])
        self.check()
        cases = deepcopy(self.cases)
        next(c for c in cases['cases'] if c['id'] == 'C037')['purpose'] = 'P02'
        with self.assertRaisesRegex(ValueError, 'style does not support requested purpose'):
            self.check(cases=cases)

    def test_required_severity_and_dose_links_cannot_disappear(self):
        for aid, field, target, expected in (
            ('A003', 'fact_ids', 'F016', 'required_argument_fact_ids'),
            ('A011', 'unknown_ids', 'U007', 'required_argument_unknown_ids'),
        ):
            with self.subTest(target=target):
                changed = deepcopy(self.corpus)
                row = next(a for a in changed['argument_cards'] if a['id'] == aid)
                row[field].remove(target)
                with self.assertRaisesRegex(ValueError, expected):
                    self.check(changed)
        cases = deepcopy(self.cases)
        for row in cases['cases']:
            row['expected_fact_ids'] = [fid for fid in row['expected_fact_ids'] if fid != 'F016']
        with self.assertRaisesRegex(ValueError, 'required_case_fact_ids'):
            self.check(cases=cases)

    def test_attribution_and_temporal_qualifiers_cannot_be_removed(self):
        for fid, before, after in (
            ('F029', 'could', 'will'),
            ('F030', 'KITB predicts', 'It is proven that'),
            ('F020', 'During COVID', 'During H2D2'),
            ('F018', 'existing mRNA platform', 'entirely new mRNA platform'),
        ):
            with self.subTest(fact=fid):
                changed = deepcopy(self.corpus)
                row = next(f for f in changed['facts'] if f['id'] == fid)
                row['text'] = row['text'].replace(before, after)
                with self.assertRaisesRegex(ValueError, 'lost qualifier'):
                    self.check(changed)

    def test_independent_background_is_allowed_but_every_fact_has_a_case(self):
        result = self.check()
        self.assertEqual(set(result['coverage']['facts_without_argument']),
                         {'F021', 'F023', 'F024', 'F027'})
        self.assertEqual(result['coverage']['facts_without_acceptance_question'], [])
        self.assertIsNone(result['runtime_connected'])
        self.assertFalse(result['retrieval_tested'])
        self.assertIsNone(result['style_copy_guard_implemented'])

    def test_direct_answer_cannot_use_a_dangling_or_raw_fact_reference(self):
        changed = deepcopy(self.corpus)
        next(s for s in changed['style_examples'] if s['id'] == 'S026')['fact_ids'] = ['F999']
        with self.assertRaisesRegex(ValueError, 'invalid fact_ids'):
            self.check(changed)
        changed = deepcopy(self.corpus)
        next(f for f in changed['facts'] if f['id'] == 'F029')['source_id'] = 'user_raw_yes_2026_1017'
        with self.assertRaisesRegex(ValueError, 'raw/editorial source cannot establish a fact'):
            self.check(changed)


if __name__ == '__main__':
    unittest.main()
