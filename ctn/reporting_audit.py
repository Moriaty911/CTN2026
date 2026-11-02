"""Reconcile what actually happened to the reports we already filed.

The platform never hands us a ruling, so this module only records what is observable:

* a reported post that ``get_post`` answers 404 for was hidden by the moderator;
* an author whose reported post is gone and who shows 100+ actions is worth an account report;
* a report call refused with the six-hour ban wording means the moderator has just ruled
  "real person" (see :func:`ctn.moderation.decision.record_report_forbidden`).

``run_audit`` collects the first two, summarises the third, and prints hidden/total per evidence
reason so the numbers can be checked against the official scoreboard by hand. It never writes a
ruling: ``report_attempts.outcome`` stays empty until the operator supplies the real event.
"""
from __future__ import annotations

import time
from typing import Any

from .client.base import PlatformError
from .moderation.reporter import BlueTeamReporter

REPORTED_STATUSES = ('succeeded', 'unknown')


def _author_id(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class _ActivityShim:
    """Expose only the reads ``refresh_account_activity`` needs (posts, never the likes bucket)."""

    def __init__(self, client):
        self.actuator = self
        self.store = None
        self.get_account_posts = client.get_account_posts


def reported_posts(store, *, limit: int = 200, checked_before: float | None = None) -> list[dict[str, Any]]:
    """Every post we filed a report against, oldest check first."""
    until = time.time() + 1.0 if checked_before is None else float(checked_before)
    with store._lock:
        rows = store._conn.execute(
            "SELECT a.target_id AS post_id,"
            " MAX(COALESCE(json_extract(a.metadata_json,'$.target_author'),'')) AS author,"
            " MAX(COALESCE(json_extract(a.metadata_json,'$.blacklist_reason'),'')) AS reason,"
            " MAX(a.created_at) AS reported_at,"
            " MIN(COALESCE(c.checked_at,0)) AS checked_at"
            " FROM action_intents a LEFT JOIN blacklist_post_checks c ON c.post_id=a.target_id"
            " WHERE a.kind='report' AND a.status IN (?,?)"
            " GROUP BY a.target_id HAVING MIN(COALESCE(c.checked_at,0))<?"
            " ORDER BY checked_at, reported_at LIMIT ?",
            (*REPORTED_STATUSES, until, int(limit))).fetchall()
    return [dict(row) for row in rows]


async def sweep_reported_posts(client, store, *, now: float | None = None, limit: int = 200,
                               dry_run: bool = False) -> dict[str, Any]:
    """Re-read the posts we reported; a 404 means the moderator hid it."""
    ts = time.time() if now is None else now
    out: dict[str, Any] = {'checked': 0, 'gone': 0, 'visible': 0, 'unreadable': 0, 'gone_ids': [], 'by_reason': {}}
    for row in reported_posts(store, limit=limit):
        try:
            await client.get_post(str(row['post_id']))
        except PlatformError as exc:
            if exc.status != 404:
                out['unreadable'] += 1
                continue
            state = 'gone'
        except Exception:
            out['unreadable'] += 1
            continue
        else:
            state = 'visible'
        out['checked'] += 1
        out[state] += 1
        reason = row['reason'] or 'legacy'
        bucket = out['by_reason'].setdefault(reason, {'gone': 0, 'visible': 0})
        bucket[state] += 1
        if state == 'gone':
            out['gone_ids'].append(str(row['post_id']))
        if dry_run:
            continue
        with store.batch_transaction():
            store._conn.execute(
                'INSERT INTO blacklist_post_checks VALUES(?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET '
                'state=excluded.state,checked_at=excluded.checked_at',
                (str(row['post_id']), _author_id(row['author']), state, ts))
    return out


async def account_readiness(client, store, *, now: float | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Blacklisted authors with a hidden post: ready for an account report, or short on actions."""
    from .blacklist import POLICY as BLACKLIST_POLICY

    ts = time.time() if now is None else now
    rules = BLACKLIST_POLICY['report']
    min_actions = int(rules['account_report_min_actions'])
    recheck = float(rules['account_report_recheck_seconds'])
    with store._lock:
        rows = store._conn.execute(
            "SELECT c.author_id, COUNT(*) hidden, b.reason, b.cluster"
            " FROM blacklist_post_checks c JOIN report_blacklist b ON b.author_id=c.author_id AND b.status='active'"
            " LEFT JOIN blacklist_account_reports r ON r.author_id=c.author_id"
            " WHERE c.state='gone' AND COALESCE(r.reported_at,0)<?"
            " GROUP BY c.author_id ORDER BY hidden DESC LIMIT ?",
            (ts - recheck, int(limit))).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        shim = _ActivityShim(client)
        shim.store = store
        activity = await BlueTeamReporter.refresh_account_activity(shim, str(row['author_id']), now=ts)
        count = int((activity or {}).get('action_count') or 0)
        out.append({'author_id': row['author_id'], 'reason': row['reason'], 'cluster': row['cluster'],
                    'hidden_posts': row['hidden'], 'action_count': count,
                    'state': 'ready' if count >= min_actions else f'waiting_actions:{count}'})
    return out


def forbidden_summary(store, *, now: float | None = None, limit: int = 20) -> dict[str, Any]:
    """The 403 picture: what the last refusal said and how long our reports have been waiting."""
    ts = time.time() if now is None else now
    with store._lock:
        latest = dict(store.policy('report_forbidden', {}) or {})
        statuses = {row[0]: row[1] for row in store._conn.execute(
            'SELECT status,COUNT(*) FROM report_attempts GROUP BY status')}
        oldest = store._conn.execute(
            "SELECT MIN(submitted_at) FROM report_attempts WHERE status='pending'").fetchone()[0]
        failures = [{'at': row[0], 'detail': str(row[1])[:180]} for row in store._conn.execute(
            "SELECT created_at,detail FROM action_intents WHERE kind='report' AND status IN ('failed','blocked')"
            " ORDER BY created_at DESC LIMIT ?", (int(limit),))]
    suspended, remaining = store.is_reporting_suspended(now=ts)
    return {'suspended': suspended, 'remaining_s': remaining, 'last_forbidden': latest,
            'attempt_status': statuses,
            'oldest_pending_age_s': round(ts - oldest, 1) if oldest else 0.0,
            'recent_failures': failures}


def precision(store) -> dict[str, Any]:
    """Hidden/total per evidence reason, counting each reported post once."""
    with store._lock:
        rows = store._conn.execute(
            "SELECT reason, COUNT(*) filed, SUM(gone) gone FROM ("
            "  SELECT a.target_id,"
            "   MAX(COALESCE(json_extract(a.metadata_json,'$.blacklist_reason'),'')) reason,"
            "   MAX(CASE WHEN c.state='gone' THEN 1 ELSE 0 END) gone"
            "  FROM action_intents a LEFT JOIN blacklist_post_checks c ON c.post_id=a.target_id"
            "  WHERE a.kind='report' AND a.status IN (?,?)"
            "  GROUP BY a.target_id) GROUP BY reason ORDER BY filed DESC",
            REPORTED_STATUSES).fetchall()
    out: dict[str, Any] = {}
    for reason, filed, gone in rows:
        gone = int(gone or 0)
        out[reason or 'legacy'] = {'filed': int(filed), 'gone': gone,
                                   'rate': round(gone / filed, 3) if filed else 0.0}
    return out


async def run_audit(client, store, *, now: float | None = None, limit: int = 200,
                    accounts: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """One read-only pass: post outcomes, account candidates, the 403 picture, per-reason precision."""
    from .blacklist import ensure_schema

    ensure_schema(store)
    ts = time.time() if now is None else now
    posts = await sweep_reported_posts(client, store, now=ts, limit=limit, dry_run=dry_run)
    candidates = await account_readiness(client, store, now=ts) if accounts and not dry_run else []
    return {'posts': posts, 'account_candidates': candidates,
            'reports': forbidden_summary(store, now=ts), 'precision': precision(store), 'dry_run': dry_run}
