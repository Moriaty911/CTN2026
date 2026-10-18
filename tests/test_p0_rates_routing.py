"""P0 fixes: official-limit pacing defaults, crawler read waste, high-tier as a preference, bounded retries."""
import time
import unittest
import uuid
from types import SimpleNamespace

from ctn.actuator import Actuator
from ctn.client.base import PlatformError
from ctn.client.legit_v1 import OfficialLegitClient
from ctn.client.mock import MockLegitClient
from ctn.config import BudgetConfig, RateConfig, Settings
from ctn.models import ActionKind, Decision, JevAnswers, Post, Side, Tier
from ctn.pipeline.crawl import FrontlineCrawler
from ctn.pipeline.loop import BotLoop
from ctn.ratelimit import build_gate, build_ledger
from ctn.storage import StateStore
from tests.test_legit_v1_adapter import FakeOfficialLegitSocial
from tests.test_persona80 import bound_router


class PacingDefaultsTests(unittest.TestCase):
    def test_team_ceilings_no_longer_bind_below_official_routes(self):
        rate, budget = RateConfig(), BudgetConfig()
        self.assertGreaterEqual(rate.read_rps * 60, 300)       # well above feeds (10/min) + other reads
        self.assertGreaterEqual(rate.write_rps * 60, 36)       # publish 3 + likes 30 + reports 3 per minute
        self.assertGreaterEqual(budget.total_requests * budget.read_share, rate.read_rps * 86400 * 14)
        self.assertEqual(Settings.load().rate.read_rps > 0, True)

    def test_local_budget_exhaustion_is_a_definite_failure(self):
        from ctn.client.pacing import HttpPacer
        store = StateStore(); self.addCleanup(store.close)
        pacer = HttpPacer(store, RateConfig(), BudgetConfig(total_requests=10, read_share=0.5), sleep=lambda s: None)
        obj = SimpleNamespace(_request=lambda method, path, body=None: {'ok': True}, _account=lambda s: s)
        pacer.attach(obj)
        store.set_counter('http_write', 5)
        with self.assertRaises(PlatformError) as ctx:
            obj._request('POST', '/posts/1/like')
        self.assertEqual((ctx.exception.status, ctx.exception.retryable), (409, False))


class ScriptedReads(MockLegitClient):
    def __init__(self):
        super().__init__(failure_rate=0, latency_ms=(0, 0))
        self.reads = []

    async def trending(self, *, limit=50):
        self.reads.append('trending'); return []

    async def get_feed(self, account, *, limit=50, cursor=None):
        self.reads.append('feed'); return []

    async def search(self, query, *, limit=50, cursor=None):
        self.reads.append('search'); return []

    async def get_mentions(self, account, *, limit=50, cursor=None):
        self.reads.append('mentions'); return []


class CrawlerReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_popular_tags_is_read_at_most_once_a_minute(self):
        client = ScriptedReads()
        store = StateStore(); self.addCleanup(store.close)
        act = Actuator(client, build_gate(RateConfig(read_rps=1000, write_rps=1000, per_account_read_rps=1000)),
                       build_ledger(BudgetConfig()), store=store)
        crawler = FrontlineCrawler(act, store, feed_account='a', accounts=['a', 'b'], keywords=('h2d2',),
                                   min_feed_interval_s=6.5)
        for i in range(100):
            await crawler.step(now=i * 1.5)          # 150 seconds of 1.5 s ticks
        self.assertLessEqual(client.reads.count('trending'), 3)
        self.assertGreater(client.reads.count('mentions'), 30)

    async def test_nothing_due_means_no_read(self):
        client = ScriptedReads()
        store = StateStore(); self.addCleanup(store.close)
        act = Actuator(client, build_gate(RateConfig(read_rps=1000, write_rps=1000)), build_ledger(BudgetConfig()), store=store)
        crawler = FrontlineCrawler(act, store, feed_account='', accounts=[], keywords=(), min_feed_interval_s=6.5)
        sources = [(await crawler.step(now=t)).source for t in (0.0, 1.5, 3.0)]
        self.assertEqual(sources, ['feed', 'trending', 'idle'])
        self.assertEqual(client.reads, ['feed', 'trending'])

    async def test_free_text_search_does_not_reread_the_feed_and_misses_are_cached(self):
        class Social(FakeOfficialLegitSocial):
            def _request(self, method, path, body=None):
                if path.startswith('/tags?'):
                    self.calls.append((method, path, body))
                    return {'items': list(self.tags_db), 'next_cursor': None}
                return super()._request(method, path, body)
        social = Social()
        client = OfficialLegitClient(social=social)
        self.assertEqual(list(await client.search('vaccine', limit=10)), [])
        first = len(social.calls)
        self.assertFalse(any(path.startswith('/posts?') for _, path, _ in social.calls))
        self.assertEqual(list(await client.search('vaccine', limit=10)), [])
        self.assertEqual(len(social.calls), first)


class HighPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.router = bound_router(self.store, ('yes_001', 'yes_002', 'yes_010'))
        self.high = {'action_decision': {'required_account_level': 'high'}}

    def test_order_is_high_tier_then_maintenance_then_experienced(self):
        accounts = [p.account_id for p in self.router.registry.list_all() if p.account_id]
        self.store.log_interaction(engine='micro', account_id=accounts[2], action='reply', target_post_id='x', text='Earlier reply')
        route = self.router.route(self.high, 'clinical trial evidence')
        self.assertEqual((route.account_id, route.reason.split(':')[0]), (accounts[2], 'high_fallback_standard'))
        with self.store.batch_transaction():
            self.store._conn.execute('INSERT OR IGNORE INTO account_lifecycle(account_id) VALUES(?)', (accounts[1],))
            self.store._conn.execute("UPDATE account_lifecycle SET initialise_status='ready',maintenance=1 WHERE account_id=?", (accounts[1],))
        route = self.router.route(self.high, 'clinical trial evidence')
        self.assertEqual((route.account_id, route.reason.split(':')[0]), (accounts[1], 'high_fallback_maintenance'))
        self.store.set_account_policy(accounts[0], capability_tier='high')
        route = self.router.route(self.high, 'clinical trial evidence')
        self.assertEqual((route.account_id, route.reason.split(':')[0]), (accounts[0], 'high_tier'))

    def test_send_gate_no_longer_requires_high_tier(self):
        account = self.router.registry.get('yes_001').account_id
        meta = {**self.router.context_for(self.router.registry.get('yes_001')), 'required_account_level': 'high'}
        ident, reason = self.store.claim_action('reply', account, 'target', 'A careful sourced reply.', metadata=meta)
        self.assertIsNotNone(ident, reason)
        self.store.start_action(ident)
        self.assertEqual(self.store.guard_wire_action(ident), (True, 'ready'))


class FakeBrain:
    def __init__(self, store, rationale, raw):
        self.router = SimpleNamespace(store=store, registry=SimpleNamespace(catalog_hash='', list_all=lambda: []))
        self.rationale, self.raw = rationale, raw

    async def assess(self, post):
        return None

    async def decide(self, post, *, commit_state=False, **kwargs):
        return Decision(uuid.uuid4().hex[:12], post.post_id, Tier.OBSERVE, 'bot_default',
                        JevAnswers(answers={}, raw=dict(self.raw), ok=True), self.rationale, ActionKind.NONE,
                        disposition='retry')


class BoundedRetryTests(unittest.IsolatedAsyncioTestCase):
    async def run_cycles(self, rationale, raw, cycles=3):
        store = StateStore(); self.addCleanup(store.close)
        act = Actuator(MockLegitClient(failure_rate=0, latency_ms=(0, 0)), build_gate(RateConfig(read_rps=1000, write_rps=1000)),
                       build_ledger(BudgetConfig()), dry_run=True, store=store)
        loop = BotLoop(act, side=Side.YES, accounts=['bot_001'], store=store, brain=FakeBrain(store, rationale, raw))
        store.upsert_observed_post(Post('p1', 'citizen', 'Which venues need proof?', time.time()))
        for _ in range(cycles):
            await loop.process_pending()
            with store.batch_transaction():
                store._conn.execute("UPDATE post_tasks SET next_attempt_at=0 WHERE status='queued'")
        return store._conn.execute('SELECT status,attempts FROM post_tasks').fetchone()

    async def test_no_eligible_account_stops_after_two_retries(self):
        row = await self.run_cycles('all_accounts_disabled', {}, cycles=5)
        # One evaluation plus two retries, then the task is dropped instead of re-running JEV until expiry.
        self.assertEqual((row['status'], row['attempts']), ('skipped', 3))

    async def test_cooldown_waits_for_the_shortest_remaining_cooldown(self):
        store = StateStore(); self.addCleanup(store.close)
        act = Actuator(MockLegitClient(failure_rate=0, latency_ms=(0, 0)), build_gate(RateConfig(read_rps=1000, write_rps=1000)),
                       build_ledger(BudgetConfig()), dry_run=True, store=store)
        loop = BotLoop(act, side=Side.YES, accounts=['bot_001'], store=store,
                       brain=FakeBrain(store, 'account_cooldown:bot_001', {'cooldown_remaining_s': 120}))
        store.upsert_observed_post(Post('p1', 'citizen', 'Which venues need proof?', time.time()))
        before = time.time()
        await loop.process_pending()
        row = store._conn.execute('SELECT status,next_attempt_at FROM post_tasks').fetchone()
        self.assertEqual(row['status'], 'queued')
        self.assertGreaterEqual(row['next_attempt_at'], before + 115)


if __name__ == '__main__':
    unittest.main()
