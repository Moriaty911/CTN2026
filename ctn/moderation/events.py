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
