"""Originals pacing by campaign day, bounded retries inside a slot, P00 opening questions, daily posts."""
import json
import re
import time
import unittest
from contextlib import closing
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

from ctn.client.mock import MockLegitClient
from ctn.jev.postflight import PURPOSE_RULES, PostflightVerdict
from ctn.macro import schedule
from ctn.storage import StateStore
from ctn.storage.lifecycle import SYDNEY
from tests.test_persona80 import bound_router
from tests.test_runtime_reliability import actor


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=SYDNEY).timestamp()


TIERS = {'tiers': [{'from_day': 0, 'per_account': 2}, {'from_day': 2, 'per_account': 3}]}


@patch.dict(schedule.POLICY, TIERS)
class ScheduleTests(unittest.TestCase):
    def test_campaign_days_and_red_multipliers(self):
        self.assertEqual(schedule.campaign_day(at('2026-10-19T09:00:00')), 0)
        self.assertEqual(schedule.campaign_day(at('2026-10-25T23:59:00')), 6)
        self.assertEqual([schedule.red_multiplier(at(d)) for d in ('2026-10-25T12:00:00', '2026-10-26T00:00:00', '2026-11-01T00:00:00')],
                         [1, 2, 4])

    def test_limits_scale_with_accounts_and_ramp_from_previous_evening(self):
        cases = {'2026-10-19T12:00:00': 2, '2026-10-20T17:59:00': 2, '2026-10-20T18:00:00': 3, '2026-11-02T12:00:00': 3}
        self.assertEqual({k: schedule.per_account_quota(at(k)) for k in cases}, cases)
        self.assertEqual(schedule.scheduled_limit(at('2026-10-19T12:00:00'), 50), 100)
        self.assertEqual(schedule.scheduled_limit(at('2026-10-19T12:00:00'), 0), 0)
        with patch.dict(schedule.POLICY, {'tiers': [{'from_day': 0, 'per_account': 9}]}):
            self.assertEqual(schedule.per_account_quota(at('2026-10-19T12:00:00')), 4)

    def test_only_ready_active_bound_accounts_count(self):
        with closing(StateStore()) as store:
            bound_router(store, ('yes_001', 'yes_002', 'yes_003'))
            accounts = list(store.persona_bindings().values())
            with store.batch_transaction():
                for account in accounts[:2]:
                    store._conn.execute("INSERT INTO account_lifecycle(account_id,initialise_status,initialised_at) VALUES(?,'ready',1)", (account,))
            store.set_account_policy(accounts[1], mode='standby')
            self.assertEqual(schedule.posting_accounts(store), 1)
            self.assertEqual(schedule.effective_limit(store, at('2026-10-19T12:00:00')), 2)

    def test_account_gap_is_random_between_one_and_three_hours(self):
        gaps = [schedule.original_gap() for _ in range(200)]
        self.assertTrue(all(3600 <= g <= 10800 for g in gaps))
        self.assertGreater(len({round(g) for g in gaps}), 150)

    @patch.dict(schedule.POLICY, {'active_hours': [0, 24]})
    def test_slots_divide_the_day_by_the_limit(self):
        suffix, start, end = schedule.slot(at('2026-10-26T10:30:00'), 24)
        self.assertEqual(suffix, '2026-10-26:24:10')
        self.assertEqual((start, end - start), (at('2026-10-26T10:00:00'), 3600))
        self.assertNotEqual(schedule.slot(at('2026-10-26T10:30:00'), 32)[0], suffix)
        with patch.dict(schedule.POLICY, {'active_hours': [7, 23]}):
            self.assertIsNone(schedule.slot(at('2026-10-26T05:00:00'), 24))
            self.assertAlmostEqual(schedule.slot(at('2026-10-26T08:00:00'), 16)[2] - schedule.slot(at('2026-10-26T08:00:00'), 16)[1], 3600)

    def test_opening_questions_dominate_the_first_days_then_stop(self):
        day0 = [schedule.purpose_for(f'2026-10-19:10:{i}', at('2026-10-19T12:00:00')) for i in range(400)]
        self.assertTrue(0.7 < day0.count('P00') / 400 < 0.9)
        day3 = {schedule.purpose_for(f'2026-10-22:16:{i}', at('2026-10-22T12:00:00')) for i in range(100)}
        self.assertEqual(day3, {'P01'})
        self.assertEqual(schedule.purpose_for('x:1', at('2026-10-19T12:00:00')), schedule.purpose_for('x:1', at('2026-10-19T12:00:00')))

    def test_operator_can_fix_the_limit_and_restore_the_schedule(self):
        with closing(StateStore()) as store, patch.object(schedule, 'posting_accounts', return_value=8):
            ts = at('2026-10-26T12:00:00')
            self.assertEqual(schedule.effective_limit(store, ts), 24)
            store.configure_campaign(12)
            self.assertEqual(schedule.effective_limit(store, ts), 12)
            store.set_policy('originals_schedule', True)
            self.assertEqual(schedule.effective_limit(store, ts), 24)


class SlotJobTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.t = at('2026-10-26T10:05:00')
        # 8 accounts x 3 = 24 slots a day; no start jitter unless a test asks for it.
        for p in (patch.object(schedule, 'posting_accounts', return_value=8), patch.dict(schedule.POLICY, {'slot_jitter': 0.0, 'active_hours': [0, 24], **TIERS})):
            p.start(); self.addCleanup(p.stop)

    def test_first_attempt_waits_for_a_stable_random_point_early_in_the_slot(self):
        with patch.dict(schedule.POLICY, {'slot_jitter': 0.6}):
            delay = schedule.slot_start_delay('2026-10-26:24:10', 0, 3600)
            self.assertTrue(0 <= delay <= 0.6 * 3600)
            self.assertEqual(delay, schedule.slot_start_delay('2026-10-26:24:10', 0, 3600))
            self.assertNotEqual(delay, schedule.slot_start_delay('2026-10-26:24:11', 0, 3600))
            start = at('2026-10-26T10:00:00')
            self.assertIsNone(self.store.claim_macro_job(now=start))
            due = self.store._conn.execute('SELECT next_attempt_at FROM macro_jobs').fetchone()[0]
            self.assertAlmostEqual(due, start + delay)
            self.assertEqual(self.store.claim_macro_job(now=due)['id'], 'macro:live:2026-10-26:24:10')

    def test_no_originals_before_seven(self):
        with patch.dict(schedule.POLICY, {'active_hours': [7, 24]}):
            self.assertIsNone(self.store.claim_macro_job(now=at('2026-10-26T06:59:00')))
            self.assertIsNotNone(self.store.claim_macro_job(now=at('2026-10-26T07:00:00')))

    def test_claim_creates_one_job_per_slot_and_expires_it_at_slot_end(self):
        job = self.store.claim_macro_job(now=self.t)
        self.assertEqual(job['id'], 'macro:live:2026-10-26:24:10')
        self.assertEqual(job['slot_end'], at('2026-10-26T11:00:00'))
        self.assertEqual(job['purpose'], 'P01')
        self.assertIsNone(self.store.claim_macro_job(now=self.t + 60))
        self.store.retry_macro_job(job, 'no_eligible_account', now=self.t)
        self.store.claim_macro_job(now=at('2026-10-26T11:00:01'))
        status = self.store._conn.execute('SELECT status FROM macro_jobs WHERE id=?', (job['id'],)).fetchone()[0]
        self.assertEqual(status, 'expired')

    def test_retries_are_bounded_spaced_and_change_persona(self):
        job = self.store.claim_macro_job(now=self.t)
        self.assertEqual(self.store.retry_macro_job(job, 'postflight', tried=['yes_001'], now=self.t), 'retry')
        self.assertIsNone(self.store.claim_macro_job(now=self.t + 599))
        second = self.store.claim_macro_job(now=self.t + 600)
        self.assertEqual((second['attempts'], json.loads(second['tried_json'])), (1, ['yes_001']))
        self.assertEqual(self.store.retry_macro_job(second, 'postflight', tried=['yes_001', 'yes_002'], now=self.t + 600), 'retry')
        third = self.store.claim_macro_job(now=self.t + 1200)
        self.assertEqual(self.store.retry_macro_job(third, 'postflight', now=self.t + 1200), 'skipped')

    def test_nothing_to_say_is_retried_only_once(self):
        job = self.store.claim_macro_job(now=self.t)
        self.assertEqual(self.store.retry_macro_job(job, 'pi_observe', observe=True, now=self.t), 'retry')
        again = self.store.claim_macro_job(now=self.t + 600)
        self.assertEqual(self.store.retry_macro_job(again, 'pi_observe', observe=True, now=self.t + 600), 'skipped')

    def test_no_retry_that_would_land_after_the_slot(self):
        job = self.store.claim_macro_job(now=at('2026-10-26T10:55:00'))
        self.assertEqual(self.store.retry_macro_job(job, 'postflight', now=at('2026-10-26T10:55:00')), 'skipped')

    def test_status_reports_day_multiplier_limit_and_slot_outcomes(self):
        job = self.store.claim_macro_job(now=self.t)
        self.store.finish_macro_job(job, 'skipped', reason='pi_observe')
        status = self.store.originals_status(now=self.t)
        self.assertEqual((status['campaign_day'], status['red_multiplier'], status['limit'], status['slot_minutes']), (7, 2, 24, 60.0))
        self.assertEqual(status['slots_today'], {'skipped:pi_observe': 1})


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from ctn.macro.worker import MacroWorker
        from ctn.rag.corpus import Corpus
        # Any time of day, first attempt at slot start.
        p = patch.dict(schedule.POLICY, {'slot_jitter': 0.0, 'active_hours': [0, 24]}); p.start(); self.addCleanup(p.stop)
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.router = bound_router(self.store, ('yes_001', 'yes_002'))
        for pid in ('yes_001', 'yes_002'):
            account = self.router.registry.get(pid).account_id
            with self.store.batch_transaction():
                self.store._conn.execute("INSERT INTO account_lifecycle(account_id,initialise_status,initialised_at) VALUES(?,'ready',?)", (account, time.time()))
        corpus = Corpus(); corpus.install_sources(self.store)
        self.act = actor(MockLegitClient(failure_rate=0, latency_ms=(0, 0)), self.store)
        self.checker = Mock(); self.checker.check.return_value = PostflightVerdict(True, 'approved')
        self.runner = AsyncMock()
        collector = Mock(); collector.collect = AsyncMock(return_value={'trending_posts': [
            {'text': 'What evidence supports vaccination-proof requirements for public spaces?'}]})
        self.worker = MacroWorker(self.store, self.act, self.router, self.checker, corpus, pi_runner=self.runner, collector=collector)

    async def run_slot(self, purpose):
        with patch('ctn.macro.schedule.purpose_for', return_value=purpose):
            return await self.worker.step()

    async def test_nothing_to_say_retries_once_with_another_persona(self):
        self.runner.return_value = {'act': False, 'reason': 'nothing supported'}
        first = await self.run_slot('P01')
        self.assertEqual(first['status'], 'retry')
        with self.store.batch_transaction():
            self.store._conn.execute('UPDATE macro_jobs SET next_attempt_at=0')
        second = await self.run_slot('P01')
        self.assertEqual(second['status'], 'skipped')
        personas = [c.args[0]['persona']['persona_id'] for c in self.runner.await_args_list]
        self.assertEqual(len(set(personas)), 2)
        self.assertEqual(self.runner.await_args_list[1].args[0]['assignment']['attempt'], 2)

    async def test_opening_question_is_published_without_strict_alignment(self):
        self.runner.return_value = {'act': True, 'purpose': 'P00', 'branch': 'branch_unknown',
                                    'text': 'Running a depot means checking a lot of things at the door already. How would people want venue entry checks to work day to day?'}
        result = await self.run_slot('P00')
        self.assertTrue(result.get('ok'), result)
        payload = self.runner.await_args.args[0]
        self.assertEqual(payload['assignment']['purpose'], 'P00')
        self.assertIn('recent_team_originals', payload)
        kwargs = self.checker.check.call_args.kwargs
        self.assertFalse(kwargs['strict_alignment'])
        self.assertEqual(kwargs['intended_purpose'], PURPOSE_RULES['P00'])
        meta = json.loads(self.store._conn.execute("SELECT metadata_json FROM action_intents WHERE kind='post'").fetchone()[0])
        self.assertEqual(meta['purpose'], 'P00')

    async def test_rejected_draft_is_retried_with_a_different_persona(self):
        self.runner.return_value = {'act': True, 'purpose': 'P01', 'branch': 'branch_unknown',
                                    'text': 'I support the shared entry requirement because clinics here are small and fill up fast.'}
        self.checker.check.return_value = PostflightVerdict(False, 'purpose_drifted')
        self.assertEqual((await self.run_slot('P01'))['status'], 'retry')
        row = self.store._conn.execute("SELECT attempts,tried_json FROM macro_jobs WHERE status='queued'").fetchone()
        self.assertEqual(row['attempts'], 1)
        self.assertEqual(len(json.loads(row['tried_json'])), 1)

    async def test_an_account_posts_at_most_four_originals_a_day(self):
        first = self.router.registry.get('yes_001').account_id
        with self.store.batch_transaction():
            for i in range(4):
                self.store._conn.execute("INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,created_at,updated_at,metadata_json) "
                    "VALUES(?,?,'post',?,'',?,'succeeded','',0,?,?,?)", (f'o{i}', f'o{i}', first, f'Original {i}', time.time(), time.time(), json.dumps({'purpose': 'P01'})))
        self.runner.return_value = {'act': False, 'reason': 'nothing supported'}
        with patch('ctn.macro.schedule.posting_accounts', return_value=10):
            await self.run_slot('P01')
        self.assertEqual(self.runner.await_args.args[0]['persona']['persona_id'], 'yes_002')

    async def test_pi_contract_requires_the_assigned_purpose(self):
        from ctn.macro import worker as module
        process = Mock(returncode=0); process.communicate = AsyncMock(return_value=(json.dumps(
            {'act': True, 'purpose': 'P01', 'text': 'hello'}).encode(), b''))
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            with self.assertRaises(ValueError):
                await module.run_pi({'assignment': {'purpose': 'P00'}})
            self.assertTrue((await module.run_pi({'assignment': {'purpose': 'P01'}}))['act'])


class DailyPostTests(unittest.TestCase):
    def test_everyday_posts_are_varied_and_off_topic(self):
        from ctn.persona.warmup_seeds import _EXTRA, list_warmup_seeds
        catalog = json.load(open('config/personas/yes.v1.json'))['personas']
        forbidden = re.compile(r'h2d2|vaccin|referend|mandate|\bvote\b|election|president|government|\bIHO\b|MOUK|KITB', re.I)
        texts = [t for p in catalog for t in _EXTRA[p['persona_id']]]
        self.assertEqual(len(texts), 4 * len(catalog))
        self.assertEqual(len(set(texts)), len(texts))
        self.assertFalse([t for t in texts if forbidden.search(t) or len(t) > 280])
        self.assertFalse([t for t in texts if t.startswith('I ')])
        for p in catalog:
            seeds = list_warmup_seeds(p['persona_id'])
            self.assertEqual(len(seeds), 6)
            self.assertTrue(any('?' in t for t in seeds[:4]), p['persona_id'])
        openings = {}
        for t in texts:
            key = ' '.join(t.split()[:2]).lower(); openings[key] = openings.get(key, 0) + 1
        self.assertLessEqual(max(openings.values()), 6)


if __name__ == '__main__':
    unittest.main()
