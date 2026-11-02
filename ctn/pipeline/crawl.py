"""Two-layer platform crawler feeding the SQLite full-platform mirror.

Layer 1 -- `FrontlineCrawler`: one read per tick (default every 1.5 s). Pulls the newest
top-level posts, upserts them into `observed_posts`, and returns the posts that were *new*.
It never paginates comments: a growing `replies` counter simply makes a post show up in
the comment queue (`StateStore.get_posts_needing_comment_crawl`).

Layer 2 -- `CommentCrawler`: one read per tick (default every 2.5 s). Picks the single most
valuable post with uncrawled replies, fetches one page of comments, upserts them with
`parent_id`, and persists pagination progress so big threads are time-sliced rather than
blocking the frontline.

Both layers go through the shared `Actuator` (one team-level read bucket; the public read
endpoints are not tied to any bot account), and both only *record* signals. Deciding who is
a bot and filing reports belongs to the detector/reporter stage.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from ..actuator import Actuator
from ..models import Post
from ..moderation.detector import BotDetector, DetectionResult
from ..storage import StateStore
from .perceive import DEFAULT_KEYWORDS, post_from_dict

# Suspicion deltas for cheap, content-agnostic signals recorded at ingest time.
DUPLICATE_TEXT_DELTA = 20.0
COORDINATED_BURST_DELTA = 15.0

# Priority weights for the comment crawl queue.
UNSEEN_REPLY_WEIGHT = 10.0
VIP_BONUS = 50.0
OWN_POST_BONUS = 40.0


def _is_question(text: str) -> bool:
    return text.rstrip().endswith("?")


def _store_post(store: StateStore, post: Post, *, now: float) -> tuple[bool, dict[str, Any]]:
    """Storage half of `_ingest`: upsert the post and the author's counters, no detection.

    Deliberately separate from `_inspect_post` so a crawler can commit the page and *then* run the
    detector. `BotDetector.inspect_post` reads a lot and writes single evidence rows; keeping it
    inside the page transaction held the write lock for up to 44s and stalled every other writer
    (2026-10-20). Returns `(changed, record)`.
    """
    is_new, rec = store.upsert_observed_post(post, now=now)
    if not is_new and not rec.get('content_changed'):
        return False, rec
    if post.author and (post.raw.get('verified') is True or post.raw.get('platform_level')=='verified'):
        if store.get_bot_profile(post.author)['status']!='own_bot': store.register_verified_accounts([post.author])
    if post.author and is_new:
        store.record_author_observation(post.author,platform_account_id=int(rec.get('author_id') or 0),
            is_question=_is_question(post.text),has_valid_timestamp=post.created_at>0,
            now=post.created_at if post.created_at>0 else now)
    return True, rec


def _inspect_post(store: StateStore, post: Post, *, now: float, detector: BotDetector | None) -> DetectionResult | None:
    """Detection half of `_ingest`; every write inside it is its own short transaction."""
    if detector is None:
        return None
    return detector.inspect_post(post, record_observation=False, now=now)


def _ingest(
    store: StateStore,
    post: Post,
    *,
    now: float,
    extra_delta: float = 0.0,
    extra_tag: str = "",
    detector: BotDetector | None = None,
) -> tuple[bool, dict[str, Any], DetectionResult | None]:
    """Upsert one post, then detect on it. Callers inside a page transaction use the two halves."""
    changed, rec = _store_post(store, post, now=now)
    if not changed:
        return False, rec, None
    return True, rec, _inspect_post(store, post, now=now, detector=detector)


async def audit_suspected_account(
    actuator: Actuator,
    store: StateStore,
    author: str,
    *,
    detector: BotDetector | None = None,
    limit: int = 20,
    now: float | None = None,
) -> list[DetectionResult]:
    """On-demand deep audit of a `suspected_bot` via `/accounts/{id}/posts` (300/min read bucket).

    Called at most once per account (guarded by the `'account_posts_audited'` evidence tag in
    `bot_registry`). Pulls up to `limit` historical posts by `author` so `BotDetector` can
    immediately verify whether behavioral patterns (rapid-fire intervals, template paraphrasing,
    100% question-machine posts, or hash collisions) confirm the account as a bot.
    """
    clean_author = str(author or "").strip()
    if not clean_author:
        return []
    prof = store.get_bot_profile(clean_author)
    if prof["status"] != "suspected_bot" or (now or time.time())-store.policy("audit:"+clean_author,0)<300:
        return []
    if getattr(actuator._client, "get_account_posts", None) is None:
        return []

    ts = time.time() if now is None else float(now)
    rows = await actuator.get_account_posts(clean_author, limit=limit)
    if rows: store.set_policy('audit:'+clean_author,ts)
    detections: list[DetectionResult] = []
    def store_history():
        fresh: list[Post] = []
        with store.batch_transaction():
            for row in rows:
                if not isinstance(row, dict):
                    continue
                post = post_from_dict(row)
                if post is None:
                    continue
                changed, _ = _store_post(store, post, now=ts)
                if changed:
                    fresh.append(post)
        return fresh

    for post in await asyncio.to_thread(store_history):
        det = await asyncio.to_thread(_inspect_post, store, post, now=ts, detector=detector)
        if det is not None:
            detections.append(det)
    return detections


# --------------------------------------------------------------------------- #
# Layer 1
# --------------------------------------------------------------------------- #
@dataclass
class FrontlineReport:
    tick: int
    source: str = ""                       # "feed" | "search:<q>" | "trending" | "mentions:<acct>"
    fetched: int = 0
    new_posts: list[Post] = field(default_factory=list)
    detections: list[DetectionResult] = field(default_factory=list)


class FrontlineCrawler:
    """One platform read per `step()`.

    Respects official Legit Social route rate limits when `min_feed_interval_s > 0`:
    - `feed` (`feed_latest` / `feed_trending` / `feed_home`): 10 req/min shared bucket (>= 6.5s cooldown).
    - `search:<q>` (`tag_posts`): 10 req/min independent bucket (>= 6.5s cooldown).
    - `trending` (`popular_tags`): 300 req/min bucket.
    - `mentions:<account>` (`notifications`): 300 req/min bucket.
    """

    def __init__(
        self,
        actuator: Actuator,
        store: StateStore,
        *,
        feed_account: str,
        accounts: Sequence[str] = (),
        keywords: Sequence[str] = DEFAULT_KEYWORDS,
        feed_limit: int | None = None,
        aux_every: int = 10,
        min_feed_interval_s: float | None = None,
        audit_suspects: bool = True,
        detector: BotDetector | None = None,
        trending_interval_s: float = 60.0,
        trending_feed_every: int | None = None,
        search_sort: str | None = None,
    ) -> None:
        from .targeting import POLICY as TARGETING
        self.actuator = actuator
        self.store = store
        self.feed_account = feed_account
        # popular_tags refreshes about once a minute; reading it more often wastes read budget.
        self.trending_interval_s = max(0.0, float(trending_interval_s))
        self._last_trending_at: float | None = None
        self.accounts = [str(a).strip() for a in accounts if str(a).strip()]
        self.keywords = [str(k).strip() for k in keywords if str(k).strip()]
        self.feed_limit = int(feed_limit or TARGETING['feed']['page_limit'])
        self.backlog_max_age_s = float(TARGETING['feed']['backlog_max_age_seconds'])
        self.aux_every = max(2, aux_every)
        is_official = actuator._client.__class__.__name__ == "OfficialLegitClient"
        # Every Nth feed read takes the ranked (trending) feed: what citizens actually see, with fresh counts.
        self.trending_feed_every = int(trending_feed_every if trending_feed_every is not None else
                                       (TARGETING['feed']['trending_every'] if is_official else 0))
        self.search_sort = search_sort if search_sort is not None else ('trending' if is_official else None)
        self._feed_reads = 0
        if min_feed_interval_s is None:
            self.min_feed_interval_s = 6.5 if is_official else 0.0
        else:
            self.min_feed_interval_s = max(0.0, float(min_feed_interval_s))
        self.audit_suspects = audit_suspects
        self.detector = detector
        self.trending: list[str] = []
        self.tick = 0
        self._aux_idx = 0
        self._search_idx = 0
        self._mention_idx = 0
        self._last_feed_at: float | None = None
        self._last_search_at: float | None = None

    def _choose_source(self, now: float) -> str:
        feed_ready = (
            self.min_feed_interval_s <= 0.0
            or self._last_feed_at is None
            or (now - self._last_feed_at) >= self.min_feed_interval_s
        )
        if self.tick % self.aux_every != 0 and feed_ready:
            self._last_feed_at = now
            self._feed_reads += 1
            if self.trending_feed_every and self._feed_reads % self.trending_feed_every == 0:
                return "feed_trending"
            return "feed"

        self._aux_idx += 1
        queries = self.keywords + self.trending[:2]
        trending_ready = (self._last_trending_at is None
                          or now - self._last_trending_at >= self.trending_interval_s)
        # Every 3rd aux tick refreshes trending when it is due; the others rotate through
        # search queries, then per-account notifications.
        if (self._aux_idx % 3 == 0 or not queries) and trending_ready:
            self._last_trending_at = now
            return "trending"

        search_ready = (
            self.min_feed_interval_s <= 0.0
            or self._last_search_at is None
            or (now - self._last_search_at) >= self.min_feed_interval_s
        )
        if search_ready and queries:
            query = queries[self._search_idx % len(queries)]
            self._search_idx += 1
            self._last_search_at = now
            return f"search:{query}"

        active_accounts = [a for a in self.accounts if a not in self.actuator.suspended]
        if active_accounts:
            acct = active_accounts[self._mention_idx % len(active_accounts)]
            self._mention_idx += 1
            return f"mentions:{acct}"
        if trending_ready:
            self._last_trending_at = now
            return "trending"
        return "idle"

    async def step(self, *, now: float | None = None) -> FrontlineReport:
        self.tick += 1
        ts = time.time() if now is None else float(now)
        report = FrontlineReport(tick=self.tick, source=self._choose_source(ts))

        if report.source == "idle":
            return report
        if report.source == "trending":
            rows = await self.actuator.trending(limit=10)
            self.trending = [str(r["hashtag"]) for r in rows if isinstance(r, dict) and r.get("hashtag")]
            return report
        if report.source == "feed_trending":
            rows = await self.actuator.trending_posts(limit=self.feed_limit)
            if getattr(rows,'read_failed',False):return report
            from .targeting import mark_trending
            await asyncio.to_thread(mark_trending,self.store,[r.get('post_id') for r in rows if isinstance(r,dict)],ts)
            return await self._ingest_page(report,rows,ts)
        key='crawl:'+report.source
        state=self.store.policy(key,{})
        # Alternate a fresh head with continuation pages so a backlog never hides new posts.
        cursor=state.get('cursor') if state.get('continue_next') else None
        if report.source == "feed":
            rows = await self.actuator.get_feed(self.feed_account, limit=self.feed_limit,cursor=cursor)
        elif report.source.startswith("mentions:"):
            acct = report.source.split(":", 1)[1]
            rows = await self.actuator.get_mentions(acct, limit=self.feed_limit,cursor=cursor)
        else:
            rows = await self.actuator.search(report.source.split(":", 1)[1], limit=self.feed_limit,cursor=cursor,sort=self.search_sort)
        if getattr(rows,'read_failed',False):return report
        next_cursor=getattr(rows,'next_cursor',None)
        ids={str(row.get('post_id','')) for row in rows if isinstance(row,dict)}
        if cursor:
            if state.get('stop_at') in ids:next_cursor=None
            # In a posting flood the backlog can never be caught up: give it up once it is stale.
            stamps=[float(r.get('created_at') or 0) for r in rows if isinstance(r,dict) and r.get('created_at')]
            if stamps and max(stamps)<ts-self.backlog_max_age_s:next_cursor=None
            state={'cursor':next_cursor,'continue_next':False,'head':state.get('head'),'stop_at':state.get('stop_at')}
        else:
            previous=state.get('head')
            backlog=state.get('cursor') or (next_cursor if previous not in ids else None)
            state={'cursor':backlog,'continue_next':bool(backlog),'head':str(rows[0].get('post_id','')) if rows else previous,'stop_at':state.get('stop_at') if state.get('cursor') else previous}
        self.store.set_policy(key,state)
        return await self._ingest_page(report,rows,ts)

    async def _ingest_page(self, report, rows, ts):
        hydrated=[]
        for row in rows:
            if row.get('text_complete') is False:
                full=await self.actuator.get_post(str(row.get('post_id','')))
                if full and full.get('post_id'):
                    row={**row,**full,'text_complete':full.get('text_complete',True)}
            hydrated.append(row)
        rows=hydrated

        suspect_authors: list[str] = []

        def store_rows():
            """One short write transaction per page: storage only, no detection."""
            fresh: list[Post] = []
            with self.store.batch_transaction():
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    post = post_from_dict(row)
                    if post is None:
                        continue
                    report.fetched += 1
                    changed, _ = _store_post(self.store, post, now=ts)
                    if changed:
                        fresh.append(post)
            return fresh

        fresh = await asyncio.to_thread(store_rows)
        # Detection runs after the page is committed: it reads many rows and writes its own evidence
        # rows, and inside the page transaction it stretched the write lock to tens of seconds.
        for post in fresh:
            det = await asyncio.to_thread(_inspect_post, self.store, post, now=ts, detector=self.detector)
            report.new_posts.append(post)
            if det is not None:
                report.detections.append(det)
                if det.is_suspected_bot and post.author and post.author not in suspect_authors:
                    suspect_authors.append(post.author)

        if self.audit_suspects and self.detector is not None and suspect_authors:
            for suspect in suspect_authors:
                extra_dets = await audit_suspected_account(
                    self.actuator, self.store, suspect, detector=self.detector, now=ts
                )
                report.detections.extend(extra_dets)

        return report


# --------------------------------------------------------------------------- #
# Layer 2
# --------------------------------------------------------------------------- #
@dataclass
class CommentReport:
    post_id: str = ""
    skipped: str = ""                      # non-empty when no read was made
    fetched: int = 0
    new_comments: list[Post] = field(default_factory=list)
    detections: list[DetectionResult] = field(default_factory=list)
    done: bool = False                     # thread fully caught up


class CommentCrawler:
    """One `get_comments` read per `step()` on the highest-priority post with uncrawled replies."""

    def __init__(
        self,
        actuator: Actuator,
        store: StateStore,
        *,
        page_size: int = 50,
        candidates: int = 15,
        cooldown_s: float = 30.0,
        max_read_pressure: float = 0.85,
        burst_window_s: float = 10.0,
        burst_min_authors: int = 3,
        audit_suspects: bool = True,
        detector: BotDetector | None = None,
    ) -> None:
        self.actuator = actuator
        self.store = store
        self.page_size = page_size
        self.candidates = candidates
        self.cooldown_s = cooldown_s
        self.max_read_pressure = max_read_pressure
        self.burst_window_s = burst_window_s
        self.burst_min_authors = burst_min_authors
        self.audit_suspects = audit_suspects
        self.detector = detector
        self._throttles_seen = actuator.throttles

    def _priority(self, rec: dict[str, Any]) -> float:
        # Logarithmic in unseen replies so one exploding thread cannot starve the rest.
        from .targeting import POLICY as TARGETING, is_trending
        bonus = TARGETING['comments']
        score = UNSEEN_REPLY_WEIGHT * math.log1p(float(rec.get("unseen_replies", 0)))
        status = self.store.get_bot_profile(str(rec.get("author", "")))["status"]
        if status == "verified_npc":
            score += bonus['verified_bonus']
        elif status == "own_bot":
            score += bonus['own_thread_bonus']
        if is_trending(self.store, str(rec.get('post_id', ''))):
            score += bonus['trending_bonus']
        return score

    def _depth_exhausted(self, pid: str, now: float) -> bool:
        """Read at most N pages per thread per hour; beyond that the thread's backlog is given up."""
        from .targeting import POLICY as TARGETING
        key = 'cpages:' + pid
        state = self.store.policy(key, {}) or {}
        hour = int(now // 3600)
        pages = int(state.get('n', 0)) if state.get('hour') == hour else 0
        if pages >= TARGETING['comments']['pages_per_post_per_hour']:
            return True
        self.store.set_policy(key, {'hour': hour, 'n': pages + 1})
        return False

    def _burst_authors(self, posts: list[Post]) -> set[str]:
        """Authors who replied to the same thread within a short window (>= N distinct authors)."""
        timed = sorted((p for p in posts if p.created_at > 0 and p.author), key=lambda p: p.created_at)
        flagged: set[str] = set()
        for i, first in enumerate(timed):
            window = {
                p.author for p in timed[i:] if p.created_at - first.created_at <= self.burst_window_s
            }
            if len(window) >= self.burst_min_authors:
                flagged |= window
        return flagged

    async def step(self, *, now: float | None = None) -> CommentReport:
        ts = time.time() if now is None else float(now)

        # Back off: never compete with the frontline for scarce read budget.
        if self.actuator.throttles != self._throttles_seen:
            self._throttles_seen = self.actuator.throttles
            return CommentReport(skipped="throttled_recently")
        if self.actuator.ledger.read_pressure > self.max_read_pressure:
            return CommentReport(skipped="read_pressure_high")

        queue = self.store.get_posts_needing_comment_crawl(
            limit=self.candidates, cooldown_s=self.cooldown_s, now=ts
        )
        if not queue:
            return CommentReport(skipped="queue_empty")

        target = max(queue, key=self._priority)
        pid = str(target["post_id"])
        total_replies = int(target["replies"])
        cursor = str(target.get("comment_cursor") or "").strip() or None
        report = CommentReport(post_id=pid)
        if self._depth_exhausted(pid, ts):
            # Mark caught up; new replies re-queue the thread and the next hour gets a fresh allowance.
            self.store.update_comment_crawl_state(pid, replies_crawled=total_replies, comment_cursor='', now=ts)
            report.skipped = 'depth_cap'
            return report

        rows = await self.actuator.get_comments(pid, limit=self.page_size, cursor=cursor)
        next_cursor = getattr(rows, "next_cursor", None)
        parsed: list[Post] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row = dict(row)
            if not row.get("parent_id"): row["parent_id"]=pid
            row.setdefault("root_post_id",pid)
            post = post_from_dict(row)
            if post is not None:
                parsed.append(post)
        report.fetched = len(parsed)

        # `coordinated_reply_burst` was never actually applied (`_ingest` accepted the delta and
        # dropped it), so the burst set is no longer computed; BotDetector has its own burst rule.
        hit_existing = False
        suspect_authors: list[str] = []
        def store_comments():
            """Short write transaction: storage only (see `_store_post`)."""
            nonlocal hit_existing
            fresh: list[Post] = []
            with self.store.batch_transaction():
                for post in parsed:
                    changed, _ = _store_post(self.store, post, now=ts)
                    if changed:
                        fresh.append(post)
                    else:
                        hit_existing = True
            return fresh

        for post in await asyncio.to_thread(store_comments):
            report.new_comments.append(post)
            det = await asyncio.to_thread(_inspect_post, self.store, post, now=ts, detector=self.detector)
            if det is not None:
                report.detections.append(det)
                if det.is_suspected_bot and post.author and post.author not in suspect_authors:
                    suspect_authors.append(post.author)

        if self.audit_suspects and self.detector is not None and suspect_authors:
            for suspect in suspect_authors:
                extra_dets = await audit_suspected_account(
                    self.actuator, self.store, suspect, detector=self.detector, now=ts
                )
                report.detections.extend(extra_dets)

        has_cursor_attr = hasattr(rows, "next_cursor")
        caught_up_count = (int(target["replies_crawled"]) + len(report.new_comments)) >= total_replies
        if getattr(rows,'read_failed',False):
            report.skipped='read_failed'
            self.store.update_comment_crawl_state(pid,add_crawled=0,now=ts)
        elif next_cursor:
            self.store.update_comment_crawl_state(pid,add_crawled=len(report.new_comments),
                comment_cursor=str(next_cursor),now=ts)
        elif has_cursor_attr or len(parsed)<self.page_size:
            report.done=True
            self.store.update_comment_crawl_state(pid,replies_crawled=max(total_replies,int(target['replies_crawled'])),
                comment_cursor='',now=ts)
        else:
            # Legacy adapters expose offset pagination; duplicates never prove completion.
            offset=int(cursor) if cursor and cursor.isdigit() else 0
            self.store.update_comment_crawl_state(pid,add_crawled=len(report.new_comments),
                comment_cursor=str(offset+len(parsed)),now=ts)
        return report


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #
async def run_periodic(
    step: Callable[[], Awaitable[Any]],
    *,
    interval_s: float,
    stop: asyncio.Event,
    on_result: Callable[[Any], Awaitable[None]] | None = None,
) -> None:
    """Call `step` every `interval_s` (measured start-to-start) until `stop` is set."""
    while not stop.is_set():
        started = time.monotonic()
        result = await step()
        if on_result is not None:
            await on_result(result)
        remaining = max(0.0, interval_s - (time.monotonic() - started))
        try:
            await asyncio.wait_for(stop.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass
