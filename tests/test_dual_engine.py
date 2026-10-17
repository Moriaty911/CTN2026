"""End-to-end integration tests for the Dual-Engine Architecture.

Verifies that the Micro Reactive Loop (replying to incoming posts) and the
Macro Strategic Loop (proactive publishing via the harness) work in unison,
sharing the same Actuator, BudgetLedger, RateGate, and PersonaRouter state.
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import MagicMock

import httpx

from ctn.actuator import Actuator
from ctn.client.mock import MockLegitClient
from ctn.config import BudgetConfig, JevConfig, LLMConfig, RateConfig
from ctn.jev import JevClient, JevPostflightChecker, JevWorkflow, MaterialBrief, TextSpan
from ctn.llm import DeepSeekClient, DraftOptions, ReplyGenerator
from ctn.macro.publisher import StrategicPublisher
from ctn.models import ActionKind, Post, Side
from ctn.persona import PersonaRouter
from ctn.pipeline.brain import JevLlmBrain
from ctn.ratelimit import build_gate, build_ledger
from tests.test_jev_actions import POLICY, set_score
from tests.test_jev_evaluator import FakeTransport, choose

FAST_RATE = RateConfig(read_rps=100.0, write_rps=100.0)


def create_mock_jev_workflow():
    """Scripted JEV workflow returning predictable stance and response decisions."""
    def handler(body, data):
        text = json.dumps(body).lower()
        stance = "yes" if "protect kingstonia" in text else "no"
        if "policy_stance" in data["answers"]:
            choose(data, "policy_stance", stance)
        if "response_specificity" in data["answers"]:
            set_score(data, "response_specificity", 4.0)
            set_score(data, "reply_value", 4.0)
        if "expression_function" in data["answers"]:
            if "transit" in text or "bus" in text:
                choose(data, "expression_function", "experience")
            elif "policy" in text or "rule" in text:
                choose(data, "expression_function", "question")
            else:
                choose(data, "expression_function", "argument")

    transport = FakeTransport(handler)
    client = JevClient(JevConfig(api_key="mock-dual-jev-key", max_retries=0), transport=transport)
    return JevWorkflow(client, action_policy=POLICY)


def create_mock_llm_client():
    """Scripted DeepSeek client generating standard drafts."""
    def send(request):
        content = json.dumps({"status": "draft", "reply": "Could you clarify which specific policy guideline you mean?", "needs": []})
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52},
        })
    return DeepSeekClient(LLMConfig(api_key="mock-dual-llm-key", max_retries=0), transport=httpx.MockTransport(send))


class DualEngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.mock_platform = MockLegitClient(failure_rate=0.0)
        self.budget_cfg = BudgetConfig()
        self.actuator = Actuator(
            self.mock_platform,
            build_gate(FAST_RATE),
            build_ledger(self.budget_cfg),
            dry_run=False,
        )

        self.workflow = create_mock_jev_workflow()
        self.llm = create_mock_llm_client()
        self.addCleanup(self.llm.close)

        # Mock postflight checker that approves safe replies
        self.postflight = MagicMock()
        pf_verdict = MagicMock()
        pf_verdict.can_send = True
        pf_verdict.reason = "authorized"
        pf_verdict.to_dict.return_value = {"can_send": True, "send_authorization": "send"}
        self.postflight.check.return_value = pf_verdict

        # Shared PersonaRouter
        self.shared_router = PersonaRouter(default_side="yes")

        # Initialize Micro Brain
        self.generator = ReplyGenerator(self.workflow, self.llm)
        self.brain = JevLlmBrain(
            workflow=self.workflow,
            generator=self.generator,
            side=Side.YES,
            router=self.shared_router,
            postflight=self.postflight,
        )

        # Initialize Macro Strategic Publisher
        self.macro_publisher = StrategicPublisher(
            actuator=self.actuator,
            postflight=self.postflight,
            router=self.shared_router,
            side="yes",
        )

    def test_shared_account_cooldown_from_macro_to_micro(self):
        """When macro publishes from an account, micro respects cooldown and uses fallback."""
        # Step 1: Macro publishes proactive post with bot_worker_yes at current time
        t0 = time.time()
        macro_res = self.macro_publisher.publish_sync(
            "bot_worker_yes",
            "Early morning transit shift started in Crown Harbour. #PublicTransit",
            bypass_jev=True,
        )
        self.assertTrue(macro_res["ok"])
        self.assertEqual(macro_res["status"], "published")

        # Verify bot_worker_yes is in cooldown
        worker_state = self.shared_router.get_state("bot_worker_yes")
        self.assertTrue(worker_state.is_in_cooldown(t0 + 5.0, 60.0))

        # Step 2: Micro loop encounters an incoming post favoring grassroots worker
        post = Post("p_transit_1", "citizen_alice", "Are the transit buses on coastal road safe? Protect Kingstonia.")
        verdict = self.brain.think(post)

        self.assertEqual(verdict.action, ActionKind.REPLY)
        # Because bot_worker_yes is in cooldown, the router should have fallen back to another available account
        selected_acc = verdict.detail.get("selected_account")
        self.assertNotEqual(selected_acc, "bot_worker_yes")
        self.assertTrue(verdict.detail["routing"]["fallback_used"])

    def test_shared_account_cooldown_from_micro_to_macro(self):
        """When micro publishes a reply, macro cannot immediately publish from that account."""
        # Step 1: Micro replies to a policy post, selecting bot_policy_yes
        post = Post("p_policy_1", "citizen_bob", "Which policy version governs parks? Protect Kingstonia.")
        verdict = self.brain.think(post)
        self.assertEqual(verdict.action, ActionKind.REPLY)
        used_acc = verdict.detail.get("selected_account")
        self.assertTrue(used_acc.startswith("bot_"))

        # Step 2: Macro tries to publish from the exact same account immediately
        macro_res = self.macro_publisher.publish_sync(
            used_acc,
            "Proactive macro announcement text",
            bypass_jev=True,
        )
        self.assertFalse(macro_res["ok"])
        self.assertEqual(macro_res["status"], "blocked")
        self.assertIn("account_in_cooldown", macro_res["reason"])

    def test_shared_budget_ledger_exhaustion(self):
        """When write budget ledger is exhausted, both micro and macro write operations fail gracefully."""
        # Set writes to write_cap so can_write() returns False
        self.actuator.ledger.writes = self.actuator.ledger.write_cap

        # Attempt to publish fails due to exhausted budget
        res = self.macro_publisher.publish_sync("bot_worker_yes", "Post text", bypass_jev=True)
        self.assertFalse(res["ok"])
        self.assertIn("write budget exhausted", res["reason"])

    def test_concurrent_mixed_cycle_execution(self):
        """Simulate an operational cycle with macro proactive warmup and micro reactive replies."""
        # 1. Macro warms up bot_worker_yes
        w_res = self.macro_publisher.publish_sync("bot_worker_yes", "Warmup text", bypass_jev=True)
        self.assertTrue(w_res["ok"])

        # 2. Micro processes incoming post
        p1 = Post("inc_1", "citizen_x", "Protect Kingstonia with sensible measures.")
        v1 = self.brain.think(p1)
        self.assertIn(v1.action, (ActionKind.REPLY, ActionKind.LIKE))

        # 3. Micro processes second incoming post
        p2 = Post("inc_2", "citizen_y", "Protect Kingstonia buses now.")
        v2 = self.brain.think(p2)
        self.assertIn(v2.action, (ActionKind.REPLY, ActionKind.LIKE))

        # Check total platform write operations executed
        self.assertGreaterEqual(len(self.mock_platform.posts), 1)


if __name__ == "__main__":
    unittest.main()
