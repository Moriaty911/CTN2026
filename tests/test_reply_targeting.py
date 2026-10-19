"""Reply targeting: audience-weighted priority, cold likely-bot skips, anti-pile-on caps, refreshes, crawl fairness."""
import json
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from ctn.actuator import Actuator
from ctn.client.legit_v1 import NormalizedPage
from ctn.client.mock import MockLegitClient
from ctn.config import BudgetConfig, RateConfig
from ctn.models import ActionKind, Decision, JevAnswers, Post, Side, Tier
from ctn.pipeline import targeting
from ctn.pipeline.crawl import CommentCrawler, FrontlineCrawler
from ctn.pipeline.loop import BotLoop
from ctn.pipeline.refresh import refresh_step
from ctn.ratelimit import build_gate, build_ledger
from ctn.storage import StateStore
from tests.test_crawlers import ScriptedClient
from tests.test_persona80 import bound_router

FAST = RateConfig(read_rps=1000, write_rps=1000, per_account_read_rps=1000, per_account_write_rps=1000)


def confirm(store, author):
    store.upsert_observed_post({'post_id': author + '_e', 'author': author, 'text': 'x', 'created_at': time.time() - 5000})
    for family, points in (('leak', 50), ('repetition', 35)):
        store.add_weighted_evidence(author, family, points, {'post_id': author + '_e', 'version': 1}, evidence_key=family + author)


def intent(store, account, target, *, author='', root='', purpose='', kind='reply', status='succeeded', at=None):
    meta = {'target_author': author, 'root_post_id': root, 'purpose': purpose}
    with store.batch_transaction():
        store._conn.execute("INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,"
                            "created_at,updated_at,metadata_json) VALUES(?,?,?,?,?,'',?,'x',0,?,?,?)",
                            (uuid.uuid4().hex, uuid.uuid4().hex, kind, account, target, status, at or time.time(),
                             at or time.time(), json.dumps(meta)))


class AudienceTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.store.register_verified_accounts(['ours'], status='own_bot')
        self.store.register_verified_accounts(['mayor'], status='verified_npc')

    def post(self, pid, author, **kw):
        self.store.upsert_observed_post({'post_id': pid, 'author': author, 'text': 'A question about venues?',
                                         'created_at': kw.pop('created_at', time.time()), **kw})
        return self.store.get_observed_post(pid)

    def test_replies_to_us_and_verified_rank_above_ordinary_and_bots(self):
        self.post('mine', 'ours')
        to_us = targeting.audience(self.store, self.post('r1', 'citizen', parent_id='mine'))
        plain = targeting.audience(self.store, self.post('p1', 'citizen2'))
        vip = targeting.audience(self.store, self.post('v1', 'mayor'))
        confirm(self.store, 'botty')
        bot = targeting.audience(self.store, self.post('b1', 'botty'))
        self.assertTrue(to_us['replies_to_us'])
        self.assertGreater(to_us['priority'], plain['priority'])
        self.assertEqual(vip['p_bot'], 0.0)
        self.assertGreater(vip['priority'], plain['priority'])
        self.assertGreater(plain['priority'], bot['priority'])
        self.assertTrue(targeting.audience(self.store, self.store.get_observed_post('mine'))['own'])

    def test_hot_bot_post_still_outranks_quiet_citizen_post(self):
        confirm(self.store, 'botty')
        hot = targeting.audience(self.store, self.post('b2', 'botty', likes=40, replies=25))
        quiet = targeting.audience(self.store, self.post('p2', 'citizen3'))
        self.assertGreater(hot['priority'], quiet['priority'])

    def test_freshness_replaces_waiting_bonus(self):
        new = targeting.audience(self.store, self.post('n', 'c'))
        old = targeting.audience(self.store, self.post('o', 'c2', created_at=time.time() - 1800))
        self.assertAlmostEqual(old['freshness'], 0.5, places=2)
        self.assertGreater(new['priority'], old['priority'])

    def test_likely_bots_without_reach_wait_until_they_can_be_judged(self):
        confirm(self.store, 'botty')
        now = time.time()
        info, start = targeting.initial_schedule(self.store, self.post('b3', 'botty', created_at=now), now=now)
        self.assertTrue(info['deferred']); self.assertGreaterEqual(start, now + 600)
        info, start = targeting.initial_schedule(self.store, self.post('c4', 'citizen4', created_at=now), now=now)
        self.assertFalse(info['deferred']); self.assertEqual(start, now)
        task = self.store._conn.execute("SELECT next_attempt_at,audience FROM post_tasks WHERE post_id='b3'").fetchone()
        self.assertGreaterEqual(task['next_attempt_at'], now + 600)

    def test_cold_skip_needs_age_low_engagement_and_likely_bot(self):
        confirm(self.store, 'botty')
        now = time.time()
        fresh = self.post('b5', 'botty', created_at=now)
        self.assertEqual(targeting.skip_reason(self.store, fresh, now=now), '')
        self.assertEqual(targeting.skip_reason(self.store, fresh, now=now + 700), 'targeting:cold_likely_bot')
        self.assertEqual(targeting.skip_reason(self.store, fresh, 'aligned', now=now + 700), '')
        hot = self.post('b6', 'botty', created_at=now, likes=5)
        self.assertEqual(targeting.skip_reason(self.store, hot, now=now + 700), '')
        citizen = self.post('c6', 'citizen6', created_at=now)
        self.assertEqual(targeting.skip_reason(self.store, citizen, now=now + 700), '')
        targeting.mark_trending(self.store, ['b5'], now + 650)
        self.assertEqual(targeting.skip_reason(self.store, fresh, now=now + 700), '')


class CapTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)

    def test_author_thread_and_account_caps(self):
        self.store.set_policy('reply_pacing', {'per_hour': 1000, 'min_gap_seconds': 0})  # daily caps only; pacing has its own test
        intent(self.store, 'a1', 't1', author='enemy', root='root1')
        self.assertEqual(targeting.cap_reason(self.store, target_author='enemy'), '')
        intent(self.store, 'a2', 't2', author='enemy', root='root2', status='sending')
        self.assertEqual(targeting.cap_reason(self.store, target_author='enemy'), 'author_daily_cap')
        intent(self.store, 'a3', 't3', author='x', root='root1')
        intent(self.store, 'a4', 't4', author='y', root='root1')
        self.assertEqual(targeting.cap_reason(self.store, root_post_id='root1'), 'thread_daily_cap')
        for i in range(35):
            intent(self.store, 'busy', f'p{i}', author=f'u{i}', root=f'r{i}')
        intent(self.store, 'busy', 'init', kind='post', purpose='initialise')
        self.assertEqual(targeting.cap_reason(self.store, account='busy'), 'account_daily_cap')
        self.assertEqual(targeting.accounts_at_daily_cap(self.store, ['busy', 'a1']), {'busy'})

    def test_yesterday_does_not_count_and_failed_does_not_count(self):
        yesterday = targeting.sydney_day_start() - 3600
        intent(self.store, 'a1', 't1', author='enemy', at=yesterday)
        intent(self.store, 'a2', 't2', author='enemy', status='failed')
        intent(self.store, 'a3', 't3', author='enemy')
        self.assertEqual(targeting.cap_reason(self.store, target_author='enemy'), '')

    def test_claim_action_enforces_caps_and_router_skips_capped_accounts(self):
        router = bound_router(self.store, ('yes_001', 'yes_002'))
        persona = router.registry.get('yes_001')
        intent(self.store, 'other', 'x', author='enemy'); intent(self.store, 'other2', 'y', author='enemy')
        meta = {**router.context_for(persona), 'target_author': 'enemy', 'root_post_id': 'z'}
        self.assertEqual(self.store.claim_action('reply', persona.account_id, 'z', 'Text', metadata=meta), (None, 'author_daily_cap'))
        for i in range(35):
            intent(self.store, persona.account_id, f'p{i}', author=f'u{i}', root=f'r{i}')
        chosen = router.route({}, 'any post')
        self.assertEqual(chosen.account_id, router.registry.get('yes_002').account_id)


class QueueOrderTests(unittest.TestCase):
    def test_claim_order_ignores_waiting_time(self):
        store = StateStore(); self.addCleanup(store.close)
        store.upsert_observed_post({'post_id': 'old', 'author': 'a', 'text': 'x', 'created_at': time.time()})
        store.upsert_observed_post({'post_id': 'new', 'author': 'b', 'text': 'y', 'created_at': time.time()})
        with store.batch_transaction():
            store._conn.execute("UPDATE post_tasks SET priority=1,queued_at=? WHERE post_id='old'", (time.time() - 1700,))
            store._conn.execute("UPDATE post_tasks SET priority=2 WHERE post_id='new'")
        self.assertEqual(store.claim_post_tasks(1)[0]['post_id'], 'new')


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.now = time.time()
        self.store.upsert_observed_post({'post_id': 'p1', 'author': 'c', 'text': 'x', 'created_at': self.now - 650})
        self.store.upsert_observed_post({'post_id': 'p2', 'author': 'd', 'text': 'y', 'created_at': self.now - 650})
        with self.store.batch_transaction():
            self.store._conn.execute("UPDATE post_tasks SET status='skipped' WHERE post_id='p2'")
        self.calls = []
        async def counters(pid):
            self.calls.append(pid); return {'likes': 9, 'replies': 4, 'reposts': 1, 'views': 50}
        self.actuator = SimpleNamespace(get_counters=counters)

    async def test_due_open_tasks_refresh_and_update_counts_queue_and_score(self):
        before = self.store._conn.execute("SELECT audience FROM post_tasks WHERE post_id='p1'").fetchone()[0]
        done = await refresh_step(self.actuator, self.store, now=self.now, sleep=lambda s: _noop())
        self.assertEqual((done, self.calls), (1, ['p1']))
        row = self.store.get_observed_post('p1')
        self.assertEqual((row['likes'], row['replies'], row['reposts']), (9, 4, 1))
        self.assertIsNotNone(self.store._conn.execute("SELECT 1 FROM comment_queue WHERE post_id='p1'").fetchone())
        after = self.store._conn.execute("SELECT audience FROM post_tasks WHERE post_id='p1'").fetchone()[0]
        self.assertGreater(after, before)
        statuses = dict(self.store._conn.execute("SELECT post_id,status FROM post_refresh WHERE stage=0").fetchall())
        self.assertEqual(statuses, {'p1': 'done', 'p2': 'dropped'})

    async def test_refresh_yields_to_backed_up_reads_and_per_minute_budget(self):
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO request_slots(bucket,next_at) VALUES('team:read',?)", (self.now + 10,))
        self.assertEqual(await refresh_step(self.actuator, self.store, now=self.now, sleep=lambda s: _noop()), 0)
        with self.store.batch_transaction():
            self.store._conn.execute("DELETE FROM request_slots")
        self.store.set_policy('refresh_window', {'minute': int(self.now // 60), 'n': 120})
        self.assertEqual(await refresh_step(self.actuator, self.store, now=self.now, sleep=lambda s: _noop()), 0)

    async def test_stale_refreshes_are_dropped_not_queued(self):
        await refresh_step(self.actuator, self.store, now=self.now + 1500 + 400, sleep=lambda s: _noop())
        self.assertEqual(self.store._conn.execute("SELECT status FROM post_refresh WHERE post_id='p1' AND stage=0").fetchone()[0], 'stale')


async def _noop():
    return None


class TrendingReads(MockLegitClient):
    def __init__(self):
        super().__init__(failure_rate=0, latency_ms=(0, 0)); self.reads = []
    async def get_feed(self, account, *, limit=50, cursor=None):
        self.reads.append(('latest', cursor))
        return NormalizedPage([{'post_id': f'old{cursor}', 'author': 'a', 'text': 'x', 'created_at': time.time() - 900}], next_cursor='c2')
    async def feed_trending(self, *, limit=50, cursor=None):
        self.reads.append(('trending', None))
        return NormalizedPage([{'post_id': 'hot', 'author': 'b', 'text': 'y', 'created_at': time.time() - 300, 'likes': 30}])


class CrawlSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_trending_feed_alternates_and_marks_posts(self):
        client = TrendingReads(); store = StateStore(); self.addCleanup(store.close)
        act = Actuator(client, build_gate(FAST), build_ledger(BudgetConfig()), store=store)
        crawler = FrontlineCrawler(act, store, feed_account='a', aux_every=1000, trending_feed_every=2, min_feed_interval_s=0)
        t0 = time.time()
        sources = [(await crawler.step(now=t0 + i)).source for i in range(4)]
        self.assertEqual(sources, ['feed', 'feed_trending', 'feed', 'feed_trending'])
        self.assertTrue(targeting.is_trending(store, 'hot'))

    async def test_stale_backlog_is_abandoned(self):
        client = TrendingReads(); store = StateStore(); self.addCleanup(store.close)
        act = Actuator(client, build_gate(FAST), build_ledger(BudgetConfig()), store=store)
        crawler = FrontlineCrawler(act, store, feed_account='a', aux_every=1000, trending_feed_every=0, min_feed_interval_s=0)
        await crawler.step(now=1.0)                  # head page: posts older than our last head -> backlog cursor
        store.set_policy('crawl:feed', {'cursor': 'c2', 'continue_next': True, 'head': 'x', 'stop_at': 'y'})
        await crawler.step(now=time.time())          # continuation page is 15 minutes old -> dropped
        self.assertIsNone(store.policy('crawl:feed')['cursor'])


class CommentFairnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.client = ScriptedClient()
        self.act = Actuator(self.client, build_gate(FAST), build_ledger(BudgetConfig()), store=self.store)

    async def test_own_thread_is_read_before_many_huge_threads(self):
        self.store.register_verified_accounts(['ours'], status='own_bot')
        for i in range(20):
            self.store.upsert_observed_post(Post(f'big{i}', f'u{i}', 'Huge thread', 1.0, replies=2000), now=1.0)
        self.store.upsert_observed_post(Post('mine', 'ours', 'Our post', 1.0, replies=2), now=1.0)
        self.client.comment_rows['mine'] = []
        report = await CommentCrawler(self.act, self.store, candidates=15, cooldown_s=0).step(now=100.0)
        self.assertEqual(report.post_id, 'mine')

    async def test_depth_cap_gives_up_a_thread_backlog_for_the_hour(self):
        self.store.upsert_observed_post(Post('big', 'u', 'Huge thread', 1.0, replies=5000), now=1.0)
        self.client.comment_rows['big'] = [{'post_id': f'c{i}', 'author': f'r{i}', 'text': 'reply text here', 'created_at': 2.0 + i}
                                           for i in range(50)]
        crawler = CommentCrawler(self.act, self.store, cooldown_s=0)
        results = [await crawler.step(now=100.0 + i) for i in range(5)]
        self.assertEqual([r.skipped for r in results[:4]], ['', '', '', ''])
        self.assertEqual(results[4].skipped, 'depth_cap')
        self.assertIsNone(self.store._conn.execute("SELECT 1 FROM comment_queue WHERE post_id='big'").fetchone())


class FakeBrain:
    def __init__(self, store):
        self.router = SimpleNamespace(store=store, registry=SimpleNamespace(catalog_hash='', list_all=lambda: []))
        self.assessed = []
    async def assess(self, post):
        self.assessed.append(post.post_id); return None
    async def decide(self, post, *, commit_state=False, **kw):
        return Decision(uuid.uuid4().hex[:12], post.post_id, Tier.OBSERVE, 'bot_default', JevAnswers(answers={}, raw={}, ok=True),
                        'jev:no_interaction', ActionKind.NONE, disposition='skip')


class EarlySkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_likely_bot_and_capped_targets_spend_no_jev(self):
        store = StateStore(); self.addCleanup(store.close)
        act = Actuator(MockLegitClient(failure_rate=0, latency_ms=(0, 0)), build_gate(FAST), build_ledger(BudgetConfig()), dry_run=True, store=store)
        brain = FakeBrain(store)
        loop = BotLoop(act, side=Side.YES, accounts=['bot_001'], store=store, brain=brain)
        confirm(store, 'botty')
        store.upsert_observed_post(Post('cold', 'botty', 'Quiet post', time.time() - 900))
        intent(store, 'x', 'a', author='loud'); intent(store, 'y', 'b', author='loud')
        store.upsert_observed_post(Post('capped', 'loud', 'Another post', time.time()))
        with store.batch_transaction():
            store._conn.execute("UPDATE post_tasks SET next_attempt_at=0")
        await loop.process_pending()
        reasons = dict(store._conn.execute("SELECT post_id,reason FROM post_tasks WHERE post_id IN ('cold','capped')").fetchall())
        self.assertEqual(reasons, {'cold': 'targeting:cold_likely_bot', 'capped': 'author_daily_cap'})
        self.assertNotIn('cold', brain.assessed); self.assertNotIn('capped', brain.assessed)


if __name__ == '__main__':
    unittest.main()
