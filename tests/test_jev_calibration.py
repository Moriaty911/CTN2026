"""Versioned human references, missing feedback and fixed-answer replay."""

from copy import deepcopy
from dataclasses import replace
import json
import unittest

from ctn.jev import (
    CalibrationSample, CalibrationValidationError, HumanReference, ObservedFeedback,
    RoutingPolicy, QuestionSet, load_rubric, replay_routes, review_assessments, compare_assessment_runs,
)
from tests.test_jev_evaluator import evaluator, content, choose


def reference(question_id="policy_stance", kind="label", value="yes", status="confirmed"):
    return HumanReference(question_id, kind, value, status, "human.v1", "2026-10-10T04:00:00Z")


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.worker, self.transport = evaluator()
        self.flow = self.worker.evaluate(content())

    def sample(self, stage=None, **kwargs):
        return CalibrationSample.capture("case-a", stage or self.flow.deep, **kwargs)

    def test_round_trip_is_metadata_only_and_detached(self):
        sample = self.sample(references=(reference(),))
        data = sample.to_dict()
        self.assertEqual(CalibrationSample.from_dict(data), sample)
        self.assertNotIn(content()["post"]["text"], json.dumps(data))
        self.assertNotIn("raw_response", json.dumps(data))
        self.assertIsNone(data["feedback"])
        data["case_id"] = "changed"
        self.assertEqual(sample.to_dict()["case_id"], "case-a")

    def test_confirmed_labels_have_metrics_candidate_disputed_do_not(self):
        refs = (reference(), reference("topic", value="public_access", status="candidate"),
                reference("expression_function", value="argument", status="disputed"))
        result = review_assessments((self.sample(references=refs),), load_rubric("post"))
        self.assertEqual(result["label_accuracy"], 1)
        self.assertEqual(result["confirmed_label_count"], 1)
        self.assertEqual(result["candidate_or_disputed_count"], 2)
        self.assertEqual(result["feedback_missing_count"], 1)
        self.assertIsNone(result["rows"][1]["match"])

    def test_fractional_score_has_absolute_error_not_integer_match(self):
        result = review_assessments((self.sample(references=(reference("discussion_openness", "level", 2),)),), load_rubric("post"))
        self.assertAlmostEqual(result["mean_absolute_level_error"], 0.4)
        self.assertEqual(result["available_level_count"], 1)

    def test_boolean_signal_has_no_automatic_threshold_or_accuracy(self):
        result = review_assessments((self.sample(references=(reference("direct_question", "yes_no", False),)),), load_rubric("post"))
        self.assertEqual(result["rows"][0]["actual"], 0)
        self.assertIsNone(result["rows"][0]["match"])
        self.assertIsNone(result["label_accuracy"])

    def test_missing_answer_is_not_expected_unavailable_context(self):
        worker, _ = evaluator(lambda body, data: data["answers"].pop("discussion_openness", None))
        sample = CalibrationSample.capture("missing", worker.evaluate(content()).deep,
            references=(reference("discussion_openness", "unavailable", None),))
        result = review_assessments((sample,), load_rubric("post"))
        self.assertFalse(result["rows"][0]["match"])
        self.assertEqual(result["rows"][0]["answer_status"], "missing")

    def test_context_gate_counts_as_unavailable(self):
        worker, _ = evaluator(lambda body, data: choose(data, "openness_evidence", "unclear") if "openness_evidence" in data["answers"] else None)
        sample = CalibrationSample.capture("context", worker.evaluate(content()).deep,
            references=(reference("discussion_openness", "unavailable", None),))
        self.assertTrue(review_assessments((sample,), load_rubric("post"))["rows"][0]["match"])

    def test_feedback_preserves_null_and_has_no_causal_credit(self):
        output = self.flow.deep.parsed.output
        feedback = ObservedFeedback(output.post_id, "action-1", "2099-01-01T00:00:00Z", "upstream-1",
                                    (("observed_likes", None), ("score_change", -1)))
        sample = self.sample(feedback=feedback)
        data = sample.to_dict()["feedback"]
        self.assertIsNone(data["metrics"]["observed_likes"])
        self.assertEqual(data["metrics"]["score_change"], -1)
        self.assertEqual(data["causal_attribution"], "not_established")
        self.assertEqual(CalibrationSample.from_dict(sample.to_dict()), sample)

    def test_wrong_target_or_early_feedback_rejected(self):
        for post, timestamp in (("wrong", "2099-01-01T00:00:00Z"), (self.flow.deep.parsed.output.post_id, "2000-01-01T00:00:00Z")):
            with self.subTest(post=post), self.assertRaises(CalibrationValidationError):
                self.sample(feedback=ObservedFeedback(post, "a", timestamp, "source"))

    def test_route_replay_changes_threshold_without_call_or_adoption(self):
        worker, transport = evaluator(lambda body, data: choose(data, "relevance", "unrelated", 0.8))
        flow = worker.evaluate(content())
        sample = CalibrationSample.capture("threshold", flow.triage)
        before = len(transport.calls)
        result = replay_routes((sample,), RoutingPolicy(), RoutingPolicy(unrelated_min_probability=0.9))
        self.assertEqual(result["changed_count"], 1)
        self.assertEqual(result["rows"][0]["baseline"]["route"], "observe")
        self.assertEqual(result["rows"][0]["candidate"]["route"], "deep")
        self.assertTrue(result["rows"][0]["candidate_deep_evaluation_required"])
        self.assertFalse(result["adopted"])
        self.assertEqual(len(transport.calls), before)

    def test_replay_does_not_mistake_deep_or_pair_for_triage(self):
        for sample in (self.sample(), self.sample(stage=self.worker.compare(content(post_id="a"), content(post_id="b")))):
            with self.subTest(sample=sample), self.assertRaises(CalibrationValidationError):
                replay_routes((sample,), RoutingPolicy(), RoutingPolicy())

    def test_replay_missing_risk_still_reviews(self):
        worker, _ = evaluator(lambda body, data: data["answers"].pop("prompt_injection"))
        sample = CalibrationSample.capture("risk", worker.evaluate(content()).triage)
        result = replay_routes((sample,), RoutingPolicy(), RoutingPolicy(injection_review_threshold=0.9))
        self.assertEqual(result["rows"][0]["candidate"]["route"], "review")

    def test_wrong_rubric_or_unrequested_reference_rejected(self):
        altered = load_rubric("post").to_dict()
        altered["version"] = "ctn-post.v2"
        new = QuestionSet.from_dict(altered)
        with self.assertRaises(CalibrationValidationError):
            review_assessments((self.sample(),), new)
        for ref in (reference("missing", value="yes"), reference("policy_stance", value="unrequested"),
                    reference("policy_stance", "level", 2), reference("discussion_openness", "level", 5)):
            with self.subTest(ref=ref), self.assertRaises(CalibrationValidationError):
                review_assessments((self.sample(references=(ref,)),), load_rubric("post"))

    def test_duplicate_case_ids_and_reference_ids_rejected(self):
        with self.assertRaises(CalibrationValidationError):
            self.sample(references=(reference(), reference()))
        with self.assertRaises(CalibrationValidationError):
            review_assessments((self.sample(), self.sample()), load_rubric("post"))
        sample = self.sample(stage=self.flow.triage)
        with self.assertRaises(CalibrationValidationError):
            replay_routes((sample, sample), RoutingPolicy(), RoutingPolicy())

    def test_reference_and_feedback_numeric_validation(self):
        with self.assertRaises(CalibrationValidationError):
            HumanReference("q", "label", "x", "confirmed", "v1")
        for value in (True, float("nan"), 10 ** 1000):
            with self.subTest(value=type(value)), self.assertRaises(CalibrationValidationError):
                ObservedFeedback("p", "a", "2099-01-01T00:00:00Z", "s", (("m", value),))

    def test_import_rejects_raw_body_and_mismatched_binding(self):
        source = self.sample().to_dict()
        for mutate in (
            lambda d: d["request"].update(body={"content": "must not retain"}),
            lambda d: d["output"].update(raw_response={}),
            lambda d: d["request"].update(request_id="wrong"),
            lambda d: d["request"]["targets"][0].update(post_id="wrong"),
        ):
            data = deepcopy(source)
            mutate(data)
            with self.assertRaises(CalibrationValidationError):
                CalibrationSample.from_dict(data)

    def test_generator_and_empty_sets_have_correct_missingness(self):
        sample = self.sample()
        report = review_assessments((s for s in (sample,)), load_rubric("post"))
        self.assertEqual(report["feedback_missing_count"], 1)
        empty = review_assessments((), load_rubric("post"))
        self.assertIsNone(empty["label_accuracy"])
        self.assertIsNone(empty["mean_absolute_level_error"])

    def test_different_question_versions_can_be_compared_after_separate_evaluation(self):
        rubric = load_rubric("post").to_dict()
        rubric["version"] = "ctn-post.v2"
        worker, _ = evaluator(lambda body, data: choose(data, "policy_stance", "no"), post_questions=QuestionSet.from_dict(rubric))
        second = CalibrationSample.capture("case-a", worker.evaluate(content()).deep)
        report = compare_assessment_runs((self.sample(),), (second,))
        row = report["rows"][0]
        self.assertEqual(row["baseline"]["question_set_version"], "ctn-post.v1")
        self.assertEqual(row["candidate"]["question_set_version"], "ctn-post.v2")
        self.assertEqual(row["answer_differences"][0]["question_id"], "policy_stance")
        self.assertFalse(row["same_evaluation"])
        self.assertFalse(report["adopted"])

    def test_assessment_comparison_rejects_changed_input_and_records_missing_samples(self):
        other = CalibrationSample.capture("case-a", self.worker.evaluate(content(post_id="changed")).deep)
        with self.assertRaises(CalibrationValidationError):
            compare_assessment_runs((self.sample(),), (other,))
        report = compare_assessment_runs((self.sample(),), ())
        self.assertEqual(report["missing_count"], 1)
        self.assertEqual(report["rows"][0]["status"], "missing_candidate")
