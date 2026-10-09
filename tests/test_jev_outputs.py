from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ctn.jev import (
    AnswerIssue, AssessmentAnswer, EvaluationError, JevInput, JevOutput,
    OutputValidationError, TokenUsage,
)
from ctn.models import Post, to_jsonable
from ctn.telemetry import JsonlWriter


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / "jev_output" / f"{name}.json").read_text())


class JevOutputTests(unittest.TestCase):
    def test_success_retains_all_typed_answers_and_metadata(self):
        output = JevOutput.from_dict(fixture("success"))
        self.assertEqual(output.status, "success")
        self.assertEqual(output.answers["intent"].value, "question")
        self.assertEqual(output.answers["openness"].value, 1.6)
        self.assertEqual(output.answers["openness"].legend["2"], "Open")
        self.assertIsNone(output.answers["is_question"].confidence)
        self.assertEqual(output.usage.input_tokens, 120)
        self.assertEqual(output.model, "sample-model-version")
        self.assertEqual(set(output.usable_answers), set(output.answers))

    def test_partial_results_do_not_turn_missing_risk_into_zero(self):
        output = JevOutput.from_dict(fixture("partial"))
        self.assertEqual(output.status, "partial")
        self.assertEqual(set(output.usable_answers), {"is_question"})
        self.assertIsNone(output.answers["risk"].value)
        self.assertIsNone(output.answers["reply_value"].value)
        self.assertEqual(output.answers["reply_value"].issue.fields, ("conversation.parent",))
        self.assertIsNone(output.model)
        self.assertIsNone(output.usage)

    def test_failure_retains_association_without_exposing_usable_answers(self):
        output = JevOutput.from_dict(fixture("failed"))
        self.assertEqual(output.status, "failed")
        self.assertEqual(output.post_id, "sample-minimal")
        self.assertTrue(output.error.retryable)
        self.assertEqual(dict(output.usable_answers), {})

    def test_error_overrides_otherwise_valid_answers(self):
        data = fixture("success")
        data.update(status="failed", error={"code": "invalid_response", "message": "Metadata invalid."})
        output = JevOutput.from_dict(data)
        self.assertEqual(output.status, "failed")
        self.assertEqual(dict(output.usable_answers), {})
        self.assertEqual(output.answers["is_question"].value, 0.95)

    def test_all_unavailable_answers_are_failed(self):
        data = fixture("partial")
        del data["answers"]["is_question"]
        data["status"] = "failed"
        output = JevOutput.from_dict(data)
        self.assertEqual(output.status, "failed")
        self.assertEqual(dict(output.usable_answers), {})

    def test_input_limitations_do_not_invalidate_unrelated_valid_classification(self):
        data = json.loads((FIXTURES / "jev_input" / "minimal.json").read_text())
        data["post"].update(kind="reply", parent_id="not-supplied")
        content = JevInput.from_dict(data)
        output = JevOutput.for_input(
            content, evaluation_id="e-1", stage="triage", question_set_version="test-v1",
            answers={"is_question": AssessmentAnswer("noul", "valid", 0.95)},
        )
        self.assertEqual(output.status, "success")
        self.assertEqual(output.post_id, content.post.post_id)
        self.assertEqual(output.source_kind, "synthetic")
        self.assertIn("conversation.parent", {issue.path for issue in output.context_issues})
        self.assertEqual(output.context_issues, content.context_issues)

    def test_bound_input_identity_cannot_be_overridden(self):
        content = JevInput.from_dict(json.loads((FIXTURES / "jev_input" / "minimal.json").read_text()))
        with self.assertRaises(OutputValidationError):
            JevOutput.for_input(content, post_id="other-post")

    def test_zero_low_confidence_and_unknown_remain_distinct(self):
        data = fixture("success")
        data["answers"] = {
            "risk": {"type": "noul", "status": "valid", "value": 0},
            "intent": {"type": "choice", "status": "valid", "value": "a",
                       "probabilities": {"a": 0.5, "b": 0.5}, "confidence": 0},
        }
        output = JevOutput.from_dict(data)
        self.assertEqual(output.status, "success")
        self.assertEqual(output.answers["risk"].value, 0)
        self.assertEqual(output.answers["intent"].confidence, 0)
        self.assertIsNone(output.answers["risk"].confidence)

    def test_false_completeness_status_rejected(self):
        for name, status in (("partial", "success"), ("success", "failed"), ("failed", "success")):
            with self.subTest(name=name):
                data = fixture(name)
                data["status"] = status
                with self.assertRaises(OutputValidationError) as caught:
                    JevOutput.from_dict(data)
                self.assertEqual(caught.exception.path, "status")

    def test_raw_response_is_frozen_and_opt_in_for_audit(self):
        data = fixture("success")
        data["raw_response"] = {"answers": {"sample": {"noul": 0.95}}, "extra": ["audit-only"]}
        output = JevOutput.from_dict(data)
        data["raw_response"]["extra"].append("changed")
        self.assertNotIn("raw_response", output.to_dict())
        audit = output.to_dict(include_raw=True)
        self.assertEqual(audit["raw_response"]["extra"], ["audit-only"])
        audit["raw_response"]["answers"]["sample"]["noul"] = 0
        self.assertEqual(output.raw_response["answers"]["sample"]["noul"], 0.95)
        with self.assertRaises(TypeError):
            output.raw_response["extra"] = ()
        self.assertNotIn("audit-only", repr(output))

    def test_normalized_roundtrip_for_every_result_state(self):
        for name in ("success", "partial", "failed"):
            with self.subTest(name=name):
                data = fixture(name)
                data["raw_response"] = {"test": [name]}
                output = JevOutput.from_dict(data)
                wire = json.loads(json.dumps(output.to_dict(include_raw=True), allow_nan=False))
                self.assertEqual(JevOutput.from_dict(wire), output)

    def test_answer_maps_are_detached_and_immutable(self):
        data = fixture("success")
        output = JevOutput.from_dict(data)
        data["answers"]["intent"]["probabilities"]["question"] = 0
        wire = output.to_dict()
        wire["answers"]["openness"]["legend"]["2"] = "Changed"
        self.assertEqual(output.answers["intent"].probabilities["question"], 0.8)
        self.assertEqual(output.answers["openness"].legend["2"], "Open")
        with self.assertRaises(TypeError):
            output.answers["risk"] = AssessmentAnswer("noul", "valid", 0)
        with self.assertRaises(FrozenInstanceError):
            output.answers["intent"].value = "opinion"

    def test_invalid_numbers_rejected(self):
        for value in (True, "0.5", float("nan"), float("inf"), -0.1, 1.1, 10 ** 400):
            with self.subTest(value=value):
                with self.assertRaises(OutputValidationError):
                    AssessmentAnswer("noul", "valid", value)
        for value in (True, -1, float("nan")):
            data = fixture("success")
            data["latency_ms"] = value
            with self.assertRaises(OutputValidationError):
                JevOutput.from_dict(data)

    def test_noul_never_invents_confidence_or_a_distribution(self):
        for extras in ({"confidence": 0.9}, {"probabilities": {"yes": 1}}, {"legend": {"0": "No"}}):
            with self.subTest(extras=extras):
                with self.assertRaises(OutputValidationError):
                    AssessmentAnswer("noul", "valid", 0.95, **extras)

    def test_choice_distribution_membership_sum_and_winner_checked(self):
        base = fixture("success")["answers"]["intent"]
        for changes in (
            {"value": "not-an-option"}, {"value": "opinion"},
            {"probabilities": {"question": 0.3, "opinion": 0.2}},
            {"probabilities": {"question": True, "opinion": 0}},
            {"probabilities": {}}, {"confidence": None},
            {"confidence": 1.1}, {"legend": {"0": "Wrong type"}},
        ):
            with self.subTest(changes=changes):
                data = deepcopy(base)
                data.update(changes)
                with self.assertRaises(OutputValidationError):
                    AssessmentAnswer.from_dict(data)

    def test_score_requires_matching_levels_and_weighted_value(self):
        base = fixture("success")["answers"]["openness"]
        for changes in (
            {"value": 4}, {"value": 1.4}, {"value": None},
            {"legend": {"1": "Low", "2": "High"}},
            {"legend": {"0": "Only level"}},
            {"probabilities": {"0": 0.5, "1": 0.5}},
            {"legend": {"0": "", "1": "Unclear", "2": "Open"}},
        ):
            with self.subTest(changes=changes):
                data = deepcopy(base)
                data.update(changes)
                with self.assertRaises(OutputValidationError):
                    AssessmentAnswer.from_dict(data)
        data = deepcopy(base)
        data["value"] += 1e-8
        self.assertEqual(AssessmentAnswer.from_dict(data).status, "valid")

    def test_unavailable_answers_need_reason_and_cannot_expose_zero(self):
        for status in ("missing", "invalid", "insufficient_context"):
            with self.subTest(status=status):
                with self.assertRaises(OutputValidationError):
                    AssessmentAnswer("noul", status)
                with self.assertRaises(OutputValidationError):
                    AssessmentAnswer("noul", status, 0, issue=AnswerIssue(status, "Unavailable."))
                answer = AssessmentAnswer.unavailable("noul", status, "Unavailable.")
                self.assertIsNone(answer.value)
        with self.assertRaises(OutputValidationError):
            AssessmentAnswer.unavailable("noul", "valid", "Wrong.")

    def test_metadata_and_schema_validation(self):
        for name, value in (
            ("schema_version", "jev-output.v2"), ("evaluation_id", ""),
            ("post_id", None), ("source_kind", "other"), ("stage", "publish"),
            ("question_set_version", ""), ("evaluated_at", "2026-10-09T10:00:00"),
            ("usage", {"input_tokens": True, "output_tokens": 0}),
            ("error", {"code": "timeout", "message": "Timeout.", "retryable": "yes"}),
            ("context_issues", [{"path": "metrics", "reason": "invented"}]),
            ("raw_response", {"bad": float("nan")}), ("undefined", "do-not-echo"),
        ):
            with self.subTest(name=name):
                data = fixture("success")
                data[name] = value
                with self.assertRaises(OutputValidationError) as caught:
                    JevOutput.from_dict(data)
                self.assertNotIn("do-not-echo", str(caught.exception))

    def test_missing_required_fields_and_answer_error_paths(self):
        for name in ("schema_version", "evaluation_id", "post_id", "source_kind",
                     "stage", "question_set_version", "status", "answers", "evaluated_at"):
            with self.subTest(name=name):
                data = fixture("success")
                del data[name]
                with self.assertRaises(OutputValidationError) as caught:
                    JevOutput.from_dict(data)
                self.assertEqual(caught.exception.path, name)
        data = fixture("success")
        data["answers"]["openness"]["probabilities"] = {}
        with self.assertRaises(OutputValidationError) as caught:
            JevOutput.from_dict(data)
        self.assertEqual(caught.exception.path, "answers.openness.probabilities")

    def test_direct_construction_validates_records(self):
        with self.assertRaises(OutputValidationError):
            TokenUsage(input_tokens=-1, output_tokens=0)
        with self.assertRaises(OutputValidationError):
            EvaluationError("timeout", "Timeout.", retryable=1)
        with self.assertRaises(OutputValidationError):
            AnswerIssue("missing", "Missing.", fields=["metrics"])
        with self.assertRaises(OutputValidationError):
            JevOutput("e", "p", "synthetic", "triage", "v1", {"bad": {"type": "noul"}})

    def test_empty_evaluation_requires_failure_reason(self):
        data = fixture("failed")
        del data["error"]
        with self.assertRaises(OutputValidationError) as caught:
            JevOutput.from_dict(data)
        self.assertEqual(caught.exception.path, "error")

    def test_provider_response_is_not_mistaken_for_normalized_output(self):
        provider_response = {"model": "sample-model", "answers": {
            "is_question": {"type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 10, "output_tokens": 2}}
        with self.assertRaises(OutputValidationError):
            JevOutput.from_dict(provider_response)

    def test_existing_jsonl_writer_handles_output_and_legacy_records(self):
        data = fixture("success")
        data["raw_response"] = {"audit": "keep-out-of-default-log"}
        output = JevOutput.from_dict(data)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "evaluations.jsonl"
            writer = JsonlWriter(path)
            try:
                writer.write({"evaluation": output, "post": Post("sample-minimal", "author", "Text.")})
            finally:
                writer.close()
            logged = json.loads(path.read_text())
        self.assertEqual(logged["evaluation"]["schema_version"], "jev-output.v1")
        self.assertEqual(logged["evaluation"]["status"], "success")
        self.assertNotIn("raw_response", logged["evaluation"])
        self.assertEqual(logged["post"]["text"], "Text.")
        self.assertEqual(to_jsonable(output.to_dict(include_raw=True))["raw_response"], data["raw_response"])


if __name__ == "__main__":
    unittest.main()
