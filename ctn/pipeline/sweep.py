"""Read the timelines of citizens who have written to us, so their next post reaches the queue.

2026-10-29, 24 hours: 6,834 citizen posts were observed from 1,028 authors, yet only 228 of the 615
citizens who had replied to us in the previous week ever got an answer back. The feed crawl sees a
warm author only when one of their posts happens to land in a page we read, and 438 reply tasks a day
expired before we reached them. Reading their own timelines costs about 20 requests a minute against
a 300/min budget and closes that gap.

Ingesting a post is enough to queue it: `StateStore.upsert_observed_post` calls `enqueue_post`.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .perceive import post_from_dict
from .targeting import CITIZEN_MAX_ID

DEFAULT_CITIZEN_SWEEP = {'hours': 168, 'batch': 100, 'posts_per_account': 6}
CURSOR_KEY = 'citizen_sweep_cursor'


def warm_citizens(store, *, now: float | None = None, hours: int | None = None) -> list[str]:
    """Citizens who replied to one of our posts recently, most recent first."""
    rules = dict(DEFAULT_CITIZEN_SWEEP)
    rules.update(store.policy('citizen_sweep') or {})
    ts = time.time() if now is None else float(now)
    since = ts - 3600 * int(hours if hours is not None else rules['hours'])
    with store._lock:
        rows = store._conn.execute(
            "SELECT o.author, MAX(o.created_at) last "
            "FROM observed_posts o JOIN publications p ON p.post_id = o.parent_id "
            "WHERE o.created_at >= ? AND o.author_id BETWEEN 1 AND ? AND o.author IS NOT NULL AND o.author <> '' "
            "GROUP BY o.author ORDER BY last DESC",
            (since, CITIZEN_MAX_ID)).fetchall()
    return [str(row[0]) for row in rows]


async def sweep_step(
    actuator,
    store,
    *,
    now: float | None = None,
    batch: int | None = None,
    limit: int | None = None,
    sleep=asyncio.sleep,
) -> dict[str, Any]:
    """One rotating slice of warm citizen timelines. Returns counts for the worker log."""
    ts = time.time() if now is None else float(now)
    rules = dict(DEFAULT_CITIZEN_SWEEP)
    rules.update(store.policy('citizen_sweep') or {})
    size = int(batch if batch is not None else rules['batch'])
    per_account = int(limit if limit is not None else rules['posts_per_account'])
    names = warm_citizens(store, now=ts, hours=rules.get('hours'))
    if not names:
        return {'accounts': 0, 'seen': 0, 'queued': 0, 'failed': 0, 'next': 0}
    cursor = store.policy(CURSOR_KEY) or 0
    start = int(cursor) % len(names)
    slice_ = names[start:start + size]
    if len(slice_) < size and len(names) > size:  # wrap so a pass always covers a full slice
        slice_ += names[:size - len(slice_)]
    seen = queued = failed = 0
    for name in slice_:
        try:
            rows = await actuator.get_account_posts(name, limit=per_account)
        except Exception:
            # A suspended or unreadable account must not stop the sweep.
            failed += 1
            continue
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if str(row.get('repost_of_post_id') or '') or not str(row.get('text') or '').strip():
                continue  # a repost or a truncated row: the feed crawl handles those, we only want fresh text
            post = post_from_dict(row)
            if post is None:
                continue
            seen += 1
            try:
                is_new, _record = store.upsert_observed_post(post, now=ts)
            except Exception:
                failed += 1
                continue
            if is_new:
                queued += 1
        await sleep(0)  # let the reply/comments workers run between accounts
    store.set_policy(CURSOR_KEY, (start + len(slice_)) % len(names))
    return {'accounts': len(slice_), 'seen': seen, 'queued': queued, 'failed': failed,
            'next': (start + len(slice_)) % len(names)}
