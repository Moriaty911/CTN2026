"""Blacklist reporting (policies/blacklist-v1.json).

Operator decision 2026-10-19: accounts proven to spam go into their own table, and every post of theirs we
see is reported, one after another, with no daily cap and regardless of team or side. The legacy
evidence/expected-value reporter is paused meanwhile; revisit once the first rulings are in.

An account is added automatically when it
  - posts the same long text again and again, or shares one long text word for word with many new accounts;
  - belongs to a reply ring: threads with 20+ distinct repliers arriving a median ~20 s apart (the
    platform's 3 posts a minute for one team), the repliers seen in several such threads and the authors
    whose posts are flooded this way;
  - leaves language-model output in a post ("Sure, here's a draft", "As an AI").
or by an operator (`ops blacklist add/import`). Accounts registered before the competition opened
(id <= npc_max_account_id), verified accounts and our own accounts are never added: a citizen ruled
genuine stops all our reporting for six hours.

The reporter sends at most one post report per step while the team's post-report route (3 a minute) has no
backlog, newest posts first, stops during a reporting suspension and backs off after any report 403. Once a
reported post of an account has disappeared (likely hidden by the moderator) and the account shows 100+
actions, the account itself is reported (15 points per reporting team if suspended; a dismissed account
report costs nothing and can be repeated later).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
import json
from pathlib import Path
import re
import statistics
import time
import uuid

POLICY = json.loads((Path(__file__).resolve().parents[1] / 'policies/blacklist-v1.json').read_text())

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_blacklist(author_id INTEGER PRIMARY KEY,username TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL,evidence_json TEXT NOT NULL DEFAULT '{}',cluster TEXT NOT NULL DEFAULT '',source TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active',added_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS blacklist_post_checks(post_id TEXT PRIMARY KEY,author_id INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'visible',checked_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS blacklist_account_reports(author_id INTEGER PRIMARY KEY,reported_at REAL NOT NULL,
 status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '');
"""
REASONS = ('copy_paste', 'shared_text', 'ring', 'ring_anchor', 'leak', 'operator')


def ensure_schema(store):
    # executescript would commit an open transaction; run statements one by one inside ours.
    with store.batch_transaction():
        for statement in filter(str.strip, SCHEMA.split(';')):
            store._conn.execute(statement)


def _id(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def protected_reason(store, author_id, username='', policy=POLICY):
    """Why this account must never be blacklisted, or ''."""
    aid = _id(author_id)
    if aid is None:
        return 'unknown_id'
    if aid <= int(policy['npc_max_account_id']):
        return 'registered_before_open'
    with store._lock:
        ours = store._conn.execute('SELECT 1 FROM managed_accounts WHERE platform_account_id=? OR username=? LIMIT 1',
                                   (aid, username or '\x00')).fetchone()
    if ours:
        return 'own_account'
    for name in {str(aid), username} - {''}:
        prof = store.get_bot_profile(name)
        if prof.get('verified') or prof['status'] in ('own_bot', 'verified_npc'):
            return 'verified_or_own'
    return ''


def add(store, author_id, *, reason, username='', evidence=None, cluster='', source='auto', now=None, policy=POLICY):
    """Blacklist an account. Returns (added, why). An existing active entry is kept; a removed one stays removed
    unless an operator adds it again."""
    if reason not in REASONS:
        raise ValueError('unknown blacklist reason')
    blocked = protected_reason(store, author_id, username, policy)
    if blocked:
        return False, blocked
    ts = time.time() if now is None else now
    aid = _id(author_id)
    with store.batch_transaction():
        row = store._conn.execute('SELECT status FROM report_blacklist WHERE author_id=?', (aid,)).fetchone()
        if row and (row['status'] == 'active' or source != 'operator'):
            return False, 'already_' + row['status']
        store._conn.execute('INSERT INTO report_blacklist VALUES(?,?,?,?,?,?,\'active\',?,?) ON CONFLICT(author_id) DO UPDATE SET '
                            "status='active',reason=excluded.reason,evidence_json=excluded.evidence_json,cluster=excluded.cluster,"
                            'source=excluded.source,username=excluded.username,updated_at=excluded.updated_at',
                            (aid, username or '', reason, json.dumps(evidence or {}, ensure_ascii=False)[:4000], cluster, source, ts, ts))
    return True, 'added'


def remove(store, author_id, *, now=None):
    ts = time.time() if now is None else now
    with store.batch_transaction():
        return store._conn.execute("UPDATE report_blacklist SET status='removed',updated_at=? WHERE author_id=? AND status='active'",
                                   (ts, _id(author_id))).rowcount == 1


def active(store):
    with store._lock:
        return {r['author_id']: dict(r) for r in store._conn.execute("SELECT * FROM report_blacklist WHERE status='active'")}


def import_intel(store, path, *, now=None):
    """Import the high-confidence sections (anchors, ring repliers, ring likers) of docs/intel/accounts-*.md."""
    added, skipped, section = [], [], ''
    for line in Path(path).read_text().splitlines():
        if line.startswith('### '):
            section = line
            continue
        if not section.startswith(('### 1.', '### 2.', '### 3.')):
            continue
        m = re.match(r'\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|', line)
        if not m:
            continue
        ok, why = add(store, m.group(1), username=m.group(2), reason='operator', source='operator', cluster='intel:ring-2356',
                      evidence={'source': str(path), 'section': section[4:60]}, now=now)
        (added if ok else skipped).append((int(m.group(1)), why))
    return {'added': len(added), 'skipped': skipped}


def _recent_posts(store, since):
    with store._lock:
        return [dict(r) for r in store._conn.execute(
            'SELECT post_id,author,author_id,text,norm_hash,parent_id,root_post_id,created_at,first_seen_at FROM observed_posts '
            'WHERE COALESCE(NULLIF(created_at,0),first_seen_at)>=? AND author_id IS NOT NULL', (since,))]


def scan(store, *, now=None, policy=POLICY):
    """Find spam accounts in the recent mirror and blacklist them. Returns [(author_id, reason)] newly added."""
    ts = time.time() if now is None else now
    posts = _recent_posts(store, ts - float(policy['scan_lookback_seconds']))
    found = []

    def put(aid, reason, username, evidence, cluster=''):
        ok, _ = add(store, aid, reason=reason, username=username, evidence=evidence, cluster=cluster, now=ts, policy=policy)
        if ok:
            found.append((int(aid), reason))

    names = {}
    for p in posts:
        names[_id(p['author_id'])] = str(p['author'] or '')
    # 1. One long text again and again, or shared word for word by many new accounts.
    cp = policy['copy_paste']
    by_hash = defaultdict(list)
    for p in posts:
        if p['norm_hash'] and len((p['text'] or '').strip()) >= int(cp['min_chars']):
            by_hash[p['norm_hash']].append(p)
    for group in by_hash.values():
        per_author = defaultdict(list)
        for p in group:
            per_author[_id(p['author_id'])].append(p['post_id'])
        for aid, ids in per_author.items():
            if len(ids) >= int(cp['min_repeats']):
                put(aid, 'copy_paste', names.get(aid, ''), {'n': len(ids), 'post_ids': ids[:10]})
        newcomers = [a for a in per_author if a is not None and a > int(policy['npc_max_account_id'])]
        # Identical small talk across new accounts may be the simulator's canned citizen lines; a text any pre-open
        # account also posted is never evidence, and only campaign lines count.
        citizen_line = any(a is None or a <= int(policy['npc_max_account_id']) for a in per_author)
        campaign_line = re.search(cp['shared_text_stance_pattern'], group[0]['text'] or '', re.I)
        if len(newcomers) >= int(cp['min_accounts_for_shared_text']) and campaign_line and not citizen_line:
            for aid in newcomers:
                put(aid, 'shared_text', names.get(aid, ''), {'n': len(newcomers), 'post_ids': per_author[aid][:5]},
                    cluster='text:' + str(group[0]['norm_hash'])[:12])
    # 2. Reply rings: many distinct repliers on one thread, arriving every ~20 seconds.
    rg = policy['ring']
    replies = defaultdict(list)
    for p in posts:
        if p['parent_id']:
            replies[str(p['parent_id'])].append(p)
    by_id = {str(p['post_id']): p for p in posts}
    ring_posts, member_hits, anchor_hits = [], defaultdict(set), defaultdict(set)
    for parent, rows in replies.items():
        authors = {_id(r['author_id']) for r in rows}
        if len(authors) < int(rg['min_repliers']):
            continue
        stamps = sorted(float(r['created_at'] or r['first_seen_at'] or 0) for r in rows)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        if not gaps or statistics.median(gaps) > float(rg['max_median_gap_seconds']):
            continue
        ring_posts.append(parent)
        for aid in authors:
            member_hits[aid].add(parent)
        if parent in by_id:
            anchor_hits[_id(by_id[parent]['author_id'])].add(parent)
    for aid, hits in member_hits.items():
        if len(hits) >= int(rg['min_ring_posts_per_replier']):
            put(aid, 'ring', names.get(aid, ''), {'n': len(hits), 'threads': sorted(hits)[:10]}, cluster='ring')
    for aid, hits in anchor_hits.items():
        if len(hits) >= int(rg['min_ring_posts_per_anchor']):
            put(aid, 'ring_anchor', names.get(aid, ''), {'n': len(hits), 'posts': sorted(hits)[:10]}, cluster='ring')
    # 3. Language-model output left in a post.
    patterns = [re.compile(x, re.I) for x in policy['leak_patterns']]
    for p in posts:
        text = (p['text'] or '').replace('’', "'")
        for pat in patterns:
            m = pat.search(text)
            if m:
                put(p['author_id'], 'leak', names.get(_id(p['author_id']), ''), {'post_id': p['post_id'], 'match': m.group(0)[:60]})
                break
    return found


def detail_for(entry, policy=POLICY):
    """Report detail text: the official opening plus the concrete evidence for this account."""
    evidence = json.loads(entry.get('evidence_json') or '{}')
    template = policy['report_detail'][entry['reason']]
    text = template.format(n=evidence.get('n', 'several'), match=evidence.get('match', ''))
    return (policy['report_detail']['prefix'] + text)[:900]


def _author_rules(policy):
    rules = policy['report']
    return (int(rules.get('max_post_reports_per_author', 1)) + int(rules.get('author_retry_limit', 1)),
            float(rules.get('author_retry_after_seconds', 86400)))


def pending_targets(store, limit=20, *, policy=POLICY, now=None):
    """Visible posts by active blacklisted authors that we may report right now, newest first.

    One author is reported once; further posts stay in the pool. A single retry is allowed
    `author_retry_after_seconds` after the previous report, because we cannot see dismissals.
    """
    ts = time.time() if now is None else float(now)
    cap, retry_after = _author_rules(policy)
    with store._lock:
        return [dict(r) for r in store._conn.execute(
            "SELECT o.post_id,o.author_id,o.text,COALESCE(NULLIF(o.created_at,0),o.first_seen_at) at FROM observed_posts o "
            "JOIN report_blacklist b ON b.author_id=o.author_id AND b.status='active' "
            "WHERE NOT EXISTS(SELECT 1 FROM action_intents a WHERE a.kind='report' AND a.target_id=o.post_id AND a.status!='failed') "
            "AND NOT EXISTS(SELECT 1 FROM blacklist_post_checks c WHERE c.post_id=o.post_id AND c.state='gone') "
            "AND (SELECT COUNT(*) FROM action_intents a WHERE a.kind='report' AND a.status!='failed' "
            "     AND CAST(json_extract(a.metadata_json,'$.target_author') AS TEXT)=CAST(o.author_id AS TEXT))<? "
            "AND NOT EXISTS(SELECT 1 FROM action_intents a WHERE a.kind='report' AND a.status!='failed' "
            "     AND CAST(json_extract(a.metadata_json,'$.target_author') AS TEXT)=CAST(o.author_id AS TEXT) AND a.created_at>?) "
            "AND NOT EXISTS(SELECT 1 FROM blacklist_account_reports r WHERE r.author_id=o.author_id AND r.reported_at>?) "
            'ORDER BY at DESC LIMIT ?', (cap, ts - retry_after, ts - retry_after, int(limit)))]


def pool_summary(store, *, policy=POLICY, now=None):
    """How much evidence is waiting: one report per author, everything else sits in the pool."""
    ensure_schema(store)
    ts = time.time() if now is None else float(now)
    cap, _ = _author_rules(policy)
    with store._lock:
        waiting = store._conn.execute(
            "SELECT COUNT(*) FROM observed_posts o JOIN report_blacklist b ON b.author_id=o.author_id AND b.status='active' "
            "WHERE NOT EXISTS(SELECT 1 FROM action_intents a WHERE a.kind='report' AND a.target_id=o.post_id AND a.status!='failed') "
            "AND NOT EXISTS(SELECT 1 FROM blacklist_post_checks c WHERE c.post_id=o.post_id AND c.state='gone')").fetchone()[0]
        reported_posts = store._conn.execute(
            "SELECT COUNT(DISTINCT a.target_id) FROM action_intents a WHERE a.kind='report' AND a.status!='failed' "
            "AND json_extract(a.metadata_json,'$.engine')='blacklist'").fetchone()[0]
        authors = store._conn.execute("SELECT COUNT(*) FROM report_blacklist WHERE status='active'").fetchone()[0]
        reported_authors = store._conn.execute(
            "SELECT COUNT(DISTINCT CAST(json_extract(metadata_json,'$.target_author') AS TEXT)) FROM action_intents "
            "WHERE kind='report' AND status!='failed' AND json_extract(metadata_json,'$.engine')='blacklist'").fetchone()[0]
        closed = store._conn.execute(
            "SELECT COUNT(*) FROM (SELECT COUNT(*) n FROM action_intents WHERE kind='report' AND status!='failed' "
            "AND json_extract(metadata_json,'$.engine')='blacklist' GROUP BY CAST(json_extract(metadata_json,'$.target_author') AS TEXT) "
            'HAVING n>=?)', (cap,)).fetchone()[0]
        ready = store._conn.execute(
            "SELECT COUNT(DISTINCT c.author_id) FROM blacklist_post_checks c JOIN report_blacklist b "
            "ON b.author_id=c.author_id AND b.status='active' WHERE c.state='gone'").fetchone()[0]
        accounts = {r[0]: r[1] for r in store._conn.execute('SELECT status,COUNT(*) FROM blacklist_account_reports GROUP BY status')}
    reportable = len(pending_targets(store, 100000, policy=policy, now=ts))
    return {'authors_active': authors, 'authors_reported': reported_authors, 'authors_closed': closed,
            'authors_ready_for_account_report': ready, 'account_reports': accounts,
            'posts_visible_unreported': waiting, 'posts_reportable_now': reportable,
            'posts_pooled': max(0, waiting - reportable), 'posts_reported': reported_posts}


def reporting_enabled(store):
    return bool(store.policy('blacklist_reporting', True))


class BlacklistReporter:
    def __init__(self, actuator, store, *, accounts=None, policy=POLICY):
        self.actuator, self.store, self.policy = actuator, store, policy
        if actuator.store is None:
            actuator.store = store
        self._accounts = list(accounts) if accounts is not None else None
        self._next = 0
        self._last_scan = 0.0
        ensure_schema(store)

    # -- reporter accounts --------------------------------------------------------------------------- #
    def _reporters(self):
        if self._accounts is not None:
            return self._accounts
        with self.store._lock:
            return [r[0] for r in self.store._conn.execute(
                "SELECT b.account_id FROM persona_bindings b JOIN account_states s USING(account_id) WHERE s.mode='active' ORDER BY b.account_id")]

    def _reporter(self):
        accounts = [a for a in self._reporters() if a not in self.actuator.suspended
                    and not self.store.get_account_state(a).is_disabled()]
        if not accounts:
            return None
        account = accounts[self._next % len(accounts)]
        self._next += 1
        return account

    # -- gates --------------------------------------------------------------------------------------- #
    def blocked(self, ts):
        """Why no report may be sent now, or ''."""
        if not reporting_enabled(self.store):
            return 'blacklist_reporting_off'
        if self.store.is_reporting_suspended(now=ts)[0]:
            return 'reporting_suspended'
        forbidden = self.store.policy('report_forbidden', {}) or {}
        if forbidden and ts - float(forbidden.get('at', 0)) < float(self.policy['report']['forbidden_backoff_seconds']):
            return 'recent_report_403'
        return ''

    # -- one worker tick ----------------------------------------------------------------------------- #
    async def step(self, *, now=None):
        ts = time.time() if now is None else now
        out = {'added': [], 'reported': None, 'account_reported': None, 'checked': 0}
        if ts - self._last_scan >= float(self.policy['scan_every_seconds']):
            self._last_scan = ts
            out['added'] = await asyncio.to_thread(scan, self.store, now=ts, policy=self.policy)
        why = self.blocked(ts)
        if why:
            out['blocked'] = why
            return out
        out['checked'] = await self._check_reported_posts(ts)
        out['account_reported'] = await self._maybe_report_account(ts)
        out['reported'] = await self._report_next_post(ts)
        return out

    async def _report_next_post(self, ts):
        if await asyncio.to_thread(self.store.request_backlog, 'route:post_report', now=ts) > float(self.policy['report']['max_route_backlog_seconds']):
            return None
        targets = await asyncio.to_thread(pending_targets, self.store, 5, policy=self.policy, now=ts)
        if not targets:
            return None
        entries = active(self.store)
        for target in targets:
            entry = entries.get(int(target['author_id']))
            if not entry:
                continue
            account = self._reporter()
            if not account:
                return None
            detail = detail_for(entry, self.policy)
            context = {'engine': 'blacklist', 'target_author': str(target['author_id']), 'reason': 'inauthentic',
                       'blacklist_reason': entry['reason'], 'cluster': entry['cluster'], 'report_attempt_id': uuid.uuid4().hex}
            result = await self.actuator.report(account, str(target['post_id']), reason='inauthentic', detail=detail, context=context)
            return {'post_id': str(target['post_id']), 'author_id': int(target['author_id']), 'status': result.status, 'detail': result.detail}
        return None

    async def _check_reported_posts(self, ts):
        """Re-read a few posts we reported: a post that is gone was most likely hidden by the moderator."""
        rules = self.policy['report']
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT a.target_id,json_extract(a.metadata_json,'$.target_author') author FROM action_intents a "
                "LEFT JOIN blacklist_post_checks c ON c.post_id=a.target_id "
                "WHERE a.kind='report' AND a.status='succeeded' AND json_extract(a.metadata_json,'$.engine')='blacklist' "
                "AND COALESCE(c.state,'visible')='visible' AND COALESCE(c.checked_at,0)<? ORDER BY COALESCE(c.checked_at,0) LIMIT ?",
                (ts - float(rules['hidden_check_every_seconds']), int(rules['hidden_checks_per_step']))).fetchall()
        from .client.base import PlatformError
        getter = getattr(self.actuator._client, 'get_post', None)
        checked = 0
        for post_id, author in rows:
            state = 'visible'
            try:
                await getter(post_id)
            except PlatformError as exc:
                if exc.status != 404:
                    continue
                state = 'gone'
            except Exception:
                continue
            checked += 1
            with self.store.batch_transaction():
                self.store._conn.execute('INSERT INTO blacklist_post_checks VALUES(?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET '
                                         'state=excluded.state,checked_at=excluded.checked_at', (post_id, _id(author) or 0, state, ts))
        return checked

    async def _maybe_report_account(self, ts):
        """Report an account once one of its reported posts is gone and it shows 100+ actions."""
        if await asyncio.to_thread(self.store.request_backlog, 'route:account_report', now=ts) > float(self.policy['report']['max_route_backlog_seconds']):
            return None
        rules = self.policy['report']
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT c.author_id FROM blacklist_post_checks c JOIN report_blacklist b ON b.author_id=c.author_id AND b.status='active' "
                "LEFT JOIN blacklist_account_reports r ON r.author_id=c.author_id "
                "WHERE c.state='gone' AND (r.author_id IS NULL OR r.reported_at<?) LIMIT 1",
                (ts - float(rules['account_report_recheck_seconds']),)).fetchone()
        if not row:
            return None
        author = row[0]
        from .moderation.reporter import BlueTeamReporter
        activity = await BlueTeamReporter.refresh_account_activity(self, str(author), now=ts)
        count = int((activity or {}).get('action_count') or 0)
        if count < int(rules['account_report_min_actions']):
            with self.store.batch_transaction():
                self.store._conn.execute('INSERT INTO blacklist_account_reports VALUES(?,?,?,?) ON CONFLICT(author_id) DO UPDATE SET '
                                         'reported_at=excluded.reported_at,status=excluded.status', (author, ts, f'waiting_actions:{count}', ''))
            return {'author_id': author, 'status': f'waiting_actions:{count}'}
        account = self._reporter()
        if not account:
            return None
        entry = active(self.store).get(author)
        detail = detail_for(entry, self.policy) if entry else self.policy['report_detail']['prefix'] + self.policy['report_detail']['operator']
        result = await self.actuator.report_account(account, str(author), detail=detail,
                                                    context={'engine': 'blacklist', 'reason': 'inauthentic', 'report_attempt_id': uuid.uuid4().hex})
        with self.store.batch_transaction():
            self.store._conn.execute('INSERT INTO blacklist_account_reports VALUES(?,?,?,?) ON CONFLICT(author_id) DO UPDATE SET '
                                     'reported_at=excluded.reported_at,status=excluded.status,detail=excluded.detail',
                                     (author, ts, result.status, str(result.detail)[:200]))
        return {'author_id': author, 'status': result.status}


def stats(store, *, since=None):
    ensure_schema(store)
    ts = time.time()
    since = ts - 86400 if since is None else since
    with store._lock:
        listed = {f"{r[0]}:{r[1]}": r[2] for r in store._conn.execute(
            'SELECT status,reason,COUNT(*) FROM report_blacklist GROUP BY status,reason')}
        sent = {r[0]: r[1] for r in store._conn.execute(
            "SELECT status,COUNT(*) FROM action_intents WHERE kind='report' AND json_extract(metadata_json,'$.engine')='blacklist' "
            'AND created_at>=? GROUP BY status', (since,))}
        gone = store._conn.execute("SELECT COUNT(*) FROM blacklist_post_checks WHERE state='gone'").fetchone()[0]
        accounts = {r[0]: r[1] for r in store._conn.execute('SELECT status,COUNT(*) FROM blacklist_account_reports GROUP BY status')}
    pool = pool_summary(store, now=ts)
    return {'enabled': reporting_enabled(store), 'blacklist': listed, 'post_reports_24h': sent, 'reported_posts_gone': gone,
            'account_reports': accounts, 'queue': pool['posts_visible_unreported'], 'reportable_now': pool['posts_reportable_now'],
            'pool': pool, 'suspended_s': store.is_reporting_suspended(now=ts)[1]}
