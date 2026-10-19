"""Everyday posts for initialise posts.

Extra posts (config/personas/daily-posts.v1.json: questions, observations, asides) come first,
then the persona catalog's daily examples. Each persona starts at its own stable offset among
the extra posts so accounts do not all open with the same kind of post; a rejected post moves
to the next one (see ctn.warmup).
"""
import hashlib
import json
from pathlib import Path

from .registry import get_default_registry

DAILY_POSTS_PATH=Path(__file__).resolve().parents[2]/'config/personas/daily-posts.v1.json'
_EXTRA=json.loads(DAILY_POSTS_PATH.read_text())['posts'] if DAILY_POSTS_PATH.exists() else {}


def _catalog_daily(persona):
    return [p['text'] for p in persona.profile.get('example_posts',[]) if p['kind']=='daily']


def _ordered(persona):
    extra=list(_EXTRA.get(persona.persona_id,()))
    if extra:
        offset=int(hashlib.sha256(persona.persona_id.encode()).hexdigest(),16)%len(extra)
        extra=extra[offset:]+extra[:offset]
    catalog=[t for t in _catalog_daily(persona) if t not in extra]
    return extra+catalog


WARMUP_SEEDS={p.persona_id:tuple(_ordered(p)) for p in get_default_registry().list_all()}


def list_warmup_seeds(identifier, *, registry=None):
    persona=(registry or get_default_registry()).get(identifier)
    if persona is None:return []
    return _ordered(persona)


def get_warmup_seed(identifier, index=0, *, registry=None):
    seeds=list_warmup_seeds(identifier,registry=registry)
    if not seeds:raise ValueError('no daily examples for this persona or bound account')
    return seeds[index % len(seeds)]
