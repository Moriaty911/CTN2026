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
from tests.test_jev_actions import POLICY, set_score
from tests.test_jev_evaluator import FakeTransport, choose

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
    return BotLoop(actuator, side=Side.YES, accounts=ACCOUNTS, brain=brain,
                   rng=random.Random(0), **kwargs)


def make_brain(workflow, llm):
    return JevLlmBrain(workflow, ReplyGenerator(workflow, llm), side=Side.YES,
                       options=DraftOptions(max_reply_chars=280))


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
                         (ActionKind.REPLY, REPLY, "P08"))
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
        self.assertTrue(any(r.ok for rep in reports for r in rep.results))

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
