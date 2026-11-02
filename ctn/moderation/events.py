"""Explicit moderation capability. No guessed endpoint and no inference from 404s."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

class ModerationSource(Protocol):
    available: bool
    async def read_events(self) -> list[dict]: ...

@dataclass
class UnavailableModerationSource:
    available: bool = False
    reason: str = 'moderation read API not verified'
    async def read_events(self):
        return []

class MockModerationSource:
    available = True
    def __init__(self): self.events=[]
    def emit(self, event): self.events.append(dict(event))
    async def read_events(self):
        events,self.events=self.events,[]
        return events

async def consume_events(store,source):
    import asyncio
    if not source.available:
        return {'capability':'unavailable','applied':0}
    applied=0
    errors=0
    for event in await source.read_events():
        try: applied+=bool(await asyncio.to_thread(store.apply_moderation_event,event))
        except (ValueError,TypeError,KeyError):
            import hashlib,json,time
            key=hashlib.sha256(json.dumps(event,sort_keys=True,default=str).encode()).hexdigest()
            with store.batch_transaction():
                store._conn.execute('INSERT OR IGNORE INTO quarantined_events VALUES(?,?,?)',(key,'invalid_or_unmatched_event',time.time()))
            errors+=1
    return {'capability':'connected','applied':applied,'quarantined':errors}


_STATUS_HINTS=('hidden','moderat','status','visib','removed','deleted','report','flag','lock')


async def probe_report_targets(store,actuator,*,limit=2,interval_s=600,min_age_s=300,now=None):
    """Record what pending report targets look like over time. Observation only, never a ruling.

    The platform's representation of "hidden by a moderator" is not yet verified, so this only
    stores the fields that could carry it (and whether the post is still readable) for an operator
    to inspect after launch. Turning these observations into rulings is a separate, reviewed change.
    """
    import asyncio,json,time
    ts=time.time() if now is None else now
    with store._lock:
        rows=store._conn.execute("SELECT id,subject_id FROM report_attempts WHERE subject_kind='post' AND status IN ('pending','unknown') "
            "AND submitted_at>0 AND submitted_at<? ORDER BY submitted_at",(ts-min_age_s,)).fetchall()
    probed=0
    for row in rows:
        if probed>=limit:break
        key='probe:'+row['id']
        if ts-store.policy(key,0)<interval_s:continue
        store.set_policy(key,ts)
        raw=await actuator.get_post(row['subject_id'])
        probed+=1
        found=bool(raw) and str(raw.get('post_id',''))==str(row['subject_id'])
        source=(raw.get('raw') if isinstance(raw.get('raw'),dict) else raw) if found else {}
        fields={k:source[k] for k in sorted(source) if any(h in k.lower() for h in _STATUS_HINTS)}
        fields['_keys']=sorted(source)[:60]
        def save():
            with store.batch_transaction():
                store._conn.execute('INSERT INTO report_target_observations(attempt_id,subject_id,observed_at,found,fields_json) VALUES(?,?,?,?,?)',
                    (row['id'],row['subject_id'],ts,int(found),json.dumps(fields,default=str)[:4000]))
        await asyncio.to_thread(save)
    return probed
