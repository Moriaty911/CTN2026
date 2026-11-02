"""Hashtags for our originals (policies/hashtags-v1.json): a few fixed tags, chosen per slot, appended by code."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

POLICY = json.loads((Path(__file__).resolve().parents[1] / 'policies/hashtags-v1.json').read_text())
_TAG = re.compile(r'(?<!\w)#\w+')


def _roll(seed, salt):
    return int(hashlib.sha256(f'{salt}:{seed}'.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def assign(seed, purpose, policy=POLICY):
    """Tags for one original; stable for a seed so a retried draft keeps them. [] means untagged."""
    rule = policy['originals'].get(purpose)
    if not rule or _roll(seed, 'share') >= float(rule['share']):
        return []
    total = sum(float(c['weight']) for c in rule['combos'])
    pick, acc = _roll(seed, 'combo') * total, 0.0
    for combo in rule['combos']:
        acc += float(combo['weight'])
        if pick < acc:
            return list(combo['tags'])
    return list(rule['combos'][-1]['tags'])


def strip(text):
    """Remove the model's own hashtags; ours are appended separately."""
    return re.sub(r'[ \t]{2,}', ' ', _TAG.sub('', text)).strip()


def for_repost(text, policy=POLICY):
    """Tags for a repost comment: the original's own tags, plus the first of ours it lacks.

    A commented repost still increments the original's repost counter (verified 2026-10-20 on post 56444:
    reps 0 -> 1) and the platform parses its hashtags, so the push can ride the original's tag and add ours.
    """
    rule = policy.get('reposts') or {}
    limit = int(rule.get('max_tags', 4))
    tags, seen = [], set()
    for tag in _TAG.findall(text or ''):
        if tag.lower() not in seen and len(tags) < max(1, limit - 1):
            seen.add(tag.lower()); tags.append(tag)
    for extra in rule.get('extra_tag_priority') or []:
        if extra.lower() in seen:
            continue
        tags.append(extra)
        break
    return tags[:limit]


def repost_comments(text, seed, count=3, index=None, policy=POLICY):
    """Candidate comment texts for one repost, most-'ours' tag first; `[]` means post it without a comment.

    `index` (a repost's rank inside one push) makes the lead sentence distinct across a push; without it the
    start phrase is hashed from `seed` so a retried action keeps its text.
    """
    tags = for_repost(text, policy)
    if not tags:
        return []
    phrases = list((policy.get('reposts') or {}).get('phrases') or [])
    if not phrases:
        return [' '.join(tags)]
    if index is None:
        start = int(hashlib.sha256(f'repost:{seed}'.encode()).hexdigest()[:8], 16) % len(phrases)
    else:
        start = int(index) % len(phrases)
    return [f'{phrases[(start + i) % len(phrases)]} {" ".join(tags)}' for i in range(min(int(count), len(phrases)))]
