"""Text for replies and posts, plus a minimal pre-publish check.

PLACEHOLDER. These templates exist so the loop can run end to end before the
LLM layer (`ctn/llm`) and personas are wired in. Replace `compose_reply` with an
LLM call that takes the target post, our side and the acting bot's `Persona`;
keep the signature so the loop does not change.

Identical text across bots reads as coordinated inauthentic behaviour
(LS.5.3), which is why `check_text` flags repeats.
"""

from __future__ import annotations

import random
from typing import Iterable

from ..models import GuardVerdict, Post, Side

# Platform length limit is not published yet; conservative guess.
MAX_LEN = 280

_TEMPLATES: dict[Side, dict[str, tuple[str, ...]]] = {
    Side.YES: {
        "persuade": (
            "Worth thinking about who is most at risk here. #KingstoniaVotes",
            "I was unsure too, but protecting each other seems worth it. #KingstoniaVotes",
        ),
        "counter": (
            "I hear you, but the hospitals tell a different story. #KingstoniaVotes",
            "Respectfully, a little inconvenience now beats a crisis later.",
        ),
    },
    Side.NO: {
        "persuade": (
            "People should get to make this call for themselves. #KingstoniaVotes",
            "I was unsure too, but mandates feel like a step too far. #KingstoniaVotes",
        ),
        "counter": (
            "I hear you, but who decides where this stops? #KingstoniaVotes",
            "Respectfully, trust works better than force.",
        ),
    },
}


def compose_reply(post: Post, side: Side, intent: str, *, rng: random.Random | None = None) -> str:
    """`intent` is "persuade" (undecided audience) or "counter" (opposing post)."""
    options = _TEMPLATES.get(side, {}).get(intent, ())
    if not options:
        return ""
    return (rng or random).choice(options)


def check_text(text: str, recent: Iterable[str] = ()) -> GuardVerdict:
    """Cheap local check before anything is published. The Jev safety gates
    (risk / injection / hate thresholds in PolicyConfig) go here later."""
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_LEN:
        return GuardVerdict.BLOCK
    if stripped in {r.strip() for r in recent}:
        return GuardVerdict.FLAG
    return GuardVerdict.ALLOW
