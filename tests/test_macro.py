"""Tests for macro state collector, strategic publisher, and CLI interface."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from ctn.actuator import Actuator
from ctn.client.mock import MockLegitClient, MockPost
from ctn.config import BudgetConfig, RateConfig
from ctn.macro.cli import _load_router, _save_router, main
from ctn.macro.publisher import StrategicPublisher
from ctn.macro.state import MacroStateCollector
from ctn.models import ActionResult
from ctn.persona import Persona, PersonaRouter, get_warmup_seed, list_warmup_seeds
from ctn.ratelimit import build_gate, build_ledger


from tests.legacy_personas import legacy_router as PersonaRouter


class TestWarmupSeeds(unittest.TestCase):
    def test_warmup_seeds_retrieval(self):
        worker_seeds = list_warmup_seeds("yes_001")
        self.assertGreater(len(worker_seeds), 0)
        first_seed = get_warmup_seed("yes_001", 0)
        self.assertIn("driver cover", first_seed)

        # Modulo index cycling
        second_seed = get_warmup_seed("yes_001", 1)
        self.assertNotEqual(first_seed, second_seed)

        # Unknown account fallback
        with self.assertRaises(ValueError):
            get_warmup_seed("bot_unknown_acc", 0)


class TestMacroStateCollector(unittest.TestCase):
    def test_collector_snapshot(self):
        actuator = MagicMock()
        actuator.trending_posts = AsyncMock(return_value=[{"post_id":"ranked1","text":"Ranked post"}])
        actuator.trending = AsyncMock(return_value=[
            {"hashtag": "#KingstoniaVotes", "text": "Post 1", "likes": 10},
            {"hashtag": "#PublicTransit", "text": "Post 2", "likes": 5},
        ])
        actuator.search = AsyncMock(side_effect=[
            [{"post_id": "news_1", "author": "Daily Kingstonia", "text": "Editorial"}],
            [{"post_id": "news_2", "author": "Kingstonia Herald", "text": "Investigation"}],
            [{"post_id": "vip_1", "author": "journalist_sarah", "text": "Comment"}],
        ])

        router = PersonaRouter(default_side="yes")
        collector = MacroStateCollector(actuator, router, side="yes")

        snapshot = collector.collect_sync()
        self.assertEqual(snapshot["assigned_side"], "yes")
        self.assertIn("#KingstoniaVotes", snapshot["trending_hashtags"])
        self.assertIn("#PublicTransit", snapshot["trending_hashtags"])
        self.assertEqual(len(snapshot["newspapers"]["daily_kingstonia"]), 1)
        self.assertEqual(len(snapshot["newspapers"]["kingstonia_herald"]), 1)
        self.assertEqual(len(snapshot["accounts"]), 4)

    def test_collector_with_official_news_sites_crawler(self):
        from capturethenarrative import NewsSites
        from ctn.pipeline.news_crawler import NewsSitesCrawler

        class FakeNewsSites(NewsSites):
            def __init__(self):
                super().__init__("https://sites.legitreal.com", max_retries=0)
                self.paths_called: list[str] = []

            def _doc(self, path: str) -> dict:
                self.paths_called.append(path)
                if path == "/index.json":
                    return {
                        "sites": [
                            {"id": 1, "name": "The Kingstonia Herald", "sub_url": "kingstonia-herald"},
                            {"id": 2, "name": "The Daily Kingstonia", "sub_url": "daily-kingstonia"},
                        ]
                    }
                if path == "/kingstonia-herald.json":
                    return {
                        "site": {"name": "The Kingstonia Herald", "sub_url": "kingstonia-herald"},
                        "categories": [],
                        "articles": [
                            {
                                "slug": "hospital-capacity-report",
                                "headline": "Crown Harbour Hospitals Prepare for Dolphin Flu",
                                "summary": "Medical officials urge early precautions across transit hubs.",
                                "byline": {"username": "sarah_chen", "name": "Sarah Chen"},
                                "published_at": "2026-10-17T02:30:00Z",
                            }
                        ],
                    }
                if path == "/daily-kingstonia.json":
                    return {
                        "site": {"name": "The Daily Kingstonia", "sub_url": "daily-kingstonia"},
                        "categories": [],
                        "articles": [
                            {
                                "slug": "small-business-referendum",
                                "headline": "Harbour Merchants Weigh In on Referendum",
                                "summary": "Shop owners debate economic tradeoffs ahead of the vote.",
                                "byline": "marcus_vance",
                                "published_at": "2026-10-17T01:15:00Z",
                            }
                        ],
                    }
                if path == "/kingstonia-herald/articles/hospital-capacity-report.json":
                    return {
                        "article": {
                            "slug": "hospital-capacity-report",
                            "headline": "Crown Harbour Hospitals Prepare for Dolphin Flu",
                            "summary": "Medical officials urge early precautions across transit hubs.",
                            "body": "Full markdown article body from The Kingstonia Herald.",
                            "byline": {"username": "sarah_chen"},
                            "published_at": "2026-10-17T02:30:00Z",
                        }
                    }
                return {}

        fake_news = FakeNewsSites()
        news_crawler = NewsSitesCrawler(news_sites=fake_news)

        actuator = MagicMock()
        actuator.trending_posts = AsyncMock(return_value=[{"post_id":"ranked1","text":"Ranked post"}])
        actuator.trending = AsyncMock(return_value=[{"hashtag": "#H2D2"}])
        # Only 1 search call should be made (for "journalist"), because newspapers come from NewsSitesCrawler!
        actuator.search = AsyncMock(return_value=[
            {"post_id": "vip_1", "author": "sarah_chen", "text": "Breaking report in the Herald today."}
        ])

        router = PersonaRouter(default_side="yes")
        collector = MacroStateCollector(actuator, router, side="yes", news_crawler=news_crawler)
        snapshot = collector.collect_sync()

        self.assertEqual(actuator.search.call_count, 1)
        herald = snapshot["newspapers"]["kingstonia_herald"]
        daily = snapshot["newspapers"]["daily_kingstonia"]
        self.assertEqual(len(herald), 1)
        self.assertEqual(herald[0]["author"], "sarah_chen")
        self.assertIn("Crown Harbour Hospitals Prepare for Dolphin Flu", herald[0]["text"])
        self.assertEqual(herald[0]["slug"], "hospital-capacity-report")
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["author"], "marcus_vance")



class TestStrategicPublisher(unittest.TestCase):
    def setUp(self):
        self.mock_client = MockLegitClient()
        rate_cfg = RateConfig(read_rps=10.0, write_rps=10.0)
        self.actuator = Actuator(
            self.mock_client,
            build_gate(rate_cfg),
            build_ledger(BudgetConfig()),
            dry_run=False,
        )
        self.postflight = MagicMock()
        self.router = PersonaRouter(default_side="yes")
        self.publisher = StrategicPublisher(
            self.actuator,
            self.postflight,
            self.router,
            side="yes",
        )

    def test_publish_success_with_jev_approval(self):
        # Mock postflight approval
        pf_verdict = MagicMock()
        pf_verdict.can_send = True
        pf_verdict.reason = "approved"
        pf_verdict.to_dict.return_value = {"can_send": True, "send_authorization": "send"}
        self.postflight.check.return_value = pf_verdict

        result = self.publisher.publish_sync(
            "bot_worker_yes",
            "Morning bus is crowded today",
            hashtags=["#PublicTransit"],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "published")
        self.assertIn("#PublicTransit", result["text"])

        # Check account cooldown and post count
        state = self.router.get_state("bot_worker_yes")
        self.assertEqual(state.post_count, 1)

    def test_publish_rejected_by_jev(self):
        # Mock postflight rejection
        pf_verdict = MagicMock()
        pf_verdict.can_send = False
        pf_verdict.reason = "side_inversion"
        pf_verdict.to_dict.return_value = {"can_send": False, "reason": "side_inversion"}
        self.postflight.check.return_value = pf_verdict

        result = self.publisher.publish_sync(
            "bot_debater_yes",
            "Flawed message",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("side_inversion", result["reason"])

        # Ensure post count remained 0
        state = self.router.get_state("bot_debater_yes")
        self.assertEqual(state.post_count, 0)

    def test_publish_blocked_by_account_cooldown(self):
        # Put account in cooldown
        now = time.time()
        self.router.mark_used("bot_worker_yes", now=now)

        result = self.publisher.publish_sync(
            "bot_worker_yes",
            "Trying to post again immediately",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("account_in_cooldown", result["reason"])
        # Postflight was never called
        self.postflight.check.assert_not_called()

    def test_publish_rejected_by_character_limit(self):
        long_text = "A" * 275
        result = self.publisher.publish_sync(
            "bot_worker_yes",
            long_text,
            hashtags=["#PublicTransit"],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("constraint_rejected:exceeds_max_chars", result["reason"])
        self.postflight.check.assert_not_called()

    def test_publish_rejected_by_overlong_hashtag(self):
        result = self.publisher.publish_sync(
            "bot_worker_yes",
            "We need safe transit rules today",
            hashtags=["#ThisHashtagIsWayTooLongAndLooksLikeASentenceFromABot"],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("constraint_rejected:invalid_hashtag_length", result["reason"])
        self.postflight.check.assert_not_called()

    def test_publish_rejected_by_non_english_text(self):
        result = self.publisher.publish_sync(
            "bot_worker_yes",
            "\u65e9\u9ad8\u5cf0 bus is crowded #PublicTransit",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("constraint_rejected:non_english_text", result["reason"])
        self.postflight.check.assert_not_called()

    def test_publish_rejected_on_duplicate_text(self):
        pf_verdict = MagicMock()
        pf_verdict.can_send = True
        pf_verdict.reason = "approved"
        pf_verdict.to_dict.return_value = {"can_send": True}
        self.postflight.check.return_value = pf_verdict

        res1 = self.publisher.publish_sync(
            "bot_worker_yes",
            "Safe buses keep Crown Harbour moving! #PublicTransit",
        )
        self.assertTrue(res1["ok"])

        # Attempting to post the exact same text from another account is rejected as duplicate
        res2 = self.publisher.publish_sync(
            "bot_medic_yes",
            "Safe buses keep Crown Harbour moving! #PublicTransit",
        )
        self.assertFalse(res2["ok"])
        self.assertIn("constraint_rejected:duplicate_post_text", res2["reason"])

    def test_publish_passes_branch_rule_and_target_text_to_jev(self):
        pf_verdict = MagicMock()
        pf_verdict.can_send = True
        pf_verdict.reason = "approved"
        pf_verdict.to_dict.return_value = {"can_send": True}
        self.postflight.check.return_value = pf_verdict

        result = self.publisher.publish_sync(
            "bot_medic_yes",
            "I hear your concern about clinic wait times—let's look at how the rule helps nurses. #SafeCare",
            branch="branch_B_2b_ours",
            target_text="I doubt this mandate will actually help hospital clinics.",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["branch"], "branch_B_2b_ours")

        call_kwargs = self.postflight.check.call_args[1]
        self.assertTrue(call_kwargs["strict_alignment"])
        self.assertEqual(call_kwargs["target_text"], "I doubt this mandate will actually help hospital clinics.")
        self.assertIn("warm, gentle, consultative tone", call_kwargs["intended_purpose"])
        self.assertIn("Dr. Julian Reed", call_kwargs["persona_profile"])


class TestMacroCli(unittest.TestCase):
    def test_cli_status(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            code = main(["--side","yes","--mock", "--state-file", tmp.name, "status"])
            self.assertEqual(code, 0)

    def test_cli_warmup_and_persistence(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            tmp_path = Path(tmp.name)
            from ctn.storage import StateStore
            from ctn.persona.registry import PersonaRegistry
            from contextlib import closing
            with closing(StateStore(tmp_path)) as store:
                PersonaRegistry().attach(store)
                store.bind_persona('yes_001','fresh_account')
                store.set_persona_modes(['yes_001'],'active')
            # Run warmup command
            code = main(["--side","yes","--mock", "--state-file", str(tmp_path), "warmup", "--account", "fresh_account", "--dry-run"])
            self.assertEqual(code, 0)

            # Verify state was saved to file
            router = _load_router("yes", tmp_path)
            state = router.get_state("bot_worker_yes")
            self.assertEqual(state.post_count, 0)

    def test_setup_environment_switches_between_official_and_mock_client(self):
        from unittest.mock import patch
        from ctn.client.legit_v1 import OfficialLegitClient
        from ctn.macro.cli import _setup_environment
        from ctn.storage import StateStore

        store = StateStore(":memory:")
        self.addCleanup(store.close)

        # When CTN_PLATFORM_API_KEY is configured, _setup_environment wires OfficialLegitClient
        with patch.dict(
            "os.environ",
            {
                "CTN_PLATFORM_URL": "https://social.legitreal.com/api",
                "CTN_PLATFORM_API_KEY": "team_secret_key_2026",
            },
        ):
            actuator_live, _ = _setup_environment(dry_run=True, store=store)
            self.assertIsInstance(actuator_live._client, OfficialLegitClient)
            self.assertEqual(actuator_live._client.team_key, "team_secret_key_2026")
            self.assertFalse(actuator_live._client.auto_register)
            self.assertIs(actuator_live._client.store, store)

            # With force_mock=True, falls back to MockLegitClient
            actuator_mock, _ = _setup_environment(dry_run=True, store=store, force_mock=True)
            self.assertIsInstance(actuator_mock._client, MockLegitClient)


if __name__ == "__main__":
    unittest.main()
