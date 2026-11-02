"""Uncertain sends: wire marker, shutdown drain, bounded account holds, reconciliation, abandonment."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ctn.client.base import PlatformError, ReadFailurePage
from ctn.client.legit_v1 import NormalizedPage
from ctn.client.mock import MockLegitClient
from ctn.client.pacing import CURRENT_ACTION, HttpPacer
from ctn.config import BudgetConfig, RateConfig
from ctn.reconcile import alert_unknown_burst, check_unknown, reconcile_unknown_posts
from ctn.storage import StateStore
from ctn.storage.runtime import SEND_RECOVERY
from tests.test_runtime_reliability import actor

HOLD = SEND_RECOVERY['unknown_account_hold_seconds']


def claim_reply(store, account='a', target='t1', text='A careful synthetic reply.', author='citizen'):
    store.upsert_observed_post({'post_id': target, 'author': 'citizen', 'text': 'Question?', 'created_at': time.time()})
    ident, reason = store.claim_action('reply', account, target, text, metadata={'target_author': author, 'root_post_id': target})
    assert ident, reason
    with store.batch_transaction():
        store._conn.execute("UPDATE post_tasks SET status='unknown' WHERE post_id=?", (target,))
    return ident


def task(store, pid):
    return store._conn.execute('SELECT status,reason FROM post_tasks WHERE post_id=?', (pid,)).fetchone()


def leased(store, account, at):
    return store._conn.execute('SELECT 1 FROM account_leases WHERE account_id=? AND lease_until>?', (account, at)).fetchone() is not None


class FakeSdk:
    def __init__(self):
        self.sent = []

    def _request(self, method, path, body=None):
        self.sent.append((method, path)); return {'id': 1}

    def _account(self, *a, **k):
        return None


class WireMarkerTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.pacer = HttpPacer(self.store, RateConfig(read_rps=100, write_rps=100, per_account_read_rps=100, per_account_write_rps=100),
                               BudgetConfig())
        self.sdk = FakeSdk(); self.pacer.attach(self.sdk)

    def send(self, ident):
        token = CURRENT_ACTION.set(ident)
        try:
            return self.sdk._request('POST', '/posts', {})
        finally:
            CURRENT_ACTION.reset(token)

    def test_wire_at_is_recorded_when_the_request_leaves(self):
        ident = claim_reply(self.store)
        self.store.start_action(ident, wire_tracked=True)
        self.assertIsNone(self.store.get_action(ident)['wire_at'])
        self.send(ident)
        self.assertIsNotNone(self.store.get_action(ident)['wire_at'])

    def test_draining_client_refuses_writes_and_leaves_no_wire_mark(self):
        ident = claim_reply(self.store)
        self.store.start_action(ident, wire_tracked=True)
        self.pacer.close()
        with self.assertRaises(PlatformError) as caught:
            self.send(ident)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.sdk.sent, [])
        self.assertIsNone(self.store.get_action(ident)['wire_at'])

    def test_a_send_cancelled_elsewhere_is_never_transmitted_late(self):
        ident = claim_reply(self.store)
        self.store.start_action(ident, wire_tracked=True)
        self.store.finish_action(ident, 'unknown', detail='cancelled during send')
        with self.assertRaises(PlatformError):
            self.send(ident)
        self.assertEqual(self.sdk.sent, [])

    def test_untracked_clients_count_as_on_the_wire_from_the_start(self):
        ident = claim_reply(self.store)
        self.store.start_action(ident)
        self.assertIsNotNone(self.store.get_action(ident)['wire_at'])


class HoldAndAbandonTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)

    def test_unknown_without_wire_is_a_definite_failure(self):
        ident = claim_reply(self.store)
        self.store.start_action(ident, wire_tracked=True)
        self.store.finish_action(ident, 'unknown', detail='cancelled during send')
        row = self.store.get_action(ident)
        self.assertEqual(row['status'], 'failed'); self.assertIn('never reached the wire', row['detail'])
        self.assertFalse(leased(self.store, 'a', time.time()))
        self.assertEqual(task(self.store, 't1')['status'], 'queued')

    def test_dead_owner_before_wire_fails_after_short_lease(self):
        ident = claim_reply(self.store)
        self.store.start_action(ident, wire_tracked=True)
        lease = self.store.get_action(ident)['lease_until']
        self.assertLessEqual(lease - time.time(), SEND_RECOVERY['send_lease_seconds'] + 1)
        self.store.recover_actions(now=lease + 1)
        self.assertEqual(self.store.get_action(ident)['status'], 'failed')
        self.assertEqual(task(self.store, 't1')['status'], 'queued')
        self.assertFalse(leased(self.store, 'a', lease + 1))

    def test_dead_owner_after_wire_holds_account_for_bounded_time_then_abandons(self):
        ident = claim_reply(self.store)
        self.store.start_action(ident, wire_tracked=True)
        with self.store.batch_transaction():
            self.store._conn.execute('UPDATE action_intents SET wire_at=? WHERE id=?', (time.time(), ident))
        t = self.store.get_action(ident)['lease_until'] + 1
        self.store.recover_actions(now=t)
        self.assertEqual(self.store.get_action(ident)['status'], 'unknown')
        self.assertTrue(leased(self.store, 'a', t + HOLD - 60))
        self.assertEqual(task(self.store, 't1')['status'], 'unknown')
        self.store.recover_actions(now=t + HOLD + 1)
        self.assertFalse(leased(self.store, 'a', t + HOLD + 1))
        self.assertEqual(tuple(task(self.store, 't1')), ('skipped', 'unknown_abandoned'))
        check = self.store._conn.execute('SELECT state FROM unknown_checks WHERE action_id=?', (ident,)).fetchone()[0]
        self.assertEqual(check, 'abandoned')
        self.assertEqual(self.store.get_action(ident)['status'], 'unknown')      # still counts toward caps

    def test_abandoned_initialise_post_returns_to_pending(self):
        ident, _ = self.store.claim_action('post', 'n1', '', 'Hello from a synthetic neighbour.', metadata={'purpose': 'initialise'})
        self.store.start_action(ident)
        self.store.finish_action(ident, 'unknown', detail='network response unavailable')
        self.assertEqual(self.store.lifecycle('n1')['initialise_status'], 'unknown')
        self.store.recover_actions(now=time.time() + HOLD + 5)
        self.assertEqual(self.store.lifecycle('n1')['initialise_status'], 'pending')

    def test_uncertain_report_stops_occupying_pending_capacity(self):
        from tests.test_runtime_reliability import evidence
        evidence(self.store); evidence(self.store, 'other', 'q')
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO action_intents(id,action_key,kind,account_id,target_id,status,owner,lease_until,created_at,updated_at,metadata_json) "
                                     "VALUES('r1','k1','report','a','p','unknown','x',0,?,?,'{\"target_author\":\"enemy\"}')", (time.time(), time.time()))
            self.store._conn.execute("INSERT INTO report_attempts VALUES('ra','r1','post','p','enemy','','unknown',0,0,'','{}')")
        self.store.set_policy('max_pending_reports', 1)
        meta = {'target_author': 'other', 'detail': 'x'}
        self.assertEqual(self.store.claim_action('report', 'a', 'p2', metadata=meta)[1], 'pending_team_limit')
        with self.store.batch_transaction():
            self.store._conn.execute("UPDATE action_intents SET updated_at=? WHERE id='r1'", (time.time() - SEND_RECOVERY['report_unknown_release_seconds'] - 1,))
        self.store.recover_actions()
        self.assertEqual(self.store._conn.execute("SELECT status FROM report_attempts WHERE id='ra'").fetchone()[0], 'unknown_released')
        self.assertNotEqual(self.store.claim_action('report', 'a', 'p2', metadata=meta)[1], 'pending_team_limit')
        self.assertEqual(self.store.claim_action('report', 'b', 'p', metadata={'target_author': 'enemy', 'detail': 'x'})[1],
                         'report_target_already_claimed')


class Pages:
    """Scripted reads: thread pages and account-history pages."""
    def __init__(self, thread=None, history=None):
        self.thread, self.history, self.calls = thread or [], history or [], []

    async def get_comments(self, post_id, *, limit=50, cursor=None):
        self.calls.append(('thread', cursor)); return self._page(self.thread, cursor)

    async def get_account_posts(self, account, *, limit=50, cursor=None):
        self.calls.append(('history', cursor)); return self._page(self.history, cursor)

    @staticmethod
    def _page(pages, cursor):
        index = int(cursor or 0)
        if index >= len(pages):return NormalizedPage([])
        page = pages[index]
        if page == 'fail':return ReadFailurePage()
        return NormalizedPage(page, next_cursor=str(index + 1) if index + 1 < len(pages) else None)


class ReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.ident = claim_reply(self.store, text='Our exact reply text.')
        self.store.start_action(self.ident)
        self.store.finish_action(self.ident, 'unknown', detail='network response unavailable')
        self.t0 = self.store.get_action(self.ident)['wire_at']

    def due(self, now):
        return [r for r in self.store.due_unknown_checks(now=now) if r['action_id'] == self.ident]

    async def test_found_in_thread_succeeds(self):
        ours = {'post_id': '99', 'author': 'a', 'text': 'Our exact reply text.', 'created_at': self.t0 + 1, 'parent_id': 't1'}
        reads = Pages(thread=[[{'post_id': '5', 'author': 'x', 'text': 'other', 'created_at': self.t0 + 2, 'parent_id': 't1'}, ours]])
        self.assertEqual(await check_unknown(reads, self.store, self.due(self.t0 + 61)[0], now=self.t0 + 61), 'succeeded')
        self.assertEqual(self.store.get_action(self.ident)['status'], 'succeeded')
        self.assertEqual(task(self.store, 't1')['status'], 'succeeded')
        self.assertFalse(leased(self.store, 'a', self.t0 + 62))

    async def test_local_observation_needs_no_read(self):
        self.store.upsert_observed_post({'post_id': '77', 'author': 'a', 'text': 'Our exact reply text.', 'created_at': self.t0 + 1, 'parent_id': 't1'})
        reads = Pages()
        self.assertEqual(await check_unknown(reads, self.store, self.due(self.t0 + 61)[0], now=self.t0 + 61), 'succeeded')
        self.assertEqual(reads.calls, [])

    async def test_absence_needs_two_complete_checks_and_minimum_age(self):
        reads = Pages(thread=[[{'post_id': '5', 'author': 'x', 'text': 'other', 'created_at': self.t0 + 2, 'parent_id': 't1'}]])
        self.assertEqual(await check_unknown(reads, self.store, self.due(self.t0 + 61)[0], now=self.t0 + 61), 'wait')
        self.assertEqual(await check_unknown(reads, self.store, self.due(self.t0 + 181)[0], now=self.t0 + 181), 'wait')   # too young
        self.assertEqual(await check_unknown(reads, self.store, self.due(self.t0 + 481)[0], now=self.t0 + 481), 'failed')
        self.assertEqual(self.store.get_action(self.ident)['status'], 'failed')
        self.assertEqual(task(self.store, 't1')['status'], 'queued')       # target may be answered again
        self.assertFalse(leased(self.store, 'a', self.t0 + 482))

    async def test_failed_or_partial_reads_never_prove_absence(self):
        reads = Pages(thread=['fail'], history=['fail'])
        for at in (61, 181, 481):
            self.assertEqual(await check_unknown(reads, self.store, self.due(self.t0 + at)[0], now=self.t0 + at), 'wait')
        long = Pages(thread=[[{'post_id': str(i), 'author': 'x', 'text': 'o', 'created_at': self.t0 + 1, 'parent_id': 't1'}] for i in range(9)])
        self.assertEqual(await check_unknown(long, self.store, self.due(self.t0 + 841)[0], now=self.t0 + 841), 'wait')
        self.assertEqual(len([c for c in long.calls if c[0] == 'thread']), SEND_RECOVERY['max_pages'])
        self.assertEqual(self.store.get_action(self.ident)['status'], 'unknown')

    async def test_checks_follow_the_backoff_schedule(self):
        self.assertEqual(self.due(self.t0 + 30), [])
        reads = Pages(thread=['fail'], history=['fail'])
        await check_unknown(reads, self.store, self.due(self.t0 + 61)[0], now=self.t0 + 61)
        self.assertEqual(self.due(self.t0 + 120), [])
        self.assertTrue(self.due(self.t0 + 181))

    async def test_root_post_absence_is_proved_by_account_history(self):
        store = StateStore(); self.addCleanup(store.close)
        ident, _ = store.claim_action('post', 'b', '', 'A synthetic original.', metadata={})
        store.start_action(ident); store.finish_action(ident, 'unknown')
        t0 = store.get_action(ident)['wire_at']
        reads = Pages(history=[[{'post_id': '1', 'author': 'b', 'text': 'older', 'created_at': t0 - 100}]])
        for at in (61, 481):
            row = [r for r in store.due_unknown_checks(now=t0 + at) if r['action_id'] == ident][0]
            outcome = await check_unknown(reads, store, row, now=t0 + at)
        self.assertEqual(outcome, 'failed')

    async def test_late_success_after_abandonment_is_still_recorded(self):
        self.store.recover_actions(now=self.t0 + HOLD + 1)
        ours = {'post_id': '99', 'author': 'a', 'text': 'Our exact reply text.', 'created_at': self.t0 + 1, 'parent_id': 't1'}
        late = self.t0 + HOLD + 1 + SEND_RECOVERY['late_check_every_seconds'] + 1
        self.assertEqual(await check_unknown(Pages(thread=[[ours]]), self.store, self.due(late)[0], now=late), 'succeeded')
        self.assertEqual(self.store.get_action(self.ident)['status'], 'succeeded')

    async def test_burst_alert_goes_to_discord_once_per_window(self):
        for i in range(5):
            ident = claim_reply(self.store, account=f'acct{i}', target=f'x{i}', text=f'Distinct synthetic reply number {i}.', author=f'c{i}')
            self.store.start_action(ident); self.store.finish_action(ident, 'unknown')
        self.assertGreaterEqual(alert_unknown_burst(self.store), 5)
        alert_unknown_burst(self.store)
        rows = self.store._conn.execute("SELECT COUNT(*) FROM discord_outbox WHERE event_key LIKE 'unknown_burst:%'").fetchone()[0]
        self.assertEqual(rows, 1)


class MeteredLost(MockLegitClient):
    http_metered = True

    def __init__(self, store, *, on_wire):
        super().__init__(failure_rate=0, latency_ms=(0, 0)); self.store, self.on_wire = store, on_wire

    async def post(self, account, text, **kwargs):
        if self.on_wire:
            ident = CURRENT_ACTION.get()
            with self.store.batch_transaction():
                self.store._conn.execute('UPDATE action_intents SET wire_at=? WHERE id=?', (time.time(), ident))
        raise TimeoutError('no response')


class ActuatorOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_metered_send_that_never_left_reports_failed(self):
        store = StateStore(); self.addCleanup(store.close)
        result = await actor(MeteredLost(store, on_wire=False), store).post('a', 'Synthetic original message')
        self.assertEqual(result.status, 'failed')
        self.assertFalse(leased(store, 'a', time.time()))

    async def test_metered_send_on_the_wire_stays_uncertain_for_fifteen_minutes(self):
        store = StateStore(); self.addCleanup(store.close)
        result = await actor(MeteredLost(store, on_wire=True), store).post('a', 'Synthetic original message')
        self.assertEqual(result.status, 'unknown')
        self.assertTrue(leased(store, 'a', time.time() + HOLD - 30))
        self.assertFalse(leased(store, 'a', time.time() + HOLD + 30))


class InitialiseRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_seed_rotates_and_alerts_after_two_failures(self):
        from ctn.warmup import initialise_step
        from tests.test_persona80 import bound_router
        store = StateStore(); self.addCleanup(store.close)
        router = bound_router(store, ('yes_001',))
        store.set_policy('initialise_spread',{'window_seconds':0,'min_gap_seconds':0})
        account = router.registry.get('yes_001').account_id
        texts = []

        async def publish(self, acct, text, **kwargs):
            texts.append(text); return {'ok': False, 'status': 'rejected', 'reason': 'postflight'}
        with patch('ctn.macro.publisher.StrategicPublisher.publish', publish):
            for i in range(3):
                with store.batch_transaction():
                    store._conn.execute('UPDATE account_lifecycle SET next_attempt_at=0')
                await initialise_step(SimpleNamespace(store=store, dry_run=False), router, None)
        self.assertEqual(len(texts), 3)
        self.assertNotEqual(texts[0], texts[1])
        self.assertEqual(store.lifecycle(account)['initialise_failures'], 3)
        alerts = store._conn.execute("SELECT COUNT(*) FROM discord_outbox WHERE event_key=?", (f'initialise_failing:{account}',)).fetchone()[0]
        self.assertEqual(alerts, 1)


if __name__ == '__main__':
    unittest.main()
