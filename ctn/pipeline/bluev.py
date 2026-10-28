"""Poll the verified accounts so their newest posts reach the queue while they are still fresh.

Eleven verified accounts produced 498 observed items, yet only 22 replies ever targeted them and 58
of those reply tasks expired before we reached the post (2026-10-28): the general feed crawl sees a
verified account only when one of its posts happens to land in a page we read. Reading their own
timelines costs about six requests a minute against a 300/min budget and removes that wait.

Ingesting a post is enough to queue it: `StateStore.upsert_observed_post` calls `enqueue_post`, which
scores it with `targeting.initial_schedule` — where `verified` now carries weight 16.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .perceive import post_from_dict

POLL_LIMIT = 8


def verified_accounts(store) -> list[str]:
    """Usernames the platform marks as verified, i.e. the newspapers, campaign leads and moderators."""
    with store._lock:
        rows = store._conn.execute(
            "SELECT account_id FROM bot_registry WHERE status='verified_npc' ORDER BY account_id").fetchall()
    return [str(row[0]) for row in rows]


async def verified_step(
    actuator,
    store,
    *,
    now: float | None = None,
    accounts: list[str] | None = None,
    limit: int = POLL_LIMIT,
    sleep=asyncio.sleep,
) -> dict[str, Any]:
    """One pass over the verified timelines. Returns counts for the worker log."""
    ts = time.time() if now is None else float(now)
    names = list(accounts) if accounts is not None else verified_accounts(store)
    seen = queued = failed = details = 0
    for name in names:
        try:
            rows = await actuator.get_account_posts(name, limit=limit)
        except Exception:
            # A suspended or unreadable account must not stop the sweep.
            failed += 1
            continue
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            # `/accounts/{id}/posts` returns the text truncated to nothing: hydrate the ones we would queue.
            if not str(row.get('text') or '').strip() or row.get('text_complete') is False:
                full = await actuator.get_post(str(row.get('post_id') or ''))
                if isinstance(full, dict) and full.get('post_id'):
                    row = {**row, **full}
                    details += 1
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
    return {'accounts': len(names), 'seen': seen, 'queued': queued, 'failed': failed, 'hydrated': details}
