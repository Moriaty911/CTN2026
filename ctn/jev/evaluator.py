"""Module-local triage -> deep evaluation, with bounded duplicate handling.

No platform reads, historical search, LLM calls, publishing or persistent state.
One instance shares in-flight requests and an optional bounded memory cache.
The default cache TTL is zero: overlapping requests and duplicates within one
batch are shared, while independent later runs are fresh.
"""

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import threading
import time
from uuid import uuid4

from .client import JevCallResult, JevClient
from .inputs import InputValidationError, JevInput
from .packing import JevPacker, PackingResult
from .parsing import JevResponseParser, ParsingResult
from .questions import QuestionSet
from .routing import RouteDecision, RoutingPolicy
from .rubrics import load_rubric, relative_stance


FLOW_SCHEMA_VERSION = "jev-flow.v1"


class EvaluatorValidationError(ValueError):
    """Local configuration error with no supplied content or credentials."""


@dataclass(frozen=True)
class EvaluationLimits:
    cache_ttl_s: float = 0.0
    max_cache_entries: int = 256
    duplicate_wait_timeout_s: float = 60.0

    def __post_init__(self):
        for name in ("cache_ttl_s", "duplicate_wait_timeout_s"):
            value = getattr(self, name)
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0 and (
                    name != "duplicate_wait_timeout_s" or value > 0)
            except OverflowError:
                valid = False
            if not valid:
                raise EvaluatorValidationError(f"{name}: invalid duration")
        if type(self.max_cache_entries) is not int or self.max_cache_entries <= 0:
            raise EvaluatorValidationError("max_cache_entries: expected positive integer")

    def to_dict(self):
        return {"cache_ttl_s": self.cache_ttl_s, "max_cache_entries": self.max_cache_entries,
                "duplicate_wait_timeout_s": self.duplicate_wait_timeout_s}


@dataclass(frozen=True)
class StageEvaluation:
    stage: str
    source: str  # network, not_sent, shared, cache
    parsed: ParsingResult
    new_attempt_count: int
    cache_age_s: float = 0.0
    _call_metadata_json: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {"stage": self.stage, "source": self.source, "new_attempt_count": self.new_attempt_count,
                "cache_age_s": self.cache_age_s, "assessment": self.parsed.to_dict(),
                # Original call metadata is provenance, not another billed call.
                "original_call": json.loads(self._call_metadata_json) if self._call_metadata_json else None}


@dataclass(frozen=True)
class EvaluationNeed:
    kind: str  # context, review, assessment
    reason: str
    answer_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"kind": self.kind, "reason": self.reason, "answer_ids": list(self.answer_ids)}


@dataclass(frozen=True)
class EvaluationFlow:
    run_id: str
    status: str
    decision: RouteDecision
    triage: StageEvaluation
    deep: StageEvaluation | None
    relative_stance: str
    needs: tuple[EvaluationNeed, ...]
    policy: RoutingPolicy
    batch_index: int | None = None

    @property
    def new_attempt_count(self) -> int:
        return self.triage.new_attempt_count + (self.deep.new_attempt_count if self.deep else 0)

    def to_dict(self) -> dict:
        return {"schema_version": FLOW_SCHEMA_VERSION, "run_id": self.run_id, "status": self.status,
                "batch_index": self.batch_index, "decision": self.decision.to_dict(),
                "relative_stance": self.relative_stance, "needs": [need.to_dict() for need in self.needs],
                "new_attempt_count": self.new_attempt_count, "policy": self.policy.to_dict(),
                "policy_fingerprint": self.policy.fingerprint,
                "triage": self.triage.to_dict(), "deep": self.deep.to_dict() if self.deep else None}


@dataclass(frozen=True)
class _Entry:
    parsed: ParsingResult
    call_metadata_json: str
    attempts: int
    completed_at: float


class JevEvaluator:
    def __init__(self, client: JevClient, *, packer: JevPacker | None = None,
                 parser: JevResponseParser | None = None, policy: RoutingPolicy | None = None,
                 limits: EvaluationLimits | None = None, post_questions: QuestionSet | None = None,
                 comparison_questions: QuestionSet | None = None, monotonic=time.monotonic):
        if not isinstance(client, JevClient):
            raise EvaluatorValidationError("client: expected JEV client")
        self.client = client
        self._client_config = client.config
        self.packer = packer if packer is not None else JevPacker(model=client.config.model,
            forbidden_values=(client.config.api_key,) if client.config.api_key else ())
        self.parser = parser if parser is not None else JevResponseParser()
        self.policy = policy if policy is not None else RoutingPolicy()
        self.limits = limits if limits is not None else EvaluationLimits()
        self.post_questions = post_questions if post_questions is not None else load_rubric("post")
        self.comparison_questions = comparison_questions if comparison_questions is not None else load_rubric("comparison")
        for value, expected, name in ((self.packer, JevPacker, "packer"), (self.parser, JevResponseParser, "parser"),
                                     (self.policy, RoutingPolicy, "policy"), (self.limits, EvaluationLimits, "limits"),
                                     (self.post_questions, QuestionSet, "post_questions"),
                                     (self.comparison_questions, QuestionSet, "comparison_questions")):
            if not isinstance(value, expected):
                raise EvaluatorValidationError(f"{name}: invalid component")
        if self.packer.model != client.config.model:
            raise EvaluatorValidationError("model: client and packer must agree")
        if self.post_questions.status != "confirmed" or self.comparison_questions.status != "confirmed":
            raise EvaluatorValidationError("questions: expected confirmed question sets")
        for stage in ("triage", "deep"):
            questions = self.post_questions.for_stage(stage)
            relevance = questions.get("relevance")
            risk = questions.get("prompt_injection")
            stance = questions.get("policy_stance")
            if (relevance is None or relevance.type != "choice" or
                set(relevance.criteria) != {"relevant", "unrelated", "unclear"} or
                risk is None or risk.type != "noul" or (stance is not None and (
                    stance.type != "choice" or set(stance.criteria) != {"yes", "no", "mixed", "neutral", "unclear"}))):
                raise EvaluatorValidationError("post_questions: incompatible routing questions")
        self._clock = monotonic
        self._lock = threading.Lock()
        self._cache = OrderedDict()
        self._inflight = {}

    def clear_cache(self):
        """Does not cancel in-flight network calls or reset provider cooldown."""
        with self._lock:
            self._cache.clear()

    def _check_config(self):
        if self.client.config != self._client_config:
            raise EvaluatorValidationError("client configuration changed: construct a new evaluator")

    def _cache_key(self, packed: PackingResult) -> str:
        # input_fingerprint catches changed materials omitted by the byte budget;
        # their identical wire body must not erase distinct local provenance.
        return hashlib.sha256(json.dumps({"request_id": packed.request_id,
            "input": packed.input_fingerprint, "packing": packed.packing_fingerprint,
            "questions": packed.question_set_fingerprint, "kind": packed.kind, "stage": packed.stage},
            sort_keys=True).encode()).hexdigest()

    def _local_failure(self, packed, questions, code, started) -> StageEvaluation:
        call = JevCallResult(status="error", call_id=str(uuid4()), request_id=packed.request_id,
            error_code=code, retryable=True, attempts=(), elapsed_s=max(0.0, self._clock() - started), packed_request=packed)
        parsed = self.parser.parse(call, question_set=questions)
        return StageEvaluation(packed.stage, "not_sent", parsed, 0, _call_metadata_json=json.dumps(call.to_dict()))

    def _evaluate_stage(self, packed: PackingResult, questions: QuestionSet, batch_memo=None) -> StageEvaluation:
        self._check_config()
        started = self._clock()
        # Deferred inputs can have no identity/fingerprint: never coalesce them.
        key = self._cache_key(packed) if packed.status == "ready" else None
        future, leader = None, True
        if key is not None:
            with self._lock:
                now = self._clock()
                for expired in [k for k, entry in self._cache.items()
                                if now - entry.completed_at >= self.limits.cache_ttl_s]:
                    self._cache.pop(expired)
                if batch_memo is not None and key in batch_memo:
                    future, leader = batch_memo[key], False
                elif key in self._cache:
                    entry = self._cache[key]
                    self._cache.move_to_end(key)
                    if batch_memo is not None:
                        completed = Future()
                        completed.set_result(entry)
                        batch_memo[key] = completed
                    return self._reused_stage(packed, entry, "cache")
                elif key in self._inflight:
                    future, leader = self._inflight[key], False
                else:
                    future = Future()
                    self._inflight[key] = future
                if batch_memo is not None:
                    batch_memo[key] = future
        if not leader:
            try:
                entry = future.result(timeout=self.limits.duplicate_wait_timeout_s)
                return self._reused_stage(packed, entry, "shared")
            except FutureTimeout:
                return self._local_failure(packed, questions, "wait_budget_exceeded", started)
            except Exception:
                return self._local_failure(packed, questions, "transport_error", started)
        try:
            call = self.client.evaluate(packed)
            parsed = self.parser.parse(call, question_set=questions, expected_request_id=packed.request_id)
            entry = _Entry(parsed, json.dumps(call.to_dict()), len(call.attempts), self._clock())
            if key is not None:
                with self._lock:
                    # Cache only complete answers. Failed/partial gates are never
                    # sticky cross-run results. In-flight reuse is still allowed.
                    if parsed.status == "success" and self.limits.cache_ttl_s > 0:
                        self._cache[key] = entry
                        self._cache.move_to_end(key)
                        while len(self._cache) > self.limits.max_cache_entries:
                            self._cache.popitem(last=False)
                    future.set_result(entry)
            return StageEvaluation(packed.stage, "network" if entry.attempts else "not_sent", parsed,
                                   entry.attempts, _call_metadata_json=entry.call_metadata_json)
        except BaseException:
            if future is not None:
                # Never retain the original exception, which can contain input.
                future.set_exception(EvaluatorValidationError("stage evaluation interrupted"))
            raise
        finally:
            if key is not None:
                with self._lock:
                    if self._inflight.get(key) is future:
                        self._inflight.pop(key)

    def _reused_stage(self, packed, entry, source):
        # Only the batch annotation may differ; key covers all input provenance.
        parsed = replace(entry.parsed, _packed=packed)
        return StageEvaluation(packed.stage, source, parsed, 0,
            max(0.0, self._clock() - entry.completed_at), entry.call_metadata_json)

    def evaluate(self, content: JevInput | dict, *, batch_index: int | None = None) -> EvaluationFlow:
        """Run one triage/deep flow; return assessments and evaluation needs."""
        return self._evaluate(content, batch_index=batch_index)

    def _evaluate(self, content: JevInput | dict, *, batch_index: int | None = None, batch_memo=None) -> EvaluationFlow:
        """Evaluate one content snapshot. Missing parent permits basic judgment.

        Deep packing uses the original validated record, not triage's reduced
        state, and reapplies the budget for the larger question set. There is no
        recursive deep loop or hidden fetch of missing context.
        """
        self._check_config()
        if batch_index is not None and (type(batch_index) is not int or batch_index < 0):
            raise EvaluatorValidationError("batch_index: expected nonnegative integer")
        snapshot = content
        try:
            snapshot = content if type(content) is JevInput else JevInput.from_dict(content)
        except InputValidationError:
            pass  # Packer returns an isolated, content-free deferred result.
        policy = self.policy
        questions = self.post_questions
        packed = replace(self.packer.pack_post(snapshot, question_set=questions), batch_index=batch_index)
        triage = self._evaluate_stage(packed, questions, batch_memo)
        decision = policy.decide(triage.parsed)
        deep = None
        needs = []
        if packed.status == "deferred":
            status = "deferred"
            decision = RouteDecision("failed", "packing_deferred")
            needs.append(EvaluationNeed("assessment", "packing_deferred"))
        elif decision.route == "review":
            status = "needs_review"
            needs.append(EvaluationNeed("review", decision.reason, decision.answer_ids))
        elif decision.route == "failed":
            status = "failed"
            needs.append(EvaluationNeed("assessment", decision.reason, decision.answer_ids))
        elif decision.route == "observe":
            status = "observe"
        else:
            deep_packed = replace(self.packer.pack_post(snapshot, stage="deep", question_set=questions), batch_index=batch_index)
            deep = self._evaluate_stage(deep_packed, questions, batch_memo)
            deep_decision = policy.decide(deep.parsed)
            if deep_packed.status == "deferred":
                status = "deferred"
                decision = RouteDecision("failed", "deep_packing_deferred")
                needs.append(EvaluationNeed("assessment", decision.reason))
            elif deep_decision.route in {"review", "failed", "observe"}:
                decision = deep_decision
                status = {"review": "needs_review", "failed": "failed", "observe": "observe"}[decision.route]
                if status != "observe":
                    needs.append(EvaluationNeed("review" if status == "needs_review" else "assessment", decision.reason, decision.answer_ids))
            else:
                output = deep.parsed.output
                unavailable = tuple(key for key, answer in output.answers.items() if answer.status == "insufficient_context")
                unclear = deep_decision.reason == "relevance_unclear"
                weak_unrelated = deep_decision.reason == "weak_unrelated_signal"
                if unclear or unavailable:
                    status = "needs_context"
                    fields = tuple(dict.fromkeys((("relevance",) if unclear else ()) + unavailable))
                    decision = RouteDecision("context", "context_insufficient", fields)
                    needs.append(EvaluationNeed("context", decision.reason, fields))
                elif weak_unrelated:
                    status = "needs_review"
                    decision = RouteDecision("review", "relevance_uncertain_after_deep", ("relevance",))
                    needs.append(EvaluationNeed("review", decision.reason, decision.answer_ids))
                elif deep.parsed.status == "partial":
                    status = "partial"
                    fields = tuple(key for key, answer in output.answers.items() if answer.status != "valid")
                    decision = RouteDecision("partial", "deep_answers_incomplete", fields)
                    needs.append(EvaluationNeed("assessment", decision.reason, fields))
                else:
                    status = "evaluated"
                    decision = RouteDecision("complete", "deep_completed")
        final_stage = deep if deep is not None else triage
        output = final_stage.parsed.output
        stance = output.usable_answers.get("policy_stance") if output is not None else None
        assignment = snapshot.task_context.assigned_side if type(snapshot) is JevInput and snapshot.task_context is not None else None
        alignment = relative_stance(stance.value, assignment) if stance is not None else "unknown"
        return EvaluationFlow(str(uuid4()), status, decision, triage, deep, alignment, tuple(needs), policy, batch_index)

    def evaluate_batch(self, contents: list | tuple) -> tuple[EvaluationFlow, ...]:
        """Independent bodies, bounded chunks/workers, stable upstream order.

        Chunk size uses CTN_JEV_BATCH; it is not a provider multi-post payload.
        Per-item packing/service failures remain results, not batch-wide drops.
        """
        self._check_config()
        if type(contents) not in {list, tuple}:
            raise EvaluatorValidationError("contents: expected list or tuple")
        results = []
        batch_memo = {}  # Deterministic reuse within this batch, even with TTL=0.
        chunk_size = self.client.config.triage_batch_size
        with ThreadPoolExecutor(max_workers=self.client.config.max_concurrency) as pool:
            for offset in range(0, len(contents), chunk_size):
                futures = [pool.submit(self._evaluate, item, batch_index=offset + i, batch_memo=batch_memo)
                           for i, item in enumerate(contents[offset:offset + chunk_size])]
                results.extend(future.result() for future in futures)
        return tuple(results)

    def compare(self, current: JevInput | dict, historical: JevInput | dict) -> StageEvaluation:
        """Evaluate one caller-selected pair; no historical retrieval/frequency.

        Comparison rubric has its own gates. This explicit entry point does not
        use the single-post triage policy or imply either author should be replied to.
        """
        questions = self.comparison_questions
        packed = self.packer.pack_comparison(current, historical, question_set=questions)
        return self._evaluate_stage(packed, questions)
