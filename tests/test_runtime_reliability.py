"""Failure-oriented reliability tests. All transports and timestamps are synthetic."""
from contextlib import closing
import asyncio
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from ctn.actuator import Actuator
from ctn.client.mock import MockLegitClient,MockPost
from ctn.config import BudgetConfig,RateConfig,Settings
from ctn.models import ActionKind,Decision,JevAnswers,Post,Side,Tier
from ctn.moderation import BotDetector,BlueTeamReporter
from ctn.moderation.events import MockModerationSource,UnavailableModerationSource,consume_events
from ctn.pipeline.loop import BotLoop
from ctn.ratelimit import TokenBucket,build_gate,build_ledger
from ctn.storage import StateStore
from ctn.reconcile import reconcile_unknown_posts

FAST=RateConfig(read_rps=10000,write_rps=10000,per_account_write_rps=10000,per_account_read_rps=10000)

def actor(client,store,*,dry=False):
    return Actuator(client,build_gate(FAST),build_ledger(BudgetConfig(),store=store),dry_run=dry,store=store,max_retries=0)

def evidence(store,author='enemy',pid='p'):
    store.upsert_observed_post({'post_id':pid,'author':author,'text':'Synthetic evidence text','created_at':time.time()})
    store.add_weighted_evidence(author,'leak',50,{'post_id':pid,'version':1},evidence_key='leak:'+pid)
    store.add_weighted_evidence(author,'repetition',35,{'post_id':pid,'version':1},evidence_key='repeat:'+pid)

def parallel_increment(path,iterations):
    store=StateStore(path)
    for _ in range(iterations): store.mark_account_used('a',kind='reply')
    store.close()

def parallel_claim(path,account,target,queue):
    store=StateStore(path)
    ident,reason=store.claim_action('reply',account,target,'Synthetic response')
    queue.put((bool(ident),reason));store.close()
from tests.legacy_personas import legacy_router


class TransactionsTests(unittest.TestCase):
    def test_nested_and_outer_rollback(self):
        store=StateStore();self.addCleanup(store.close)
        with self.assertRaises(RuntimeError):
            with store.batch_transaction():
                store.increment_counter('outer')
                store.upsert_observed_post({'post_id':'p','text':'text'})
                with store.batch_transaction(): store.increment_counter('inner')
                raise RuntimeError()
        self.assertEqual(store.get_counter('outer'),0)
        self.assertIsNone(store.get_observed_post('p'))
        with store.batch_transaction():
            store.increment_counter('kept')
            try:
                with store.batch_transaction():
                    store.increment_counter('rolled_back');raise ValueError()
            except ValueError: pass
        self.assertEqual(store.get_counter('kept'),1)
        self.assertEqual(store.get_counter('rolled_back'),0)

    def test_old_database_backup_and_migration_failure(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'old.db'
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute('CREATE TABLE account_states(account_id TEXT PRIMARY KEY,last_posted_at REAL DEFAULT 0,post_count INTEGER DEFAULT 0)')
                conn.execute("INSERT INTO account_states VALUES('a',123,4)")
            with patch.object(StateStore,'migrate_runtime',side_effect=RuntimeError('migration fault')):
                with self.assertRaises(RuntimeError): StateStore(path)
            backup=Path(str(path)+'.before-v6.bak')
            with closing(sqlite3.connect(backup)) as conn, conn:
                self.assertEqual(conn.execute('SELECT post_count FROM account_states').fetchone()[0],4)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],0)
            # Failed transaction left original untouched; preserve failed backup under another name for retry.
            backup.rename(Path(td)/'recovery.db')
            store=StateStore(path);self.addCleanup(store.close)
            self.assertEqual(store.get_account_state('a').post_count,4)
            self.assertEqual(store._conn.execute('PRAGMA user_version').fetchone()[0],6)
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_two_process_increments_and_claims(self):
        with tempfile.TemporaryDirectory() as td:
            path=str(Path(td)/'shared.db');StateStore(path).close()
            ctx=multiprocessing.get_context('spawn')
            processes=[ctx.Process(target=parallel_increment,args=(path,40)) for _ in range(2)]
            for p in processes:p.start()
            for p in processes:p.join(15);self.assertEqual(p.exitcode,0)
            store=StateStore(path);self.assertEqual(store.get_account_state('a').post_count,80);store.close()
            q=ctx.Queue()
            processes=[ctx.Process(target=parallel_claim,args=(path,'b'+str(i),'same-target',q)) for i in range(2)]
            for p in processes:p.start()
            for p in processes:p.join(15);self.assertEqual(p.exitcode,0)
            self.assertEqual(sum(q.get(timeout=2)[0] for _ in processes),1)
            q.close()

    def test_shared_request_reservations_and_future_queue(self):
        with tempfile.TemporaryDirectory() as td:
            one=StateStore(Path(td)/'shared.db');two=StateStore(Path(td)/'shared.db')
            self.addCleanup(one.close);self.addCleanup(two.close)
            self.assertEqual(one.reserve_requests([('team',.5)],kind='write',cap=3,now=100),0)
            self.assertEqual(two.reserve_requests([('team',.5)],kind='write',cap=3,now=100),2)
            self.assertEqual(one.reserve_requests([('team',.5)],kind='write',cap=3,now=100),4)
            with self.assertRaises(RuntimeError):two.reserve_requests([('team',.5)],kind='write',cap=3,now=100)
            self.assertEqual(one.get_counter('http_write'),0) # reservations are not actual HTTP attempts

class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=StateStore();self.addCleanup(self.store.close)
        self.platform=MockLegitClient(failure_rate=0,latency_ms=(0,0))
        self.a=actor(self.platform,self.store)

    async def test_lost_response_unknown_no_resend_and_positive_reconciliation(self):
        send=self.platform.post
        async def lost(*args,**kwargs):
            await send(*args,**kwargs)
            raise TimeoutError('response lost after platform commit')
        self.platform.post=lost
        result=await self.a.post('a','Synthetic original message')
        self.assertEqual(result.status,'unknown')
        self.assertEqual(self.store.get_account_state('a').post_count,0)
        result2=await self.a.post('a','Synthetic original message')
        self.assertEqual(result2.detail,'action_unknown')
        self.assertEqual(self.platform.write_count,1)
        self.assertEqual(await reconcile_unknown_posts(self.a,self.store,now=time.time()+61),1)
        self.assertEqual(self.store.get_account_state('a').post_count,1)
        self.assertGreater(self.store.get_account_state('a').original_locked_until,time.time())

    async def test_dry_run_is_not_success_or_formal_state(self):
        evidence(self.store)
        a=actor(self.platform,self.store,dry=True)
        self.assertEqual((await a.post('a','simulation')).status,'simulated')
        self.assertEqual((await a.reply('a','p','simulation')).status,'simulated')
        self.assertEqual((await a.report('a','p',detail='evidence',context={'target_author':'enemy','detail':'evidence'})).status,'simulated')
        self.assertEqual(self.store.get_account_state('a').post_count,0)
        self.assertEqual(self.store.list_actions(),[])
        self.assertEqual(self.platform.write_count,0)
        self.assertEqual(self.store.get_counter('http_write'),0)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM report_log').fetchone()[0],0)

    async def test_same_target_and_account_claimed_before_network(self):
        self.platform.posts['p']=MockPost('p','enemy','question',time.time())
        results=await asyncio.gather(self.a.reply('a','p','reply'),self.a.reply('b','p','reply'))
        self.assertEqual(sum(r.ok for r in results),1)
        self.assertEqual(self.platform.write_count,1)
        self.assertEqual(sum(self.store.get_account_state(a).post_count for a in ('a','b')),1)

    async def test_queue_backlog_restart_cooldown_and_draft_age(self):
        class Brain:
            allied_reply_hourly_cap=3
            def __init__(self,store):
                from ctn.persona import PersonaRouter
                self.router=legacy_router(store=store)
            async def decide(self,p,commit_state=False):
                return Decision(p.post_id,p.post_id,Tier.AMPLIFY,'a',JevAnswers(),action=ActionKind.LIKE)
        self.store.set_policy('like_pacing',{'per_hour':100,'per_day':100})  # this test is about queue persistence
        loop=BotLoop(self.a,side=Side.YES,accounts=['a'],store=self.store,brain=Brain(self.store))
        for i in range(12):
            self.platform.posts[str(i)]=MockPost(str(i),'enemy','ordinary question',time.time())
            self.store.upsert_observed_post(self.platform._as_dict(self.platform.posts[str(i)]))
        await loop.process_pending()
        self.assertEqual(self.store.task_counts().get('succeeded'),3)
        self.assertEqual(self.store.task_counts().get('queued'),9)
        # Recreate scheduler; queue survives all in-memory lists.
        loop=BotLoop(self.a,side=Side.YES,accounts=['a'],store=self.store,brain=Brain(self.store))
        for _ in range(4):
            self.store._conn.execute('UPDATE post_tasks SET next_attempt_at=0');self.store._conn.commit()
            await loop.process_pending()
        self.assertEqual(self.store.task_counts().get('succeeded'),12)
        self.assertEqual(self.platform.write_count,12)

class EvidenceTests(unittest.TestCase):
    def setUp(self):self.store=StateStore();self.addCleanup(self.store.close);self.detector=BotDetector(self.store)

    def test_quotes_questions_and_slogans_never_suffice(self):
        for i in range(8):
            self.detector.inspect_post(Post(str(i),'citizen','"As an AI" is a phrase in prompt injection examples?',100+i*100),record_observation=True)
        self.assertLessEqual(self.store.get_bot_profile('citizen')['suspicion_score'],10)
        self.assertFalse(self.detector.inspect_post(Post('slogan','citizen2','Protect Kingstonia!')).is_confirmed_bot)
        for i,text in enumerate(('Do you work as an AI researcher?', 'As an AI researcher, I study public discussion.', 'I am an AI engineer working on software.')):
            result=self.detector.inspect_post(Post('discussion'+str(i),'human'+str(i),text),record_observation=True)
            self.assertEqual(result.suspicion_score,0)

    def test_two_groups_dedup_and_edit_invalidation(self):
        text='As an AI, I am sharing a synthetic long template about transit conditions. '+('ordinary additional words '*5)
        p=Post('p','a',text,100)
        first=self.detector.inspect_post(p,record_observation=True)
        self.assertEqual(first.suspicion_score,50)
        self.detector.inspect_post(p,record_observation=True)
        self.assertEqual(self.store.get_bot_profile('a')['post_seen_count'],1)
        second=self.detector.inspect_post(Post('q','a',text,200),record_observation=True)
        self.assertEqual(second.suspicion_score,85)
        self.store.upsert_observed_post(Post('q','a','Edited ordinary text',200))
        self.assertEqual(self.store.get_observed_post('q')['version'],2)
        self.assertEqual(self.store.get_bot_profile('a')['suspicion_score'],50)

class RulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=StateStore();self.addCleanup(self.store.close)
        evidence(self.store)
        self.platform=MockLegitClient(failure_rate=0,latency_ms=(0,0))
        self.platform.posts['p']=MockPost('p','enemy','synthetic',time.time())
        self.a=actor(self.platform,self.store)
        self.source=MockModerationSource()

    async def test_disabled_capability_and_pending_then_noisy_duplicate_ruling(self):
        reporter=BlueTeamReporter(self.a,self.store,accounts=['a'],enabled=True)
        self.assertEqual((await reporter.sweep_and_report()).submitted_count,0)
        reporter.event_source=self.source
        from tests.runtime_helpers import approve_synthetic_reports
        approve_synthetic_reports(self.store)
        self.assertEqual((await reporter.sweep_and_report()).submitted_count,1)
        self.assertEqual(self.store.get_bot_profile('enemy')['upheld_strikes'],0)
        self.assertEqual((await reporter.sweep_and_report()).submitted_count,0)
        event={'event_id':'event1','subject_kind':'post','subject_id':'p','outcome':'rejected',
            'ruled_at':time.time()+10,'source':'synthetic-moderator'}
        self.source.emit(event);await consume_events(self.store,self.source)
        self.assertEqual(self.store.get_bot_profile('enemy')['suspicion_score'],80)
        self.assertFalse(self.store.get_bot_profile('enemy')['verified'])
        self.assertTrue(self.store.is_reporting_suspended()[0])
        self.source.emit(event);await consume_events(self.store,self.source)
        self.assertEqual(self.store.get_bot_profile('enemy')['suspicion_score'],80)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM moderation_events').fetchone()[0],1)

    async def test_pending_cap_is_atomic_and_both_paths(self):
        evidence(self.store,'enemy','p2')
        self.platform.posts['p2']=MockPost('p2','enemy','synthetic second',time.time())
        from tests.runtime_helpers import approve_synthetic_reports
        from ctn.moderation.policy import candidate_payload
        approve_synthetic_reports(self.store)
        payload=candidate_payload(self.store,'post','p','enemy')
        candidate=self.store.store_report_candidate(payload)
        approval=self.store.approve_report(candidate)
        context={'target_author':'enemy','detail':payload['detail'],'evidence_hash':payload['evidence_hash'],'candidate_id':candidate,'approval_id':approval}
        one,two=await asyncio.gather(self.a.report('a','p',detail=payload['detail'],context=context),
            self.a.report('b','p2',detail=payload['detail'],context=context))
        self.assertEqual(sum(r.ok for r in (one,two)),1)
        successful=one if one.ok else two
        self.store.apply_moderation_event({'event_id':'upheld','subject_kind':'post','subject_id':successful.target_id,
            'outcome':'upheld','ruled_at':time.time()+5,'source':'synthetic'})
        reporter=BlueTeamReporter(self.a,self.store,accounts=['a'],enabled=True,event_source=self.source)
        self.assertIsNone(await reporter.report_account_candidate('enemy',confirmed_action_count=99))
        self.store.record_confirmed_activity('enemy',100,observed_at=time.time(),source='synthetic platform activity')
        approve_synthetic_reports(self.store)
        result=await reporter.report_account_candidate('enemy',confirmed_action_count=100)
        self.assertTrue(result.ok)

    async def test_verified_is_protected_regardless_of_suspicion(self):
        self.store.register_verified_accounts(['enemy'])
        result=await self.a.report('a','p',detail='synthetic',context={'target_author':'enemy','detail':'synthetic'})
        self.assertEqual(result.detail,'protected_account')
        self.assertEqual(self.platform.write_count,0)

class AdditionalFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=StateStore();self.addCleanup(self.store.close)
        self.platform=MockLegitClient(failure_rate=0,latency_ms=(0,0));self.a=actor(self.platform,self.store)

    async def test_expired_or_edited_draft_never_sends(self):
        self.store.upsert_observed_post(Post('p','enemy','old text',time.time()))
        self.platform.posts['p']=MockPost('p','enemy','old text',time.time())
        result=await self.a.reply('a','p','reply',context={'draft_expires_at':time.time()-1})
        self.assertEqual(result.status,'failed')
        self.assertEqual(self.platform.write_count,0)
        self.store.upsert_observed_post(Post('p','enemy','edited text',time.time()))
        result=await self.a.reply('a','p','reply',context={'target_version':1})
        self.assertEqual(result.detail,'target_edited')
        self.assertEqual(self.platform.write_count,0)

    async def test_claim_expiry_and_crash_during_send_have_different_recovery(self):
        unsent,_=self.store.claim_action('reply','a','p','reply',now=100)
        sending,_=self.store.claim_action('reply','b','q','A different response',now=100)
        self.store.start_action(sending)
        self.store.recover_actions(now=1100)
        states={r['id']:r['status'] for r in self.store.list_actions()}
        self.assertEqual(states[unsent],'failed');self.assertEqual(states[sending],'unknown')
        self.assertIsNotNone(self.store.claim_action('reply','a','p','reply',now=1100)[0])
        self.assertIsNone(self.store.claim_action('reply','b','other','reply',now=1100)[0])

    async def test_queue_lease_cooldown_and_expiration_are_explicit(self):
        self.store.upsert_observed_post(Post('p','enemy','text'),now=100)
        task=self.store.claim_post_tasks(now=100)[0]
        self.assertEqual(self.store.claim_post_tasks(now=110),[])
        self.store.update_post_task(task,'queued',reason='account_cooldown',delay=60,now=120)
        self.assertEqual(self.store.claim_post_tasks(now=179),[])
        retry=self.store.claim_post_tasks(now=180)[0]
        self.assertEqual(retry['reason'],'account_cooldown')
        self.store.claim_post_tasks(now=2000)
        self.assertEqual(self.store.task_counts()['expired'],1)

    async def test_non_idempotent_5xx_never_retries(self):
        from ctn.client.base import PlatformError
        calls=[]
        async def uncertain(*args,**kwargs):
            calls.append(1);raise PlatformError('upstream reset',status=502)
        self.platform.post=uncertain
        self.a._max_retries=3
        self.assertEqual((await self.a.post('a','text')).status,'unknown')
        self.assertEqual(len(calls),1)

    async def test_full_retry_after_and_cancelled_reservation_budget(self):
        from ctn.client.pacing import full_retry_after
        self.assertEqual(full_retry_after('600'),600)
        reservation,_=self.store.reserve_http_request([('x',1)],kind='write',cap=1,now=100)
        self.store.cancel_http_reservation(reservation)
        other,_=self.store.reserve_http_request([('x',1)],kind='write',cap=1,now=100)
        self.assertTrue(self.store.mark_http_sent(other,cap=1))
        self.assertEqual(self.store.get_counter('http_write'),1)
        with self.assertRaises(RuntimeError): self.store.reserve_http_request([('x',1)],kind='write',cap=1,now=100)

    async def test_retry_after_requeues_waiters_sequentially(self):
        buckets=[('route',.5)]
        one,_=self.store.reserve_http_request(buckets,kind='write',cap=10,now=100)
        two,_=self.store.reserve_http_request(buckets,kind='write',cap=10,now=100)
        self.store.block_request_bucket('route',200)
        first=self.store.reschedule_http_request(one,buckets,now=110)
        second=self.store.reschedule_http_request(two,buckets,now=110)
        self.assertEqual((first,second),(90,92))

    async def test_token_bucket_concurrent_future_waits(self):
        waits=[]
        async def sleep(delay): waits.append(delay)
        from types import SimpleNamespace
        with patch('ctn.ratelimit.time',SimpleNamespace(monotonic=lambda:100)),patch('ctn.ratelimit.asyncio.sleep',side_effect=sleep):
            bucket=TokenBucket(2,1)
            result=await asyncio.gather(*(bucket.acquire() for _ in range(3)))
        self.assertEqual(result,[0,.5,1])

    async def test_ruling_day_duplicate_and_conflicting_events(self):
        submitted=1792220000.
        self.store.log_report('p','enemy',target_team='confirmed-team',now=submitted)
        event={'event_id':'one','subject_kind':'post','subject_id':'p','outcome':'upheld',
            'ruled_at':submitted+86400,'source':'synthetic'}
        self.assertTrue(self.store.apply_moderation_event(event))
        from ctn.storage import current_sydney_day
        self.assertEqual(self.store.count_daily_reports_against(target_team='confirmed-team',sydney_day=current_sydney_day(submitted)),0)
        self.assertEqual(self.store.count_daily_reports_against(target_team='confirmed-team',sydney_day=current_sydney_day(submitted+86400)),1)
        self.assertFalse(self.store.apply_moderation_event({**event,'event_id':'conflict','outcome':'rejected'}))
        self.assertFalse(self.store.is_reporting_suspended()[0])
        self.assertEqual(self.store.get_bot_profile('enemy')['upheld_strikes'],1)

    async def test_unassigned_live_and_conflicting_modes_fail(self):
        with self.assertRaises(ValueError):Settings().require_live_writes()
        with patch.dict('os.environ',{'CTN_MODE':'live','CTN_DRY_RUN':'true'}):
            with self.assertRaises(ValueError):Settings(mode='live',dry_run=True).execution_mode()

    async def test_actual_http_pacer_counts_each_sdk_route(self):
        from ctn.client.pacing import HttpPacer
        from ctn.config import BudgetConfig
        class SDK:
            rate_limit=None
            def _request(self,method,path,body=None): return {'ok':True}
            def _account(self,session): return SDK()
        clock=[100.]
        def sleep(n):clock[0]+=n
        pacer=HttpPacer(self.store,FAST,BudgetConfig(),clock=lambda:clock[0],sleep=sleep)
        sdk=pacer.attach(SDK())
        sdk._request('POST','/auth/login',body={'username':'test'})
        sdk._request('GET','/tags?q=test')
        sdk._request('GET','/tags/2/posts')
        sdk._account({})._request('POST','/posts',body={'body':'synthetic'})
        self.assertEqual(self.store.get_counter('http_read'),2)
        self.assertEqual(self.store.get_counter('http_write'),2)

class ContextAndMigrationTests(unittest.TestCase):
    def test_nested_comment_keeps_immediate_parent_and_root(self):
        from ctn.client.legit_v1 import OfficialLegitClient
        client=OfficialLegitClient(url='https://example.invalid')
        normalized=client.normalize_post({'post_id':7,'body':'Nested comment','author':'a',
            'reply_to_post_id':6,'root_post_id':5})
        self.assertEqual(normalized['parent_id'],'6')
        self.assertEqual(normalized['root_post_id'],'5')
        store=StateStore();self.addCleanup(store.close)
        store.upsert_observed_post(normalized)
        row=store.get_observed_post('7')
        self.assertEqual((row['parent_id'],row['root_post_id']),('6','5'))
        self.assertEqual(store.get_post_replies('5')[0]['post_id'],'7')

    def test_legacy_logs_become_unknown_and_old_scores_are_archived(self):
        from ctn.storage.sqlite_store import _SCHEMA_SQL
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'legacy.db'
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.executescript(_SCHEMA_SQL)
                conn.execute("INSERT INTO account_states(account_id,post_count) VALUES('a',1)")
                conn.execute("INSERT INTO interaction_log(engine,account_id,action,target_post_id,normalized_text,created_at) VALUES('micro','a','reply','p','legacy reply',100)")
                conn.execute("INSERT INTO bot_registry(account_id,status,suspicion_score) VALUES('rival','confirmed_bot',100)")
            store=StateStore(path);self.addCleanup(store.close)
            self.assertEqual(store.list_actions()[0]['status'],'unknown')
            self.assertIsNone(store.claim_action('reply','a','different','reply')[0])
            self.assertEqual(store.get_bot_profile('rival')['suspicion_score'],0)
            self.assertEqual(store._conn.execute('SELECT COUNT(*) FROM legacy_bot_profiles').fetchone()[0],1)

    def test_dormancy_and_explicit_capability_are_routing_constraints(self):
        from ctn.persona import PersonaRouter
        store=StateStore();self.addCleanup(store.close)
        router=legacy_router(store=store)
        store.seed_campaign_policy(router.registry.list_all())
        store.set_account_policy('bot_medic_yes',mode='dormant')
        store.set_account_policy('bot_policy_yes',mode='dormant')
        data={'action_decision':{'required_account_level':'high'}}
        fallback=router.route(data,'Which clinical study?',side='yes')
        self.assertNotIn(fallback.account_id,('bot_medic_yes','bot_policy_yes'))
        self.assertTrue(fallback.reason.startswith('high_fallback'))
        store.set_account_policy('bot_medic_yes',mode='active',capability_tier='high')
        self.assertEqual(router.route(data,'Which clinical study?',side='yes').account_id,'bot_medic_yes')

    def test_backup_restore_is_consistent_and_never_overwrites(self):
        from ctn.ops import main
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'state.db';backup=Path(td)/'backup.db';restored=Path(td)/'restored.db'
            store=StateStore(db);store.increment_counter('proof',7);store.backup(backup);store.close()
            self.assertEqual(main(['restore',str(backup),str(restored)]),0)
            store=StateStore(restored);self.addCleanup(store.close)
            self.assertEqual(store.get_counter('proof'),7)
            with self.assertRaises(FileExistsError):store.backup(backup)

class PaginationOverlapTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_comment_does_not_truncate_server_cursor(self):
        from ctn.client.legit_v1 import NormalizedPage
        from ctn.pipeline.crawl import CommentCrawler
        store=StateStore();self.addCleanup(store.close)
        platform=MockLegitClient(failure_rate=0,latency_ms=(0,0));a=actor(platform,store)
        store.upsert_observed_post(Post('root','a','root',100,replies=5),now=100)
        seen=[]
        async def page(pid,*,limit,cursor=None):
            seen.append(cursor)
            ids=['c1','c2'] if cursor is None else ['c2','c3'] if cursor=='two' else ['c4','c5']
            return NormalizedPage([{'post_id':i,'author':'u','text':'Comment '+i,'created_at':100} for i in ids],
                next_cursor='two' if cursor is None else 'three' if cursor=='two' else None)
        platform.get_comments=page
        crawler=CommentCrawler(a,store,page_size=2,cooldown_s=0)
        results=[await crawler.step(now=ts) for ts in (200,201,202)]
        self.assertEqual(seen,[None,'two','three'])
        self.assertFalse(results[1].done)
        self.assertTrue(results[2].done)
        self.assertEqual(len(store.get_post_replies('root')),5)

def send_from_process(path,url,account,result_queue):
    import urllib.request
    class NetworkClient:
        async def reply(self,account,post_id,text):
            def request():
                req=urllib.request.Request(url,data=json.dumps({'account':account,'post_id':post_id,'text':text}).encode(),
                    method='POST',headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
            return await asyncio.to_thread(request)
    store=StateStore(path)
    result=asyncio.run(actor(NetworkClient(),store).reply(account,'shared-target','Synthetic local HTTP response'))
    result_queue.put(result.status);store.close()

class MultiprocessWireTests(unittest.TestCase):
    def test_two_processes_produce_only_one_http_write(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers['Content-Length'])))
                body=b'{"post_id":"reply-result"}'
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                path=str(Path(td)/'shared.db');StateStore(path).close()
                ctx=multiprocessing.get_context('spawn');queue=ctx.Queue()
                processes=[ctx.Process(target=send_from_process,args=(path,f'http://127.0.0.1:{server.server_port}/reply','a'+str(i),queue)) for i in range(2)]
                for process in processes:process.start()
                for process in processes:process.join(15);self.assertEqual(process.exitcode,0)
                statuses=[queue.get(timeout=2) for _ in processes];queue.close()
                self.assertEqual(sorted(statuses),['blocked','succeeded'])
                self.assertEqual(len(received),1)
        finally:server.shutdown();server.server_close();thread.join(2)

class ReplyMaterialContextTests(unittest.TestCase):
    def test_parent_metrics_and_prior_reply_are_typed_and_preserved(self):
        from unittest.mock import MagicMock
        from ctn.persona import PersonaRouter
        from ctn.pipeline.brain import JevLlmBrain
        from ctn.jev import JevInput
        store=StateStore();self.addCleanup(store.close)
        store.upsert_observed_post(Post('parent','citizen','Actual immediate parent',time.time()))
        target=Post('child','citizen2','Target claim',time.time(),likes=4,replies=2,
            raw={'parent_id':'parent','root_post_id':'root'})
        store.upsert_observed_post(target)
        store.log_interaction(engine='micro',account_id='a',action='reply',target_post_id='child',text='Exact previous reply')
        brain=JevLlmBrain(MagicMock(),MagicMock(),side=Side.YES,router=legacy_router(store=store),postflight=MagicMock())
        payload=brain.payload_for(target)
        self.assertEqual(payload['conversation']['items'][0]['text'],'Actual immediate parent')
        self.assertEqual(payload['metrics']['likes'],4)
        self.assertEqual(payload['interaction_history']['events'][0]['text'],'Exact previous reply')
        JevInput.from_dict(payload)

class MaterialAndSideGuardsTests(unittest.TestCase):
    def test_target_claim_cannot_be_its_own_answer_basis(self):
        from unittest.mock import MagicMock
        from ctn.pipeline.brain import JevLlmBrain
        from ctn.persona import PersonaRouter
        store=StateStore();self.addCleanup(store.close)
        workflow=MagicMock();generator=MagicMock()
        workflow.evaluate.return_value.to_dict.return_value={'flow':{'status':'ready','relative_stance':'opposing'},
            'action_decision':{'status':'decided','action':'reply'},'response_decision':{'prompt_type':'P02'},
            'materials':{'verified_background':{'items':[]}}}
        brain=JevLlmBrain(workflow,generator,side=Side.YES,router=legacy_router(store=store),postflight=MagicMock())
        verdict=brain.think(Post('p','citizen','Does this rule have a medical exemption?'))
        self.assertEqual(verdict.action,ActionKind.NONE)
        self.assertEqual(verdict.reason,'needs_materials:P02')
        generator.generate.assert_not_called()

    def test_shared_database_rejects_a_conflicting_campaign_side(self):
        store=StateStore();self.addCleanup(store.close)
        store.bind_campaign_side('yes');store.bind_campaign_side('yes')
        with self.assertRaises(ValueError):store.bind_campaign_side('no')
        self.assertEqual(store.policy('assigned_side'),'yes')
        store.bind_data_source('mock');store.bind_data_source('mock')
        with self.assertRaises(ValueError):store.bind_data_source('real')
