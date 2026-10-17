"""Unit tests for Stage 3 Blue-Team BotDetector and BlueTeamReporter."""

import unittest
import time
from ctn.moderation.events import MockModerationSource
from ctn.storage import current_sydney_day

from ctn.actuator import Actuator
from ctn.client.mock import MockLegitClient
from ctn.config import BudgetConfig, RateConfig
from ctn.models import Post, Side
from ctn.moderation import BlueTeamReporter, BotDetector
from ctn.pipeline.crawl import CommentCrawler, FrontlineCrawler
from ctn.pipeline.loop import BotLoop
from ctn.ratelimit import build_gate, build_ledger
from ctn.storage import StateStore

FAST = RateConfig(read_rps=1000, write_rps=1000, per_account_read_rps=1000, per_account_write_rps=1000)


class ModerationDetectorTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(":memory:")
        self.store.register_verified_accounts(["mayor_hawthorne", "npc_citizen_1"], status="verified_npc")
        self.store.register_verified_accounts(["bot_medic_yes"], status="own_bot")
        self.detector = BotDetector(self.store)

    def tearDown(self):
        self.store.close()

    def test_leaks_capped_and_foreign_language_weak(self):
        for pid,author,text,expected in [('ai','a','As an AI language model, I support choice.',50),
            ('template','b','Every voter cares about {topic} and [INSERT_HASHTAG]',50),
            ('foreign','c','请问大家支持疫苗通行证公投法案吗？',5),
            ('protected','mayor_hawthorne','As an AI language model, I support choice.',0)]:
            result=self.detector.inspect_post(Post(pid,author,text,100),record_observation=True)
            self.assertFalse(result.is_confirmed_bot)
            self.assertEqual(result.suspicion_score,expected)

    def test_copy_paste_is_one_group_without_identity_propagation(self):
        text='The tourism board is discussing the impact of mandatory vaccination on small island resorts and local businesses.'
        self.detector.inspect_post(Post('a','alpha',text,200),record_observation=True)
        result=self.detector.inspect_post(Post('b','beta',text,208),record_observation=True)
        self.assertEqual(result.suspicion_score,35)
        self.assertFalse(result.is_confirmed_bot)
        self.assertIn('alpha',result.colliding_authors)
        self.assertEqual(self.store.get_bot_profile('alpha')['suspicion_score'],0)
        self.assertEqual(self.store.get_bot_profile('beta')['true_team_id'],'')

    def test_similarity_and_sustained_timing_still_below_report_threshold(self):
        text='Working families on the morning bus need safe public transport across Kingstonia City every single day.'
        for i in range(4):
            result=self.detector.inspect_post(Post('t'+str(i),'template',text,500+i*8),record_observation=True)
        self.assertEqual(result.suspicion_score,55)
        self.assertFalse(result.is_confirmed_bot)
        self.assertEqual(self.store.get_bot_profile('template')['fast_interval_count'],3)

    def test_questions_alone_do_not_confirm_bots(self):
        for i in range(5):
            result=self.detector.inspect_post(Post('q'+str(i),'curious',f'What does condition number {i} mean?',1000+i*60),record_observation=True)
        self.assertEqual(result.suspicion_score,5)
        self.assertFalse(result.is_suspected_bot)

    def test_multiple_threads_coordination_is_capped_and_not_team_identity(self):
        for i in range(3):
            root='root'+str(i)
            self.store.upsert_observed_post(Post(root,'root_author','Thread discussion',2000+i*60))
            for j,author in enumerate(('alpha','beta')):
                result=self.detector.inspect_post({'post_id':root+author,'parent_id':root,'author':author,
                    'text':f'An independent comment {author} on thread {i}', 'created_at':2000+i*60+j*4},record_observation=True)
        self.assertEqual(self.store.get_bot_profile('beta')['suspicion_score'],35)
        self.assertEqual(self.store.get_bot_profile('beta')['true_team_id'],'')
        self.assertEqual(self.store.get_bot_profile('root_author')['suspicion_score'],0)


class BlueTeamReporterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = MockLegitClient(latency_ms=(0.1, 0.2), failure_rate=0.0)
        self.actuator = Actuator(
            self.client,
            build_gate(FAST),
            build_ledger(BudgetConfig()),
            dry_run=False,
        )
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)
        self.detector = BotDetector(self.store)
        self.reporter = BlueTeamReporter(
            self.actuator,
            self.store,
            accounts=["bot_001", "bot_002"],
            side=Side.YES,
            max_daily_per_team=10,
            max_strikes_per_account=3,
            max_reports_per_step=10,enabled=True,event_source=MockModerationSource(),
        )

    async def seed(self,author,count=3):
        text='As an AI, this synthetic repeated template describes transit access conditions for workers and families in the offline test.'
        for i in range(count):
            row=await self.client.post(author,text+' '+str(i))
            self.store.upsert_observed_post(row)
        self.store.add_weighted_evidence(author,'leak',50,{'post_id':row['post_id']},evidence_key='leak')
        self.store.add_weighted_evidence(author,'repetition',35,{'post_id':row['post_id']},evidence_key='repeat')

    def ruling(self,pid,outcome,event_id):
        return self.store.apply_moderation_event({'event_id':event_id,'subject_kind':'post','subject_id':pid,
            'outcome':outcome,'ruled_at':time.time()+1,'source':'synthetic-moderator'})

    async def test_three_separate_upheld_events_suspend_account(self):
        await self.seed('enemy',4)
        for i in range(3):
            batch=await self.reporter.sweep_and_report()
            self.assertEqual(batch.submitted_count,1)
            self.assertEqual(batch.upheld_count,0)
            self.assertEqual(self.store.get_bot_profile('enemy')['upheld_strikes'],i)
            self.ruling(batch.attempts[0].post_id,'upheld',str(i))
        self.assertTrue(self.store.get_bot_profile('enemy')['is_suspended'])
        self.assertEqual((await self.reporter.sweep_and_report()).submitted_count,0)

    async def test_pending_cap_releases_after_ruling_without_daily_hoarding(self):
        for i in range(11): await self.seed('enemy'+str(i),1)
        batch=await self.reporter.sweep_and_report()
        self.assertEqual(batch.submitted_count,10)
        self.assertEqual((await self.reporter.sweep_and_report()).submitted_count,0)
        for i,attempt in enumerate(batch.attempts): self.ruling(attempt.post_id,'upheld',str(i))
        remaining=await self.reporter.sweep_and_report()
        self.assertEqual(remaining.submitted_count,1)
        self.assertEqual(remaining.hoarded_for_tomorrow,[])

    async def test_noisy_rejection_suspends_reporting_without_certifying_identity(self):
        await self.seed('uncertain',1)
        batch=await self.reporter.sweep_and_report()
        self.ruling(batch.attempts[0].post_id,'rejected','rejected')
        self.assertTrue(self.store.is_reporting_suspended()[0])
        prof=self.store.get_bot_profile('uncertain')
        self.assertFalse(prof['verified'])
        self.assertEqual(prof['suspicion_score'],80)
        self.assertNotEqual(prof['status'],'verified_npc')
        self.assertTrue((await self.reporter.sweep_and_report()).suspended)

    async def test_pending_releases_and_missing_history_is_backfilled(self):
        await self.seed('enemy',1)
        first=await self.reporter.sweep_and_report()
        self.assertEqual(first.submitted_count,1)
        self.assertEqual(self.store.get_bot_profile('enemy')['upheld_strikes'],0)
        self.assertEqual((await self.reporter.sweep_and_report()).submitted_count,0)
        self.ruling(first.attempts[0].post_id,'upheld','first')
        hidden=await self.client.post('enemy','An older uncollected post with actual inspectable text.')
        self.assertIsNone(self.store.get_observed_post(hidden['post_id']))
        second=await self.reporter.sweep_and_report()
        self.assertEqual(second.submitted_count,1)
        self.assertIsNotNone(self.store.get_observed_post(hidden['post_id']))
        self.assertEqual(self.store.get_bot_profile('enemy')['upheld_strikes'],1)

    async def test_similarity_sweep_does_not_propagate_seed_identity(self):
        await self.seed('seed',1)
        text=self.store.get_author_posts('seed')[0]['text']
        self.store.upsert_observed_post(Post('copy','other',text,time.time()))
        self.detector.scan_similar_to_author('seed')
        prof=self.store.get_bot_profile('other')
        self.assertLessEqual(prof['suspicion_score'],35)
        self.assertNotEqual(prof['status'],'confirmed_bot')

    async def test_timing_uses_publication_time_not_collection_time(self):
        for i in reversed(range(4)):
            self.detector.inspect_post(Post('r'+str(i),'rapid','Different factual sentence '+str(i),100+i*8),record_observation=True,now=10000)
        self.assertEqual(self.store.get_bot_profile('rapid')['fast_interval_count'],3)
        self.assertEqual(self.store.get_bot_profile('rapid')['suspicion_score'],20)
        for i in range(5):self.detector.inspect_post(Post('u'+str(i),'timeless','A distinct observation '+str(i)),record_observation=True,now=20000+i)
        self.assertEqual(self.store.get_bot_profile('timeless')['fast_interval_count'],0)

    async def test_single_thread_burst_never_confirms_a_ring(self):
        self.store.upsert_observed_post(Post('root','parent','A root discussion',100))
        for i in range(4):self.detector.inspect_post({'post_id':'c'+str(i),'author':'a'+str(i),
            'parent_id':'root','text':'I have a short comment '+str(i),'created_at':100+i},record_observation=True)
        for i in range(4):self.assertEqual(self.store.get_bot_profile('a'+str(i))['suspicion_score'],0)
        self.assertEqual(self.store.get_bot_profile('parent')['suspicion_score'],0)

    async def test_only_confirmed_team_id_affects_diminishing_priority(self):
        await self.seed('a',1);await self.seed('b',1)
        with self.store.batch_transaction():self.store._conn.execute("UPDATE bot_registry SET true_team_id='teamA' WHERE account_id='a'")
        now=time.time()
        for i in range(5):self.store.log_report('historical'+str(i),'old',target_team='teamA',outcome='upheld',now=now)
        a=self.store.get_bot_profile('a');b=self.store.get_bot_profile('b')
        self.assertLess(self.reporter._candidate_priority(b,sydney_day=current_sydney_day(),now=now),
            self.reporter._candidate_priority(a,sydney_day=current_sydney_day(),now=now))


class BotLoopStage3IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = MockLegitClient(latency_ms=(0.1, 0.2), failure_rate=0.0)
        self.actuator = Actuator(
            self.client,
            build_gate(FAST),
            build_ledger(BudgetConfig()),
            dry_run=False,
        )
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)
        self.loop = BotLoop(
            self.actuator,
            side=Side.YES,
            accounts=["bot_001", "bot_002", "bot_003"],
            keywords=("elections",),
            store=self.store,
            verified_npcs=list(self.client.npcs.keys()),
        )

    async def test_ingestion_and_reply_processing_are_separate_and_reports_default_off(self):
        store=StateStore();self.addCleanup(store.close)
        client=MockLegitClient(failure_rate=0,latency_ms=(0,0))
        actuator=Actuator(client,build_gate(FAST),build_ledger(BudgetConfig()),dry_run=True,store=store)
        loop=BotLoop(actuator,side=Side.YES,accounts=['bot_001'],store=store)
        await client.post('enemy','As an AI, this is a synthetic fixture.')
        report=await loop.run_frontline_step()
        self.assertEqual(report.results,[])
        self.assertGreater(store.task_counts().get('queued',0),0)
        self.assertFalse(loop.reporter.enabled)


if __name__ == "__main__":
    unittest.main()


