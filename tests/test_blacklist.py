"""Blacklist reporting: detection rules, protection, the separate send gate, the reporter loop and its switches."""
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ctn import blacklist
from ctn.client.base import PlatformError
from ctn.client.mock import MockLegitClient, MockPost
from ctn.storage import StateStore
from tests.test_persona80 import bound_router
from tests.test_runtime_reliability import actor

NOW = 1_800_000_000.0
TEXT = 'Let us save our friends and family from the H2D2 Dolphin Flu, vote yes on November 2! Copy-paste this message.'


def post(store, pid, author_id, text='Some ordinary words about the ferry today.', *, at=NOW, parent=None, root=None, author=None):
    store.upsert_observed_post({'post_id': str(pid), 'author': author or str(author_id), 'author_id': author_id, 'text': text,
                                'created_at': at, 'parent_id': parent, 'root_post_id': root})


def ring(store, anchor, post_id, members, *, start=NOW, gap=20.0):
    post(store, post_id, anchor, f'Anchor post {post_id} about protecting hospitals and jobs.', at=start - 60)
    for i, m in enumerate(members):
        post(store, f'{post_id}r{i}', m, f'Count me in {post_id}-{i}.', at=start + i * gap, parent=post_id, root=post_id)


class Base(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        blacklist.ensure_schema(self.store)


class ProtectionTests(Base):
    def test_pre_open_verified_and_own_accounts_are_never_blacklisted(self):
        self.store.upsert_managed_account('nolan_mercer', platform_account_id=2374)
        self.store.register_verified_accounts(['DailyKingstonia'], status='verified_npc')
        self.assertEqual(blacklist.protected_reason(self.store, 2343), 'registered_before_open')
        self.assertEqual(blacklist.protected_reason(self.store, 2374), 'own_account')
        self.assertEqual(blacklist.protected_reason(self.store, 3000, 'DailyKingstonia'), 'verified_or_own')
        self.assertEqual(blacklist.protected_reason(self.store, 'x'), 'unknown_id')
        self.assertEqual(blacklist.protected_reason(self.store, 2344), '')
        for aid in (2343, 2374):
            self.assertEqual(blacklist.add(self.store, aid, reason='operator', source='operator')[0], False)
        self.assertEqual(blacklist.active(self.store), {})

    def test_add_remove_and_only_an_operator_reinstates(self):
        self.assertEqual(blacklist.add(self.store, 3001, reason='leak', username='bot1'), (True, 'added'))
        self.assertEqual(blacklist.add(self.store, 3001, reason='leak')[1], 'already_active')
        self.assertTrue(blacklist.remove(self.store, 3001))
        self.assertFalse(blacklist.remove(self.store, 3001))
        self.assertEqual(blacklist.add(self.store, 3001, reason='leak')[1], 'already_removed')
        self.assertEqual(blacklist.add(self.store, 3001, reason='operator', source='operator'), (True, 'added'))
        self.assertEqual(blacklist.active(self.store)[3001]['reason'], 'operator')
        with self.assertRaises(ValueError):
            blacklist.add(self.store, 3002, reason='vibes')


class DetectionTests(Base):
    def test_copy_paste_and_shared_text(self):
        for i in range(3):
            post(self.store, f'cp{i}', 3001, TEXT, at=NOW - i * 60)
        post(self.store, 'one', 3002, TEXT + ' Once.', at=NOW)
        added = dict(blacklist.scan(self.store, now=NOW))
        self.assertEqual(added, {3001: 'copy_paste'})
        for i, aid in enumerate((3010, 3011, 3012)):
            post(self.store, f'sh{i}', aid, 'Vote YES on November 2, the same campaign sentence shared word for word today.', at=NOW)
        added = dict(blacklist.scan(self.store, now=NOW))
        self.assertEqual(added, {3010: 'shared_text', 3011: 'shared_text', 3012: 'shared_text'})

    def test_shared_small_talk_or_text_a_citizen_also_posted_is_not_evidence(self):
        for i, aid in enumerate((3010, 3011, 3012, 3013)):
            post(self.store, f'st{i}', aid, 'That is a lot to carry on your own. Glad you wrote it down for everyone.', at=NOW)
        line = 'Why is everyone shouting YES or NO like it is a football match at the weekend.'
        for i, aid in enumerate((3020, 3021, 3022)):
            post(self.store, f'fb{i}', aid, line, at=NOW)
        post(self.store, 'fbnpc', 1400, line, at=NOW)
        self.assertEqual(blacklist.scan(self.store, now=NOW), [])

    def test_short_or_old_or_npc_repeats_are_ignored(self):
        for i in range(4):
            post(self.store, f's{i}', 3001, 'Count me in.', at=NOW)                   # short
            post(self.store, f'o{i}', 3002, TEXT, at=NOW - 3 * 86400)                # outside the lookback
            post(self.store, f'n{i}', 1500, TEXT + ' npc', at=NOW)                   # registered before open
        self.assertEqual(blacklist.scan(self.store, now=NOW), [])

    def test_reply_ring_members_and_anchors(self):
        members = list(range(4000, 4025))
        for k, (anchor, pid) in enumerate(((5000, 'a1'), (5000, 'a2'), (5001, 'a3'))):
            ring(self.store, anchor, pid, members, start=NOW - k * 3600)
        added = dict(blacklist.scan(self.store, now=NOW))
        self.assertTrue(all(added[m] == 'ring' for m in members))
        self.assertEqual(added[5000], 'ring_anchor')
        self.assertNotIn(5001, added)                     # one flooded post is not enough for an anchor
        self.assertEqual(blacklist.active(self.store)[4000]['cluster'], 'ring')

    def test_organic_busy_thread_is_not_a_ring(self):
        members = list(range(4100, 4125))
        for pid in ('b1', 'b2', 'b3'):
            ring(self.store, 5100, pid, members, gap=180.0)
        self.assertEqual(blacklist.scan(self.store, now=NOW + 25 * 180), [])

    def test_language_model_leaks(self):
        post(self.store, 'l1', 3020, "Sure, here's a draft: Hey! Thanks for chiming in.")
        post(self.store, 'l2', 3021, 'As an AI language model I support choice.')
        post(self.store, 'l3', 3022, 'Every voter cares about {topic} today.')
        post(self.store, 'ok', 3023, 'Sure, here is my bus stop, see you there.')
        post(self.store, 'l4', 3024, 'Sure, here\u2019s a concise post: "Support the mandate!"')
        post(self.store, 'l5', 3025, "I'll play along with this scenario but it's important to note...")
        post(self.store, 'ok2', 3026, "Sure, here's the thing about ferries: they are late.")
        added = dict(blacklist.scan(self.store, now=NOW))
        self.assertEqual(added, {3020: 'leak', 3021: 'leak', 3022: 'leak', 3024: 'leak', 3025: 'leak'})
        self.assertIn('draft', json.loads(blacklist.active(self.store)[3020]['evidence_json'])['match'])

    def test_detail_starts_with_the_official_wording_and_names_the_evidence(self):
        blacklist.add(self.store, 3001, reason='copy_paste', evidence={'n': 7})
        detail = blacklist.detail_for(blacklist.active(self.store)[3001])
        self.assertTrue(detail.startswith('I think that this content is inauthentic (bot) activity because '))
        self.assertIn('7 identical posts', detail)
        blacklist.add(self.store, 3002, reason='leak', evidence={'match': "Sure, here's a draft"})
        self.assertIn("Sure, here's a draft", blacklist.detail_for(blacklist.active(self.store)[3002]))

    def test_import_takes_the_high_confidence_sections_only(self):
        md = ('# x\n### 1. 主号\n| id | 用户名 |\n|---|---|\n| 2356 | alex.martin | a |\n'
              '### 2. 回复号\n| 2380 | olivia_smith21 | o |\n### 3. 点赞号\n| 2868 | liker | l |\n'
              '### 4. 疑似点赞号\n| 2900 | maybe | m |\n## 三、NPC\n| 12 | npc | n |\n')
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as f:
            f.write(md)
        self.addCleanup(os.unlink, f.name)
        result = blacklist.import_intel(self.store, f.name)
        self.assertEqual(result['added'], 3)
        self.assertEqual(set(blacklist.active(self.store)), {2356, 2380, 2868})


class GateTests(Base):
    def setUp(self):
        super().setUp()
        self.store.set_policy('report_mode', 'hybrid')
        blacklist.add(self.store, 3001, reason='leak', evidence={'match': 'As an AI'})
        post(self.store, 'p1', 3001, 'As an AI I think so.')

    def claim(self, target='p1', author='3001', **extra):
        meta = {'engine': 'blacklist', 'target_author': author, 'reason': 'inauthentic', 'detail': 'I think that ...',
                'report_attempt_id': extra.pop('attempt', 'attempt-1'), **extra}
        return self.store.claim_action('report', 'reporter', target, metadata=meta, now=time.time())

    def test_blacklisted_post_skips_the_legacy_evidence_and_pending_gates(self):
        self.store.set_policy('max_pending_reports', 0)
        ident, reason = self.claim()
        self.assertTrue(ident, reason)
        # Another of our accounts may not report the same post again.
        meta = {'engine': 'blacklist', 'target_author': '3001', 'reason': 'inauthentic', 'detail': 'I think that ...', 'report_attempt_id': 'attempt-2'}
        self.assertEqual(self.store.claim_action('report', 'other_reporter', 'p1', metadata=meta)[1], 'report_target_already_claimed')

    def test_unlisted_suspended_switched_off_or_detail_missing_are_refused(self):
        post(self.store, 'p2', 3999, 'Plain post.')
        self.assertEqual(self.claim('p2', '3999')[1], 'not_blacklisted')
        self.assertEqual(self.claim(detail='')[1], 'invalid_report_reason')
        self.store.set_policy('blacklist_reporting', False)
        self.assertEqual(self.claim()[1], 'blacklist_reporting_off')
        self.store.set_policy('blacklist_reporting', True)
        self.store.set_report_suspension(now=time.time())
        self.assertEqual(self.claim()[1], 'reporting_suspended')

    def test_legacy_reports_keep_their_gates(self):
        ident, reason = self.store.claim_action('report', 'reporter', 'p1', metadata={'target_author': '3001', 'detail': 'x'})
        self.assertIsNone(ident)
        self.assertNotEqual(reason, 'blacklisted')

    def test_wire_time_recheck_stops_a_report_removed_from_the_list_meanwhile(self):
        ident, _ = self.claim()
        self.store.start_action(ident)
        self.assertEqual(self.store.guard_wire_action(ident), (True, 'ready'))
        blacklist.remove(self.store, 3001)
        self.assertEqual(self.store.guard_wire_action(ident), (False, 'not_blacklisted'))


class ReporterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.router = bound_router(self.store, ('yes_001', 'yes_002'))
        self.accounts = sorted(self.store.persona_bindings().values())
        self.platform = MockLegitClient(failure_rate=0, latency_ms=(0, 0))
        self.act = actor(self.platform, self.store)
        self.act.catalog_hash = self.router.registry.catalog_hash   # as ctn/main.py sets it
        self.reporter = blacklist.BlacklistReporter(self.act, self.store)
        self.reporter._last_scan = time.time() + 10**6          # scans are tested separately
        self.store.set_policy('report_mode', 'hybrid')
        blacklist.add(self.store, 3001, reason='leak', evidence={'match': 'As an AI'})
        now = time.time()
        for i, at in enumerate((now - 300, now - 100, now - 200)):
            pid = f'b{i}'
            self.platform.posts[pid] = MockPost(post_id=pid, author='3001', text='As an AI...', created_at=at)
            post(self.store, pid, 3001, 'As an AI...', at=at)
        for author, pid, at in ((3002, 'c0', now - 50), (3003, 'c1', now - 40)):
            blacklist.add(self.store, author, reason='leak', evidence={'match': 'As an AI'})
            self.platform.posts[pid] = MockPost(post_id=pid, author=str(author), text='As an AI...', created_at=at)
            post(self.store, pid, author, 'As an AI...', at=at)
        post(self.store, 'clean', 3999, 'Not listed.')

    def reported(self):
        return [r[0] for r in self.store._conn.execute("SELECT target_id FROM action_intents WHERE kind='report' ORDER BY created_at")]

    async def step(self):
        """One reporter tick with the submit interval cleared; pacing has its own tests."""
        self.reporter._next_submit_at = 0.0
        return await self.reporter.step()

    async def test_reports_one_post_per_author_newest_first(self):
        for _ in range(5):
            await self.step()
        self.assertEqual(self.reported(), ['c1', 'c0', 'b1'])
        meta = json.loads(self.store._conn.execute("SELECT metadata_json FROM action_intents WHERE kind='report' LIMIT 1").fetchone()[0])
        self.assertEqual((meta['engine'], meta['blacklist_reason'], meta['target_author']), ('blacklist', 'leak', '3003'))
        self.assertTrue(meta['detail'].startswith('I think that this content is inauthentic (bot) activity because'))
        self.assertEqual(len({r[0] for r in self.store._conn.execute("SELECT account_id FROM action_intents WHERE kind='report'")}), 2)

    async def test_the_extra_posts_of_a_reported_author_wait_in_the_pool(self):
        for _ in range(3):
            await self.step()
        summary = blacklist.pool_summary(self.store, now=time.time())
        self.assertEqual(summary['authors_reported'], 3)
        self.assertEqual(summary['posts_visible_unreported'], 2)
        self.assertEqual(summary['posts_reportable_now'], 0)
        self.assertEqual(summary['posts_pooled'], 2)
        self.assertIsNone((await self.step())['reported'])

    async def test_one_retry_per_author_is_allowed_after_the_window(self):
        for _ in range(3):
            await self.step()
        now = time.time()
        retry_after = float(blacklist.POLICY['report']['author_retry_after_seconds'])
        self.assertEqual(blacklist.pending_targets(self.store, 5, now=now + retry_after - 60), [])
        self.assertEqual(len(blacklist.pending_targets(self.store, 5, now=now + retry_after + 60)), 2)
        strict = dict(blacklist.POLICY, report=dict(blacklist.POLICY['report'], author_retry_limit=0))
        self.assertEqual(blacklist.pending_targets(self.store, 5, policy=strict, now=now + retry_after + 60), [])

    async def test_an_account_report_parks_the_authors_remaining_posts(self):
        await self.step()
        await self.step()
        await self.step()
        ts = time.time()
        with self.store.batch_transaction():
            self.store._conn.execute('INSERT INTO blacklist_account_reports VALUES(?,?,?,?)', (3001, ts, 'succeeded', ''))
        self.assertEqual(blacklist.pending_targets(self.store, 5, now=ts + 3600), [])
        self.assertGreater(len(blacklist.pending_targets(self.store, 5, now=ts + 86400 + 60)), 0)

    async def test_new_posts_from_a_fresh_author_are_reported_too(self):
        for _ in range(4):
            await self.step()
        blacklist.add(self.store, 3004, reason='leak', evidence={'match': 'As an AI'})
        self.platform.posts['b9'] = MockPost(post_id='b9', author='3004', text='As an AI again', created_at=time.time())
        post(self.store, 'b9', 3004, 'As an AI again', at=time.time())
        out = await self.step()
        self.assertEqual(out['reported']['post_id'], 'b9')

    async def test_waits_while_the_report_route_is_backed_up(self):
        with patch.object(self.store, 'request_backlog', return_value=30.0):
            out = await self.step()
        self.assertIsNone(out['reported'])
        self.assertEqual(self.reported(), [])

    async def test_stops_when_suspended_switched_off_or_after_a_403(self):
        self.store.set_report_suspension(now=time.time())
        self.assertEqual((await self.step())['blocked'], 'reporting_suspended')
        self.store.clear_report_suspension()
        self.store.set_policy('blacklist_reporting', False)
        self.assertEqual((await self.step())['blocked'], 'blacklist_reporting_off')
        self.store.set_policy('blacklist_reporting', True)
        self.store.set_policy('report_forbidden', {'at': time.time(), 'message': 'forbidden'})
        self.assertEqual((await self.step())['blocked'], 'recent_report_403')
        self.store.set_policy('report_forbidden', {'at': time.time() - 3600, 'message': 'forbidden'})
        self.assertIn('reported', await self.step())
        self.assertEqual(len(self.reported()), 1)

    async def test_a_report_403_with_suspension_wording_stops_reporting(self):
        async def forbidden(*a, **k):
            raise PlatformError('reporting suspended for six hours', retryable=False, status=403)
        self.platform.report = forbidden
        await self.step()
        self.assertTrue(self.store.is_reporting_suspended()[0])
        self.assertEqual((await self.step())['blocked'], 'reporting_suspended')

    async def test_gone_post_and_enough_actions_lead_to_one_account_report(self):
        for _ in range(3):
            await self.step()
        del self.platform.posts['b1']                            # hidden by the moderator (reads 404)
        with self.store.batch_transaction():
            self.store._conn.execute('UPDATE blacklist_post_checks SET checked_at=0')
        with patch('ctn.moderation.reporter.BlueTeamReporter.refresh_account_activity', return_value={'action_count': 150}):
            out = await self.step()
        self.assertEqual(out['checked'], 3)
        self.assertEqual(out['account_reported']['author_id'], 3001)
        rows = self.store._conn.execute("SELECT target_id,json_extract(metadata_json,'$.engine') FROM action_intents WHERE kind='report_account'").fetchall()
        self.assertEqual([tuple(r) for r in rows], [('3001', 'blacklist')])
        gone = self.store._conn.execute("SELECT post_id FROM blacklist_post_checks WHERE state='gone'").fetchall()
        self.assertEqual([r[0] for r in gone], ['b1'])
        with patch('ctn.moderation.reporter.BlueTeamReporter.refresh_account_activity', return_value={'action_count': 150}):
            out = await self.step()
        self.assertIsNone(out['account_reported'])               # not again within the recheck window

    async def test_too_few_actions_waits(self):
        for _ in range(3):
            await self.step()
        del self.platform.posts['b1']
        with patch('ctn.moderation.reporter.BlueTeamReporter.refresh_account_activity', return_value={'action_count': 40}):
            out = await self.step()
            self.assertEqual(out['account_reported']['status'], 'waiting_actions:40')
            self.assertIsNone((await self.step())['account_reported'])   # rechecked after an hour, not every step
        self.assertIsNone(self.store._conn.execute("SELECT 1 FROM action_intents WHERE kind='report_account'").fetchone())

    async def test_a_saturated_family_holds_its_account_reports_back(self):
        for _ in range(3):
            await self.step()
        del self.platform.posts['b1']
        with self.store.batch_transaction():
            for i in range(5):                                # the ring family already used its full-price slots
                self.store._conn.execute(
                    "INSERT INTO report_attempts(id,action_id,subject_kind,subject_id,author,report_id,status,submitted_at,ruled_at,outcome,payload_json)"
                    " VALUES(?,?,'post',?,'a','','pending',?,0,'',?)",
                    (f'q{i}', f'qa{i}', f'qp{i}', time.time() - i - 1, json.dumps({'price_group': 'leak'})))
        with patch('ctn.moderation.reporter.BlueTeamReporter.refresh_account_activity', return_value={'action_count': 150}):
            out = await self.step()
        self.assertEqual(out['account_reported']['status'], 'price_wait:leak')
        self.assertIsNone(self.store._conn.execute("SELECT 1 FROM action_intents WHERE kind='report_account'").fetchone())

    async def test_scan_runs_inside_the_step(self):
        self.reporter._last_scan = 0
        post(self.store, 'leak2', 3050, "Sure, here's a draft: thanks!")
        self.platform.posts['leak2'] = MockPost(post_id='leak2', author='3050', text='x', created_at=time.time())
        out = await self.step()
        self.assertIn((3050, 'leak'), out['added'])


class RankingTests(Base):
    def rows(self):
        return [
            {'post_id': 'weak', 'author_id': 1, 'at': NOW, 'reason': 'shared_text', 'cluster': 'text:aaa',
             'evidence_json': json.dumps({'n': 5}), 'likes': 0, 'replies': 0, 'reposts': 0, 'trending': 0},
            {'post_id': 'ring', 'author_id': 2, 'at': NOW - 900, 'reason': 'ring', 'cluster': 'ring',
             'evidence_json': json.dumps({'n': 3, 'threads': ['900', '800', '700']}), 'likes': 0, 'replies': 0,
             'reposts': 0, 'trending': 0},
            {'post_id': 'copy', 'author_id': 3, 'at': NOW - 1800, 'reason': 'copy_paste', 'cluster': '',
             'evidence_json': json.dumps({'n': 4}), 'likes': 0, 'replies': 0, 'reposts': 0, 'trending': 0},
        ]

    def test_hard_evidence_beats_a_newer_weak_text_post(self):
        ranked = blacklist.rank_candidates(self.rows(), now=NOW, price_counts={}, hidden_clusters={'text:aaa'})
        self.assertEqual([r['post_id'] for r in ranked], ['ring', 'copy', 'weak'])

    def test_weak_text_waits_until_its_cluster_has_a_hidden_post(self):
        ranked = blacklist.rank_candidates(self.rows(), now=NOW, price_counts={}, hidden_clusters=set())
        self.assertNotIn('weak', [r['post_id'] for r in ranked])
        self.assertEqual([r['post_id'] for r in ranked], ['ring', 'copy'])

    def test_trending_and_busy_posts_come_first(self):
        rows = self.rows()
        rows[1].update(trending=1, likes=40, replies=10, reposts=12)
        ranked = blacklist.rank_candidates(rows, now=NOW, price_counts={}, hidden_clusters={'text:aaa'})
        self.assertEqual(ranked[0]['post_id'], 'ring')

    def test_a_family_is_dropped_once_its_five_full_price_slots_are_used(self):
        rows = self.rows()
        group = blacklist.price_group(rows[0])
        self.assertEqual(group, 'shared_text')
        kept = blacklist.rank_candidates(rows, now=NOW, price_counts={group: 4}, hidden_clusters={'text:aaa'})
        self.assertIn('weak', [r['post_id'] for r in kept])
        dropped = blacklist.rank_candidates(rows, now=NOW, price_counts={group: 5}, hidden_clusters={'text:aaa'})
        self.assertNotIn('weak', [r['post_id'] for r in dropped])

    def test_diminished_prices_stay_available_only_when_allowed(self):
        rows = self.rows()
        group = blacklist.price_group(rows[0])
        permissive = dict(blacklist.POLICY, ranking=dict(blacklist.POLICY['ranking'], price=dict(
            blacklist.POLICY['ranking']['price'], allow_diminished=True)))
        ranked = blacklist.rank_candidates(rows, now=NOW, price_counts={group: 5}, hidden_clusters={'text:aaa'}, policy=permissive)
        self.assertIn('weak', [r['post_id'] for r in ranked])
        self.assertLess(ranked[-1]['rank_score'], 0.5)

    def test_price_group_is_one_family_per_evidence_type(self):
        self.assertEqual(blacklist.price_group(self.rows()[1]), 'ring')
        self.assertEqual(blacklist.price_group(self.rows()[2]), 'copy_paste')
        thread = dict(blacklist.POLICY, ranking=dict(blacklist.POLICY['ranking'], price_group_mode='thread'))
        self.assertEqual(blacklist.price_group(self.rows()[1], policy=thread), 'ring:700')
        self.assertEqual(blacklist.price_group(self.rows()[2], policy=thread), 'author:3')

    def test_ring_accounts_share_one_price_group_whatever_thread_they_came_from(self):
        first = {'post_id': 'a', 'author_id': 1, 'reason': 'ring', 'evidence_json': json.dumps({'threads': ['900']})}
        second = {'post_id': 'b', 'author_id': 2, 'reason': 'ring_anchor', 'evidence_json': json.dumps({'posts': ['55']})}
        self.assertEqual(blacklist.price_group(first), blacklist.price_group(second))

    def test_trending_posts_enter_the_pool_before_newer_cold_posts(self):
        blacklist.add(self.store, 3001, reason='ring', cluster='ring', evidence={'n': 3, 'threads': ['1']})
        blacklist.add(self.store, 3002, reason='ring', cluster='ring', evidence={'n': 3, 'threads': ['2']})
        post(self.store, 'old-trending', 3001, 'Count me in.', at=NOW - 2 * 3600)
        post(self.store, 'new-cold', 3002, 'Count me in.', at=NOW - 60)
        self.store._conn.execute('INSERT INTO trending_seen(post_id,seen_at) VALUES(?,?)', ('old-trending', NOW))
        self.store._conn.commit()
        ranked = blacklist.pending_targets(self.store, 5, now=NOW)
        self.assertEqual([r['post_id'] for r in ranked], ['old-trending', 'new-cold'])
        self.assertEqual(ranked[0]['trending'], 1)

    def test_pending_targets_carry_the_ranking(self):
        blacklist.add(self.store, 3001, reason='shared_text', cluster='text:aaa', evidence={'n': 5})
        blacklist.add(self.store, 3002, reason='ring', cluster='ring', evidence={'n': 3, 'threads': ['900']})
        post(self.store, 'weak', 3001, 'Vote yes for the mandate before November 2!', at=NOW)
        post(self.store, 'ring', 3002, 'Count me in.', at=NOW - 60)
        self.assertEqual([r['post_id'] for r in blacklist.pending_targets(self.store, 5, now=NOW)], ['ring'])
        self.store._conn.execute(
            'INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,'
            "created_at,updated_at,metadata_json) VALUES('i1','k1','report','a1','w0','','succeeded','',0,?,?,?)",
            (NOW - 2 * 86400, NOW - 2 * 86400, json.dumps({'engine': 'blacklist', 'target_author': '3001'})))
        self.store._conn.execute('INSERT INTO blacklist_post_checks(post_id,author_id,state,checked_at) VALUES(?,?,?,?)',
                                 ('w0', 3001, 'gone', NOW))
        self.store._conn.commit()
        ranked = blacklist.pending_targets(self.store, 5, now=NOW)
        self.assertEqual([r['post_id'] for r in ranked], ['ring', 'weak'])
        self.assertEqual({r['post_id']: r['price_group'] for r in ranked}, {'ring': 'ring', 'weak': 'shared_text'})
        self.assertGreater(ranked[0]['rank_score'], ranked[1]['rank_score'])


class CounterRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.platform = MockLegitClient(failure_rate=0, latency_ms=(0, 0))
        self.act = actor(self.platform, self.store)
        self.reporter = blacklist.BlacklistReporter(self.act, self.store)

    async def test_counters_are_read_once_then_cached(self):
        calls = []

        async def counters(post_id):
            calls.append(post_id)
            return {'likes': 40, 'replies': 2, 'reposts': 5}

        self.act.get_counters = counters
        rows = [{'post_id': 'p1', 'at': NOW}]
        out = await self.reporter._refresh_counters(rows, 1000.0)
        self.assertEqual((out[0]['likes'], out[0]['replies'], out[0]['reposts']), (40, 2, 5))
        await self.reporter._refresh_counters(rows, 1100.0)
        self.assertEqual(calls, ['p1'])
        await self.reporter._refresh_counters(rows, 1000.0 + 301.0)
        self.assertEqual(calls, ['p1', 'p1'])

    async def test_a_read_budget_caps_one_step(self):
        calls = []

        async def counters(post_id):
            calls.append(post_id)
            return {'likes': 1, 'replies': 0, 'reposts': 0}

        self.act.get_counters = counters
        rows = [{'post_id': f'p{i}', 'at': NOW} for i in range(12)]
        await self.reporter._refresh_counters(rows, 1000.0)
        self.assertEqual(len(calls), 8)


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    """The pool budget, the submit interval and the pre-submit liveness check."""

    async def asyncSetUp(self):
        self.store = StateStore(); self.addCleanup(self.store.close)
        self.router = bound_router(self.store, ('yes_001', 'yes_002'))
        self.platform = MockLegitClient(failure_rate=0, latency_ms=(0, 0))
        self.act = actor(self.platform, self.store)
        self.act.catalog_hash = self.router.registry.catalog_hash
        self.store.set_policy('report_mode', 'hybrid')
        blacklist.ensure_schema(self.store)
        self.t0 = NOW
        for i in range(4):
            author = 3100 + i
            pid = f'z{i}'
            at = self.t0 - 60 * (i + 1)                     # z0 is the newest, so it ranks first
            blacklist.add(self.store, author, reason='ring', cluster='ring',
                          evidence={'n': 3, 'threads': [str(author)]}, now=self.t0)
            self.platform.posts[pid] = MockPost(post_id=pid, author=str(author), text='Count me in.', created_at=at)
            post(self.store, pid, author, 'Count me in.', at=at)
        self.reporter = blacklist.BlacklistReporter(self.act, self.store)
        self.reporter._last_scan = self.t0 + 10**9           # scanning is tested on its own

    def reported(self):
        return [r[0] for r in self.store._conn.execute("SELECT target_id FROM action_intents WHERE kind='report' ORDER BY created_at")]

    async def test_one_submission_per_submit_interval(self):
        self.assertTrue((await self.reporter.step(now=self.t0))['reported'])
        self.assertIsNone((await self.reporter.step(now=self.t0 + 30))['reported'])
        self.assertTrue((await self.reporter.step(now=self.t0 + 91))['reported'])

    async def test_the_pool_stops_submissions_when_it_is_full(self):
        with self.store.batch_transaction():
            for i in range(26):
                self.store._conn.execute(
                    "INSERT INTO report_attempts(id,action_id,subject_kind,subject_id,author,report_id,status,submitted_at,ruled_at,outcome,payload_json)"
                    " VALUES(?,?,'post',?,'a','','pending',?,0,'','{}')", (f'r{i}', f'a{i}', f'p{i}', self.t0 - 10))
        out = await self.reporter.step(now=self.t0)
        self.assertIsNone(out['reported'])
        self.assertEqual(self.reported(), [])
        summary = blacklist.stats(self.store, since=self.t0 - 3600)
        self.assertEqual((summary['pending'], summary['pool_size']), (26, 26))

    async def test_rows_older_than_the_budget_window_do_not_block_the_pool(self):
        with self.store.batch_transaction():
            for i in range(26):                              # unresolved as far as our bookkeeping knows
                self.store._conn.execute(
                    "INSERT INTO report_attempts(id,action_id,subject_kind,subject_id,author,report_id,status,submitted_at,ruled_at,outcome,payload_json)"
                    " VALUES(?,?,'post',?,'a','','pending',?,0,'','{}')", (f'o{i}', f'oa{i}', f'op{i}', self.t0 - 3 * 3600))
        self.assertFalse(self.reporter._pool_full(self.t0))
        self.assertTrue((await self.reporter.step(now=self.t0))['reported'])

    async def test_a_target_that_already_disappeared_is_marked_gone_and_skipped(self):
        del self.platform.posts['z0']                        # somebody else's report took it down
        out = await self.reporter.step(now=self.t0)
        self.assertEqual(out['reported']['post_id'], 'z1')
        self.assertEqual(self.reported(), ['z1'])
        gone = [r[0] for r in self.store._conn.execute("SELECT post_id FROM blacklist_post_checks WHERE state='gone'")]
        self.assertEqual(gone, ['z0'])

    def test_old_thread_groups_count_against_their_family(self):
        with self.store.batch_transaction():
            self.store._conn.execute(
                "INSERT INTO report_attempts(id,action_id,subject_kind,subject_id,author,report_id,status,submitted_at,ruled_at,outcome,payload_json)"
                " VALUES('old','old','post','oldp','a','','pending',?,0,'',?)",
                (self.t0 - 60, json.dumps({'price_group': 'ring:6793', 'blacklist_reason': 'ring'})))
        self.assertEqual(blacklist._price_counts(self.store, self.t0).get('ring'), 1)

    def test_account_reports_count_against_the_same_price_group(self):
        with self.store.batch_transaction():
            for kind, ident, group in (('post', 'p1', 'ring'), ('account', '3100', 'ring'), ('post', 'p2', 'shared_text')):
                self.store._conn.execute(
                    "INSERT INTO report_attempts(id,action_id,subject_kind,subject_id,author,report_id,status,submitted_at,ruled_at,outcome,payload_json)"
                    " VALUES(?,?,?,?,'a','','pending',?,0,'',?)",
                    (ident, ident, kind, ident, self.t0 - 10, json.dumps({'price_group': group})))
        self.assertEqual(blacklist._price_counts(self.store, self.t0), {'ring': 2, 'shared_text': 1})


class SwitchTests(unittest.TestCase):
    def test_legacy_reporter_paused_and_blacklist_on_by_default(self):
        from ctn.main import reporting_workers
        with patch.dict(os.environ, {}, clear=False):
            for k in ('CTN_LEGACY_REPORTS', 'CTN_BLACKLIST_REPORTS'):
                os.environ.pop(k, None)
            self.assertEqual(reporting_workers(), (False, True))
        with patch.dict(os.environ, {'CTN_LEGACY_REPORTS': 'true', 'CTN_BLACKLIST_REPORTS': 'false'}):
            self.assertEqual(reporting_workers(), (True, False))

    def test_ops_commands(self):
        from ctn import ops
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / 'db.sqlite3')
            def run(*args):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    ops.main(['--state-file', db, 'blacklist', *args])
                return json.loads(buf.getvalue())
            self.assertEqual(run('add', '3001', '--username', 'spammer', '--note', 'seen copy-paste'), {'added': True, 'why': 'added'})
            self.assertEqual(run('add', '2000')['why'], 'registered_before_open')
            self.assertEqual([a['author_id'] for a in run('list')['active']], [3001])
            self.assertFalse(run('off')['enabled'])
            self.assertTrue(run('on')['enabled'])
            self.assertEqual(run('remove', '3001'), {'removed': True})
            self.assertEqual(run('stats')['blacklist'], {'removed:operator': 1})


if __name__ == '__main__':
    unittest.main()
