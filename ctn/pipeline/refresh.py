"""Staged engagement refresh for open reply candidates.

A post seen in the latest feed is seconds old and has no engagement yet. Instead of paging back
through the feed, open candidates are re-read by ID through `/posts/{id}/counters` (one request,
no pages) at fixed ages (default 10 and 25 minutes, inside the 30-minute opportunity window).
Refreshes are the lowest-priority reads: they are spaced out, capped per minute, skipped while
the team read queue is backed up, and dropped when stale instead of queueing up.
"""
from __future__ import annotations

import asyncio
import time

from .targeting import POLICY


async def refresh_step(actuator, store, *, now=None, sleep=asyncio.sleep):
    cfg = POLICY['refresh']
    ts = time.time() if now is None else now
    if await asyncio.to_thread(store.request_backlog, 'team:read', now=ts) > cfg['max_team_read_backlog_seconds']:
        return 0
    window = store.policy('refresh_window', {}) or {}
    minute = int(ts // 60)
    used = int(window.get('n', 0)) if window.get('minute') == minute else 0
    allowance = min(cfg['per_step'], max(0, cfg['per_minute'] - used))
    if allowance <= 0:
        return 0
    rows = await asyncio.to_thread(store.claim_refreshes, allowance, now=ts, stale_after=cfg['stale_after_seconds'])
    done = 0
    for row in rows:
        counters = await actuator.get_counters(row['post_id'])
        await asyncio.to_thread(store.finish_refresh, row, counters, now=time.time() if now is None else ts)
        done += 1
        if cfg['spacing_seconds'] and done < len(rows):
            await sleep(cfg['spacing_seconds'])
    store.set_policy('refresh_window', {'minute': minute, 'n': used + done})
    return done


# Our own publications: the crawl stream only carries other people's feed rows (and `/posts/{id}` returns
# no counters), so our own engagement is polled directly at fixed ages instead of being guessed.
OWN_METRIC_RULE = {'per_step': 3, 'per_minute': 9, 'spacing_seconds': 2.0,
                   'max_team_read_backlog_seconds': 120}


async def own_metric_step(actuator, store, *, now=None, sleep=asyncio.sleep):
    """Read back counters for our own posts that are due (stages: 90s .. 24h)."""
    ts = time.time() if now is None else now
    if await asyncio.to_thread(store.request_backlog, 'team:read', now=ts) > OWN_METRIC_RULE['max_team_read_backlog_seconds']:
        return 0
    window = store.policy('own_metric_window', {}) or {}
    minute = int(ts // 60)
    used = int(window.get('n', 0)) if window.get('minute') == minute else 0
    allowance = min(OWN_METRIC_RULE['per_step'], max(0, OWN_METRIC_RULE['per_minute'] - used))
    if allowance <= 0:
        return 0
    rows = await asyncio.to_thread(store.claim_own_metrics, allowance, now=ts)
    done = 0
    for row in rows:
        counters = await actuator.get_counters(row['post_id'])
        await asyncio.to_thread(store.finish_own_metric, row, counters, now=time.time() if now is None else ts)
        done += 1
        if done < len(rows):
            await sleep(OWN_METRIC_RULE['spacing_seconds'])
    store.set_policy('own_metric_window', {'minute': minute, 'n': used + done})
    return done


async def refresh_tick(actuator, store):
    """The `refresh` worker: candidate counters first, then our own publications."""
    return await refresh_step(actuator, store) + await own_metric_step(actuator, store)


def own_metrics_summary(store, limit=10):
    """Latest read-back per publication (`ops metrics`), with the trending flag for comparison."""
    now = time.time()
    return {'originals': [{'post_id': r['post_id'], 'kind': r['kind'], 'stage': r['stage'], 'reads': r['reads'],
                           'done': bool(r['done']), 'likes': r['likes'], 'replies': r['replies'],
                           'reposts': r['reposts'], 'views': r['views'], 'trending': bool(r.get('trending_at')),
                           'age_h': round((now - float(r['published_at'] or 0)) / 3600, 2) if r.get('published_at') else None}
                          for r in store.own_metric_rows(limit)]}
