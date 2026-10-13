"""Confirmed action boundaries and explicit account requirements."""

from dataclasses import replace
import math
import unittest

from ctn.jev import ActionDecision, ActionPolicy, ActionValidationError, JevResponseParser, load_rubric
from tests.test_jev_evaluator import evaluator, content, choose


def set_score(data, key, value):
    answer = data["answers"][key]
    lower, upper = math.floor(value), math.ceil(value)
    answer["score"] = value
    answer["probabilities"] = {k: (1.0 if int(k) == lower else 0.0) if lower == upper else
        (upper - value if int(k) == lower else value - lower if int(k) == upper else 0.0)
        for k in answer["probabilities"]}


def assessed(side="yes", value=2.5, specificity=2.5, level="test-standard", handler=None):
    def respond(body, data):
        choose(data, "policy_stance", side)
        if "reply_value" in data["answers"]:
            set_score(data, "reply_value", value)
            set_score(data, "response_specificity", specificity)
        if handler:
            handler(body, data)
    worker, transport = evaluator(respond, post_questions=load_rubric("post", version="v2"))
    record = content()
    record["task_context"] = {"assigned_side": "yes"}
    record["author"] = {"platform_level": level}
    return worker.evaluate(record), worker, transport


POLICY = ActionPolicy(platform_levels=(("test-standard", "standard"), ("test-high", "high")))


class ActionTests(unittest.TestCase):
    def test_aligned_uses_specificity_not_value_and_boundary_is_strict(self):
        for score, expected in ((0, "like"), (2.5, "like"), (2.5001, "reply"), (4, "reply")):
            with self.subTest(score=score):
                flow, _, _ = assessed(value=0, specificity=score)
                decision = POLICY.decide(flow)
                self.assertEqual(decision.action, expected)
                self.assertEqual(decision.signal_id, "response_specificity")

    def test_opposing_value_ranges_and_high_boundary(self):
        for score, action, level in ((0, "no_interaction", None), (.9999, "no_interaction", None),
            (1, "reply", "standard"), (2, "reply", "standard"), (3, "reply", "standard"), (3.0001, "reply", "high"), (4, "reply", "high")):
            with self.subTest(score=score):
                flow, _, _ = assessed(side="no", value=score, specificity=0)
                decision = POLICY.decide(flow)
                self.assertEqual(decision.action, action)
                self.assertEqual(decision.required_account_level, level)
                self.assertEqual(decision.signal_id, "reply_value")

    def test_high_target_upgrades_only_after_value_gate(self):
        for value in (0, .9, 1, 2):
            flow, _, _ = assessed(side="no", value=value, level="test-high")
            decision = POLICY.decide(flow)
            self.assertEqual(decision.action, "no_interaction" if value < 1 else "reply")
            self.assertEqual(decision.required_account_level, None if value < 1 else "high")
            if value >= 1:
                self.assertEqual(decision.reply_quality, "high")

    def test_unknown_missing_unmapped_and_followers_do_not_become_low_level(self):
        for level in (None, "unmapped"):
            flow, _, _ = assessed(side="no", value=2, level=level)
            decision = POLICY.decide(flow)
            self.assertEqual(decision.status, "needs_account_metadata")
            self.assertEqual(decision.action, "reply")
            self.assertEqual(decision.required_account_level, "unknown")
            self.assertEqual(decision.minimum_account_level, "standard")
        flow, _, _ = assessed(side="no", value=4, level=None)
        self.assertEqual(POLICY.decide(flow).required_account_level, "high")

    def test_missing_value_never_falls_back_to_specificity(self):
        flow, _, _ = assessed(side="no", value=4, specificity=4,
            handler=lambda body, data: data["answers"].pop("reply_value", None))
        decision = POLICY.decide(flow)
        self.assertIsNone(decision.action)
        self.assertEqual(decision.status, "needs_assessment")

    def test_insufficient_context_or_high_risk_holds_even_high_value(self):
        for handler in (
            lambda body, data: choose(data, "reply_value_evidence", "insufficient") if "reply_value_evidence" in data["answers"] else None,
            lambda body, data: data["answers"]["prompt_injection"].update(noul=.5),
        ):
            flow, _, _ = assessed(side="no", value=4, handler=handler)
            self.assertIsNone(POLICY.decide(flow).action)

    def test_mixed_neutral_request_jev_without_action_value_gate_or_account_selection(self):
        for side in ("mixed", "neutral"):
            flow, _, _ = assessed(side=side, value=0, level=None)
            decision = POLICY.decide(flow)
            self.assertEqual(decision.status, "needs_jev_assessment")
            self.assertIsNone(decision.action)
            self.assertIsNone(decision.signal_value)
            self.assertIsNone(decision.required_account_level)

    def test_unknown_and_legacy_mixed_neutral_remain_pending(self):
        for side in ("mixed", "neutral", "unclear"):
            flow, _, _ = assessed(side=side)
            self.assertEqual(replace(POLICY, version="jev-actions.v1").decide(flow).status, "pending_rules")
        flow, _, _ = assessed(side="unclear")
        self.assertEqual(POLICY.decide(flow).status, "pending_rules")
        self.assertNotIn("mixed_neutral_actions", POLICY.to_dict()["pending_rules"])
        self.assertNotEqual(POLICY.fingerprint, replace(POLICY, version="jev-actions.v1").fingerprint)

    def test_unrelated_observation_is_no_interaction_but_failure_is_not(self):
        flow, _, _ = assessed(handler=lambda body, data: choose(data, "relevance", "unrelated"))
        self.assertEqual(POLICY.decide(flow).action, "no_interaction")
        flow, _, _ = assessed(handler=lambda body, data: data["answers"].clear())
        self.assertIsNone(POLICY.decide(flow).action)

    def test_v2_does_not_change_v1_or_triage_questions(self):
        old, new = load_rubric("post"), load_rubric("post", version="v2")
        self.assertEqual(new.version, "ctn-post.v2")
        self.assertEqual(new.status, "confirmed")
        self.assertEqual(new.for_stage("triage"), old.for_stage("triage"))
        self.assertEqual(len(new.for_stage("deep")), 17)
        self.assertNotEqual(old.fingerprint, new.fingerprint)
        self.assertNotIn("reply_value", old.questions)
        self.assertEqual(new.questions["reply_value"].availability_question_id, "reply_value_evidence")

    def test_configuration_strictness_and_fingerprint(self):
        self.assertEqual(POLICY.fingerprint, replace(POLICY).fingerprint)
        self.assertNotEqual(POLICY.fingerprint, replace(POLICY, aligned_reply_above=3).fingerprint)
        for kwargs in ({"aligned_reply_above": True}, {"opposing_reply_from": 4, "opposing_high_above": 1},
            {"platform_levels": (("x", "high"), ("x", "standard"))}, {"platform_levels": (("x", {}),)}, {"aligned_reply_above": float("nan")}, {"version": {}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ActionValidationError):
                ActionPolicy(**kwargs)
        for version in ("../v2", "v3", None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                load_rubric("post", version=version)

    def test_decision_contract_rejects_unknown_actions_and_unavailable_numeric_defaults(self):
        for make in (
            lambda: ActionDecision("decided", None, "x"),
            lambda: ActionDecision("decided", "unknown-action", "x"),
            lambda: ActionDecision("needs_review", "like", "x"),
            lambda: ActionDecision("decided", "like", "x", signal_id="reply_value", signal_value=float("nan")),
        ):
            with self.assertRaises(ActionValidationError):
                make()
