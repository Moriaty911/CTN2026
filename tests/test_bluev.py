"""The verified watcher: read the eleven verified timelines directly instead of waiting for the feed crawl."""
import time
import unittest
import uuid

from ctn.pipeline import bluev
from ctn.storage import StateStore


def row(post_id, author, *, text='', created_at=None):
    return {'post_id': post_id, 'author': author, 'author_id': 3, 'text': text,
            'created_at': time.time() - 60 if created_at is None else created_at}


class FakeActuator:
    def __init__(self, pages, *, error=None):
        self.pages = pages
        self.error = error or {}
        self.calls = []
        self.details = []

    async def get_account_posts(self, target, *, limit=8):
        self.calls.append((target, limit))
        if target in self.error:
            raise self.error[target]
        return self.pages.get(target, [])

    async def get_post(self, post_id):
        self.details.append(post_id)
        if post_id.startswith('orig'):
            return {'post_id': post_id, 'text': f'Original text of {post_id}?', 'author': 'citizen', 'author_id': 77,
                    'created_at': time.time() - 900}
        return {'post_id': post_id, 'text': f'Text of {post_id}?', 'author': 'mayor', 'author_id': 3,
                'created_at': time.time() - 60}


class VerifiedStepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.store.register_verified_accounts(['mayor', 'herald'], status='verified_npc')

    def tasks(self, post_id):
        with self.store._lock:
            return self.store._conn.execute('SELECT status FROM post_tasks WHERE post_id=?', (post_id,)).fetchall()

    async def test_new_posts_are_stored_and_queued(self):
        actuator = FakeActuator({'mayor': [row('p1', 'mayor'), row('p2', 'mayor')]})
        out = await bluev.verified_step(actuator, self.store, accounts=['mayor'])
        self.assertEqual(out, {'accounts': 1, 'seen': 2, 'queued': 2, 'failed': 0, 'hydrated': 2})
        self.assertEqual(sorted(actuator.details), ['p1', 'p2'], 'the listing has no text: each post is read once')
        self.assertIn('Text of p1', self.store.get_observed_post('p1')['text'])
        self.assertTrue(self.tasks('p1'), 'a fresh verified post must be queued for a reply')

    async def test_a_repost_is_read_through_to_its_original(self):
        repost = {'post_id': 'r1', 'author': 'mayor', 'kind': 'repost', 'repost_of_post_id': 'orig1', 'text': ''}
        actuator = FakeActuator({'mayor': [repost]})
        out = await bluev.verified_step(actuator, self.store, accounts=['mayor'])
        self.assertEqual(out['hydrated'], 1)
        stored = self.store.get_observed_post('orig1')
        self.assertIsNotNone(stored, 'the original must be stored, not the empty repost')
        self.assertIn('Original text', stored['text'])
        self.assertEqual(self.store.policy('verified_repost:orig1', 0) > 0, True,
                         'a verified repost is a signal we want to keep')

    async def test_rows_that_already_carry_text_are_not_read_again(self):
        actuator = FakeActuator({'mayor': [row('p3', 'mayor', text='Where exactly does the pass apply?')]})
        out = await bluev.verified_step(actuator, self.store, accounts=['mayor'])
        self.assertEqual(out['hydrated'], 0)
        self.assertEqual(actuator.details, [])

    async def test_second_pass_queues_nothing_new(self):
        actuator = FakeActuator({'mayor': [row('p1', 'mayor')]})
        await bluev.verified_step(actuator, self.store, accounts=['mayor'])
        out = await bluev.verified_step(actuator, self.store, accounts=['mayor'])
        self.assertEqual(out['queued'], 0)
        self.assertEqual(out['seen'], 1)

    async def test_unreadable_account_does_not_stop_the_sweep(self):
        actuator = FakeActuator({'herald': [row('h1', 'herald')]}, error={'mayor': RuntimeError('503')})
        out = await bluev.verified_step(actuator, self.store, accounts=['mayor', 'herald'])
        self.assertEqual(out['failed'], 1)
        self.assertEqual(out['queued'], 1)
        self.assertIsNotNone(self.store.get_observed_post('h1'))

    async def test_polls_every_registered_verified_account_by_default(self):
        actuator = FakeActuator({})
        await bluev.verified_step(actuator, self.store)
        self.assertEqual({name for name, _ in actuator.calls}, {'mayor', 'herald'})

    def test_verified_accounts_reads_the_registry(self):
        self.assertEqual(sorted(bluev.verified_accounts(self.store)), ['herald', 'mayor'])

    async def test_verified_post_ranks_above_a_plain_citizen_post(self):
        with self.store.batch_transaction():
            self.store._conn.execute('INSERT INTO bot_registry(account_id,status,suspicion_score) VALUES(?,?,?) '
                                     'ON CONFLICT(account_id) DO UPDATE SET suspicion_score=excluded.suspicion_score',
                                     ('citizen' + uuid.uuid4().hex[:4], 'unknown', 0.0))
        actuator = FakeActuator({'mayor': [row('p1', 'mayor')]})
        await bluev.verified_step(actuator, self.store, accounts=['mayor'])
        with self.store._lock:
            priority = self.store._conn.execute('SELECT priority,audience FROM post_tasks WHERE post_id=?',
                                                ('p1',)).fetchone()
        self.assertGreaterEqual(float(priority['audience'] or 0), 16.0)
