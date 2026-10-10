"""Rubric construction and fixture checks, not model accuracy evaluation."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from ctn.jev import JevInput, QuestionSet, QuestionValidationError, load_rubric, relative_stance


REVIEW_CASES = Path(__file__).parent / "fixtures" / "jev_rubrics" / "review_cases.json"


class RubricTests(unittest.TestCase):
    def test_confirmations_are_per_field_not_all_labels_of_reviewed_scenarios(self):
        cases = json.loads(REVIEW_CASES.read_text(encoding="utf-8"))
        confirmed = cases["confirmed_reference_fields"]
        self.assertEqual(set(confirmed), set(cases["reviewed_scenario_cases"]))
        for group in ("post_cases", "comparison_cases"):
            for case in cases[group]:
                fields = set(case.get("expected_labels", {})) | set(case.get("expected_levels", {})) | set(case.get("expected_yes", {})) | set(case["unavailable_answers"])
                self.assertTrue(set(confirmed.get(case["id"], ())) <= fields)
        self.assertEqual(confirmed["voluntary_not_mandate"], ["policy_stance"])
        self.assertNotIn("emotion_intensity", confirmed["voluntary_not_mandate"])

    def test_both_rubrics_are_confirmed_baselines_with_separate_versions(self):
        post, comparison = load_rubric("post"), load_rubric("comparison")
        self.assertEqual(post.status, "confirmed")
        self.assertEqual(comparison.status, "confirmed")
        self.assertEqual(post.version, "ctn-post.v1")
        self.assertEqual(comparison.version, "ctn-comparison.v1")
        for rubric in (post, comparison):
            self.assertEqual(rubric, QuestionSet.from_dict(json.loads(json.dumps(rubric.to_dict()))))

    def test_post_stage_selection_and_exact_target_binding(self):
        rubric = load_rubric("post")
        self.assertEqual(set(rubric.for_stage("triage")), {"relevance", "topic", "policy_stance", "prompt_injection"})
        compiled = rubric.to_provider_questions("deep", content_path="items[3].content")
        self.assertIn("items[3].content.post.text", compiled["policy_stance"]["instructions"]["question"])
        self.assertNotIn("{content_path}", json.dumps(compiled))
        self.assertTrue(set(rubric.for_stage("triage")) <= set(compiled))

    def test_comparison_binds_both_posts_and_does_not_run_in_triage(self):
        rubric = load_rubric("comparison")
        compiled = rubric.to_provider_questions("deep", content_path="pairs[4]")
        for question in compiled.values():
            self.assertIn("pairs[4].current.post.text", question["instructions"]["question"])
            self.assertIn("pairs[4].historical.post.text", question["instructions"]["question"])
        with self.assertRaises(QuestionValidationError):
            rubric.for_stage("triage")

    def test_numeric_scales_all_declare_a_local_availability_gate(self):
        rubric = load_rubric("post")
        scores = [spec for spec in rubric.questions.values() if spec.type == "score"]
        self.assertTrue(scores)
        for spec in scores:
            self.assertEqual(len(spec.criteria), 5)
            self.assertEqual(set(spec.score_legend), {"0", "1", "2", "3", "4"})
            gate = rubric.questions[spec.availability_question_id]
            self.assertEqual(gate.type, "choice")
            self.assertEqual(spec.available_when, ("sufficient",))
            self.assertNotIn(gate.unknown_option, spec.available_when)

    def test_availability_metadata_never_enters_provider_questions(self):
        for name in ("post", "comparison"):
            compiled = load_rubric(name).to_provider_questions("deep")
            for question in compiled.values():
                self.assertEqual(set(question), {"type", "instructions", "criteria"})
                self.assertNotIn("availability_question_id", question)
                self.assertNotIn("available_when", question)

    def test_relative_stance_for_both_assignments(self):
        self.assertEqual(relative_stance("yes", "yes"), "aligned")
        self.assertEqual(relative_stance("no", "yes"), "opposing")
        self.assertEqual(relative_stance("yes", "no"), "opposing")
        self.assertEqual(relative_stance("no", "no"), "aligned")
        self.assertEqual(relative_stance("mixed", "yes"), "mixed")
        self.assertEqual(relative_stance("neutral", "no"), "neutral")

    def test_unknown_assignment_or_position_is_not_guessed(self):
        for assignment in (None, "unknown"):
            for position in ("yes", "no", "neutral", "mixed", "unclear"):
                self.assertEqual(relative_stance(position, assignment), "unknown")
        self.assertEqual(relative_stance("unclear", "yes"), "unknown")
        for position, assignment in (("aligned", "yes"), (True, "yes"), ("yes", "enemy"), ("yes", {})):
            with self.subTest(position=position, assignment=assignment), self.assertRaises(QuestionValidationError):
                relative_stance(position, assignment)

    def test_unknown_rubric_and_non_string_names_rejected(self):
        for name in ("../post", "default", None, {}):
            with self.subTest(name=name), self.assertRaises(QuestionValidationError):
                load_rubric(name)

    def test_invalid_gate_references_are_rejected(self):
        payload = load_rubric("post").to_dict()
        for gate_id in ("not_present", "emotion_intensity", "prompt_injection"):
            edited = deepcopy(payload)
            edited["questions"]["emotion_intensity"]["availability_question_id"] = gate_id
            with self.subTest(gate_id=gate_id), self.assertRaises(QuestionValidationError):
                QuestionSet.from_dict(edited)

    def test_gate_must_be_ungated_and_present_in_all_dependent_stages(self):
        edited = load_rubric("post").to_dict()
        edited["questions"]["emotion_evidence"].update(
            availability_question_id="openness_evidence", available_when=["sufficient"])
        with self.assertRaises(QuestionValidationError):
            QuestionSet.from_dict(edited)
        edited = load_rubric("post").to_dict()
        edited["questions"]["emotion_evidence"]["stages"] = ["triage"]
        with self.assertRaises(QuestionValidationError):
            QuestionSet.from_dict(edited)

    def test_invalid_available_options_and_orphan_metadata_rejected(self):
        payload = load_rubric("post").to_dict()
        for options in ([], ["unknown_value"], ["unclear"], ["sufficient", "sufficient"], "sufficient", [True]):
            edited = deepcopy(payload)
            edited["questions"]["emotion_intensity"]["available_when"] = options
            with self.subTest(options=options), self.assertRaises(QuestionValidationError):
                QuestionSet.from_dict(edited)
        edited = deepcopy(payload)
        edited["questions"]["emotion_intensity"]["availability_question_id"] = None
        with self.assertRaises(QuestionValidationError):
            QuestionSet.from_dict(edited)

    def test_review_inputs_use_valid_synthetic_records(self):
        cases = json.loads(REVIEW_CASES.read_text(encoding="utf-8"))
        self.assertEqual(cases["status"], "partially_reviewed_human_references")
        reviewed = cases["reviewed_scenario_cases"]
        self.assertEqual(len(reviewed), len(set(reviewed)))
        self.assertTrue(set(reviewed) <= {c["id"] for group in ("post_cases", "comparison_cases") for c in cases[group]})
        identifiers = []
        for case in cases["post_cases"]:
            record = JevInput.from_dict(case["input"])
            self.assertEqual(record.source.kind, "synthetic")
            identifiers.append(record.post.post_id)
        for case in cases["comparison_cases"]:
            current = JevInput.from_dict(case["input"]["current"])
            historical = JevInput.from_dict(case["input"]["historical"])
            self.assertNotEqual(current.post.post_id, historical.post.post_id)
            self.assertNotEqual(current.post.author_id, historical.post.author_id)
            self.assertEqual(current.source.kind, "synthetic")
            self.assertEqual(historical.source.kind, "synthetic")
            identifiers.extend([current.post.post_id, historical.post.post_id])
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_review_references_match_types_and_express_unavailability_separately(self):
        cases = json.loads(REVIEW_CASES.read_text(encoding="utf-8"))
        for name, collection in (("post", "post_cases"), ("comparison", "comparison_cases")):
            rubric = load_rubric(name)
            for case in cases[collection]:
                with self.subTest(case=case["id"]):
                    for key, label in case["expected_labels"].items():
                        self.assertEqual(rubric.questions[key].type, "choice")
                        self.assertIn(label, rubric.questions[key].criteria)
                    for key, level in case.get("expected_levels", {}).items():
                        self.assertEqual(rubric.questions[key].type, "score")
                        self.assertIn(str(level), rubric.questions[key].score_legend)
                    for key, truth in case.get("expected_yes", {}).items():
                        self.assertEqual(rubric.questions[key].type, "noul")
                        self.assertIs(type(truth), bool)
                    for key in case["unavailable_answers"]:
                        spec = rubric.questions[key]
                        self.assertNotIn(key, case.get("expected_levels", {}))
                        self.assertNotIn(key, case.get("expected_yes", {}))
                        self.assertNotIn(key, case["expected_labels"])
                        gate_label = case["expected_labels"][spec.availability_question_id]
                        self.assertNotIn(gate_label, spec.available_when)


if __name__ == "__main__":
    unittest.main()
