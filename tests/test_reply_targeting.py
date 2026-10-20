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


def responded(store, author, *, account='a1', at=None):
    """Record that `author` answered one of our publications, so a second reply is allowed."""
    ts = time.time() if at is None else at
    with store.batch_transaction():
        store._conn.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            ('act' + author, 'ours_' + author, account, 'p', 1, 'g', 'reply', '', '', '', 0,
                             ts - 120, ts - 120, '{}'))
    store.upsert_observed_post({'post_id': 'back_' + author, 'author': author, 'text': 'A reply to you.',
                                'created_at': ts, 'parent_id': 'ours_' + author})


def registry(store, author, score, *, status='unknown'):
    """Pin an author's detector score so a test does not depend on the evidence pipeline."""
    with store.batch_transaction():
        store._conn.execute('INSERT INTO bot_registry(account_id,status,suspicion_score) VALUES(?,?,?) '
                            'ON CONFLICT(account_id) DO UPDATE SET suspicion_score=excluded.suspicion_score,'
                            'status=excluded.status', (author, status, score))


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
        # Suspicion below the hard floor (40 on the 0-100 scale) but a suspected-bot status: this is the cold ordering rule, not the filter.
        registry(self.store, 'botty', 6, status='suspected_bot')
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
        # Daily caps only; the 30-minute per-author gap has its own test.
        gap = patch.dict(targeting.POLICY['caps'], {'per_author_min_gap_seconds': 0}); gap.start(); self.addCleanup(gap.stop)

    def test_author_thread_and_account_caps(self):
        self.store.set_policy('reply_pacing', {'per_hour': 1000, 'min_gap_seconds': 0})  # daily caps only; pacing has its own test
        intent(self.store, 'a1', 't1', author='enemy', root='root1', at=time.time() - 60)
        responded(self.store, 'enemy', at=time.time() - 30)
        self.assertEqual(targeting.cap_reason(self.store, target_author='enemy'), '')
        intent(self.store, 'a2', 't2', author='enemy', root='root2', status='sending')
        self.assertEqual(targeting.cap_reason(self.store, target_author='enemy'), 'author_daily_cap')
        intent(self.store, 'a3', 't3', author='x', root='root1')
        intent(self.store, 'a4', 't4', author='y', root='root1')
        self.assertEqual(targeting.cap_reason(self.store, root_post_id='root1'), 'thread_daily_cap')
        for i in range(targeting.POLICY['caps']['per_account_per_day']):
            intent(self.store, 'busy', f'p{i}', author=f'u{i}', root=f'r{i}')
        intent(self.store, 'busy', 'init', kind='post', purpose='initialise')
        self.assertEqual(targeting.cap_reason(self.store, account='busy'), 'account_daily_cap')
        self.assertEqual(targeting.accounts_at_daily_cap(self.store, ['busy', 'a1']), {'busy'})

    def test_yesterday_does_not_count_and_failed_does_not_count(self):
        yesterday = targeting.sydney_day_start() - 3600
        intent(self.store, 'a1', 't1', author='enemy', at=yesterday)
        intent(self.store, 'a2', 't2', author='enemy', status='failed')
        intent(self.store, 'a3', 't3', author='enemy', at=time.time() - 60)
        responded(self.store, 'enemy', at=time.time() - 30)
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
        registry(store, 'botty', 0.6, status='suspected_bot')  # below the hard floor: the cold ordering rule decides
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


class ThreadDepthAndAuthorGapTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.store.register_verified_accounts(['ours'], status='own_bot')
        now = time.time()
        rows = [('r0', 'npc1', None, None), ('r1', 'npc2', 'r0', 'r0'), ('r2', 'ours', 'r1', 'r0'),
                ('r3', 'npc1', 'r2', 'r0'), ('r4', 'npc3', 'r3', 'r0')]
        for pid, author, parent, root in rows:
            self.store.upsert_observed_post({'post_id': pid, 'author': author, 'text': 'Text ' + pid, 'created_at': now,
                                             'parent_id': parent, 'root_post_id': root})

    def post(self, pid):
        return self.store.get_observed_post(pid)

    def test_depth_counts_levels_and_treats_missing_ancestors_as_deep(self):
        self.assertEqual([targeting.thread_depth(self.store, self.post(p)) for p in ('r0', 'r1', 'r2', 'r3', 'r4')], [0, 1, 2, 3, 4])
        self.assertGreaterEqual(targeting.thread_depth(self.store, {'post_id': 'x', 'parent_id': 'gone', 'root_post_id': 'root'}), 2)

    def test_deep_targets_are_skipped_unless_a_citizen_answers_us(self):
        self.assertEqual(targeting.deep_thread_reason(self.store, self.post('r2')), '')
        self.assertEqual(targeting.deep_thread_reason(self.store, self.post('r4')), 'targeting:deep_thread')
        self.assertEqual(targeting.skip_reason(self.store, self.post('r4')), 'targeting:deep_thread')
        # r3 is a citizen replying to our r2: allowed until we have answered twice in this thread today.
        self.assertEqual(targeting.deep_thread_reason(self.store, self.post('r3')), '')
        intent(self.store, 'ours', 'a', root='r0'); intent(self.store, 'ours', 'b', root='r0')
        self.assertEqual(targeting.deep_thread_reason(self.store, self.post('r3')), 'targeting:deep_thread')

    def test_deeper_targets_rank_lower(self):
        p = [targeting.audience(self.store, self.post(x))['priority'] for x in ('r0', 'r1')]
        self.assertGreater(p[0], p[1])

    def test_second_reply_to_the_same_author_waits_thirty_minutes(self):
        self.store.set_policy('reply_pacing', {'per_hour': 1000, 'min_gap_seconds': 0})
        intent(self.store, 'a1', 't1', author='npc1', at=time.time() - 600)
        self.assertEqual(targeting.cap_reason(self.store, target_author='npc1'), 'author_min_gap')
        responded(self.store, 'npc1', at=time.time() - 500)
        self.assertEqual(targeting.cap_reason(self.store, target_author='npc1', now=time.time() + 1300), '')


class BotAuthorFilterTests(unittest.TestCase):
    """A reply to an account the detector already scores as a bot earns nothing and raises its hot score."""

    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)

    def post(self, pid, author, **kw):
        self.store.upsert_observed_post({'post_id': pid, 'author': author, 'text': 'A question about venues?',
                                         'created_at': kw.pop('created_at', time.time()), **kw})
        return self.store.get_observed_post(pid)

    def score(self, author, value, status='unknown'):
        registry(self.store, author, value, status=status)

    def test_hot_post_by_a_suspected_bot_is_skipped(self):
        now = time.time(); self.score('enemybot', 90)
        hot = self.post('q1', 'enemybot', created_at=now, likes=40, replies=12)
        self.assertEqual(targeting.skip_reason(self.store, hot, now=now), 'targeting:bot_author')

    def test_quiet_citizen_is_still_targeted(self):
        now = time.time(); self.score('citizen9', 20)
        self.assertEqual(targeting.skip_reason(self.store, self.post('q2', 'citizen9', created_at=now), now=now), '')

    def test_a_weak_signal_does_not_hide_a_real_author(self):
        # 2026-10-20: the floor was written as 0.7 against a 0-100 score, so a score of 5 hid 32% of authors.
        now = time.time(); self.score('weak_signal', 5)
        self.assertEqual(targeting.skip_reason(self.store, self.post('q4', 'weak_signal', created_at=now), now=now), '')

    def test_the_shipped_floor_uses_the_detector_scale(self):
        # ctn/moderation/detector.py scores 0-100 (it confirms bots at >=85). A floor written as 0.7
        # skips every author with any signal at all, which removed 32% of authors on 2026-10-20.
        self.assertGreaterEqual(float(targeting.POLICY['bot_filter']['min_suspicion']), 1.0)

    def test_our_own_accounts_are_never_filtered(self):
        now = time.time(); self.score('ours', 95, status='own_bot')
        self.assertEqual(targeting.skip_reason(self.store, self.post('q3', 'ours', created_at=now), now=now), '')


class AuthorResponseTests(unittest.TestCase):
    """A second reply to the same author only goes out when that author answered us in between."""

    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        gap = patch.dict(targeting.POLICY['caps'], {'per_author_min_gap_seconds': 0}); gap.start(); self.addCleanup(gap.stop)
        self.store.set_policy('reply_pacing', {'per_hour': 1000, 'min_gap_seconds': 0})

    def test_second_reply_needs_an_answer_from_the_author(self):
        now = time.time()
        intent(self.store, 'a1', 't1', author='npc7', at=now - 60)
        self.assertEqual(targeting.cap_reason(self.store, target_author='npc7', now=now), 'author_not_responding')
        responded(self.store, 'npc7', at=now - 30)
        self.assertEqual(targeting.cap_reason(self.store, target_author='npc7', now=now), '')

    def test_answering_someone_else_does_not_count(self):
        now = time.time()
        intent(self.store, 'a1', 't1', author='npc8', at=now - 60)
        responded(self.store, 'someone_else', at=now - 20)
        self.assertEqual(targeting.cap_reason(self.store, target_author='npc8', now=now), 'author_not_responding')
