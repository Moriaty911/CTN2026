"""Tests for ctn/pipeline/sweep.py: warm citizens' timelines feed the reply queue."""
import time
import unittest

from ctn.pipeline import sweep
from ctn.storage import StateStore


class FakeActuator:
    def __init__(self, posts=None):
        self.posts = posts or {}
        self.calls = []

    async def get_account_posts(self, account, limit=6):
        self.calls.append(account)
        answer = self.posts.get(account, [])
        if isinstance(answer, Exception):
            raise answer
        return answer


def row(pid, author, *, author_id=1500, text='A thought on the vote.', parent=None):
    return {'post_id': pid, 'author': author, 'author_id': author_id, 'text': text,
            'created_at': time.time(), 'parent_id': parent, 'root_post_id': parent}


def our_post(store, author, *, post_id='ours_1', at=None):
    """A citizen answering one of our posts is what makes them warm."""
    ts = time.time() if at is None else at
    with store.batch_transaction():
        store._conn.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            ('act' + post_id, post_id, 'ours', 'p', 1, 'g', 'reply', '', '', '', 0, ts - 60, ts - 60, '{}'))
    store.upsert_observed_post({'post_id': 'back_' + author, 'author': author, 'author_id': 1500,
                                'text': 'Replying to you.', 'created_at': ts, 'parent_id': post_id})


class WarmCitizenTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)

    def test_only_citizens_who_wrote_to_us_are_swept(self):
        our_post(self.store, 'warm_one')
        our_post(self.store, 'warm_two', post_id='ours_2')
        # A citizen posting on its own is not warm; neither is another team's account answering us.
        self.store.upsert_observed_post({'post_id': 'cold_1', 'author': 'cold_citizen', 'author_id': 1600,
                                         'text': 'Anyone else?', 'created_at': time.time()})
        with self.store.batch_transaction():
            self.store._conn.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                     ('actx', 'ours_3', 'ours', 'p', 1, 'g', 'reply', '', '', '', 0,
                                      time.time() - 60, time.time() - 60, '{}'))
        self.store.upsert_observed_post({'post_id': 'player_back', 'author': 'player_one', 'author_id': 9000,
                                         'text': 'Sure.', 'created_at': time.time(), 'parent_id': 'ours_3'})

        self.assertEqual(sorted(sweep.warm_citizens(self.store)), ['warm_one', 'warm_two'])


class SweepStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_posts_are_ingested_once(self):
        store = StateStore(); self.addCleanup(store.close)
        our_post(store, 'warm_one')
        actuator = FakeActuator({'warm_one': [row('p_new', 'warm_one')]})

        result = await sweep.sweep_step(actuator, store)

        self.assertEqual(actuator.calls, ['warm_one'])
        self.assertEqual((result['accounts'], result['seen'], result['queued'], result['failed']), (1, 1, 1, 0))
        self.assertIsNotNone(store.get_observed_post('p_new'))

        again = await sweep.sweep_step(actuator, store)
        self.assertEqual(again['queued'], 0, 'the same post must not queue twice')

    async def test_unreadable_account_does_not_stop_the_sweep(self):
        store = StateStore(); self.addCleanup(store.close)
        our_post(store, 'gone', post_id='ours_a')
        our_post(store, 'alive', post_id='ours_b')
        actuator = FakeActuator({'gone': RuntimeError('account suspended'), 'alive': [row('p_alive', 'alive')]})

        result = await sweep.sweep_step(actuator, store)

        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['queued'], 1)
        self.assertIsNotNone(store.get_observed_post('p_alive'))

    async def test_reposts_and_empty_rows_are_left_to_the_feed_crawl(self):
        store = StateStore(); self.addCleanup(store.close)
        our_post(store, 'warm_one')
        actuator = FakeActuator({'warm_one': [
            {'post_id': 'p_repost', 'author': 'warm_one', 'repost_of_post_id': 'orig', 'text': 'x'},
            row('p_blank', 'warm_one', text=''),
            row('p_real', 'warm_one'),
        ]})

        result = await sweep.sweep_step(actuator, store)

        self.assertEqual(result['seen'], 1)
        self.assertIsNone(store.get_observed_post('p_repost'))
        self.assertIsNone(store.get_observed_post('p_blank'))

    async def test_the_cursor_rotates_so_every_warm_citizen_is_reached(self):
        store = StateStore(); self.addCleanup(store.close)
        names = ['warm_%d' % i for i in range(5)]
        for index, name in enumerate(names):
            our_post(store, name, post_id='ours_%d' % index, at=time.time() - index)
        actuator = FakeActuator()

        seen = []
        for _ in range(3):
            before = len(actuator.calls)
            await sweep.sweep_step(actuator, store, batch=2)
            seen.extend(actuator.calls[before:])

        self.assertEqual(len(seen), 6)
        self.assertEqual(sorted(set(seen)), sorted(names), 'two passes over three slices must cover everyone')

    async def test_no_warm_citizens_means_no_requests(self):
        store = StateStore(); self.addCleanup(store.close)
        actuator = FakeActuator()

        result = await sweep.sweep_step(actuator, store)

        self.assertEqual(actuator.calls, [])
        self.assertEqual(result['accounts'], 0)


if __name__ == '__main__':
    unittest.main()
