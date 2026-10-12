"""Bound downstream materials, no external consumers or live services."""

from dataclasses import replace
import json
import unittest

from ctn.jev import HandoffValidationError, build_handoff, build_comparison_handoff, JevPacker, PackingLimits
from tests.test_jev_evaluator import evaluator, content, choose


class HandoffTests(unittest.TestCase):
    def test_exact_evaluated_text_is_explicitly_requested_and_detached(self):
        worker, transport = evaluator()
        flow = worker.evaluate(content("with_context"))
        package = build_handoff(flow)
        self.assertNotIn(content("with_context")["post"]["text"], json.dumps(package.to_dict()))
        self.assertNotIn("materials", package.to_dict())
        data = package.to_dict(include_materials=True)
        self.assertEqual(data["materials"]["content"], transport.calls[-1]["state"]["content"])
        self.assertNotIn("questions", data["materials"])
        self.assertFalse(data["execution_authorized"])
        self.assertIsNone(data["generation_task"])
        self.assertEqual(data["action_decision"]["status"], "pending_rules")
        self.assertIsNone(data["action_decision"]["action"])
        data["materials"]["content"]["post"]["text"] = "changed"
        self.assertNotEqual(package.to_dict(include_materials=True)["materials"]["content"]["post"]["text"], "changed")
        self.assertNotIn("synthetic-test-key", json.dumps(package.to_dict(include_materials=True)))

    def test_observe_review_failure_and_context_are_preserved(self):
        handlers = (
            lambda body, data: choose(data, "relevance", "unrelated"),
            lambda body, data: data["answers"]["prompt_injection"].update(noul=0.5),
            lambda body, data: data["answers"].clear(),
            lambda body, data: choose(data, "relevance", "unclear"),
        )
        for handler, status in zip(handlers, ("observe", "needs_review", "failed", "needs_context")):
            with self.subTest(status=status):
                worker, _ = evaluator(handler)
                package = build_handoff(worker.evaluate(content()))
                self.assertEqual(package.to_dict()["flow"]["status"], status)
                self.assertIsNone(package.to_dict()["generation_task"])

    def test_deferred_has_no_invented_text_or_target(self):
        worker, _ = evaluator()
        package = build_handoff(worker.evaluate({})).to_dict(include_materials=True)
        self.assertEqual(package["flow"]["status"], "deferred")
        self.assertIsNone(package["materials"])
        self.assertEqual(package["targets"], [])

    def test_mismatched_deep_snapshot_rejected(self):
        worker, _ = evaluator()
        first, second = worker.evaluate(content()), worker.evaluate(content(post_id="different"))
        with self.assertRaises(HandoffValidationError):
            build_handoff(replace(first, deep=second.deep))

    def test_bad_request_binding_rejected(self):
        worker, _ = evaluator()
        flow = worker.evaluate(content())
        wrong = replace(flow.deep.parsed, output=replace(flow.deep.parsed.output, request_id="wrong"))
        with self.assertRaises(HandoffValidationError):
            build_handoff(replace(flow, deep=replace(flow.deep, parsed=wrong)))

    def test_comparison_keeps_both_authors_texts_and_identity(self):
        worker, _ = evaluator()
        stage = worker.compare(content(post_id="current"), content(post_id="historical"))
        package = build_comparison_handoff(stage).to_dict(include_materials=True)
        self.assertEqual([t["post_id"] for t in package["targets"]], ["current", "historical"])
        self.assertEqual(set(package["materials"]["content"]), {"current", "historical"})
        self.assertIsNone(package["generation_task"])

    def test_deep_omission_is_visible_not_replaced_by_original_input(self):
        worker, transport = evaluator(packer=JevPacker(model="jev-latest", limits=PackingLimits(max_conversation_items=0)))
        flow = worker.evaluate(content("with_context"))
        data = build_handoff(flow).to_dict(include_materials=True)
        self.assertEqual(data["materials"]["content"], transport.calls[-1]["state"]["content"])
        self.assertTrue(data["flow"]["deep"]["assessment"]["request"]["issues"])

    def test_input_types_and_material_switch_validated(self):
        with self.assertRaises(HandoffValidationError):
            build_handoff({})
        with self.assertRaises(HandoffValidationError):
            build_comparison_handoff({})
        worker, _ = evaluator()
        with self.assertRaises(HandoffValidationError):
            build_handoff(worker.evaluate(content())).to_dict(include_materials=1)
