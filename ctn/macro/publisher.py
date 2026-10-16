"""Strategic proactive publisher reusing unified postflight gatekeeping and cooldown tracking."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from ctn.actuator import Actuator
from ctn.jev import JevPostflightChecker
from ctn.jev.postflight import (
    MAX_HASHTAG_CHARS,
    MAX_POST_CHARS,
    PURPOSE_RULES,
    format_persona_profile,
    validate_text_constraints,
)
from ctn.persona import PersonaRouter

# Alias kept for external callers
BRANCH_RULES = PURPOSE_RULES


class StrategicPublisher:
    """Executes safe proactive posts, delegating text validation and semantic review to postflight."""

    def __init__(
        self,
        actuator: Actuator,
        postflight: JevPostflightChecker,
        router: PersonaRouter | None = None,
        *,
        side: str = "yes",
        max_post_chars: int = MAX_POST_CHARS,
        max_hashtag_chars: int = MAX_HASHTAG_CHARS,
    ) -> None:
        self.actuator = actuator
        self.postflight = postflight
        self.side = side.lower()
        self.router = router if router is not None else PersonaRouter(default_side=self.side)
        self.max_post_chars = max_post_chars
        self.max_hashtag_chars = max_hashtag_chars
        self._recent_normalized_posts: set[str] = set()

    def validate_post_constraints(self, post_text: str) -> tuple[bool, str]:
        """Delegate deterministic Layer-1 code checks to unified postflight validator and SQLite store."""
        is_valid, reason = validate_text_constraints(
            post_text,
            max_chars=self.max_post_chars,
            max_hashtag_chars=self.max_hashtag_chars,
            seen_normalized_texts=self._recent_normalized_posts,
        )
        if not is_valid:
            return False, reason
        if self.router.store.has_normalized_text(post_text):
            return False, "duplicate_post_text"
        return True, "valid"

    async def publish(
        self,
        account_id: str,
        text: str,
        *,
        hashtags: list[str] | None = None,
        purpose: str = "P01",
        branch: str = "",
        target_text: str = "",
        bypass_jev: bool = False,
    ) -> dict[str, Any]:
        """Publish a proactive post after cooldown and unified Layer-1 + Layer-2 verification."""
        now = time.monotonic()
        persona = self.router.registry.get(account_id)
        if persona is None:
            return {
                "ok": False,
                "status": "rejected",
                "reason": f"unknown_account:{account_id}",
                "account_id": account_id,
            }

        # 1. Suspension / Ban & Cooldown verification
        state = self.router.get_state(account_id)
        if state.is_banned:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "account_banned",
                "account_id": account_id,
            }
        if state.is_frozen:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "account_frozen",
                "account_id": account_id,
            }
        if state.is_in_cooldown(now, persona.cooldown_s):
            rem = state.remaining_cooldown(now, persona.cooldown_s)
            return {
                "ok": False,
                "status": "blocked",
                "reason": f"account_in_cooldown:{round(rem, 1)}s",
                "account_id": account_id,
                "remaining_cooldown_s": round(rem, 1),
            }

        # Append hashtags if specified and not already in text
        post_text = text.strip()
        if hashtags:
            for tag in hashtags:
                formatted_tag = tag if tag.startswith("#") else f"#{tag}"
                if formatted_tag.lower() not in post_text.lower():
                    post_text = f"{post_text} {formatted_tag}"

        # 2. Layer-1 Deterministic Code Gate (shared with micro replies)
        is_valid, validation_reason = self.validate_post_constraints(post_text)
        if not is_valid:
            return {
                "ok": False,
                "status": "rejected",
                "reason": f"constraint_rejected:{validation_reason}",
                "account_id": account_id,
                "text": post_text,
            }

        # 3. Layer-2 JEV Postflight Gatekeeper (shared with micro replies, with strict_alignment=True)
        active_key = branch if branch else purpose
        resolved_purpose = PURPOSE_RULES.get(active_key, active_key)
        persona_profile = format_persona_profile(persona)
        effective_target = target_text.strip() or "Kingstonia referendum public discussion"

        postflight_dict: dict[str, Any] = {}
        if not bypass_jev:
            verdict = self.postflight.check(
                post_id="proactive_publish",
                target_text=effective_target,
                reply_text=post_text,
                assigned_side=self.side,
                intended_purpose=resolved_purpose,
                persona_profile=persona_profile,
                strict_alignment=True,
            )
            postflight_dict = verdict.to_dict()
            if not verdict.can_send:
                return {
                    "ok": False,
                    "status": "rejected",
                    "reason": f"jev_rejected:{verdict.reason}",
                    "account_id": account_id,
                    "branch": active_key,
                    "postflight": postflight_dict,
                }
        else:
            postflight_dict = {"can_send": True, "bypassed": True}

        # 4. Execution via Actuator
        res = await self.actuator.post(account_id, post_text)
        if res.ok:
            self.router.mark_used(account_id, now=now)
            normalized = re.sub(r"\s+", " ", post_text.lower())
            self._recent_normalized_posts.add(normalized)
            post_id = res.response.get("post_id") if isinstance(res.response, dict) else ""
            final_id = post_id or res.decision_id
            self.router.store.log_interaction(
                engine="macro",
                account_id=account_id,
                action="post",
                target_post_id=final_id,
                target_stance="aligned",
                purpose=active_key,
                text=post_text,
            )
            return {
                "ok": True,
                "status": "published",
                "post_id": final_id,
                "account_id": account_id,
                "branch": active_key,
                "text": post_text,
                "postflight": postflight_dict,
            }

        return {
            "ok": False,
            "status": "failed",
            "reason": f"actuator_failed:{res.detail}",
            "account_id": account_id,
        }

    def publish_sync(
        self,
        account_id: str,
        text: str,
        *,
        hashtags: list[str] | None = None,
        purpose: str = "P01",
        branch: str = "",
        target_text: str = "",
        bypass_jev: bool = False,
    ) -> dict[str, Any]:
        """Synchronous wrapper for publish."""
        return asyncio.run(
            self.publish(
                account_id,
                text,
                hashtags=hashtags,
                purpose=purpose,
                branch=branch,
                target_text=target_text,
                bypass_jev=bypass_jev,
            )
        )
