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
