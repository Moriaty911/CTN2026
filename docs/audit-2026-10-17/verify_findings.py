"""Offline audit reproductions; uses temporary databases and fake platform calls only.

Run from the repository root with Python 3.14, requirements.txt and the official
SDK v0.5.0 available. No configuration file, credentials or live endpoint is read.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ctn.actuator import Actuator
from ctn.client.base import PlatformError
from ctn.client.legit_v1 import NormalizedPage
from ctn.config import BudgetConfig, RateConfig
from ctn.macro.publisher import StrategicPublisher
from ctn.macro.state import MacroStateCollector
from ctn.models import ActionKind, Decision, JevAnswers, Post, Side, Tier
from ctn.moderation import BlueTeamReporter, BotDetector
from ctn.persona import PersonaRouter
from ctn.pipeline.crawl import CommentCrawler
from ctn.pipeline.loop import BotLoop
from ctn.ratelimit import TokenBucket, build_gate, build_ledger
from ctn.storage import StateStore
from ctn.storage.sqlite_store import _SCHEMA_SQL

FAST = RateConfig(read_rps=10000, write_rps=10000,
                  per_account_read_rps=10000, per_account_write_rps=10000)


def actuator(client, *, dry_run=False):
    return Actuator(client, build_gate(FAST), build_ledger(BudgetConfig()),
                    dry_run=dry_run, backoff_s=0)


class FakeClient:
    def __init__(self):
        self.posts_sent = []
        self.reports_sent = []
        self.feed = []
        self.comments = NormalizedPage()
        self.fail_first_post_after_commit = False

    async def post(self, account, text, **kwargs):
        self.posts_sent.append((account, text))
        if self.fail_first_post_after_commit and len(self.posts_sent) == 1:
            raise PlatformError('response lost after server commit', retryable=True)
        return {'post_id': str(len(self.posts_sent))}

    async def report(self, account, post_id, reason=''):
        self.reports_sent.append((account, post_id))
        await asyncio.sleep(.02)
        return {'outcome': 'pending'}

    async def get_feed(self, account='', limit=50):
        return self.feed[:limit]

    async def get_comments(self, post_id, limit=50, cursor=None):
        return self.comments

    async def trending(self, limit=50):
        return [{'hashtag': '#H2D2', 'volume': 100}]

    async def search(self, query, limit=50):
        return []


async def reproduce():
    out = {}
    bucket = TokenBucket(20, 1)
    started = time.monotonic()
    async def acquire_one():
        await bucket.acquire()
        return round(time.monotonic() - started, 4)
    out['concurrent_token_bucket'] = {
        'completion_seconds': await asyncio.gather(*(acquire_one() for _ in range(5))),
        'minimum_expected_last_seconds': .2,
    }

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / 'atomic.db'
        s = StateStore(db)
        try:
            with s.batch_transaction():
                s.upsert_observed_post({'post_id': 'rolled-back', 'author': 'a', 'text': 'x'})
                s.increment_counter('inside_batch')
                raise RuntimeError('deliberate rollback')
        except RuntimeError:
            pass
        out['batch_rollback'] = {
            'post_survives_rollback': s.get_observed_post('rolled-back') is not None,
            'counter_survives_rollback': s.get_counter('inside_batch'),
        }
        s.close()

        old_schema = _SCHEMA_SQL
        begin = old_schema.index('CREATE TABLE IF NOT EXISTS bot_registry (')
        end = old_schema.index('CREATE TABLE IF NOT EXISTS managed_accounts (')
        bot_block = old_schema[begin:end].replace(
            '    platform_account_id INTEGER NOT NULL DEFAULT 0,\n', '')
        bot_block = bot_block.replace(
            'CREATE INDEX IF NOT EXISTS idx_bot_registry_platform_id\n'
            '    ON bot_registry(platform_account_id);\n', '')
        old_schema = old_schema[:begin] + bot_block + old_schema[end:]
        legacy = Path(td) / 'legacy.db'
        conn = sqlite3.connect(legacy)
        conn.executescript(old_schema)
        conn.close()
        try:
            migrated = StateStore(legacy)
        except sqlite3.OperationalError as e:
            out['legacy_migration'] = str(e)
        else:
            out['legacy_migration'] = 'succeeded'
            migrated.close()

    s = StateStore()
    s.upsert_observed_post({'post_id': 'edited', 'author': 'a', 'text': 'Old content before edit.'})
    s.upsert_observed_post({'post_id': 'edited', 'author': 'a', 'text': 'Different content after edit.'})
    rec = s.get_observed_post('edited')
    out['edited_post'] = {'indexed_text': rec['text'], 'raw_text': rec['raw']['text']}
    st1 = s.get_account_state('same-account')
    st2 = s.get_account_state('same-account')
    st1.mark_used(100)
    st2.mark_used(101)
    s.save_account_state(st1)
    s.save_account_state(st2)
    out['overlapping_account_updates'] = {
        'two_successful_uses_recorded_as': s.get_account_state('same-account').post_count}
    out['allied_gate_without_reservations'] = [
        s.check_allied_reply_gate(str(i), 'citizen', set(), every_n=1, hourly_cap=1, now=100)
        for i in range(4)
    ]
    s.close()

    s = StateStore()
    cli = FakeClient()
    router = PersonaRouter(store=s)
    pub = StrategicPublisher(actuator(cli, dry_run=True), None, router)
    result = await pub.publish('bot_worker_yes', 'Checking a dry run in Kingstonia.', bypass_jev=True)
    out['dry_run'] = {
        'status': result['status'], 'platform_posts': len(cli.posts_sent),
        'persisted_post_count': router.get_state('bot_worker_yes').post_count,
        'persisted_dedup': s.has_normalized_text('Checking a dry run in Kingstonia.'),
    }
    snap = await MacroStateCollector(actuator(cli), router).collect()
    out['macro_trending_shape'] = {'tags': snap['trending_hashtags'], 'posts': snap['trending_posts']}
    s.close()

    cli = FakeClient()
    cli.fail_first_post_after_commit = True
    result = await actuator(cli).post('own', 'A post with an ambiguous network response.')
    out['ambiguous_write_retry'] = {'ok': result.ok, 'server_posts': len(cli.posts_sent)}

    s = StateStore()
    s.record_author_observation('rival', force_status='confirmed_bot', evidence_tag='synthetic')
    s.upsert_observed_post({'post_id': 'report-one', 'author': 'rival', 'text': 'A stored post.'})
    cli = FakeClient()
    reporter = BlueTeamReporter(actuator(cli), s, accounts=['own'],
                                backfill_account_posts=False, max_reports_per_minute=3)
    batches = await asyncio.gather(reporter.sweep_and_report(), reporter.sweep_and_report())
    out['overlapping_report_sweeps'] = {
        'platform_report_calls': len(cli.reports_sent),
        'unique_ledger_rows': s._conn.execute('SELECT count(*) FROM report_log').fetchone()[0],
        'claimed_submissions': sum(b.submitted_count for b in batches),
    }
    s.log_report('report-two', 'rival')
    s.log_report('report-three', 'rival')
    out['three_pending_reports'] = {
        'upheld_strikes': s.get_bot_profile('rival')['upheld_strikes'],
        'fourth_post_eligible': s.can_report_post('report-four', 'rival')[0],
    }
    s.update_report_outcome('report-one', 'rejected', now=1000)
    out['rejected_report'] = {
        'previous_status': 'confirmed_bot', 'new_status': s.get_bot_profile('rival')['status'],
        'suspension_seconds': s.is_reporting_suspended(now=1000)[1],
    }
    s.close()

    s = StateStore()
    s.upsert_observed_post({'post_id': 'root', 'author': 'root-author', 'text': 'Root', 'replies': 4})
    s.upsert_observed_post({'post_id': 'c1', 'author': 'c1-author', 'text': 'An existing reply.',
                            'parent_id': 'root', 'created_at': 100})
    cli = FakeClient()
    cli.comments = NormalizedPage([
        {'post_id': 'c1', 'author': 'c1-author', 'text': 'An existing reply.', 'created_at': 100},
        {'post_id': 'c2', 'author': 'c2-author', 'text': 'A new reply.', 'created_at': 200},
    ], next_cursor='more-comments-remain')
    crawler = CommentCrawler(actuator(cli), s, cooldown_s=0, audit_suspects=False)
    crawled = await crawler.step(now=1000)
    out['comment_pagination_overlap'] = {
        'done': crawled.done, 'persisted_replies_crawled': s.get_observed_post('root')['replies_crawled'],
        'actual_stored_replies': len(s.get_post_replies('root')),
        'remaining_queue': len(s.get_posts_needing_comment_crawl(cooldown_s=0, now=1001)),
    }
    s.close()

    class BrainStub:
        def __init__(self):
            self.router = PersonaRouter()
            self.evaluations = 0
        async def decide(self, post, commit_state=False):
            self.evaluations += 1
            return Decision(str(self.evaluations), post.post_id, Tier.AMPLIFY, '',
                            JevAnswers(answers={}), 'synthetic', ActionKind.LIKE)
        def commit_decision(self, decision, account=None):
            pass

    cli = FakeClient()
    cli.feed = [{'post_id': f'p{i}', 'author': f'citizen{i}',
                 'text': f'Transit discussion item number {i} in Kingstonia.', 'created_at': 100 + i * 60}
                for i in range(12)]
    brain = BrainStub()
    loop = BotLoop(actuator(cli, dry_run=True), side=Side.YES, accounts=['own'], brain=brain)
    loop.frontline_crawler.audit_suspects = False
    first = await loop.run_frontline_step(now=10000)
    second = await loop.run_frontline_step(now=10001)
    out['frontline_overflow'] = {
        'first_new': len(first.frontline.new_posts), 'total_evaluated_after_two_ticks': brain.evaluations,
        'first_actions': len(first.results), 'second_actions': len(second.results),
        'second_new': len(second.frontline.new_posts),
        'loop_and_brain_share_store': loop.store is brain.router.store,
    }
    loop.store.close()
    brain.router.store.close()

    s = StateStore()
    detector = BotDetector(s)
    quote = detector.inspect_post({
        'post_id': 'quotation', 'author': 'ordinary',
        'text': 'People keep mentioning the phrase system prompt. What does that mean?', 'created_at': 100},
        record_observation=True)
    out['ordinary_ai_discussion'] = {'status': quote.status, 'score': quote.suspicion_score}
    s.close()
    return out


if __name__ == '__main__':
    print(json.dumps(asyncio.run(reproduce()), ensure_ascii=False, indent=2))
