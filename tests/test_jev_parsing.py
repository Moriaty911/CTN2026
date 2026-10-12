"""Bound response validation and availability tests; entirely offline."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
import unittest

from ctn.config import JevConfig
from ctn.jev import (
    HttpResponse, JevCallResult, JevClient, JevInput, JevOutput, JevPacker, JevResponseParser,
    PackingLimits, ParsingValidationError, QuestionSet, load_rubric,
)
from ctn.models import to_jsonable


FIXTURES = Path(__file__).parent / "fixtures"


def content(name="minimal"):
    return json.loads((FIXTURES / "jev_input" / (name + ".json")).read_text())


def response_for(packed):
    """Synthetic wire answers, not model-generated reference judgments."""
    answers = {}
    for key, question in packed.to_provider_body()["questions"].items():
        criteria = question.get("criteria")
        if question["type"] == "choice":
            option = "sufficient" if "sufficient" in criteria else next(iter(criteria))
            answers[key] = {"type": "choice", "choice": option,
                "probabilities": {item: float(item == option) for item in criteria}, "confidence": 1.0}
        elif question["type"] == "score":
            probabilities = {str(i): 0.0 for i in range(len(criteria))}
            probabilities.update({"1": 0.4, "2": 0.6})
            answers[key] = {"type": "score", "score": 1.6,
                "probabilities": probabilities, "confidence": 0.2,
                "legend": {str(i): text for i, text in enumerate(criteria)}}
        else:
            answers[key] = {"type": "noul", "noul": 0.0}
    return {"model": "jev-synthetic-fixture", "answers": answers,
            "usage": {"input_tokens": 120, "output_tokens": 24}}


def call_for(packed, response=None, **kwargs):
    data = response if response is not None else response_for(packed)
    return JevCallResult(status="success", call_id="synthetic-parse-call", request_id=packed.request_id,
        error_code=None, retryable=False, attempts=(), elapsed_s=0.125,
        packed_request=packed, _response_bytes=json.dumps(data).encode(), **kwargs)


def select(response, key, option):
    answer = response["answers"][key]
    answer["choice"] = option
    answer["probabilities"] = {k: float(k == option) for k in answer["probabilities"]}


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.packed = JevPacker().pack_post(content(), stage="deep")
        self.parser = JevResponseParser()

    def test_independently_rounded_wire_scores_retain_values_and_warn(self):
        # Numeric shapes observed during synthetic live integration, not gold
        # labels or expected judgments. Substitute this request's own legends.
        data = response_for(self.packed)
        observations = {
            "emotion_intensity": (.12, [.89, .11, 0, 0, 0]),
            "discussion_openness": (3.83, [0, .01, 0, .12, .87]),
            "response_specificity": (3.86, [0, 0, .01, .1, .89]),
        }
        for key, (score, probabilities) in observations.items():
            data["answers"][key].update(score=score,
                probabilities={str(i): p for i, p in enumerate(probabilities)})
        result = self.parser.parse(call_for(self.packed, data))
        self.assertEqual(result.status, "success")
        self.assertEqual(len([i for i in result.issues if i.code == "score_rounding_tolerance"]), 3)
        for key, (score, _) in observations.items():
            self.assertEqual(result.output.answers[key].value, score)
        self.assertEqual(JevOutput.from_dict(result.output.to_dict()), result.output)

    def test_score_rounding_does_not_accept_large_or_high_precision_mismatch(self):
        for score in (.15, .120001):
            with self.subTest(score=score):
                data = response_for(self.packed)
                data["answers"]["emotion_intensity"].update(score=score,
                    probabilities={"0": .89, "1": .11, "2": 0, "3": 0, "4": 0})
                answer = self.parser.parse(call_for(self.packed, data)).output.answers["emotion_intensity"]
                self.assertEqual(answer.status, "invalid")

    def test_live_shaped_client_response_becomes_bound_standard_output(self):
        data = response_for(self.packed)
        class Transport:
            def send(self, **kwargs):
                return HttpResponse(200, body=json.dumps(data).encode())
        call = JevClient(JevConfig(api_key="synthetic-local-test"), transport=Transport()).evaluate(self.packed)
        result = self.parser.parse(call)
        self.assertEqual(result.status, "success")
        output = result.output
        self.assertIsInstance(output, JevOutput)
        self.assertEqual(output.evaluation_id, call.call_id)
        self.assertEqual(output.request_id, self.packed.request_id)
        self.assertEqual(output.post_id, content()["post"]["post_id"])
        self.assertEqual(output.stage, "deep")
        self.assertEqual(set(output.answers), set(self.packed.question_ids))
        self.assertEqual(output.answers["emotion_intensity"].value, 1.6)
        self.assertEqual(output.answers["prompt_injection"].value, 0)
        self.assertIsNone(output.answers["prompt_injection"].confidence)
        self.assertEqual(output.usage.input_tokens, 120)
        self.assertEqual(JevOutput.from_dict(output.to_dict()), output)

    def test_missing_risk_answer_is_partial_and_never_becomes_zero(self):
        data = response_for(self.packed)
        del data["answers"]["prompt_injection"]
        result = self.parser.parse(call_for(self.packed, data))
        self.assertEqual(result.status, "partial")
        risk = result.output.answers["prompt_injection"]
        self.assertEqual(risk.status, "missing")
        self.assertIsNone(risk.value)
        self.assertNotIn("prompt_injection", result.output.usable_answers)
        self.assertIn("policy_stance", result.output.usable_answers)

    def test_empty_answers_marks_every_requested_answer_missing(self):
        data = response_for(self.packed)
        data["answers"] = {}
        result = self.parser.parse(call_for(self.packed, data))
        self.assertEqual(result.status, "failed")
        self.assertEqual(set(result.output.answers), set(self.packed.question_ids))
        self.assertEqual(result.output.usable_answers, {})
        self.assertTrue(all(a.status == "missing" for a in result.output.answers.values()))

    def test_unknown_classification_is_not_neutral_missing_or_zero_confidence(self):
        data = response_for(self.packed)
        select(data, "policy_stance", "unclear")
        data["answers"]["policy_stance"]["confidence"] = 0
        answer = self.parser.parse(call_for(self.packed, data)).output.answers["policy_stance"]
        self.assertEqual((answer.status, answer.value, answer.confidence), ("valid", "unclear", 0))

    def test_probabilities_must_match_all_request_options_without_filling(self):
        for mutation in (lambda a: a["probabilities"].pop("unclear"),
                         lambda a: a["probabilities"].update({"unrequested": 0.0})):
            data = response_for(self.packed)
            mutation(data["answers"]["policy_stance"])
            result = self.parser.parse(call_for(self.packed, data))
            answer = result.output.answers["policy_stance"]
            self.assertEqual(answer.status, "invalid")
            self.assertEqual(answer.issue.code, "criteria_mismatch")
            self.assertIsNone(answer.probabilities)

    def test_answer_type_fields_and_object_shape_validated(self):
        cases = [None, [], "unexpected", {"type": "score", "score": 0},
                 {"type": "noul"}, {"type": "noul", "noul": 0.1, "confidence": 0.9}]
        for answer in cases:
            with self.subTest(answer=answer):
                data = response_for(self.packed)
                data["answers"]["prompt_injection"] = answer
                result = self.parser.parse(call_for(self.packed, data))
                self.assertEqual(result.status, "partial")
                self.assertEqual(result.output.answers["prompt_injection"].status, "invalid")

    def test_invalid_probability_ranges_types_sums_and_choice_winners(self):
        edits = [lambda a: a.update(confidence=True), lambda a: a.update(confidence=1.01),
                 lambda a: a["probabilities"].update(yes=-0.1, no=1.1),
                 lambda a: a["probabilities"].update(yes="1"),
                 lambda a: a["probabilities"].update(yes=0.5),
                 lambda a: a.update(choice="no"), lambda a: a.update(choice="not_an_option")]
        for edit in edits:
            with self.subTest(edit=edit):
                data = response_for(self.packed)
                select(data, "policy_stance", "yes")
                edit(data["answers"]["policy_stance"])
                self.assertEqual(self.parser.parse(call_for(self.packed, data)).output.answers["policy_stance"].status, "invalid")

    def test_tied_choice_and_float_score_are_preserved_without_rounding(self):
        data = response_for(self.packed)
        answer = data["answers"]["policy_stance"]
        answer["choice"] = "yes"
        answer["probabilities"] = {key: 0.5 if key in {"yes", "no"} else 0 for key in answer["probabilities"]}
        answer["confidence"] = 0
        output = self.parser.parse(call_for(self.packed, data)).output
        self.assertEqual(output.answers["policy_stance"].status, "valid")
        self.assertEqual(output.answers["emotion_intensity"].value, 1.6)

    def test_score_legend_order_and_weighted_value_must_match_request(self):
        edits = [lambda a: a.update(score=2), lambda a: a.update(score=5),
                 lambda a: a["legend"].update({"0": "changed meaning"}),
                 lambda a: a["legend"].pop("4"),
                 lambda a: a["probabilities"].pop("4"), lambda a: a.update(score="1.6")]
        for edit in edits:
            with self.subTest(edit=edit):
                data = response_for(self.packed)
                edit(data["answers"]["emotion_intensity"])
                self.assertEqual(self.parser.parse(call_for(self.packed, data)).output.answers["emotion_intensity"].status, "invalid")

    def test_noul_uses_only_yes_probability_and_rejects_false_or_percentages(self):
        for value in (True, "0.3", -0.1, 1.1, 95):
            with self.subTest(value=value):
                data = response_for(self.packed)
                data["answers"]["prompt_injection"]["noul"] = value
                self.assertEqual(self.parser.parse(call_for(self.packed, data)).output.answers["prompt_injection"].status, "invalid")

    def test_declared_gate_rejection_removes_numeric_values_and_distributions(self):
        data = response_for(self.packed)
        select(data, "emotion_evidence", "unclear")
        result = self.parser.parse(call_for(self.packed, data), retain_raw=True)
        answer = result.output.answers["emotion_intensity"]
        self.assertEqual(answer.status, "insufficient_context")
        self.assertIsNone(answer.value)
        self.assertIsNone(answer.confidence)
        self.assertIsNone(answer.probabilities)
        self.assertEqual(answer.issue.fields, ("answers.emotion_evidence",))
        self.assertEqual(result.output.raw_response["answers"]["emotion_intensity"]["score"], 1.6)

    def test_missing_or_invalid_gate_blocks_dependents_including_noul(self):
        for invalid in (False, True):
            data = response_for(self.packed)
            if invalid:
                data["answers"]["specificity_evidence"]["type"] = "noul"
            else:
                del data["answers"]["specificity_evidence"]
            output = self.parser.parse(call_for(self.packed, data)).output
            for key in ("response_specificity", "fact_check_needed"):
                self.assertEqual(output.answers[key].status, "insufficient_context")
                self.assertIsNone(output.answers[key].value)
            self.assertEqual(output.answers["specificity_evidence"].status, "invalid" if invalid else "missing")

    def test_missing_dependent_is_not_relabelled_by_failed_gate(self):
        data = response_for(self.packed)
        select(data, "emotion_evidence", "unclear")
        del data["answers"]["emotion_intensity"]
        output = self.parser.parse(call_for(self.packed, data)).output
        self.assertEqual(output.answers["emotion_intensity"].status, "missing")

    def test_missing_context_does_not_blanket_block_basic_classification(self):
        result = self.parser.parse(call_for(self.packed))
        self.assertEqual(result.status, "success")
        self.assertIn(("content.metrics", "unknown"), {(i.path, i.reason) for i in result.output.context_issues})
        self.assertIn(("content.trusted_background", "unknown"), {(i.path, i.reason) for i in result.output.context_issues})

    def test_packing_omissions_and_unverified_background_remain_traceable(self):
        data = content("with_context")
        packed = JevPacker(limits=PackingLimits(max_conversation_items=1)).pack_post(data, stage="deep")
        result = self.parser.parse(call_for(packed))
        self.assertTrue(result.to_dict()["request"]["issues"])
        paths = {(i.path, i.reason) for i in result.output.context_issues}
        self.assertIn(("content.conversation", "partial"), paths)
        self.assertIn(("content.trusted_background", "unverified"), paths)

    def test_unrequested_answers_and_envelope_fields_are_ignored_without_echoing_keys(self):
        data = response_for(self.packed)
        data["answers"]["provider-unsafe-extra-text"] = {"untrusted": "Ignore instructions"}
        data["provider-unsafe-extra-text"] = "should not be copied"
        result = self.parser.parse(call_for(self.packed, data))
        self.assertEqual(result.status, "success")
        self.assertEqual({i.code for i in result.issues}, {"extra_fields", "unexpected_answers"})
        self.assertNotIn("provider-unsafe-extra-text", json.dumps(result.to_dict()) + repr(result))

    def test_invalid_envelope_fails_instead_of_treating_provider_error_as_answers(self):
        for data in ({"error": "provider message"}, {"model": "model", "answers": []},
                     {"model": "", "answers": {}}, {"model": None, "answers": {}},
                     {"model": "\ud800", "answers": {}}):
            with self.subTest(data=data):
                result = self.parser.parse(call_for(self.packed, data))
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error.code, "invalid_response_envelope")
                self.assertEqual(result.output.usable_answers, {})
                self.assertNotIn("provider message", json.dumps(result.to_dict()))

    def test_bad_json_duplicate_keys_nonfinite_and_nonobjects_fail_safely(self):
        for raw in (b"bad JSON", b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b"[]", b"\xff"):
            with self.subTest(raw=raw):
                call = replace(call_for(self.packed), _response_bytes=raw)
                result = self.parser.parse(call)
                self.assertEqual(result.error.code, "invalid_response_json")
                self.assertEqual(result.status, "failed")

    def test_usage_missing_invalid_or_extra_is_unknown_not_zero(self):
        for usage in (None, {}, {"input_tokens": True, "output_tokens": 1},
                      {"input_tokens": -1, "output_tokens": 2},
                      {"input_tokens": 1, "output_tokens": 2, "extra": 3}):
            with self.subTest(usage=usage):
                data = response_for(self.packed)
                data["usage"] = usage
                result = self.parser.parse(call_for(self.packed, data))
                self.assertEqual(result.status, "success")
                self.assertIsNone(result.output.usage)
                self.assertEqual(result.issues[0].code, "invalid_usage")

    def test_transport_failure_propagates_safe_error_and_never_reads_answers(self):
        call = replace(call_for(self.packed), status="error", error_code="rate_limited", retryable=True)
        result = self.parser.parse(call, retain_raw=True)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.code, "rate_limited")
        self.assertTrue(result.output.error.retryable)
        self.assertIsNone(result.output.raw_response)
        self.assertEqual(result.output.usable_answers, {})

    def test_deferred_packing_without_safe_identity_does_not_invent_post_id(self):
        packed = JevPacker().pack_post({})
        call = JevClient(JevConfig()).evaluate(packed)
        result = self.parser.parse(call)
        self.assertIsNone(result.output)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.code, "packing_deferred")
        self.assertEqual(result.targets, ())

    def test_deferred_known_target_can_carry_failure_without_any_valid_values(self):
        packed = JevPacker(limits=PackingLimits(max_state_bytes=1)).pack_post(content())
        result = self.parser.parse(JevClient(JevConfig()).evaluate(packed))
        self.assertEqual(result.output.post_id, content()["post"]["post_id"])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.output.usable_answers, {})

    def test_caller_work_item_and_call_request_ids_cannot_be_crossed(self):
        call = call_for(self.packed)
        for tested in (replace(call, request_id="other-request"), call):
            result = self.parser.parse(tested, expected_request_id="other-request")
            self.assertEqual(result.error.code, "request_binding_mismatch")
            self.assertEqual(result.output.usable_answers, {})

    def test_changed_question_set_with_same_version_is_rejected_by_fingerprint(self):
        data = load_rubric("post").to_dict()
        data["shared_instructions"] += " Changed criteria interpretation."
        result = self.parser.parse(call_for(self.packed), question_set=QuestionSet.from_dict(data))
        self.assertEqual(result.error.code, "question_set_mismatch")
        self.assertEqual(result.output.usable_answers, {})

    def test_version_stage_question_ids_and_fingerprint_are_checked(self):
        for edit in ({"question_set_version": "old"}, {"question_set_fingerprint": "old"},
                     {"question_ids": ("another_id",)}, {"stage": "triage"}):
            with self.subTest(edit=edit):
                result = self.parser.parse(call_for(replace(self.packed, **edit), response_for(self.packed)))
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error.code, "question_set_mismatch")

    def test_changed_body_and_changed_targets_are_rejected(self):
        data = self.packed.to_provider_body()
        data["state"]["content"]["post"]["post_id"] = "crossed-post"
        body = json.dumps(data)
        digest = hashlib.sha256(body.encode()).hexdigest()
        for packed in (replace(self.packed, _body_json=body), replace(self.packed, _body_json=body, request_id=digest),
                       replace(self.packed, targets=(replace(self.packed.targets[0], content_path="other"),))):
            with self.subTest(request=packed.request_id):
                result = self.parser.parse(call_for(packed, response_for(self.packed)))
                self.assertEqual(result.error.code, "request_binding_mismatch")

    def test_actual_model_version_can_differ_from_requested_alias(self):
        data = response_for(self.packed)
        data["model"] = "jev-synthetic-resolved-version"
        result = self.parser.parse(call_for(self.packed, data))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.output.model, "jev-synthetic-resolved-version")

    def test_raw_audit_is_opt_in_immutable_and_excluded_from_default_handoff(self):
        call = call_for(self.packed)
        normal = self.parser.parse(call)
        retained = self.parser.parse(call, retain_raw=True)
        self.assertIsNone(normal.output.raw_response)
        self.assertNotIn("raw_response", retained.to_dict()["output"])
        self.assertNotIn(content()["post"]["text"], json.dumps(to_jsonable(retained)))
        self.assertNotIn("_body_json", repr(retained))
        wire = retained.to_dict(include_raw=True)
        wire["output"]["raw_response"]["answers"]["prompt_injection"]["noul"] = 1
        self.assertEqual(retained.output.raw_response["answers"]["prompt_injection"]["noul"], 0)
        with self.assertRaises(TypeError):
            retained.output.answers["emotion_intensity"].probabilities["1"] = 1
        with self.assertRaises(FrozenInstanceError):
            retained.error = None

    def test_reordered_batch_completion_retains_original_index_and_identity(self):
        second = content()
        second["post"]["post_id"] = "second-post"
        packed = JevPacker().pack_batch([content(), second])
        results = [self.parser.parse(call_for(item)) for item in reversed(packed)]
        self.assertEqual([r.to_dict()["request"]["batch_index"] for r in results], [1, 0])
        self.assertEqual([r.output.post_id for r in results], ["second-post", "sample-minimal"])

    def test_custom_confirmed_rubric_is_supported_only_when_explicitly_supplied(self):
        payload = load_rubric("post").to_dict()
        payload["version"] = "synthetic-custom-v1"
        payload["questions"] = {"prompt_injection": payload["questions"]["prompt_injection"]}
        rubric = QuestionSet.from_dict(payload)
        packed = JevPacker().pack_post(content(), question_set=rubric)
        call = call_for(packed)
        self.assertEqual(self.parser.parse(call).error.code, "question_set_mismatch")
        self.assertEqual(self.parser.parse(call, question_set=rubric).status, "success")

    def test_invalid_caller_arguments_raise_safe_validation_errors(self):
        for call, kwargs in (({}, {}), (replace(call_for(self.packed), packed_request=None), {}),
                             (call_for(self.packed), {"question_set": {}}),
                             (call_for(self.packed), {"retain_raw": "yes"}),
                             (call_for(self.packed), {"expected_request_id": 12})):
            with self.subTest(kwargs=list(kwargs)):
                with self.assertRaises(ParsingValidationError):
                    self.parser.parse(call, **kwargs)


class ComparisonParsingTests(unittest.TestCase):
    def setUp(self):
        self.current = content()
        self.current["post"]["post_id"] = "current-post"
        self.historical = content()
        self.historical["post"]["post_id"] = "historical-post"
        self.packed = JevPacker().pack_comparison(self.current, self.historical)
        self.parser = JevResponseParser()

    def test_pair_binding_preserves_both_targets_for_every_answer(self):
        result = self.parser.parse(call_for(self.packed))
        self.assertEqual(result.output.post_id, "current-post")
        self.assertEqual([(t.role, t.post_id) for t in result.targets],
                         [("current", "current-post"), ("historical", "historical-post")])
        for binding in result.to_dict()["request"]["answer_bindings"].values():
            self.assertEqual([t["post_id"] for t in binding["targets"]], ["current-post", "historical-post"])

    def test_noncomparable_pair_never_exposes_usable_conflict_relation(self):
        data = response_for(self.packed)
        select(data, "pair_comparability", "different_time_or_version")
        select(data, "claim_relation", "factual_conflict")
        result = self.parser.parse(call_for(self.packed, data))
        self.assertEqual(result.output.answers["pair_comparability"].value, "different_time_or_version")
        self.assertEqual(result.output.answers["claim_relation"].status, "insufficient_context")
        self.assertNotIn("claim_relation", result.output.usable_answers)

    def test_attribution_is_retained_for_later_author_disagreement_decision(self):
        data = response_for(self.packed)
        select(data, "claim_attribution", "not_both_endorsed")
        select(data, "claim_relation", "factual_conflict")
        result = self.parser.parse(call_for(self.packed, data))
        self.assertEqual(result.output.answers["claim_relation"].value, "factual_conflict")
        self.assertEqual(result.output.answers["claim_attribution"].value, "not_both_endorsed")
        self.assertNotIn("action", result.to_dict())
        self.assertNotIn("credibility", result.to_dict())

    def test_swapping_pair_metadata_without_swapping_body_fails(self):
        packed = replace(self.packed, targets=tuple(reversed(self.packed.targets)))
        result = self.parser.parse(call_for(packed, response_for(self.packed)))
        self.assertEqual(result.error.code, "request_binding_mismatch")


if __name__ == "__main__":
    unittest.main()
