"""Bounded context acquisition for material waiters through the shared read budget."""
import asyncio
import time


async def hydrate_waiting_context(actuator,store):
    from .perceive import post_from_dict
    with store._lock:
        rows=store._conn.execute("SELECT post_id FROM post_tasks WHERE status='waiting_materials' AND expires_at>? ORDER BY priority DESC LIMIT 8",(time.time(),)).fetchall()
    remaining=2
    for row in rows:
        target=store.get_observed_post(row['post_id'])
        if not target:continue
        ids=[target['post_id']]+[str(target.get(k) or (target.get('raw') or {}).get(k) or '') for k in ('parent_id','quoted_post_id')]
        for ident in ids:
            if not ident:continue
            cached=store.get_observed_post(ident)
            if cached and (cached.get('raw') or {}).get('text_complete',True):continue
            key='context_backfill:'+ident
            if store.policy(key,0)>time.time()-30:continue
            store.set_policy(key,time.time())
            full=await actuator.get_post(ident);remaining-=1
            if full and str(full.get('post_id'))==ident:
                full={**full,'text_complete':full.get('text_complete',True)}
                post=post_from_dict(full)
                if post:await asyncio.to_thread(store.upsert_observed_post,post)
            if remaining<=0:return
