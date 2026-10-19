"""Launch hardening: spread initialise posts, no multi-account threads, frequency bot prior, self-check,
hidden-post slowdown, overlap-aware reporting risk and the renewal-bounded stop cost."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ctn.moderation import decision
from ctn.pipeline import targeting
from ctn.selfcheck import apply_slowdown, own_post_check, self_scan
from ctn.storage import StateStore
from tests.test_persona80 import bound_router


class InitialiseSpreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_posts_are_spread_over_the_window_with_a_minimum_gap(self):
        from ctn.warmup import initialise_step
        store = StateStore(); self.addCleanup(store.close)
        ids = tuple(f'yes_{i:03d}' for i in range(1, 11))
        router = bound_router(store, ids)
        published = []

        async def publish(self, account, text, **kwargs):
            published.append(account)
            with store.batch_transaction():
                store._conn.execute("INSERT INTO account_lifecycle(account_id,initialise_status) VALUES(?,'ready') "
                                    "ON CONFLICT(account_id) DO UPDATE SET initialise_status='ready'", (account,))
            return {'ok': True, 'status': 'published'}
        t0 = 1_000_000.0
        with patch('ctn.macro.publisher.StrategicPublisher.publish', publish), patch('ctn.warmup.time.time', return_value=t0):
            await initialise_step(SimpleNamespace(store=store, dry_run=False), router, None)
        schedule = store.policy('initialise_schedule')
        offsets = sorted(v - t0 for v in schedule.values())
        self.assertEqual(len(offsets), 10)
        self.assertTrue(all(0 <= o < 18000 for o in offsets))
        self.assertGreater(offsets[-1] - offsets[0], 3600)          # genuinely spread, not bunched
        self.assertLessEqual(len(published), 1)
        # Everything due at once still goes out one at a time, at least min_gap apart.
        with patch('ctn.macro.publisher.StrategicPublisher.publish', publish):
            with patch('ctn.warmup.time.time', return_value=t0 + 20000):
                await initialise_step(SimpleNamespace(store=store, dry_run=False), router, None)
                before = len(published)
                await initialise_step(SimpleNamespace(store=store, dry_run=False), router, None)
                self.assertEqual(len(published), before)            # within the gap: idle
            with patch('ctn.warmup.time.time', return_value=t0 + 20000 + 91):
                await initialise_step(SimpleNamespace(store=store, dry_run=False), router, None)
        self.assertEqual(len(set(published)), len(published))


class BatchSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_default_sends_a_single_reply_where_a_batch_was_allowed(self):
        from tests.test_persona80 import BatchEndToEndTests
        helper = BatchEndToEndTests(); self.addCleanup(helper.doCleanups)
        loop, platform, transport, requests = await helper.make_loop()
        loop.store.set_policy('reply_batches_enabled', False)
        for _ in range(4):await loop.process_pending()
        self.assertEqual(platform.write_count, 1)
        plan = loop.store._conn.execute('SELECT plan_json FROM reply_batches').fetchone()
        if plan:self.assertEqual(json.loads(plan[0])['expansion']['reason'], 'batches_disabled_at_launch')


class TargetingTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)

    def test_frequent_posters_are_treated_as_likely_bots_for_ordering(self):
        now = time.time()
        for i in range(4):
            self.store.upsert_observed_post({'post_id': f'f{i}', 'author': 'flooder', 'text': f'Post number {i} here', 'created_at': now - 60 * i})
        for i in range(3):
            self.store.upsert_observed_post({'post_id': f'c{i}', 'author': 'citizen', 'text': f'Citizen post {i} here', 'created_at': now - 60 * i})
        self.assertEqual(targeting.bot_probability(self.store, 'flooder'), 0.6)
        self.assertEqual(targeting.bot_probability(self.store, 'citizen'), 0.3)
        self.store.register_verified_accounts(['flooder'], status='verified_npc')
        self.assertEqual(targeting.bot_probability(self.store, 'flooder'), 0.0)

    def test_slowed_accounts_get_a_lower_daily_cap(self):
        self.store.set_policy('slowed_accounts', {'acct': 2})
        for i in range(2):
            with self.store.batch_transaction():
                self.store._conn.execute("INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,created_at,updated_at,metadata_json) "
                                         "VALUES(?,?,'reply','acct','t','x','succeeded','o',0,?,?,'{}')", (f'i{i}', f'k{i}', time.time(), time.time()))
        self.assertEqual(targeting.cap_reason(self.store, account='acct'), 'account_daily_cap')
        self.assertEqual(targeting.accounts_at_daily_cap(self.store, ['acct', 'other']), {'acct'})


class ReplyPacingTests(unittest.TestCase):
    # Live 2026-10-19: two fresh accounts replied every ~2 minutes (60 s cooldown + jitter, daily cap only).
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)

    def sent(self, ident, at, purpose=''):
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,created_at,updated_at,metadata_json) "
                                     "VALUES(?,?,'reply','acct','t','x','succeeded','o',0,?,?,?)", (ident, ident, at, at, json.dumps({'purpose': purpose})))

    def test_min_gap_and_hourly_cap_spread_an_accounts_replies(self):
        now = time.time()
        self.sent('a', now - 120)
        self.assertEqual(targeting.cap_reason(self.store, account='acct', now=now), 'account_min_gap')
        self.assertEqual(targeting.accounts_at_daily_cap(self.store, ['acct', 'other'], now=now), {'acct'})
        self.sent('b', now - 1500); self.sent('c', now - 3000)
        self.assertEqual(targeting.cap_reason(self.store, account='acct', now=now + 600), 'account_hourly_cap')
        self.assertEqual(targeting.cap_reason(self.store, account='acct', now=now + 1000), '')
        # First posts do not count against the pace.
        fresh = StateStore(); self.addCleanup(fresh.close); self.store, old = fresh, self.store
        self.sent('i', now - 10, purpose='initialise')
        self.assertEqual(targeting.cap_reason(self.store, account='acct', now=now), '')


class SelfCheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.store.register_verified_accounts(['a', 'b'], status='own_bot')

    def intent(self, ident, account, text, root, at):
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,created_at,updated_at,metadata_json) "
                                     "VALUES(?,?,'reply',?,?,?,'succeeded','o',0,?,?,?)", (ident, ident, account, root, text, at, at, json.dumps({'root_post_id': root})))

    async def test_scan_flags_shared_wording_crowded_threads_and_leaks(self):
        now = time.time()
        self.intent('1', 'a', 'One hospital system and it is already forty nurses short, that is why I back the rule.', 'r1', now - 100)
        self.intent('2', 'b', 'One hospital system and it is already forty nurses short, that is why I back this rule.', 'r1', now - 50)
        self.intent('3', 'a', 'As an AI language model, I support the rule.', 'r2', now - 30)
        report = self_scan(self.store, now=now)
        self.assertGreaterEqual(report['a']['score'], 3)
        self.assertEqual(report['a']['leak_phrases'], 1)
        self.assertTrue(report['b']['shared_wording'] and report['b']['crowded_threads'])
        alerts = {json.loads(r[0])['account_id'] for r in self.store._conn.execute("SELECT payload_json FROM discord_outbox WHERE event_key LIKE 'self_check:%'")}
        self.assertIn('a', alerts)

    async def test_repeated_selfcheck_steps_keep_their_rule(self):
        # Live 2026-10-19: the scan result overwrote the `self_check` rule and the second step crashed.
        from ctn.selfcheck import selfcheck_step
        actuator = SimpleNamespace(get_post=AsyncMock(return_value={}))
        now = time.time()
        await selfcheck_step(actuator, self.store, now=now)
        await selfcheck_step(actuator, self.store, now=now + 4000)
        self.assertEqual(targeting.launch_rule(self.store, 'self_check')['every_seconds'], 1800)
        self.assertIn('at', self.store.policy('self_check:scan'))
        # A database already holding the old scan result under the rule key still runs.
        self.store.set_policy('self_check', {'at': now, 'accounts': {}})
        await selfcheck_step(actuator, self.store, now=now + 8000)

    async def test_hidden_posts_alert_and_slow_the_account(self):
        now = time.time()
        with self.store.batch_transaction():
            for pid in ('p1', 'p2', 'p3'):
                self.store._conn.execute("INSERT INTO publications VALUES(?,?,?,'yes_001',2,'g','reply','t','t','',0,?,?,'{}')", ('act' + pid, pid, 'a', now - 3600, now - 3600))
        reads = {'p1': {'post_id': 'p1', 'hidden': True}, 'p2': {}, 'p3': {'post_id': 'p3', 'status': 'visible'}}
        actuator = SimpleNamespace(get_post=AsyncMock(side_effect=lambda pid: reads[pid]))
        await own_post_check(actuator, self.store, now=now)
        states = dict(self.store._conn.execute('SELECT post_id,state FROM own_post_checks').fetchall())
        self.assertEqual(states, {'p1': 'hidden', 'p2': 'visible', 'p3': 'visible'})   # one unreadable read is not enough
        await own_post_check(actuator, self.store, now=now + 3601)
        states = dict(self.store._conn.execute('SELECT post_id,state FROM own_post_checks').fetchall())
        self.assertEqual(states['p2'], 'hidden')
        self.assertEqual(self.store.policy('slowed_accounts'), {'a': 8})
        kinds = [json.loads(r[0])['kind'] for r in self.store._conn.execute('SELECT payload_json FROM discord_outbox')]
        self.assertEqual(sorted(kinds), ['account_slowed', 'own_post_hidden', 'own_post_hidden'])
        self.assertEqual(apply_slowdown(self.store), {'a': 8})                          # idempotent


class ReportingRiskTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.pol = decision.policy(self.store)

    def attempts(self, times, p=0.9):
        with self.store.batch_transaction():
            for i, at in enumerate(times):
                self.store._conn.execute("INSERT INTO report_attempts VALUES(?,?,'post',?,?,'','pending',?,0,'',?)",
                                         (f'r{i}', f'a{i}', f's{i}', f'u{i}', at, json.dumps({'decision_p': p})))

    def test_overlapping_stops_count_once(self):
        now = 1_000_000.0
        self.attempts([now] * 10)
        clustered = decision.expected_genuine(self.store, self.pol, now=now)
        self.assertLess(clustered, 1.0)
        self.store._conn.execute('DELETE FROM report_attempts')
        self.attempts([now + i * 21600 for i in range(4)] * 3)
        self.assertGreater(decision.expected_genuine(self.store, self.pol, now=now), 2.5)
        self.assertEqual(self.pol['max_expected_genuine'], 3.0)

    def test_stop_cost_is_bounded_when_rulings_are_fast(self):
        now = time.time()
        with self.store.batch_transaction():
            for i in range(60):
                self.store._conn.execute("INSERT INTO report_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                         (f'c{i}', 'post', f's{i}', f'u{i}', 'leak_behavior', 0.95, 0.725, 19.0, 0, 0, 'auto', 'ok', 0, now, now))
            for i in range(5):
                self.store._conn.execute("INSERT INTO report_attempts VALUES(?,?,'post',?,?,'','ruled',?,?,'upheld','{}')",
                                         (f'q{i}', f'b{i}', f't{i}', f'v{i}', now - 4000, now - 4000 + 5400))
        cost = decision.estimate_cost(self.store, self.pol, now)
        average = 0.725 * 19.0
        self.assertLessEqual(cost, average * 0.725 / 0.275 + 1e-3)
        self.assertGreater(0.725 * 19.0 - 0.275 * cost, -1e-2)     # an average fresh candidate is still worth reporting

    def test_defaults_no_longer_cap_reports_at_five(self):
        from ctn.config import Settings
        self.assertEqual((Settings().max_pending_reports, Settings().max_pending_per_author), (40, 2))


class LockStatsTests(unittest.TestCase):
    def test_slow_transactions_are_counted_attributed_and_logged(self):
        store = StateStore(); self.addCleanup(store.close)
        before = store.lock_stats()['current']['transactions']
        with store.batch_transaction():
            with store.batch_transaction():pass          # nested blocks are not counted twice
        clock = iter([0.0, 0.0, 1.5])                     # requested, acquired, finished
        with patch('ctn.storage.sqlite_store.time.perf_counter', side_effect=lambda: next(clock)), \
             self.assertLogs('ctn.db', level='WARNING') as logs:
            with store.batch_transaction():pass
        snap = store.lock_stats()['current']
        self.assertEqual(snap['transactions'] - before, 2)
        self.assertEqual(snap['max_hold_s'], 1.5)
        self.assertIn('test_launch.py', snap['slow'][0]['caller'])
        self.assertIn('slow db transaction', logs.output[0])


if __name__ == '__main__':
    unittest.main()


class LikePacingTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.store.set_policy('require_warmup', True)

    def like(self, target, now):
        return self.store.claim_action('like', 'acct', target, metadata={}, now=now)

    def test_likes_wait_for_the_first_post_then_respect_hourly_and_daily_caps(self):
        now = time.time()
        self.assertEqual(self.like('p0', now), (None, 'like_needs_initialise'))
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO account_lifecycle(account_id,initialise_status) VALUES('acct','ready')")
        for i in range(4):
            ident, reason = self.like(f'p{i}', now + i)
            self.assertTrue(ident, reason)
        self.assertEqual(self.like('p4', now + 5), (None, 'like_hourly_cap'))
        self.store.set_policy('like_pacing', {'per_hour': 100, 'per_day': 4})
        self.assertEqual(self.like('p5', now + 6), (None, 'like_daily_cap'))
