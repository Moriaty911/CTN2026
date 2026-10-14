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


class TestWarmupSeeds(unittest.TestCase):
    def test_warmup_seeds_retrieval(self):
        worker_seeds = list_warmup_seeds("bot_worker_yes")
        self.assertGreater(len(worker_seeds), 0)
        first_seed = get_warmup_seed("bot_worker_yes", 0)
        self.assertIn("Crown Harbour", first_seed)

        # Modulo index cycling
        second_seed = get_warmup_seed("bot_worker_yes", 1)
        self.assertNotEqual(first_seed, second_seed)

        # Unknown account fallback
        unknown_seed = get_warmup_seed("bot_unknown_acc", 0)
        self.assertTrue(len(unknown_seed) > 0)


class TestMacroStateCollector(unittest.TestCase):
    def test_collector_snapshot(self):
        actuator = MagicMock()
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
        now = time.monotonic()
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


class TestMacroCli(unittest.TestCase):
    def test_cli_status(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
            code = main(["--state-file", tmp.name, "status"])
            self.assertEqual(code, 0)

    def test_cli_warmup_and_persistence(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
            tmp_path = Path(tmp.name)
            # Run warmup command
            code = main(["--state-file", str(tmp_path), "warmup", "--account", "bot_worker_yes", "--dry-run"])
            self.assertEqual(code, 0)

            # Verify state was saved to file
            router = _load_router("yes", tmp_path)
            state = router.get_state("bot_worker_yes")
            self.assertEqual(state.post_count, 1)


if __name__ == "__main__":
    unittest.main()
