"""Watch ourselves the way other teams' detectors will (policies/launch-v1.json).

* `self_scan`: score each of our accounts on the signals bot detectors use: AI leak phrases, wording
  shared with another of our accounts, several of our accounts in one thread within minutes, and volume.
* `own_post_check`: re-read our recent posts; a post that reports a hidden/removed state, or that cannot
  be read twice in a row, counts as hidden. An account with `hidden_slowdown.hidden_posts` hidden posts
  gets a lower daily cap, because three permanently hidden posts suspend an account.
Both raise Discord alerts once and are visible in `ops status` (`self_check`).
"""
from __future__ import annotations

import json
import time

from ctn.pipeline.targeting import launch_rule

_HIDDEN_FLAGS = ('hidden', 'is_hidden', 'removed', 'deleted', 'suppressed')
_HIDDEN_STATES = {'hidden', 'removed', 'deleted', 'temporarily_hidden', 'permanently_hidden', 'suppressed', 'moderated'}

SCHEMA = '''CREATE TABLE IF NOT EXISTS own_post_checks(post_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'visible',
 misses INTEGER NOT NULL DEFAULT 0,last_checked REAL NOT NULL DEFAULT 0,fields_json TEXT NOT NULL DEFAULT '{}')'''


def _ensure(store):
    with store.batch_transaction():
        store._conn.execute(SCHEMA)


def our_accounts(store):
    with store._lock:
        bound = {r[0] for r in store._conn.execute('SELECT account_id FROM persona_bindings')}
        own = {r[0] for r in store._conn.execute("SELECT account_id FROM bot_registry WHERE status='own_bot'")}
    return bound | own


def self_scan(store, *, now=None):
    from ctn.moderation.detector import _AI_LEAK_RE
    from ctn.rag.corpus import too_close
    ts = time.time() if now is None else now
    rule = launch_rule(store, 'self_check')
    with store._lock:
        rows = [dict(r) for r in store._conn.execute(
            "SELECT account_id,target_id,text,created_at,metadata_json FROM action_intents WHERE kind IN ('post','reply') "
            "AND status IN ('sending','unknown','succeeded') AND created_at>=? ORDER BY created_at", (ts - rule['lookback_seconds'],))]
    by_account = {}
    for r in rows:
        meta = json.loads(r['metadata_json'] or '{}')
        if meta.get('purpose') in ('initialise', 'warmup'):continue
        r['root'] = str(meta.get('root_post_id') or r['target_id'] or '')
        by_account.setdefault(r['account_id'], []).append(r)
    report = {}
    for account, mine in by_account.items():
        others = [r for a, rs in by_account.items() if a != account for r in rs]
        leaks = sum(bool(_AI_LEAK_RE.search(r['text'] or '')) for r in mine)
        shared = sum(too_close(r['text'] or '', [o['text'] for o in others], threshold=.6) for r in mine)
        crowded = sum(any(o['root'] and o['root'] == r['root'] and abs(o['created_at'] - r['created_at']) <= rule['thread_window_seconds']
                          for o in others) for r in mine)
        volume = len(mine) > 30
        score = 3 * leaks + 2 * shared + crowded + int(volume)
        if score:
            report[account] = {'score': score, 'leak_phrases': leaks, 'shared_wording': shared, 'crowded_threads': crowded, 'high_volume': volume}
    flagged = {a: v for a, v in report.items() if v['score'] >= rule['alert_score']}
    day = time.strftime('%Y-%m-%d', time.gmtime(ts))
    with store.batch_transaction():
        store.set_policy('self_check', {'at': ts, 'accounts': report})
        for account, detail in flagged.items():
            store.notify_discord(f'self_check:{day}:{account}', {'type': 'alert', 'kind': 'self_check', 'account_id': account, 'detail': detail})
    return report


def _hidden(raw):
    source = raw.get('raw') if isinstance(raw.get('raw'), dict) else raw
    flags = {k: source[k] for k in source if k.lower() in _HIDDEN_FLAGS and source[k]}
    states = {k: source[k] for k in source if k.lower() in ('status', 'state', 'moderation_status', 'visibility')
              and str(source[k]).lower() in _HIDDEN_STATES}
    return {**flags, **states}


async def own_post_check(actuator, store, *, now=None):
    import asyncio
    ts = time.time() if now is None else now
    rule = launch_rule(store, 'own_posts')
    await asyncio.to_thread(_ensure, store)
    with store._lock:
        rows = [dict(r) for r in store._conn.execute(
            "SELECT p.post_id,p.account_id,COALESCE(c.last_checked,0) AS last_checked,COALESCE(c.misses,0) AS misses,COALESCE(c.state,'visible') AS state "
            "FROM publications p LEFT JOIN own_post_checks c ON c.post_id=p.post_id WHERE p.post_id IS NOT NULL AND p.published_at>=? "
            "AND COALESCE(c.state,'visible')<>'hidden' AND COALESCE(c.last_checked,0)<=? ORDER BY COALESCE(c.last_checked,0) LIMIT ?",
            (ts - rule['lookback_seconds'], ts - rule['recheck_seconds'], int(rule['checks_per_step'])))]
    changed = []
    for row in rows:
        raw = await actuator.get_post(row['post_id'])
        found = bool(raw) and str(raw.get('post_id', '')) == str(row['post_id'])
        fields = _hidden(raw) if found else {}
        misses = 0 if found else row['misses'] + 1
        state = 'hidden' if fields or misses >= int(rule['missing_confirmations']) else 'visible'
        def save():
            with store.batch_transaction():
                store._conn.execute('INSERT INTO own_post_checks(post_id,account_id,state,misses,last_checked,fields_json) VALUES(?,?,?,?,?,?) '
                                    'ON CONFLICT(post_id) DO UPDATE SET state=excluded.state,misses=excluded.misses,last_checked=excluded.last_checked,'
                                    'fields_json=excluded.fields_json', (row['post_id'], row['account_id'], state, misses, ts,
                                                                         json.dumps(fields or ({'unreadable': misses} if misses else {}), default=str)))
                if state == 'hidden':
                    store.notify_discord(f"own_hidden:{row['post_id']}", {'type': 'alert', 'kind': 'own_post_hidden', 'account_id': row['account_id'],
                                                                          'post_id': row['post_id'], 'fields': fields or {'unreadable': misses}})
        await asyncio.to_thread(save)
        if state == 'hidden':changed.append(row['account_id'])
    if changed:await asyncio.to_thread(apply_slowdown, store)
    return len(rows)


def apply_slowdown(store):
    rule = launch_rule(store, 'hidden_slowdown')
    with store._lock:
        counts = dict(store._conn.execute("SELECT account_id,COUNT(*) FROM own_post_checks WHERE state='hidden' GROUP BY account_id").fetchall())
    slowed = dict(store.policy('slowed_accounts', {}) or {})
    with store.batch_transaction():
        for account, n in counts.items():
            if n >= int(rule['hidden_posts']) and account not in slowed:
                slowed[account] = int(rule['daily_cap'])
                store.notify_discord(f'slowed:{account}', {'type': 'alert', 'kind': 'account_slowed', 'account_id': account, 'hidden_posts': n,
                                                           'daily_cap': int(rule['daily_cap'])})
        store.set_policy('slowed_accounts', slowed)
    return slowed


async def selfcheck_step(actuator, store, *, now=None):
    """Worker entry: scan every `self_check.every_seconds`, check our posts every `own_posts.every_seconds`."""
    import asyncio
    ts = time.time() if now is None else now
    done = {}
    if ts - float(store.policy('selfcheck:scan_at', 0)) >= launch_rule(store, 'self_check')['every_seconds']:
        store.set_policy('selfcheck:scan_at', ts)
        done['scanned'] = len(await asyncio.to_thread(self_scan, store, now=ts))
    if ts - float(store.policy('selfcheck:posts_at', 0)) >= launch_rule(store, 'own_posts')['every_seconds']:
        store.set_policy('selfcheck:posts_at', ts)
        done['checked'] = await own_post_check(actuator, store, now=ts)
    return done
