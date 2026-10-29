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


class BoostTests(unittest.TestCase):
    def setUp(self):
        self.store, self.router = setup_store(); self.addCleanup(self.store.close)
        self.accounts = amplify.amplifiable_accounts(self.store)
        publish(self.store, 'leader', self.accounts[0], at_ts=1000.0)

    def likes(self, post_id='leader'):
        return [dict(r) for r in self.store._conn.execute("SELECT * FROM amplify_plan WHERE post_id=? AND kind='like'", (post_id,))]

    def test_boosts_one_post_to_the_asked_total(self):
        got = amplify.boost_post(self.store, 'leader', self.accounts, extra_likes=3, rng=random.Random(3), now=1000.0)
        self.assertEqual(got['added'], 3)
        self.assertEqual(got['likes_now'], 3)
        rows = self.likes()
        self.assertNotIn(self.accounts[0], {r['account_id'] for r in rows})  # never the author
        self.assertEqual(len({r['account_id'] for r in rows}), 3)
        self.assertTrue(all(1090 <= r['due_at'] <= 3400 for r in rows))

    def test_second_boost_only_tops_up_with_unused_accounts(self):
        amplify.boost_post(self.store, 'leader', self.accounts, extra_likes=2, rng=random.Random(3), now=1000.0)
        before = {r['account_id'] for r in self.likes()}
        got = amplify.boost_post(self.store, 'leader', self.accounts, extra_likes=3, rng=random.Random(4), now=1100.0)
        after = {r['account_id'] for r in self.likes()}
        self.assertTrue(before < after)
        self.assertEqual(len(after), 4)  # the 4 other accounts, no duplicates
        self.assertEqual(got['already_planned'], 2)

    def test_boosting_something_that_is_not_our_original_does_nothing(self):
        got = amplify.boost_post(self.store, 'someone-else', self.accounts, extra_likes=3, now=1000.0)
        self.assertEqual(got, {'post_id': 'someone-else', 'added': 0, 'reason': 'not_one_of_our_originals'})
        self.assertEqual(self.likes('someone-else'), [])

    def test_boost_does_not_change_the_level(self):
        amplify.boost_post(self.store, 'leader', self.accounts, extra_likes=2, now=1000.0)
        self.assertEqual(amplify.level(self.store, at('2026-10-29T12:00:00')), 'low')


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

    async def test_waiting_for_a_rate_window_is_not_a_failure_until_the_post_is_stale(self):
        self.store.set_policy('like_pacing', {'per_hour': 0, 'per_day': 100, 'require_initialised': True})
        self.queue('like', self.accounts[1])
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO amplify_sources VALUES('o1',?,'post','high',?)", (self.accounts[0], time.time()))
        for _ in range(amplify.POLICY['max_like_attempts'] + 2):
            with self.store.batch_transaction():
                self.store._conn.execute("UPDATE amplify_plan SET due_at=0")
            with patch.object(amplify, 'level', return_value='high'):
                await self.worker.step()
        self.assertEqual(self.status('like', self.accounts[1]), ('queued', 'like_hourly_cap'))
        with self.store.batch_transaction():
            self.store._conn.execute("UPDATE amplify_sources SET planned_at=?", (time.time() - amplify.POLICY['max_wait_seconds'] - 1,))
            self.store._conn.execute("UPDATE amplify_plan SET due_at=0")
        with patch.object(amplify, 'level', return_value='high'):
            await self.worker.step()
        self.assertEqual(self.status('like', self.accounts[1]), ('skipped', 'stale:like_hourly_cap'))

    async def test_a_busy_replier_hands_the_reply_to_a_free_account(self):
        busy = self.accounts[2]
        self.store.set_policy('reply_pacing', {'per_hour': 100, 'min_gap_seconds': 600})
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO amplify_sources VALUES('o1',?,'post','high',?)", (self.accounts[0], time.time()))
            self.store._conn.execute("INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,created_at,updated_at,metadata_json) "
                                     "VALUES('r','r','reply',?,'x','earlier','succeeded','',0,?,?,'{}')", (busy, time.time() - 60, time.time() - 60))
        self.queue('reply', busy)
        with patch.object(amplify, 'level', return_value='high'):
            await self.worker.step()
        row = self.store._conn.execute("SELECT account_id,status FROM amplify_plan WHERE kind='reply'").fetchone()
        self.assertEqual(row['status'], 'done')
        self.assertNotIn(row['account_id'], (busy, self.accounts[0]))
        sender = self.store._conn.execute("SELECT account_id FROM action_intents WHERE kind='reply' AND id<>'r'").fetchone()[0]
        self.assertEqual(sender, row['account_id'])

    async def test_all_accounts_busy_waits_without_spending_attempts(self):
        self.store.set_policy('reply_pacing', {'per_hour': 0, 'min_gap_seconds': 0})
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO amplify_sources VALUES('o1',?,'post','high',?)", (self.accounts[0], time.time()))
            self.store._conn.execute("INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,created_at,updated_at,metadata_json) "
                                     "VALUES('r','r','reply','someone','x','earlier','succeeded','',0,?,?,'{}')", (time.time(), time.time()))
        self.queue('reply', self.accounts[2])
        for _ in range(amplify.POLICY['max_reply_attempts'] + 1):
            with self.store.batch_transaction():
                self.store._conn.execute("UPDATE amplify_plan SET due_at=0")
            with patch.object(amplify, 'level', return_value='high'):
                await self.worker.step()
        self.assertEqual(self.status('reply', self.accounts[2]), ('queued', 'all_accounts_busy'))
        self.llm.call.assert_not_called()


TEN = tuple(f'yes_{i:03d}' for i in range(1, 11))


class PushTests(unittest.TestCase):
    """Operator 2026-10-20: push a few originals hard (reposts + replies early) so they reach trending."""
    def setUp(self):
        self.store, self.router = setup_store(TEN); self.addCleanup(self.store.close)
        self.accounts = amplify.amplifiable_accounts(self.store)
        self.author = self.accounts[0]

    def rows(self, post_id, kind=None):
        sql = 'SELECT * FROM amplify_plan WHERE post_id=?' + (' AND kind=?' if kind else '')
        return [dict(r) for r in self.store._conn.execute(sql, (post_id, kind) if kind else (post_id,))]

    def test_push_adds_reposts_and_tops_replies_up_from_different_accounts(self):
        amplify.plan_post(self.store, 'o1', self.author, 'post', 1000.0, self.accounts, lvl='high', rng=random.Random(1), now=1000.0)
        before = self.rows('o1', 'reply')
        out = amplify.push_post(self.store, 'o1', self.author, 1000.0, self.accounts, reposts=3, replies=6, rng=random.Random(2), now=1000.0)
        reposts, replies = self.rows('o1', 'repost'), self.rows('o1', 'reply')
        self.assertEqual((out['reposts'], out['replies']), (3, 6))
        self.assertEqual((len(reposts), len(replies)), (3, 6))
        actors = [r['account_id'] for r in reposts + replies]
        self.assertEqual(len(actors), len(set(actors)))  # one action each, never repost and reply from the same account
        self.assertNotIn(self.author, actors)
        lo, hi = amplify.POLICY['push']['window_seconds']
        self.assertTrue(all(1000 + lo <= r['due_at'] <= 1000 + hi for r in reposts + replies))
        # Replies the normal plan had spread over an hour now land inside the push window too.
        self.assertEqual({r['id'] for r in before} <= {r['id'] for r in replies}, True)
        self.assertEqual(amplify.push_post(self.store, 'o1', self.author, 1000.0, self.accounts, reposts=3, replies=6, now=1000.0)['reason'], 'already_pushed')

    def test_an_account_joins_at_most_three_pushes_a_day(self):
        cap = int(amplify.POLICY['push']['max_per_account_per_day'])
        for i in range(cap):
            amplify.push_post(self.store, f'p{i}', self.author, 1000.0 + i, self.accounts, reposts=4, replies=5,
                              rng=random.Random(i), now=1000.0 + i)
        counts = {}
        for i in range(cap):
            for r in self.rows(f'p{i}'):
                counts[r['account_id']] = counts.get(r['account_id'], 0) + 1
        self.assertEqual(max(counts.values()), cap)  # 9 accounts, 9 places per push: everyone is used exactly `cap` times
        # The accounts are spent for the day; a further push finds nobody available.
        spent = amplify.push_post(self.store, 'extra', self.author, 2000.0, self.accounts, reposts=4, replies=5,
                                  rng=random.Random(9), now=2000.0)
        self.assertEqual((spent['reposts'], spent['replies']), (0, 0))
        # A day later the same accounts are free again.
        later = amplify.push_post(self.store, 'p4', self.author, 1000.0 + 86401, self.accounts, reposts=4, replies=5, now=1000.0 + 86401)
        self.assertEqual((later['reposts'], later['replies']), (4, 5))

    def test_request_push_needs_one_of_our_originals(self):
        publish(self.store, 'mine', self.author)
        publish(self.store, 'myreply', self.author, kind='reply')
        self.assertEqual(amplify.request_push(self.store, 'mine', reposts=2, replies=2)['reposts'], 2)
        with self.assertRaises(ValueError):
            amplify.request_push(self.store, 'myreply')
        with self.assertRaises(ValueError):
            amplify.request_push(self.store, 'someone_else')

    def test_next_request_expires(self):
        amplify.request_push(self.store, 'next', now=1000.0)
        self.assertIsNotNone(amplify.pending_push(self.store, 1000.0 + amplify.POLICY['push']['request_ttl_seconds']))
        self.assertIsNone(amplify.pending_push(self.store, 1001.0 + amplify.POLICY['push']['request_ttl_seconds']))
        amplify.request_push(self.store, 'cancel')
        self.assertIsNone(amplify.pending_push(self.store, 1000.0))


class PushWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.router = setup_store(TEN); self.addCleanup(self.store.close)
        self.accounts = amplify.amplifiable_accounts(self.store)
        self.platform = MockLegitClient(failure_rate=0, latency_ms=(0, 0))
        self.worker = amplify.AmplifyWorker(actor(self.platform, self.store), self.router, Mock(), Mock(), rng=random.Random(3))

    async def test_next_request_pushes_only_the_first_original_published_after_it(self):
        now = time.time()
        publish(self.store, 'before', self.accounts[0], at_ts=now - 60)
        amplify.request_push(self.store, 'next', reposts=2, replies=3, now=now - 30)
        publish(self.store, 'after', self.accounts[1], at_ts=now - 10)
        publish(self.store, 'reply', self.accounts[2], kind='reply', at_ts=now - 5)
        with patch.object(amplify, 'level', return_value='high'), patch.object(self.worker, '_due', return_value=[]):
            await self.worker.step(now=now)
        pushed = [r[0] for r in self.store._conn.execute('SELECT post_id FROM amplify_push')]
        self.assertEqual(pushed, ['after'])
        self.assertEqual(self.store.policy('amplify_push_next', {}), {})
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM amplify_plan WHERE post_id='after' AND kind='repost'").fetchone()[0], 2)
        publish(self.store, 'later', self.accounts[3], at_ts=now + 1)
        with patch.object(amplify, 'level', return_value='high'), patch.object(self.worker, '_due', return_value=[]):
            await self.worker.step(now=now + 2)
        self.assertEqual([r[0] for r in self.store._conn.execute('SELECT post_id FROM amplify_push')], ['after'])

    async def test_off_switch_holds_a_pending_push(self):
        publish(self.store, 'o1', self.accounts[1])
        amplify.request_push(self.store, 'next', now=time.time() - 60)
        self.store.set_policy('amplify_override', 'off')
        await self.worker.step()
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM amplify_push').fetchone()[0], 0)

    async def test_repost_goes_through_the_send_path_and_yields_to_a_backed_up_queue(self):
        self.platform.posts['o1'] = MockPost(post_id='o1', author=self.accounts[0], text='Ferry queue talk.', created_at=time.time())
        with self.store.batch_transaction():
            for account in self.accounts[1:3]:
                self.store._conn.execute("INSERT INTO amplify_plan(id,post_id,kind,account_id,due_at,updated_at) VALUES(?,?,'repost',?,0,0)",
                                         ('rp' + account, 'o1', account))
        with patch.object(amplify, 'level', return_value='high'), patch.object(self.store, 'request_backlog', return_value=600):
            await self.worker.step()
        self.assertEqual({tuple(r) for r in self.store._conn.execute("SELECT status,reason FROM amplify_plan")}, {('queued', 'publish_backlog')})
        with self.store.batch_transaction():
            self.store._conn.execute('UPDATE amplify_plan SET due_at=0')
        with patch.object(amplify, 'level', return_value='high'):
            await self.worker.step()
        self.assertEqual({tuple(r) for r in self.store._conn.execute("SELECT status,reason FROM amplify_plan")}, {('done', 'ok')})
        self.assertEqual(self.platform.posts['o1'].reposts, 2)
        meta = json.loads(self.store._conn.execute("SELECT metadata_json FROM action_intents WHERE kind='repost'").fetchone()[0])
        self.assertEqual(meta['engine'], 'amplify')


class RepostCommentPushTests(unittest.IsolatedAsyncioTestCase):
    """Push reposts carry a short comment with the original's tags plus one of ours (verified live 2026-10-20)."""
    async def asyncSetUp(self):
        self.store, self.router = setup_store(TEN); self.addCleanup(self.store.close)
        self.accounts = amplify.amplifiable_accounts(self.store)
        self.platform = MockLegitClient(failure_rate=0, latency_ms=(0, 0))
        self.platform.posts['o1'] = MockPost(post_id='o1', author=self.accounts[0],
                                            text='Ferry crews rotate weekly. #KeepKingstoniaOpen', created_at=time.time())
        self.store.upsert_observed_post({'post_id': 'o1', 'author': self.accounts[0],
                                         'text': 'Ferry crews rotate weekly. #KeepKingstoniaOpen', 'created_at': time.time()})
        self.worker = amplify.AmplifyWorker(actor(self.platform, self.store), self.router, Mock(), Mock(), rng=random.Random(3))
        with self.store.batch_transaction():
            for account in self.accounts[1:3]:
                self.store._conn.execute("INSERT INTO amplify_plan(id,post_id,kind,account_id,due_at,updated_at) VALUES(?,?,'repost',?,0,0)",
                                         ('rp' + account, 'o1', account))

    async def test_reposts_carry_the_original_tag_plus_one_of_ours(self):
        with patch.object(amplify, 'level', return_value='high'):
            await self.worker.step()
        comments = [c['comment'] for c in self.platform.reposts_with_comment]
        self.assertEqual(len(comments), 2)
        for text in comments:
            self.assertIn('#KeepKingstoniaOpen', text)
            self.assertIn('#ProofKeepsUsOpen', text)
        self.assertEqual(len(set(comments)), 2)  # different accounts open differently
        self.assertEqual(self.platform.posts['o1'].reposts, 2)

    async def test_a_rejected_comment_still_goes_out_as_a_plain_repost(self):
        with patch.object(amplify, 'level', return_value='high'), \
             patch('ctn.hashtags.repost_comments', return_value=['中文不受支持 #h2d2']):
            await self.worker.step()
        self.assertEqual(self.platform.reposts_with_comment, [])
        self.assertEqual(self.platform.posts['o1'].reposts, 2)
        meta = [json.loads(r[0]) for r in self.store._conn.execute("SELECT metadata_json FROM action_intents WHERE kind='repost'")]
        self.assertEqual({m.get('repost_comment_rejected') for m in meta}, {'non_english_text'})


class PushIntervalTests(unittest.TestCase):
    def setUp(self):
        self.store, self.router = setup_store(); self.addCleanup(self.store.close)
        self.accounts = amplify.amplifiable_accounts(self.store)
        publish(self.store, 'o1', self.accounts[0])
        publish(self.store, 'o2', self.accounts[1])

    def test_the_push_spec_stays_small_while_our_reposts_are_deleted(self):
        push = amplify.POLICY['push']
        self.assertEqual((push['reposts'], push['replies'], push['max_per_account_per_day']), (3, 4, 3))

    def test_a_second_push_inside_the_minimum_interval_is_refused(self):
        gap = float(amplify.POLICY['push']['min_seconds_between_pushes'])
        self.assertEqual(amplify.request_push(self.store, 'o1', reposts=2, replies=2, now=1000.0)['reposts'], 2)
        early = amplify.request_push(self.store, 'o2', reposts=2, replies=2, now=1000.0 + 60)
        self.assertEqual(early['reason'], 'too_soon')
        self.assertEqual(early['wait_s'], int(gap) - 60)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM amplify_push').fetchone()[0], 1)
        self.assertEqual(amplify.request_push(self.store, 'o2', reposts=2, replies=2, now=1000.0 + gap)['reposts'], 2)


class AmplifyReplyKeyTests(unittest.TestCase):
    """Live 2026-10-20: a pushed post got 1 of 7 replies; the rest hit the one-team-reply-per-target dedupe (action_succeeded)."""
    def test_each_account_may_reply_once_to_our_post_but_citizen_replies_stay_one_per_target(self):
        store, _ = setup_store(); self.addCleanup(store.close)
        a, b = amplify.amplifiable_accounts(store)[:2]
        amp = {'engine': 'amplify'}
        self.assertNotEqual(store.action_key('reply', a, 'o1', 'x', amp), store.action_key('reply', b, 'o1', 'y', amp))
        self.assertEqual(store.action_key('reply', a, 'o1', 'x', amp), store.action_key('reply', a, 'o1', 'z', amp))
        self.assertEqual(store.action_key('reply', a, 'c1', 'x', {}), store.action_key('reply', b, 'c1', 'y', {}))
        self.assertNotEqual(store.action_key('reply', a, 'o1', 'x', amp), store.action_key('reply', a, 'o1', 'x', {}))

if __name__ == '__main__':
    unittest.main()
