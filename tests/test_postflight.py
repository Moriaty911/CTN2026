"""Unit tests for the JEV Postflight verification gate and Option B retry flow."""

import json
import unittest

from ctn.config import JevConfig, LLMConfig
from ctn.jev import JevClient, JevPostflightChecker, JevWorkflow, PostflightVerdict
from ctn.llm import DeepSeekClient, DraftOptions, ReplyGenerator
from ctn.models import ActionKind, Post, Side
from ctn.pipeline.brain import JevLlmBrain
from tests.test_jev_actions import POLICY
from tests.test_jev_evaluator import FakeTransport, choose
from tests.test_jev_response import responder
from tests.test_llm import response


class PostflightTests(unittest.TestCase):
    def test_postflight_approves_safe_aligned_reply(self):
        # Default FakeTransport answers: approve, aligned, fulfilled, not toxic
        transport = FakeTransport()
        client = JevClient(JevConfig(api_key="synthetic-test-key", max_retries=0), transport=transport)
        checker = JevPostflightChecker(client)

        verdict = checker.check(
            post_id="post_001",
            target_text="Should we require vaccination proof?",
            reply_text="Which specific policy version defines this rule?",
            assigned_side="yes",
            intended_purpose="P08",
        )
        self.assertTrue(verdict.can_send)
        self.assertEqual(verdict.reason, "approved")
        self.assertEqual(verdict.side_alignment, "aligned")
        self.assertEqual(verdict.purpose_compliance, "compliant")
        self.assertFalse(verdict.toxicity_and_injection)
        self.assertEqual(verdict.send_authorization, "approve")
        self.assertEqual(len(transport.calls), 1)

    def test_postflight_blocks_toxic_or_injection(self):
        def handler(body, data):
            if "toxicity_and_injection" in data["answers"]:
                data["answers"]["toxicity_and_injection"]["noul"] = 1.0

        transport = FakeTransport(handler)
        client = JevClient(JevConfig(api_key="synthetic-test-key", max_retries=0), transport=transport)
        checker = JevPostflightChecker(client)

        verdict = checker.check(
            post_id="post_001",
            target_text="Should we require vaccination proof?",
            reply_text="You idiot, wake up!!",
            assigned_side="yes",
            intended_purpose="P08",
        )
        self.assertFalse(verdict.can_send)
        self.assertEqual(verdict.reason, "toxic_or_injection")
        self.assertTrue(verdict.toxicity_and_injection)

    def test_postflight_blocks_stance_inversion(self):
        def handler(body, data):
            if "side_alignment" in data["answers"]:
                choose(data, "side_alignment", "opposing_or_inversion")

        transport = FakeTransport(handler)
        client = JevClient(JevConfig(api_key="synthetic-test-key", max_retries=0), transport=transport)
        checker = JevPostflightChecker(client)

        verdict = checker.check(
            post_id="post_001",
            target_text="Should we require vaccination proof?",
            reply_text="I agree, vaccination mandates are completely unfair.",
            assigned_side="yes",
            intended_purpose="P08",
        )
        self.assertFalse(verdict.can_send)
        self.assertEqual(verdict.reason, "stance_inversion")
        self.assertEqual(verdict.side_alignment, "opposing_or_inversion")

    def test_postflight_blocks_purpose_drift(self):
        def handler(body, data):
            if "purpose_compliance" in data["answers"]:
                choose(data, "purpose_compliance", "drifted")

        transport = FakeTransport(handler)
        client = JevClient(JevConfig(api_key="synthetic-test-key", max_retries=0), transport=transport)
        checker = JevPostflightChecker(client)

        verdict = checker.check(
            post_id="post_001",
            target_text="Should we require vaccination proof?",
            reply_text="Let me tell you about economic inflation instead.",
            assigned_side="yes",
            intended_purpose="P08",
        )
        self.assertFalse(verdict.can_send)
        self.assertEqual(verdict.reason, "purpose_drifted")
        self.assertEqual(verdict.purpose_compliance, "drifted")

    def test_option_b_retry_recovers_when_second_attempt_is_approved(self):
        # First attempt triggers stance_inversion; second attempt succeeds
        check_count = 0

        def postflight_handler(body, data):
            nonlocal check_count
            check_count += 1
            if check_count == 1:
                choose(data, "side_alignment", "opposing_or_inversion")
                choose(data, "send_authorization", "reject")
            else:
                choose(data, "side_alignment", "aligned")
                choose(data, "send_authorization", "approve")

        pf_transport = FakeTransport(postflight_handler)
        pf_client = JevClient(JevConfig(api_key="synthetic-test-key", max_retries=0), transport=pf_transport)
        postflight = JevPostflightChecker(pf_client)

        workflow, _ = responder("neutral")
        import httpx
        llm_calls = []

        def llm_handler(request):
            llm_calls.append(request)
            return response("Which specific policy condition applies?")

        llm = DeepSeekClient(LLMConfig(api_key="synthetic-key", max_retries=0),
                             transport=httpx.MockTransport(llm_handler))
        generator = ReplyGenerator(workflow, llm)

        brain = JevLlmBrain(workflow, generator, side=Side.YES, postflight=postflight)
        post = Post(post_id="p1", author="alice", text="Is version A required for entry?")

        verdict = brain.think(post)

        # Verified that 2 LLM generations and 2 postflight checks occurred
        self.assertEqual(verdict.action, ActionKind.REPLY)
        self.assertEqual(verdict.reason, "approved_on_retry")
        self.assertEqual(len(llm_calls), 2)
        self.assertEqual(check_count, 2)
        self.assertIn("postflight_attempt_1_rejected", verdict.detail)

    def test_option_b_retry_fails_and_drops_to_none(self):
        # Both attempts rejected by postflight
        def postflight_handler(body, data):
            choose(data, "side_alignment", "opposing_or_inversion")
            choose(data, "send_authorization", "reject")

        pf_transport = FakeTransport(postflight_handler)
        pf_client = JevClient(JevConfig(api_key="synthetic-test-key", max_retries=0), transport=pf_transport)
        postflight = JevPostflightChecker(pf_client)

        workflow, _ = responder("neutral")
        import httpx
        llm = DeepSeekClient(LLMConfig(api_key="synthetic-key", max_retries=0),
                             transport=httpx.MockTransport(lambda r: response("Bad reply")))
        generator = ReplyGenerator(workflow, llm)

        brain = JevLlmBrain(workflow, generator, side=Side.YES, postflight=postflight)
        post = Post(post_id="p1", author="alice", text="Is version A required for entry?")

        verdict = brain.think(post)

        # Both failed -> drops to NONE with postflight_rejected reason
        self.assertEqual(verdict.action, ActionKind.NONE)
        self.assertTrue(verdict.reason.startswith("postflight_rejected:"))


if __name__ == "__main__":
    unittest.main()
