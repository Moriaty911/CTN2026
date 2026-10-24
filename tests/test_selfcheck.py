"""Watching our own posts: which ones get read back, and how a hidden one is noticed."""
import asyncio
import json
import unittest

from ctn import selfcheck
from ctn.storage import StateStore

NOW = 1_800_000_000.0


def publish(store, post_id, account='a1', *, at=NOW, kind='post'):
    with store.batch_transaction():
        store._conn.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            ('a' + post_id, post_id, account, 'p', 1, 'g', kind, '', '', '', 0, at, at,
                             json.dumps({'purpose': 'P01'})))


def check(store, post_id, *, account='a1', state='visible', misses=0, last_checked=0.0):
    with store.batch_transaction():
        store._conn.execute('INSERT INTO own_post_checks(post_id,account_id,state,misses,last_checked,fields_json) VALUES(?,?,?,?,?,?)',
                            (post_id, account, state, misses, last_checked, '{}'))


def states(store):
    with store._lock:
        return {r[0]: (r[1], r[2]) for r in store._conn.execute('SELECT post_id,state,misses FROM own_post_checks')}


class Stub:
    """Stands in for the actuator: records what was read and can answer with a hidden or missing post."""

    def __init__(self, *, hidden=(), missing=()):
        self.hidden = set(hidden)
        self.missing = set(missing)
        self.calls = []

    async def get_post(self, post_id):
        self.calls.append(str(post_id))
        if str(post_id) in self.missing:return None
        if str(post_id) in self.hidden:return {'post_id': str(post_id), 'status': 'hidden'}
        return {'post_id': str(post_id), 'text': 'The ferries are running again.'}


class Base(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        selfcheck._ensure(self.store)
        self.store.set_policy('own_posts', {'every_seconds': 120, 'lookback_seconds': 172800, 'checks_per_step': 40,
                                            'fresh_seconds': 900, 'recheck_seconds': 3600, 'missing_confirmations': 2})

    def run_check(self, actuator, *, now=NOW):
        return asyncio.run(self.store and selfcheck.own_post_check(actuator, self.store, now=now))


class SelectionTests(Base):
    def test_a_post_published_minutes_ago_is_read_before_the_backlog(self):
        for i in range(3):
            publish(self.store, f'old{i}', at=NOW - 7200)
        publish(self.store, 'fresh', at=NOW - 60)
        actuator = Stub()
        self.run_check(actuator)
        self.assertEqual(actuator.calls[0], 'fresh')

    def test_the_backlog_is_rechecked_oldest_first(self):
        publish(self.store, 'older', at=NOW - 6000); check(self.store, 'older', last_checked=NOW - 5000)
        publish(self.store, 'newer', at=NOW - 6000); check(self.store, 'newer', last_checked=NOW - 4000)
        actuator = Stub()
        self.run_check(actuator)
        self.assertEqual(actuator.calls[:2], ['older', 'newer'])

    def test_a_post_checked_within_the_recheck_window_is_not_reread(self):
        publish(self.store, 'recent', at=NOW - 600)
        check(self.store, 'recent', last_checked=NOW - 60)
        actuator = Stub()
        self.run_check(actuator)
        self.assertEqual(actuator.calls, [])

    def test_a_post_older_than_the_lookback_leaves_the_queue(self):
        publish(self.store, 'ancient', at=NOW - 200000)
        actuator = Stub()
        self.run_check(actuator)
        self.assertEqual(actuator.calls, [])

    def test_checks_per_step_caps_the_reads(self):
        for i in range(6):
            publish(self.store, f'p{i}', at=NOW - 60 * i)
        self.store.set_policy('own_posts', {'checks_per_step': 2})
        actuator = Stub()
        self.run_check(actuator)
        self.assertEqual(len(actuator.calls), 2)


class HiddenTests(Base):
    def test_a_hidden_flag_marks_the_post_at_once(self):
        publish(self.store, 'gone', at=NOW - 600)
        self.run_check(Stub(hidden={'gone'}))
        self.assertEqual(states(self.store)['gone'][0], 'hidden')
        with self.store._lock:
            notified = self.store._conn.execute("SELECT COUNT(*) FROM discord_outbox WHERE event_key LIKE 'own_hidden:%'").fetchone()[0]
        self.assertEqual(notified, 1)

    def test_one_unreadable_read_is_not_enough(self):
        publish(self.store, 'mystery', at=NOW - 600)
        self.run_check(Stub(missing={'mystery'}))
        self.assertEqual(states(self.store)['mystery'], ('visible', 1))
        self.run_check(Stub(missing={'mystery'}), now=NOW + 4000)
        self.assertEqual(states(self.store)['mystery'][0], 'hidden')

    def test_two_hidden_posts_slow_the_account_down(self):
        publish(self.store, 'g1', at=NOW - 600); publish(self.store, 'g2', at=NOW - 600)
        self.run_check(Stub(hidden={'g1', 'g2'}))
        self.assertEqual(self.store.policy('slowed_accounts'), {'a1': 8})

    def test_one_hidden_post_does_not_slow_the_account(self):
        publish(self.store, 'g1', at=NOW - 600)
        self.run_check(Stub(hidden={'g1'}))
        self.assertEqual(self.store.policy('slowed_accounts'), {})


class StatusTests(Base):
    def test_status_shows_hidden_posts_and_how_much_is_read(self):
        publish(self.store, 'g1', at=NOW - 600); publish(self.store, 'ok', at=NOW - 600)
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO own_post_checks(post_id,account_id,state,misses,last_checked,fields_json) "
                                     "VALUES('g1','a1','hidden',2,?,'{}')", (NOW,))
        check = self.store.runtime_metrics()['self_check']
        self.assertEqual([row['post_id'] for row in check['hidden']], ['g1'])
        self.assertIn('checked_last_hour', check)
        self.assertIn('published_last_hour', check)
