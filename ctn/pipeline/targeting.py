"""Reply targeting: who is likely to read a reply, and how much of our scarce publish budget it deserves.

Points come only from citizens. A reply's first reader is the author (the platform notifies them);
everyone else is an onlooker whose number follows the post's visibility. Bot status can never be
confirmed, so the author's value is weighted by (1 - p_bot) from local evidence, not filtered by a label.
All numbers here are ordinal weights for ordering work, never probabilities of scoring.
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

POLICY = json.loads((Path(__file__).resolve().parents[2] / 'policies/reply-targeting-v1.json').read_text())
SYDNEY = ZoneInfo('Australia/Sydney')


def sydney_day_start(ts=None):
    now = datetime.fromtimestamp(time.time() if ts is None else ts, SYDNEY)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


LAUNCH = json.loads((Path(__file__).resolve().parents[2] / 'policies/launch-v1.json').read_text())


def launch_rule(store, key):
    """Launch policy value, overridable at runtime with the store policy of the same key.

    Dict rules merge the override over the file defaults, so a partial (or stale) stored value
    never drops a required field.
    """
    value = store.policy(key, LAUNCH[key])
    if isinstance(LAUNCH[key], dict) and isinstance(value, dict):
        return {**LAUNCH[key], **value}
    return value


def posting_frequency(store, author, now=None):
    ts = time.time() if now is None else now
    window = float(launch_rule(store, 'frequency_bot')['window_seconds'])
    with store._lock:
        return store._conn.execute('SELECT COUNT(*) FROM observed_posts WHERE author=? AND created_at>=?', (author, ts - window)).fetchone()[0]


def bot_probability(store, author):
    """Ordering prior that the author is a competitor bot. None for our own accounts."""
    if not author:
        return POLICY['bot_prior']['unknown']
    prof = store.get_bot_profile(author)
    if prof['status'] == 'own_bot':
        return None
    if prof['status'] == 'verified_npc' or prof.get('verified'):
        return 0.0
    p = POLICY['bot_prior'].get(prof['status'], POLICY['bot_prior']['unknown'])
    if int(prof.get('upheld_strikes') or 0) >= 1:
        p = max(p, POLICY['bot_prior']['upheld_author'])
    rule = launch_rule(store, 'frequency_bot')
    # Citizens rarely post several times an hour; a flood of new bot accounts would otherwise cost JEV
    # calls until the detector has gathered evidence. Ordering only: this never feeds a report.
    if p < rule['p_bot'] and posting_frequency(store, author) >= int(rule['min_posts']):
        p = float(rule['p_bot'])
    return p


def engagement(post):
    return int(post.get('likes') or 0) + 2 * int(post.get('replies') or 0) + 2 * int(post.get('reposts') or 0)


def author_reach(store, author, exclude_post_id=''):
    """Average engagement of the author's other observed posts: a visibility prior for brand-new posts."""
    if not author:
        return 0.0
    with store._lock:
        row = store._conn.execute(
            'SELECT AVG(likes+2*replies+2*reposts) FROM (SELECT likes,replies,reposts FROM observed_posts '
            'WHERE author=? AND post_id<>? ORDER BY first_seen_at DESC LIMIT ?)',
            (author, exclude_post_id, POLICY['author_history_posts'])).fetchone()
    return float(row[0] or 0.0)


def is_trending(store, post_id, now=None):
    ts = time.time() if now is None else now
    with store._lock:
        row = store._conn.execute('SELECT seen_at FROM trending_seen WHERE post_id=?', (post_id,)).fetchone()
    return bool(row and row[0] >= ts - POLICY['trending_window_seconds'])


def mark_trending(store, post_ids, now=None):
    ts = time.time() if now is None else now
    ids = [str(p) for p in post_ids if p]
    if not ids:
        return
    with store.batch_transaction():
        store._conn.executemany('INSERT INTO trending_seen(post_id,seen_at) VALUES(?,?) ON CONFLICT(post_id) '
                                'DO UPDATE SET seen_at=excluded.seen_at', [(p, ts) for p in ids])


def replies_to_us(store, post):
    parent = str(post.get('parent_id') or '')
    if not parent:
        return False
    row = store.get_observed_post(parent)
    return bool(row and store.get_bot_profile(row['author'])['status'] == 'own_bot')


def thread_depth(store, post, limit=4):
    """0 for a top-level post, 1 for a reply to it, and so on; a missing ancestor counts as at least 2."""
    depth, parent = 0, str(post.get('parent_id') or '')
    root = str(post.get('root_post_id') or '')
    while parent and depth < limit:
        depth += 1
        if parent == root:
            return depth
        row = store.get_observed_post(parent)
        if row is None:
            return max(depth + 1, 2)
        parent = str(row.get('parent_id') or '')
    return depth


def deep_thread_reason(store, post, now=None):
    """Skip targets deep in a thread (NPCs rarely reply there), unless a citizen is answering us directly."""
    rules = POLICY['thread_depth']
    if thread_depth(store, post) <= int(rules['max_target_depth']):
        return ''
    if replies_to_us(store, post):
        ts = time.time() if now is None else now
        with store._lock:
            ours = store._conn.execute(f"SELECT COUNT(*) FROM action_intents WHERE kind='reply' AND status IN ('claimed','sending','unknown','succeeded') "
                f"AND created_at>=? AND COALESCE(json_extract(metadata_json,'$.root_post_id'),target_id)=? AND {NOT_AMPLIFY}",
                (sydney_day_start(ts), thread_root(post))).fetchone()[0]
        if ours < int(rules['followups_when_replying_to_us']):
            return ''
    return 'targeting:deep_thread'


def post_age(post, now):
    created = float(post.get('created_at') or 0) or float(post.get('first_seen_at') or now)
    return max(0.0, now - created)


def is_cold(store, post, now=None):
    """Old enough to have been seen, still almost no engagement, not trending."""
    ts = time.time() if now is None else now
    cfg = POLICY['cold']
    return (post_age(post, ts) >= cfg['min_age_seconds'] and engagement(post) < cfg['max_engagement']
            and not is_trending(store, post['post_id'], ts))


_PERSUADABLE = re.compile(r"\b(not sure|unsure|undecided|on the fence|can'?t decide|haven'?t decided|torn|"
                          r"what do (you|people) think|convince me|genuine question|honest question|which way|"
                          r"leaning|still deciding|mixed feelings|don'?t know (how|what|whether))\b", re.I)


def persuadable(post):
    """A question or open hesitation: the undecided readers the client brief asks us to find."""
    text = str(post.get('text', ''))
    return bool(_PERSUADABLE.search(text)) or text.rstrip().endswith('?')


def audience(store, post, now=None):
    """Components and an ordinal priority for one observed post (dict as stored in observed_posts)."""
    ts = time.time() if now is None else now
    w = POLICY['weights']
    p_bot = bot_probability(store, post.get('author', ''))
    if p_bot is None:
        return {'own': True, 'priority': -100.0}
    trending = is_trending(store, post['post_id'], ts)
    reach = author_reach(store, post.get('author', ''), post['post_id'])
    onlooker = math.log1p(max(engagement(post), reach))
    to_us = replies_to_us(store, post)
    verified = p_bot == 0.0
    undecided = persuadable(post)
    raw = (w['author_reader'] * (1 - p_bot) + w['onlooker'] * onlooker + w['replies_to_us'] * to_us
           + w['trending'] * trending + w['verified_author'] * verified + w.get('persuadable', 0) * undecided * (1 - p_bot))
    freshness = 0.5 ** (post_age(post, ts) / POLICY['freshness_half_life_seconds'])
    factors = POLICY['thread_depth']['factors']
    depth = thread_depth(store, post)
    raw *= factors[min(depth, len(factors) - 1)]
    return {'own': False, 'depth': depth, 'p_bot': round(p_bot, 3), 'onlooker': round(onlooker, 3), 'reach': round(reach, 2),
            'replies_to_us': to_us, 'trending': trending, 'verified': verified, 'persuadable': undecided, 'freshness': round(freshness, 3),
            'priority': round(raw * freshness, 3)}


def initial_schedule(store, post, now=None):
    """Priority and first eligible time for a new reply task (no JEV call spent yet).

    Likely-bot authors on posts without visible reach wait until the post is old enough to judge,
    so a cold bot post can be dropped without spending JEV, while a post that takes off is still caught.
    """
    ts = time.time() if now is None else now
    info = audience(store, post, ts)
    if info.get('own'):
        return info, ts
    defer = (info['p_bot'] >= POLICY['cold']['min_bot_probability'] and not info['replies_to_us']
             and not info['trending'] and not info['verified']
             and info['reach'] < POLICY['cold']['max_engagement'])
    created = float(post.get('created_at') or 0) or ts
    start = max(ts, created + POLICY['defer_suspected_until_seconds']) if defer else ts
    info['deferred'] = bool(defer)
    return info, start


def suspicion_score(store, author):
    """Heuristic bot detector score for an author; 0.0 for anything we have never profiled."""
    if not author:
        return 0.0
    return float((store.get_bot_profile(author) or {}).get('suspicion_score') or 0.0)


def author_responded(store, author, since):
    """True when the author replied directly to one of our publications after `since`."""
    if not author or not since:
        return False
    with store._lock:
        return store._conn.execute(
            'SELECT 1 FROM observed_posts o JOIN publications p ON p.post_id=o.parent_id '
            'WHERE o.author=? AND o.created_at>? LIMIT 1', (author, float(since))).fetchone() is not None


def skip_reason(store, post, relative_stance=None, now=None):
    """Drop a cold post by a likely bot. Before assessment any stance; after assessment opposing only."""
    ts = time.time() if now is None else now
    deep = deep_thread_reason(store, post, ts)
    if deep:
        return deep
    # A competitor bot never changes its mind, and our reply only raises its reply count (hot score).
    floor = float((POLICY.get('bot_filter') or {}).get('min_suspicion') or 0.0)
    if floor and not replies_to_us(store, post):
        prof = store.get_bot_profile(post.get('author', '')) if post.get('author') else {}
        if prof.get('status') != 'own_bot' and float(prof.get('suspicion_score') or 0.0) >= floor:
            return 'targeting:bot_author'
    p_bot = bot_probability(store, post.get('author', ''))
    if p_bot is None or p_bot < POLICY['cold']['min_bot_probability']:
        return ''
    if relative_stance not in (None, 'opposing'):
        return ''
    if is_cold(store, post, ts) and not replies_to_us(store, post):
        return 'targeting:cold_likely_bot'
    return ''


# Our accounts replying to each other (ctn/amplify.py) do not use up the caps on replying to citizens.
NOT_AMPLIFY = "COALESCE(json_extract(metadata_json,'$.engine'),'')<>'amplify'"


def cap_reason(store, *, target_author='', root_post_id='', account='', now=None, include_account=True):
    """Team-wide anti-pile-on caps (per target author, per thread) and per-account daily volume."""
    ts = time.time() if now is None else now
    caps = POLICY['caps']
    start = sydney_day_start(ts)
    live = "status IN ('claimed','sending','unknown','succeeded')"
    with store._lock:
        if target_author:
            n = store._conn.execute(f"SELECT COUNT(*) FROM action_intents WHERE kind='reply' AND {live} AND created_at>=? "
                                    f"AND json_extract(metadata_json,'$.target_author')=? AND {NOT_AMPLIFY}", (start, target_author)).fetchone()[0]
            if n >= caps['per_author_per_day']:
                return 'author_daily_cap'
            # A citizen just persuaded in a session is less receptive to the same push (organiser note 2026-10-19).
            last = store._conn.execute(f"SELECT MAX(created_at) FROM action_intents WHERE kind='reply' AND {live} "
                                       f"AND json_extract(metadata_json,'$.target_author')=? AND {NOT_AMPLIFY}", (target_author,)).fetchone()[0]
            if last and ts - float(last) < float(caps.get('per_author_min_gap_seconds', 0)):
                return 'author_min_gap'
            if (n >= 1 and caps.get('second_reply_requires_response')
                    and not author_responded(store, target_author, last)):
                return 'author_not_responding'
        if root_post_id:
            n = store._conn.execute(f"SELECT COUNT(*) FROM action_intents WHERE kind='reply' AND {live} AND created_at>=? "
                                    f"AND COALESCE(json_extract(metadata_json,'$.root_post_id'),target_id)=? AND {NOT_AMPLIFY}",
                                    (start, root_post_id)).fetchone()[0]
            if n >= caps['per_thread_per_day']:
                return 'thread_daily_cap'
        if account and include_account:
            n = store._conn.execute(f"SELECT COUNT(*) FROM action_intents WHERE account_id=? AND kind IN ('post','reply') AND {live} "
                                    "AND created_at>=? AND COALESCE(json_extract(metadata_json,'$.purpose'),'') NOT IN ('initialise','warmup')",
                                    (account, start)).fetchone()[0]
            if n >= account_daily_cap(store, account):
                return 'account_daily_cap'
            paced = account_pacing_reason(store, account, ts)
            if paced:
                return paced
    return ''


def account_pacing_reason(store, account, now):
    """Spread an account's daily volume: a fresh account replying every two minutes reads as a bot."""
    rule = launch_rule(store, 'reply_pacing')
    stamps = [r[0] for r in store._conn.execute(
        "SELECT created_at FROM action_intents WHERE account_id=? AND kind IN ('post','reply') "
        "AND status IN ('claimed','sending','unknown','succeeded') AND created_at>=? "
        "AND COALESCE(json_extract(metadata_json,'$.purpose'),'') NOT IN ('initialise','warmup')",
        (account, now - 3600))]
    if stamps and now - max(stamps) < float(rule['min_gap_seconds']):
        return 'account_min_gap'
    if len(stamps) >= int(rule['per_hour']):
        return 'account_hourly_cap'
    return ''


def account_daily_cap(store, account):
    """Accounts with posts hidden by moderators are slowed down so a third hide does not suspend them."""
    slowed = store.policy('slowed_accounts', {}) or {}
    return int(slowed[account]) if account in slowed else int(POLICY['caps']['per_account_per_day'])


def accounts_at_daily_cap(store, accounts, now=None):
    ts = time.time() if now is None else now
    accounts = [a for a in accounts if a]
    if not accounts:
        return set()
    marks = ','.join('?' * len(accounts))
    with store._lock:
        rows = store._conn.execute(
            f"SELECT account_id,COUNT(*) FROM action_intents WHERE account_id IN ({marks}) AND kind IN ('post','reply') "
            "AND status IN ('claimed','sending','unknown','succeeded') AND created_at>=? "
            "AND COALESCE(json_extract(metadata_json,'$.purpose'),'') NOT IN ('initialise','warmup') GROUP BY account_id",
            (*accounts, sydney_day_start(ts))).fetchall()
        capped = {r[0] for r in rows if r[1] >= account_daily_cap(store, r[0])}
        return capped | {a for a in accounts if a not in capped and account_pacing_reason(store, a, ts)}


def thread_root(post):
    return str(post.get('root_post_id') or post.get('parent_id') or post.get('post_id') or '')
