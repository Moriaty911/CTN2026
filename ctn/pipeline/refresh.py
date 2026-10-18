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
