"""REACT: decide what, if anything, to do about each post.

Pure functions, no I/O: given a post and our side, return a `Decision`. That
keeps the strategy unit-testable and lets us swap the heuristics for Jev later
without touching the read or act stages.

PLACEHOLDER SCORING. `relevance` and `alignment` are keyword heuristics standing
in for Jev's calibrated answers. They write into the same `JevAnswers` shape
Jev will return, so the rules below keep working when Jev lands -- at which
point the thresholds move to `PolicyConfig`.

The rules (one Decision per post):

    side not assigned            -> observe   (never act partisan without a side)
    not relevant enough          -> observe
    allied post, popular         -> repost    (Story score: spread what spreads)
    allied post                  -> like
    opposing post, active thread -> reply     (counter where people are reading)
    opposing post, quiet         -> observe   (replying would boost it)
    neutral relevant post        -> reply     (undecided audience: persuade)
"""

from __future__ import annotations

import random
import uuid
from typing import Sequence

from ..jev.postflight import check_incoming_post_language
from ..models import ActionKind, Decision, JevAnswers, Post, Side, Tier
from .compose import compose_reply

# Hashtags / phrases that signal a side. Guessed from the mock's content;
# replace with the real campaign vocabulary once sides are assigned.
SIDE_SIGNALS: dict[Side, tuple[str, ...]] = {
    Side.YES: ("#mandatenow", "#protectkingstonia", "protect kingstonia"),
    Side.NO: ("#mybodymychoice", "#keepkingstoniaopen", "protect choice"),
}

MIN_RELEVANCE = 1.5          # at least one keyword hit
ALLY_ALIGNMENT = 0.5         # |alignment| above this counts as clearly partisan
REPOST_MIN_ENGAGEMENT = 5    # likes + reposts before an allied post is worth reposting
COUNTER_MIN_REPLIES = 2      # replies before an opposing thread is worth entering


def _text_for(post: Post) -> str:
    return f"{post.text} {' '.join(post.hashtags)}".lower()


def relevance(post: Post, keywords: Sequence[str]) -> float:
    text = _text_for(post)
    score = sum(1.5 for k in keywords if k.strip() and k.strip().lower() in text)
    score += post.likes * 0.05 + post.reposts * 0.08 + post.replies * 0.07
    return score


def alignment(post: Post, side: Side) -> float | None:
    """+1 = clearly our side, -1 = clearly theirs, 0 = neutral/unknown.
    None when we have no side yet."""
    if side is Side.UNASSIGNED:
        return None
    text = _text_for(post)
    ours = sum(sig in text for sig in SIDE_SIGNALS[side])
    theirs = sum(sig in text for sig in SIDE_SIGNALS[side.opponent])
    if ours + theirs == 0:
        return 0.0
    return (ours - theirs) / (ours + theirs)


def decide(
    post: Post,
    *,
    side: Side,
    keywords: Sequence[str],
    account: str = "",
    rng: random.Random | None = None,
) -> Decision:
    rel = relevance(post, keywords)
    align = alignment(post, side)
    scores = JevAnswers(
        answers={"relevance": rel, "alignment": align},
        raw={"source": "heuristic"},
    )

    raw = post.raw if isinstance(post.raw, dict) else {}
    root_post_id = str(raw.get("root_post_id") or raw.get("parent_id") or "").strip()

    def make(tier: Tier, action: ActionKind, why: str, text: str = "") -> Decision:
        return Decision(
            decision_id=uuid.uuid4().hex[:12], post_id=post.post_id, tier=tier,
            account=account, scores=scores, rationale=why, action=action, text=text,
            root_post_id=root_post_id,
        )

    lang_ok, lang_reason = check_incoming_post_language(post.text)
    if not lang_ok:
        return make(Tier.OBSERVE, ActionKind.NONE, f"preflight:{lang_reason}")

    if align is None:
        return make(Tier.OBSERVE, ActionKind.NONE, "side not assigned yet")
    if rel < MIN_RELEVANCE:
        return make(Tier.OBSERVE, ActionKind.NONE, "below relevance threshold")

    if align >= ALLY_ALIGNMENT:
        if post.likes + post.reposts >= REPOST_MIN_ENGAGEMENT:
            return make(Tier.AMPLIFY, ActionKind.REPOST, "popular allied post")
        return make(Tier.AMPLIFY, ActionKind.LIKE, "allied post")

    if align <= -ALLY_ALIGNMENT:
        if post.replies >= COUNTER_MIN_REPLIES:
            text = compose_reply(post, side, "counter", rng=rng)
            return make(Tier.PUBLISH, ActionKind.REPLY, "active opposing thread", text)
        return make(Tier.OBSERVE, ActionKind.NONE, "quiet opposing post: replying would boost it")

    text = compose_reply(post, side, "persuade", rng=rng)
    return make(Tier.PUBLISH, ActionKind.REPLY, "relevant neutral post", text)
