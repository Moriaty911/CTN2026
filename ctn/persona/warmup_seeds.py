"""Daily examples come from the same canonical persona catalog."""
from .registry import get_default_registry

WARMUP_SEEDS={p.persona_id:tuple(e['text'] for e in p.profile['example_posts'] if e['kind']=='daily')
              for p in get_default_registry().list_all()}


def list_warmup_seeds(identifier, *, registry=None):
    persona=(registry or get_default_registry()).get(identifier)
    if persona is None:return []
    return [p['text'] for p in persona.profile.get('example_posts',[]) if p['kind']=='daily']


def get_warmup_seed(identifier, index=0, *, registry=None):
    seeds=list_warmup_seeds(identifier,registry=registry)
    if not seeds:raise ValueError('no daily examples for this persona or bound account')
    return seeds[index % len(seeds)]
