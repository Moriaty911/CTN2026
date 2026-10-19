"""Offline catalog, occupation routing, fixed reply batches and sampled metrics."""
import asyncio
from collections import Counter
from contextlib import closing,redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
import multiprocessing
from pathlib import Path
import random
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from ctn.analytics import PerformanceCollector,render_report
from ctn.client.base import PlatformError
from ctn.client.mock import MockLegitClient,MockPost
from ctn.config import JevConfig,LLMConfig
from ctn.jev import JevClient,JevEvaluator,JevWorkflow,JevPostflightChecker,load_rubric
from ctn.jev.expansion import expansion_decision
from ctn.llm import DeepSeekClient,ReplyGenerator
from ctn.models import Post,Side
from ctn.persona import PersonaRegistry,PersonaRouter
from ctn.persona.catalog import GROUP_ALLOCATION,load_catalog,digest,render_preview
from ctn.persona.router import preferred_group
from ctn.pipeline.brain import JevLlmBrain
from ctn.pipeline.loop import BotLoop,CycleReport
from ctn.storage import StateStore
from tests.test_jev_actions import POLICY,set_score
from tests.test_jev_evaluator import FakeTransport,choose,content
from tests.test_runtime_reliability import actor


def bound_router(store,ids=('yes_001','yes_002','yes_003','yes_004'),*,active=True):
    router=PersonaRouter(store=store,rng=random.Random(4))
    store.set_policy('phase','shaping')
    for pid in ids:store.bind_persona(pid,'fresh_'+pid)
    if active:store.set_persona_modes(list(ids),'active')
    return router


def hint(group):
    return {'type':'choice','status':'valid','value':group,
            'probabilities':{**{g:0. for g in GROUP_ALLOCATION},group:.9,'unknown':.1}}


def batch_fixture(store,count=4,*,target='target',now=None):
    now=time.time() if now is None else now
    router=bound_router(store,tuple('yes_'+str(i+1).zfill(3) for i in range(count)))
    store.upsert_observed_post(Post(target,'citizen','Bus terminal entry question',now),now=now)
    profiles=router.registry.list_by_group('transport_logistics')[:count]
    slots=[{'account_id':p.account_id,'persona_id':p.persona_id,'persona_version':p.version} for p in profiles]
    plan={'catalog_hash':router.registry.catalog_hash,'version':'test'}
    batch,_=store.create_reply_batch(target,1,plan,slots,expires_at=now+1800,now=now)
    return router,batch


def slot_meta(router,slot):
    return {**router.context_for(router.registry.get(slot['account_id'])),
        'reply_batch_id':slot['batch_id'],'reply_slot':slot['slot'],'slot_owner':slot['owner'],
        'target_version':slot['target_version'],'opportunity_expires_at':slot['expires_at']}


class CatalogAndRoutingTests(unittest.TestCase):
    def setUp(self):
        self.store=StateStore();self.addCleanup(self.store.close)

    def test_every_persona_has_a_valid_backstory(self):
        from ctn.persona.catalog import validate_story
        catalog=load_catalog()
        self.assertTrue(all(row['version']==2 and 'story' in row for row in catalog['personas']))
        for row in catalog['personas']:validate_story(row['story'])
        broken=deepcopy(catalog['personas'][0]['story']);broken['vote_2025']='Somebody'
        with self.assertRaises(ValueError):validate_story(broken)

    def test_exact_roster_and_preview(self):
        catalog=load_catalog()
        self.assertEqual(Counter(p['occupation_group'] for p in catalog['personas']),Counter(GROUP_ALLOCATION))
        self.assertEqual(len({p['name'] for p in catalog['personas']}),80)
        self.assertEqual(len({p['candidate_username'] for p in catalog['personas']}),80)
        self.assertTrue(all(p['side']=='yes' for p in catalog['personas']))
        self.assertEqual(Path('docs/personas-80/preview.md').read_text(),render_preview(catalog))
        self.assertEqual(len({e['text'] for p in catalog['personas'] for e in p['example_posts']}),320)
        registry=PersonaRegistry().attach(self.store)
        self.assertIsNone(registry.get('bot_worker_yes'))
        self.assertFalse(any(p.account_id for p in registry.list_all()))
        self.assertEqual(self.store.load_all_account_states(),{})

    def test_invalid_catalog_rejected(self):
        catalog=load_catalog()
        for mutation in (
            lambda d:d['personas'].pop(),
            lambda d:d['personas'][1].update(persona_id='yes_001'),
            lambda d:d['personas'][0].update(side='no'),
            lambda d:d['personas'][0]['identity'].update(household=''),
            lambda d:d['personas'][0]['example_posts'][0].update(kind='invented'),
            lambda d:d['personas'][0]['example_posts'][0].update(text=d['personas'][1]['example_posts'][0]['text']),
            lambda d:d['personas'][0].update(occupation_group='law_policy'),
        ):
            data=deepcopy(catalog);mutation(data)
            with tempfile.TemporaryDirectory() as td:
                path=Path(td)/'bad.json';path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):load_catalog(path)

    def test_new_binding_standby_and_no_identity_reuse(self):
        router=bound_router(self.store,('yes_001',),active=False)
        self.assertEqual(router.get_state('fresh_yes_001').mode,'standby')
        with self.assertRaises(ValueError):router.route({},'bus')
        self.store.bind_persona('yes_001','fresh_yes_001') # idempotent
        with self.assertRaises(ValueError):self.store.bind_persona('yes_002','fresh_yes_001')
        self.store.log_interaction(engine='micro',account_id='old',action='post',target_post_id='oldpost',text='Old identity')
        with self.assertRaises(ValueError):self.store.bind_persona('yes_002','old')

    def test_activation_cap_phase_and_explicit_grade(self):
        router=bound_router(self.store,('yes_001','yes_002','yes_073'),active=False)
        self.store.set_policy('phase','warmup')
        self.assertEqual(self.store.set_persona_modes(['yes_073'],'active'),1)
        with self.assertRaises(ValueError):self.store.set_persona_modes(['yes_001','yes_002'],'active',max_active=1)
        self.store.set_policy('phase','shaping');self.store.set_persona_modes(['yes_073'],'active')
        high={'action_decision':{'required_account_level':'high'}}
        self.assertEqual(router.get_state('fresh_yes_073').capability_tier,'standard')
        # High is a preference: without a high-tier account a standard account still replies.
        fallback=router.route(high,'clinical trial')
        self.assertEqual(fallback.account_id,'fresh_yes_073');self.assertTrue(fallback.reason.startswith('high_fallback_standard'))
        self.store.set_account_policy('fresh_yes_073',capability_tier='high')
        preferred=router.route(high,'clinical trial')
        self.assertEqual(preferred.account_id,'fresh_yes_073');self.assertTrue(preferred.reason.startswith('high_tier'))
        self.store.set_account_banned('fresh_yes_073',True)
        with self.assertRaises(ValueError):router.route(high,'clinical trial')

    def test_preference_confidence_and_eligibility_before_ranking(self):
        router=bound_router(self.store,('yes_001','yes_002','yes_010'))
        data={'answers':{'preferred_occupation':hint('retail_food')}}
        self.assertEqual(router.route(data,'Bus queues').account_id,'fresh_yes_010')
        self.store.set_account_policy('fresh_yes_010',mode='standby')
        self.assertEqual(router.route(data,'Bus queues').persona.occupation_group,'transport_logistics')
        weak=hint('retail_food');weak['probabilities']['retail_food']=.69
        self.assertIsNone(preferred_group({'answers':{'preferred_occupation':weak}}))
        weak=hint('retail_food');weak['probabilities'].update(retail_food=.70,unknown=.56)
        self.assertIsNone(preferred_group({'answers':{'preferred_occupation':weak}}))

    def test_same_group_least_used_and_distinct_account_sampling(self):
        router=bound_router(self.store)
        self.store.mark_account_used('fresh_yes_001',now=time.time()-100)
        picked=router.route({},'bus')
        self.assertNotEqual(picked.account_id,'fresh_yes_001')
        extra=router.additional_candidates({},'bus','fresh_yes_001',3,rng=random.Random(1))
        self.assertEqual(len(extra),3)
        self.assertEqual({p.occupation_group for p in extra},{'transport_logistics'})
        self.assertEqual(len({p.account_id for p in extra}),3)
        self.assertNotIn('fresh_yes_001',{p.account_id for p in extra})
        self.assertEqual(router.additional_candidates({},'unrelated wording','fresh_yes_001',3),[])

    def test_catalog_version_mismatch_blocks_new_writes_and_keeps_state(self):
        router=bound_router(self.store,('yes_001',))
        old_hash=router.registry.catalog_hash
        new=deepcopy(router.registry.catalog);new['catalog_version']='yes.v3';new['personas'][0]['version']=3
        self.store.register_persona_catalog(new,activate=True)
        with self.assertRaises(ValueError):router.route({},'bus')
        ident,reason=self.store.claim_action('post','fresh_yes_001',text='example',metadata={'catalog_hash':old_hash})
        self.assertIsNone(ident);self.assertEqual(reason,'persona_catalog_mismatch')
        self.assertEqual(router.get_state('fresh_yes_001').mode,'active')
        self.assertEqual(self.store.persona_bindings()['yes_001'],'fresh_yes_001')
        changed=deepcopy(new);changed['personas'][0]['name']='Changed without revision'
        with self.assertRaises(ValueError):self.store.register_persona_catalog(changed)


class ExpansionRubricTests(unittest.TestCase):
    def test_threshold_optional_outputs_and_ordinary_action_gate(self):
        base={'action_decision':{'status':'decided','action':'reply'},'answers':{}}
        for value,allowed in ((.899999,False),(.90,True),(1,True),(float('nan'),False),(-1,False)):
            data=deepcopy(base);data['answers']['multi_reply_space']={'type':'noul','status':'valid','value':value}
            self.assertEqual(expansion_decision(data,material_ready=True)['allowed'],allowed)
            self.assertFalse(expansion_decision(data,material_ready=False)['allowed'])
        self.assertFalse(expansion_decision(base,material_ready=True)['allowed'])
        for status in ('missing','invalid','insufficient_context'):
            data=deepcopy(base);data['answers']['multi_reply_space']={'type':'noul','status':status,'value':1}
            self.assertFalse(expansion_decision(data,material_ready=True)['allowed'])
        data=deepcopy(base);data['action_decision']['action']='like'
        data['answers']['multi_reply_space']={'type':'noul','status':'valid','value':1}
        self.assertFalse(expansion_decision(data,material_ready=True)['allowed'])

    def test_v3_preserves_prior_questions_and_optional_failure_keeps_reply(self):
        old=load_rubric('post',version='v2');new=load_rubric('post',version='v3')
        self.assertEqual(old.for_stage('triage'),new.for_stage('triage'))
        for key,value in old.questions.items():self.assertEqual(new.questions[key],value)
        for bad in ('missing','invalid'):
            def handler(body,data):
                choose(data,'policy_stance','no')
                if 'reply_value' in data['answers']:set_score(data,'reply_value',2)
                for key in ('preferred_occupation','multi_reply_space'):
                    if key in data['answers']:
                        if bad=='missing':data['answers'].pop(key)
                        else:data['answers'][key]={'type':'noul','noul':'not-a-probability'}
            client=JevClient(JevConfig(api_key='synthetic-v3',max_retries=0),transport=FakeTransport(handler))
            record=content();record['task_context']={'assigned_side':'yes'};record['author']={'platform_level':'test-standard'}
            package=JevWorkflow(client,action_policy=POLICY).evaluate(record)
            self.assertEqual(package.to_dict()['action_decision']['action'],'reply')
        for version in ('v1','v2'):self.assertEqual(load_rubric('post',version=version).version,'ctn-post.'+version)

    def test_addition_review_rejects_duplicates_and_unsupported_content(self):
        def reject(body,data):
            if 'substantive_addition' in data['answers']:choose(data,'substantive_addition','duplicate_or_unsupported')
        client=JevClient(JevConfig(api_key='synthetic-postflight',max_retries=0),transport=FakeTransport(reject))
        checker=JevPostflightChecker(client)
        args=dict(post_id='p',target_text='Bus question',assigned_side='yes',intended_purpose='P08',
            previous_replies=[{'text':'Which rule applies?'}],approved_materials={'scope':'synthetic'})
        self.assertFalse(checker.check(reply_text='Which rule applies?',**args).can_send)
        verdict=checker.check(reply_text='Which entrance are you referring to?',**args)
        self.assertFalse(verdict.can_send);self.assertEqual(verdict.reason,'no_substantive_addition')


# Batch mechanics are tested with the anti-pile-on caps lifted; caps have their own tests.
@patch.dict('ctn.pipeline.targeting.POLICY',{'caps':{'per_author_per_day':10,'per_thread_per_day':10,'per_account_per_day':35}})
class PersistentBatchTests(unittest.TestCase):
    def setUp(self):
        self.store=StateStore();self.addCleanup(self.store.close)
        self.router,self.batch=batch_fixture(self.store)

    def claim_action(self,slot):
        ident,reason=self.store.claim_action('reply',slot['account_id'],slot['target_id'],'Different reply '+str(slot['slot']),metadata=slot_meta(self.router,slot))
        self.assertIsNotNone(ident,reason);self.store.start_action(ident)
        return ident

    def test_primary_first_four_fixed_slots_and_legacy_dedupe(self):
        slot=self.store.claim_reply_slot();self.assertEqual(slot['slot'],0)
        self.assertIsNone(self.store.claim_reply_slot())
        ident=self.claim_action(slot);self.store.finish_action(ident,'succeeded',response={'post_id':'reply0'})
        for i in range(1,4):
            slot=self.store.claim_reply_slot();self.assertEqual(slot['slot'],i)
            ident=self.claim_action(slot);self.store.finish_action(ident,'succeeded',response={'post_id':'reply'+str(i)})
        self.assertIsNone(self.store.claim_reply_slot())
        self.assertEqual(self.store._conn.execute('SELECT status FROM reply_batches').fetchone()[0],'completed')
        self.assertEqual(self.store.task_counts(),{'succeeded':1})
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0],4)
        self.assertIsNone(self.store.claim_action('reply','fresh_yes_001','target','legacy')[0])
        self.assertFalse(self.store.finish_action(ident,'succeeded',response={'post_id':'reply3'}))
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0],4)

    def test_unknown_pauses_until_positive_resolution(self):
        slot=self.store.claim_reply_slot();ident=self.claim_action(slot)
        self.store.finish_action(ident,'unknown')
        self.assertIsNone(self.store.claim_reply_slot())
        self.assertEqual(self.store.task_counts(),{'unknown':1})
        self.store.finish_action(ident,'succeeded',response={'post_id':'confirmed'})
        self.assertEqual(self.store.claim_reply_slot()['slot'],1)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0],1)

    def test_failed_primary_cancels_remaining_without_replacement(self):
        slot=self.store.claim_reply_slot();ident=self.claim_action(slot)
        self.store.finish_action(ident,'failed',detail='rejected')
        self.assertIsNone(self.store.claim_reply_slot())
        self.assertEqual(dict(self.store._conn.execute('SELECT status,COUNT(*) FROM reply_slots GROUP BY status')),{'cancelled':3,'failed':1})

    def test_edit_and_expiration_cancel_unsent_work(self):
        self.store.upsert_observed_post(Post('target','citizen','Changed target',time.time()))
        self.assertIsNone(self.store.claim_reply_slot())
        self.assertEqual(self.store._conn.execute('SELECT reason FROM reply_batches').fetchone()[0],'target_edited')
        self.assertNotIn('planned',self.store.task_counts()) # edited observation is final, no redraw
        other=StateStore();self.addCleanup(other.close);_,_=batch_fixture(other,now=time.time()-1900)
        self.assertIsNone(other.claim_reply_slot())
        self.assertEqual(other._conn.execute('SELECT reason FROM reply_batches').fetchone()[0],'opportunity_expired')

    def test_slot_lease_account_occupancy_and_stale_owner(self):
        now=time.time();slot=self.store.claim_reply_slot(now=now,lease_s=1)
        self.assertIsNone(self.store.claim_action('post',slot['account_id'],text='other',metadata=self.router.context_for(self.router.registry.get(slot['account_id'])))[0])
        resumed=self.store.claim_reply_slot(now=now+2)
        self.assertNotEqual(resumed['owner'],slot['owner'])
        self.assertFalse(self.store.save_slot_draft(slot,{}))
        self.assertFalse(self.store.reply_slot_ready(slot['account_id'],'target',slot_meta(self.router,slot))[0])

    def test_restart_retains_draws_and_does_not_replay_v3_migration(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'state.db';store=StateStore(path);router,batch=batch_fixture(store)
            before=[tuple(r) for r in store._conn.execute('SELECT persona_id,account_id FROM reply_slots ORDER BY slot')]
            store.set_account_frozen('fresh_yes_001',True)
            store._conn.execute('PRAGMA user_version=3');store.close()
            reopened=StateStore(path);self.addCleanup(reopened.close)
            self.assertEqual(reopened._conn.execute('PRAGMA user_version').fetchone()[0],6)
            self.assertTrue(Path(str(path)+'.before-v6.bak').exists())
            self.assertTrue(reopened.get_account_state('fresh_yes_001').is_frozen)
            self.assertEqual(before,[tuple(r) for r in reopened._conn.execute('SELECT persona_id,account_id FROM reply_slots ORDER BY slot')])
            self.assertIsNotNone(reopened.claim_reply_slot())


# Batch mechanics are tested with the anti-pile-on caps lifted; caps have their own tests.
@patch.dict('ctn.pipeline.targeting.POLICY',{'caps':{'per_author_per_day':10,'per_thread_per_day':10,'per_account_per_day':35}})
class BatchEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def make_loop(self,*,dry=False,probability=.9,duplicate=False):
        store=StateStore();self.addCleanup(store.close);router=bound_router(store)
        store.set_policy('reply_batches_enabled',True)
        class Three(random.Random):
            def randint(self,a,b):return 3
        router.rng=Three(5)
        def handler(body,data):
            if 'policy_stance' in data['answers']:choose(data,'policy_stance','no')
            if 'reply_value' in data['answers']:
                set_score(data,'reply_value',2);choose(data,'expression_function','venting')
            if 'multi_reply_space' in data['answers']:data['answers']['multi_reply_space']['noul']=probability
            if 'preferred_occupation' in data['answers']:choose(data,'preferred_occupation','transport_logistics')
            if 'substantive_addition' in data['answers']:choose(data,'substantive_addition','new_supported_content')
        transport=FakeTransport(handler);client=JevClient(JevConfig(api_key='synthetic-batch-jev',max_retries=0),transport=transport)
        workflow=JevWorkflow(client,action_policy=POLICY)
        requests=[]
        texts=['Which entry rule do you mean for ferry passengers?',
               'Are you asking about checks at the terminal or onboard?',
               'Is your concern the queue itself or how proof would be checked?',
               'Which part of staff access needs a clearer explanation?']
        def send(request):
            requests.append(json.loads(request.content))
            reply=texts[0] if duplicate else texts[min(len(requests)-1,3)]
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':reply}}]})
        llm=DeepSeekClient(LLMConfig(api_key='synthetic-batch-llm',max_retries=0),transport=httpx.MockTransport(send));self.addCleanup(llm.close)
        brain=JevLlmBrain(workflow,ReplyGenerator(workflow,llm),side=Side.YES,router=router,postflight=JevPostflightChecker(client))
        platform=MockLegitClient(failure_rate=0,latency_ms=(0,0));platform.posts['target']=MockPost('target','citizen','This bus terminal entry rule is frustrating',time.time())
        a=actor(platform,store,dry=dry)
        loop=BotLoop(a,side=Side.YES,accounts=list(store.persona_bindings().values()),brain=brain,store=store,max_actions_per_cycle=4)
        store.upsert_observed_post(platform._as_dict(platform.posts['target']))
        return loop,platform,transport,requests

    async def test_four_slots_share_one_assessment_and_successful_batch_context(self):
        loop,platform,transport,requests=await self.make_loop()
        report=await loop.process_pending()
        self.assertEqual(len(report.results),4)
        self.assertEqual(platform.write_count,4)
        self.assertEqual(len([c for c in transport.calls if 'policy_stance' in c['questions']]),2)
        self.assertEqual(len(requests),4)
        for i in range(1,4):
            context=json.loads(requests[i]['messages'][1]['content'])['batch_context']
            self.assertEqual(len(context['previous_successful_replies']),i)
        self.assertEqual(len({r.account for r in report.results}),4)
        await loop.process_pending();self.assertEqual(platform.write_count,4)
        self.assertEqual(loop.store._conn.execute('SELECT status FROM reply_batches').fetchone()[0],'completed')

    async def test_duplicate_extras_cancel_without_refill(self):
        loop,platform,_,requests=await self.make_loop(duplicate=True)
        await loop.process_pending()
        self.assertEqual(platform.write_count,1)
        self.assertEqual(len(requests),7) # main plus at most one rewrite per fixed extra
        self.assertEqual(dict(loop.store._conn.execute('SELECT status,COUNT(*) FROM reply_slots GROUP BY status')),{'cancelled':3,'succeeded':1})

    async def test_single_reply_below_threshold(self):
        loop,platform,_,_=await self.make_loop(probability=.899)
        await loop.process_pending();self.assertEqual(platform.write_count,1)
        self.assertEqual(loop.store._conn.execute('SELECT COUNT(*) FROM reply_slots').fetchone()[0],1)

    async def test_simulation_does_not_create_batches_or_metrics(self):
        loop,platform,_,_=await self.make_loop(dry=True)
        report=await loop.process_pending()
        self.assertEqual(report.results[0].status,'simulated');self.assertEqual(platform.write_count,0)
        for table in ('reply_batches','reply_slots','publications','performance_jobs','action_intents'):
            self.assertEqual(loop.store._conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        self.assertTrue(all(s.post_count==0 for s in loop.store.load_all_account_states().values()))
        await PerformanceCollector(loop.actuator,loop.brain.router).step()
        self.assertEqual(loop.store.performance_report()['groups'],[])

    async def test_lost_response_pauses_and_never_automatically_resends(self):
        loop,platform,_,_=await self.make_loop()
        original=platform.reply
        async def lost(*args,**kwargs):
            await original(*args,**kwargs);raise TimeoutError()
        platform.reply=lost
        await loop.process_pending();await loop.process_pending()
        self.assertEqual(platform.write_count,1)
        self.assertEqual(len(loop.store.list_actions('unknown')),1)
        self.assertEqual(loop.store._conn.execute('SELECT status FROM reply_batches').fetchone()[0],'paused')


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.store=StateStore();self.addCleanup(self.store.close)
        self.router=bound_router(self.store,('yes_001',))

    def publish(self,*,now=None,unknown=False):
        now=time.time() if now is None else now
        meta={**self.router.context_for(self.router.registry.get('fresh_yes_001')),'topic':'transport','purpose':'P08'}
        ident,reason=self.store.claim_action('post','fresh_yes_001',text='An original',metadata=meta,now=now)
        self.assertIsNotNone(ident,reason);self.store.start_action(ident)
        self.store.finish_action(ident,'unknown' if unknown else 'succeeded',response={} if unknown else {'post_id':'own-post','created_at':now},now=now)
        return ident

    def test_publication_windows_confirmation_and_idempotency(self):
        now=time.time();ident=self.publish(now=now,unknown=True)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0],0)
        self.store.finish_action(ident,'succeeded',response={'post_id':'own-post','created_at':now},now=now+20)
        self.assertFalse(self.store.finish_action(ident,'succeeded',response={'post_id':'own-post'},now=now+30))
        row=dict(self.store._conn.execute('SELECT * FROM publications').fetchone())
        self.assertEqual((row['persona_id'],row['persona_version'],row['occupation_group']),('yes_001',2,'transport_logistics'))
        self.assertEqual(row['confirmed_at'],now+20)
        self.assertEqual([r[0] for r in self.store._conn.execute('SELECT window_s FROM performance_jobs ORDER BY window_s')],[300,900,3600,21600,86400])

    def test_missing_is_unknown_negative_changes_and_rank_scope(self):
        now=time.time();self.publish(now=now)
        for at,raw in ((now+300,{'post_id':'own-post','likes':10}), (now+900,{'post_id':'own-post','likes':7,'replies':2})):
            job=self.store.claim_performance_jobs(now=at)[0]
            self.store.record_performance_observation(job,raw,source='synthetic',now=at)
        self.store.record_feed_scan('scan1',[{'post_id':'other'},{'post_id':'own-post'}],now=now+100)
        self.store.record_feed_scan('scan2',[],now=now+400)
        self.store.ensure_performance_jobs(['fresh_yes_001'],now=now)
        for job in self.store.claim_performance_jobs(now=now):
            self.store.record_performance_observation(job,{'followers':11} if job['kind']=='account' else [],source='synthetic',now=now)
        self.store.ensure_performance_jobs(['fresh_yes_001'],now=now+21600)
        for job in self.store.claim_performance_jobs(now=now+21600,limit=10):
            self.store.record_performance_observation(job,{'followers':8} if job['kind']=='account' else None,source='synthetic',now=now+21600)
        report=self.store.performance_report(since=now-1,now=now+21601)
        row=report['groups'][0]
        self.assertEqual(row['metrics']['reposts']['total'],None)
        self.assertEqual(row['observed_like_change']['total'],-3)
        self.assertEqual(row['followers_net_change']['total'],-3)
        self.assertEqual(row['ranked_feed']['best_observed_rank'],2)
        self.assertEqual(row['ranked_feed']['scope'],'sample_only')
        self.assertFalse(report['automatic_routing_adjustment'])
        self.assertIn('unknown',render_report(report))
        self.assertNotIn('competition_revenue',json.dumps(report))

    def test_failed_sampling_keeps_attempt_history_and_recovers_lease(self):
        now=time.time();self.publish(now=now)
        job=self.store.claim_performance_jobs(now=now+300)[0]
        self.assertEqual(self.store.claim_performance_jobs(now=now+301),[])
        resumed=self.store.claim_performance_jobs(now=now+481)[0]
        self.assertNotEqual(job['owner'],resumed['owner'])
        self.assertFalse(self.store.record_performance_observation(job,{'likes':10},source='stale',now=now+482))
        self.store.record_performance_observation(resumed,None,source='failed',now=now+482)
        retry=self.store.claim_performance_jobs(now=now+782)[0]
        self.store.record_performance_observation(retry,{'post_id':'own-post'},source='retry',now=now+782)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM metric_observation_events').fetchone()[0],2)
        row=self.store._conn.execute('SELECT likes,status,observed_at FROM post_metric_snapshots').fetchone()
        self.assertEqual(tuple(row),(None,'available',now+782))

    def test_normalized_missing_metrics_do_not_become_zero(self):
        from ctn.client.legit_v1 import OfficialLegitClient
        from ctn.storage.performance import metric
        c=OfficialLegitClient(url='https://example.invalid')
        raw=c.normalize_post({'post_id':123,'body':'text','author':'a','likes':0})
        self.assertEqual(metric(raw,'likes'),0)
        self.assertIsNone(metric(raw,'replies'))
        self.assertIsNone(metric(raw,'reposts'))

    def test_v3_migration_failure_rollback_preserves_backup_and_states(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'old.db';store=StateStore(path)
            store.set_account_policy('old_identity',mode='dormant',original_locked_until=time.time()+999)
            store.mark_account_used('old_identity',now=100)
            store._conn.execute('PRAGMA user_version=3');store.close()
            def fail(store):
                store._conn.execute('CREATE TABLE should_rollback(id TEXT)')
                raise RuntimeError('synthetic migration failure')
            with patch.object(StateStore,'migrate_campaign',fail),self.assertRaises(RuntimeError):StateStore(path)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],3)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone())
                self.assertEqual(conn.execute("SELECT mode,post_count FROM account_states WHERE account_id='old_identity'").fetchone(),('dormant',1))
            with closing(sqlite3.connect(str(path)+'.before-v6.bak')) as backup:
                self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0],3)


class CollectorAndRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_reuse_observed_data_and_record_failures_as_unknown(self):
        store=StateStore();self.addCleanup(store.close);router=bound_router(store,('yes_001',))
        now=time.time();store.ensure_performance_jobs(['fresh_yes_001'],now=now)
        platform=MockLegitClient(failure_rate=0,latency_ms=(0,0))
        async def fail(*args,**kwargs):raise PlatformError('synthetic',status=503,retryable=False)
        platform.get_profile=fail
        a=actor(platform,store);collector=PerformanceCollector(a,router)
        store.record_feed_scan('recent',[{'post_id':'ranked'}],now=time.time())
        await collector.step()
        row=store._conn.execute('SELECT followers,status FROM account_metric_snapshots').fetchone()
        self.assertEqual(tuple(row),(None,'unavailable'))
        self.assertTrue(store._conn.execute("SELECT 1 FROM feed_scans WHERE source='shared_ranked_sample'").fetchone())

    async def test_saved_draft_expiration_cancels_slot_without_model_calls(self):
        store=StateStore();self.addCleanup(store.close);router,batch=batch_fixture(store)
        platform=MockLegitClient(failure_rate=0,latency_ms=(0,0))
        from types import SimpleNamespace
        brain=SimpleNamespace(router=router)
        loop=BotLoop(actor(platform,store),side=Side.YES,accounts=list(store.persona_bindings().values()),store=store,brain=brain)
        store._conn.execute("UPDATE reply_slots SET decision_json='{}',draft_at=? WHERE slot=0",(time.time()-301,))
        await loop.process_pending()
        self.assertEqual(store._conn.execute('SELECT reason FROM reply_batches').fetchone()[0],'draft_expired')
        self.assertEqual(platform.write_count,0)

    async def test_budget_deferral_does_not_redraw_or_generate(self):
        setup=BatchEndToEndTests();self.addCleanup(setup.doCleanups)
        loop,platform,transport,requests=await setup.make_loop()
        loop.actuator.ledger.can_write=lambda:False
        await loop.process_pending()
        self.assertEqual(len(requests),1);self.assertEqual(platform.write_count,0)
        first=[tuple(r) for r in loop.store._conn.execute('SELECT persona_id,account_id FROM reply_slots ORDER BY slot')]
        loop.store._conn.execute('UPDATE reply_slots SET next_attempt_at=0')
        await loop.process_pending()
        self.assertEqual(len(requests),1)
        self.assertEqual(first,[tuple(r) for r in loop.store._conn.execute('SELECT persona_id,account_id FROM reply_slots ORDER BY slot')])

    async def test_missing_publication_id_is_unknown(self):
        store=StateStore();self.addCleanup(store.close)
        class MissingId:
            async def post(self,*args,**kwargs):return {'ok':True}
        result=await actor(MissingId(),store).post('a','Synthetic')
        self.assertEqual(result.status,'unknown')
        self.assertEqual(store._conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0],0)


def send_slot_from_process(path,url,queue):
    import urllib.request
    class NetworkClient:
        async def reply(self,account,post_id,text):
            def request():
                req=urllib.request.Request(url,data=json.dumps({'account':account,'post_id':post_id,'text':text}).encode(),method='POST',headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
            return await asyncio.to_thread(request)
    with closing(StateStore(path)) as store:
        router=PersonaRouter(store=store)
        slot=store.claim_reply_slot()
        if slot is None:queue.put('idle');return
        result=asyncio.run(actor(NetworkClient(),store).reply(slot['account_id'],slot['target_id'],'Synthetic fixed-slot response',context=slot_meta(router,slot)))
        queue.put(result.status)


class MultiprocessSlotWireTests(unittest.TestCase):
    def test_two_independent_processes_send_one_slot_once(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers['Content-Length'])))
                body=b'{"post_id":"single-slot-result"}'
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                path=str(Path(td)/'shared.db')
                with closing(StateStore(path)) as store:batch_fixture(store,count=1)
                ctx=multiprocessing.get_context('spawn');queue=ctx.Queue()
                processes=[ctx.Process(target=send_slot_from_process,args=(path,f'http://127.0.0.1:{server.server_port}/reply',queue)) for _ in range(2)]
                for p in processes:p.start()
                for p in processes:p.join(15);self.assertEqual(p.exitcode,0)
                statuses=[queue.get(timeout=2) for _ in processes];queue.close()
                self.assertEqual(sorted(statuses),['idle','succeeded'])
                self.assertEqual(len(received),1)
                with closing(StateStore(path)) as store:
                    self.assertEqual(store._conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0],1)
                    self.assertEqual(len(store.list_actions('succeeded')),1)
        finally:server.shutdown();server.server_close();thread.join(2)


class LocalPersonaOperationsTests(unittest.TestCase):
    def test_cli_bind_group_activation_standby_report_and_phase_preservation(self):
        from ctn.ops import main
        with tempfile.TemporaryDirectory() as td:
            path=str(Path(td)/'local.db')
            def call(*args):
                out=io.StringIO()
                with redirect_stdout(out):code=main(['--state-file',path,*args])
                self.assertEqual(code,0)
                return json.loads(out.getvalue())
            call('persona','bind','--id','yes_001','--account','prepared_one')
            listing=call('persona','list')
            self.assertEqual(listing['personas'][0]['mode'],'standby')
            call('persona','activate','--group','transport_logistics')
            self.assertEqual(call('persona','list')['personas'][0]['mode'],'active')
            call('persona','standby','--id','yes_001')
            call('phase','shaping','--until','2099-01-01T00:00:00+11:00')
            with closing(StateStore(path)) as store:
                store.seed_campaign_policy(PersonaRegistry().list_all())
                self.assertEqual(store.effective_phase(),'shaping')
            self.assertEqual(call('performance')['groups'],[])
            self.assertEqual(call('persona','show','--id','yes_001')['profile']['name'],'Nolan Mercer')

    def test_additional_pool_rejects_purpose_or_material_mismatch(self):
        with closing(StateStore()) as store:
            router=bound_router(store)
            self.assertEqual(router.additional_candidates({},'bus','fresh_yes_001',3,purpose='P09',materials={}),[])
            self.assertEqual(router.additional_candidates({},'bus','fresh_yes_001',3,purpose='P02',materials={
                'prompt_type':'P02','action_decision':{'action':'reply'},'support':[]}),[])

    def test_revision_identity_and_bad_bindings_are_rejected(self):
        with closing(StateStore()) as store:
            PersonaRegistry().attach(store)
            for account in ('',' ','with space','trailing '):
                with self.assertRaises(ValueError):store.bind_persona('yes_001',account)
            before=store.get_account_state('published_identity')
            ident,_=store.claim_action('post','published_identity',text='original')
            # A catalog also blocks direct uncatalogued actions.
            self.assertIsNone(ident)
            self.assertEqual(before.post_count,0)


class UnboundServiceStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_offline_service_starts_with_all_profiles_unbound(self):
        from ctn.config import Settings
        from ctn.main import serve
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);database=root/'state'/'mock.sqlite3'
            settings=Settings(side=Side.YES,mode='mock',dry_run=True,state_dir=root/'state',log_dir=root/'logs')
            with patch.object(asyncio.get_running_loop(),'add_signal_handler'):
                await serve(settings,mode='mock',state_file=database,once=True)
            heartbeat=json.loads((root/'state'/'heartbeat').read_text())
            self.assertEqual(heartbeat['error'],'')
            self.assertFalse(heartbeat['alive']) # clean one-cycle shutdown
            with closing(StateStore(database)) as store:
                self.assertEqual(store.persona_bindings(),{})
                self.assertEqual(store.load_all_account_states(),{})
                self.assertEqual(store.list_actions(),[])
                self.assertEqual(store._conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0],0)


class SimulationQueueIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_simulation_draft_is_reassessed_before_real_execution(self):
        setup=BatchEndToEndTests();self.addCleanup(setup.doCleanups)
        loop,platform,transport,requests=await setup.make_loop(dry=True)
        loop.max_actions_per_cycle=0
        await loop.process_pending()
        self.assertEqual(len(requests),1)
        saved=json.loads(loop.store._conn.execute('SELECT draft_json FROM post_tasks').fetchone()[0])
        self.assertNotIn('reply_batch_plan',saved['scores']['raw'])
        self.assertEqual(saved['scores']['raw']['planning_mode'],'simulated')
        self.assertEqual(loop.store._conn.execute('SELECT COUNT(*) FROM reply_batches').fetchone()[0],0)
        loop.actuator.dry_run=False;loop.max_actions_per_cycle=4
        loop.store._conn.execute('UPDATE post_tasks SET next_attempt_at=0')
        await loop.process_pending()
        self.assertEqual(len([c for c in transport.calls if 'policy_stance' in c['questions']]),4)
        self.assertGreater(platform.write_count,0)
        self.assertEqual(loop.store._conn.execute('SELECT COUNT(*) FROM reply_batches').fetchone()[0],1)
        rows=loop.store._conn.execute('SELECT root_post_id,metadata_json FROM publications').fetchall()
        self.assertTrue(all(r['root_post_id']=='target' for r in rows))
        self.assertTrue(all(json.loads(r['metadata_json'])['prompt_version']=='ctn-reply-prompts.v2' for r in rows))


class PublicationAttributionConflictTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_platform_post_cannot_increment_two_actions(self):
        with closing(StateStore()) as store:
            router=bound_router(store,('yes_001','yes_002'))
            class DuplicateId:
                async def post(self,*args,**kwargs):return {'post_id':'same-platform-id'}
            a=actor(DuplicateId(),store)
            first=await a.post('fresh_yes_001','First distinct text',context=router.context_for(router.registry.get('fresh_yes_001')))
            second=await a.post('fresh_yes_002','Second distinct text',context=router.context_for(router.registry.get('fresh_yes_002')))
            self.assertTrue(first.ok);self.assertEqual(second.status,'unknown')
            self.assertEqual(store.get_account_state('fresh_yes_001').post_count,1)
            self.assertEqual(store.get_account_state('fresh_yes_002').post_count,0)
            self.assertEqual(store._conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0],1)
