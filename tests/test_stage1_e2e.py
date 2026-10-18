"""Stage 1: mock platform -> JEV -> DeepSeek -> Actuator, fully offline.

JEV answers and DeepSeek text are scripted, so this verifies the WIRING (routing,
hand-off, guards, rate-limit accounting, dry-run), not model quality.
"""

import asyncio
import json
import random
import unittest

import httpx

from ctn.actuator import Actuator
from ctn.client.mock import MockLegitClient
from ctn.config import BudgetConfig, JevConfig, LLMConfig, RateConfig
from ctn.jev import JevClient, JevWorkflow
from ctn.llm import DeepSeekClient, DraftOptions, ReplyGenerator
from ctn.models import ActionKind, Post, Side
from ctn.pipeline import BotLoop, JevLlmBrain
from ctn.ratelimit import build_gate, build_ledger
from tests.runtime_helpers import with_test_background
from tests.test_jev_actions import POLICY, set_score
from tests.test_jev_evaluator import FakeTransport, choose

from tests.legacy_personas import legacy_router

ACCOUNTS = ["bot_001", "bot_002", "bot_003", "bot_004"]
FAST = RateConfig(read_rps=100, write_rps=100, per_account_read_rps=100, per_account_write_rps=100)
REPLY = "Which specific policy detail or condition do you mean?"


def jev_workflow(*, specificity=4.0):
    """Scripted JEV: stance is read from the post text, as a stand-in for judgment."""
    def handler(body, data):
        text = json.dumps(body).lower()
        stance = "no" if "protect choice" in text else "yes" if "protect kingstonia" in text else "neutral"
        if "policy_stance" in data["answers"]:
            choose(data, "policy_stance", stance)
        if "response_specificity" in data["answers"]:
            set_score(data, "response_specificity", specificity)
            set_score(data, "reply_value", 0)       # opposing posts: not worth a reply here
    transport = FakeTransport(handler)
    client = JevClient(JevConfig(api_key="synthetic-e2e-jev-key", max_retries=0), transport=transport)
    return JevWorkflow(client, action_policy=POLICY), transport


def llm_client(handler=None):
    calls = []

    def send(request):
        calls.append(request)
        if handler:
            return handler(request)
        content = json.dumps({"status": "draft", "reply": REPLY, "needs": []})
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}})
    client = DeepSeekClient(LLMConfig(api_key="synthetic-e2e-llm-key", max_retries=0),
                            transport=httpx.MockTransport(send))
    return client, calls


def make_loop(client, brain, *, dry_run=False, **kwargs):
    actuator = Actuator(client, build_gate(FAST), build_ledger(BudgetConfig()),
                        dry_run=dry_run, max_retries=0, backoff_s=0.01)
    return BotLoop(actuator, side=Side.YES, accounts=[p.account_id for p in brain.router.registry.list_by_side("yes")], brain=brain,
                   rng=random.Random(0), **kwargs)


def make_brain(workflow, llm):
    return with_test_background(JevLlmBrain(workflow, ReplyGenerator(workflow, llm), side=Side.YES,router=legacy_router(),
                       options=DraftOptions(max_reply_chars=280)))


def mock_platform():
    return MockLegitClient(failure_rate=0.0, latency_ms=(0.0, 1.0))


class BrainTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: None)

    def test_aligned_specific_post_gets_a_reply_draft(self):
        workflow, jev = jev_workflow(specificity=4)
        llm, llm_calls = llm_client()
        self.addCleanup(llm.close)
        verdict = make_brain(workflow, llm).think(
            Post("p1", "citizen_1", "We should protect Kingstonia, but what are the exceptions?"))
        self.assertEqual((verdict.action, verdict.text, verdict.prompt_type),
                         (ActionKind.REPLY, REPLY, "P01"))
        self.assertEqual(len(llm_calls), 1)
        self.assertGreaterEqual(len(jev.calls), 2)          # triage + deep, no extra on generate

    def test_aligned_vague_post_is_only_liked_without_llm_call(self):
        workflow, _ = jev_workflow(specificity=1)
        llm, llm_calls = llm_client()
        self.addCleanup(llm.close)
        verdict = make_brain(workflow, llm).think(Post("p2", "c", "We should protect Kingstonia."))
        self.assertEqual(verdict.action, ActionKind.LIKE)
        self.assertEqual(llm_calls, [])

    def test_opposing_low_value_post_is_ignored_without_llm_call(self):
        workflow, _ = jev_workflow()
        llm, llm_calls = llm_client()
        self.addCleanup(llm.close)
        verdict = make_brain(workflow, llm).think(Post("p3", "c", "We should protect choice."))
        self.assertEqual(verdict.action, ActionKind.NONE)
        self.assertEqual(llm_calls, [])

    def test_llm_failure_degrades_to_no_action(self):
        workflow, _ = jev_workflow(specificity=4)
        llm, _ = llm_client(lambda request: httpx.Response(500))
        self.addCleanup(llm.close)
        verdict = make_brain(workflow, llm).think(Post("p4", "c", "protect Kingstonia? exceptions?"))
        self.assertEqual(verdict.action, ActionKind.NONE)
        self.assertTrue(verdict.reason.startswith("llm:"))

    def test_brain_exception_becomes_observe_decision(self):
        workflow, _ = jev_workflow()
        llm, _ = llm_client()
        self.addCleanup(llm.close)
        brain = make_brain(workflow, llm)
        brain.think = lambda post: (_ for _ in ()).throw(RuntimeError("boom"))
        decision = asyncio.run(brain.decide(Post("p5", "c", "x")))
        self.assertEqual(decision.action, ActionKind.NONE)
        self.assertFalse(decision.scores.ok)

    def test_neutral_and_mixed_posts_unlock_p02_p03_p07_replies(self):
        from tests.test_jev_response import responder

        for stance in ("neutral", "mixed"):
            for purpose in ("P02", "P03", "P07"):
                with self.subTest(stance=stance, purpose=purpose):
                    workflow, _ = responder(side=stance, action="reply", purpose=purpose)
                    llm, llm_calls = llm_client()
                    self.addCleanup(llm.close)
                    brain = make_brain(workflow, llm)
                    verdict = brain.think(
                        Post(
                            f"p_{stance}_{purpose}",
                            "citizen_undecided",
                            "I am worried about bus safety—how does the H2D2 rule work for commuters?",
                        )
                    )
                    self.assertEqual(verdict.action, ActionKind.REPLY, verdict.reason)
                    self.assertEqual(verdict.prompt_type, purpose)
                    self.assertEqual(verdict.text, REPLY)
                    self.assertEqual(len(llm_calls), 1)

    def test_opposing_missing_platform_level_stays_unknown_without_generation(self):
        cases = [
            ("question", "policy_proposal", "Why mandate H2D2 on public buses?", "P02"),
            ("argument", "policy_proposal", "A citywide transit mandate hurts small shops.", "P03"),
            ("experience", "personal", "I am worried about vaccine side effects for my kids.", "P07"),
            ("venting", "value", "This whole mandate is a total scam!!", "P08"),
        ]
        for idx, (func, claim_kind, text, expected_purpose) in enumerate(cases):
            with self.subTest(func=func, expected_purpose=expected_purpose):
                def handler(body, data, f=func, ck=claim_kind):
                    if "policy_stance" in data["answers"]:
                        choose(data, "policy_stance", "no")
                    if "reply_value" in data["answers"]:
                        set_score(data, "reply_value", 2.0)  # [1, 3] standard range
                        set_score(data, "response_specificity", 3.0)
                    if "expression_function" in data["answers"]:
                        choose(data, "expression_function", f)
                    if "claim_kind" in data["answers"]:
                        choose(data, "claim_kind", ck)

                transport = FakeTransport(handler)
                client = JevClient(JevConfig(api_key="synthetic-opposing-jev-key", max_retries=0), transport=transport)
                # Use default ActionPolicy() with empty platform_levels=() to verify JevLlmBrain auto-populates levels
                workflow = JevWorkflow(client)
                llm, llm_calls = llm_client()
                self.addCleanup(llm.close)
                brain = make_brain(workflow, llm)
                # Post has empty raw={} (no explicit platform_level)
                verdict = brain.think(Post(f"opp_{idx}", "citizen_opp", text))
                self.assertEqual(brain._infer_platform_level(Post('unknown','dr_unverified',text)), 'unknown')
                self.assertEqual(verdict.action, ActionKind.NONE, verdict.reason)
                self.assertEqual(len(llm_calls), 0)


class EndToEndTests(unittest.TestCase):
    def run_cycles(self, n=3, *, dry_run=False, specificity=4):
        platform = mock_platform()
        workflow, jev = jev_workflow(specificity=specificity)
        llm, llm_calls = llm_client()
        self.addCleanup(llm.close)
        loop = make_loop(platform, make_brain(workflow, llm), dry_run=dry_run)

        async def go():
            return [await loop.run_cycle() for _ in range(n)]
        return platform, loop, asyncio.run(go()), jev, llm_calls

    def test_full_loop_reads_judges_drafts_and_writes(self):
        platform, loop, reports, jev, llm_calls = self.run_cycles()
        results = [r for rep in reports for r in rep.results]
        self.assertTrue(any(r.ok for r in results), [r.detail for r in results])
        kinds = {r.kind for r in results if r.ok}
        self.assertLessEqual(kinds, {"like", "reply"})
        self.assertEqual(platform.write_count, sum(r.ok for r in results))
        # Every published reply is exactly the (scripted) DeepSeek draft.
        replies = [p for p in platform.posts.values() if p.text == REPLY]
        self.assertEqual(len(replies), sum(1 for r in results if r.ok and r.kind == "reply"))
        self.assertGreater(len(replies), 0, "expected at least one DeepSeek reply to be published")
        self.assertGreater(len(jev.calls), 0)
        for rep in reports:
            self.assertLessEqual(len(rep.results), loop.max_actions_per_cycle)

    def test_never_acts_twice_on_the_same_post(self):
        _, _, reports, _, _ = self.run_cycles(n=4)
        targets = [r.target_id for rep in reports for r in rep.results if r.ok]
        self.assertEqual(len(targets), len(set(targets)))

    def test_dry_run_sends_nothing_even_with_brain(self):
        platform, _, reports, _, _ = self.run_cycles(dry_run=True)
        self.assertEqual(platform.write_count, 0)
        self.assertTrue(any(r.status=="simulated" for rep in reports for r in rep.results))

    def test_ruled_out_posts_are_not_rejudged(self):
        platform = mock_platform()
        workflow, jev = jev_workflow(specificity=4)
        llm, _ = llm_client()
        self.addCleanup(llm.close)
        loop = make_loop(platform, make_brain(workflow, llm))

        async def go():
            first = await loop.run_cycle()
            second = await loop.run_cycle()
            return first, second
        first, second = asyncio.run(go())
        ruled_out = {d.post_id for d in first.decisions if d.action is ActionKind.NONE}
        self.assertTrue(ruled_out)
        self.assertFalse(ruled_out & {d.post_id for d in second.decisions})

    def test_evaluation_is_capped_per_cycle(self):
        platform = mock_platform()
        workflow, jev = jev_workflow()
        llm, _ = llm_client()
        self.addCleanup(llm.close)
        loop = make_loop(platform, make_brain(workflow, llm), max_evaluations_per_cycle=2)
        report = asyncio.run(loop.run_cycle())
        self.assertLessEqual(len(report.decisions), 2)


if __name__ == "__main__":
    unittest.main()
