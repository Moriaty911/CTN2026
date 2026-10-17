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
    for event in await source.read_events():
        applied+=bool(await asyncio.to_thread(store.apply_moderation_event,event))
    return {'capability':'connected','applied':applied}
