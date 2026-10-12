"""Request/evidence boundary tests; no model or platform access."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import unittest

from ctn.jev import (
    BackgroundReference, JevInput, JevPacker, PackingLimits,
    PackingValidationError, load_rubric,
)
from ctn.models import to_jsonable


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name="minimal"):
    return json.loads((FIXTURES / "jev_input" / f"{name}.json").read_text())


def reference():
    return BackgroundReference(reference_id="approved-fixture", source_id="local-fixture-source",
                               text="Synthetic local reference; not a competition fact.")


def reference_payload(ref):
    return {"reference_id": ref.reference_id, "source_id": ref.source_id, "text": ref.text}


class PackingTests(unittest.TestCase):
    def test_minimal_input_builds_request_without_inventing_context(self):
        data = fixture()
        result = JevPacker().pack_post(data)
        self.assertEqual(result.status, "ready")
        body = result.to_provider_body()
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(set(body["questions"]), {"relevance", "topic", "policy_stance", "prompt_injection"})
        packed = body["state"]["content"]
        self.assertEqual(packed["post"]["text"], data["post"]["text"])
        self.assertIsNone(packed["metrics"])
        self.assertIsNone(packed["author"])
        self.assertEqual(packed["source"], JevInput.from_dict(data).to_dict()["source"])
        self.assertEqual(body["state"]["verified_background"]["status"], "not_provided")

    def test_verbatim_text_preserves_whitespace_negation_and_unicode(self):
        data = fixture("with_context")
        data["post"]["text"] = "  I do NOT endorse this quotation.\n不支持。  "
        result = JevPacker().pack_post(data, stage="deep")
        packed = result.to_provider_body()["state"]["content"]
        self.assertEqual(packed["post"], JevInput.from_dict(data).to_dict()["post"])
        self.assertEqual(packed["conversation"], JevInput.from_dict(data).to_dict()["conversation"])
        self.assertEqual(packed["metrics"]["likes"], 2)
        self.assertEqual(len(result.question_ids), 15)

    def test_body_and_input_mutation_do_not_change_snapshot(self):
        data = fixture("with_context")
        result = JevPacker().pack_post(data)
        data["post"]["text"] = "changed upstream"
        body = result.to_provider_body()
        body["state"]["content"]["post"]["text"] = "changed detached body"
        self.assertEqual(result.to_provider_body()["state"]["content"]["post"]["text"], "What evidence supports that claim?")
        with self.assertRaises(FrozenInstanceError):
            result.status = "deferred"

    def test_budget_preserves_parent_and_drops_whole_large_comment(self):
        data = fixture("with_context")
        data["conversation"]["items"][0]["text"] = "I do not endorse the quoted claim."
        data["conversation"]["items"][1]["text"] = "Long context. " * 1000
        result = JevPacker(limits=PackingLimits(max_state_bytes=2500)).pack_post(data)
        self.assertEqual(result.status, "ready")
        packed = result.to_provider_body()["state"]["content"]
        self.assertEqual([i["role"] for i in packed["conversation"]["items"]], ["parent"])
        self.assertEqual(packed["conversation"]["items"][0]["text"], data["conversation"]["items"][0]["text"])
        self.assertEqual(packed["post"]["text"], data["post"]["text"])
        self.assertEqual(packed["conversation"]["status"], "partial")
        self.assertIn(("content.conversation.items[1]", "budget_omitted"), {(i.path, i.reason) for i in result.issues})
        self.assertLessEqual(result.state_bytes, 2500)

    def test_item_limit_prioritizes_parent_even_when_comment_comes_first(self):
        data = fixture("with_context")
        data["conversation"]["items"].reverse()
        result = JevPacker(limits=PackingLimits(max_conversation_items=1)).pack_post(data)
        packed = result.to_provider_body()["state"]["content"]
        self.assertEqual(packed["conversation"]["items"][0]["post_id"], "sample-parent")
        self.assertIn(("content.conversation.items[0]", "item_limit"), {(i.path, i.reason) for i in result.issues})

    def test_large_optional_list_does_not_displace_target_with_omission_descriptions(self):
        data = fixture("with_context")
        template = deepcopy(data["conversation"]["items"][1])
        for index in range(198):
            item = {**template, "post_id": f"additional-comment-{index}", "text": "A synthetic comment."}
            data["conversation"]["items"].append(item)
        result = JevPacker(limits=PackingLimits(max_conversation_items=2, max_state_bytes=2500)).pack_post(data)
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(result.to_provider_body()["state"]["content"]["conversation"]["items"]), 2)
        self.assertEqual(sum(i.reason == "item_limit" for i in result.issues), 198)
        wire = result.to_provider_body()["state"]["packing"]["limitations"]
        self.assertIn({"path": "content.conversation.items", "reason": "item_limit", "count": 198}, wire)
        self.assertLessEqual(result.state_bytes, 2500)

    def test_retained_conversation_order_remains_upstream_order(self):
        data = fixture("with_context")
        data["conversation"]["items"].reverse()
        packed = JevPacker().pack_post(data).to_provider_body()["state"]["content"]
        self.assertEqual([i["post_id"] for i in packed["conversation"]["items"]], ["sample-comment", "sample-parent"])

    def test_missing_parent_and_truncation_are_visible_without_blanket_rejection(self):
        data = fixture("with_context")
        result = JevPacker(limits=PackingLimits(max_conversation_items=0)).pack_post(data, stage="deep")
        self.assertEqual(result.status, "ready")
        limitations = result.to_provider_body()["state"]["packing"]
        self.assertIn({"path": "content.conversation.parent", "reason": "unknown"}, limitations["context_issues"])
        result = JevPacker().pack_post(data)
        self.assertIn({"path": "content.conversation.items[1].text_complete", "reason": "truncated"},
                      result.to_provider_body()["state"]["packing"]["context_issues"])

    def test_zero_unknown_and_official_level_survive_context_selection(self):
        data = fixture("with_context")
        data["metrics"].update(likes=0, replies=None)
        data["author"].update(platform_level="demo-platform-level", profile_text="large profile " * 1000)
        result = JevPacker(limits=PackingLimits(max_state_bytes=2500)).pack_post(data)
        packed = result.to_provider_body()["state"]["content"]
        self.assertEqual(packed["metrics"]["likes"], 0)
        self.assertIsNone(packed["metrics"]["replies"])
        self.assertEqual(packed["author"]["platform_level"], "demo-platform-level")
        self.assertEqual(packed["author"]["observed_at"], data["author"]["observed_at"])
        self.assertIsNone(packed["author"]["profile_text"])

    def test_history_limit_retains_partial_status_and_known_timestamps(self):
        data = fixture("with_context")
        result = JevPacker(limits=PackingLimits(max_history_events=0)).pack_post(data)
        packed = result.to_provider_body()["state"]["content"]
        self.assertEqual(packed["interaction_history"], {"status": "partial", "events": []})
        self.assertEqual(packed["source"]["observed_at"], data["source"]["observed_at"])
        self.assertIn(("content.interaction_history.events[0]", "item_limit"), {(i.path, i.reason) for i in result.issues})

    def test_oversize_required_text_is_deferred_without_a_truncated_body(self):
        data = fixture()
        data["post"]["text"] = "Do not shorten this. " * 3000
        result = JevPacker().pack_post(data)
        self.assertEqual(result.status, "deferred")
        self.assertEqual(result.issues[0].reason, "core_state_over_budget")
        with self.assertRaises(PackingValidationError):
            result.to_provider_body()
        self.assertNotIn("body", result.to_dict(include_body=True))

    def test_request_budget_counts_question_instructions(self):
        result = JevPacker(limits=PackingLimits(max_request_bytes=2000)).pack_post(fixture(), stage="deep")
        self.assertEqual(result.status, "deferred")
        self.assertEqual(result.issues[0].reason, "core_request_over_budget")

    def test_unicode_is_measured_in_utf8_bytes_not_character_count(self):
        data = fixture()
        data["post"]["text"] = "字" * 2000
        result = JevPacker(limits=PackingLimits(max_state_bytes=4000)).pack_post(data)
        self.assertEqual(result.status, "deferred")
        full = JevPacker().pack_post(data)
        body = full.to_provider_body()
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(full.request_bytes, len(encoded))
        self.assertEqual(full.to_provider_bytes(), encoded)
        self.assertGreater(full.state_bytes, len(data["post"]["text"]))

    def test_invalid_optional_data_and_unknown_fields_are_not_silently_dropped(self):
        for extra in ({"metrics": {"likes": True}}, {"raw": {"api_key": "fake-value-not-to-echo"}}):
            data = {**fixture(), **extra}
            result = JevPacker().pack_post(data)
            self.assertEqual(result.status, "deferred")
            self.assertTrue(result.issues[0].reason.startswith("invalid_input:"))
            self.assertNotIn("fake-value-not-to-echo", json.dumps(result.to_dict()))

    def test_unverified_background_is_not_forwarded_or_promoted(self):
        data = fixture("with_context")
        result = JevPacker().pack_post(data)
        state = result.to_provider_body()["state"]
        self.assertIsNone(state["content"]["trusted_background"])
        self.assertEqual(state["verified_background"]["items"], [])
        self.assertEqual(state["verified_background"]["status"], "unavailable")
        self.assertNotIn(data["trusted_background"]["items"][0]["text"], json.dumps(state))
        self.assertEqual(result.issues[0].reason, "unverified_reference")

    def test_registry_requires_exact_id_source_and_text_match(self):
        ref = reference()
        packer = JevPacker(verified_references=(ref,))
        data = fixture()
        data["trusted_background"] = {"items": [reference_payload(ref)]}
        matched = packer.pack_post(data).to_provider_body()["state"]
        self.assertEqual(matched["verified_background"]["items"], [reference_payload(ref)])
        self.assertEqual(matched["verified_background"]["status"], "verified")
        for field in ("reference_id", "source_id", "text"):
            changed = deepcopy(data)
            changed["trusted_background"]["items"][0][field] += " changed"
            result = packer.pack_post(changed)
            self.assertEqual(result.to_provider_body()["state"]["verified_background"]["items"], [])
            self.assertEqual(result.issues[0].reason, "unverified_reference")

    def test_verified_reference_duplicates_and_budget_omissions_are_recorded(self):
        ref = reference()
        data = fixture()
        data["trusted_background"] = {"items": [reference_payload(ref), reference_payload(ref)]}
        result = JevPacker(verified_references=(ref,)).pack_post(data)
        self.assertEqual(len(result.to_provider_body()["state"]["verified_background"]["items"]), 1)
        self.assertIn("duplicate_reference", {i.reason for i in result.issues})
        large = replace(ref, text="Large local reference. " * 2000)
        data["trusted_background"]["items"] = [reference_payload(large)]
        result = JevPacker(verified_references=(large,), limits=PackingLimits(max_state_bytes=2500)).pack_post(data)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.to_provider_body()["state"]["verified_background"]["items"], [])
        self.assertIn("budget_omitted", {i.reason for i in result.issues})

    def test_empty_background_is_distinct_from_missing_background(self):
        data = fixture()
        data["trusted_background"] = {"items": []}
        self.assertEqual(JevPacker().pack_post(data).to_provider_body()["state"]["verified_background"]["status"], "empty")

    def test_credentials_in_target_profile_or_dropped_reference_defer_without_echo(self):
        fake = "apikey_" + "x" * 40
        for location in ("post", "author", "trusted_background"):
            data = fixture()
            if location == "post":
                data["post"]["text"] = fake
            elif location == "author":
                data["author"] = {"profile_text": fake}
            else:
                data["trusted_background"] = {"items": [{"reference_id": "fake", "source_id": "fake", "text": fake}]}
            result = JevPacker().pack_post(data)
            self.assertEqual(result.status, "deferred")
            self.assertEqual(result.issues[0].reason, "credential_detected")
            self.assertNotIn(fake, json.dumps(result.to_dict(include_body=True)))
            self.assertNotIn(fake, repr(result))

    def test_exact_known_credentials_and_credential_ids_are_not_in_metadata(self):
        fake = "opaque-fixture-value"
        data = fixture()
        data["post"]["post_id"] = fake
        result = JevPacker(forbidden_values=(fake,)).pack_post(data)
        self.assertIsNone(result.targets[0].post_id)
        self.assertNotIn(fake, json.dumps(result.to_dict()))

    def test_invalid_unicode_is_deferred_and_metadata_id_is_safe(self):
        data = fixture()
        data["post"]["post_id"] = "bad-\ud800"
        result = JevPacker().pack_post(data)
        self.assertEqual(result.issues[0].reason, "invalid_unicode")
        self.assertIsNone(result.targets[0].post_id)
        json.dumps(result.to_dict(), ensure_ascii=False).encode("utf-8")

    def test_comparison_keeps_both_posts_and_question_target_mapping(self):
        cases = json.loads((FIXTURES / "jev_rubrics" / "review_cases.json").read_text())
        for case in cases["comparison_cases"]:
            with self.subTest(case=case["id"]):
                result = JevPacker().pack_comparison(**case["input"])
                self.assertEqual(result.status, "ready")
                body = result.to_provider_body()
                for role in ("current", "historical"):
                    self.assertEqual(body["state"]["content"][role]["post"]["text"], case["input"][role]["post"]["text"])
                self.assertIn("content.current.post.text", body["questions"]["claim_relation"]["instructions"]["question"])
                self.assertIn("content.historical.post.text", body["questions"]["claim_relation"]["instructions"]["question"])
                binding = result.to_dict()["answer_bindings"]["claim_relation"]
                self.assertEqual([t["role"] for t in binding["targets"]], ["current", "historical"])

    def test_comparison_never_truncates_either_target_and_rejects_same_post(self):
        current, historical = fixture(), fixture()
        historical["post"]["post_id"] = "other-post"
        historical["post"]["text"] = "Long target. " * 4000
        result = JevPacker().pack_comparison(current, historical)
        self.assertEqual(result.status, "deferred")
        self.assertEqual(len(result.targets), 2)
        result = JevPacker().pack_comparison(current, current)
        self.assertEqual(result.issues[0].reason, "same_post_pair")

    def test_batch_failure_isolation_order_and_stable_request_ids(self):
        valid = fixture()
        invalid = {**fixture(), "metrics": {"likes": -1}}
        long = fixture()
        long["post"]["text"] = "Long. " * 10000
        results = JevPacker().pack_batch([valid, invalid, long, valid])
        self.assertEqual([r.status for r in results], ["ready", "deferred", "deferred", "ready"])
        self.assertEqual([r.batch_index for r in results], list(range(4)))
        self.assertEqual(results[0].request_id, results[3].request_id)
        self.assertEqual(results[0].targets[0].post_id, valid["post"]["post_id"])
        changed = deepcopy(valid)
        changed["post"]["text"] += " Changed."
        self.assertNotEqual(results[0].request_id, JevPacker().pack_post(changed).request_id)

    def test_default_audit_serialization_excludes_body_and_omitted_text(self):
        result = JevPacker().pack_post(fixture("with_context"))
        encoded = json.dumps(to_jsonable(result))
        self.assertNotIn("What evidence supports that claim?", encoded)
        self.assertNotIn("body", result.to_dict())
        self.assertIn("body", result.to_dict(include_body=True))

    def test_invalid_config_registry_and_draft_questions_fail_safely(self):
        for limits in ({"max_state_bytes": 0}, {"max_request_bytes": True},
                       {"max_conversation_items": -1}, {"context_priority": ("parent",)}):
            with self.subTest(limits=limits), self.assertRaises(PackingValidationError):
                PackingLimits(**limits)
        ref = reference()
        for kwargs in ({"verified_references": (ref, ref)}, {"forbidden_values": ["fake"]},
                       {"model": ""}, {"model": "bad\ud800"}, {"limits": {}},
                       {"verified_references": (replace(ref, text="apikey_" + "x" * 40),)}):
            with self.subTest(fields=list(kwargs)), self.assertRaises(PackingValidationError):
                JevPacker(**kwargs)
        with self.assertRaises(PackingValidationError):
            JevPacker().pack_post(fixture(), question_set=replace(load_rubric("post"), status="draft"))
        with self.assertRaises(PackingValidationError):
            JevPacker().pack_batch("not-a-batch")


if __name__ == "__main__":
    unittest.main()
