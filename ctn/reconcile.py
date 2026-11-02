"""Resolve uncertain post/reply sends (policies/send-recovery-v1.json).

An uncertain send holds its account for a bounded time and blocks its target until resolved.
Evidence, cheapest first: our own observed_posts, the target thread (replies), the account's
history. "Absent" needs complete coverage back to the send time on two checks, the last one at
least `absent_min_age_seconds` after the send; anything less keeps waiting, and the store
abandons the send (account freed, target never retried) after `abandon_after_seconds`.
"""
import asyncio
import json
import time

from .storage.runtime import SEND_RECOVERY
from .storage.sqlite_store import normalize_text


def _matches(row, intent, since):
    if not isinstance(row, dict):return False
    if str(row.get('author', '')) != intent['account_id']:return False
    if normalize_text(row.get('text', '')) != normalize_text(intent['text']):return False
    if float(row.get('created_at') or 0) < since:return False
    return intent['kind'] == 'post' or str(row.get('parent_id', '')) == intent['target_id']


def _local_match(store, intent, since):
    with store._lock:
        rows = store._conn.execute('SELECT * FROM observed_posts WHERE author=? AND created_at>=? ORDER BY created_at LIMIT 200',
                                   (intent['account_id'], since)).fetchall()
    found = [dict(r) for r in rows if _matches(dict(r), intent, since)]
    return found[0] if len(found) == 1 else None


async def _scan(fetch, intent, since, max_pages):
    """Page through one source. Returns (match, complete); complete means the send time is covered."""
    cursor = None
    for _ in range(max_pages):
        page = await fetch(cursor)
        if getattr(page, 'read_failed', False):return None, False
        rows = [r for r in page if isinstance(r, dict)]
        hits = [r for r in rows if _matches(r, intent, since)]
        if len(hits) == 1:return hits[0], True
        if len(hits) > 1:return None, False          # ambiguous: never guess
        stamps = [float(r.get('created_at') or 0) for r in rows]
        cursor = getattr(page, 'next_cursor', None)
        if not cursor:return None, True              # whole list read
        if stamps and stamps[0] >= stamps[-1] and stamps[-1] < since:
            return None, True                        # newest-first and already past the send time
    return None, False


async def check_unknown(actuator, store, intent, now=None):
    """One reconciliation attempt; returns 'succeeded', 'failed' or 'wait'."""
    ts = time.time() if now is None else float(now)
    since = float(intent['wire_at'] or intent['created_at']) - 5
    max_pages = int(SEND_RECOVERY['max_pages'])
    match = await asyncio.to_thread(_local_match, store, intent, since)
    complete = False
    sources = []
    if match is None and intent['kind'] == 'reply':
        match, complete = await _scan(lambda c: actuator.get_comments(intent['target_id'], limit=50, cursor=c), intent, since, max_pages)
        sources.append('thread' + ('' if complete else '(partial)'))
    if match is None and not (intent['kind'] == 'reply' and complete):
        match, history_complete = await _scan(lambda c: actuator.get_account_posts(intent['account_id'], limit=50, cursor=c), intent, since, max_pages)
        sources.append('history' + ('' if history_complete else '(partial)'))
        # Whether account history lists replies is unverified, so it only proves absence for posts.
        if intent['kind'] == 'post':complete = history_complete
    if match is not None:
        post_id = match.get('post_id') or match.get('id')
        changed = await asyncio.to_thread(store.finish_action, intent['action_id'], 'succeeded',
                                          response={**match, 'post_id': str(post_id)}, detail='reconciled: found our post',
                                          now=float(match.get('created_at') or ts))
        await asyncio.to_thread(store.record_unknown_check, intent['action_id'], 'found', complete=True, detail='found', now=ts)
        return 'succeeded' if changed else 'wait'
    verdict = await asyncio.to_thread(store.record_unknown_check, intent['action_id'], 'absent' if complete else 'incomplete',
                                      complete=complete, detail=','.join(sources) or 'local', now=ts)
    if verdict == 'fail':
        await asyncio.to_thread(store.finish_action, intent['action_id'], 'failed',
                                detail='reconciled_absent: not found with full coverage on repeated checks')
        return 'failed'
    return 'wait'


async def reconcile_unknown_posts(actuator, store, *, now=None):
    resolved = 0
    for intent in await asyncio.to_thread(store.due_unknown_checks, now=now, limit=int(SEND_RECOVERY['checks_per_step'])):
        outcome = await check_unknown(actuator, store, intent, now=now)
        resolved += outcome in ('succeeded', 'failed')
    await asyncio.to_thread(alert_unknown_burst, store, now=now)
    return resolved


def alert_unknown_burst(store, *, now=None):
    """Many accounts going uncertain at once usually means the platform is failing; tell the operators once."""
    ts = time.time() if now is None else float(now)
    window = float(SEND_RECOVERY['alert_window_seconds'])
    with store._lock:
        accounts = store._conn.execute('SELECT COUNT(DISTINCT a.account_id) FROM unknown_checks c JOIN action_intents a ON a.id=c.action_id '
                                       'WHERE c.unknown_at>=?', (ts - window,)).fetchone()[0]
    if accounts >= int(SEND_RECOVERY['alert_accounts']):
        bucket = int(ts // window)
        with store.batch_transaction():
            store.notify_discord(f'unknown_burst:{bucket}', {'type': 'alert', 'kind': 'unknown_burst', 'accounts': accounts,
                                                              'window_seconds': window, 'at': ts})
        return accounts
    return 0
