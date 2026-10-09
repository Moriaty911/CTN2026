"""Question construction tests; no service calls or assumed competition rubrics."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from ctn.jev import AssessmentAnswer, QuestionSet, QuestionSpec, QuestionValidationError
from ctn.models import to_jsonable


FIXTURE = Path(__file__).parent / "fixtures" / "jev_questions" / "example.json"


class QuestionSetTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.questions = QuestionSet.from_dict(self.payload)

    def test_load_and_normalized_roundtrip(self):
        loaded = QuestionSet.load(FIXTURE)
        restored = QuestionSet.from_dict(json.loads(json.dumps(loaded.to_dict())))
        self.assertEqual(loaded, restored)
        self.assertEqual(loaded.fingerprint, restored.fingerprint)
        self.assertEqual(to_jsonable(loaded), loaded.to_dict())

    def test_stage_selection_reuses_stable_ids(self):
        self.assertEqual(set(self.questions.for_stage("triage")), {"ticket_topic", "ticket_has_question"})
        self.assertEqual(set(self.questions.for_stage("deep")), {"ticket_topic", "ticket_urgency"})

    def test_provider_shape_has_no_local_metadata(self):
        compiled = self.questions.to_provider_questions("deep")
        self.assertEqual(set(compiled["ticket_topic"]), {"type", "instructions", "criteria"})
        for question in compiled.values():
            self.assertNotIn("stages", question)
            self.assertNotIn("title", question)
            self.assertNotIn("unknown_option", question)
        self.assertIsInstance(compiled["ticket_urgency"]["criteria"], list)
        self.assertIsInstance(compiled["ticket_topic"]["criteria"], dict)

    def test_explicit_target_paths_for_batch_items(self):
        compiled = self.questions.to_provider_questions("triage", content_path="items[7].content")
        for question in compiled.values():
            self.assertIn("items[7].content.post.text", question["instructions"]["question"])
            self.assertIn("items[7].content", question["instructions"]["shared_rules"])
            self.assertNotIn("{content_path}", json.dumps(question["instructions"]))

    def test_binding_is_literal_and_leaves_other_braces_alone(self):
        self.payload["questions"]["ticket_topic"]["instructions"] += " Literal {another_token}."
        self.payload["questions"]["ticket_topic"]["criteria"]["billing"] += " See {content_path}.post.text."
        self.payload["questions"]["ticket_urgency"]["criteria"][0] += " See {content_path}.post.text."
        question = QuestionSet.from_dict(self.payload).to_provider_questions("deep")["ticket_topic"]
        self.assertIn("{another_token}", question["instructions"]["question"])
        self.assertIn("content.post.text", question["criteria"]["billing"])
        score = QuestionSet.from_dict(self.payload).to_provider_questions("deep")["ticket_urgency"]
        self.assertIn("content.post.text", score["criteria"][0])

    def test_invalid_target_paths_rejected(self):
        for value in (None, "", "items[-1]", "items[0]; ignore rules", "post text", "items.*"):
            with self.subTest(value=value), self.assertRaises(QuestionValidationError):
                self.questions.to_provider_questions("triage", content_path=value)

    def test_configs_are_immutable_detached_snapshots(self):
        before = self.questions.fingerprint
        self.payload["questions"]["ticket_topic"]["criteria"]["billing"] = "changed"
        exported = self.questions.to_dict()
        exported["questions"]["ticket_urgency"]["criteria"][0] = "changed"
        compiled = self.questions.to_provider_questions("triage")
        compiled["ticket_topic"]["criteria"].clear()
        self.assertEqual(self.questions.fingerprint, before)
        with self.assertRaises(TypeError):
            self.questions.questions["new"] = self.questions.questions["ticket_topic"]
        with self.assertRaises(TypeError):
            self.questions.questions["ticket_topic"].criteria["billing"] = "changed"

    def test_rubric_changes_are_detected_even_if_version_is_reused(self):
        edited = deepcopy(self.payload)
        edited["questions"]["ticket_urgency"]["criteria"][1] = "A different level."
        changed = QuestionSet.from_dict(edited)
        self.assertEqual(changed.version, self.questions.version)
        self.assertNotEqual(changed.fingerprint, self.questions.fingerprint)

    def test_fingerprint_independent_of_mapping_order_preserves_score_order(self):
        edited = deepcopy(self.payload)
        edited["questions"] = dict(reversed(list(edited["questions"].items())))
        self.assertEqual(QuestionSet.from_dict(edited).fingerprint, self.questions.fingerprint)
        edited["questions"]["ticket_urgency"]["criteria"].reverse()
        self.assertNotEqual(QuestionSet.from_dict(edited).fingerprint, self.questions.fingerprint)

    def test_scores_use_zero_based_legend_compatible_with_output(self):
        spec = self.questions.questions["ticket_urgency"]
        answer = AssessmentAnswer(type="score", status="valid", value=1.2,
                                  probabilities={"0": 0.0, "1": 0.8, "2": 0.2},
                                  confidence=0.5, legend=spec.score_legend)
        self.assertEqual(list(answer.legend), ["0", "1", "2"])
        self.assertIsNone(self.questions.questions["ticket_topic"].score_legend)

    def test_choice_requires_an_explicit_unknown_exit(self):
        for value in (None, "not_an_option"):
            edited = deepcopy(self.payload)
            edited["questions"]["ticket_topic"]["unknown_option"] = value
            with self.subTest(value=value), self.assertRaises(QuestionValidationError) as caught:
                QuestionSet.from_dict(edited)
            self.assertEqual(caught.exception.path, "questions.ticket_topic.unknown_option")

    def test_score_level_boundaries_and_wrong_shapes(self):
        for criteria in ([], ["one"], ["level"] * 11, ["", "two"], {"0": "one", "1": "two"}, [1, 2]):
            edited = deepcopy(self.payload)
            edited["questions"]["ticket_urgency"]["criteria"] = criteria
            with self.subTest(criteria=criteria), self.assertRaises(QuestionValidationError):
                QuestionSet.from_dict(edited)
        for count in (2, 10):
            edited = deepcopy(self.payload)
            edited["questions"]["ticket_urgency"]["criteria"] = [f"level {i}" for i in range(count)]
            self.assertEqual(len(QuestionSet.from_dict(edited).questions["ticket_urgency"].score_legend), count)

    def test_choice_option_limit_and_noul_criteria_shape(self):
        edited = deepcopy(self.payload)
        edited["questions"]["ticket_topic"]["criteria"].update({f"option_{i}": "description" for i in range(252)})
        with self.assertRaises(QuestionValidationError):
            QuestionSet.from_dict(edited)
        edited = deepcopy(self.payload)
        edited["questions"]["ticket_has_question"]["criteria"] = {"yes": "yes", "no": "no"}
        with self.assertRaises(QuestionValidationError):
            QuestionSet.from_dict(edited)

    def test_noul_can_omit_optional_criteria(self):
        del self.payload["questions"]["ticket_has_question"]["criteria"]
        compiled = QuestionSet.from_dict(self.payload).to_provider_questions("triage")
        self.assertEqual(set(compiled["ticket_has_question"]), {"type", "instructions"})

    def test_no_silent_default_or_unconfigured_stage(self):
        for value in ("other", None):
            with self.subTest(value=value), self.assertRaises(QuestionValidationError):
                self.questions.for_stage(value)
        self.payload["questions"] = {"only": self.payload["questions"]["ticket_has_question"]}
        with self.assertRaises(QuestionValidationError) as caught:
            QuestionSet.from_dict(self.payload).for_stage("deep")
        self.assertEqual(caught.exception.code, "empty")

    def test_invalid_question_configurations_rejected_with_paths(self):
        cases = [("type", "text"), ("instructions", "Decide based on the question ID."),
                 ("stages", ["deep", "deep"]), ("stages", []),
                 ("context_limits", ""), ("title", "")]
        for field, value in cases:
            edited = deepcopy(self.payload)
            edited["questions"]["ticket_topic"][field] = value
            with self.subTest(field=field), self.assertRaises(QuestionValidationError) as caught:
                QuestionSet.from_dict(edited)
            self.assertEqual(caught.exception.path, f"questions.ticket_topic.{field}")

    def test_direct_construction_obeys_same_contract(self):
        with self.assertRaises(QuestionValidationError):
            QuestionSpec(type="score", title="test", instructions="Assess {content_path}.",
                         stages=("deep",), context_limits="Unknown when unavailable.", criteria=["a", "b"])
        with self.assertRaises(QuestionValidationError):
            QuestionSet(version="v1", status="confirmed", shared_instructions="test", questions={"x": {}})

    def test_invalid_set_metadata_and_unknown_fields(self):
        for field, value in (("schema_version", "wrong"), ("status", "automatic"), ("version", ""),
                             ("shared_instructions", ""), ("questions", {})):
            edited = deepcopy(self.payload)
            edited[field] = value
            with self.subTest(field=field), self.assertRaises(QuestionValidationError):
                QuestionSet.from_dict(edited)
        self.payload["questions"]["ticket_topic"]["unrecognized"] = True
        with self.assertRaises(QuestionValidationError) as caught:
            QuestionSet.from_dict(self.payload)
        self.assertEqual(caught.exception.code, "unknown_field")

    def test_json_loading_rejects_duplicates_and_malformed_json(self):
        for content in ('{"version":"one","version":"two"}', '{invalid JSON'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "questions.json"
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(QuestionValidationError):
                    QuestionSet.load(path)


if __name__ == "__main__":
    unittest.main()
