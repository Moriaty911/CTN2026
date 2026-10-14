"""Macro-level environment state collector for trending topics, news, and accounts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time
from typing import Any

from ctn.actuator import Actuator
from ctn.persona import PersonaRouter


class MacroStateCollector:
    """Collects macro platform dynamics: trending hashtags, newspapers, VIPs, and bot accounts."""

    def __init__(
        self,
        actuator: Actuator,
        router: PersonaRouter | None = None,
        *,
        side: str = "yes",
    ) -> None:
        self.actuator = actuator
        self.side = side.lower()
        self.router = router if router is not None else PersonaRouter(default_side=self.side)

    async def collect(self) -> dict[str, Any]:
        """Query platform endpoints and account state machine to build a macro snapshot."""
        now_monotonic = time.monotonic()
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Fetch trending items
        trending_raw = await self.actuator.trending(limit=20)
        trending_tags: list[str] = []
        trending_posts: list[dict[str, Any]] = []

        if isinstance(trending_raw, list):
            for item in trending_raw:
                if not isinstance(item, dict):
                    continue
                tag = item.get("hashtag") or item.get("tag")
                if tag and str(tag) not in trending_tags:
                    trending_tags.append(str(tag))
                if item.get("text") or item.get("post_id"):
                    trending_posts.append({
                        "post_id": item.get("post_id", ""),
                        "author": item.get("author", ""),
                        "text": item.get("text", ""),
                        "likes": item.get("likes", 0),
                        "replies": item.get("replies", 0),
                        "reposts": item.get("reposts", 0),
                    })

        # 2. Search newspapers and VIP NPC coverage
        daily_k_posts = await self.actuator.search("Daily Kingstonia", limit=5)
        herald_posts = await self.actuator.search("Kingstonia Herald", limit=5)
        vip_posts_raw = await self.actuator.search("journalist", limit=5)

        def clean_post_list(raw_list: Any) -> list[dict[str, Any]]:
            cleaned = []
            if isinstance(raw_list, list):
                for p in raw_list:
                    if isinstance(p, dict) and (p.get("text") or p.get("post_id")):
                        cleaned.append({
                            "post_id": p.get("post_id", ""),
                            "author": p.get("author", ""),
                            "text": p.get("text", ""),
                            "likes": p.get("likes", 0),
                            "replies": p.get("replies", 0),
                        })
            return cleaned

        # 3. Account states from PersonaRouter
        accounts_info = []
        candidates = self.router.registry.list_by_side(self.side)
        for p in candidates:
            state = self.router.get_state(p.account_id)
            in_cd = state.is_in_cooldown(now_monotonic, p.cooldown_s)
            rem_cd = state.remaining_cooldown(now_monotonic, p.cooldown_s)
            accounts_info.append({
                "account_id": p.account_id,
                "name": p.name,
                "archetype": p.archetype,
                "bio": p.bio,
                "in_cooldown": in_cd,
                "remaining_cooldown_s": round(rem_cd, 1),
                "post_count": state.post_count,
            })

        return {
            "timestamp": now_iso,
            "assigned_side": self.side,
            "trending_hashtags": trending_tags[:10],
            "trending_posts": trending_posts[:5],
            "newspapers": {
                "daily_kingstonia": clean_post_list(daily_k_posts),
                "kingstonia_herald": clean_post_list(herald_posts),
            },
            "vip_npc_posts": clean_post_list(vip_posts_raw),
            "accounts": accounts_info,
        }

    def collect_sync(self) -> dict[str, Any]:
        """Synchronous helper for CLI and script invocations."""
        return asyncio.run(self.collect())
