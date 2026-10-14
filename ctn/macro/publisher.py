"""Strategic proactive publisher with JEV gatekeeping and account cooldown tracking."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ctn.actuator import Actuator
from ctn.jev import JevPostflightChecker
from ctn.persona import PersonaRouter


class StrategicPublisher:
    """Executes safe proactive posts, checking account cadence and JEV safety gate."""

    def __init__(
        self,
        actuator: Actuator,
        postflight: JevPostflightChecker,
        router: PersonaRouter | None = None,
        *,
        side: str = "yes",
    ) -> None:
        self.actuator = actuator
        self.postflight = postflight
        self.side = side.lower()
        self.router = router if router is not None else PersonaRouter(default_side=self.side)

    async def publish(
        self,
        account_id: str,
        text: str,
        *,
        hashtags: list[str] | None = None,
        purpose: str = "P01",
        bypass_jev: bool = False,
    ) -> dict[str, Any]:
        """Publish a proactive post after cooldown and JEV preflight verification."""
        now = time.monotonic()
        persona = self.router.registry.get(account_id)
        if persona is None:
            return {
                "ok": False,
                "status": "rejected",
                "reason": f"unknown_account:{account_id}",
                "account_id": account_id,
            }

        # 1. Cooldown verification
        state = self.router.get_state(account_id)
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

        # 2. JEV preflight gatekeeper
        postflight_dict: dict[str, Any] = {}
        if not bypass_jev:
            verdict = self.postflight.check(
                post_id="proactive_publish",
                target_text="Kingstonia referendum public discussion",
                reply_text=post_text,
                assigned_side=self.side,
                intended_purpose=purpose,
            )
            postflight_dict = verdict.to_dict()
            if not verdict.can_send:
                return {
                    "ok": False,
                    "status": "rejected",
                    "reason": f"jev_rejected:{verdict.reason}",
                    "account_id": account_id,
                    "postflight": postflight_dict,
                }
        else:
            postflight_dict = {"can_send": True, "bypassed": True}

        # 3. Execution via Actuator
        res = await self.actuator.post(account_id, post_text)
        if res.ok:
            self.router.mark_used(account_id, now=now)
            post_id = res.response.get("post_id") if isinstance(res.response, dict) else ""
            return {
                "ok": True,
                "status": "published",
                "post_id": post_id or res.decision_id,
                "account_id": account_id,
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
        bypass_jev: bool = False,
    ) -> dict[str, Any]:
        """Synchronous wrapper for publish."""
        return asyncio.run(
            self.publish(
                account_id,
                text,
                hashtags=hashtags,
                purpose=purpose,
                bypass_jev=bypass_jev,
            )
        )
