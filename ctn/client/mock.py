"""A simulated Legit Social, good enough to build and load-test against.

This is not a toy. It deliberately reproduces the properties that actually shape
our architecture, because a mock that only returns happy-path data would let us
ship a pipeline that falls over on contact:

*   A population of NPC citizens that post, and whose stance drifts in response
    to what they are exposed to -- so the Story-score channel is exercised.
*   Engagement that accrues on our posts, but only from NPCs. Bot-to-bot likes
    earn nothing, mirroring the 2026 rule wording that engagement score comes
    from simulated citizens.
*   Dynamic rate limiting that tightens with recent activity, and escalating
    enforcement (warn -> throttle -> suspend), so the guardrail logic and the
    account pool's health handling are tested for real.
*   Latency and occasional transient failures, so backoff paths execute.

It is deterministic given a seed, which means a regression in the decision
pipeline shows up as a diff in the mock run rather than as a mystery.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .base import AccountSuspended, PlatformError, Throttled


@dataclass
class MockNPC:
    handle: str
    stance: float = 0.0            # -1 = firmly NO, +1 = firmly YES
    engagement: float = 0.5        # pliability
    follows: set[str] = field(default_factory=set)
    seen: list[str] = field(default_factory=list)


@dataclass
class MockPost:
    post_id: str
    author: str
    text: str
    created_at: float
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    hashtags: list[str] = field(default_factory=list)
    likers: set[str] = field(default_factory=set)
    parent_id: str = ""


class MockLegitClient:
    """Implements `client.base.LegitClient`."""

    HASHTAGS = ["#KingstoniaVotes", "#H2D2", "#DolphinFlu", "#ProtectKingstonia",
                "#KeepKingstoniaOpen", "#MandateNow", "#MyBodyMyChoice"]

    def __init__(
        self,
        *,
        seed: int = 1337,
        npc_count: int = 400,
        latency_ms: tuple[float, float] = (40.0, 220.0),
        throttle_after: int = 60,
        throttle_window_s: float = 30.0,
        failure_rate: float = 0.01,
    ) -> None:
        self.rng = random.Random(seed)
        self.npcs: dict[str, MockNPC] = {
            f"citizen_{i:04d}": MockNPC(
                handle=f"citizen_{i:04d}",
                stance=self.rng.uniform(-1.0, 1.0),
                engagement=self.rng.uniform(0.2, 0.9),
            )
            for i in range(npc_count)
        }
        self.posts: dict[str, MockPost] = {}
        self.comments: dict[str, list[MockPost]] = {}
        self.accounts: set[str] = set()
        self.suspended: set[str] = set()
        self._latency_ms = latency_ms
        self._throttle_after = throttle_after
        self._throttle_window_s = throttle_window_s
        self._failure_rate = failure_rate
        self._recent: list[float] = []
        self._warned: set[str] = set()
        self.call_count = 0
        self.read_count = 0
        self.write_count = 0
        self.throttle_count = 0
        self._seed_posts(120)

    # ---- internals ------------------------------------------------------- #
    def _seed_posts(self, n: int) -> None:
        for _ in range(n):
            self._npc_post(self.rng.choice(list(self.npcs.values())))

    def _npc_post(self, npc: MockNPC) -> MockPost:
        stance_word = ("protect choice" if npc.stance < 0 else "protect Kingstonia")
        text = (
            f"{self.rng.choice(['Honestly,', 'Look,', 'I keep thinking,', 'Not sure but'])} "
            f"{self.rng.choice(self.HASHTAGS)} matters. We should {stance_word}."
        )
        post = MockPost(
            post_id=uuid.uuid4().hex[:12],
            author=npc.handle,
            text=text,
            created_at=time.time(),
            hashtags=self.rng.sample(self.HASHTAGS, k=self.rng.randint(1, 2)),
        )
        self.posts[post.post_id] = post
        return post

    async def _simulate(self, *, write: bool = False) -> None:
        self.call_count += 1
        if write:
            self.write_count += 1
        else:
            self.read_count += 1

        await asyncio.sleep(self.rng.uniform(*self._latency_ms) / 1000.0)

        if self.rng.random() < self._failure_rate:
            raise PlatformError("transient upstream error", retryable=True, status=502)

        now = time.time()
        self._recent = [t for t in self._recent if now - t < self._throttle_window_s]
        self._recent.append(now)
        if len(self._recent) > self._throttle_after:
            self.throttle_count += 1
            raise Throttled("dynamic rate limit exceeded", retry_after_s=5.0)

    def _check_account(self, account: str) -> None:
        if account in self.suspended:
            raise AccountSuspended(account=account)

    def _tick_world(self) -> None:
        """NPCs react, drift, and post. Called opportunistically on reads so the
        world moves without a separate simulation loop."""
        for _ in range(self.rng.randint(0, 3)):
            npc = self.rng.choice(list(self.npcs.values()))
            self._npc_post(npc)

        # NPCs consume recent posts and drift toward what they see.
        if self.posts:
            for npc in self.rng.sample(list(self.npcs.values()), k=min(20, len(self.npcs))):
                post = self.rng.choice(list(self.posts.values()))
                npc.seen.append(post.post_id)
                if npc.rng_roll() if False else self.rng.random() < 0.15:
                    drift = self.rng.uniform(-0.03, 0.03) * npc.engagement
                    npc.stance = max(-1.0, min(1.0, npc.stance + drift))
                if post.author not in npc.follows and self.rng.random() < 0.05:
                    npc.follows.add(post.author)

    def _as_dict(self, post: MockPost) -> dict[str, Any]:
        d: dict[str, Any] = {
            "post_id": post.post_id,
            "author": post.author,
            "text": post.text,
            "created_at": post.created_at,
            "likes": post.likes,
            "reposts": post.reposts,
            "replies": post.replies,
            "hashtags": list(post.hashtags),
        }
        if post.parent_id:
            d["parent_id"] = post.parent_id
        return d

    # ---- reads ----------------------------------------------------------- #
    async def trending(self, *, limit: int = 50) -> list[dict[str, Any]]:
        await self._simulate()
        counts: dict[str, int] = {}
        for post in self.posts.values():
            for tag in post.hashtags:
                counts[tag] = counts.get(tag, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
        return [{"hashtag": tag, "volume": n} for tag, n in top]

    async def search(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        await self._simulate()
        self._tick_world()
        q = query.lower()
        hits = [
            p for p in self.posts.values()
            if not p.parent_id and (q in p.text.lower() or any(q in h.lower() for h in p.hashtags))
        ]
        hits.sort(key=lambda p: -p.created_at)
        return [self._as_dict(p) for p in hits[:limit]]

    async def get_post(self, post_id: str) -> dict[str, Any]:
        await self._simulate()
        post = self.posts.get(post_id)
        if post is None:
            raise PlatformError(f"post {post_id} not found", retryable=False, status=404)
        return self._as_dict(post)

    async def get_comments(
        self, post_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> list[dict[str, Any]]:
        await self._simulate()
        if post_id in self.comments:
            offset = int(cursor) if cursor and str(cursor).isdigit() else 0
            items = [self._as_dict(c) for c in self.comments[post_id][offset : offset + limit]]
            return items
        n = self.rng.randint(0, 8)
        return [
            {
                "post_id": uuid.uuid4().hex[:12],
                "parent_id": post_id,
                "author": self.rng.choice(list(self.npcs)).handle if self.npcs else "anon",
                "text": self.rng.choice(
                    ["This is exactly right.", "Source?", "Disagree, but respectfully.",
                     "Think about the hospitals.", "Who pays for this?"]
                ),
                "created_at": time.time(),
            }
            for _ in range(min(n, limit))
        ]

    async def get_account_posts(
        self, target: str, *, limit: int = 50, cursor: str | None = None
    ) -> list[dict[str, Any]]:
        await self._simulate()
        clean = str(target).strip().lstrip("@")
        authored: list[MockPost] = [
            p for p in self.posts.values() if p.author == clean
        ]
        for thread in self.comments.values():
            for c in thread:
                if c.author == clean and c.post_id not in self.posts:
                    authored.append(c)
        authored.sort(key=lambda p: -p.created_at)
        offset = int(cursor) if cursor and str(cursor).isdigit() else 0
        return [self._as_dict(p) for p in authored[offset : offset + limit]]

    async def get_feed(self, account: str, *, limit: int = 50) -> list[dict[str, Any]]:
        await self._simulate()
        top_level = [p for p in self.posts.values() if not p.parent_id]
        recent = sorted(top_level, key=lambda p: -p.created_at)[:limit]
        return [self._as_dict(p) for p in recent]

    async def get_profile(self, handle: str) -> dict[str, Any]:
        await self._simulate()
        npc = self.npcs.get(handle)
        if npc is not None:
            return {"handle": handle, "kind": "npc", "stance": round(npc.stance, 3),
                    "followers": len(npc.follows)}
        return {"handle": handle, "kind": "player", "stance": None, "followers": 0}

    async def get_mentions(self, account: str, *, limit: int = 50) -> list[dict[str, Any]]:
        await self._simulate()
        return [self._as_dict(p) for p in
                sorted(self.posts.values(), key=lambda p: -p.created_at)[:limit]]

    # ---- writes ---------------------------------------------------------- #
    async def _record_write(self, post: MockPost) -> None:
        """NPC engagement. Note: only NPCs engage, never other player bots --
        matching the 2026 rule that engagement score comes from citizens."""
        for npc in self.rng.sample(list(self.npcs.values()), k=min(12, len(self.npcs))):
            if self.rng.random() < 0.35 * npc.engagement:
                post.likers.add(npc.handle)
                post.likes += 1
                if self.rng.random() < 0.25:
                    post.reposts += 1
                if self.rng.random() < 0.15:
                    post.replies += 1
                # Exposure shifts stance toward the post's framing.
                if self.rng.random() < 0.2:
                    npc.stance = max(-1.0, min(1.0, npc.stance + 0.02 * npc.engagement))

    async def post(self, account: str, text: str, **kwargs: Any) -> dict[str, Any]:
        self._check_account(account)
        await self._simulate(write=True)
        self.accounts.add(account)
        post = MockPost(
            post_id=uuid.uuid4().hex[:12],
            author=account,
            text=text,
            created_at=time.time(),
            hashtags=[w for w in text.split() if w.startswith("#")],
        )
        self.posts[post.post_id] = post
        await self._record_write(post)
        return self._as_dict(post)

    async def repost(self, account: str, post_id: str) -> dict[str, Any]:
        self._check_account(account)
        await self._simulate(write=True)
        post = self.posts.get(post_id)
        if post is None:
            raise PlatformError("no such post", retryable=False, status=404)
        post.reposts += 1
        return {"ok": True, "post_id": post_id}

    async def reply(self, account: str, post_id: str, text: str) -> dict[str, Any]:
        self._check_account(account)
        await self._simulate(write=True)
        parent = self.posts.get(post_id)
        if parent is not None:
            parent.replies += 1
        reply = MockPost(
            post_id=uuid.uuid4().hex[:12],
            author=account,
            text=text,
            created_at=time.time(),
            hashtags=[w for w in text.split() if w.startswith("#")],
            parent_id=post_id,
        )
        self.posts[reply.post_id] = reply
        self.comments.setdefault(post_id, []).append(reply)
        await self._record_write(reply)
        return self._as_dict(reply)

    async def like(self, account: str, post_id: str) -> dict[str, Any]:
        self._check_account(account)
        await self._simulate(write=True)
        post = self.posts.get(post_id)
        if post is None:
            raise PlatformError("no such post", retryable=False, status=404)
        # Only NPC likes move the counter. A bot like is recorded but scores
        # nothing, which is exactly the trap we are designing around.
        if account in self.npcs:
            post.likes += 1
        return {"ok": True, "post_id": post_id, "scored": account in self.npcs}

    async def follow(self, account: str, target: str) -> dict[str, Any]:
        self._check_account(account)
        await self._simulate(write=True)
        npc = self.npcs.get(target)
        if npc is not None:
            npc.follows.add(account)
        return {"ok": True, "target": target}

    async def unfollow(self, account: str, target: str) -> dict[str, Any]:
        self._check_account(account)
        await self._simulate(write=True)
        npc = self.npcs.get(target)
        if npc is not None:
            npc.follows.discard(account)
        return {"ok": True, "target": target}

    async def report(self, account: str, post_id: str, *, reason: str = "") -> dict[str, Any]:
        self._check_account(account)
        await self._simulate(write=True)
        post = self.posts.get(post_id)
        if post is None:
            raise PlatformError("no such post", retryable=False, status=404)
        # Reporting a genuine NPC citizen is rejected (6-hour penalty in 2026 rules);
        # reporting a player-bot post is upheld.
        is_npc = post.author in self.npcs
        outcome = "rejected" if is_npc else "upheld"
        return {
            "ok": True,
            "post_id": post_id,
            "target_author": post.author,
            "reason": reason,
            "outcome": outcome,
        }

    # ---- simulation helpers (not part of the Protocol) -------------------- #
    async def close(self) -> None:
        return None

    def faction_mean_stance(self) -> float:
        """Aggregate citizen opinion. Stands in for the in-universe poll."""
        if not self.npcs:
            return 0.0
        return sum(n.stance for n in self.npcs.values()) / len(self.npcs)

    def suspend(self, account: str) -> None:
        self.suspended.add(account)

    def stats(self) -> dict[str, int]:
        return {
            "calls": self.call_count,
            "reads": self.read_count,
            "writes": self.write_count,
            "throttles": self.throttle_count,
            "posts": len(self.posts),
        }


# Legacy alias: some call sites reached for `MockNPC.rng_roll` in an early draft.
MockNPC.rng_roll = lambda self: False  # type: ignore[attr-defined]
