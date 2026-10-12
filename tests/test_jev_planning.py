"""Neutral/mixed delegation and guided/open prompts, without LLM calls."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from ctn.config import JevConfig
from ctn.jev import ActionPolicy, JevAuditLog, JevClient, JevWorkflow, MaterialBrief, ResponsePlanningTask
from tests.test_jev_actions import set_score
from tests.test_jev_evaluator import FakeTransport, choose
from tests.test_jev_workflow import record, clarification


def planner(side="neutral", expression="assertion", handler=None, audit=None, policy=None):
    def respond(body, data):
        choose(data, "policy_stance", side)
        if "expression_function" in data["answers"]:
            choose(data, "expression_function", expression)
            set_score(data, "reply_value", 0)
        if handler:
            handler(body, data)
    transport = FakeTransport(respond)
    workflow = JevWorkflow(JevClient(JevConfig(api_key="synthetic-planning-key", max_retries=0), transport=transport),
                           audit_log=audit, action_policy=policy if policy is not None else ActionPolicy(version="jev-actions.v2"))
    return workflow, transport


class ResponsePlanningTests(unittest.TestCase):
    def test_neutral_and_mixed_open_planning_without_value_or_account_gate(self):
        for side in ("neutral", "mixed"):
            with self.subTest(side=side):
                workflow, transport = planner(side)
                data = record(level=None)
                package = workflow.evaluate(data)
                meta = package.to_dict()
                full = package.to_dict(include_materials=True)
                self.assertEqual(meta["action_decision"]["status"], "needs_llm_assessment")
                self.assertIsNone(meta["action_decision"]["action"])
                self.assertIsNone(meta["generation_task"])
                self.assertEqual(meta["response_planning_task"]["status"], "ready")
                self.assertEqual(meta["response_planning_task"]["mode"], "open")
                self.assertNotIn(data["post"]["text"], json.dumps(meta))
                self.assertNotIn("response_planning_materials", meta)
                task = full["response_planning_materials"]
                self.assertEqual(task["alignment"], side)
                self.assertEqual(task["evaluated_materials"]["content"]["post"]["text"], data["post"]["text"])
                self.assertIn("no_interaction", task["requested_output"]["decision"])
                self.assertIn("does not establish", task["instructions"])
                self.assertFalse(task["execution_authorized"])
                self.assertEqual(len(transport.calls), 2)
                full["response_planning_materials"]["instructions"] = "changed"
                self.assertNotEqual(package.to_dict(include_materials=True)["response_planning_materials"]["instructions"], "changed")

    def test_question_hint_does_not_invent_exact_question_or_probability_threshold(self):
        workflow, _ = planner(expression="question")
        task = workflow.evaluate(record()).to_dict(include_materials=True)["response_planning_materials"]
        self.assertEqual(task["mode"], "question_first")
        self.assertIn("not proof", task["direction_hint"])
        self.assertIsNone(task["selected_direction"])
        self.assertIn("request it or ask a clarification", task["instructions"])

    def test_guided_direction_preserved_after_evaluation_with_no_additional_call(self):
        workflow, transport = planner()
        data = record()
        package = workflow.evaluate(data)
        fingerprint = package.to_dict()["action_policy_fingerprint"]
        workflow.action_policy = replace(workflow.action_policy, version="jev-actions.v1")
        prepared = workflow.prepare(package, clarification(data))
        output = prepared.to_dict(include_materials=True)
        task = output["response_planning_materials"]
        self.assertEqual(task["mode"], "guided")
        self.assertEqual(task["selected_direction"]["prompt_type"], "P08")
        self.assertEqual(task["selected_direction"]["focus"]["binding"]["quote"], data["post"]["text"])
        self.assertEqual(task["action_policy_fingerprint"], fingerprint)
        self.assertIn("do not silently switch", task["instructions"])
        self.assertEqual(len(transport.calls), 2)
        self.assertIsNone(output["generation_task"])

    def test_missing_answer_basis_does_not_fall_back_to_unrestricted_planning(self):
        workflow, _ = planner()
        data = record()
        initial = workflow.evaluate(data)
        bad = replace(clarification(data), prompt_type="P02")
        output = workflow.prepare(initial, bad).to_dict(include_materials=True)
        self.assertEqual(output["response_planning_task"]["mode"], "guided")
        self.assertEqual(output["response_planning_task"]["status"], "needs_materials")
        self.assertIn("answer_basis", output["response_planning_needs"])
        self.assertNotIn("response_planning_materials", output)

    def test_bad_new_guidance_removes_previous_open_and_guided_tasks(self):
        workflow, _ = planner()
        data = record()
        open_package = workflow.evaluate(data)
        guided_package = workflow.prepare(open_package, clarification(data))
        for package in (open_package, guided_package):
            bad = replace(clarification(data), prompt_type="P02")
            result = workflow.prepare(package, bad).to_dict(include_materials=True)
            self.assertNotIn("response_planning_materials", result)
            self.assertNotIn("generation_materials", result)

    def test_quote_binding_and_caller_added_secrets_cannot_bypass_checks(self):
        workflow, _ = planner()
        data = record()
        package = workflow.evaluate(data)
        wrong = replace(clarification(data), focus=replace(clarification(data).focus, quote="other text"))
        secret = replace(clarification(data), information_gap="Check synthetic-planning-key")
        for brief, status in ((wrong, "needs_materials"), (secret, "needs_review")):
            result = workflow.prepare(package, brief).to_dict(include_materials=True)
            self.assertEqual(result["response_planning_task"]["status"], status)
            self.assertNotIn("response_planning_materials", result)
            self.assertNotIn("synthetic-planning-key", json.dumps(result))

    def test_risk_context_failure_and_observation_do_not_dispatch_planning(self):
        handlers = (
            lambda body, data: data["answers"]["prompt_injection"].update(noul=.5),
            lambda body, data: data["answers"].pop("prompt_injection", None),
            lambda body, data: choose(data, "specificity_evidence", "insufficient") if "specificity_evidence" in data["answers"] else None,
            lambda body, data: choose(data, "relevance", "unrelated"),
            lambda body, data: data["answers"].clear(),
        )
        for handler in handlers:
            workflow, _ = planner(handler=handler)
            result = workflow.evaluate(record()).to_dict(include_materials=True)
            self.assertIsNone(result["response_planning_task"])
            self.assertNotIn("response_planning_materials", result)

    def test_partial_nonessential_answer_retained_without_numeric_default(self):
        workflow, _ = planner(handler=lambda body, data: data["answers"].pop("reply_value", None))
        result = workflow.evaluate(record()).to_dict(include_materials=True)
        self.assertEqual(result["flow"]["status"], "partial")
        task = result["response_planning_materials"]
        answer = task["assessment"]["deep"]["assessment"]["output"]["answers"]["reply_value"]
        self.assertEqual(answer["status"], "missing")
        self.assertIsNone(answer["value"])

    def test_legacy_policy_and_unknown_alignment_do_not_delegate(self):
        for side, policy in (("neutral", ActionPolicy(version="jev-actions.v1")), ("unclear", ActionPolicy())):
            workflow, _ = planner(side=side, policy=policy)
            result = workflow.evaluate(record()).to_dict(include_materials=True)
            self.assertEqual(result["action_decision"]["status"], "pending_rules")
            self.assertIsNone(result["response_planning_task"])

    def test_batch_and_audit_preserve_target_and_new_attempt_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev.jsonl"
            with JevAuditLog(path) as audit:
                workflow, transport = planner(audit=audit)
                a, b = record("planning-a"), record("planning-b")
                packages = workflow.evaluate_batch([a, b, a])
                workflow.prepare(packages[0], clarification(a))
            logs = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(sum(l["new_attempt_count"] for l in logs), 4)
        self.assertEqual(logs[-1]["response_planning"]["mode"], "guided")
        self.assertEqual(logs[-1]["new_attempt_count"], 0)
        self.assertNotIn(a["post"]["text"], json.dumps(logs))
        self.assertNotIn("instructions", json.dumps(logs))
        for package, target in zip(packages, ("planning-a", "planning-b", "planning-a")):
            self.assertEqual(package.to_dict(include_materials=True)["response_planning_materials"]["target"]["post_id"], target)

    def test_planning_metadata_rejects_invalid_or_textual_modes_and_types(self):
        for args in (("ready", "open"), ("blocked", "arbitrary text"), ("blocked", "guided", "P99"), ("blocked", "open", "P08")):
            with self.assertRaises(ValueError):
                ResponsePlanningTask(*args)

    def test_fixed_answer_does_not_become_ready_without_required_material(self):
        workflow, _ = planner(expression="question")
        data = record()
        brief = replace(clarification(data), prompt_type="P02")
        result = workflow.evaluate(data, brief=brief).to_dict(include_materials=True)
        self.assertEqual(result["response_planning_task"]["status"], "needs_materials")
        self.assertNotIn("response_planning_materials", result)
