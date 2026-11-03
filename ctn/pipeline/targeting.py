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


def poll_window_rules(now=None):
    """The poll-window rules (policies/reply-targeting-v1.json `poll_window`) while they apply, else None."""
    from datetime import datetime
    rules = POLICY.get('poll_window') or {}
    until = rules.get('until')
    if not until:
        return None
    ts = time.time() if now is None else float(now)
    return rules if ts < datetime.fromisoformat(until).timestamp() else None


def citizen_max_id():
    return int((POLICY.get('citizens') or {}).get('max_account_id') or 0)


def is_known_citizen(store, author, author_id=0):
    """True for an account registered before the platform opened (id <= citizens.max_account_id).

    Competitor accounts can only be created after the open, so these are simulated citizens: the only
    authors whose replies, likes and changes of mind score. Our own accounts are never citizens.
    """
    limit = citizen_max_id()
    if not limit or not author:
        return False
    try:
        aid = int(author_id or 0)
    except (TypeError, ValueError):
        aid = 0
    if aid <= 0:
        aid = int(store.resolve_platform_account_id(author) or 0)
    if not 0 < aid <= limit:
        return False
    return store.get_bot_profile(author)['status'] != 'own_bot'


def bot_probability(store, author, author_id=0):
    """Ordering prior that the author is a competitor bot. None for our own accounts."""
    if not author:
        return POLICY['bot_prior']['unknown']
    prof = store.get_bot_profile(author)
    if prof['status'] == 'own_bot':
        return None
    if prof['status'] == 'verified_npc' or prof.get('verified'):
        return 0.0
    if is_known_citizen(store, author, author_id):
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
    """The post answers one of our posts, whoever wrote it."""
    parent = str(post.get('parent_id') or '')
    if not parent:
        return False
    row = store.get_observed_post(parent)
    return bool(row and store.get_bot_profile(row['author'])['status'] == 'own_bot')


def responder_counts(store, author, author_id=0):
    """Whether an answer from this author is worth rewarding (ordering bonus, follow-ups, a second reply).

    2026-10-26 (48 h of replies): competitor bots answered 16-23% of our replies and citizens almost never
    answered our replies to non-citizens (1.1%), yet any answer to us raised priority by 15 and unlocked a
    second reply, so we kept talking to bots. A known citizen always counts; any other author counts only
    while its bot prior stays below citizens.responder_max_bot_probability (unless count_newcomers is off).
    """
    if not author:
        return False
    if is_known_citizen(store, author, author_id):
        return True
    rules = POLICY.get('citizens') or {}
    if not rules.get('count_newcomers', True):
        return False
    p_bot = bot_probability(store, author, author_id)
    return p_bot is not None and p_bot < float(rules.get('responder_max_bot_probability', 1.0))


def counted_reply_to_us(store, post):
    """A reply to us from an author whose answer we reward (see responder_counts)."""
    return replies_to_us(store, post) and responder_counts(store, post.get('author', ''), post.get('author_id', 0))


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


# Accounts registered before the competition opened are citizens; everything above is a player.
CITIZEN_MAX_ID = 2343


def deep_thread_reason(store, post, now=None):
    """Skip targets deep in a thread (NPCs rarely reply there), unless a citizen is answering us directly."""
    rules = POLICY['thread_depth']
    citizen = 1 <= int(post.get('author_id') or 0) <= CITIZEN_MAX_ID
    limit = int(rules.get('citizen_max_target_depth', rules['max_target_depth'])) if citizen else int(rules['max_target_depth'])
    if thread_depth(store, post) <= limit:
        return ''
    if counted_reply_to_us(store, post):
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


_CONCERN = re.compile(r"\b(not against|not anti|not opposed|personal choice|my body|bodily autonomy|"
                      r"exempt\w*|fine print|the details|as long as|if the (rule|plan|policy|law)|"
                      r"show us|show me|spell (it|this) out|works? out how)\b", re.I)


def persuadable(post):
    """A question, open hesitation or an objection that leaves room: the undecided readers the brief asks us to find."""
    text = str(post.get('text', ''))
    return bool(_PERSUADABLE.search(text)) or bool(_CONCERN.search(text)) or text.rstrip().endswith('?')


def concerned(post):
    """Objecting but not closed off: 'I'm not against vaccines, I'm against how this rule works'."""
    return bool(_CONCERN.search(str(post.get('text') or '')))


def audience(store, post, now=None):
    """Components and an ordinal priority for one observed post (dict as stored in observed_posts)."""
    ts = time.time() if now is None else now
    w = POLICY['weights']
    author, author_id = post.get('author', ''), post.get('author_id', 0)
    p_bot = bot_probability(store, author, author_id)
    if p_bot is None:
        return {'own': True, 'priority': -100.0}
    trending = is_trending(store, post['post_id'], ts)
    reach = author_reach(store, author, post['post_id'])
    onlooker = math.log1p(max(engagement(post), reach))
    citizen = is_known_citizen(store, author, author_id)
    to_us = replies_to_us(store, post) and responder_counts(store, author, author_id)
    prof = store.get_bot_profile(author) if author else {}
    verified = bool(prof.get('status') == 'verified_npc' or prof.get('verified'))
    undecided = persuadable(post)
    raw = (w['author_reader'] * (1 - p_bot) + w['onlooker'] * onlooker + w['replies_to_us'] * to_us
           + w['trending'] * trending + w['verified_author'] * verified + w.get('persuadable', 0) * undecided * (1 - p_bot)
           + w.get('known_citizen', 0) * citizen)
    freshness = 0.5 ** (post_age(post, ts) / POLICY['freshness_half_life_seconds'])
    factors = POLICY['thread_depth']['factors']
    depth = thread_depth(store, post)
    raw *= factors[min(depth, len(factors) - 1)]
    return {'own': False, 'depth': depth, 'p_bot': round(p_bot, 3), 'onlooker': round(onlooker, 3), 'reach': round(reach, 2),
            'replies_to_us': to_us, 'citizen': citizen, 'trending': trending, 'verified': verified, 'persuadable': undecided, 'freshness': round(freshness, 3),
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
    if not responder_counts(store, author):
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
    citizen = is_known_citizen(store, post.get('author', ''), post.get('author_id', 0))
    # A detector score never filters a citizen; a bot answering us no longer buys its way past the filter.
    if floor and not citizen and not counted_reply_to_us(store, post):
        prof = store.get_bot_profile(post.get('author', '')) if post.get('author') else {}
        if prof.get('status') != 'own_bot' and float(prof.get('suspicion_score') or 0.0) >= floor:
            return 'targeting:bot_author'
    p_bot = bot_probability(store, post.get('author', ''), post.get('author_id', 0))
    if p_bot is None or p_bot < POLICY['cold']['min_bot_probability']:
        return ''
    if relative_stance not in (None, 'opposing'):
        return ''
    if is_cold(store, post, ts) and not counted_reply_to_us(store, post):
        return 'targeting:cold_likely_bot'
    return ''


# Our accounts replying to each other (ctn/amplify.py) do not use up the caps on replying to citizens.
NOT_AMPLIFY = "COALESCE(json_extract(metadata_json,'$.engine'),'')<>'amplify'"


def is_verified_author(store, author):
    """True for the platform's own verified accounts (newspapers, campaign leads, moderators)."""
    if not author:
        return False
    try:
        prof = store.get_bot_profile(author)
    except Exception:
        return False
    return bool(prof.get('status') == 'verified_npc' or prof.get('verified'))


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
            # Verified accounts answer more often and carry the argument; they get a slightly wider budget (2026-10-28).
            verified_author = is_verified_author(store, target_author)
            per_author = int(caps.get('verified_per_author_per_day', caps['per_author_per_day'])) if verified_author else int(caps['per_author_per_day'])
            min_gap = float(caps.get('verified_author_min_gap_seconds', caps.get('per_author_min_gap_seconds', 0))) if verified_author else float(caps.get('per_author_min_gap_seconds', 0))
            if n >= per_author:
                return 'author_daily_cap'
            # A citizen just persuaded in a session is less receptive to the same push (organiser note 2026-10-19).
            last = store._conn.execute(f"SELECT MAX(created_at) FROM action_intents WHERE kind='reply' AND {live} "
                                       f"AND json_extract(metadata_json,'$.target_author')=? AND {NOT_AMPLIFY}", (target_author,)).fetchone()[0]
            if last and ts - float(last) < min_gap:
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
            # Several of our accounts landing in one thread within minutes is what a coordination detector sees.
            last_thread = store._conn.execute(f"SELECT MAX(created_at) FROM action_intents WHERE kind='reply' AND {live} "
                                              f"AND COALESCE(json_extract(metadata_json,'$.root_post_id'),target_id)=? AND {NOT_AMPLIFY}",
                                              (root_post_id,)).fetchone()[0]
            if last_thread and ts - float(last_thread) < float(caps.get('per_thread_min_gap_seconds', 0)):
                return 'thread_min_gap'
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


def pacing_rule(store, account, now):
    """This account's cadence right now: tighter for its first hours, because a brand-new account at adult
    volume is what gets it looked at. 2026-10-27: no account had ever been suspended within 37 h of its first
    post, so this is precaution, not a measured effect."""
    rule = launch_rule(store, 'reply_pacing')
    warm = launch_rule(store, 'warmup') or {}
    hours = float(warm.get('hours') or 0)
    if hours > 0:
        with store._lock:
            row = store._conn.execute('SELECT initialised_at FROM account_lifecycle WHERE account_id=?', (account,)).fetchone()
        started = float(row[0]) if row and row[0] else 0.0
        if started and now - started < hours * 3600:
            return {'per_hour': int(warm.get('per_hour') or rule['per_hour']),
                    'min_gap_seconds': int(warm.get('min_gap_seconds') or rule['min_gap_seconds'])}
    return rule


def account_pacing_reason(store, account, now):
    """Spread an account's daily volume: a fresh account replying every two minutes reads as a bot."""
    rule = pacing_rule(store, account, now)
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
