"""Persona and account state models for persona-driven bot operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Historical constructors remain usable for explicit offline replay only.
PersonaArchetype = str


@dataclass(frozen=True)
class Persona:
    """Immutable character profile; the registry supplies its separate SQLite binding."""

    account_id: str = ""
    archetype: PersonaArchetype = ""
    name: str = ""
    bio: str = ""
    tone_instruction: str = ""
    cooldown_s: float = 60.0
    assigned_side: str = "yes"
    weight: float = 1.0
    capability_tier: str = "standard"
    persona_id: str = ""
    version: int = 1
    occupation_group: str = ""
    topic_tags: tuple[str, ...] = ()
    profile_json: str = ""

    def __post_init__(self):
        legacy = {"medical_expert":"medical_public_health", "policy_analyst":"law_policy",
                  "sharp_debater":"independent_commentary", "grassroots_worker":"transport_logistics"}
        if not self.persona_id:
            object.__setattr__(self, "persona_id", self.account_id)
        if not self.occupation_group:
            object.__setattr__(self, "occupation_group", legacy.get(self.archetype, self.archetype))

    @property
    def profile(self):
        import json
        return json.loads(self.profile_json) if self.profile_json else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "archetype": self.archetype,
            "name": self.name,
            "bio": self.bio,
            "tone_instruction": self.tone_instruction,
            "cooldown_s": self.cooldown_s,
            "assigned_side": self.assigned_side,
            "weight": self.weight,
            "persona_id": self.persona_id,
            "version": self.version,
            "occupation_group": self.occupation_group,
            "topic_tags": list(self.topic_tags),
            "profile": self.profile,
        }


@dataclass
class AccountState:
    """Runtime operational state of an account, tracking cooldowns and suspension status."""

    account_id: str
    last_posted_at: float = 0.0
    post_count: int = 0
    is_frozen: bool = False
    is_banned: bool = False

    mode: str = "active"
    capability_tier: str = "standard"
    last_original_at: float = 0.0
    last_reply_at: float = 0.0
    original_locked_until: float = 0.0

    def is_disabled(self) -> bool:
        """Return True if the account is temporarily frozen or permanently banned."""
        return self.is_frozen or self.is_banned or self.mode != "active"

    def is_in_cooldown(self, now: float, cooldown_s: float) -> bool:
        """Check if the account is currently cooling down."""
        if self.last_posted_at <= 0.0:
            return False
        return (now - self.last_posted_at) < cooldown_s

    def remaining_cooldown(self, now: float, cooldown_s: float) -> float:
        """Return the seconds remaining before cooldown expires."""
        if self.last_posted_at <= 0.0:
            return 0.0
        elapsed = now - self.last_posted_at
        return max(0.0, cooldown_s - elapsed)

    def mark_used(self, now: float) -> None:
        """Update last posted timestamp and increment post counter."""
        self.last_posted_at = now
        self.post_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "last_posted_at": self.last_posted_at,
            "post_count": self.post_count,
            "is_frozen": self.is_frozen,
            "is_banned": self.is_banned,
            "mode": self.mode, "capability_tier": self.capability_tier,
            "original_locked_until": self.original_locked_until,
            "last_original_at":self.last_original_at,"last_reply_at":self.last_reply_at,
        }


@dataclass(frozen=True)
class RoutingDecision:
    """Outcome of matching a target post to an available account persona."""

    account_id: str
    persona: Persona
    target_archetype: str
    score: float
    reason: str
    fallback_used: bool = False
    in_cooldown: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "persona": self.persona.to_dict(),
            "target_archetype": self.target_archetype,
            "score": self.score,
            "reason": self.reason,
            "fallback_used": self.fallback_used,
            "in_cooldown": self.in_cooldown,
            "occupation_group": self.persona.occupation_group,
            "persona_id": self.persona.persona_id,
        }
