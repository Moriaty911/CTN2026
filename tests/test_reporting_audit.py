"""Reporting audit: re-read reported posts, count account candidates, summarise the 403 picture."""
import json
import unittest
from unittest.mock import patch

from ctn import blacklist, reporting_audit
from ctn.client.mock import MockLegitClient, MockPost
from ctn.moderation.decision import record_report_forbidden, starts_six_hour_stop
from ctn.storage import StateStore

NOW = 1_800_000_000.0


def post(store, pid, author_id, text='Ordinary words about the ferry timetable today.', *, at=NOW):
    store.upsert_observed_post({'post_id': str(pid), 'author': str(author_id), 'author_id': author_id,
                                'text': text, 'created_at': at})


def raw_filed(store, post_id, author_id, *, reason='leak', at=NOW, status='succeeded', key='1'):
    """A report intent as the actuator leaves it behind, without going through the send gates."""
    meta = {'engine': 'blacklist', 'target_author': str(author_id), 'reason': 'inauthentic',
            'blacklist_reason': reason, 'detail': 'I think that this content is inauthentic (bot) activity because x'}
    with store.batch_transaction():
        store._conn.execute(
            "INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,"
            "created_at,updated_at,detail,metadata_json,response_json,wire_at,wire_tracked) "
            "VALUES(?,?,?,?,?,'',?,'',0,?,?,'',?,'{}',?,1)",
            (f'intent-{post_id}-{key}', f'key-{post_id}-{key}', 'report', 'reporter', str(post_id),
             status, at, at, json.dumps(meta), at))


def filed(store, post_id, author_id, **kwargs):
    """A report that really went through the store's own gates."""
    ident, why = store.claim_action('report', 'reporter', str(post_id), metadata={
        'engine': 'blacklist', 'target_author': str(author_id), 'reason': 'inauthentic',
        'blacklist_reason': kwargs.pop('reason', 'leak'),
        'detail': 'I think that this content is inauthentic (bot) activity because x',
        'report_attempt_id': f'attempt-{post_id}'}, now=kwargs.pop('at', NOW))
    if not ident:
        raise AssertionError(why)
    store.start_action(ident)
    store.finish_action(ident, kwargs.pop('status', 'succeeded'), response={'report_id': f'r-{post_id}'},
                        now=kwargs.pop('at', NOW) + 1)
    return ident


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = StateStore()
        self.addCleanup(self.store.close)
        blacklist.ensure_schema(self.store)
        self.store.set_policy('report_mode', 'hybrid')
        self.platform = MockLegitClient(failure_rate=0, latency_ms=(0, 0))
        blacklist.add(self.store, 3001, reason='leak', evidence={'match': 'As an AI'}, cluster='leak')
        for i, pid in enumerate(('p1', 'p2', 'p3')):
            post(self.store, pid, 3001, at=NOW - i)
            self.platform.posts[pid] = MockPost(post_id=pid, author='3001', text='As an AI...', created_at=NOW - i)
            filed(self.store, pid, 3001)

    async def test_hidden_posts_are_recorded_and_visible_ones_are_left_alone(self):
        del self.platform.posts['p2']                       # the moderator hid it: reads 404
        out = await reporting_audit.sweep_reported_posts(self.platform, self.store, now=NOW + 60)
        self.assertEqual((out['checked'], out['gone'], out['visible']), (3, 1, 2))
        self.assertEqual(out['gone_ids'], ['p2'])
        self.assertEqual(out['by_reason'], {'leak': {'gone': 1, 'visible': 2}})
        rows = {r[0]: r[1] for r in self.store._conn.execute("SELECT post_id,state FROM blacklist_post_checks")}
        self.assertEqual(rows, {'p1': 'visible', 'p2': 'gone', 'p3': 'visible'})

    async def test_dry_run_writes_nothing(self):
        del self.platform.posts['p1']
        out = await reporting_audit.sweep_reported_posts(self.platform, self.store, now=NOW + 60, dry_run=True)
        self.assertEqual(out['gone'], 1)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM blacklist_post_checks").fetchone()[0], 0)

    async def test_unreadable_posts_are_counted_but_not_judged(self):
        from ctn.client.base import PlatformError

        async def boom(post_id):
            raise PlatformError('rate limited', retryable=True, status=429)

        self.platform.get_post = boom
        out = await reporting_audit.sweep_reported_posts(self.platform, self.store, now=NOW + 60)
        self.assertEqual((out['checked'], out['unreadable']), (0, 3))
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM blacklist_post_checks").fetchone()[0], 0)

    async def test_reported_posts_lists_each_target_once(self):
        raw_filed(self.store, 'p1', 3001, reason='ring', key='2')     # a second attempt on the same post
        rows = reporting_audit.reported_posts(self.store)
        self.assertEqual(sorted(r['post_id'] for r in rows), ['p1', 'p2', 'p3'])
        self.assertEqual(len(rows), 3)

    async def test_account_candidates_need_a_hidden_post_and_hundred_actions(self):
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO blacklist_post_checks VALUES(?,?,?,?)", ('p1', 3001, 'gone', NOW))
        with patch('ctn.moderation.reporter.BlueTeamReporter.refresh_account_activity',
                   return_value={'action_count': 150}):
            ready = await reporting_audit.account_readiness(self.platform, self.store, now=NOW + 60)
        self.assertEqual([(r['author_id'], r['state'], r['hidden_posts']) for r in ready], [(3001, 'ready', 1)])
        with patch('ctn.moderation.reporter.BlueTeamReporter.refresh_account_activity',
                   return_value={'action_count': 40}):
            waiting = await reporting_audit.account_readiness(self.platform, self.store, now=NOW + 60)
        self.assertEqual(waiting[0]['state'], 'waiting_actions:40')

    async def test_an_author_without_a_hidden_post_is_not_a_candidate(self):
        out = await reporting_audit.account_readiness(self.platform, self.store, now=NOW + 60)
        self.assertEqual(out, [])

    async def test_precision_counts_each_reported_post_once(self):
        raw_filed(self.store, 'p1', 3001, reason='ring', key='2')
        with self.store.batch_transaction():
            self.store._conn.execute('INSERT INTO blacklist_post_checks VALUES(?,?,?,?)', ('p1', 3001, 'gone', NOW))
            self.store._conn.execute('INSERT INTO blacklist_post_checks VALUES(?,?,?,?)', ('p3', 3001, 'gone', NOW))
        out = reporting_audit.precision(self.store)
        # p1 was reported twice, so it is counted once and keeps the last reason recorded for it.
        self.assertEqual(sum(v['filed'] for v in out.values()), 3)
        self.assertEqual(out['leak'], {'filed': 2, 'gone': 1, 'rate': 0.5})
        self.assertEqual(out['ring'], {'filed': 1, 'gone': 1, 'rate': 1.0})

    async def test_forbidden_summary_reports_the_waiting_pile(self):
        with self.store.batch_transaction():
            self.store._conn.execute(
                "INSERT INTO report_attempts(id,action_id,subject_kind,subject_id,author,report_id,status,"
                "submitted_at,ruled_at,outcome,payload_json) VALUES('a1','i1','post','p9','3001','','pending',?,0,'','{}')",
                (NOW - 7200,))
        record_report_forbidden(self.store, 'Your team is unable to report any post or account for six hours.', now=NOW)
        out = reporting_audit.forbidden_summary(self.store, now=NOW + 60)
        self.assertTrue(out['suspended'])
        self.assertEqual(out['remaining_s'], 21540.0)
        self.assertIn('six hours', out['last_forbidden']['message'])
        self.assertGreater(out['oldest_pending_age_s'], 7000)
        self.assertEqual(out['attempt_status'], {'pending': 4})   # three from setUp plus this one

    async def test_run_audit_puts_the_three_sections_together(self):
        del self.platform.posts['p3']
        with patch('ctn.moderation.reporter.BlueTeamReporter.refresh_account_activity',
                   return_value={'action_count': 150}):
            out = await reporting_audit.run_audit(self.platform, self.store, now=NOW + 60)
        self.assertEqual(out['posts']['gone'], 1)
        self.assertEqual([c['author_id'] for c in out['account_candidates']], [3001])
        self.assertEqual(set(out['reports']), {'suspended', 'remaining_s', 'last_forbidden', 'attempt_status',
                                               'oldest_pending_age_s', 'recent_failures'})
        self.assertEqual(out['precision']['leak']['filed'], 3)
        self.assertFalse(out['dry_run'])

    async def test_run_audit_can_skip_the_account_pass(self):
        out = await reporting_audit.run_audit(self.platform, self.store, now=NOW + 60, accounts=False)
        self.assertEqual(out['account_candidates'], [])


class SixHourStopTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore()
        self.addCleanup(self.store.close)

    def test_the_real_genuine_ruling_wording_starts_the_six_hour_stop(self):
        self.assertTrue(starts_six_hour_stop('Your team is unable to report any post or account for six hours.'))
        record_report_forbidden(self.store, 'Your team is unable to report any post or account for six hours.', now=NOW)
        self.assertTrue(self.store.is_reporting_suspended(now=NOW)[0])

    def test_the_pending_cap_wording_does_not(self):
        message = "You cannot report this: your team already has 20 reports awaiting a moderator's ruling."
        self.assertFalse(starts_six_hour_stop(message))
        record_report_forbidden(self.store, message, now=NOW)
        self.assertFalse(self.store.is_reporting_suspended(now=NOW)[0])
        self.assertTrue(self.store.policy('report_forbidden', {}).get('message', '').startswith('You cannot report'))

    def test_suspension_wording_keeps_working(self):
        self.assertTrue(starts_six_hour_stop('reporting suspended for six hours'))
