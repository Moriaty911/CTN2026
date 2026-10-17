"""Resolve uncertain post/reply sends only from exact positive history evidence."""
import asyncio
from .storage.sqlite_store import normalize_text

async def reconcile_unknown_posts(actuator,store):
    resolved=0
    for intent in await asyncio.to_thread(store.list_actions,'unknown'):
        if intent['kind'] not in ('post','reply'): continue
        posts=await actuator.get_account_posts(intent['account_id'],limit=50)
        matches=[p for p in posts if p.get('author')==intent['account_id']
            and normalize_text(p.get('text',''))==normalize_text(intent['text'])
            and float(p.get('created_at',0))>=intent['created_at']-5
            and (intent['kind']=='post' or str(p.get('parent_id',''))==intent['target_id'])]
        if len(matches)==1:
            resolved+=bool(await asyncio.to_thread(store.finish_action,intent['id'],'succeeded',
                response=matches[0],detail='reconciled exact author history',now=matches[0]['created_at']))
    return resolved
