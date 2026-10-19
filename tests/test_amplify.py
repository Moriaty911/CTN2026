"""Team amplification: plans, levels and off switch, cap exemptions, execution through the normal send path."""
import json
import random
import time
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from ctn import amplify
from ctn.client.mock import MockLegitClient, MockPost
from ctn.jev.postflight import PostflightVerdict
from ctn.storage import StateStore
from ctn.storage.lifecycle import SYDNEY
from tests.test_persona80 import bound_router
from tests.test_runtime_reliability import actor

IDS = ('yes_001', 'yes_002', 'yes_003', 'yes_004', 'yes_005')


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=SYDNEY).timestamp()


def setup_store(ids=IDS):
    store = StateStore()
    router = bound_router(store, ids)
    with store.batch_transaction():
        for account in store.persona_bindings().values():
            store._conn.execute("INSERT INTO account_lifecycle(account_id,initialise_status,initialised_at) VALUES(?,'ready',1)", (account,))
    amplify.ensure_schema(store)
    return store, router


def publish(store, post_id, account, kind='post', purpose='P01', engine='', at_ts=None):
    ts = time.time() if at_ts is None else at_ts
    meta = {'purpose': purpose, **({'engine': engine} if engine else {})}
    with store.batch_transaction():
        store._conn.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            ('a' + post_id, post_id, account, 'p', 1, 'g', kind, '', '', '', 0, ts, ts, json.dumps(meta)))


class LevelTests(unittest.TestCase):
    def test_phases_and_operator_override(self):
        store, _ = setup_store(); self.addCleanup(store.close)
        self.assertEqual(amplify.level(store, at('2026-10-19T12:00:00')), 'off')
        self.assertEqual(amplify.level(store, at('2026-10-20T12:00:00')), 'high')
        self.assertEqual(amplify.level(store, at('2026-10-25T12:00:00')), 'low')
        self.assertEqual(amplify.level(store, at('2026-11-01T12:00:00')), 'max')
        store.set_policy('amplify_override', 'off')
        self.assertEqual(amplify.level(store, at('2026-11-01T12:00:00')), 'off')
        store.set_policy('amplify_override', '')
        self.assertEqual(amplify.level(store, at('2026-11-01T12:00:00')), 'max')


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.store, self.router = setup_store(); self.addCleanup(self.store.close)
        self.accounts = amplify.amplifiable_accounts(self.store)

    def rows(self, post_id):
        return [dict(r) for r in self.store._conn.execute('SELECT * FROM amplify_plan WHERE post_id=?', (post_id,))]

    def test_original_gets_likes_and_replies_from_other_accounts_once(self):
        author = self.accounts[0]
        n = amplify.plan_post(self.store, 'o1', author, 'post', 1000.0, self.accounts, lvl='max', rng=random.Random(1), now=1000.0)
        rows = self.rows('o1')
        self.assertEqual(n, len(rows))
        self.assertNotIn(author, {r['account_id'] for r in rows})
        likes = [r for r in rows if r['kind'] == 'like']; replies = [r for r in rows if r['kind'] == 'reply']
        self.assertTrue(3 <= len(likes) <= 4)  # 70-100% of the 4 other accounts
        self.assertTrue(3 <= len(replies) <= 4)
        self.assertEqual(len({r['account_id'] for r in likes}), len(likes))
        self.assertTrue(all(1090 <= r['due_at'] <= 3400 for r in likes))
        self.assertTrue(all(1180 <= r['due_at'] <= 4600 for r in replies))
        self.assertEqual(amplify.plan_post(self.store, 'o1', author, 'post', 1000.0, self.accounts, lvl='max', now=1000.0), 0)

    def test_our_replies_only_get_likes_and_off_plans_nothing(self):
        amplify.plan_post(self.store, 'r1', self.accounts[0], 'reply', 1000.0, self.accounts, lvl='max', rng=random.Random(2), now=1000.0)
        self.assertEqual({r['kind'] for r in self.rows('r1')}, {'like'})
        self.assertEqual(amplify.plan_post(self.store, 'o2', self.accounts[0], 'post', 1000.0, self.accounts, lvl='off', now=1000.0), 0)

    def test_first_posts_and_amplification_replies_are_not_sources(self):
        now = time.time()
        publish(self.store, 'first', self.accounts[0], purpose='initialise', at_ts=now)
        publish(self.store, 'amp', self.accounts[1], kind='reply', engine='amplify', at_ts=now)
        publish(self.store, 'orig', self.accounts[2], at_ts=now)
        publish(self.store, 'old', self.accounts[2], at_ts=now - 7200)
        self.assertEqual([s['post_id'] for s in amplify.new_sources(self.store, now)], ['orig'])

    def test_only_ready_active_accounts_take_part(self):
        self.store.set_account_policy(self.accounts[1], mode='standby')
        self.assertNotIn(self.accounts[1], amplify.amplifiable_accounts(self.store))


class CapTests(unittest.TestCase):
    def test_amplification_replies_skip_pile_on_caps_but_keep_account_pacing(self):
        from ctn.pipeline.targeting import cap_reason
        store, router = setup_store(); self.addCleanup(store.close)
        accounts = amplify.amplifiable_accounts(store)
        with store.batch_transaction():
            for i, account in enumerate(accounts[:3]):
                store._conn.execute("INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,created_at,updated_at,metadata_json) "
                    "VALUES(?,?,'reply',?,'root','x','succeeded','',0,?,?,?)",
                    (f'i{i}', f'i{i}', account, time.time() - 4000, time.time() - 4000,
                     json.dumps({'engine': 'amplify', 'target_author': accounts[4], 'root_post_id': 'root'})))
        # Three amplification replies in one thread do not use up the thread or author cap for citizen replies.
        self.assertEqual(cap_reason(store, target_author=accounts[4], root_post_id='root', account=accounts[3]), '')
        self.assertEqual(cap_reason(store, account=accounts[0]), '')
        store.set_policy('reply_pacing', {'per_hour': 1, 'min_gap_seconds': 0})
        with store.batch_transaction():
            store._conn.execute("UPDATE action_intents SET created_at=? WHERE id='i0'", (time.time() - 60,))
        self.assertEqual(cap_reason(store, account=accounts[0]), 'account_hourly_cap')


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.router = setup_store(); self.addCleanup(self.store.close)
        self.accounts = amplify.amplifiable_accounts(self.store)
        self.platform = MockLegitClient(failure_rate=0, latency_ms=(0, 0))
        self.platform.posts['o1'] = MockPost(post_id='o1', author=self.accounts[0], text='Our ferry crew talked about clinic queues today.', created_at=time.time())
        self.store.upsert_observed_post({'post_id': 'o1', 'author': self.accounts[0], 'text': 'Our ferry crew talked about clinic queues today.', 'created_at': time.time()})
        self.store.set_policy('like_pacing', {'per_hour': 100, 'per_day': 100, 'require_initialised': True})
        self.store.set_policy('reply_pacing', {'per_hour': 100, 'min_gap_seconds': 0})
        self.llm = Mock()
        from ctn.llm.client import LlmCallResult
        self.llm.call.return_value = LlmCallResult('ok', 1, 'r', json.dumps({'choices': [{'message': {'content': 'Same at our stop, the morning queue was out the door.'}}]}).encode())
        self.checker = Mock(); self.checker.check.return_value = PostflightVerdict(True, 'approved')
        self.worker = amplify.AmplifyWorker(actor(self.platform, self.store), self.router, self.llm, self.checker, rng=random.Random(3))

    def queue(self, kind, account):
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO amplify_plan(id,post_id,kind,account_id,due_at,updated_at) VALUES(?,?,?,?,0,0)",
                                     (kind + account, 'o1', kind, account))

    def status(self, kind, account):
        return tuple(self.store._conn.execute('SELECT status,reason FROM amplify_plan WHERE id=?', (kind + account,)).fetchone())

    async def test_like_and_reply_go_through_the_send_path(self):
        self.queue('like', self.accounts[1]); self.queue('reply', self.accounts[2])
        with patch.object(amplify, 'level', return_value='high'):
            await self.worker.step()
        self.assertEqual(self.status('like', self.accounts[1]), ('done', 'ok'))
        self.assertEqual(self.status('reply', self.accounts[2]), ('done', 'ok'))
        meta = json.loads(self.store._conn.execute("SELECT metadata_json FROM action_intents WHERE kind='reply'").fetchone()[0])
        self.assertEqual((meta['engine'], meta['purpose'], meta['target_stance']), ('amplify', 'P01', 'team'))
        kwargs = self.checker.check.call_args.kwargs
        self.assertTrue(kwargs['strict_alignment'])
        self.assertIn('untrusted', self.llm.call.call_args.args[0][1]['content'])

    async def test_rejected_draft_is_not_sent(self):
        self.checker.check.return_value = PostflightVerdict(False, 'formal_or_off_persona')
        self.queue('reply', self.accounts[2])
        with patch.object(amplify, 'level', return_value='high'):
            await self.worker.step()
        self.assertEqual(self.status('reply', self.accounts[2]), ('failed', 'postflight_rejected:formal_or_off_persona'))
        self.assertIsNone(self.store._conn.execute("SELECT 1 FROM action_intents WHERE kind='reply'").fetchone())

    async def test_replies_yield_to_a_backed_up_publish_queue(self):
        self.queue('reply', self.accounts[2])
        with patch.object(amplify, 'level', return_value='high'), patch.object(self.store, 'request_backlog', return_value=600):
            await self.worker.step()
        self.assertEqual(self.status('reply', self.accounts[2]), ('queued', 'publish_backlog'))
        self.llm.call.assert_not_called()

    async def test_off_switch_stops_planning_and_sending(self):
        self.queue('like', self.accounts[1])
        publish(self.store, 'o1', self.accounts[0])
        self.store.set_policy('amplify_override', 'off')
        self.assertEqual(await self.worker.step(), {'level': 'off'})
        self.assertEqual(self.status('like', self.accounts[1]), ('queued', ''))
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM amplify_sources').fetchone()[0], 0)

    async def test_a_row_left_running_by_a_restart_is_retried(self):
        self.queue('like', self.accounts[1])
        with self.store.batch_transaction():
            self.store._conn.execute("UPDATE amplify_plan SET status='running',updated_at=?", (time.time() - 700,))
        with patch.object(amplify, 'level', return_value='high'):
            await self.worker.step()
        self.assertEqual(self.status('like', self.accounts[1]), ('done', 'ok'))

    async def test_pacing_refusal_requeues_and_gives_up_after_the_attempt_limit(self):
        self.store.set_policy('like_pacing', {'per_hour': 0, 'per_day': 100, 'require_initialised': True})
        self.queue('like', self.accounts[1])
        for _ in range(amplify.POLICY['max_like_attempts']):
            with self.store.batch_transaction():
                self.store._conn.execute("UPDATE amplify_plan SET due_at=0")
            with patch.object(amplify, 'level', return_value='high'):
                await self.worker.step()
        self.assertEqual(self.status('like', self.accounts[1]), ('skipped', 'like_hourly_cap'))


if __name__ == '__main__':
    unittest.main()
