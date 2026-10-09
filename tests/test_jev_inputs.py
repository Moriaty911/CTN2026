from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import unittest

from ctn.jev import ContentSource, InputPost, InputValidationError, JevInput


FIXTURES = Path(__file__).parent / "fixtures" / "jev_input"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class JevInputTests(unittest.TestCase):
    def test_minimal_input_preserves_unknown_context(self):
        record = JevInput.from_dict(fixture("minimal"))
        self.assertIsNone(record.metrics)
        self.assertIsNone(record.post.created_at)
        self.assertIsNone(record.post.text_complete)
        issues = {issue.path: issue.reason for issue in record.context_issues}
        self.assertEqual(issues["metrics"], "unknown")
        self.assertEqual(issues["interaction_history"], "unknown")
        self.assertEqual(issues["post.text_complete"], "unknown")

    def test_full_context_retains_relationships_and_limitations(self):
        record = JevInput.from_dict(fixture("with_context"))
        self.assertEqual(record.conversation.items[0].post_id, record.post.parent_id)
        self.assertEqual(record.interaction_history.events[0].target_post_id, "sample-parent")
        issues = {issue.path: issue.reason for issue in record.context_issues}
        self.assertEqual(issues["conversation"], "partial")
        self.assertEqual(issues["conversation.items[1].text_complete"], "truncated")
        self.assertEqual(issues["trusted_background"], "unverified")
        self.assertNotIn("conversation.parent", issues)

    def test_reply_without_parent_is_accepted_with_limitation(self):
        data = fixture("minimal")
        data["post"].update(kind="reply", parent_id="missing-parent")
        record = JevInput.from_dict(data)
        self.assertIn("conversation.parent", {i.path for i in record.context_issues})

    def test_zero_and_unknown_metrics_remain_distinct(self):
        data = fixture("minimal")
        data["metrics"] = {"likes": 0, "replies": None}
        record = JevInput.from_dict(data)
        self.assertEqual(record.metrics.likes, 0)
        self.assertIsNone(record.metrics.replies)
        self.assertIsNone(record.metrics.reposts)
        self.assertNotIn("metrics.likes", {i.path for i in record.context_issues})
        self.assertIn("metrics.replies", {i.path for i in record.context_issues})

    def test_explicit_platform_level_is_preserved_without_reclassification(self):
        data = fixture("minimal")
        data["author"] = {"platform_level": "demo-official-level-A", "followers_count": 0}
        record = JevInput.from_dict(data)
        self.assertEqual(record.author.platform_level, "demo-official-level-A")
        self.assertNotIn("author.platform_level", {i.path for i in record.context_issues})
        self.assertEqual(JevInput.from_dict(record.to_dict()), record)

    def test_missing_platform_level_is_not_inferred_from_influence_or_profile(self):
        data = fixture("minimal")
        data["author"] = {"followers_count": 100000, "profile_text": "I am a high-level account."}
        data["metrics"] = {"likes": 50000, "replies": 20000}
        absent = JevInput.from_dict(data)
        data["author"]["platform_level"] = None
        self.assertEqual(JevInput.from_dict(data), absent)
        self.assertIsNone(absent.author.platform_level)
        self.assertIn("author.platform_level", {i.path for i in absent.context_issues})

    def test_invalid_supplied_platform_level_is_rejected_instead_of_unknown(self):
        for value in ("", " \n", 2, True, [], {}):
            with self.subTest(value=value):
                data = fixture("minimal")
                data["author"] = {"platform_level": value}
                with self.assertRaises(InputValidationError) as caught:
                    JevInput.from_dict(data)
                self.assertEqual(caught.exception.path, "author.platform_level")

    def test_explicit_null_is_equivalent_to_absent_optional(self):
        data = fixture("minimal")
        absent = JevInput.from_dict(data)
        data.update(author=None, metrics=None, conversation=None, task_context=None)
        data["post"]["created_at"] = None
        self.assertEqual(JevInput.from_dict(data), absent)

    def test_roundtrip_and_json_serialization(self):
        record = JevInput.from_dict(fixture("with_context"))
        output = json.loads(json.dumps(record.to_dict()))
        self.assertIsInstance(output["conversation"]["items"], list)
        self.assertEqual(JevInput.from_dict(output), record)

    def test_input_and_output_mutation_do_not_change_record(self):
        data = fixture("with_context")
        record = JevInput.from_dict(data)
        data["conversation"]["items"][0]["text"] = "changed upstream"
        output = record.to_dict()
        output["conversation"]["items"][0]["text"] = "changed output"
        self.assertEqual(record.conversation.items[0].text, "I have not shared my sources yet.")
        with self.assertRaises(FrozenInstanceError):
            record.post.text = "changed"

    def test_missing_required_fields_return_precise_paths(self):
        cases = [(None, "schema_version"), (None, "post"), (None, "source"),
                 ("post", "post_id"), ("post", "text"),
                 ("source", "kind"), ("source", "observed_at")]
        for group, field in cases:
            with self.subTest(group=group, field=field):
                data = fixture("minimal")
                del (data if group is None else data[group])[field]
                with self.assertRaises(InputValidationError) as caught:
                    JevInput.from_dict(data)
                self.assertEqual(caught.exception.path, f"{group}.{field}" if group else field)
                self.assertEqual(caught.exception.code, "required")

    def test_blank_identifiers_and_text_rejected_without_trimming_original(self):
        for field in ("post_id", "text", "author_id", "parent_id"):
            with self.subTest(field=field):
                data = fixture("minimal")
                data["post"][field] = " \n "
                with self.assertRaises(InputValidationError) as caught:
                    JevInput.from_dict(data)
                self.assertEqual(caught.exception.path, f"post.{field}")
        data = fixture("minimal")
        data["post"]["text"] = "  Original wording\n"
        self.assertEqual(JevInput.from_dict(data).post.text, data["post"]["text"])

    def test_strict_types_do_not_coerce_booleans_numbers_or_arrays(self):
        cases = [("post", "post_id", 123), ("post", "text", False),
                 ("post", "text_complete", 1), ("post", "hashtags", "#tag"),
                 ("post", "hashtags", [1]), ("metrics", "likes", True),
                 ("metrics", "likes", 1.0), ("metrics", "likes", "1"),
                 ("metrics", "likes", -1)]
        for group, field, value in cases:
            with self.subTest(group=group, field=field, value=value):
                data = fixture("minimal")
                data.setdefault(group, {})[field] = value
                with self.assertRaises(InputValidationError):
                    JevInput.from_dict(data)

    def test_invalid_enums_and_unknown_fields_rejected(self):
        cases = [(None, "schema_version", "jev-input.v2"),
                 ("source", "kind", "other"), ("post", "kind", "message"),
                 (None, "raw", {"private": "not for model"}),
                 ("post", "api_key", "do-not-echo-this-value")]
        for group, field, value in cases:
            with self.subTest(group=group, field=field):
                data = fixture("minimal")
                (data if group is None else data[group])[field] = value
                with self.assertRaises(InputValidationError) as caught:
                    JevInput.from_dict(data)
                self.assertNotIn("do-not-echo-this-value", str(caught.exception))

    def test_times_require_valid_datetime_and_timezone(self):
        for value in ("2026-10-09", "2026-10-09T10:00:00", "not-a-time",
                      "2026-02-30T10:00:00Z", 0):
            with self.subTest(value=value):
                data = fixture("minimal")
                data["source"]["observed_at"] = value
                with self.assertRaises(InputValidationError) as caught:
                    JevInput.from_dict(data)
                self.assertEqual(caught.exception.path, "source.observed_at")
        for value in ("2026-10-09T00:00:00Z", "2026-10-09T10:00:00+11:00"):
            data = fixture("minimal")
            data["source"]["observed_at"] = value
            self.assertEqual(JevInput.from_dict(data).source.observed_at, value)

    def test_optional_context_with_bad_data_is_not_silently_dropped(self):
        data = fixture("with_context")
        data["conversation"]["items"][1]["source"]["observed_at"] = "bad"
        with self.assertRaises(InputValidationError) as caught:
            JevInput.from_dict(data)
        self.assertEqual(caught.exception.path, "conversation.items[1].source.observed_at")

    def test_inconsistent_parent_or_history_status_rejected(self):
        base = fixture("with_context")
        variants = []
        data = deepcopy(base)
        data["conversation"]["items"][0]["post_id"] = "other-parent"
        variants.append(data)
        data = deepcopy(base)
        data["conversation"]["items"][0]["post_id"] = data["post"]["post_id"]
        variants.append(data)
        data = deepcopy(base)
        data["conversation"]["items"].append(deepcopy(data["conversation"]["items"][0]))
        variants.append(data)
        data = deepcopy(base)
        data["conversation"]["status"] = "not_provided"
        variants.append(data)
        data = deepcopy(base)
        data["interaction_history"]["status"] = "not_provided"
        variants.append(data)
        for data in variants:
            with self.subTest(data=data):
                with self.assertRaises(InputValidationError) as caught:
                    JevInput.from_dict(data)
                self.assertEqual(caught.exception.code, "relationship")

    def test_unavailable_and_empty_context_have_distinct_meanings(self):
        data = fixture("minimal")
        data["conversation"] = {"status": "not_provided", "items": []}
        unavailable = JevInput.from_dict(data)
        self.assertIn("conversation", {i.path for i in unavailable.context_issues})
        data["conversation"]["status"] = "not_applicable"
        inapplicable = JevInput.from_dict(data)
        self.assertNotIn("conversation", {i.path for i in inapplicable.context_issues})
        self.assertEqual(inapplicable.conversation.items, ())

    def test_optional_groups_require_their_declared_structure(self):
        for name in ("conversation", "interaction_history", "trusted_background"):
            with self.subTest(name=name):
                data = fixture("minimal")
                data[name] = {}
                with self.assertRaises(InputValidationError) as caught:
                    JevInput.from_dict(data)
                self.assertEqual(caught.exception.code, "required")
        for payload in (None, [], "not-an-object"):
            with self.assertRaises(InputValidationError):
                JevInput.from_dict(payload)

    def test_direct_construction_cannot_bypass_validation(self):
        with self.assertRaises(InputValidationError):
            InputPost(post_id="p", text=" ")
        with self.assertRaises(InputValidationError):
            ContentSource(kind="platform", observed_at="missing-timezone")
        with self.assertRaises(InputValidationError):
            JevInput(schema_version="jev-input.v1", post={}, source={})

    def test_invalid_fixture_rejected(self):
        with self.assertRaises(InputValidationError):
            JevInput.from_dict(fixture("invalid"))


if __name__ == "__main__":
    unittest.main()
