"""Selected-purpose material checks, comparison gates and human fact records."""

from dataclasses import replace
import json
import unittest

from ctn.jev import (
    BackgroundReference, FactVerification, JevPacker, MaterialBrief, MaterialChecker, MaterialUse,
    MaterialValidationError, TextSpan, build_handoff, build_comparison_handoff, load_rubric,
)
from tests.test_jev_actions import assessed
from tests.test_jev_evaluator import evaluator, content, choose


def span(text, path=("content", "post", "text"), material_set="target"):
    return TextSpan(path, 0, len(text), text, material_set)


def post_fixture(assigned="yes", handler=None):
    data = content()
    data["task_context"] = {"assigned_side": assigned}
    data["post"]["text"] = "Does version A require proof for park entry? I am concerned about fair exceptions."
    source = {"kind": "synthetic", "observed_at": "2026-10-10T00:00:00Z"}
    data["conversation"] = {"status": "partial", "items": [
        {"role": "comment", "post_id": "comment-a", "text": "What exceptions are proposed?", "source": source},
        {"role": "comment", "post_id": "comment-b", "text": "Which version describes those exceptions?", "source": source}]}
    reference = BackgroundReference("synthetic-reference", "Version B exempts public parks; check which version applies.", "synthetic-source")
    data["trusted_background"] = {"items": [vars(reference)]}
    worker, _ = evaluator(handler, post_questions=load_rubric("post", version="v2"),
        packer=JevPacker(verified_references=(reference,)))
    flow = worker.evaluate(data)
    package = build_handoff(flow)
    focus = span(data["post"]["text"])
    evidence = span(reference.text, ("verified_background", "items", 0, "text"))
    return worker, data, package, focus, evidence, reference


class MaterialTests(unittest.TestCase):
    def test_support_answer_argument_and_concern_require_named_material(self):
        _, _, package, focus, evidence, _ = post_fixture()
        for purpose_id, use in (("P01", "addition"), ("P02", "answer_basis"), ("P03", "argument_basis"), ("P07", "response_basis")):
            with self.subTest(purpose=purpose_id):
                checker = MaterialChecker()
                missing = checker.check(package, MaterialBrief(purpose_id, focus))
                self.assertEqual(missing.status, "needs_materials")
                self.assertIn(use, missing.needs)
                result = checker.check(package, MaterialBrief(purpose_id, focus, (MaterialUse(evidence, use),)))
                self.assertEqual(result.status, "ready")
                self.assertNotIn(focus.quote, json.dumps(result.to_dict()))
                task = result.to_dict(include_materials=True)["task"]
                self.assertEqual(task["evaluated_materials"]["content"]["conversation"]["status"], "partial")
                self.assertFalse(task["execution_authorized"])
                self.assertTrue(result.to_dict()["downstream_semantic_review_required"])

    def test_support_cannot_relabel_opposing_as_aligned(self):
        _, _, package, focus, evidence, _ = post_fixture(handler=lambda body, data: choose(data, "policy_stance", "no"))
        result = MaterialChecker().check(package, MaterialBrief("P01", focus, (MaterialUse(evidence, "addition"),)))
        self.assertIn("aligned_position", result.needs)

    def test_changed_quote_out_of_bounds_omitted_material_and_wrong_target_hold(self):
        _, _, package, focus, evidence, _ = post_fixture()
        for bad in (replace(focus, quote="changed"), replace(focus, end=focus.end + 1),
                    replace(focus, path=("content", "conversation", "items", 0, "text"))):
            with self.subTest(bad=bad):
                result = MaterialChecker().check(package, MaterialBrief("P08", bad, information_gap="Which version?"))
                self.assertEqual(result.status, "needs_materials")
        absent = replace(evidence, path=("verified_background", "items", 100, "text"))
        result = MaterialChecker().check(package, MaterialBrief("P02", focus, (MaterialUse(absent, "answer_basis"),)))
        self.assertIn("support_quote_mismatch", result.needs)

    def test_clarification_requires_specific_gap_without_automatic_fallback(self):
        _, _, package, focus, _, _ = post_fixture()
        checker = MaterialChecker()
        self.assertIn("specific_information_gap", checker.check(package, MaterialBrief("P08", focus)).needs)
        result = checker.check(package, MaterialBrief("P08", focus, information_gap="Which policy version applies?"))
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.prompt_type, "P08")

    def test_registered_background_is_not_human_fact_verification(self):
        _, data, package, focus, evidence, reference = post_fixture()
        brief = MaterialBrief("P06", focus, (MaterialUse(evidence, "verified_correction"),))
        missing = MaterialChecker().check(package, brief)
        self.assertIn("applicable_human_fact_verification", missing.needs)
        proof = FactVerification(data["post"]["post_id"], focus.fingerprint, evidence.fingerprint,
            reference.reference_id, reference.source_id, "synthetic-reviewer", "2026-10-10T02:00:00Z",
            "version A/B park access", "the supplied proposal versions", "park visitors", "version applicability reviewed")
        result = MaterialChecker(verifications=(proof,)).check(package, brief)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.to_dict(include_materials=True)["task"]["human_verifications"][0]["reviewer_id"], "synthetic-reviewer")
        wrong = replace(proof, target_post_id="other-target")
        self.assertIn("applicable_human_fact_verification", MaterialChecker(verifications=(wrong,)).check(package, brief).needs)

    def test_correction_rejects_unregistered_platform_claim_as_source(self):
        _, _, package, focus, _, _ = post_fixture()
        brief = MaterialBrief("P06", focus, (MaterialUse(focus, "verified_correction"),))
        self.assertIn("registered_correction_source", MaterialChecker().check(package, brief).needs)

    def test_two_traceable_comments_with_known_sample_scope_can_be_summarized(self):
        _, data, package, focus, _, _ = post_fixture()
        uses = tuple(MaterialUse(span(item["text"], ("content", "conversation", "items", i, "text")), "comment")
                     for i, item in enumerate(data["conversation"]["items"]))
        result = MaterialChecker().check(package, MaterialBrief("P09", focus, uses, sample_scope="supplied_comments"))
        self.assertEqual(result.status, "ready")
        task = result.to_dict(include_materials=True)["task"]
        self.assertEqual(len(task["comments"]), 2)
        self.assertIn("do not assert a thread-wide consensus", " ".join(task["constraints"]))
        missing = MaterialChecker().check(package, MaterialBrief("P09", focus, uses[:1], sample_scope="supplied_comments"))
        self.assertIn("multiple_comments_and_known_sample_scope", missing.needs)
        missing = MaterialChecker().check(package, MaterialBrief("P09", focus, uses))
        self.assertIn("multiple_comments_and_known_sample_scope", missing.needs)

    def test_risk_failed_context_observe_and_deferred_block_material_route(self):
        for handler in (
            lambda body, data: data["answers"]["prompt_injection"].update(noul=.5),
            lambda body, data: data["answers"].clear(),
            lambda body, data: choose(data, "relevance", "unclear"),
            lambda body, data: choose(data, "relevance", "unrelated"),
        ):
            _, _, package, focus, _, _ = post_fixture(handler=handler)
            self.assertEqual(MaterialChecker().check(package, MaterialBrief("P08", focus, information_gap="Which version?")).status, "blocked")

    def pair_setup(self, prompt_type, overrides=None):
        def handler(body, data):
            if "claim_relation" in data["answers"]:
                choose(data, "pair_comparability", "comparable")
                choose(data, "claim_attribution", "both_endorsed")
                choose(data, "comparison_basis", "policy_proposal" if prompt_type == "P04" else "factual")
                choose(data, "claim_relation", "proposal_difference" if prompt_type == "P04" else "factual_conflict")
                if overrides:
                    for key, value in overrides.items():
                        choose(data, key, value)
        worker, record, package, focus, _, _ = post_fixture(handler=handler)
        historic = content(post_id="synthetic-history")
        historic["post"]["text"] = "Version A does not require proof for park entry."
        paired = build_comparison_handoff(worker.compare(record, historic))
        secondary = span(historic["post"]["text"], ("content", "historical", "post", "text"), "comparison")
        return worker, record, historic, package, paired, MaterialBrief(prompt_type, focus, secondary=secondary)

    def test_proposals_and_factual_cross_check_require_all_pair_gates(self):
        for prompt in ("P04", "P05"):
            _, _, _, package, paired, brief = self.pair_setup(prompt)
            result = MaterialChecker().check(package, brief, comparison=paired)
            self.assertEqual(result.status, "ready")
            task = result.to_dict(include_materials=True)["task"]
            self.assertEqual(len(task["comparison_assessment"]["assessment"]["request"]["targets"]), 2)
            for key, value in (("pair_comparability", "different_time_or_version"), ("claim_attribution", "not_both_endorsed"),
                               ("claim_relation", "value_difference"), ("comparison_basis", "value_priority")):
                with self.subTest(prompt=prompt, key=key):
                    _, _, _, package, paired, brief = self.pair_setup(prompt, {key: value})
                    held = MaterialChecker().check(package, brief, comparison=paired)
                    self.assertEqual(held.status, "needs_materials")
                    self.assertIn(key, held.needs)

    def test_pair_with_same_id_but_changed_current_context_is_not_borrowed(self):
        worker, record, historical, package, _, brief = self.pair_setup("P05")
        record["metrics"] = {"likes": 100}
        other = build_comparison_handoff(worker.compare(record, historical))
        result = MaterialChecker().check(package, brief, comparison=other)
        self.assertIn("comparison_current_snapshot_mismatch", result.needs)

    def test_contract_rejects_invalid_enum_path_offsets_and_false_scope(self):
        focus = span("text")
        for make in (
            lambda: TextSpan(("content", "post", "text"), True, 2, "t"),
            lambda: TextSpan(("content", "post", "text"), 2, 1, "t"),
            lambda: TextSpan(("content", "post", "author_id"), 0, 1, "t"),
            lambda: MaterialBrief({}, focus),
            lambda: MaterialBrief("P09", focus, sample_scope="whole_thread"),
            lambda: MaterialUse(focus, {}),
        ):
            with self.subTest(make=make), self.assertRaises(MaterialValidationError):
                make()

    def test_unicode_character_offsets_preserve_source_context(self):
        text = "是，不是强制注射。"
        data = content()
        data["post"]["text"] = text
        worker, _ = evaluator(post_questions=load_rubric("post", version="v2"))
        package = build_handoff(worker.evaluate(data))
        focus = TextSpan(("content", "post", "text"), 2, len(text), text[2:])
        check = MaterialChecker().check(package, MaterialBrief("P08", focus, information_gap="Which requirement?"))
        self.assertEqual(check.status, "ready")
        self.assertEqual(check.to_dict(include_materials=True)["task"]["focus"]["source_text"], text)

    def test_downstream_json_selection_round_trip_and_unknown_fields(self):
        _, _, _, focus, evidence, _ = post_fixture()
        brief = MaterialBrief("P02", focus, (MaterialUse(evidence, "answer_basis"),))
        wire = brief.to_dict(include_quotes=True)
        self.assertEqual(MaterialBrief.from_dict(json.loads(json.dumps(wire))), brief)
        self.assertNotIn(focus.quote, json.dumps(brief.to_dict()))
        wire["execute"] = True
        with self.assertRaises(MaterialValidationError):
            MaterialBrief.from_dict(wire)
        del wire["execute"]
        wire["focus"]["fingerprint"] = "wrong"
        with self.assertRaises(MaterialValidationError):
            MaterialBrief.from_dict(wire)
