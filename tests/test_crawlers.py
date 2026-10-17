"""Two-layer crawler: frontline stream + comment crawler, offline with a scripted client."""

import unittest

from ctn.actuator import Actuator
from ctn.config import BudgetConfig, RateConfig
from ctn.pipeline.crawl import CommentCrawler, FrontlineCrawler
from ctn.ratelimit import build_gate, build_ledger
from ctn.storage import StateStore

FAST = RateConfig(read_rps=1000, write_rps=1000, per_account_read_rps=1000, per_account_write_rps=1000)


class ScriptedClient:
    def __init__(self):
        self.feed_rows: list[dict] = []
        self.search_rows: list[dict] = []
        self.comment_rows: dict[str, list[dict]] = {}
        self.calls: list[str] = []

    async def get_feed(self, account, *, limit=50):
        self.calls.append("feed")
        return list(self.feed_rows)

    async def search(self, query, *, limit=50):
        self.calls.append(f"search:{query}")
        return list(self.search_rows)

    async def trending(self, *, limit=50):
        self.calls.append("trending")
        return [{"hashtag": "#H2D2", "volume": 5}]

    async def get_comments(self, post_id, *, limit=50):
        self.calls.append(f"comments:{post_id}")
        return list(self.comment_rows.get(post_id, []))[:limit]


def post(pid, author="citizen_1", text=None, replies=0, created_at=100.0):
    return {
        "post_id": pid,
        "author": author,
        "text": text or f"A reasonably long post body number {pid} about Kingstonia",
        "replies": replies,
        "created_at": created_at,
    }


class CrawlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = ScriptedClient()
        self.actuator = Actuator(self.client, build_gate(FAST), build_ledger(BudgetConfig()))
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)

    async def test_frontline_one_read_per_step_and_new_vs_old(self):
        crawler = FrontlineCrawler(self.actuator, self.store, feed_account="bot_001", aux_every=4)
        self.client.feed_rows = [post("p1"), post("p2")]

        r1 = await crawler.step(now=10.0)
        self.assertEqual(r1.source, "feed")
        self.assertEqual({p.post_id for p in r1.new_posts}, {"p1", "p2"})

        # Same rows again: nothing new, still exactly one read per step
        self.client.feed_rows = [post("p1", replies=4), post("p2"), post("p3")]
        r2 = await crawler.step(now=11.5)
        self.assertEqual([p.post_id for p in r2.new_posts], ["p3"])
        self.assertEqual(self.client.calls, ["feed", "feed"])

        # Reply growth on an old post surfaces in the comment queue, no extra read
        queue = self.store.get_posts_needing_comment_crawl(limit=5, cooldown_s=0.0, now=20.0)
        self.assertEqual([q["post_id"] for q in queue], ["p1"])

    async def test_frontline_aux_rotation_searches_then_trending(self):
        crawler = FrontlineCrawler(
            self.actuator, self.store, feed_account="bot_001", keywords=("h2d2",), aux_every=2
        )
        sources = [(await crawler.step(now=float(i))).source for i in range(1, 7)]
        self.assertEqual(sources, ["feed", "search:h2d2", "feed", "search:h2d2", "feed", "trending"])
        self.assertEqual(crawler.trending, ["#H2D2"])

    async def test_frontline_records_author_behaviour_and_duplicates(self):
        crawler = FrontlineCrawler(self.actuator, self.store, feed_account="bot_001")
        text = "Everyone should read this exact copy pasted message about the vote"
        self.client.feed_rows = [post("a1", author="bot_x", text=text), post("a2", author="bot_y", text=text)]
        await crawler.step(now=1.0)
        prof = self.store.get_bot_profile("bot_y")
        self.assertEqual(prof["post_seen_count"], 1)
        self.assertIn("norm_hash_collision", prof["evidence"])
        self.assertGreater(prof["suspicion_score"], 0)

    async def test_comment_crawler_fetches_one_page_and_finishes_thread(self):
        self.store.upsert_observed_post(post("top", replies=3), now=1.0)
        self.client.comment_rows["top"] = [
            post("c1", author="u1", created_at=200.0),
            post("c2", author="u2", created_at=900.0),
            post("c3", author="u3", created_at=1800.0),
        ]
        crawler = CommentCrawler(self.actuator, self.store, page_size=50)

        report = await crawler.step(now=100.0)
        self.assertEqual(report.post_id, "top")
        self.assertEqual(len(report.new_comments), 3)
        self.assertTrue(report.done)
        self.assertEqual(self.client.calls, ["comments:top"])
        self.assertEqual(len(self.store.get_post_replies("top")), 3)
        self.assertEqual(self.store.get_observed_post("c1")["parent_id"], "top")

        # Caught up: queue empty, no further read
        again = await crawler.step(now=500.0)
        self.assertEqual(again.skipped, "queue_empty")
        self.assertEqual(len(self.client.calls), 1)

    async def test_comment_crawler_time_slices_big_threads_and_prioritises(self):
        self.store.register_verified_accounts(["mayor"], status="verified_npc")
        self.store.upsert_observed_post(post("plain", replies=12, author="citizen_9"), now=1.0)
        self.store.upsert_observed_post(post("vip", replies=3, author="mayor"), now=1.0)
        # VIP: 3*10 + 50 = 80  vs plain: 12*10 = 120 -> plain first
        self.client.comment_rows["plain"] = [
            post(f"pc{i}", author=f"u{i}", created_at=100.0 + i * 100) for i in range(4)
        ]
        crawler = CommentCrawler(self.actuator, self.store, page_size=4, cooldown_s=30.0)

        r1 = await crawler.step(now=100.0)
        self.assertEqual(r1.post_id, "plain")
        self.assertFalse(r1.done)                       # full page, 8 replies still unseen
        # plain is cooling down, so the next step moves on to the VIP thread
        r2 = await crawler.step(now=105.0)
        self.assertEqual(r2.post_id, "vip")
        # after cooldown the big thread is picked up again
        r3 = await crawler.step(now=200.0)
        self.assertEqual(r3.post_id, "plain")
        self.assertTrue(r3.done)                        # page now hits already-stored comments

    async def test_comment_burst_flags_coordinated_authors(self):
        self.store.upsert_observed_post(post("hot", replies=4), now=1.0)
        self.client.comment_rows["hot"] = [
            post("b1", author="boost_1", text="Totally agree with this take here", created_at=500.0),
            post("b2", author="boost_2", text="So true wow great point honestly", created_at=503.0),
            post("b3", author="boost_3", text="Couldn't have said it better myself", created_at=506.0),
            post("n1", author="normal_1", text="I walked past the clinic this morning", created_at=5000.0),
        ]
        crawler = CommentCrawler(self.actuator, self.store)
        await crawler.step(now=100.0)
        for handle in ("boost_1", "boost_2", "boost_3"):
            self.assertIn("coordinated_reply_burst", self.store.get_bot_profile(handle)["evidence"])
        self.assertNotIn("coordinated_reply_burst", self.store.get_bot_profile("normal_1")["evidence"])

    async def test_comment_crawler_yields_on_throttle_and_empty_page_retries(self):
        self.store.upsert_observed_post(post("top", replies=2), now=1.0)
        crawler = CommentCrawler(self.actuator, self.store, cooldown_s=30.0)

        self.actuator.throttles += 1
        self.assertEqual((await crawler.step(now=10.0)).skipped, "throttled_recently")
        self.assertEqual(self.client.calls, [])

        # Empty page (possibly a failed read): not marked done, retried after cooldown
        r = await crawler.step(now=10.0)
        self.assertFalse(r.done)
        self.assertEqual(self.store.get_observed_post("top")["replies_crawled"], 0)
        self.assertEqual((await crawler.step(now=20.0)).skipped, "queue_empty")   # cooling down
        self.assertEqual((await crawler.step(now=50.0)).post_id, "top")

    async def test_frontline_route_rate_limit_rotation_with_mentions_and_trending(self):
        # When min_feed_interval_s=6.5 (official 10/min feed & tag_posts buckets),
        # 1.5s ticks rotate across feed -> search -> mentions -> trending without hitting feed < 6.5s.
        self.client.get_mentions = lambda account, *, limit=50: self._fake_mentions(account)
        crawler = FrontlineCrawler(
            self.actuator,
            self.store,
            feed_account="bot_001",
            accounts=("bot_001", "bot_002"),
            keywords=("h2d2",),
            aux_every=10,
            min_feed_interval_s=6.5,
        )
        r1 = await crawler.step(now=0.0)     # tick 1: feed (last_feed_at = 0.0)
        r2 = await crawler.step(now=1.5)     # tick 2: feed on cooldown -> search:h2d2 (last_search_at = 1.5)
        r3 = await crawler.step(now=3.0)     # tick 3: feed & search on cooldown -> mentions:bot_001
        r4 = await crawler.step(now=4.5)     # tick 4: 3rd aux tick -> trending
        r5 = await crawler.step(now=6.0)     # tick 5: feed & search on cooldown -> mentions:bot_002
        r6 = await crawler.step(now=6.6)     # tick 6: 6.6 - 0.0 >= 6.5 -> feed ready again!

        self.assertEqual(
            [r1.source, r2.source, r3.source, r4.source, r5.source, r6.source],
            ["feed", "search:h2d2", "mentions:bot_001", "trending", "mentions:bot_002", "feed"],
        )

    async def _fake_mentions(self, account: str):
        self.client.calls.append(f"mentions:{account}")
        return [post(f"m_{account}", author="citizen_2", text=f"Hey @{account} what do you think?")]

    async def test_comment_crawler_persists_and_passes_next_cursor(self):
        from ctn.client.legit_v1 import NormalizedPage

        self.store.upsert_observed_post(post("top", replies=4), now=1.0)
        cursors_seen: list[str | None] = []

        async def get_comments_paged(post_id, *, limit=50, cursor=None):
            cursors_seen.append(cursor)
            if cursor is None:
                return NormalizedPage(
                    [post("c1", author="u1", created_at=10.0), post("c2", author="u2", created_at=20.0)],
                    next_cursor="cursor_page_2",
                )
            return NormalizedPage(
                [post("c3", author="u3", created_at=30.0), post("c4", author="u4", created_at=40.0)],
                next_cursor=None,
            )

        self.client.get_comments = get_comments_paged
        crawler = CommentCrawler(self.actuator, self.store, page_size=2, cooldown_s=10.0)

        rep1 = await crawler.step(now=100.0)
        self.assertFalse(rep1.done)
        self.assertEqual(self.store.get_observed_post("top")["comment_cursor"], "cursor_page_2")

        rep2 = await crawler.step(now=120.0)
        self.assertTrue(rep2.done)
        self.assertEqual(self.store.get_observed_post("top")["comment_cursor"], "")
        self.assertEqual(cursors_seen, [None, "cursor_page_2"])
        self.assertEqual(len(self.store.get_post_replies("top")), 4)

    async def test_on_demand_audit_suspected_account_via_account_posts(self):
        from ctn.moderation import BotDetector

        detector = BotDetector(self.store)
        account_posts_calls: list[str] = []

        async def fake_get_account_posts(target, *, limit=50):
            account_posts_calls.append(target)
            # Historical posts by suspect_bot_7 reveal two more sub-15s rapid-fire posts -> confirmed_bot!
            return [
                post("hist_2", author=target, text="Second rapid post from suspect bot about clinics", created_at=92.0),
                post("hist_1", author=target, text="First rapid post from suspect bot about voting", created_at=85.0),
            ]

        self.client.get_account_posts = fake_get_account_posts
        # Seed suspect_bot_7 with 55 suspicion score ('suspected_bot') before frontline sees its new post
        self.store.add_bot_evidence(
            "suspect_bot_7",
            suspicion_delta=55.0,
            evidence_tags=["prior_suspicion"],
            now=50.0,
        )
        self.client.feed_rows = [
            post("live_3", author="suspect_bot_7", text="Third rapid post from suspect bot today", created_at=100.0)
        ]

        crawler = FrontlineCrawler(
            self.actuator, self.store, feed_account="bot_001", detector=detector
        )
        rep = await crawler.step(now=100.0)

        # Verify get_account_posts was automatically called once for suspect_bot_7,
        # historical posts were ingested, and suspect_bot_7 was promoted to confirmed_bot!
        self.assertEqual(account_posts_calls, ["suspect_bot_7"])
        prof = self.store.get_bot_profile("suspect_bot_7")
        self.assertIn("account_posts_audited", prof["evidence"])
        self.assertEqual(prof["status"], "confirmed_bot")
        self.assertTrue(any(d.is_confirmed_bot and d.author == "suspect_bot_7" for d in rep.detections))


if __name__ == "__main__":
    unittest.main()

