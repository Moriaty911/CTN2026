"""Stream completed assessments into a bounded priority drafting queue."""
import asyncio
import heapq
import time
from ctn.models import ActionKind


def assessment_priority(data, task):
    action=data.get('action_decision') or {}
    # Ordinal scheduling heuristic, never a probability or expected score.
    base=20 if action.get('action')=='reply' else 0
    from ctn.persona.router import _extract_jev_answers,_get_val
    answers=_extract_jev_answers(data)
    function=str(_get_val(answers.get('expression_function')) or '')
    base+={'question':12,'argument':8,'experience':4,'venting':-10}.get(function,0)
    base+=float(action.get('signal_value') or action.get('score') or 0)
    from .targeting import poll_window_rules
    window=poll_window_rules()
    if window:
        # Before the poll the undecided are the share that moves; opponents who engage come next.
        stance=(data.get('flow') or {}).get('relative_stance')
        base+=float((window.get('stance_bonus') or {}).get(stance,0))
        if stance=='opposing' and function in window.get('opposing_engaged_functions',()):
            base+=float(window.get('opposing_engaged_bonus',0))
    return base


def _skip_decision(post,reason):
    import uuid
    from ctn.models import Decision,JevAnswers,Tier
    return Decision(uuid.uuid4().hex[:12],post.post_id,Tier.OBSERVE,'bot_default',
        JevAnswers(answers={},raw={'targeting':reason},ok=True),reason,ActionKind.NONE,disposition='skip')


async def ordered_evaluations(loop,tasks):
    from .perceive import post_from_dict
    from .decide import decide
    assessing={};drafting={};ready=[];serial=0
    from . import targeting
    async def assess(task):
        row=await asyncio.to_thread(loop.store.get_observed_post,task['post_id'])
        if not row or row['author'] in loop._own:return None,None,None
        # Cheap checks before any JEV call: cold likely-bot posts and exhausted per-target caps.
        early=await asyncio.to_thread(targeting.skip_reason,loop.store,row)
        if not early:
            early=await asyncio.to_thread(targeting.cap_reason,loop.store,target_author=row['author'],
                root_post_id=targeting.thread_root(row),include_account=False)
        if early:return post_from_dict(row),None,_skip_decision(post_from_dict(row),early)
        if task['draft_json'] and time.time()-task['draft_at']<=300:
            import json
            saved=loop._restore_decision(json.loads(task['draft_json']))
            mode='simulated' if loop.actuator.dry_run else 'live'
            if saved.scores.raw.get('planning_mode',mode)==mode:return None,None,saved
        post=post_from_dict(row)
        if loop.brain and callable(getattr(loop.brain,'assess',None)):
            return post,await loop.brain.assess(post),None
        return post,None,None
    async def draft(task,post,assessment,saved):
        if saved is not None:return saved
        if post is None:return None
        if not await asyncio.to_thread(loop.store.post_task_send_ready,task):return None
        task['draft_at']=time.time()
        if assessment is not None:
            stance=(assessment.to_dict().get('flow') or {}).get('relative_stance')
            row=await asyncio.to_thread(loop.store.get_observed_post,task['post_id'])
            late=await asyncio.to_thread(targeting.skip_reason,loop.store,row or {'post_id':task['post_id']},stance)
            if late:return _skip_decision(post,late)
        if loop.brain:
            kwargs={'assessment':assessment} if assessment is not None else {}
            decision=await loop.brain.decide(post,commit_state=False,**kwargs)
            decision.scores.raw['planning_mode']='simulated' if loop.actuator.dry_run else 'live'
            task['draft_at']=decision.scores.raw.get('draft_created_at',time.time())
            return decision
        return decide(post,side=loop.side,keywords=loop.keywords,rng=loop._rng)
    try:
        for task in tasks:assessing[asyncio.create_task(assess(task))]=task
        while assessing or drafting or ready:
            if ready and len(drafting)<2:
                _,_,task,args=heapq.heappop(ready)
                drafting[asyncio.create_task(draft(task,*args))]=task
                continue
            done,_=await asyncio.wait(set(assessing)|set(drafting),return_when=asyncio.FIRST_COMPLETED)
            # Queue every assessment that completed in this turn before choosing drafts.
            for future in done:
                if future not in assessing:continue
                task=assessing.pop(future)
                try:args=future.result()
                except Exception:
                    # decide() records provider failures and applies the bounded retry policy.
                    args=(post_from_dict(await asyncio.to_thread(loop.store.get_observed_post,task['post_id'])),None,None)
                assessment=args[1]
                jev=assessment_priority(assessment.to_dict() if assessment else {},task)
                audience=float(task.get('audience') or 0)
                priority=jev+audience
                serial+=1
                heapq.heappush(ready,(-priority,serial,task,args))
                with loop.store.batch_transaction():
                    loop.store._conn.execute('UPDATE post_tasks SET jev_score=?,priority=? WHERE post_id=? AND version=?',(jev,priority,task['post_id'],task['version']))
            for future in done:
                if future in drafting:
                    task=drafting.pop(future)
                    yield task,await future
    finally:
        for future in (*assessing,*drafting):future.cancel()
        await asyncio.gather(*assessing,*drafting,return_exceptions=True)
