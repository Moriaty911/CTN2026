"""Own-publication counter read-back.

Our own posts never appear in the crawl stream with counters (the crawler only mirrors feed rows of
other accounts, and `/posts/{id}` returns none), so `own_post_metrics` polls `/posts/{id}/counters`
at fixed ages. Without it, amplify pushes were tuned blind.
"""

import time
import unittest

from ctn.actuator import Actuator
from ctn.client.mock import MockLegitClient
from ctn.config import BudgetConfig, RateConfig
from ctn.pipeline.refresh import OWN_METRIC_RULE, own_metric_step, own_metrics_summary
from ctn.ratelimit import build_gate, build_ledger
from ctn.storage import StateStore
from ctn.storage.runtime import OWN_METRIC_STAGES

FAST = RateConfig(read_rps=1000, write_rps=1000, per_account_read_rps=1000, per_account_write_rps=1000)


async def no_sleep(_seconds):
    return None


class CountingClient:
    def __init__(self, counters=None):
        self.counters = counters or {'likes': 4, 'replies': 1, 'reposts': 2, 'views': 55}
        self.reads: list[str] = []

    async def get_counters(self, post_id):
        self.reads.append(post_id)
        return dict(self.counters)


class OwnMetricStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(':memory:')
        self.addCleanup(self.store.close)

    def test_queue_is_idempotent_and_not_due_before_the_first_stage(self):
        self.assertTrue(self.store.queue_own_metric('p1', 'acct', now=1000.0))
        self.assertFalse(self.store.queue_own_metric('p1', 'acct', now=1000.0))
        self.assertEqual(self.store.claim_own_metrics(5, now=1000.0 + OWN_METRIC_STAGES[0] - 1.0), [])
        rows = self.store.claim_own_metrics(5, now=1000.0 + OWN_METRIC_STAGES[0])
        self.assertEqual([(r['post_id'], r['stage']) for r in rows], [('p1', 0)])

    def test_claim_leases_the_row_so_a_slow_read_is_not_claimed_twice(self):
        self.store.queue_own_metric('p1', now=0.0)
        due = float(OWN_METRIC_STAGES[0])
        self.assertEqual(len(self.store.claim_own_metrics(5, now=due)), 1)
        self.assertEqual(self.store.claim_own_metrics(5, now=due + 1.0), [])
        self.assertEqual(len(self.store.claim_own_metrics(5, now=due + 1000.0)), 1)

    def test_every_stage_advances_and_the_last_one_finishes(self):
        self.store.queue_own_metric('p1', now=0.0)
        due = float(OWN_METRIC_STAGES[0])
        for stage in range(len(OWN_METRIC_STAGES)):
            rows = self.store.claim_own_metrics(1, now=due)
            self.assertEqual(rows[0]['stage'], stage)
            due = self.store.finish_own_metric(rows[0], {}, now=due)
        self.assertEqual(due, 0.0)
        row = self.store.own_metric_rows(1)[0]
        self.assertTrue(row['done'])
        self.assertEqual(row['reads'], len(OWN_METRIC_STAGES))
        self.assertEqual(self.store.claim_own_metrics(5, now=1e9), [])

    def test_counters_are_kept_and_mirrored_into_the_observed_post(self):
        self.store.upsert_observed_post({'post_id': 'p1', 'author': 'bot_001', 'text': 'original', 'created_at': 0.0})
        self.store.queue_own_metric('p1', 'bot_001', now=0.0)
        row = self.store.claim_own_metrics(1, now=float(OWN_METRIC_STAGES[0]))[0]
        self.store.finish_own_metric(row, {'likes': 9, 'replies': 3, 'reposts': 5, 'views': 120}, now=90.0)
        metrics = self.store.own_metric_rows(1)[0]
        self.assertEqual((metrics['likes'], metrics['replies'], metrics['reposts'], metrics['views']), (9, 3, 5, 120))
        self.assertEqual(self.store.get_observed_post('p1')['likes'], 9)


class OwnMetricStepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = StateStore(':memory:')
        self.addCleanup(self.store.close)
        self.client = CountingClient()
        self.actuator = Actuator(self.client, build_gate(FAST), build_ledger(BudgetConfig()))

    async def test_step_reads_due_posts_and_schedules_the_next_stage(self):
        self.store.queue_own_metric('p1', 'bot_001', now=0.0)
        done = await own_metric_step(self.actuator, self.store, now=float(OWN_METRIC_STAGES[0]), sleep=no_sleep)
        self.assertEqual(done, 1)
        self.assertEqual(self.client.reads, ['p1'])
        self.assertEqual(self.store.own_metric_rows(1)[0]['likes'], 4)

    async def test_per_minute_budget_caps_the_reads(self):
        for n in range(10):
            self.store.queue_own_metric(f'p{n}', now=0.0)
        due = float(OWN_METRIC_STAGES[0])
        reads = 0
        for _ in range(4):
            reads += await own_metric_step(self.actuator, self.store, now=due, sleep=no_sleep)
        self.assertEqual(reads, OWN_METRIC_RULE['per_minute'])
        self.assertEqual(len(self.client.reads), OWN_METRIC_RULE['per_minute'])

    async def test_summary_reports_stage_and_trending_flag(self):
        self.store.queue_own_metric('p1', now=0.0)
        await own_metric_step(self.actuator, self.store, now=float(OWN_METRIC_STAGES[0]), sleep=no_sleep)
        entry = own_metrics_summary(self.store, 5)['originals'][0]
        self.assertEqual(entry['post_id'], 'p1')
        self.assertEqual(entry['stage'], 1)
        self.assertFalse(entry['trending'])


class OwnMetricQueueHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_successful_original_is_queued_for_read_back(self):
        store = StateStore(':memory:')
        self.addCleanup(store.close)
        actuator = Actuator(MockLegitClient(failure_rate=0.0), build_gate(FAST), build_ledger(BudgetConfig()),
                            dry_run=False, store=store)
        result = await actuator.post('bot_001', 'Proof beats a lockdown promise #ProofKeepsUsOpen')
        self.assertEqual(result.status, 'succeeded')
        post_id = result.response.get('post_id')
        self.assertTrue(post_id)
        rows = store.claim_own_metrics(5, now=time.time() + OWN_METRIC_STAGES[0] + 1.0)
        self.assertEqual([r['post_id'] for r in rows], [post_id])

    async def test_a_simulated_write_is_not_queued(self):
        store = StateStore(':memory:')
        self.addCleanup(store.close)
        actuator = Actuator(MockLegitClient(failure_rate=0.0), build_gate(FAST), build_ledger(BudgetConfig()),
                            dry_run=True, store=store)
        result = await actuator.post('bot_001', 'Dry run only #ProofNotLockdown')
        self.assertEqual(result.status, 'simulated')
        self.assertEqual(store.own_metric_rows(5), [])


if __name__ == '__main__':
    unittest.main()
