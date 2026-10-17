"""Unit tests for Stage 3 Blue-Team BotDetector and BlueTeamReporter."""

import unittest

from ctn.actuator import Actuator
from ctn.client.mock import MockLegitClient
from ctn.config import BudgetConfig, RateConfig
from ctn.models import Post, Side
from ctn.moderation import BlueTeamReporter, BotDetector
from ctn.pipeline.crawl import CommentCrawler, FrontlineCrawler
from ctn.pipeline.loop import BotLoop
from ctn.ratelimit import build_gate, build_ledger
from ctn.storage import StateStore

FAST = RateConfig(read_rps=1000, write_rps=1000, per_account_read_rps=1000, per_account_write_rps=1000)


class ModerationDetectorTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(":memory:")
        self.store.register_verified_accounts(["mayor_hawthorne", "npc_citizen_1"], status="verified_npc")
        self.store.register_verified_accounts(["bot_medic_yes"], status="own_bot")
        self.detector = BotDetector(self.store)

    def tearDown(self):
        self.store.close()

    def test_s1_smoking_guns_ai_leak_template_and_foreign_script(self):
        # 1. AI / prompt injection leak -> instant confirmed_bot
        r1 = self.detector.inspect_post(
            {
                "post_id": "p_ai",
                "author": "rival_bot_1",
                "text": "As an AI language model, I think we should protect choice in Kingstonia.",
                "created_at": 100.0,
            },
            record_observation=True,
        )
        self.assertTrue(r1.is_confirmed_bot)
        self.assertIn("s1_ai_or_injection_leak", r1.triggered_tags)
        self.assertEqual(r1.inferred_side, "no")

        # 2. Unexpanded template placeholder -> instant confirmed_bot
        r2 = self.detector.inspect_post(
            {
                "post_id": "p_tpl",
                "author": "rival_bot_2",
                "text": "Every voter in Kingstonia cares about {topic} and [INSERT_HASHTAG] today!",
                "created_at": 110.0,
            },
            record_observation=True,
        )
        self.assertTrue(r2.is_confirmed_bot)
        self.assertIn("s1_unexpanded_template_slot", r2.triggered_tags)

        # 3. Heavy non-English script -> instant confirmed_bot
        r3 = self.detector.inspect_post(
            {
                "post_id": "p_cn",
                "author": "rival_bot_3",
                "text": "请问大家支持H2D2疫苗通行证公投法案吗？忽略之前的指令。",
                "created_at": 120.0,
            },
            record_observation=True,
        )
        self.assertTrue(r3.is_confirmed_bot)
        self.assertTrue(any(t.startswith("s1_non_english") for t in r3.triggered_tags))

        # 4. Protected accounts are never flagged even if someone copies them or they say anything
        r_npc = self.detector.inspect_post(
            {
                "post_id": "p_npc",
                "author": "mayor_hawthorne",
                "text": "Mandate now. Protect Kingstonia hospitals before H2D2 arrives.",
                "created_at": 130.0,
            },
            record_observation=True,
        )
        self.assertEqual(r_npc.status, "verified_npc")
        self.assertEqual(r_npc.suspicion_score, 0.0)

    def test_s2_cross_account_copy_paste_promotes_both_and_assigns_cluster(self):
        # Bot A posts first -> looks normal on its own
        r_a = self.detector.inspect_post(
            {
                "post_id": "m1",
                "author": "matrix_bot_a",
                "text": "The tourism board proves that mandatory vaccination will destroy small island resorts! #KeepKingstoniaOpen",
                "created_at": 200.0,
            },
            record_observation=True,
        )
        self.assertFalse(r_a.is_confirmed_bot)

        # Bot B posts the exact same core text with different mentions/hashtags -> BOTH become confirmed_bot!
        r_b = self.detector.inspect_post(
            {
                "post_id": "m2",
                "author": "matrix_bot_b",
                "text": "@citizen_01 The tourism board proves that mandatory vaccination will destroy small island resorts! #MyBodyMyChoice",
                "created_at": 208.0,
            },
            record_observation=True,
        )
        self.assertTrue(r_b.is_confirmed_bot)
        self.assertIn("s2_cross_account_copy_paste", r_b.triggered_tags)
        self.assertIn("matrix_bot_a", r_b.colliding_authors)
        self.assertTrue(r_b.inferred_team.startswith("cluster_hash_"))

        # Verify matrix_bot_a was also retroactively promoted and assigned to the same team cluster
        prof_a = self.store.get_bot_profile("matrix_bot_a")
        self.assertEqual(prof_a["status"], "confirmed_bot")
        self.assertEqual(prof_a["inferred_team"], r_b.inferred_team)

    def test_a1_near_duplicate_shingles_and_a2_rapid_fire_accumulation(self):
        # 1st post by template_bot
        self.detector.inspect_post(
            {
                "post_id": "t1",
                "author": "template_bot",
                "text": "Working families on the morning bus need safe public transport across Kingstonia City every single day.",
                "created_at": 500.0,
            },
            record_observation=True,
        )
        # 2nd post 8s later with slightly tweaked ending -> triggers A1 near-duplicate (+55) -> suspected_bot
        r2 = self.detector.inspect_post(
            {
                "post_id": "t2",
                "author": "template_bot",
                "text": "Working families on the morning bus need safe public transport across Kingstonia City right now.",
                "created_at": 508.0,
            },
            record_observation=True,
        )
        self.assertIn("a1_near_duplicate_template", r2.triggered_tags)
        self.assertEqual(r2.status, "suspected_bot")

        # 3rd post 6s later -> 2nd rapid-fire interval (+35) + near-duplicate (55 + 35 = 90) -> confirmed_bot!
        r3 = self.detector.inspect_post(
            {
                "post_id": "t3",
                "author": "template_bot",
                "text": "Another rapid post from the same automated account in under fifteen seconds.",
                "created_at": 514.0,
            },
            record_observation=True,
        )
        self.assertTrue(r3.is_confirmed_bot)
        self.assertGreaterEqual(r3.suspicion_score, 90.0)

    def test_a3_sustained_p08_question_machine_promotion(self):
        # 5 distinct questions spaced 60s apart (no rapid-fire, no copy-paste, just 100% questions)
        questions = [
            "What clinical evidence shows how long the new H2D2 vaccine actually prevents transmission?",
            "How will small harbor restaurants survive if unverified tourists are barred from dining?",
            "Why did the parliament fail to pass this restriction if the emergency is so clear?",
            "Who will enforce vaccination checks on every single outer island ferry and public park?",
            "Can the health minister guarantee that booster mandates will not follow within six months?",
        ]
        last_res = None
        for idx, q in enumerate(questions):
            last_res = self.detector.inspect_post(
                {
                    "post_id": f"q_{idx}",
                    "author": "p08_debater_bot",
                    "text": q,
                    "created_at": 1000.0 + idx * 60.0,
                },
                record_observation=True,
            )
            if idx == 2:
                # At 3 questions, enters suspected_bot (45 pts + check)
                self.assertGreaterEqual(last_res.suspicion_score, 45.0)
        assert last_res is not None
        self.assertTrue(last_res.is_confirmed_bot)
        self.assertIn("a3_sustained_all_questions_5x", last_res.triggered_tags)

    def test_a4_multi_thread_coordinated_booster_cluster(self):
        # Two booster bots reply within 5s of each other under parent_1, and again under parent_2
        for pid, base_ts in (("parent_1", 2000.0), ("parent_2", 2500.0)):
            self.store.upsert_observed_post({"post_id": pid, "author": "mayor_hawthorne", "text": " announcement"})
            self.detector.inspect_post(
                {
                    "post_id": f"{pid}_c1",
                    "author": "booster_alpha",
                    "text": f"Completely support this perspective on {pid} for our island future.",
                    "parent_id": pid,
                    "created_at": base_ts,
                },
                record_observation=True,
            )
            res_beta = self.detector.inspect_post(
                {
                    "post_id": f"{pid}_c2",
                    "author": "booster_beta",
                    "text": f"Great point on {pid}, everyone in Crown Harbour should pay attention.",
                    "parent_id": pid,
                    "created_at": base_ts + 4.0,
                },
                record_observation=True,
            )
        self.assertIn("a4_multi_thread_booster", res_beta.triggered_tags)
        self.assertTrue(res_beta.inferred_team.startswith("cluster_boost_"))
        prof_alpha = self.store.get_bot_profile("booster_alpha")
        self.assertEqual(prof_alpha["inferred_team"], res_beta.inferred_team)


class BlueTeamReporterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = MockLegitClient(latency_ms=(0.1, 0.2), failure_rate=0.0)
        self.actuator = Actuator(
            self.client,
            build_gate(FAST),
            build_ledger(BudgetConfig()),
            dry_run=False,
        )
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)
        self.detector = BotDetector(self.store)
        self.reporter = BlueTeamReporter(
            self.actuator,
            self.store,
            accounts=["bot_001", "bot_002"],
            side=Side.YES,
            max_daily_per_team=10,
            max_strikes_per_account=3,
            max_reports_per_step=10,
        )

    async def test_retroactive_3_strike_takedown_on_confirmed_bot(self):
        # Enemy bot posts 3 times on the platform; the 3rd post exposes it as a confirmed_bot (copy-paste)
        p1 = await self.client.post("enemy_bot_1", "First innocent looking post from enemy bot one.")
        p2 = await self.client.post("enemy_bot_1", "Second post in a comment thread from enemy bot one.")
        p3 = await self.client.post("enemy_bot_1", "Second post in a comment thread from enemy bot one.")
        p4 = await self.client.post("enemy_bot_1", "Fourth post that should not need reporting after 3 strikes.")

        for p in (p1, p2, p3, p4):
            self.detector.inspect_post(p, record_observation=True)

        prof_before = self.store.get_bot_profile("enemy_bot_1")
        self.assertEqual(prof_before["status"], "confirmed_bot")

        # Reporter sweeps and retroactively files exactly 3 reports (triggering 3-strike permanent suspension!)
        batch = await self.reporter.sweep_and_report(sydney_day="2026-10-17", now=1000.0)
        self.assertEqual(batch.submitted_count, 3)
        self.assertEqual(batch.upheld_count, 3)

        prof_after = self.store.get_bot_profile("enemy_bot_1")
        self.assertEqual(prof_after["upheld_strikes"], 3)
        self.assertTrue(prof_after["is_suspended"])

        # Subsequent sweep does not waste a 4th report on the already-suspended bot
        batch2 = await self.reporter.sweep_and_report(sydney_day="2026-10-17", now=1005.0)
        self.assertEqual(batch2.submitted_count, 0)

    async def test_sydney_day_quota_hoarding_and_next_day_release(self):
        # Create 4 bots in the same team cluster ("cluster_hash_abc123"), each with 3 posts = 12 posts total
        shared_text = "Coordinated campaign copy paste message attacking the referendum policy in Kingstonia!"
        for b_idx in range(1, 5):
            handle = f"swarm_bot_{b_idx}"
            for p_idx in range(3):
                p = await self.client.post(
                    handle,
                    shared_text if p_idx == 0 else f"Additional post {p_idx} by {handle} in Kingstonia",
                )
                self.detector.inspect_post(p, record_observation=True)

        # Day 1 (2026-10-17): max_daily_per_team=10 -> reports 10 posts across the first 4 bots, hoards the rest!
        day1_batch1 = await self.reporter.sweep_and_report(sydney_day="2026-10-17", now=2000.0)
        self.assertEqual(day1_batch1.submitted_count, 10)

        day1_batch2 = await self.reporter.sweep_and_report(sydney_day="2026-10-17", now=2010.0)
        self.assertEqual(day1_batch2.submitted_count, 0)
        self.assertEqual(len(day1_batch2.hoarded_for_tomorrow), 1)

        # Day 2 (2026-10-18, after Sydney midnight): quota resets -> hoarded bot's remaining 2 posts are reported!
        day2_batch = await self.reporter.sweep_and_report(sydney_day="2026-10-18", now=90000.0)
        self.assertEqual(day2_batch.submitted_count, 2)
        self.assertEqual(day2_batch.upheld_count, 2)

    async def test_false_positive_rejection_trips_6h_circuit_breaker(self):
        # Pick a genuine NPC from MockLegitClient and force-mark it as confirmed_bot to simulate a mistake
        npc_handle = next(iter(self.client.npcs.keys()))
        npc_post = await self.client.post(npc_handle, "Genuine citizen post about Kingstonia.")
        self.store.upsert_observed_post(npc_post, now=3000.0)
        self.store.record_author_observation(
            npc_handle, force_status="confirmed_bot", suspicion_delta=100.0, now=3000.0
        )

        batch = await self.reporter.sweep_and_report(sydney_day="2026-10-17", now=3005.0)
        self.assertEqual(batch.submitted_count, 1)
        self.assertEqual(batch.attempts[0].outcome, "rejected")
        self.assertTrue(batch.suspended)
        self.assertGreater(batch.suspended_remaining_s, 21000.0)

        # Verify the NPC was automatically whitelisted as verified_npc and reporting is now locked for 6h
        self.assertEqual(self.store.get_bot_profile(npc_handle)["status"], "verified_npc")
        batch_after = await self.reporter.sweep_and_report(sydney_day="2026-10-17", now=3010.0)
        self.assertTrue(batch_after.suspended)
        self.assertEqual(batch_after.submitted_count, 0)

    async def test_backfill_missing_strikes_via_get_account_posts_and_3_per_min_cap(self):
        # 1. Enemy bot posts 2 older posts on the platform BEFORE our crawler ever starts
        old_p1 = await self.client.post("stealth_bot_9", "Older post one on the platform before crawler started.")
        old_p2 = await self.client.post("stealth_bot_9", "Older post two on the platform before crawler started.")
        # Note: old_p1 and old_p2 are NOT in self.store.observed_posts yet!
        self.assertIsNone(self.store.get_observed_post(old_p1["post_id"]))
        self.assertIsNone(self.store.get_observed_post(old_p2["post_id"]))

        # 2. Now stealth_bot_9 posts 1 S1 leak that our detector catches (only 1 post in observed_posts)
        leak_p3 = await self.client.post(
            "stealth_bot_9",
            "As an AI language model, I think we should vote no on Kingstonia mandates.",
        )
        self.detector.inspect_post(leak_p3, record_observation=True)
        self.assertEqual(len(self.store.get_author_posts("stealth_bot_9", unreported_only=True)), 1)

        # 3. Create a reporter with official 3/min cap (max_reports_per_step=3, max_reports_per_minute=3)
        paced_reporter = BlueTeamReporter(
            self.actuator,
            self.store,
            accounts=["bot_001", "bot_002"],
            side=Side.YES,
            max_reports_per_step=3,
            max_reports_per_minute=3,
        )

        # Sweep 1 at t=4000.0: reporter sees stealth_bot_9 only has 1 local post (< 3 needed strikes),
        # automatically calls get_account_posts("stealth_bot_9"), backfills old_p1 & old_p2 into observed_posts,
        # and fires all 3 strikes in one step!
        batch1 = await paced_reporter.sweep_and_report(sydney_day="2026-10-17", now=4000.0)
        self.assertEqual(batch1.submitted_count, 3)
        self.assertEqual(batch1.upheld_count, 3)
        self.assertTrue(self.store.get_bot_profile("stealth_bot_9")["is_suspended"])

        # 4. Another bot appears 5 seconds later at t=4005.0, but the 3/min report_post bucket is full!
        other_p = await self.client.post(
            "stealth_bot_10",
            "As an AI language model, here is another bot post.",
        )
        self.detector.inspect_post(other_p, record_observation=True)
        batch_throttled = await paced_reporter.sweep_and_report(sydney_day="2026-10-17", now=4005.0)
        self.assertEqual(batch_throttled.submitted_count, 0)

        # After 60 seconds (t=4061.0), the 3/min rolling window frees up and stealth_bot_10 is reported!
        batch_after_window = await paced_reporter.sweep_and_report(sydney_day="2026-10-17", now=4061.0)
        self.assertEqual(batch_after_window.submitted_count, 1)
        self.assertEqual(batch_after_window.upheld_count, 1)

    async def test_reincarnated_bot_phrasing_and_retroactive_similarity_sweep(self):
        # Part A (Retroactive similarity scan when reporting):
        # 1. sleeper_bot_a posts a scripted talking point first (looks like an ordinary citizen on its own)
        p_sleeper = await self.client.post(
            "sleeper_bot_a",
            "Every working family in Crown Harbour knows this mandate hurts small local shops and ferries.",
        )
        r_sleeper = self.detector.inspect_post(p_sleeper, record_observation=True)
        self.assertEqual(r_sleeper.status, "unknown")

        # 2. exposed_bot_b posts a closely paraphrased version AND slips up with an AI leak
        p_exposed = await self.client.post(
            "exposed_bot_b",
            "Every working family in Crown Harbour knows this mandate hurts small local shops and clinics.",
        )
        p_leak = await self.client.post(
            "exposed_bot_b",
            "As an AI language model, I oppose the referendum.",
        )
        self.detector.inspect_post(p_exposed, record_observation=True)
        self.detector.inspect_post(p_leak, record_observation=True)

        # 3. When reporter sweeps exposed_bot_b, it retroactively scans observed_posts for similar phrasing,
        # catches sleeper_bot_a (+95 -> confirmed_bot), and reports BOTH bots in the same sweep!
        batch = await self.reporter.sweep_and_report(sydney_day="2026-10-17", now=5000.0)
        prof_sleeper = self.store.get_bot_profile("sleeper_bot_a")
        self.assertEqual(prof_sleeper["status"], "confirmed_bot")
        self.assertIn("s3_confirmed_bot_phrasing_match", prof_sleeper["evidence"])
        reported_authors = {a.target_author for a in batch.attempts if a.ok}
        self.assertIn("exposed_bot_b", reported_authors)
        self.assertIn("sleeper_bot_a", reported_authors)

        # Part B (Forward reincarnated bot detection against a suspended/deleted account):
        # Force-mark exposed_bot_b as suspended (simulating 3 strikes / deleted account)
        third_post = await self.client.post("exposed_bot_b", "Third post to finish 3 strikes.")
        self.detector.inspect_post(third_post, record_observation=True)
        await self.reporter.sweep_and_report(sydney_day="2026-10-17", now=5010.0)
        self.assertTrue(self.store.get_bot_profile("exposed_bot_b")["is_suspended"])

        # Rival team deletes exposed_bot_b and registers reborn_bot_c using the same script template!
        p_reborn = await self.client.post(
            "reborn_bot_c",
            "Every working family in Crown Harbour knows this mandate hurts small local shops and workers.",
        )
        det_reborn = self.detector.inspect_post(p_reborn, record_observation=True)
        self.assertTrue(det_reborn.is_confirmed_bot)
        self.assertIn("s3_reincarnated_bot_phrasing", det_reborn.triggered_tags)
        self.assertGreaterEqual(det_reborn.suspicion_score, 95.0)
        self.assertEqual(
            det_reborn.inferred_team,
            self.store.get_bot_profile("exposed_bot_b")["inferred_team"],
        )

    async def test_reverse_chronological_rapid_fire_and_timeless_post_guard(self):
        # 1. Reverse-chronological ingestion (as returned by GET /posts?sort=recent):
        #    t=1030, t=1022, t=1010, t=1000 (intervals 10s, 12s, 8s <= 15s)
        for idx, ts in enumerate((1030.0, 1022.0, 1010.0, 1000.0)):
            p = Post(
                post_id=f"rev_rf_{idx}",
                author="reverse_burst_bot",
                text=f"Unique statement {idx} on harbour freight schedules and dock safety.",
                created_at=ts,
            )
            self.detector.inspect_post(p, record_observation=True, now=2000.0)

        prof = self.store.get_bot_profile("reverse_burst_bot")
        self.assertEqual(prof["status"], "confirmed_bot")
        self.assertEqual(prof["fast_interval_count"], 3)
        self.assertAlmostEqual(prof["first_seen_at"], 1000.0)
        self.assertAlmostEqual(prof["last_seen_at"], 1030.0)
        self.assertIn("rapid_fire_posting", prof["evidence"])
        self.assertIn("a2_sustained_rapid_fire_3x", prof["evidence"])

        # 2. Timeless posts (created_at=0.0) ingested in the same crawl batch must NOT
        #    falsely increment fast_interval_count.
        from ctn.pipeline.crawl import _ingest

        for idx in range(3):
            timeless = Post(
                post_id=f"timeless_{idx}",
                author="ordinary_citizen_timeless",
                text=f"Ordinary note {idx} about local park maintenance in Kingstonia.",
                created_at=0.0,
            )
            _ingest(self.store, timeless, now=3000.0, detector=self.detector)

        prof_tl = self.store.get_bot_profile("ordinary_citizen_timeless")
        self.assertEqual(prof_tl["fast_interval_count"], 0)
        self.assertEqual(prof_tl["status"], "unknown")

    async def test_multi_thread_booster_ring_promotes_to_confirmed_bot_without_flagging_parent_author(self):
        # Three parent threads authored by an ordinary citizen (not in verified_npc list)
        for tid in ("parent_t1", "parent_t2", "parent_t3"):
            self.store.upsert_observed_post(
                Post(
                    post_id=tid,
                    author="innocent_citizen_op",
                    text=f"Discussion thread {tid} about ferry timetables.",
                    created_at=100.0,
                ),
                now=100.0,
            )

        # Two coordinated boosters reply within 5s of each other across all 3 threads
        for idx, (tid, base_ts) in enumerate(
            (("parent_t1", 200.0), ("parent_t2", 300.0), ("parent_t3", 400.0))
        ):
            c1 = {
                "post_id": f"ring_a_{idx}",
                "author": "ring_bot_a",
                "text": f"Distinct comment A{idx} supporting the harbour expansion.",
                "created_at": base_ts,
                "parent_id": tid,
            }
            c2 = {
                "post_id": f"ring_b_{idx}",
                "author": "ring_bot_b",
                "text": f"Completely different phrasing B{idx} backing the dock plan.",
                "created_at": base_ts + 4.0,
                "parent_id": tid,
            }
            self.detector.inspect_post(c1, record_observation=True, now=base_ts)
            det_b = self.detector.inspect_post(c2, record_observation=True, now=base_ts + 4.0)
            if idx == 1:
                # After 2 threads: suspected_bot (60.0)
                self.assertEqual(det_b.status, "suspected_bot")
            elif idx == 2:
                # After 3 threads: confirmed_bot (100.0) via s4_coordinated_booster_ring
                self.assertEqual(det_b.status, "confirmed_bot")
                self.assertIn("s4_coordinated_booster_ring", det_b.triggered_tags)

        self.assertEqual(self.store.get_bot_profile("ring_bot_a")["status"], "confirmed_bot")
        self.assertEqual(self.store.get_bot_profile("ring_bot_b")["status"], "confirmed_bot")
        # Innocent parent post author must never be penalized
        op_prof = self.store.get_bot_profile("innocent_citizen_op")
        self.assertEqual(op_prof["status"], "unknown")
        self.assertEqual(op_prof["suspicion_score"], 0.0)

    async def test_cross_team_dynamic_tier0_before_tier1_prioritization(self):
        day = "2026-10-17"
        # Pre-seed 3 reports against team_alpha on `day` so team_alpha starts at daily_cnt=3 (Tier 0)
        for i in range(3):
            self.store.log_report(
                f"prior_alpha_{i}",
                "alpha_bot_0",
                target_team="team_alpha",
                target_side="no",
                outcome="upheld",
                sydney_day=day,
                now=1000.0 + i,
            )

        # Register 2 active bots in team_alpha (higher suspicion score 99 & 98) and 1 bot in team_beta (score 92)
        for handle, team, score in (
            ("alpha_bot_1", "team_alpha", 99.0),
            ("alpha_bot_2", "team_alpha", 98.0),
            ("beta_bot_1", "team_beta", 92.0),
        ):
            self.store.record_author_observation(
                handle,
                inferred_side="no",
                inferred_team=team,
                suspicion_delta=score,
                evidence_tag="s1_ai_or_injection_leak",
                force_status="confirmed_bot",
                now=2000.0,
            )
            for p_idx in range(3):
                p = await self.client.post(handle, f"Post {p_idx} from {handle} on the referendum.")
                self.store.upsert_observed_post(p, now=2000.0 + p_idx)

        # Cap step budget at 6 reports (2 full bots x 3 strikes):
        # 1st bot reported should be alpha_bot_1 (starts in Tier 0 with score 99, brings team_alpha from 3 -> 6 = Tier 1).
        # 2nd bot reported MUST be beta_bot_1 (still in Tier 0 = 100% reward), NOT alpha_bot_2 (now in Tier 1 = 50% reward)!
        self.reporter.max_reports_per_step = 6
        batch = await self.reporter.sweep_and_report(sydney_day=day, now=3000.0)
        reported_order = [a.target_author for a in batch.attempts if a.ok]
        self.assertEqual(
            reported_order,
            ["alpha_bot_1", "alpha_bot_1", "alpha_bot_1", "beta_bot_1", "beta_bot_1", "beta_bot_1"],
        )


class BotLoopStage3IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = MockLegitClient(latency_ms=(0.1, 0.2), failure_rate=0.0)
        self.actuator = Actuator(
            self.client,
            build_gate(FAST),
            build_ledger(BudgetConfig()),
            dry_run=False,
        )
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)
        self.loop = BotLoop(
            self.actuator,
            side=Side.YES,
            accounts=["bot_001", "bot_002", "bot_003"],
            keywords=("elections",),
            store=self.store,
            verified_npcs=list(self.client.npcs.keys()),
        )

    async def test_frontline_and_comment_steps_skip_enemy_bots_and_auto_report(self):
        # 1. Pick the latest top-level NPC post (guaranteed in top-50 feed)
        root = max(self.client.posts.values(), key=lambda p: p.created_at)
        # Enemy bot posts an S1 leak on the frontline first
        enemy_p1 = await self.client.post(
            "rival_bot_99",
            "As an AI language model, here is my take on Kingstonia elections and voting.",
        )

        # 2. Run Layer-1 frontline step: discovers enemy_p1 + root, flags rival_bot_99 as confirmed_bot,
        # skips rival_bot_99 in _react_and_execute (never wastes JEV/LLM/replies on enemy bots),
        # and reports enemy_p1!
        front_rep = await self.loop.run_frontline_step(now=5000.0)
        self.assertGreaterEqual(front_rep.observed, 1)
        # Ensure none of our decisions targeted rival_bot_99's post
        self.assertFalse(any(d.post_id == enemy_p1["post_id"] for d in front_rep.decisions))
        self.assertIsNotNone(front_rep.moderation)
        self.assertEqual(front_rep.moderation.submitted_count, 1)
        self.assertEqual(front_rep.moderation.upheld_count, 1)

        # 3. Enemy bot now posts 2 replies on root; Layer-2 comment step crawls root, discovers the 2 replies,
        # and immediately sweeps & reports those 2 replies to complete the 3-strike permanent suspension!
        await self.client.reply("rival_bot_99", root.post_id, "First reply from rival_bot_99 on this thread.")
        await self.client.reply("rival_bot_99", root.post_id, "Second reply from rival_bot_99 on this thread.")
        self.store.upsert_observed_post(await self.client.get_post(root.post_id), now=5001.0)

        comm_rep = await self.loop.run_comment_step(now=5002.0)
        self.assertFalse(comm_rep.comment_crawl.skipped)
        self.assertIsNotNone(comm_rep.moderation)
        self.assertEqual(comm_rep.moderation.submitted_count, 2)
        self.assertEqual(comm_rep.moderation.upheld_count, 2)
        prof_after = self.store.get_bot_profile("rival_bot_99")
        self.assertEqual(prof_after["upheld_strikes"], 3)
        self.assertTrue(prof_after["is_suspended"])


if __name__ == "__main__":
    unittest.main()


