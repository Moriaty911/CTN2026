"""Macro-level environment state collector for trending topics, news, and accounts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time
from typing import Any

from ctn.actuator import Actuator
from ctn.jev.postflight import check_incoming_post_language
from ctn.persona import PersonaRouter
from ctn.pipeline.news_crawler import NewsSitesCrawler


class MacroStateCollector:
    """Collects macro platform dynamics: trending hashtags, newspapers, VIPs, and bot accounts."""

    def __init__(
        self,
        actuator: Actuator,
        router: PersonaRouter | None = None,
        *,
        side: str = "yes",
        news_crawler: NewsSitesCrawler | None = None,
    ) -> None:
        self.actuator = actuator
        self.side = side.lower()
        self.router = router if router is not None else PersonaRouter(default_side=self.side)
        if news_crawler is not None:
            self.news_crawler: NewsSitesCrawler | None = news_crawler
        elif getattr(actuator, "_client", None).__class__.__name__ == "OfficialLegitClient":
            self.news_crawler = NewsSitesCrawler()
        else:
            self.news_crawler = None

    async def collect(self) -> dict[str, Any]:
        """Query platform endpoints and account state machine to build a macro snapshot."""
        now_ts = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Fetch trending items
        trending_raw = await self.actuator.trending(limit=20)
        trending_tags: list[str] = []
        trending_posts: list[dict[str, Any]] = []

        for item in trending_raw:
            tag=item.get('hashtag') or item.get('tag')
            if tag and str(tag) not in trending_tags: trending_tags.append(str(tag))
        ranked=await self.actuator.trending_posts(limit=20)
        ranked_at=time.time()
        ranked_iso=datetime.fromtimestamp(ranked_at,timezone.utc).isoformat()
        if not getattr(self.actuator,'dry_run',True) and isinstance(ranked,list):
            # Only successful reads can be reused; generic [] fallback is ambiguous here.
            if ranked:
                await asyncio.to_thread(self.router.store.record_feed_scan,'macro:'+str(ranked_at),ranked,now=ranked_at,source='macro_ranked_feed')
        for item in ranked:
            if isinstance(item,dict) and item.get('post_id'):
                trending_posts.append({**item,'source':'ranked_feed','observed_at':ranked_iso})

        # 2. Fetch newspapers (prefer official sites.legitreal.com NewsSitesCrawler when attached)
        def clean_post_list(raw_list: Any) -> list[dict[str, Any]]:
            cleaned = []
            if isinstance(raw_list, list):
                for p in raw_list:
                    if isinstance(p, dict) and (p.get("text") or p.get("post_id")):
                        text_val = str(p.get("text") or "")
                        if text_val:
                            lang_ok, _ = check_incoming_post_language(text_val)
                            if not lang_ok:
                                continue
                        entry: dict[str, Any] = {
                            "post_id": p.get("post_id", ""),
                            "author": p.get("author", ""),
                            "text": text_val,
                            "likes": p.get("likes", 0),
                            "replies": p.get("replies", 0),
                        }
                        for extra_key in ("headline", "summary", "slug", "url", "published_at", "body"):
                            if p.get(extra_key):
                                entry[extra_key] = p[extra_key]
                        cleaned.append(entry)
            return cleaned

        newspapers_dict: dict[str, list[dict[str, Any]]]
        if self.news_crawler is not None:
            raw_news = await self.news_crawler.fetch_newspapers_snapshot(limit_per_site=5)
            newspapers_dict = {
                k: clean_post_list(v) for k, v in raw_news.items()
            }
        else:
            daily_k_posts = await self.actuator.search("Daily Kingstonia", limit=5)
            herald_posts = await self.actuator.search("Kingstonia Herald", limit=5)
            newspapers_dict = {
                "daily_kingstonia": clean_post_list(daily_k_posts),
                "kingstonia_herald": clean_post_list(herald_posts),
            }

        vip_posts_raw = await self.actuator.search("journalist", limit=5)

        # 3. Account states and SQLite metrics from PersonaRouter
        accounts_info = []
        candidates = self.router.registry.list_by_side(self.side)
        for p in candidates:
            if not p.account_id:
                accounts_info.append({'persona_id':p.persona_id,'persona_version':p.version,
                    'occupation_group':p.occupation_group,'account_id':None,'name':p.name,
                    'mode':'unbound','eligible':False})
                continue
            state = self.router.get_state(p.account_id)
            metrics = self.router.store.get_account_metrics(p.account_id)
            in_cd = state.is_in_cooldown(now_ts, p.cooldown_s)
            rem_cd = state.remaining_cooldown(now_ts, p.cooldown_s)
            accounts_info.append({
                "account_id": p.account_id,
                'persona_id':p.persona_id,'persona_version':p.version,'occupation_group':p.occupation_group,
                "name": p.name,
                "archetype": p.archetype,
                "bio": p.bio,
                "is_frozen": state.is_frozen,
                "mode":state.mode,"capability_tier":state.capability_tier,
                "original_locked_until":state.original_locked_until,
                "is_banned": state.is_banned,
                "in_cooldown": in_cd,
                "remaining_cooldown_s": round(rem_cd, 1),
                "post_count": state.post_count,
                "allied_reply_count": metrics.get("allied_reply_count", 0),
                "opposing_reply_count": metrics.get("opposing_reply_count", 0),
            })

        from ctn.ratelimit import BudgetLedger
        return {
            "requests":self.actuator.ledger.snapshot() if isinstance(self.actuator.ledger,BudgetLedger) else {},
            "limits":{"daily_original_limit":self.router.store.policy('daily_original_limit',10),
                "max_pending_reports":self.router.store.policy('max_pending_reports',10)},
            "runtime":self.router.store.runtime_metrics(),
            'persona_catalog':{'hash':self.router.registry.catalog_hash,
                'active_hash':self.router.store.policy('active_persona_catalog'),
                'version':(self.router.registry.catalog or {}).get('catalog_version')},
            'performance':self.router.store.performance_report(),
            "timestamp": now_iso,
            "assigned_side": self.side,
            "execution": {"mode":"mock" if self.actuator._client.__class__.__name__=="MockLegitClient" else ("dry-run" if self.actuator.dry_run else "live")},
            "trending_hashtags": trending_tags[:10],
            "trending_tags": trending_tags[:10],
            "sample": {"source":"ranked_feed","requested":20,"received":len(ranked),"observed_at":ranked_iso},
            "majority": {"status":"requires_pi_assessment","allow_unknown":True},
            "trending_posts": trending_posts[:20],
            "newspapers": newspapers_dict,
            "vip_npc_posts": clean_post_list(vip_posts_raw),
            "accounts": accounts_info,
            "allied_reply_stats": {
                "recent_1h_count": self.router.store.count_recent_allied_replies(3600.0),
                "qualifying_seen_total": self.router.store.get_counter("allied_qualifying_seen"),
            },
        }

    def collect_sync(self) -> dict[str, Any]:
        """Synchronous helper for CLI and script invocations."""
        return asyncio.run(self.collect())
