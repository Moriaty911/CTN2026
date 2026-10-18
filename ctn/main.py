"""Continuous competition service. Pi alone owns original-post strategy."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import time

from .actuator import Actuator
from .client.legit_v1 import OfficialLegitClient
from .client.mock import MockLegitClient
from .config import Settings
from .jev import JevClient,JevWorkflow,JevPostflightChecker,JevEvaluator,JevPacker,BackgroundReference,load_rubric
from .llm import DeepSeekClient,ReplyGenerator
from .models import Side
from .persona import PersonaRouter,PersonaRegistry
from .pipeline.brain import JevLlmBrain
from .pipeline.loop import BotLoop
from .moderation.events import UnavailableModerationSource,consume_events
from .ratelimit import build_gate,build_ledger
from .storage import StateStore
from .telemetry import Telemetry
from .reconcile import reconcile_unknown_posts
from .jev.rubrics import ACTIVE_POST_VERSION
from .analytics import PerformanceCollector

async def serve(settings,*,mode=None,state_file=None,once=False):
    mode=settings.execution_mode(mode)
    if mode=='live': settings.require_live_writes(replies=True)
    registry=PersonaRegistry()
    if settings.side.value and settings.side.value!=registry.catalog['side']:
        raise ValueError('assigned side conflicts with persona catalog')
    os.umask(0o077)
    store=StateStore(settings.database_path(state_file))
    store.bind_data_source("mock" if mode=="mock" else "real")
    if settings.side in (Side.YES,Side.NO): store.bind_campaign_side(settings.side.value)
    router=PersonaRouter(registry=registry,default_side=settings.side.value,store=store)
    store.seed_campaign_policy(router.registry.list_all())
    if min(settings.max_pending_reports,settings.max_pending_per_author)<0: raise ValueError('negative pending report limit')
    store.set_policy('max_pending_reports',settings.max_pending_reports)
    store.set_policy('max_pending_per_author',settings.max_pending_per_author)
    store.set_policy('report_mode',settings.report_mode)
    store.set_policy('require_warmup',mode!='mock')
    from .rag.corpus import Corpus
    corpus=Corpus(settings.rag_corpus or None)
    corpus.install_sources(store)
    if mode=='live' and not any(not s.is_disabled() for s in store.account_states_for(store.persona_bindings().values()).values()):
        store.close()
        raise ValueError('live service needs a bound active account; use explicit fleet operations first')
    telemetry=Telemetry(settings.log_dir,settings.log_level)
    stop=asyncio.Event()
    for signum in (signal.SIGINT,signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum,stop.set)
    if mode=='mock': client=MockLegitClient(failure_rate=0.)
    else:
        if not settings.platform.api_key: raise ValueError('platform key missing; use --mock offline')
        client=OfficialLegitClient(url=settings.platform.base_url or None,team_key=settings.platform.api_key,
            default_password=settings.platform.bot_password,timeout=settings.platform.request_timeout_s,
            auto_register=False,store=store)
        client.configure_pacing(settings.rate,settings.budget)
    actuator=Actuator(client,build_gate(settings.rate),build_ledger(settings.budget,store=store),
        dry_run=mode!='live',store=store,telemetry=telemetry)
    actuator.catalog_hash=router.registry.catalog_hash
    brain=None
    jev=None;llm=None
    if mode!='mock' and settings.side in (Side.YES,Side.NO) and settings.jev.enabled and settings.jev.api_key and settings.llm.api_key:
        jev=JevClient(settings.jev);llm=DeepSeekClient(settings.llm)
        refs=tuple(BackgroundReference(**r) for r in store.policy('trusted_background',[]))
        workflow=JevWorkflow(jev,evaluator=JevEvaluator(
            jev,packer=JevPacker(verified_references=refs),
            post_questions=load_rubric("post",version=ACTIVE_POST_VERSION)))
        brain=JevLlmBrain(workflow,ReplyGenerator(workflow,llm),side=settings.side,router=router,
            postflight=JevPostflightChecker(jev),trusted_background=refs,corpus=corpus)
    accounts=[p.account_id for p in router.registry.list_by_side(settings.side.value) if p.account_id]
    loop=BotLoop(actuator,side=settings.side,accounts=accounts,store=store,brain=brain,telemetry=telemetry)
    performance=PerformanceCollector(actuator,router)
    source=UnavailableModerationSource()
    loop.reporter.enabled=settings.report_mode!="off"
    loop.reporter.event_source=source
    if mode=='live' and brain is None: raise ValueError('live replies require configured JEV and LLM')
    # Read-only/dry sessions without model credentials collect only, never substitute heuristics for live decisions.
    process_replies=(brain is not None or mode=='mock')
    heartbeat=settings.state_dir/'heartbeat'
    progress={};running=True;error='';started_at=time.time()
    def write_heartbeat():
        runtime=store.runtime_metrics()
        counts=runtime["tasks"]
        now=time.time()
        payload={**runtime,'alive':running,'timestamp':now,'mode':mode,'workers':progress,'error':error,
            'tasks':counts,
            'side':settings.side.value,'report_mode':settings.report_mode,'active_accounts':sum(not s.is_disabled() for s in store.account_states_for(store.persona_bindings().values()).values()),
            'rag_version':corpus.data['corpus_version'],'moderation_capability':'unavailable',
            'auto_reports_effective':settings.report_mode=='hybrid' and store.report_state_ready(automatic=True)[0],
            'report_block_reason':store.report_state_ready(automatic=settings.report_mode=='hybrid')[1],
            'business_stalled':bool((counts.get('queued',0) or runtime['reply_slots'].get('pending',0)) and process_replies and
                now-(store.get_counter('business_progress_ms')/1000 or started_at)>300),
            'last_business_progress_at':store.get_counter('business_progress_ms')/1000 or started_at,
            'requests':actuator.ledger.snapshot()}
        heartbeat.parent.mkdir(parents=True,exist_ok=True)
        tmp=heartbeat.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload),encoding='utf-8');tmp.chmod(0o600);tmp.replace(heartbeat)
    async def worker(name,step,interval):
        while not stop.is_set():
            start=time.monotonic()
            progress[name]={**progress.get(name,{}),'started_at':time.time()}
            try:
                await step()
                progress[name].pop('error',None)
                progress[name]['completed_at']=time.time()
            except Exception as exc:
                from .client.base import PlatformError
                if not isinstance(exc,(PlatformError,TimeoutError,OSError)) or isinstance(exc,OSError) and not isinstance(exc,(TimeoutError,ConnectionError)):
                    raise
                progress[name]['error']=type(exc).__name__
                telemetry.error('worker dependency failed',worker=name,error=type(exc).__name__)
            telemetry.metric('worker',worker=name,duration_s=round(time.monotonic()-start,3))
            progress[name]['attempted_at']=time.time()
            if once: return
            try: await asyncio.wait_for(stop.wait(),timeout=max(.01,interval-(time.monotonic()-start)))
            except asyncio.TimeoutError: pass
    async def state_step():
        await asyncio.to_thread(store.recover_actions)
        await consume_events(store,source)
        # Reconciliation has its own worker; it cannot stall the heartbeat.
    tasks=[]
    stopper=None
    async def shutdown_after_signal():
        await stop.wait()
        if tasks:
            _,pending=await asyncio.wait(tasks,timeout=20)
            for task in pending: task.cancel()
    try:
        # Official verified status is separate from detector suspicion. The read is metered.
        getter=getattr(client,'list_verified_accounts',None)
        if getter:
            rows=await actuator._read('verified_accounts',getter,[])
            store.register_verified_accounts(rows)
        if mode=='live':
            with store.batch_transaction():
                store._conn.execute("UPDATE post_tasks SET status='queued',next_attempt_at=? WHERE status='simulated'",(time.time(),))
        telemetry.info('service started',mode=mode,database=store.db_path,moderation_capability='unavailable')
        tasks=[asyncio.create_task(worker('frontline',loop.run_frontline_step,1.5)),
               asyncio.create_task(worker('comments',loop.run_comment_step,2.5)),
               asyncio.create_task(worker('moderation',loop.reporter.sweep_and_report,10)),
               asyncio.create_task(worker('state',state_step,10)),
               asyncio.create_task(worker('heartbeat',lambda:asyncio.to_thread(write_heartbeat),5))]
        if process_replies: tasks.append(asyncio.create_task(worker('reply',loop.process_pending,1)))
        if brain is not None:
            from .warmup import warmup_step
            tasks.append(asyncio.create_task(worker('warmup',lambda:warmup_step(actuator,router,brain.postflight),30)))
        tasks.append(asyncio.create_task(worker('performance',performance.step,30)))
        if mode!='mock': tasks.append(asyncio.create_task(worker('reconcile',lambda:reconcile_unknown_posts(actuator,store),60)))
        stopper=asyncio.create_task(shutdown_after_signal())
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        if not stop.is_set(): raise
    except BaseException as exc:
        error=type(exc).__name__
        telemetry.error('service worker stopped',error=error)
        raise
    finally:
        stop.set()
        if stopper:
            stopper.cancel()
            await asyncio.gather(stopper,return_exceptions=True)
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        running=False
        write_heartbeat()
        close=getattr(client,'close',None)
        if close: await close()
        for component in (jev,llm):
            closer=getattr(component,'close',None)
            if closer: closer()
        telemetry.close();store.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--mock',action='store_const',dest='mode',const='mock')
    modes.add_argument('--dry-run',action='store_const',dest='mode',const='dry-run')
    modes.add_argument('--live',action='store_const',dest='mode',const='live')
    parser.add_argument('--state-file')
    parser.add_argument('--side',choices=['yes','no'])
    parser.add_argument('--once',action='store_true',help='one offline/smoke cycle')
    args=parser.parse_args(argv)
    settings=Settings.load()
    if args.side: settings=replace(settings,side=Side(args.side))
    try: asyncio.run(serve(settings,mode=args.mode,state_file=args.state_file,once=args.once))
    except (ValueError,RuntimeError) as exc:
        parser.exit(2,str(exc)+'\n')

if __name__=='__main__': main()
