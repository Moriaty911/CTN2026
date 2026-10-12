"""Offline evaluation routes, duplicate handling and stage accounting."""

from concurrent.futures import ThreadPoolExecutor, Future
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from ctn.config import JevConfig
from ctn.jev import (
    EvaluationLimits, EvaluatorValidationError, HttpResponse, JevClient, JevEvaluator,
    JevInput, JevPacker, PackingLimits, QuestionSet, RoutingPolicy, RoutingValidationError, load_rubric,
)
from ctn.models import to_jsonable


FIXTURES = Path(__file__).parent / "fixtures"


def content(name="minimal", post_id=None):
    data = json.loads((FIXTURES / "jev_input" / (name + ".json")).read_text())
    if post_id is not None:
        data["post"]["post_id"] = post_id
    return data


def answer_body(body):
    answers = {}
    for key, question in body["questions"].items():
        if question["type"] == "noul":
            answers[key] = {"type": "noul", "noul": 0.0}
        elif question["type"] == "choice":
            criteria = question["criteria"]
            option = "yes" if key == "policy_stance" else "sufficient" if "sufficient" in criteria else next(iter(criteria))
            answers[key] = {"type": "choice", "choice": option,
                "probabilities": {k: float(k == option) for k in criteria}, "confidence": 1.0}
        else:
            criteria = question["criteria"]
            answers[key] = {"type": "score", "score": 1.6, "confidence": 0.2,
                "probabilities": {str(i): 0.4 if i == 1 else 0.6 if i == 2 else 0.0 for i in range(len(criteria))},
                "legend": {str(i): v for i, v in enumerate(criteria)}}
    return {"model": "synthetic-test-model", "answers": answers,
            "usage": {"input_tokens": 12, "output_tokens": 6}}


def choose(data, question_id, option, probability=1.0):
    answer = data["answers"][question_id]
    alternatives = [k for k in answer["probabilities"] if k != option]
    answer["choice"] = option
    answer["probabilities"] = {key: probability if key == option else (1 - probability) / len(alternatives)
                               for key in answer["probabilities"]}


class FakeTransport:
    def __init__(self, handler=None):
        self.handler = handler
        self.calls = []
        self.lock = threading.Lock()

    def send(self, **kwargs):
        body = json.loads(kwargs["body"])
        with self.lock:
            self.calls.append(body)
        data = answer_body(body)
        returned = self.handler(body, data) if self.handler else None
        return returned if isinstance(returned, HttpResponse) else HttpResponse(200, body=json.dumps(data).encode())


class Clock:
    now = 0.0
    def monotonic(self):
        return self.now


def evaluator(handler=None, **kwargs):
    transport = FakeTransport(handler)
    config = kwargs.pop("config", JevConfig(api_key="synthetic-test-key", max_retries=0))
    return JevEvaluator(JevClient(config, transport=transport), **kwargs), transport


class RouteAndFlowTests(unittest.TestCase):
    def test_relevant_post_runs_two_bound_stages_and_preserves_values(self):
        flow, transport = evaluator()
        data = content()
        data["task_context"] = {"assigned_side": "yes"}
        result = flow.evaluate(data)
        self.assertEqual(result.status, "evaluated")
        self.assertEqual([len(c["questions"]) for c in transport.calls], [4, 15])
        self.assertEqual(result.triage.parsed.output.stage, "triage")
        self.assertEqual(result.deep.parsed.output.stage, "deep")
        self.assertEqual(result.deep.parsed.output.answers["emotion_intensity"].value, 1.6)
        self.assertNotEqual(result.triage.parsed.output.evaluation_id, result.deep.parsed.output.evaluation_id)
        self.assertEqual(result.new_attempt_count, 2)
        self.assertEqual(result.relative_stance, "aligned")
        self.assertNotIn("action", result.to_dict())

    def test_all_stances_can_enter_deep_and_missing_assignment_is_unknown(self):
        for stance in ("yes", "no", "mixed", "neutral", "unclear"):
            with self.subTest(stance=stance):
                flow, transport = evaluator(lambda body, data: choose(data, "policy_stance", stance))
                result = flow.evaluate(content())
                self.assertEqual(result.status, "evaluated")
                self.assertEqual(len(transport.calls), 2)
                self.assertEqual(result.relative_stance, "unknown")

    def test_team_mapping_uses_supplied_assignment_without_account_level_gate(self):
        flow, transport = evaluator(lambda body, data: choose(data, "policy_stance", "no"))
        data = content("with_context")
        data["task_context"] = {"assigned_side": "yes"}
        result = flow.evaluate(data)
        self.assertEqual(result.relative_stance, "opposing")
        self.assertEqual(result.status, "evaluated")

    def test_exact_unrelated_threshold_observes_without_deep(self):
        flow, transport = evaluator(lambda body, data: choose(data, "relevance", "unrelated", 0.8))
        result = flow.evaluate(content())
        self.assertEqual(result.status, "observe")
        self.assertEqual(result.decision.reason, "clearly_unrelated")
        self.assertIsNone(result.deep)
        self.assertEqual(len(transport.calls), 1)

    def test_weak_unrelated_enters_deep_then_requires_review(self):
        flow, transport = evaluator(lambda body, data: choose(data, "relevance", "unrelated", 0.79))
        result = flow.evaluate(content())
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.decision.reason, "relevance_uncertain_after_deep")

    def test_unclear_relevance_runs_deep_once_then_returns_context_need(self):
        flow, transport = evaluator(lambda body, data: choose(data, "relevance", "unclear"))
        result = flow.evaluate(content())
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(result.status, "needs_context")
        self.assertEqual(result.needs[0].answer_ids, ("relevance",))
        self.assertEqual(result.deep.parsed.output.answers["relevance"].value, "unclear")

    def test_unclear_triage_can_be_resolved_by_deep(self):
        def handle(body, data):
            if len(body["questions"]) == 4:
                choose(data, "relevance", "unclear")
        flow, transport = evaluator(handle)
        result = flow.evaluate(content())
        self.assertEqual(result.status, "evaluated")
        self.assertEqual(result.triage.parsed.output.answers["relevance"].value, "unclear")
        self.assertEqual(result.deep.parsed.output.answers["relevance"].value, "relevant")

    def test_risk_boundary_and_missing_or_invalid_risk_pause_even_unrelated(self):
        for value in (0.5, 0.9, "missing", "invalid"):
            def handle(body, data):
                choose(data, "relevance", "unrelated")
                if value == "missing":
                    del data["answers"]["prompt_injection"]
                else:
                    data["answers"]["prompt_injection"]["noul"] = "bad" if value == "invalid" else value
            with self.subTest(value=value):
                flow, transport = evaluator(handle)
                result = flow.evaluate(content())
                self.assertEqual(result.status, "needs_review")
                self.assertEqual(len(transport.calls), 1)
                self.assertIsNone(result.deep)
                self.assertIn("prompt_injection", result.needs[0].answer_ids)

    def test_risk_just_below_threshold_permits_evaluation(self):
        def handle(body, data):
            data["answers"]["prompt_injection"]["noul"] = 0.499
        flow, transport = evaluator(handle)
        self.assertEqual(flow.evaluate(content()).status, "evaluated")
        self.assertEqual(len(transport.calls), 2)

    def test_relevance_missing_or_invalid_is_failure_not_normal_observation(self):
        for invalid in (False, True):
            def handle(body, data):
                if invalid:
                    data["answers"]["relevance"]["choice"] = "not-requested"
                else:
                    del data["answers"]["relevance"]
            with self.subTest(invalid=invalid):
                flow, transport = evaluator(handle)
                result = flow.evaluate(content())
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.decision.reason, "relevance_unavailable")
                self.assertEqual(len(transport.calls), 1)

    def test_partial_optional_triage_does_not_prevent_deep(self):
        def handle(body, data):
            if len(body["questions"]) == 4:
                del data["answers"]["topic"]
                del data["answers"]["policy_stance"]
        flow, transport = evaluator(handle)
        result = flow.evaluate(content())
        self.assertEqual(result.status, "evaluated")
        self.assertEqual(result.triage.parsed.status, "partial")
        self.assertEqual(result.deep.parsed.status, "success")

    def test_upstream_mutation_during_triage_does_not_change_deep_snapshot(self):
        data = content()
        original = data["post"]["text"]
        def handler(body, response):
            if len(body["questions"]) == 4:
                data["post"]["text"] = "New content arriving during the request."
        flow, transport = evaluator(handler)
        self.assertEqual(flow.evaluate(data).status, "evaluated")
        self.assertEqual([call["state"]["content"]["post"]["text"] for call in transport.calls], [original, original])

    def test_question_set_change_during_run_applies_only_to_the_next_run(self):
        data = load_rubric("post").to_dict()
        data["version"] = "synthetic-post-v2"
        replacement = QuestionSet.from_dict(data)
        def handler(body, response):
            if len(body["questions"]) == 4:
                flow.post_questions = replacement
        flow, transport = evaluator(handler)
        first = flow.evaluate(content())
        self.assertEqual(first.triage.parsed.output.question_set_version, "ctn-post.v1")
        self.assertEqual(first.deep.parsed.output.question_set_version, "ctn-post.v1")
        second = flow.evaluate(content())
        self.assertEqual(second.deep.parsed.output.question_set_version, "synthetic-post-v2")

    def test_empty_triage_and_http_error_do_not_become_filtered_posts(self):
        def empty(body, data):
            data["answers"] = {}
        for handler in (empty, lambda body, data: HttpResponse(401)):
            flow, transport = evaluator(handler)
            result = flow.evaluate(content())
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.triage.parsed.status, "failed")
            self.assertEqual(len(transport.calls), 1)

    def test_deep_rechecks_risk_and_keeps_both_stage_results(self):
        def handle(body, data):
            if len(body["questions"]) > 4:
                data["answers"]["prompt_injection"]["noul"] = 0.7
        flow, transport = evaluator(handle)
        result = flow.evaluate(content("with_context"))
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.triage.parsed.output.answers["prompt_injection"].value, 0)
        self.assertEqual(result.deep.parsed.output.answers["prompt_injection"].value, 0.7)

    def test_deep_missing_risk_requires_review(self):
        def handle(body, data):
            if len(body["questions"]) > 4:
                del data["answers"]["prompt_injection"]
        flow, transport = evaluator(handle)
        self.assertEqual(flow.evaluate(content()).status, "needs_review")

    def test_deep_gate_context_need_keeps_other_valid_answers(self):
        def handle(body, data):
            if len(body["questions"]) > 4:
                choose(data, "emotion_evidence", "unclear")
        flow, transport = evaluator(handle)
        result = flow.evaluate(content())
        self.assertEqual(result.status, "needs_context")
        self.assertEqual(result.needs[0].answer_ids, ("emotion_intensity",))
        self.assertIsNone(result.deep.parsed.output.answers["emotion_intensity"].value)
        self.assertIn("policy_stance", result.deep.parsed.output.usable_answers)

    def test_deep_unclear_and_gate_need_are_both_reported(self):
        def handle(body, data):
            if len(body["questions"]) > 4:
                choose(data, "relevance", "unclear")
                choose(data, "emotion_evidence", "unclear")
        flow, transport = evaluator(handle)
        self.assertEqual(flow.evaluate(content()).needs[0].answer_ids, ("relevance", "emotion_intensity"))

    def test_optional_deep_answer_error_is_partial_and_not_fatal(self):
        def handle(body, data):
            if len(body["questions"]) > 4:
                data["answers"]["emotion_intensity"]["score"] = 5
        flow, transport = evaluator(handle)
        result = flow.evaluate(content())
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.needs[0].answer_ids, ("emotion_intensity",))
        self.assertIn("policy_stance", result.deep.parsed.output.usable_answers)

    def test_deep_unrelated_result_does_not_overwrite_relevant_triage(self):
        def handle(body, data):
            if len(body["questions"]) > 4:
                choose(data, "relevance", "unrelated")
        flow, transport = evaluator(handle)
        result = flow.evaluate(content())
        self.assertEqual(result.status, "observe")
        self.assertEqual(result.triage.parsed.output.answers["relevance"].value, "relevant")
        self.assertEqual(result.deep.parsed.output.answers["relevance"].value, "unrelated")

    def test_deep_service_failure_preserves_successful_triage(self):
        def handle(body, data):
            return HttpResponse(503) if len(body["questions"]) > 4 else None
        flow, transport = evaluator(handle)
        result = flow.evaluate(content())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.triage.parsed.status, "success")
        self.assertEqual(result.deep.parsed.error.code, "service_error")

    def test_bad_input_and_body_budget_are_deferred_without_network(self):
        for data, packer in (({}, None), (content(), JevPacker(limits=PackingLimits(max_state_bytes=1)))):
            flow, transport = evaluator(packer=packer)
            result = flow.evaluate(data)
            self.assertEqual(result.status, "deferred")
            self.assertEqual(len(transport.calls), 0)
            self.assertEqual(result.new_attempt_count, 0)

    def test_deep_larger_question_budget_can_defer_after_valid_triage(self):
        data = content()
        triage_bytes = JevPacker().pack_post(data).request_bytes
        flow, transport = evaluator(packer=JevPacker(limits=PackingLimits(max_request_bytes=triage_bytes)))
        result = flow.evaluate(data)
        self.assertEqual(result.status, "deferred")
        self.assertEqual(result.decision.reason, "deep_packing_deferred")
        self.assertEqual(result.triage.parsed.status, "success")
        self.assertEqual(len(transport.calls), 1)

    def test_missing_credentials_are_reported_without_service_attempt(self):
        flow, transport = evaluator(config=JevConfig())
        result = flow.evaluate(content())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.triage.parsed.error.code, "missing_credentials")
        self.assertEqual(result.new_attempt_count, 0)
        self.assertEqual(transport.calls, [])

    def test_explicit_pair_comparison_retains_both_ids_without_triage_or_search(self):
        flow, transport = evaluator()
        result = flow.compare(content(post_id="current"), content(post_id="historical"))
        self.assertEqual(result.parsed.status, "success")
        self.assertEqual([t.post_id for t in result.parsed.targets], ["current", "historical"])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(set(transport.calls[0]["state"]["content"]), {"current", "historical"})


class CacheAndBatchTests(unittest.TestCase):
    def test_cross_run_cache_disabled_by_default(self):
        flow, transport = evaluator()
        first, second = flow.evaluate(content()), flow.evaluate(content())
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(second.new_attempt_count, 2)
        self.assertNotEqual(first.run_id, second.run_id)

    def test_enabled_cache_exposes_age_origin_and_zero_new_attempts(self):
        clock = Clock()
        flow, transport = evaluator(limits=EvaluationLimits(cache_ttl_s=60), monotonic=clock.monotonic)
        first = flow.evaluate(content())
        clock.now = 7
        second = flow.evaluate(content())
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(second.new_attempt_count, 0)
        self.assertEqual(second.triage.source, "cache")
        self.assertEqual(second.deep.cache_age_s, 7)
        self.assertEqual(first.deep.parsed.output.evaluation_id, second.deep.parsed.output.evaluation_id)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(second.deep.to_dict()["original_call"]["attempt_count"], 1)

    def test_ttl_boundary_and_explicit_clear_re_evaluate(self):
        clock = Clock()
        flow, transport = evaluator(limits=EvaluationLimits(cache_ttl_s=10), monotonic=clock.monotonic)
        flow.evaluate(content())
        clock.now = 10
        flow.evaluate(content())
        self.assertEqual(len(transport.calls), 4)
        flow.clear_cache()
        flow.evaluate(content())
        self.assertEqual(len(transport.calls), 6)

    def test_input_and_context_snapshot_changes_invalidate_cache(self):
        flow, transport = evaluator(limits=EvaluationLimits(cache_ttl_s=60))
        data = content()
        first = flow.evaluate(data)
        data["post"]["text"] += " A different claim."
        second = flow.evaluate(data)
        data["source"]["observed_at"] = "2026-10-10T11:00:00+11:00"
        third = flow.evaluate(data)
        self.assertEqual(len(transport.calls), 6)
        self.assertNotEqual(first.triage.parsed.output.request_id, second.triage.parsed.output.request_id)
        self.assertNotEqual(second.triage.parsed.output.request_id, third.triage.parsed.output.request_id)

    def test_omitted_material_change_does_not_share_wrong_input_provenance(self):
        packer = JevPacker(limits=PackingLimits(max_conversation_items=0))
        first, second = content("with_context"), content("with_context")
        second["conversation"]["items"][1]["text"] += " Changed omitted content."
        packed_first, packed_second = packer.pack_post(first), packer.pack_post(second)
        self.assertEqual(packed_first.request_id, packed_second.request_id)
        self.assertNotEqual(packed_first.input_fingerprint, packed_second.input_fingerprint)
        flow, transport = evaluator(packer=packer, limits=EvaluationLimits(cache_ttl_s=60))
        flow.evaluate(first)
        result = flow.evaluate(second)
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(result.triage.parsed.to_dict()["request"]["input_fingerprint"], packed_second.input_fingerprint)

    def test_failed_and_partial_assessments_are_not_cross_run_cached(self):
        def partial(body, data):
            choose(data, "relevance", "unrelated")
            del data["answers"]["topic"]
        for handler in (partial, lambda body, data: HttpResponse(401)):
            flow, transport = evaluator(handler, limits=EvaluationLimits(cache_ttl_s=60))
            flow.evaluate(content())
            flow.evaluate(content())
            self.assertEqual(len(transport.calls), 2)

    def test_changed_question_fingerprint_invalidates_cached_assessments(self):
        flow, transport = evaluator(limits=EvaluationLimits(cache_ttl_s=60))
        first = flow.evaluate(content())
        data = flow.post_questions.to_dict()
        data["shared_instructions"] += " Synthetic configuration revision."
        flow.post_questions = QuestionSet.from_dict(data)
        second = flow.evaluate(content())
        self.assertEqual(len(transport.calls), 4)
        self.assertNotEqual(first.deep.parsed.to_dict()["request"]["question_set_fingerprint"],
                            second.deep.parsed.to_dict()["request"]["question_set_fingerprint"])

    def test_attempt_accounting_includes_client_retries_only_once(self):
        transport = FakeTransport(lambda body, data: HttpResponse(503) if len(transport.calls) == 1 else None)
        clock = Clock()
        def sleep(seconds):
            clock.now += seconds
        client = JevClient(JevConfig(api_key="synthetic-test-key", max_retries=1), transport=transport,
                           monotonic=clock.monotonic, sleep=sleep, random_value=lambda: 1)
        result = JevEvaluator(client).evaluate(content())
        self.assertEqual(result.status, "evaluated")
        self.assertEqual(result.new_attempt_count, 3)
        self.assertEqual(result.triage.new_attempt_count, 2)
        self.assertEqual(len(transport.calls), 3)

    def test_unknown_relevance_can_be_configured_to_review_instead_of_deep(self):
        flow, transport = evaluator(lambda body, data: choose(data, "relevance", "unclear"),
                                    policy=RoutingPolicy(unclear_to_deep=False))
        result = flow.evaluate(content())
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(len(transport.calls), 1)

    def test_lru_eviction_is_bounded(self):
        flow, transport = evaluator(lambda body, data: choose(data, "relevance", "unrelated"),
                                    limits=EvaluationLimits(cache_ttl_s=60, max_cache_entries=2))
        flow.evaluate(content(post_id="a"))
        flow.evaluate(content(post_id="b"))
        self.assertEqual(flow.evaluate(content(post_id="a")).new_attempt_count, 0)
        flow.evaluate(content(post_id="c"))
        self.assertEqual(flow.evaluate(content(post_id="b")).new_attempt_count, 1)
        self.assertEqual(len(transport.calls), 4)

    def test_routing_policy_is_reapplied_to_cached_assessment(self):
        def handler(body, data):
            data["answers"]["prompt_injection"]["noul"] = 0.6
        flow, transport = evaluator(handler, limits=EvaluationLimits(cache_ttl_s=60))
        first = flow.evaluate(content())
        self.assertEqual(first.status, "needs_review")
        flow.policy = RoutingPolicy(injection_review_threshold=0.7)
        second = flow.evaluate(content())
        self.assertEqual(second.status, "evaluated")
        self.assertEqual(second.triage.source, "cache")
        self.assertEqual(second.new_attempt_count, 1)
        self.assertNotEqual(first.policy.fingerprint, second.policy.fingerprint)

    def test_duplicate_batch_items_share_each_stage_even_across_chunks(self):
        flow, transport = evaluator(config=JevConfig(api_key="synthetic-test-key", max_retries=0,
                                                     max_concurrency=2, triage_batch_size=1))
        results = flow.evaluate_batch([content(), content(), content()])
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual([r.batch_index for r in results], [0, 1, 2])
        self.assertEqual([r.new_attempt_count for r in results], [2, 0, 0])
        self.assertEqual(results[2].triage.source, "shared")
        self.assertEqual(results[2].triage.parsed.to_dict()["request"]["batch_index"], 2)
        self.assertEqual(len({r.run_id for r in results}), 3)
        # The next independent batch is fresh when TTL is zero.
        flow.evaluate_batch([content()])
        self.assertEqual(len(transport.calls), 4)

    def test_failed_duplicate_batch_does_not_retry_as_separate_work_items(self):
        flow, transport = evaluator(lambda body, data: HttpResponse(401),
                                    config=JevConfig(api_key="synthetic-test-key", max_retries=0, triage_batch_size=1))
        results = flow.evaluate_batch([content(), content()])
        self.assertEqual([r.status for r in results], ["failed", "failed"])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sum(r.new_attempt_count for r in results), 1)

    def test_out_of_order_completion_and_bad_input_preserve_batch_order(self):
        release = threading.Event()
        def handler(body, data):
            target = body["state"]["content"]["post"]["post_id"]
            if target == "slow" and len(body["questions"]) == 4:
                self.assertTrue(release.wait(2))
            if target == "fast":
                release.set()
        flow, transport = evaluator(handler, config=JevConfig(api_key="synthetic-test-key", max_retries=0,
                                                               max_concurrency=2, triage_batch_size=3))
        results = flow.evaluate_batch([content(post_id="slow"), {}, content(post_id="fast")])
        self.assertEqual([r.batch_index for r in results], [0, 1, 2])
        self.assertEqual([r.status for r in results], ["evaluated", "deferred", "evaluated"])
        self.assertEqual(results[0].deep.parsed.output.post_id, "slow")
        self.assertEqual(results[2].deep.parsed.output.post_id, "fast")

    def test_overlapping_calls_share_inflight_request_without_global_cache(self):
        started, release, waiting = threading.Event(), threading.Event(), threading.Event()
        def handler(body, data):
            started.set()
            self.assertTrue(release.wait(2))
            choose(data, "relevance", "unrelated")
        flow, transport = evaluator(handler)
        original_result = Future.result
        def tracked_result(future, *args, **kwargs):
            waiting.set()
            return original_result(future, *args, **kwargs)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(flow.evaluate, content())
            self.assertTrue(started.wait(2))
            with patch("ctn.jev.evaluator.Future.result", new=tracked_result):
                second = pool.submit(flow.evaluate, content())
                try:
                    self.assertTrue(waiting.wait(2))
                finally:
                    release.set()
                results = [first.result(), second.result()]
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sorted(r.triage.source for r in results), ["network", "shared"])
        self.assertEqual(sum(r.new_attempt_count for r in results), 1)

    def test_duplicate_wait_is_bounded_and_does_not_issue_another_request(self):
        started, release = threading.Event(), threading.Event()
        def handler(body, data):
            started.set()
            self.assertTrue(release.wait(2))
            choose(data, "relevance", "unrelated")
        flow, transport = evaluator(handler, limits=EvaluationLimits(duplicate_wait_timeout_s=0.01))
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(flow.evaluate, content())
            self.assertTrue(started.wait(2))
            try:
                second = flow.evaluate(content())
                self.assertEqual(second.status, "failed")
                self.assertEqual(second.triage.parsed.error.code, "wait_budget_exceeded")
                self.assertEqual(second.new_attempt_count, 0)
            finally:
                release.set()
            self.assertEqual(first.result().status, "observe")
        self.assertEqual(len(transport.calls), 1)

    def test_result_serialization_is_content_free_and_raw_free(self):
        flow, transport = evaluator()
        data = content("with_context")
        result = flow.evaluate(data)
        wire = json.dumps(to_jsonable(result))
        self.assertNotIn(data["post"]["text"], wire)
        self.assertNotIn("synthetic-test-key", wire + repr(result))
        self.assertNotIn("raw_response", wire)

    def test_configuration_and_call_shape_are_validated(self):
        for changes in ({"cache_ttl_s": -1}, {"cache_ttl_s": float("nan")},
                        {"cache_ttl_s": 10 ** 400}, {"max_cache_entries": True}, {"duplicate_wait_timeout_s": 0}):
            with self.assertRaises(EvaluatorValidationError):
                EvaluationLimits(**changes)
        for value in (-0.1, 1.1, True, float("nan"), 10 ** 400):
            with self.assertRaises(RoutingValidationError):
                RoutingPolicy(injection_review_threshold=value)
        flow, transport = evaluator()
        for items in ({}, "items"):
            with self.assertRaises(EvaluatorValidationError):
                flow.evaluate_batch(items)
        self.assertEqual(flow.evaluate_batch([]), ())
        with self.assertRaises(EvaluatorValidationError):
            flow.evaluate(content(), batch_index=-1)
        with self.assertRaises(EvaluatorValidationError):
            evaluator(packer=JevPacker(model="other-model"))
        data = load_rubric("post").to_dict()
        del data["questions"]["prompt_injection"]
        with self.assertRaises(EvaluatorValidationError):
            evaluator(post_questions=QuestionSet.from_dict(data))
        flow.client.config = replace(flow.client.config, enabled=False)
        with self.assertRaises(EvaluatorValidationError):
            flow.evaluate(content())


if __name__ == "__main__":
    unittest.main()
