"""Resume fixed, reviewed reply slots. Model and network work stay outside transactions."""
import asyncio
import json
import time
from ctn.models import ActionKind,to_jsonable
from ctn.client.base import AuthError
from ctn.llm.models import PROMPT_VERSION


async def plan_reply_batch(loop,task,decision):
    plan=decision.scores.raw.pop('reply_batch_plan',None)
    if not plan or loop.actuator.dry_run:return False
    primary=to_jsonable(decision)
    plan={**plan,'primary_decision':primary,'input_version':task['version']}
    accounts=[decision.account]+plan['extra_accounts']
    slots=[]
    for index,account in enumerate(accounts):
        persona=loop.brain.router.registry.get(account)
        if persona is None:raise ValueError('planned persona missing')
        slots.append({'persona_id':persona.persona_id,'persona_version':persona.version,
            'account_id':account,**({'decision':primary,'draft_at':task['draft_at']} if index==0 else {})})
    ident,created=await asyncio.to_thread(loop.store.create_reply_batch,decision.post_id,task['version'],plan,slots,
        expires_at=task['expires_at'])
    if not ident:
        await asyncio.to_thread(loop.store.update_post_task,task,'skipped',reason='historical_reply_or_expired')
    return True


async def run_reply_batches(loop,report,limit):
    if not loop.brain or not loop.brain.router.registry.catalog_hash or loop.actuator.dry_run:return
    for _ in range(max(0,limit)):
        slot=await asyncio.to_thread(loop.store.claim_reply_slot)
        if slot is None:return
        async def renew():
            while True:
                await asyncio.sleep(20)
                await asyncio.to_thread(loop.store.renew_reply_slot,slot)
        lease=asyncio.create_task(renew())
        try:
            plan=json.loads(slot['plan_json'])
            if plan.get('prompt_version',PROMPT_VERSION)!=PROMPT_VERSION:
                await asyncio.to_thread(loop.store.defer_reply_slot,slot,'prompt_version_mismatch',cancel=True);continue
            if plan.get('rag_snapshot_id'):
                await asyncio.to_thread(loop.store.load_rag_snapshot,plan['rag_snapshot_id'])
            persona=loop.brain.router.registry.get(slot['account_id'])
            if (not persona or persona.persona_id!=slot['persona_id'] or persona.version!=slot['persona_version']
                or loop.brain.router.registry.catalog_hash!=slot['catalog_hash']):
                await asyncio.to_thread(loop.store.defer_reply_slot,slot,'persona_version_mismatch',cancel=True);continue
            state=await asyncio.to_thread(loop.brain.router.get_state,persona.account_id)
            if state.is_disabled():
                await asyncio.to_thread(loop.store.defer_reply_slot,slot,'account_disabled',cancel=True);continue
            if state.is_in_cooldown(time.time(),persona.cooldown_s):
                await asyncio.to_thread(loop.store.defer_reply_slot,slot,'account_cooldown');continue
            if slot['decision_json']:
                if time.time()-slot['draft_at']>300:
                    await asyncio.to_thread(loop.store.defer_reply_slot,slot,'draft_expired',cancel=True);continue
                raw=json.loads(slot['decision_json'])
            else:
                previous=await asyncio.to_thread(loop.store.successful_batch_replies,slot['batch_id'])
                raw,reason=await asyncio.to_thread(loop.brain.draft_additional,plan,persona,previous,plan['primary_decision'])
                if raw is None:
                    await asyncio.to_thread(loop.store.defer_reply_slot,slot,reason,cancel=True);continue
                slot['draft_at']=raw['scores']['raw'].get('draft_created_at',time.time())
                if not await asyncio.to_thread(loop.store.save_slot_draft,slot,raw,now=slot['draft_at']):continue
            decision=loop._restore_decision(raw)
            decision.scores.raw.update(reply_batch_id=slot['batch_id'],reply_slot=slot['slot'],slot_owner=slot['owner'],
                target_version=slot['target_version'],draft_expires_at=slot['draft_at']+300,
                opportunity_expires_at=slot['expires_at'],prompt_version=plan['prompt_version'],
                material_version=plan['material_version'],post_rubric_version=plan['post_rubric_version'])
            loop._log_decision(decision)
            result=await loop._execute(decision)
            report.results.append(result)
            if result.ok:loop._remember(decision,result)
            if not result.action_id:
                terminal=result.detail in ('target_edited','draft_or_opportunity_expired','persona_catalog_mismatch',
                    'account_disabled','account_level_required','invalid_reply_slot','duplicate_publication','near_duplicate_publication','rag_snapshot_withdrawn')
                await asyncio.to_thread(loop.store.defer_reply_slot,slot,result.detail,cancel=terminal)
        except AuthError:
            loop.actuator.suspended.add(slot['account_id'])
            await asyncio.to_thread(loop.store.set_account_frozen,slot['account_id'],True)
            await asyncio.to_thread(loop.store.defer_reply_slot,slot,'authentication_failed',cancel=True)
        except (KeyError,TypeError,ValueError):
            await asyncio.to_thread(loop.store.defer_reply_slot,slot,'invalid_saved_batch_context',cancel=True)
        except Exception as exc:
            # Dependency errors leave this fixed candidate queued, never draw a replacement.
            await asyncio.to_thread(loop.store.defer_reply_slot,slot,'dependency_error:'+type(exc).__name__,delay=30)
        finally:
            lease.cancel();await asyncio.gather(lease,return_exceptions=True)
