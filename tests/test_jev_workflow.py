"""Full offline content -> JEV -> action -> selected material -> audit flow."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from ctn.config import JevConfig
from ctn.jev import (
    ActionPolicy, JevAuditLog, JevClient, JevPacker, JevResponseParser, JevWorkflow,
    MaterialBrief, TextSpan, WorkflowValidationError, load_rubric,
)
from tests.test_jev_actions import POLICY, set_score
from tests.test_jev_evaluator import FakeTransport, choose, content


def record(post_id="synthetic-workflow-target", level="test-standard"):
    data = content(post_id=post_id)
    data["task_context"] = {"assigned_side": "yes"}
    data["author"] = {"platform_level": level}
    return data


def worker(side="yes", specificity=4, value=2, action_policy=POLICY, audit_log=None):
    def handler(body, data):
        if "policy_stance" in data["answers"]:
            choose(data, "policy_stance", side)
        if "response_specificity" in data["answers"]:
            set_score(data, "response_specificity", specificity)
            set_score(data, "reply_value", value)
    transport = FakeTransport(handler)
    client = JevClient(JevConfig(api_key="synthetic-workflow-key", max_retries=0), transport=transport)
    return JevWorkflow(client, action_policy=action_policy, audit_log=audit_log), transport


def clarification(data):
    text = data["post"]["text"]
    return MaterialBrief("P08", TextSpan(("content", "post", "text"), 0, len(text), text),
                         information_gap="Which condition or policy version is meant?")


class WorkflowTests(unittest.TestCase):
    def test_rounding_compatibility_preserves_provider_action_threshold(self):
        for score, expected_action in ((2.49, "like"), (2.51, "reply")):
            with self.subTest(score=score):
                def handler(body, data):
                    choose(data, "policy_stance", "yes")
                    if "response_specificity" in data["answers"]:
                        set_score(data, "response_specificity", 2.5)
                        data["answers"]["response_specificity"]["score"] = score
                client = JevClient(JevConfig(api_key="synthetic-workflow-key", max_retries=0),
                                   transport=FakeTransport(handler))
                output = JevWorkflow(client).evaluate(record()).to_dict()
                self.assertEqual(output["action_decision"]["action"], expected_action)
                self.assertEqual(output["action_decision"]["signal_value"], score)

    def test_v2_full_path_hands_off_action_and_explicit_generation_materials(self):
        workflow, transport = worker()
        data = record()
        package = workflow.evaluate(data, brief=clarification(data))
        default = package.to_dict()
        self.assertEqual(default["flow"]["deep"]["assessment"]["output"]["question_set_version"], "ctn-post.v3")
        self.assertEqual([len(c["questions"]) for c in transport.calls], [4, 19])
        self.assertEqual(default["action_decision"]["action"], "reply")
        self.assertEqual(default["generation_task"]["prompt_type"], "P08")
        self.assertNotIn(data["post"]["text"], json.dumps(default))
        self.assertNotIn("generation_materials", default)
        explicit = package.to_dict(include_materials=True)
        self.assertEqual(explicit["generation_materials"]["target"]["post_id"], data["post"]["post_id"])
        self.assertEqual(explicit["generation_materials"]["action_decision"]["action"], "reply")
        self.assertFalse(explicit["execution_authorized"])
        self.assertNotIn("synthetic-workflow-key", json.dumps(explicit))
        explicit["generation_materials"]["focus"]["source_text"] = "mutated"
        self.assertNotEqual(package.to_dict(include_materials=True)["generation_materials"]["focus"]["source_text"], "mutated")

    def test_low_specificity_like_has_no_generation_even_with_brief(self):
        workflow, _ = worker(specificity=2.5)
        data = record()
        output = workflow.evaluate(data, brief=clarification(data)).to_dict(include_materials=True)
        self.assertEqual(output["action_decision"]["action"], "like")
        self.assertIsNone(output["generation_task"])
        self.assertEqual(output["material_check"]["status"], "blocked")
        self.assertNotIn("generation_materials", output)

    def test_opposing_high_value_and_high_target_are_distinct_triggers(self):
        for value, level in ((4, "test-standard"), (2, "test-high")):
            with self.subTest(value=value, level=level):
                workflow, _ = worker(side="no", value=value)
                data = record(level=level)
                output = workflow.evaluate(data, brief=clarification(data)).to_dict()
                self.assertEqual(output["action_decision"]["required_account_level"], "high")
                self.assertEqual(output["generation_task"]["reply_quality"], "high")

    def test_unknown_level_does_not_dispatch_standard_generation(self):
        workflow, _ = worker(side="no", value=2, action_policy=ActionPolicy())
        data = record()
        output = workflow.evaluate(data, brief=clarification(data)).to_dict()
        self.assertEqual(output["action_decision"]["status"], "needs_account_metadata")
        self.assertEqual(output["material_check"]["status"], "ready")
        self.assertIsNone(output["generation_task"])
        self.assertIn("platform_level_mapping", output["generation_needs"])

    def test_no_brief_and_missing_material_preserve_reply_without_fabricating_task(self):
        workflow, _ = worker()
        data = record()
        output = workflow.evaluate(data).to_dict()
        self.assertEqual(output["action_decision"]["action"], "reply")
        self.assertIsNone(output["generation_task"])
        self.assertIn("downstream_prompt_type_and_material_brief", output["generation_needs"])
        text = data["post"]["text"]
        output = workflow.evaluate(data, brief=MaterialBrief("P02", TextSpan(("content", "post", "text"), 0, len(text), text))).to_dict()
        self.assertEqual(output["material_check"]["status"], "needs_materials")
        self.assertIn("answer_basis", output["generation_needs"])

    def test_batch_binding_deduplication_and_decision_audit_are_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev.jsonl"
            with JevAuditLog(path) as audit:
                workflow, transport = worker(audit_log=audit)
                a, b = record("a"), record("b")
                outputs = workflow.evaluate_batch([a, b, a], briefs=[clarification(a), None, clarification(a)])
            logs = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([p.to_dict()["targets"][0]["post_id"] for p in outputs], ["a", "b", "a"])
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(sum(l["new_attempt_count"] for l in logs), 4)
        self.assertEqual(len(logs), 3)
        # Worker completion order is independent of the ordered batch return.
        logs.sort(key=lambda entry: entry["batch_index"])
        self.assertEqual(logs[0]["action"]["action"], "reply")
        self.assertEqual(logs[0]["material_check"]["prompt_type"], "P08")
        self.assertNotIn(a["post"]["text"], json.dumps(logs))

    def test_comparison_is_explicit_and_logged_with_two_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev.jsonl"
            with JevAuditLog(path) as audit:
                workflow, transport = worker(audit_log=audit)
                output = workflow.compare(record("current"), record("historic")).to_dict()
            logs = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(output["kind"], "comparison")
        self.assertEqual(len(output["targets"]), 2)
        self.assertEqual(logs[0]["event"], "comparison_evaluation")

    def test_missing_credentials_and_disabled_never_generate_or_interact(self):
        for config in (JevConfig(), JevConfig(enabled=False)):
            transport = FakeTransport()
            workflow = JevWorkflow(JevClient(config, transport=transport))
            data = record()
            package = workflow.evaluate(data, brief=clarification(data)).to_dict()
            self.assertIsNone(package["action_decision"]["action"])
            self.assertIsNone(package["generation_task"])
            self.assertEqual(len(transport.calls), 0)

    def test_selection_shape_validated_before_provider_calls(self):
        workflow, transport = worker()
        for run in (lambda: workflow.evaluate(record(), brief={}),
                    lambda: workflow.evaluate_batch([record()], briefs=[]),
                    lambda: workflow.evaluate_batch([record()], comparisons=[{}])):
            with self.assertRaises(WorkflowValidationError):
                run()
        self.assertEqual(len(transport.calls), 0)

    def test_parser_without_explicit_rubric_recognizes_v2_binding(self):
        transport = FakeTransport()
        client = JevClient(JevConfig(api_key="synthetic-only-key", max_retries=0), transport=transport)
        packed = JevPacker().pack_post(record(), stage="deep", question_set=load_rubric("post", version="v2"))
        result = JevResponseParser().parse(client.evaluate(packed))
        self.assertEqual(result.status, "success")
        self.assertIn("reply_value", result.output.answers)

    def test_caller_added_gap_cannot_export_credentials_in_generation_material(self):
        workflow, _ = worker()
        data = record()
        brief = replace(clarification(data), information_gap="Check synthetic-workflow-key here")
        package = workflow.evaluate(data, brief=brief)
        output = package.to_dict(include_materials=True)
        self.assertEqual(output["material_check"]["status"], "needs_review")
        self.assertIsNone(output["generation_task"])
        self.assertNotIn("synthetic-workflow-key", json.dumps(output))

    def test_selecting_purpose_after_evaluation_does_not_call_jev_again(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev.jsonl"
            with JevAuditLog(path) as audit:
                workflow, transport = worker(audit_log=audit)
                data = record()
                initial = workflow.evaluate(data)
                fingerprint = initial.to_dict()["action_policy_fingerprint"]
                attempts = len(transport.calls)
                workflow.action_policy = replace(POLICY, aligned_reply_above=4)
                prepared = workflow.prepare(initial, clarification(data))
                self.assertEqual(len(transport.calls), attempts)
                self.assertEqual(prepared.to_dict()["action_policy_fingerprint"], fingerprint)
                self.assertIsNotNone(prepared.to_dict()["generation_task"])
            logs = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([l["event"] for l in logs], ["post_evaluation", "material_preparation"])
        self.assertEqual(logs[-1]["new_attempt_count"], 0)

    def test_action_policy_snapshot_applies_to_one_evaluation_not_midflight_updates(self):
        workflow, transport = worker()
        original = workflow.action_policy
        original_handler = transport.handler
        def change_policy(body, data):
            original_handler(body, data)
            workflow.action_policy = replace(original, aligned_reply_above=4)
        transport.handler = change_policy
        first = workflow.evaluate(record()).to_dict()
        self.assertEqual(first["action_decision"]["action"], "reply")
        self.assertEqual(first["action_policy_fingerprint"], original.fingerprint)
        second = workflow.evaluate(record()).to_dict()
        self.assertEqual(second["action_decision"]["action"], "like")
        self.assertEqual(second["action_policy_fingerprint"], workflow.action_policy.fingerprint)

    def test_preparing_again_removes_previous_task_when_new_materials_are_missing(self):
        workflow, _ = worker()
        data = record()
        first = workflow.evaluate(data, brief=clarification(data))
        self.assertIsNotNone(first.to_dict()["generation_task"])
        bad = replace(clarification(data), prompt_type="P02")
        second = workflow.prepare(first, bad).to_dict(include_materials=True)
        self.assertIsNone(second["generation_task"])
        self.assertNotIn("generation_materials", second)
