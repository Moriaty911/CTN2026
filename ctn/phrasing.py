"""Keep our accounts from sounding like one template (policies/phrasing-v1.json).

Two parts: a per-draft variation (opening style, whether the vote may be declared), stable for a seed so a
retry of the same draft keeps it; and a local gate that rejects a draft whose opening, closing or stock
phrase the team has used recently, with a reason a redraft can act on.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

POLICY = json.loads((Path(__file__).resolve().parents[1] / 'policies/phrasing-v1.json').read_text())

_STANCE = re.compile(r"[^.!?]*\b(?:i'?m|i am|i'?ll be|i'?d|i will)\s+(?:be\s+)?vot(?:e|ing)\s+yes\b[^.!?]*[.!?]?\s*", re.I)


def _words(text):
    return re.findall(r"[a-z0-9#']+", text.lower().replace('’', "'"))


def _roll(seed, salt):
    return int(hashlib.sha256(f'{salt}:{seed}'.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def variation(seed, policy=POLICY):
    """Opening style and whether this draft may say how the author votes; deterministic per seed."""
    styles = policy['opening_styles']
    return {'opening': styles[int(_roll(seed, 'opening') * len(styles)) % len(styles)],
            'allow_stance': _roll(seed, 'stance') < float(policy['stance_share'])}


def instructions(seed, policy=POLICY):
    v = variation(seed, policy)
    banned = ', '.join(f'"{p}"' for p in policy['stock_openings'][:6])
    stance = ("You may mention how you will vote once, briefly, if it fits." if v['allow_stance'] else
              "Do not say how you will vote; let the reasoning show where you stand.")
    avoid = policy.get('overuse', {}).get('avoid_in_prompt') or []
    overused = f" These constructions are everywhere in this debate already, so avoid them: {', '.join(avoid)}." if avoid else ''
    return (f"Phrasing: {v['opening']} Never open with a stock acknowledgement such as {banned}.{overused} {stance} "
            "No campaign slogans or slogan hashtags.")


def strip_stance(text):
    """Remove vote-declaration sentences from a style example so it is not copied as a template."""
    return _STANCE.sub('', text).strip() or text


def recent_texts(store, policy=POLICY):
    with store._lock:
        return [r[0] for r in store._conn.execute(
            "SELECT text FROM action_intents WHERE kind IN ('post','reply','repost') AND status IN ('claimed','sending','unknown','succeeded') "
            "AND COALESCE(json_extract(metadata_json,'$.purpose'),'') NOT IN ('initialise','warmup') AND text<>'' "
            "ORDER BY created_at DESC LIMIT ?", (int(policy['recent_window']),))]


def _share(recent, match):
    if not recent:
        return 0.0
    return sum(1 for t in recent if match(t)) / len(recent)


def overuse_reason(text, recent, policy=POLICY):
    """'' unless this draft leans on a construction the team is already leaning on too hard.

    The gate is a share, not a ban: one account may open with 'the venue list' occasionally, but once enough of
    the recent window does, another draft like it is rejected. Five thousand replies in three days showed the
    team converging on a handful of constructions, which is what makes them easy to group.
    """
    cfg = policy.get('overuse') or {}
    lowered = text.lower()
    for cap in cfg.get('opening_patterns') or []:
        rx = re.compile(cap['pattern'], re.I)
        if not rx.match(lowered):
            continue
        share = _share(recent, lambda t: bool(rx.match(t.lower())))
        if share >= float(cap['max_share']):
            return f'overused_opening: {int(round(share * 100))}% of recent posts open like this; open differently'
    for phrase, cap in (cfg.get('phrase_caps') or {}).items():
        if phrase not in lowered:
            continue
        share = _share(recent, lambda t: phrase in t.lower())
        if share >= float(cap):
            return f'overused_phrase: {int(round(share * 100))}% of recent posts say "{phrase}"; say it another way'
    return ''


def repetition_reason(text, recent, policy=POLICY):
    """'' when the draft is fine, else a reason naming what to avoid (fed back to the redraft)."""
    # Hashtags are assigned and shared on purpose (policies/hashtags-v1.json): they never count as a repeated
    # opening or ending, only as a stock phrase.
    words = [w for w in _words(text) if not w.startswith('#')]
    if not words:
        return ''
    lowered = ' '.join(_words(text))
    head = ' '.join(words[:int(policy['opening_words'])])
    for opening in policy['stock_openings']:
        o = _words(opening)
        if words[:len(o)] == o:
            return f'repetitive_phrasing: do not open with "{opening}"'
    full = [' '.join(_words(t)) for t in recent]
    others = [' '.join(w for w in o.split() if not w.startswith('#')) for o in full]
    limit = int(policy['max_repeats'])
    if sum(o.startswith(head + ' ') or o == head for o in others) >= limit:
        return f'repetitive_phrasing: other accounts recently opened with "{head}"; open differently'
    tail = ' '.join(words[-int(policy['closing_words']):])
    if len(words) > int(policy['closing_words']) and sum(o.endswith(' ' + tail) for o in others) >= limit:
        return f'repetitive_phrasing: other accounts recently ended with "{tail}"; end differently'
    for phrase in policy['stock_phrases']:
        p = ' '.join(_words(phrase))
        if p and f' {p} ' in f' {lowered} ' and any(f' {p} ' in f' {o} ' for o in full):
            return f'repetitive_phrasing: drop the stock phrase "{phrase}"'
    return overuse_reason(text, recent, policy)


def stats(store, n=50, policy=POLICY):
    """Share of recent publications using stock openings or a vote declaration, and the most repeated openings."""
    texts = recent_texts(store, {**policy, 'recent_window': n})
    if not texts:
        return {'sample': 0}
    norm = [' '.join(_words(t)) for t in texts]
    stock = sum(any(x.split()[:len(_words(o))] == _words(o) for o in policy['stock_openings']) for x in norm)
    stance = sum(bool(_STANCE.search(t)) for t in texts)
    heads = {}
    for x in norm:
        h = ' '.join(x.split()[:int(policy['opening_words'])]); heads[h] = heads.get(h, 0) + 1
    top = sorted(heads.items(), key=lambda kv: -kv[1])[:3]
    return {'sample': len(texts), 'stock_opening_share': round(stock / len(texts), 2),
            'vote_declaration_share': round(stance / len(texts), 2), 'top_openings': dict(top),
            'overused': {p: round(_share(texts, lambda t, p=p: p in t.lower()), 2)
                         for p in (policy.get('overuse', {}).get('phrase_caps') or {})}}
