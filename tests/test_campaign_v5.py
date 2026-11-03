"""Offline integration and fault tests for source-bound RAG and mixed reporting."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

import httpx

from ctn.accounts import fleet_plan,register_one,check_registration
from ctn.client.base import PlatformError,ReadFailurePage
from ctn.client.legit_v1 import NormalizedPage
from ctn.client.mock import MockLegitClient,MockPost
from ctn.config import JevConfig,LLMConfig
from ctn.jev import JevClient,JevPacker,JevWorkflow,JevPostflightChecker
from ctn.llm import DeepSeekClient,ReplyGenerator
from ctn.models import Post,Side,ActionKind
from ctn.moderation.detector import BotDetector
from ctn.moderation.events import MockModerationSource,consume_events
from ctn.moderation.policy import candidate_payload,automatic_evidence
from ctn.moderation.reporter import BlueTeamReporter
from ctn.pipeline.brain import JevLlmBrain
from ctn.pipeline.crawl import CommentCrawler
from ctn.rag.corpus import Corpus,too_close
from ctn.storage import StateStore
from tests.test_jev_actions import POLICY,set_score
from tests.test_jev_evaluator import FakeTransport,choose
from tests.test_persona80 import bound_router,batch_fixture
from tests.test_runtime_reliability import actor


def send_report_process(path,target,context,queue):
    async def run():
        with closing(StateStore(path)) as store:
            platform=MockLegitClient(failure_rate=0,latency_ms=(0,0))
            platform.posts[target]=MockPost(target,'enemy','Synthetic report target',time.time())
            result=await actor(platform,store).report('reporter_'+target,target,
                detail=context['detail'],context=context)
            queue.put((result.status,platform.write_count))
    asyncio.run(run())


def strong_evidence(store,author='enemy',target='leak'):
    detector=BotDetector(store)
    now=time.time()-120
    text='The ferry terminal needs a clear explanation of the entry procedure and the documents passengers should carry when travelling between these islands.'
    for pid,body,root,ts in ((target,'As an AI, I cannot actually hold this referendum opinion.','r1',now),
        ('behavior1',text,'r2',now+60),('behavior2',text,'r3',now+120)):
        detector.inspect_post(Post(pid,author,body,ts,raw={'parent_id':root,'root_post_id':root}),record_observation=True)
    return candidate_payload(store,'post',target,author)


def clearance(store,*,live=True):
    store.set_policy('report_clearance',{'source':'synthetic confirmed clearance','allowed':True,
        'expires_at':time.time()+300,'live_source_verified':live})


class RagRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.corpus=Corpus()
        self.store=StateStore();self.addCleanup(self.store.close)

    def test_all_cases_retain_required_basis_and_qualifiers(self):
        cases=json.loads(Path('config/rag/review-cases.v1.1.json').read_text())['cases']
        self.assertEqual(len(cases),54)  # 52 curated + C053/C054 (poll window, 2026-10-26)
        for case in cases:
            with self.subTest(case=case['id']):
                data=self.corpus.retrieve(case['query'],purpose=case.get('purpose')).to_dict()
                facts={r['id']:r for r in data['facts']};unknowns={r['id']:r for r in data['unknowns']}
                self.assertTrue(set(case['expected_fact_ids'])<=facts.keys())
                self.assertTrue(set(case['expected_unknown_ids'])<=unknowns.keys())
                self.assertLessEqual(len(data['styles']),1)
                for row in facts.values():
                    self.assertEqual(row,self.corpus.facts[row['id']])
                    self.assertTrue(all(q in row['text'] for q in row['required_qualifiers']))
                for row in unknowns.values():self.assertTrue(row['do_not_infer'])
                self.assertNotIn('raw_review',data)

    def test_immutable_source_and_snapshot_survive_restart_and_revocation(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'state.db';store=StateStore(path)
            source=store.archive_source('newspaper','v1','Full source with a qualification.',{'attribution':'editor'})
            self.assertEqual(source,store.archive_source('newspaper','v1','Full source with a qualification.',{'attribution':'editor'}))
            with self.assertRaises(ValueError):store.archive_source('newspaper','v1','Changed text',{})
            snapshot=self.corpus.retrieve('How long does protection last?',purpose='P02')
            ident=store.save_rag_snapshot(snapshot);store.close()
            with closing(StateStore(path)) as restored:
                self.assertEqual(restored.load_rag_snapshot(ident).to_dict(),snapshot.to_dict())
                restored.revoke_rag_snapshot(ident)
                with self.assertRaises(ValueError):restored.load_rag_snapshot(ident)

    def test_claim_merge_keeps_both_sources_and_news_is_attributed(self):
        first=self.store.archive_source('paper-a','v1','A quoted position.',{'kind':'news'})
        second=self.store.archive_source('paper-b','v1','A quoted position.',{'kind':'news'})
        one=self.store.record_claim('attributed_claim','A quoted position.','speaker A at day 1',first,0,18)
        two=self.store.record_claim('attributed_claim','A quoted position.','speaker A at day 1',second,0,18)
        self.assertEqual(one,two)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM claim_provenance').fetchone()[0],2)
        with self.assertRaises(ValueError):self.store.record_claim('official_fact','A quoted position.','day 1',first,0,18)
        with self.assertRaises(ValueError):self.store.record_claim('attributed_claim','Text','day 1',first,0,999)

    def test_p08_style_purpose_is_independent_and_retrieval_not_question_whitelist(self):
        data=self.corpus.retrieve('What information is retained by a venue during verification?',purpose='P08').to_dict()
        self.assertTrue(data['styles'])
        self.assertEqual(data['styles'][0]['purposes'],['P08'])
        unfamiliar=self.corpus.retrieve('parliament National Assembly institutions').to_dict()
        self.assertIn('F025',{r['id'] for r in unfamiliar['facts']})

    def test_primary_generation_receives_bound_typed_material_and_single_style(self):
        router=bound_router(self.store,('yes_001',))
        def respond(body,data):
            if 'policy_stance' in data['answers']:choose(data,'policy_stance','no')
            if 'reply_value' in data['answers']:
                set_score(data,'reply_value',2);choose(data,'expression_function','question')
        transport=FakeTransport(respond)
        client=JevClient(JevConfig(api_key='synthetic-rag-jev',max_retries=0),transport=transport)
        workflow=JevWorkflow(client,action_policy=POLICY)
        messages=[]
        def respond_llm(request):
            messages.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'role':'assistant',
                'content':'The supplied evidence leaves transmission uncertain. I support the access rule while keeping that limitation explicit.'}}]})
        llm=DeepSeekClient(LLMConfig(api_key='synthetic-rag-llm',max_retries=0),transport=httpx.MockTransport(respond_llm));self.addCleanup(llm.close)
        brain=JevLlmBrain(workflow,ReplyGenerator(workflow,llm),side=Side.YES,router=router,corpus=self.corpus)
        post=Post('target','citizen','Can vaccinated people still spread H2D2?',time.time(),raw={'platform_level':'standard'})
        self.store.upsert_observed_post(post)
        result=brain.think(post)
        self.assertEqual(result.action,ActionKind.REPLY,result.reason)
        self.assertTrue(result.detail['rag_snapshot_id'])
        assessed=next(c['state']['content']['campaign_materials'] for c in transport.calls if 'policy_stance' in c['questions'])
        self.assertIn('unknown',{r['kind'] for r in assessed['items']})
        self.assertNotIn('style',{r['kind'] for r in assessed['items']})
        payload=json.loads(messages[0]['messages'][1]['content'])
        self.assertLessEqual(len(payload['style_reference']),1)
        review=next(c['state']['content']['context'] for c in transport.calls if 'send_authorization' in c['questions'])
        self.assertEqual(review['approved_materials']['content']['campaign_materials']['snapshot_id'],assessed['snapshot_id'])

    def test_quote_and_unknown_are_not_upgraded_to_official_fact(self):
        snapshot=self.corpus.retrieve('KITB predicts harm to tourism and missing exemptions',purpose='P02')
        items=snapshot.campaign_items()
        self.assertTrue(any(r['kind']=='attributed_claim' for r in items))
        self.assertTrue(any(r['kind']=='unknown' for r in items))
        self.assertFalse(any(r['kind']=='official_fact' and r['reference_id']=='F030' for r in items))

    def test_unregistered_campaign_material_cannot_enter_assessment(self):
        from tests.test_jev_evaluator import content
        from ctn.jev import load_rubric
        snapshot=self.corpus.retrieve('What do trials suggest?')
        payload=content();payload['campaign_materials']={'snapshot_id':snapshot.snapshot_id,
            'corpus_hash':self.corpus.hash,'items':snapshot.campaign_items()}
        packer=JevPacker()
        self.assertEqual(packer.pack_post(payload).status,'deferred')
        packer.register_campaign_snapshot(snapshot)
        self.assertEqual(packer.pack_post(payload).status,'ready')
        payload['campaign_materials']['items'][0]['text']='An unsupported invented policy.'
        self.assertEqual(packer.pack_post(payload).status,'deferred')


class SharedPublicationTests(unittest.TestCase):
    def test_v4_migration_preserves_identity_history_and_consistent_backup(self):
        from ctn.storage.campaign import CAMPAIGN_SCHEMA
        from ctn.storage.operations import SCHEMA
        import re
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'state.db'
            store=StateStore(path)
            router,batch=batch_fixture(store)
            ident,reason=store.claim_action('post','fresh_yes_001',text='An original identity must be retained.',metadata=router.context_for(router.registry.get('fresh_yes_001')))
            self.assertIsNotNone(ident,reason);store.start_action(ident)
            store.finish_action(ident,'succeeded',response={'post_id':'old-original'})
            with store.batch_transaction():
                # Restore the actual v4 batch definition and remove only v5 additions.
                store._conn.execute('ALTER TABLE reply_batches RENAME TO saved_batches')
                for statement in CAMPAIGN_SCHEMA.split(';'):
                    if statement.strip():store._conn.execute(statement)
                store._conn.execute('INSERT INTO reply_batches SELECT * FROM saved_batches')
                store._conn.execute('DROP TABLE saved_batches')
                for table in re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)',SCHEMA):
                    store._conn.execute('DROP TABLE '+table)
                store._conn.execute('PRAGMA user_version=4')
            store.close()
            with closing(StateStore(path)) as migrated:
                self.assertEqual(migrated._conn.execute('PRAGMA user_version').fetchone()[0],6)
                self.assertEqual(migrated.persona_bindings()['yes_001'],'fresh_yes_001')
                self.assertEqual(migrated.list_actions()[0]['status'],'succeeded')
                self.assertEqual(migrated._conn.execute('SELECT id FROM reply_batches').fetchone()[0],batch)
                self.assertEqual(migrated._conn.execute('SELECT COUNT(*) FROM publication_texts').fetchone()[0],1)
                self.assertEqual(migrated._conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            with closing(sqlite3.connect(str(path)+'.before-v6.bak')) as backup:
                self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0],4)
                self.assertEqual(backup.execute('SELECT status FROM action_intents').fetchone()[0],'succeeded')

    def test_two_connections_cannot_claim_same_text_for_different_targets(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'state.db'
            stores=[StateStore(path),StateStore(path)]
            for store in stores:self.addCleanup(store.close)
            with ThreadPoolExecutor(2) as pool:
                results=list(pool.map(lambda i:stores[i].claim_action('reply','account'+str(i),'target'+str(i),'The same precise response cannot be sent twice.'),range(2)))
            self.assertEqual(sum(bool(r[0]) for r in results),1)
            self.assertIn('duplicate_publication',[r[1] for r in results])

    def test_fact_reuse_allowed_and_unknown_keeps_text_reserved(self):
        store=StateStore();self.addCleanup(store.close)
        first,_=store.claim_action('reply','a','p','Trial evidence suggests lower severe-disease risk; transmission remains uncertain.')
        store.start_action(first);store.finish_action(first,'unknown')
        self.assertIsNone(store.claim_action('reply','b','q','Trial evidence suggests lower severe-disease risk; transmission remains uncertain.')[0])
        self.assertIsNotNone(store.claim_action('reply','b','q','The trials address serious illness. Your question about access-rule effectiveness needs separate evidence.')[0])
        store.finish_action(first,'failed',detail='operator confirmed not sent')
        self.assertIsNotNone(store.claim_action('reply','c','r','Trial evidence suggests lower severe-disease risk; transmission remains uncertain.')[0])

    def test_snapshot_revocation_blocks_an_already_claimed_send(self):
        store=StateStore();self.addCleanup(store.close)
        snapshot=Corpus().retrieve('Is proof required?',purpose='P02')
        snapshot_id=store.save_rag_snapshot(snapshot)
        ident,reason=store.claim_action('reply','a','p','I support the requirement, with its details still needing explanation.',metadata={'rag_snapshot_id':snapshot_id})
        self.assertIsNotNone(ident,reason);store.start_action(ident)
        store.revoke_rag_snapshot(snapshot_id)
        self.assertEqual(store.guard_wire_action(ident),(False,'rag_snapshot_withdrawn'))

    def test_warmup_and_strategic_quotas_are_separate_and_reserved(self):
        store=StateStore();self.addCleanup(store.close)
        store.set_policy('daily_original_limit',1);store.set_policy('daily_warmup_limit',1)
        warm,_=store.claim_action('post','a',text='Morning ferry coffee on the island.',metadata={'purpose':'warmup'})
        strategic,_=store.claim_action('post','b',text='I support proof for shared public spaces.',metadata={'purpose':'P01'})
        self.assertTrue(warm);self.assertTrue(strategic)
        self.assertIsNotNone(store.claim_action('post','c',text='A different daily warmup.',metadata={'purpose':'warmup'})[0])
        store.start_action(warm);store.finish_action(warm,'succeeded',response={'post_id':'warm'})
        self.assertGreater(store._conn.execute('SELECT warmed_at FROM account_readiness').fetchone()[0],0)

    def test_edited_unsent_target_can_replan_but_sent_target_cannot(self):
        store=StateStore();self.addCleanup(store.close)
        router,batch=batch_fixture(store)
        store.upsert_observed_post(Post('target','citizen','Edited bus question',time.time()))
        slots=[{'account_id':p.account_id,'persona_id':p.persona_id,'persona_version':p.version} for p in router.registry.list_all() if p.account_id]
        new,created=store.create_reply_batch('target',2,{'catalog_hash':router.registry.catalog_hash},slots,expires_at=time.time()+1800)
        self.assertTrue(created);self.assertNotEqual(batch,new)

    def test_missing_material_waits_for_change_and_expires(self):
        store=StateStore();self.addCleanup(store.close)
        store.upsert_observed_post(Post('p','citizen','Question',100),now=100)
        task=store.claim_post_tasks(now=101)[0]
        store.wait_for_material(task,'corpus-one','needs_materials:P02')
        store.resume_material_tasks('corpus-one',now=102)
        self.assertEqual(store.claim_post_tasks(now=103),[])
        store.resume_material_tasks('corpus-two',now=104)
        task=store.claim_post_tasks(now=105)[0]
        store.wait_for_material(task,'corpus-two','needs_materials:P02')
        store.resume_material_tasks('corpus-two',now=2000)
        self.assertEqual(store.task_counts(),{'expired':1})


class HybridReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=StateStore();self.addCleanup(self.store.close)
        self.platform=MockLegitClient(failure_rate=0,latency_ms=(0,0))
        self.platform.posts['leak']=MockPost('leak','enemy','Synthetic target',time.time())
        self.actuator=actor(self.platform,self.store)
        self.reporter=BlueTeamReporter(self.actuator,self.store,accounts=['reporter'],enabled=True)
        self.store.set_policy('report_mode','hybrid')
        # Automation is enabled explicitly; the shipped default is shadow mode.
        self.store.set_policy('report_decision',{'shadow':False})
        self.payload=strong_evidence(self.store)

    async def test_strong_independent_evidence_sends_without_human(self):
        self.assertTrue(automatic_evidence(self.store,self.payload))
        clearance(self.store)
        result=await self.reporter._submit_candidate('post','leak','enemy')
        self.assertTrue(result.ok,result.detail)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM report_approvals').fetchone()[0],0)
        self.assertEqual(self.store._conn.execute('SELECT status FROM report_attempts').fetchone()[0],'pending')

    async def test_shadow_mode_records_decision_but_never_sends(self):
        self.store.set_policy('report_decision',{'shadow':True})
        self.assertIsNone(await self.reporter._submit_candidate('post','leak','enemy'))
        clearance(self.store,live=False)
        self.assertIsNone(await self.reporter._submit_candidate('post','leak','enemy'))
        self.assertEqual(self.platform.write_count,0)
        row=self.store._conn.execute('SELECT action,reason,bucket FROM report_decisions').fetchone()
        self.assertEqual(tuple(row),('human','shadow_mode','leak_behavior'))
        # The recommendation is queued for a human instead.
        self.assertEqual(self.store._conn.execute("SELECT status FROM report_reviews").fetchone()[0],'review')

    async def test_hybrid_allows_explicit_manual_fallback_when_auto_capability_is_unavailable(self):
        self.store.set_policy('report_decision',{'shadow':True})
        clearance(self.store,live=False)
        candidate=self.store.store_report_candidate(self.payload)
        approval=self.store.approve_report(candidate)
        result=await self.reporter._submit_candidate('post','leak','enemy')
        self.assertTrue(result.ok,result.detail)
        self.assertTrue(self.store._conn.execute('SELECT consumed_by FROM report_approvals WHERE id=?',(approval,)).fetchone()[0])

    async def test_manual_approval_is_bound_single_use_and_expires(self):
        self.store.set_policy('report_mode','manual');clearance(self.store,live=False)
        candidate=self.store.store_report_candidate(self.payload)
        approval=self.store.approve_report(candidate)
        self.assertTrue((await self.reporter._submit_candidate('post','leak','enemy')).ok)
        self.assertTrue(self.store._conn.execute('SELECT consumed_by FROM report_approvals WHERE id=?',(approval,)).fetchone()[0])
        self.assertIsNone(await self.reporter._submit_candidate('post','leak','enemy'))

    async def test_edited_evidence_invalidates_approval_before_send(self):
        self.store.set_policy('report_mode','manual');clearance(self.store,live=False)
        candidate=self.store.store_report_candidate(self.payload);self.store.approve_report(candidate)
        self.store.upsert_observed_post(Post('behavior1','enemy','Ordinary edited text',time.time()))
        result=await self.reporter._submit_candidate('post','leak','enemy')
        self.assertIsNone(result)
        self.assertEqual(self.platform.write_count,0)

    async def test_dry_run_does_not_consume_approval_or_report_slot(self):
        self.store.set_policy('report_mode','manual');clearance(self.store,live=False)
        candidate=self.store.store_report_candidate(self.payload);self.store.approve_report(candidate)
        self.actuator.dry_run=True
        result=await self.reporter._submit_candidate('post','leak','enemy')
        self.assertEqual(result.status,'simulated')
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM report_attempts').fetchone()[0],0)
        self.assertEqual(self.store._conn.execute("SELECT consumed_by FROM report_approvals").fetchone()[0],'')

    async def test_approval_expiration_is_checked_at_the_wire_gate(self):
        self.store.set_policy('report_mode','manual');clearance(self.store,live=False)
        candidate=self.store.store_report_candidate(self.payload)
        approval=self.store.approve_report(candidate)
        meta={'target_author':'enemy','detail':self.payload['detail'],'evidence_hash':self.payload['evidence_hash'],
            'candidate_id':candidate,'approval_id':approval}
        ident,reason=self.store.claim_action('report','a','leak',metadata=meta)
        self.assertIsNotNone(ident,reason);self.store.start_action(ident)
        with self.store.batch_transaction():self.store._conn.execute('UPDATE report_approvals SET expires_at=0 WHERE id=?',(approval,))
        self.assertEqual(self.store.guard_wire_action(ident),(False,'human_approval_required'))

    async def test_lost_report_response_is_resolved_by_an_explicit_ruling(self):
        clearance(self.store)
        async def lost(*args,**kwargs):
            self.platform.write_count+=1
            raise PlatformError('response lost',retryable=True)
        self.platform.report=lost
        result=await self.reporter._submit_candidate('post','leak','enemy')
        self.assertEqual(result.status,'unknown')
        attempt=self.store._conn.execute('SELECT id FROM report_attempts').fetchone()[0]
        self.store.apply_moderation_event({'event_id':'confirmed-report','attempt_id':attempt,
            'subject_kind':'post','subject_id':'leak','outcome':'upheld','ruled_at':time.time()+1,'source':'synthetic confirmed ruling'})
        self.assertEqual(self.store.list_actions()[0]['status'],'succeeded')
        self.assertEqual(self.store._conn.execute('SELECT status FROM report_attempts').fetchone()[0],'ruled')
        self.assertEqual(self.platform.write_count,1)

    async def test_report_403_invalidates_clearance_without_inventing_account_or_team_ban(self):
        clearance(self.store)
        async def forbidden(*args,**kwargs):raise PlatformError('reporting currently unavailable',status=403,retryable=False)
        self.platform.report=forbidden
        result=await self.reporter._submit_candidate('post','leak','enemy')
        self.assertEqual(result.status,'failed')
        self.assertFalse(self.store.report_state_ready()[0])
        self.assertFalse(self.store.is_reporting_suspended()[0])
        self.assertFalse(self.store.get_account_state('reporter').is_banned)

    async def test_duplicate_and_invalid_rulings_are_isolated(self):
        clearance(self.store)
        result=await self.reporter._submit_candidate('post','leak','enemy');self.assertTrue(result.ok)
        source=MockModerationSource()
        event={'event_id':'ruling','subject_kind':'post','subject_id':'leak','outcome':'rejected','ruled_at':time.time()+1,'source':'synthetic moderator'}
        source.emit({'bad':'event'});source.emit(event);source.emit(event)
        report=await consume_events(self.store,source)
        self.assertEqual(report['applied'],1);self.assertEqual(report['quarantined'],1)
        self.assertTrue(self.store.is_reporting_suspended()[0])
        self.assertFalse(self.store.get_bot_profile('enemy')['verified'])
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM ruling_receipts').fetchone()[0],1)

    async def test_high_score_or_repeated_leak_alone_does_not_qualify(self):
        store=StateStore();self.addCleanup(store.close);detector=BotDetector(store)
        for i in range(3):detector.inspect_post(Post(str(i),'x','As an AI, I cannot actually hold this referendum opinion. '*2,time.time()+i),record_observation=True)
        payload=candidate_payload(store,'post','0','x')
        self.assertIsNotNone(payload)
        self.assertFalse(automatic_evidence(store,payload))

    async def test_quoted_examples_ordinary_questions_and_verified_are_protected(self):
        detector=BotDetector(self.store)
        for i,text in enumerate(('He wrote: "As an AI, I cannot vote."','Example: As an AI, this is a quoted failure.','> As an AI, this is quoted.','What proof would a ferry require?')):
            result=detector.inspect_post(Post('normal'+str(i),'normal'+str(i),text,time.time()),record_observation=True)
            self.assertLess(result.suspicion_score,85)
        self.assertTrue(BotDetector._unquoted('As an AI, this is text. A neighbour said hello.'))
        self.store.register_verified_accounts(['enemy'])
        self.assertIsNone(candidate_payload(self.store,'post','leak','enemy'))


class PaginationAndRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_warmup_readiness_and_original_lock_do_not_block_ready_replies(self):
        from ctn.warmup import warmup_step
        from ctn.jev.postflight import PostflightVerdict
        store=StateStore();self.addCleanup(store.close)
        router=bound_router(store,('yes_001','yes_002'))
        store.set_policy('require_warmup',True);store.set_policy('initialise_spread',{'window_seconds':0,'min_gap_seconds':0})
        platform=MockLegitClient(failure_rate=0,latency_ms=(0,0));act=actor(platform,store)
        first=router.registry.get('yes_001')
        blocked=await act.reply(first.account_id,'p','A response before readiness.',context=router.context_for(first))
        self.assertEqual(blocked.detail,'account_needs_initialise')
        checker=Mock();checker.check.return_value=PostflightVerdict(True,'approved')
        self.assertTrue((await warmup_step(act,router,checker))['ok'])
        self.assertTrue((await warmup_step(act,router,checker))['ok'])
        self.assertEqual((await warmup_step(act,router,checker))['status'],'idle')
        with store.batch_transaction():
            store._conn.execute('UPDATE account_states SET last_posted_at=last_posted_at-300')  # beyond base cooldown + jitter
        result=await act.reply(first.account_id,'p','A response after successful warmup.',context=router.context_for(first))
        self.assertTrue(result.ok,result.detail)
        self.assertEqual(store.get_account_state(first.account_id).original_locked_until,0)

    async def test_successful_empty_page_completes_failed_page_preserves_cursor(self):
        store=StateStore();self.addCleanup(store.close)
        store.upsert_observed_post(Post('root','citizen','Root',1,replies=3),now=1)
        store.update_comment_crawl_state('root',comment_cursor='next',now=1)
        platform=MockLegitClient(failure_rate=0,latency_ms=(0,0));act=actor(platform,store)
        async def failure(*args,**kwargs):return ReadFailurePage()
        act.get_comments=failure
        crawler=CommentCrawler(act,store,cooldown_s=0,audit_suspects=False)
        self.assertEqual((await crawler.step(now=2)).skipped,'read_failed')
        self.assertEqual(store.get_observed_post('root')['comment_cursor'],'next')
        async def empty(*args,**kwargs):return NormalizedPage([],None)
        act.get_comments=empty
        self.assertTrue((await crawler.step(now=3)).done)
        self.assertEqual(store.get_observed_post('root')['comment_cursor'],'')

    async def test_registration_timeout_blocks_repeat_and_positive_login_resolves(self):
        store=StateStore();self.addCleanup(store.close);bound_router(store,('yes_002',))
        client=Mock();client.team_key='synthetic-team';client.default_password='synthetic-password'
        client.social.register.side_effect=TimeoutError()
        client._translate_error.return_value=PlatformError('response lost',retryable=True)
        result=await register_one(store,client,'yes_001','new_handle','synthetic-password')
        self.assertEqual(result['status'],'unknown')
        self.assertEqual((await register_one(store,client,'yes_001','new_handle','synthetic-password'))['status'],'unknown')
        self.assertEqual(client.social.register.call_count,1)
        self.assertEqual((await check_registration(store,client,'new_handle'))['status'],'succeeded')
        self.assertEqual(len(fleet_plan(30)),30)
        self.assertEqual(len(fleet_plan(50,phase='shaping')),50)
        # Registration sends the persona's name as the visible display name.
        row=fleet_plan(50,persona_id='yes_003',account_id='owenp_driving')[0]
        self.assertEqual(row['display_name'],'Owen Park')
        client.social.register.side_effect=None
        self.assertEqual((await register_one(store,client,row['persona_id'],row['account_id'],'synthetic-password',
            display_name=row['display_name']))['status'],'succeeded')
        self.assertEqual(client.social.register.call_args.kwargs['display_name'],'Owen Park')
        self.assertEqual(len(fleet_plan(50,phase='warmup')),50)


class MultiprocessReportTests(unittest.TestCase):
    def test_two_processes_cannot_exceed_per_author_limit(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'state.db';store=StateStore(path)
            strong_evidence(store);store.set_policy('report_mode','manual');clearance(store,live=False)
            store.set_policy('max_pending_per_author',1)
            contexts=[]
            for target in ('leak','behavior1'):
                payload=candidate_payload(store,'post',target,'enemy')
                candidate=store.store_report_candidate(payload);approval=store.approve_report(candidate)
                contexts.append({'target_author':'enemy','detail':payload['detail'],'evidence_hash':payload['evidence_hash'],
                    'candidate_id':candidate,'approval_id':approval,'report_attempt_id':'attempt-'+target})
            store.close()
            ctx=multiprocessing.get_context('spawn');queue=ctx.Queue()
            workers=[ctx.Process(target=send_report_process,args=(str(path),target,context,queue))
                for target,context in zip(('leak','behavior1'),contexts)]
            for worker in workers:worker.start()
            results=[queue.get(timeout=20) for _ in workers]
            for worker in workers:worker.join(20);self.assertEqual(worker.exitcode,0)
            self.assertEqual(sum(r[1] for r in results),1)
            with closing(StateStore(path)) as store:
                self.assertEqual(store._conn.execute("SELECT COUNT(*) FROM report_attempts WHERE status='pending'").fetchone()[0],1)


class EvidencePostflightTests(unittest.TestCase):
    def test_sdk_reporting_suspension_is_not_translated_to_account_suspension(self):
        from capturethenarrative import APIError
        from types import SimpleNamespace
        from ctn.client.legit_v1 import OfficialLegitClient
        from ctn.client.base import AccountSuspended
        store=StateStore();self.addCleanup(store.close)
        error=APIError('POST','/synthetic/report',403,'{"error":"team suspended from reporting"}')
        translated=OfficialLegitClient._translate_error(SimpleNamespace(store=store),error,account='reporter')
        self.assertNotIsInstance(translated,AccountSuspended)
        self.assertEqual(translated.status,403)

    def test_approval_cannot_override_unsupported_evidence(self):
        def respond(body,data):
            choose(data,'evidence_support','unsupported')
        client=JevClient(JevConfig(api_key='synthetic-support-check',max_retries=0),transport=FakeTransport(respond))
        verdict=JevPostflightChecker(client).check(post_id='p',target_text='Is vaccination free?',
            reply_text='The policy guarantees free vaccination for every resident.',assigned_side='yes',
            intended_purpose='P02',approved_materials={'unknowns':['Price is not specified.']})
        self.assertFalse(verdict.can_send)
        self.assertEqual(verdict.reason,'unsupported_or_unclear_evidence')


@patch.dict('ctn.pipeline.targeting.POLICY',{'caps':{'per_author_per_day':10,'per_thread_per_day':10,'per_account_per_day':35}})
class FrozenRagBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_corpus_update_does_not_retrieve_again_for_existing_batch(self):
        from tests.test_persona80 import BatchEndToEndTests
        helper=BatchEndToEndTests();self.addCleanup(helper.doCleanups)
        loop,platform,transport,requests=await helper.make_loop()
        loop.brain.corpus=Corpus();loop.max_actions_per_cycle=1
        await loop.process_pending()
        self.assertEqual(platform.write_count,1)
        loop.brain.corpus=Mock(hash='new-corpus-hash')
        loop.brain.corpus.retrieve.side_effect=AssertionError('fixed batch must not re-retrieve')
        for _ in range(3):await loop.process_pending()
        self.assertEqual(platform.write_count,4)
        self.assertEqual(len(requests),4)
        snapshots={json.loads(r['metadata_json'])['rag_snapshot_id'] for r in loop.store.list_actions()}
        self.assertEqual(len(snapshots),1)
        for request in requests:
            self.assertIn('rag_identity',json.loads(request['messages'][1]['content']))


if __name__=='__main__':unittest.main()
