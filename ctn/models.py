"""Domain types shared across the pipeline.

Deliberately framework-free: plain dataclasses that serialise to/from JSON so
that the state store, the decision log, and the eventual presentation write-up
all speak the same vocabulary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class AccountKind(str, Enum):
    PRIMARY = "primary"     # the one human-operated account
    BOT = "bot"             # persona-driven PC bot account


class AccountStatus(str, Enum):
    ACTIVE = "active"
    THROTTLED = "throttled"     # platform slowed us down; keep but back off
    SUSPENDED = "suspended"     # parked, probe later
    RETIRED = "retired"         # permanently dead; stop using


class Tier(str, Enum):
    """The action ladder.

    Tiers are ordered by how scarce the underlying platform action is, and each
    maps to a different scoring channel. This is the key correction to the naive
    "cheap vs expensive" framing: liking a post and replying to a post do not buy
    the same score.
    """

    OBSERVE = "observe"     # costs no platform write; builds the world model
    AMPLIFY = "amplify"     # like / repost -> Story-score channel (narrative spread)
    PUBLISH = "publish"     # reply / original post -> the only channel that can
                            # produce citizen -> us engagement (Engagement score)


class Side(str, Enum):
    """Which side of the referendum the team argues for.

    Assigned by the organisers, so it is configuration (CTN_SIDE), never a
    constant. Until it is assigned the pipeline must not take partisan actions.
    YES/NO follow the mock's stance convention: +1 = firmly YES.
    """

    YES = "yes"
    NO = "no"
    UNASSIGNED = ""

    @property
    def opponent(self) -> "Side":
        if self is Side.YES:
            return Side.NO
        if self is Side.NO:
            return Side.YES
        return Side.UNASSIGNED


class ActionKind(str, Enum):
    """Concrete platform actions. `Tier` says how scarce an action is; this says
    which call the actuator makes."""

    NONE = "none"           # observe only
    LIKE = "like"
    REPOST = "repost"       # a.k.a. retweet
    REPLY = "reply"         # a.k.a. comment
    POST = "post"
    FOLLOW = "follow"
    UNFOLLOW = "unfollow"


class GuardVerdict(str, Enum):
    ALLOW = "allow"
    FLAG = "flag"      # allowed but logged and down-weighted
    BLOCK = "block"    # must not be published


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #
@dataclass
class Persona:
    """A bot's stable identity. Diversity across personas is not cosmetic: if a
    side's accounts all sound alike the network reads as coordinated inauthentic
    behaviour (Legit Social rule LS.5.3) and we are competing with our own
    teammates for the same narrative space."""

    persona_id: str
    display_name: str
    voice: str                 # prose description injected into the LLM prompt
    demographics: dict[str, str] = field(default_factory=dict)
    interests: list[str] = field(default_factory=list)
    # Lexical habits, so surface forms diverge even on identical arguments.
    signature_phrases: list[str] = field(default_factory=list)
    avoid_phrases: list[str] = field(default_factory=list)
    # Bias toward one side of the argument. We never want a flat 50/50 botnet
    # repeating one line: we want a population.
    stance_intensity: float = 0.7      # 0 = mild, 1 = crusader


@dataclass
class Account:
    handle: str
    kind: AccountKind
    persona_id: str
    status: AccountStatus = AccountStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    actions_taken: int = 0
    # Trust signals matter: LS.5.1 says limits vary by trust and recent activity.
    warmup_until: float = 0.0
    notes: str = ""


@dataclass
class Post:
    """A platform item. `author_is_npc` is a local inference, not ground truth:
    the platform does not label citizens versus competitor bots."""

    post_id: str
    author: str
    text: str
    created_at: float = 0.0
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    hashtags: list[str] = field(default_factory=list)
    author_is_npc: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class JevAnswers:
    """Normalised Jev response.

    `answers` holds per-question normalised values: floats for noul/score,
    strings for choice. `confidence` holds the per-question confidence, and
    `raw` keeps the full response for the decision log -- probabilities are the
    whole point of using a calibrated model, so we never throw them away.
    """

    answers: dict[str, Any] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    ok: bool = True
    error: str = ""

    def noul(self, key: str, default: float = 0.0) -> float:
        value = self.answers.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def score(self, key: str, default: float = 0.0) -> float:
        return self.noul(key, default)

    def choice(self, key: str, default: str = "") -> str:
        value = self.answers.get(key, default)
        return str(value)


@dataclass
class Decision:
    """The audit record for one allocation choice.

    This is simultaneously (a) the input to the actuator, (b) the fuel for
    threshold calibration between polls, and (c) the evidence base for the
    seven-minute finalist presentation.
    """

    decision_id: str
    post_id: str
    tier: Tier
    account: str
    scores: JevAnswers
    rationale: str = ""
    action: ActionKind = ActionKind.NONE
    text: str = ""                     # reply / post body, when the action has one
    created_at: float = field(default_factory=time.time)
    # Filled in later, when the in-universe poll next moves.
    observed_poll_delta: float | None = None


@dataclass
class ActionResult:
    decision_id: str
    kind: str
    account: str
    target_id: str
    ok: bool
    detail: str = ""
    latency_ms: float = 0.0
    response: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


def to_jsonable(obj: Any) -> Any:
    """Dataclass/enum aware JSON conversion for logging."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
