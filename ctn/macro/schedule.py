"""Campaign-day pacing for strategic originals (policies/originals-v1.json).

Day 0 is the first campaign day in Sydney time. The daily limit follows the Red multiplier tiers and
switches to the next tier from `ramp_hour` on the previous evening, because engagement on a post keeps
arriving for hours and Red points are multiplied when they are recorded. The day is divided into
`limit` equal slots inside `active_hours`; each slot is one macro job with bounded retries.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo('Australia/Sydney')
POLICY = json.loads((Path(__file__).resolve().parents[2] / 'policies/originals-v1.json').read_text())


def _local(ts):
    return datetime.fromtimestamp(ts, tz=SYDNEY)


def campaign_day(ts, policy=POLICY):
    return (_local(ts).date() - date.fromisoformat(policy['day0'])).days


def _tier(day, rows, key):
    value = rows[0][key]
    for row in rows:
        if day >= row['from_day']:value = row[key]
    return value


def red_multiplier(ts, policy=POLICY):
    return _tier(campaign_day(ts, policy), policy['red_multipliers'], 'red')


def scheduled_limit(ts, policy=POLICY):
    """Daily original ceiling in force at `ts`, including the evening ramp to the next tier."""
    day = campaign_day(ts, policy)
    if _local(ts).hour >= int(policy['ramp_hour']):day += 1
    return int(_tier(day, policy['tiers'], 'limit'))


def effective_limit(store, ts, policy=POLICY):
    """The schedule unless an operator has fixed the limit (`ops campaign configure`)."""
    if store.policy('originals_schedule', True):return scheduled_limit(ts, policy)
    return int(store.policy('daily_original_limit', 10))


def slot(ts, limit, policy=POLICY):
    """(slot_id_suffix, start, end) for the slot containing `ts`, or None outside active hours."""
    local = _local(ts)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_h, end_h = policy.get('active_hours') or (0, 24)
    window_start = midnight + timedelta(hours=start_h)
    window_end = midnight + timedelta(hours=end_h)
    if not (window_start <= local < window_end) or limit <= 0:return None
    length = (window_end - window_start).total_seconds() / limit
    index = int((local - window_start).total_seconds() // length)
    start = window_start.timestamp() + index * length
    return f'{local.date().isoformat()}:{limit}:{index}', start, start + length


def opener_share(ts, policy=POLICY):
    return float(policy.get('opener_share', {}).get(str(campaign_day(ts, policy)), 0.0))


def purpose_for(slot_id, ts, policy=POLICY):
    """Deterministic per slot: early campaign days are mostly open questions (P00), later all P01."""
    share = opener_share(ts, policy)
    roll = int(hashlib.sha256(slot_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return 'P00' if roll < share else 'P01'
