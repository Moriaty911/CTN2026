"""Lifecycle, review and scheduling fault cases. No live services or credentials."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing,redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,Mock,patch

from ctn.storage import StateStore
from ctn.storage.lifecycle import SYDNEY
from ctn.models import Post,ActionKind,Decision,JevAnswers,Tier
from ctn.accounts import fleet_plan
from ctn.discord_bot import DiscordConfig,build_bot,deliver_one
from ctn.moderation.review import ReviewService
from ctn.jev.postflight import PostflightVerdict
from ctn.client.mock import MockLegitClient,MockPost
from ctn.client.legit_v1 import NormalizedPage
from ctn.pipeline.brain import JevLlmBrain,BrainVerdict
from ctn.pipeline.scheduling import ordered_evaluations
from ctn.pipeline.context import hydrate_waiting_context
from ctn.warmup import initialise_step
from tests.test_persona80 import bound_router,batch_fixture
from tests.test_runtime_reliability import actor
from tests.test_campaign_v5 import strong_evidence


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.store=StateStore();self.addCleanup(self.store.close)

    def test_schedule_respects_revised_1800_and_sydney_boundaries(self):
        times=self.store.policy('phase_times')
        self.assertEqual(times['startup'],'2026-10-19T18:00:00')
        for text,expected in [('2026-10-19T18:00:00','startup'),('2026-10-20T16:59:59','startup'),('2026-10-20T17:00:00','shaping'),('2026-11-01T00:00:00','climax')]:
            self.assertEqual(self.store.effective_phase(now=datetime.fromisoformat(text).replace(tzinfo=SYDNEY).timestamp()),expected)

    def test_phase_override_expires_without_rewriting_account_history(self):
        self.store.override_phase('climax',200,now=100)
        self.assertEqual(self.store.effective_phase(now=150),'climax')
        self.assertEqual(self.store.effective_phase(now=201),'startup')
        with self.assertRaises(ValueError):self.store.override_phase('climax',50,now=100)

    def test_all_fifty_initialisation_intents_are_independent_of_old_quota(self):
        self.store.set_policy('daily_warmup_limit',1);self.store.set_policy('daily_original_limit',0)
        ids=[]
        for i in range(50):
            ident,reason=self.store.claim_action('post',f'account_{i}',text=f'Daily record {i}: distinct life note {i*i}.',metadata={'purpose':'initialise'})
            self.assertIsNotNone(ident,reason);ids.append(ident)
        self.assertEqual(len(set(ids)),50)

    def test_initialise_once_and_no_strategic_lock(self):
        ident,_=self.store.claim_action('post','a',text='Morning ferry coffee.',metadata={'purpose':'initialise'})
        self.store.start_action(ident);self.store.finish_action(ident,'succeeded',response={'post_id':'first'})
        self.assertEqual(self.store.lifecycle('a')['initialise_status'],'ready')
        self.assertEqual(self.store.get_account_state('a').original_locked_until,0)
        self.assertEqual(self.store.claim_action('post','a',text='Another daily note.',metadata={'purpose':'initialise'})[1],'already_initialised')
        self.store.finish_action(ident,'succeeded',response={'post_id':'first'})
        self.assertEqual(self.store.get_account_state('a').post_count,1)

    def test_strategic_original_locks_the_account_for_a_random_one_to_three_hours(self):
        ident,_=self.store.claim_action('post','a',text='Substantive YES policy argument.',metadata={'purpose':'P01'},now=100)
        self.store.start_action(ident);self.store.finish_action(ident,'succeeded',response={'post_id':'strategic'},now=110)
        self.assertTrue(110+3600<=self.store.get_account_state('a').original_locked_until<=110+10800)

    def test_unknown_initialisation_stays_unavailable_even_in_climax(self):
        ident,_=self.store.claim_action('post','a',text='Uncertain first message.',metadata={'purpose':'initialise'})
        self.store.start_action(ident);self.store.finish_action(ident,'unknown')
        self.store.override_phase('climax',time.time()+3600)
        self.assertFalse(self.store.account_initialise_ready('a'))
        self.assertEqual(self.store.lifecycle('a')['initialise_status'],'unknown')
        self.store.finish_action(ident,'succeeded',response={'post_id':'confirmed'})
        self.assertTrue(self.store.account_initialise_ready('a'))

    def test_unsent_claim_recovery_allows_initialisation_retry(self):
        ident,_=self.store.claim_action('post','a',text='Queued first note.',metadata={'purpose':'initialise'},now=100)
        self.store.recover_actions(now=1100)
        self.assertEqual(self.store.lifecycle('a')['initialise_status'],'pending')
        self.assertEqual(self.store.claim_action('post','a',text='Queued first note.',metadata={'purpose':'initialise'},now=1200)[0],ident)

    def test_climax_waiver_is_separate_from_real_initialisation(self):
        self.store.override_phase('climax',time.time()+3600)
        self.assertTrue(self.store.account_initialise_ready('new'))
        row=self.store.lifecycle('new')
        self.assertEqual(row['initialise_status'],'waived');self.assertEqual(row['initialised_at'],0)
        self.assertEqual(row['waiver_reason'],'campaign_climax')

    def test_maintenance_requires_reason_and_does_not_grant_high(self):
        with self.assertRaises(ValueError):self.store.set_maintenance('a',True,'good')
        ident,_=self.store.claim_action('post','a',text='Daily useful fixture.',metadata={'purpose':'initialise'})
        self.store.finish_action(ident,'succeeded',response={'post_id':'p'})
        self.store.set_maintenance('a',True,'manual review of comparable samples')
        self.assertEqual(self.store.get_account_state('a').capability_tier,'standard')
        self.assertTrue(self.store.lifecycle('a')['maintenance'])
        self.store.set_maintenance('a',False,'sustained decline')
        self.assertFalse(self.store.lifecycle('a')['maintenance'])

    def test_quota_change_survives_restart_and_is_rechecked_at_wire(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'state.db'
            with closing(StateStore(path)) as store:
                store.configure_campaign(2)
                ident,_=store.claim_action('post','a',text='A queued strategic message.',metadata={'purpose':'P01'})
                store.start_action(ident);store.configure_campaign(0)
                self.assertEqual(store.guard_wire_action(ident),(False,'daily_original_limit'))
            with closing(StateStore(path)) as store:self.assertEqual(store.policy('daily_original_limit'),0)

    def test_fleet_plan_includes_medical_and_law_from_start(self):
        rows=fleet_plan(50,phase='warmup');self.assertEqual(len(rows),50)
        self.assertIn('medical_public_health',{r['occupation_group'] for r in rows})
        self.assertIn('law_policy',{r['occupation_group'] for r in rows})

    def test_registration_transition_activates_and_queues_only_new_account(self):
        router=bound_router(self.store,('yes_001','yes_002'),active=False)
        self.store.initialise_registered_account('fresh_yes_001')
        self.assertEqual(router.get_state('fresh_yes_001').mode,'active')
        self.assertEqual(router.get_state('fresh_yes_002').mode,'standby')
        self.assertEqual(self.store.lifecycle('fresh_yes_001')['initialise_status'],'pending')

    def test_full_sync_and_secondary_worker_requires_migration(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'db'
            with closing(StateStore(path)) as store:self.assertEqual(store._conn.execute('PRAGMA synchronous').fetchone()[0],2)
            with closing(StateStore(path,migrate=False)) as store:self.assertEqual(store._conn.execute('PRAGMA user_version').fetchone()[0],6)
            with self.assertRaises(ValueError):StateStore(Path(td)/'unmigrated',migrate=False)

    def test_legacy_warmup_migration_releases_only_derived_lock(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'db'
            with closing(StateStore(path)) as store:
                ident,_=store.claim_action('post','a',text='Legacy daily fixture.',metadata={'purpose':'warmup'},now=100)
                store.finish_action(ident,'succeeded',response={'post_id':'p'},now=110)
                store.set_account_policy('a',original_locked_until=86510)
                store._conn.execute('DELETE FROM account_lifecycle');store._conn.execute('PRAGMA user_version=5');store._conn.commit()
            with closing(StateStore(path)) as store:
                self.assertEqual(store.get_account_state('a').original_locked_until,0)
                self.assertEqual(store.lifecycle('a')['initialise_status'],'ready')
            self.assertTrue(Path(str(path)+'.before-v6.bak').exists())


class DependencyTests(unittest.TestCase):
    def setUp(self):self.store=StateStore();self.addCleanup(self.store.close)
    def waiting(self):
        now=time.time();self.store.upsert_observed_post(Post('child','author','Which condition applies?',now,raw={'parent_id':'parent','text_complete':True}))
        task=self.store.claim_post_tasks()[0];self.store.wait_for_material(task,'corpus','needs_context');return task
    def test_parent_arrival_resumes_without_spending_retry(self):
        self.waiting();self.store.resume_material_tasks('corpus')
        self.assertEqual(self.store._conn.execute("SELECT attempts FROM post_tasks WHERE post_id='child'").fetchone()[0],0)
        self.store.upsert_observed_post(Post('parent','parentauthor','The missing parent context.',time.time()))
        self.store.resume_material_tasks('corpus')
        self.assertEqual(self.store._conn.execute("SELECT status FROM post_tasks WHERE post_id='child'").fetchone()[0],'queued')
    def test_reobservation_without_material_change_does_not_resume(self):
        self.waiting();self.store.resume_material_tasks('corpus')
        self.store.upsert_observed_post(Post('child','author','Which condition applies?',time.time(),raw={'parent_id':'parent','text_complete':True}))
        self.store.resume_material_tasks('corpus')
        self.assertEqual(self.store._conn.execute("SELECT status FROM post_tasks WHERE post_id='child'").fetchone()[0],'waiting_materials')
    def test_first_background_registration_resumes(self):
        self.waiting();self.store.resume_material_tasks('corpus');self.store.set_policy('background_version','new')
        self.store.resume_material_tasks('corpus')
        self.assertEqual(self.store._conn.execute("SELECT status FROM post_tasks WHERE post_id='child'").fetchone()[0],'queued')
    def test_needs_assessment_with_missing_context_has_explicit_wait_disposition(self):
        verdict=BrainVerdict(reason='jev:needs_assessment',detail={'flow_status':'needs_context','action_status':'needs_assessment'})
        self.assertEqual(JevLlmBrain._disposition(verdict,''),'wait')
    def test_central_question_precedes_concern_keyword(self):
        data={'flow':{'deep':{'assessment':{'output':{'answers':{'expression_function':{'value':'question'}}}}}}}
        self.assertEqual(JevLlmBrain._select_opposing_purpose(data,"I'm worried about my family. What do the trials show?"),'P02')


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=StateStore();self.addCleanup(self.store.close)
        self.service=ReviewService(self.store);self.store.set_policy('report_mode','manual')
        self.payload=strong_evidence(self.store);self.ident=self.store.store_report_candidate(self.payload)
    def approve(self,**kwargs):return self.service.review(self.ident,'approve',revision=1,actor='reviewer',**kwargs)
    async def test_duplicate_interaction_returns_same_approval(self):
        first=self.approve(interaction_id='one');second=self.approve(interaction_id='one')
        self.assertEqual(first,second)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM report_approvals').fetchone()[0],1)
    async def test_concurrent_review_has_one_winner(self):
        def review():
            try:return self.approve()['status']
            except ValueError:return 'stale'
        results=await asyncio.gather(asyncio.to_thread(review),asyncio.to_thread(review))
        self.assertEqual(sorted(results),['approved','stale'])
    async def test_approval_does_not_grant_clearance(self):
        self.approve();self.assertEqual(self.store.report_state_ready(),(False,'reporting_state_unknown'))
        self.service.clearance('official observation','admin')
        self.assertTrue(self.store.report_state_ready()[0]);self.assertFalse(self.store.report_state_ready(automatic=True)[0])
    async def test_rejection_suppresses_identical_evidence(self):
        self.service.review(self.ident,'reject',revision=1,reason='insufficient independence')
        self.assertEqual(self.store.store_report_candidate(self.payload),self.ident)
        with self.assertRaises(ValueError):self.approve()
    async def test_defer_and_expired_approval_return_to_review(self):
        self.service.review(self.ident,'defer',revision=1,reason='wait',resume_at=time.time()+30)
        self.store.refresh_report_reviews(now=time.time()+31)
        row=self.store.report_review(self.ident);self.assertEqual(row['status'],'review')
        self.service.review(self.ident,'approve',revision=row['revision'])
        self.store.refresh_report_reviews(now=time.time()+301)
        self.assertEqual(self.store.report_review(self.ident)['status'],'review')
    async def test_changed_evidence_invalidates_old_card(self):
        self.approve()
        changed={**self.payload,'evidence_hash':'different','target_version':2}
        new=self.store.store_report_candidate(changed)
        self.assertNotEqual(new,self.ident);self.assertEqual(self.store.report_review(self.ident)['status'],'stale')
        with self.assertRaises(ValueError):self.approve()
    async def test_full_manual_send_pending_ruling_and_conflict(self):
        approval=self.approve()['approval_id'];self.service.clearance('official reporting page','admin')
        platform=MockLegitClient(failure_rate=0,latency_ms=(0,0))
        platform.posts['leak']=MockPost('leak','enemy','Synthetic report target.',time.time())
        result=await actor(platform,self.store).report('reporter','leak',detail=self.payload['detail'],context={'target_author':'enemy','evidence_hash':self.payload['evidence_hash'],'candidate_id':self.ident,'approval_id':approval,'report_attempt_id':'attempt'})
        self.assertTrue(result.ok,result.detail);self.assertEqual(self.store.report_review(self.ident)['status'],'pending')
        at=time.time();event=self.service.prepare_ruling('attempt','rejected',at,'official activity log')
        self.assertTrue(self.service.ruling(event,'admin'));self.assertFalse(self.service.ruling(event,'admin'))
        self.assertTrue(self.store.is_reporting_suspended()[0])
        with self.assertRaises(ValueError):self.service.ruling({**event,'outcome':'upheld','event_id':'conflict'},'admin')
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM quarantined_events').fetchone()[0],1)
    async def test_aged_pending_alert_is_once_and_does_not_release(self):
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO report_attempts VALUES('aged','a','post','p','enemy','','pending',?,0,'','{}')",(time.time()-90000,))
        self.store.refresh_report_reviews();self.store.refresh_report_reviews()
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM discord_outbox WHERE event_key='aged:aged'").fetchone()[0],1)
        self.assertEqual(self.store._conn.execute("SELECT status FROM report_attempts WHERE id='aged'").fetchone()[0],'pending')
    async def test_outbox_failed_delivery_is_retriable(self):
        transport=AsyncMock(side_effect=OSError('network'))
        await deliver_one(self.store,transport)
        row=self.store._conn.execute('SELECT * FROM discord_outbox ORDER BY id LIMIT 1').fetchone()
        self.assertEqual(row['status'],'queued');self.assertGreater(row['next_attempt_at'],time.time())
    async def test_pause_and_clearance_do_not_cancel_suspension(self):
        self.service.pause('admin');self.assertEqual(self.store.report_state_ready()[1],'reporting_paused')
        self.store.set_counter('report_suspended_until_ms',int((time.time()+600)*1000))
        with self.assertRaises(ValueError):self.service.clearance('manual observation','admin')
    async def test_affiliation_priority_uses_explicit_source(self):
        from ctn.moderation.reporter import BlueTeamReporter
        self.service.affiliation('enemy','no','team','official team info')
        r=BlueTeamReporter(actor(MockLegitClient(),self.store),self.store,accounts=['r'])
        prof=self.store.get_bot_profile('enemy')
        self.assertEqual(r._candidate_priority(prof,sydney_day='2026-10-18',now=time.time())[0],0)


class DiscordTests(unittest.TestCase):
    def setUp(self):self.config=DiscordConfig('synthetic',1,2,reviewer_users=frozenset({3}),admin_users=frozenset({4}))
    def test_scope_and_two_level_authorization(self):
        self.assertNotIn('synthetic',repr(self.config))
        self.config.authorize(1,2,3);self.config.authorize(1,2,4,admin=True)
        for guild,channel,user,admin in [(None,2,4,False),(1,9,4,False),(1,2,5,False),(1,2,3,True)]:
            with self.assertRaises(ValueError):self.config.authorize(guild,channel,user,admin=admin)
    def test_only_explicit_team_channel_members_can_read(self):
        self.config.authorize(1,2,99,read=True)
        with self.assertRaises(ValueError):self.config.authorize(None,None,4,read=True)
    def test_role_permissions_and_config_validation(self):
        cfg=DiscordConfig('synthetic',1,2,reviewer_roles=frozenset({10}),admin_roles=frozenset({20}))
        cfg.authorize(1,2,99,[10]);cfg.authorize(1,2,99,[20],admin=True)
        with patch.dict('os.environ',{},clear=True):
            with self.assertRaises(ValueError):DiscordConfig.load()
    def test_actual_command_tree_builds_without_network(self):
        try:import discord
        except ImportError:self.skipTest('optional discord dependency not installed')
        with closing(StateStore()) as store:
            bot=build_bot(self.config,store)
            self.assertEqual({c.name for c in bot.tree.get_commands(guild=discord.Object(id=1))},{'reports','report','reporting','ruling'})
            self.assertFalse(bot.intents.message_content)
            self.assertFalse(bot.allowed_mentions.everyone)


class AsyncIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifty_initialise_posts_complete_without_global_daily_limit(self):
        with closing(StateStore()) as store:
            ids=tuple(row['persona_id'] for row in fleet_plan(50))
            router=bound_router(store,ids);store.set_policy('daily_original_limit',0);store.set_policy('daily_warmup_limit',1);store.set_policy('initialise_spread',{'window_seconds':0,'min_gap_seconds':0})
            client=MockLegitClient(failure_rate=0,latency_ms=(0,0));act=actor(client,store)
            checker=Mock();checker.check.return_value=PostflightVerdict(True,'approved')
            for _ in range(50):
                result=await initialise_step(act,router,checker)
                self.assertTrue(result.get('ok'),result)
            self.assertEqual(store._conn.execute("SELECT COUNT(*) FROM account_lifecycle WHERE initialise_status='ready'").fetchone()[0],50)
            self.assertEqual(store._conn.execute('SELECT MAX(original_locked_until) FROM account_states').fetchone()[0],0)
            self.assertEqual((await initialise_step(act,router,checker))['status'],'idle')
    async def test_slow_assessment_does_not_block_ready_draft(self):
        with closing(StateStore()) as store:
            for ident in ('slow','fast'):store.upsert_observed_post(Post(ident,'other','A useful question?',time.time()))
            async def assess(post):
                await asyncio.sleep(.2 if post.post_id=='slow' else .001)
                return Mock(to_dict=lambda:{'action_decision':{'action':'reply'}})
            async def draft(post,**kwargs):
                return Decision(post.post_id,post.post_id,Tier.PUBLISH,'a',JevAnswers(),action=ActionKind.REPLY,text='Relevant reply.')
            loop=Mock(store=store,brain=Mock(assess=assess,decide=draft),_own=set(),actuator=Mock(dry_run=True))
            tasks=store.claim_post_tasks()
            stream=ordered_evaluations(loop,tasks)
            first=await anext(stream)
            self.assertEqual(first[0]['post_id'],'fast')
            await stream.aclose()
    async def test_missing_parent_is_acquired_through_metered_read(self):
        with closing(StateStore()) as store:
            store.upsert_observed_post(Post('child','a','A question?',time.time(),raw={'parent_id':'parent'}))
            task=store.claim_post_tasks()[0];store.wait_for_material(task,'hash','context')
            act=Mock();act.get_post=AsyncMock(return_value={'post_id':'parent','author':'b','text':'Full parent context.'})
            await hydrate_waiting_context(act,store);store.resume_material_tasks('hash')
            self.assertEqual(store.get_observed_post('parent')['text'],'Full parent context.')
            act.get_post.assert_awaited_once_with('parent')
    @patch('ctn.macro.schedule.posting_accounts', lambda store: 10)
    @patch.dict('ctn.macro.schedule.POLICY', {'slot_jitter': 0.0, 'active_hours': [0, 24]})
    async def test_macro_job_lease_and_saved_draft_survive_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'db'
            with closing(StateStore(path)) as store:
                job=store.claim_macro_job(now=100)
                with store.batch_transaction():store._conn.execute('UPDATE macro_jobs SET payload_json=? WHERE id=?',(json.dumps({'draft':'saved'}),job['id']))
                self.assertIsNone(store.claim_macro_job(now=101))
            with closing(StateStore(path)) as store:
                recovered=store.claim_macro_job(now=701)
                self.assertEqual(recovered['id'],job['id']);self.assertEqual(json.loads(recovered['payload_json']),{'draft':'saved'})
                self.assertNotEqual(recovered['owner'],job['owner'])
    # One posting account: few, long slots, so the 10-minute retry fits whatever the time of day.
    @patch('ctn.macro.schedule.posting_accounts', lambda store: 1)
    @patch.dict('ctn.macro.schedule.POLICY', {'slot_jitter': 0.0, 'active_hours': [0, 24]})
    async def test_macro_existing_success_never_generates_or_sends_again(self):
        from ctn.macro.worker import MacroWorker
        with closing(StateStore()) as store:
            job=store.claim_macro_job();store.finish_macro_job(job,'failed')
            # Prepare recovery of the same pulse after a crash immediately after publication.
            with store.batch_transaction():store._conn.execute("UPDATE macro_jobs SET status='queued' WHERE id=?",(job['id'],))
            ident,_=store.claim_action('post','a',text='The prior published strategic contribution.',metadata={'macro_job_id':job['id'],'macro_owner':job['owner']})
            # A released macro lease cannot authorize a publication.
            self.assertIsNone(ident)
            runner=AsyncMock()
            worker=MacroWorker(store,Mock(),Mock(default_side='yes'),Mock(),Mock(),pi_runner=runner,collector=Mock())
            # No account is eligible, so model use is avoided; the slot is retried later with another persona.
            worker.router.registry.list_by_side.return_value=[]
            self.assertEqual((await worker.step())['status'],'retry');runner.assert_not_awaited()
            row=store._conn.execute("SELECT status,attempts FROM macro_jobs WHERE id LIKE 'macro:simulated:%'").fetchone()
            self.assertEqual(tuple(row),('queued',1))


class AdditionalFaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_positive_registration_check_respects_explicit_manual_stop(self):
        with closing(StateStore()) as store:
            bound_router(store,('yes_001',),active=False)
            store.set_account_policy('fresh_yes_001',mode='standby')
            store.initialise_registered_account('fresh_yes_001')
            self.assertEqual(store.get_account_state('fresh_yes_001').mode,'standby')
    async def test_candidate_score_change_does_not_invalidate_identical_evidence(self):
        with closing(StateStore()) as store:
            payload=strong_evidence(store);first=store.store_report_candidate(payload)
            changed={**payload,'suspicion_score':payload['suspicion_score']+1}
            self.assertEqual(store.store_report_candidate(changed),first)
            self.assertEqual(store.report_review(first)['revision'],1)
    @patch.dict('ctn.macro.schedule.POLICY', {'slot_jitter': 0.0, 'active_hours': [0, 24]})
    async def test_macro_generation_publication_and_restart_dedup(self):
        from ctn.macro.worker import MacroWorker
        from ctn.rag.corpus import Corpus
        with closing(StateStore()) as store:
            router=bound_router(store,('yes_001',));account=router.registry.get('yes_001').account_id
            with store.batch_transaction():store._conn.execute("INSERT INTO account_lifecycle(account_id,initialise_status,initialised_at) VALUES(?,'ready',?)",(account,time.time()))
            corpus=Corpus();corpus.install_sources(store)
            act=actor(MockLegitClient(failure_rate=0,latency_ms=(0,0)),store)
            checker=Mock();checker.check.return_value=PostflightVerdict(True,'approved')
            runner=AsyncMock(return_value={'act':True,'text':'I support the shared entry requirement. A clear explanation of the evidence and remaining uncertainty matters to me.','purpose':'P01','branch':'branch_unknown'})
            collector=Mock();collector.collect=AsyncMock(return_value={'trending_posts':[{'text':'What evidence supports vaccination-proof requirements for public spaces?'}]})
            worker=MacroWorker(store,act,router,checker,corpus,pi_runner=runner,collector=collector)
            result=await worker.step();self.assertTrue(result.get('ok'),result)
            self.assertEqual(act._client.write_count,1)
            with store.batch_transaction():store._conn.execute("UPDATE macro_jobs SET status='queued'")
            recovered=await worker.step();self.assertEqual(recovered['status'],'succeeded')
            self.assertEqual(act._client.write_count,1);runner.assert_awaited_once()
    async def test_real_discord_button_defers_before_review_and_checks_actor(self):
        try:import discord
        except ImportError:self.skipTest('optional dependency')
        from types import SimpleNamespace
        with closing(StateStore()) as store:
            store.set_policy('report_mode','manual');ident=store.store_report_candidate(strong_evidence(store))
            cfg=DiscordConfig('synthetic',1,2,reviewer_users=frozenset({3}),admin_users=frozenset({4}))
            bot=build_bot(cfg,store)
            from tests.test_discord_panels import interaction
            opening=interaction(3)
            command=next(c for c in bot.tree.get_commands(guild=discord.Object(id=1)) if c.name=='report')
            await command.callback(opening,ident)
            view=opening.edit_original_response.await_args.kwargs['view']
            attacker=interaction(99);await view.children[0].callback(attacker)
            self.assertEqual(store.report_review(ident)['status'],'review')
            reviewer=interaction(3);await view.children[0].callback(reviewer)
            reviewer.response.defer.assert_awaited_once()
            self.assertEqual(store.report_review(ident)['status'],'approved')
            self.assertFalse(store.report_state_ready()[0])


class AuditRegressionTests(unittest.TestCase):
    def test_sync_publisher_preserves_source_snapshot_to_real_wire_seam(self):
        from ctn.macro.publisher import StrategicPublisher
        from ctn.rag.corpus import Corpus
        class Wire:
            def __init__(self):self.writes=0
            async def post(self,account,text,**kwargs):
                self.writes+=1
                return {'post_id':'offline-wire','created_at':time.time()}
        with closing(StateStore()) as store:
            router=bound_router(store,('yes_001',));wire=Wire()
            checker=Mock();checker.check.return_value=PostflightVerdict(True,'approved')
            snapshot=store.save_rag_snapshot(Corpus().retrieve('Public spaces and hospital capacity',purpose='P01'))
            result=StrategicPublisher(actor(wire,store),checker,router).publish_sync('fresh_yes_001','I support proof for shared spaces, with a clear explanation of implementation.',rag_snapshot_id=snapshot)
            self.assertTrue(result['ok'],result);self.assertEqual(wire.writes,1)
            intent=store.list_actions('succeeded')[0]
            self.assertEqual(json.loads(intent['metadata_json'])['rag_snapshot_id'],snapshot)
    def test_full_workflow_missing_context_remains_waiting_after_three_ticks(self):
        from ctn.jev import JevClient,JevWorkflow
        from ctn.config import JevConfig
        from ctn.models import Side
        from ctn.pipeline.loop import BotLoop
        from tests.test_jev_evaluator import FakeTransport,choose
        def answer(body,data):
            choose(data,'policy_stance','no')
            if 'specificity_evidence' in data['answers']:choose(data,'specificity_evidence','insufficient')
        client=JevClient(JevConfig(api_key='synthetic-context',max_retries=0),transport=FakeTransport(answer))
        with closing(StateStore()) as store:
            router=bound_router(store,('yes_001',));brain=JevLlmBrain(JevWorkflow(client),None,side=Side.YES,router=router)
            loop=BotLoop(actor(MockLegitClient(failure_rate=0),store,dry=True),side=Side.YES,accounts=['fresh_yes_001'],brain=brain,store=store)
            store.upsert_observed_post(Post('child','citizen','What about that condition?',time.time(),raw={'parent_id':'missing','platform_level':'standard'}))
            async def run():
                for _ in range(3):await loop.process_pending()
            asyncio.run(run())
            task=store._conn.execute("SELECT status,attempts FROM post_tasks WHERE post_id='child'").fetchone()
            self.assertEqual(task['status'],'waiting_materials');self.assertEqual(task['attempts'],0)

if __name__=='__main__':unittest.main()

class RuntimePolicyTests(unittest.TestCase):
    @patch('ctn.macro.schedule.posting_accounts', lambda store: 10)
    @patch.dict('ctn.macro.schedule.POLICY', {'slot_jitter': 0.0, 'active_hours': [0, 24]})
    def test_simulated_macro_pulse_does_not_block_live_pulse(self):
        with closing(StateStore()) as store:
            simulated=store.claim_macro_job(now=100,mode='simulated')
            store.finish_macro_job(simulated,'simulated')
            live=store.claim_macro_job(now=101,mode='live')
            self.assertIsNotNone(live);self.assertNotEqual(live['id'],simulated['id'])
    def test_new_unknown_policy_has_distinct_provenance(self):
        from ctn.jev.actions import ActionPolicy
        self.assertEqual(ActionPolicy().to_dict()['unknown_target_level_policy'],'allow_standard_preserve_unknown')
        self.assertEqual(ActionPolicy(version='jev-actions.v2').to_dict()['unknown_target_level_policy'],'await_metadata')


class ReplacementPlanTests(unittest.TestCase):
    def test_explicit_unused_profile_is_reachable_outside_initial_fifty(self):
        initial={r['persona_id'] for r in fleet_plan(50)}
        replacement=fleet_plan(50,persona_id='yes_080',account_id='replacement_080')
        self.assertNotIn('yes_080',initial)
        self.assertEqual(replacement,[{'persona_id':'yes_080','account_id':'replacement_080','occupation_group':'independent_commentary','display_name':'Zain Porter'}])
        with self.assertRaises(ValueError):fleet_plan(50,persona_id='missing')
        with self.assertRaises(ValueError):fleet_plan(50,persona_id='yes_080',account_id='bad handle')
