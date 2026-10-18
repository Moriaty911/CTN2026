"""Expected-value report automation under the 2026 moderator model (0.75 bot / 0.25 citizen)."""
import json
import time
import unittest
from types import SimpleNamespace

from ctn.client.base import PlatformError
from ctn.client.legit_v1 import NormalizedPage
from ctn.client.mock import MockLegitClient, MockPost
from ctn.models import Post
from ctn.moderation import decision
from ctn.moderation.detector import BotDetector
from ctn.moderation.events import probe_report_targets
from ctn.moderation.policy import candidate_payload, PREFIXES
from ctn.moderation.reporter import BlueTeamReporter
from ctn.storage import StateStore
from tests.test_campaign_v5 import strong_evidence
from tests.test_runtime_reliability import actor


def behavior_only(store, author='ring'):
    """Repetition + coordination + timing, no leak: confirmed locally but below the probability floor."""
    pid = author + '0'
    store.upsert_observed_post({'post_id': pid, 'author': author, 'text': 'Synthetic ordinary text', 'created_at': time.time() - 60})
    for family, points in (('repetition', 35), ('coordination', 35), ('timing', 20)):
        store.add_weighted_evidence(author, family, points, {'post_id': pid, 'version': 1}, evidence_key=family + ':' + pid)
    return pid


class DecisionMathTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.store.set_policy('report_mode', 'hybrid')

    def decide(self, target='leak', author='enemy', **kw):
        payload = candidate_payload(self.store, 'post', target, author)
        return decision.decide(self.store, payload, self.store.report_candidate_id(payload), **kw)

    def test_strong_fresh_leak_is_auto_only_when_shadow_is_off(self):
        strong_evidence(self.store)
        shadow = self.decide()
        self.assertEqual((shadow['bucket'], shadow['action'], shadow['reason']), ('leak_behavior', 'human', 'shadow_mode'))
        self.assertGreaterEqual(shadow['p'], 0.85)
        decision.set_overrides(self.store, shadow=False)
        self.assertEqual(self.decide()['action'], 'auto')

    def test_probability_floor_cannot_be_lowered_and_blocks_automation(self):
        with self.assertRaises(ValueError):
            decision.set_overrides(self.store, p_floor=0.5)
        decision.set_overrides(self.store, shadow=False, auto_buckets=['leak_behavior', 'behavior_only'])
        target = behavior_only(self.store)
        result = self.decide(target, 'ring')
        self.assertEqual(result['bucket'], 'behavior_only')
        self.assertLess(result['p'], 0.7)
        # Positive expected value is not enough: below the floor it only goes to a human.
        self.assertGreater(result['expected_value'], 0)
        self.assertEqual((result['action'], result['reason']), ('human', 'below_floor_needs_human'))

    def test_no_reward_for_old_or_hidden_targets(self):
        strong_evidence(self.store)
        old = self.decide(now=time.time() + 7 * 3600)
        self.assertEqual((old['reward'], old['action'], old['reason']), (0.0, 'skip', 'target_too_old'))
        post = self.store.get_observed_post('leak')
        self.store.upsert_observed_post({**post['raw'], 'post_id': 'leak', 'author': 'enemy', 'text': post['text'],
                                         'created_at': post['created_at'], 'hidden': True})
        self.assertEqual(self.decide()['reason'], 'target_hidden')

    def test_first_report_odds_decay_with_age(self):
        strong_evidence(self.store)
        fresh = self.decide()['reward']
        later = self.decide(now=time.time() + 2 * 3600)['reward']
        self.assertGreater(fresh, later)
        self.assertLessEqual(fresh, 25)

    def test_many_opportunities_raise_the_cost_of_a_suspension(self):
        strong_evidence(self.store)
        quiet = self.decide(record=False)
        now = time.time()
        with self.store.batch_transaction():
            for i in range(40):
                self.store._conn.execute("INSERT INTO report_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f'c{i}', 'post', f'p{i}', f'author{i}', 'leak_behavior', 0.95, 0.725, 24.0, 5, 1, 'auto', 'x', 0, now, now))
            # Fast rulings mean more reports fit into a six-hour stop.
            for i in range(3):
                self.store._conn.execute("INSERT INTO report_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (f'a{i}', f'act{i}', 'post', f'x{i}', 'other', '', 'ruled', now - 4000, now - 400, 'upheld', '{}'))
        busy = self.decide(record=False)
        self.assertGreater(busy['cost'], quiet['cost'])
        self.assertGreater(busy['threshold'], quiet['threshold'])

    def test_rejections_recalibrate_a_bucket_below_the_floor(self):
        strong_evidence(self.store)
        before = decision.estimate_p(self.store, 'leak_behavior', '', decision.policy(self.store))
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO report_calibration VALUES('leak_behavior',4,16)")
        after = decision.estimate_p(self.store, 'leak_behavior', '', decision.policy(self.store))
        self.assertGreater(before, 0.85)
        self.assertLess(after, 0.5)
        decision.set_overrides(self.store, shadow=False)
        self.assertEqual((self.decide()['action'], self.decide()['reason']), ('skip', 'below_floor'))

    def test_author_rulings_update_probability_by_bayes(self):
        pol = decision.policy(self.store)
        base = decision.estimate_p(self.store, 'leak_only', 'x', pol)
        self.store.log_report('p1', 'x', outcome='upheld')
        up = decision.estimate_p(self.store, 'leak_only', 'x', pol)
        self.assertAlmostEqual(up, base * 0.75 / (base * 0.75 + (1 - base) * 0.25), places=3)

    def test_detail_is_plain_english_and_bounded(self):
        payload = strong_evidence(self.store)
        self.assertTrue(payload['detail'].startswith(PREFIXES['post']))
        self.assertNotIn('{', payload['detail'])
        self.assertIn('AI or template origin', payload['detail'])
        self.assertIn('near-identical long text', payload['detail'])
        self.assertLessEqual(len(payload['detail']), len(PREFIXES['post']) + 900)


class ReportFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.platform = MockLegitClient(failure_rate=0, latency_ms=(0, 0))
        self.platform.posts['leak'] = MockPost('leak', 'enemy', 'Synthetic target', time.time())
        self.actuator = actor(self.platform, self.store)
        self.reporter = BlueTeamReporter(self.actuator, self.store, accounts=['reporter'], enabled=True)
        self.store.set_policy('report_mode', 'hybrid')
        decision.set_overrides(self.store, shadow=False)
        self.payload = strong_evidence(self.store)

    async def test_auto_send_records_bucket_and_ruling_calibrates(self):
        result = await self.reporter._submit_candidate('post', 'leak', 'enemy')
        self.assertTrue(result.ok, result.detail)
        attempt = self.store._conn.execute('SELECT id,payload_json FROM report_attempts').fetchone()
        meta = json.loads(attempt['payload_json'])
        self.assertEqual((meta['decision_action'], meta['decision_bucket']), ('auto', 'leak_behavior'))
        self.store.apply_moderation_event({'event_id': 'r1', 'attempt_id': attempt['id'], 'subject_kind': 'post',
                                           'subject_id': 'leak', 'outcome': 'upheld', 'ruled_at': time.time() + 1, 'source': 'synthetic'})
        row = self.store._conn.execute("SELECT upheld,rejected FROM report_calibration WHERE bucket='leak_behavior'").fetchone()
        self.assertEqual(tuple(row), (1, 0))

    async def test_risk_budget_limits_unresolved_reports(self):
        decision.set_overrides(self.store, max_expected_genuine=0.2)
        result = await self.reporter._submit_candidate('post', 'leak', 'enemy')
        self.assertEqual(result.detail, 'genuine_risk_budget')
        self.assertEqual(self.platform.write_count, 0)

    async def test_report_403_pauses_automation_and_suspension_wording_stops_reporting(self):
        async def refused(*a, **k): raise PlatformError('reporting currently unavailable', status=403, retryable=False)
        self.platform.report = refused
        self.assertEqual((await self.reporter._submit_candidate('post', 'leak', 'enemy')).status, 'failed')
        self.assertEqual(self.store.report_state_ready(automatic=True), (False, 'reporting_forbidden_recently'))
        self.assertFalse(self.store.is_reporting_suspended()[0])
        self.store.set_report_clearance('operator checked the platform', actor='admin')
        self.assertTrue(self.store.report_state_ready(automatic=True)[0])
        decision.record_report_forbidden(self.store, 'reporting is suspended for your team')
        self.assertTrue(self.store.is_reporting_suspended()[0])

    async def test_skip_decisions_do_not_create_review_cards(self):
        self.platform.posts['leak'].created_at = time.time() - 8 * 3600
        with self.store.batch_transaction():
            self.store._conn.execute("UPDATE observed_posts SET created_at=? WHERE post_id='leak'", (time.time() - 8 * 3600,))
        payload = candidate_payload(self.store, 'post', 'leak', 'enemy')
        self.assertIsNone(await self.reporter._submit_candidate('post', 'leak', 'enemy'))
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM report_candidates').fetchone()[0], 0)
        self.assertEqual(self.store._conn.execute('SELECT action FROM report_decisions').fetchone()[0], 'skip')
        self.assertIsNotNone(payload)

    async def test_account_activity_is_counted_from_public_reads(self):
        pages = {None: NormalizedPage([{'post_id': str(i)} for i in range(50)], next_cursor='2'),
                 '2': NormalizedPage([{'post_id': str(i)} for i in range(50, 80)], next_cursor=None)}
        likes = NormalizedPage([{'post_id': 'l' + str(i)} for i in range(30)], next_cursor=None)
        async def posts(target, *, limit=50, cursor=None): return pages[cursor]
        async def liked(target, *, limit=50, cursor=None): return likes
        self.reporter.actuator = SimpleNamespace(get_account_posts=posts, get_account_likes=liked, suspended=set())
        activity = await self.reporter.refresh_account_activity('enemy')
        self.assertEqual((activity['action_count'], activity['source']), (110, 'api_count:posts+likes'))
        # Rate-limited: a second call within the interval reuses the record.
        pages[None] = NormalizedPage([], None)
        self.assertEqual((await self.reporter.refresh_account_activity('enemy'))['action_count'], 110)

    async def test_target_probe_only_records_fields(self):
        result = await self.reporter._submit_candidate('post', 'leak', 'enemy')
        self.assertTrue(result.ok)
        with self.store.batch_transaction():
            self.store._conn.execute('UPDATE report_attempts SET submitted_at=submitted_at-600')
        self.assertEqual(await probe_report_targets(self.store, self.actuator), 1)
        row = self.store._conn.execute('SELECT found,fields_json FROM report_target_observations').fetchone()
        self.assertEqual(row['found'], 1)
        self.assertIn('_keys', json.loads(row['fields_json']))
        self.assertEqual(self.store._conn.execute('SELECT status FROM report_attempts').fetchone()[0], 'pending')
        self.assertEqual(await probe_report_targets(self.store, self.actuator), 0)


if __name__ == '__main__':
    unittest.main()
