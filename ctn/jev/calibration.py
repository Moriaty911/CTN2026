"""Local, versioned assessment review and routing-only replay.

No collection, network calls, weight training or automatic adoption. Replaying
thresholds reuses fixed answers, not a hypothetical new JEV classification.
Human labels must be confirmed per field before they contribute to metrics.
"""

from dataclasses import dataclass, field
from datetime import datetime
import json
import math

from .evaluator import StageEvaluation
from .outputs import JevOutput
from .parsing import ParsingResult
from .questions import QuestionSet
from .routing import RoutingPolicy


class CalibrationValidationError(ValueError):
    """Invalid review data; no supplied content appears in error messages."""


def _text(value, name):
    if type(value) is not str or not value.strip():
        raise CalibrationValidationError(f"{name}: expected nonblank string")


def _time(value, name):
    _text(value, name)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value or result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result
    except ValueError:
        raise CalibrationValidationError(f"{name}: expected timestamp with timezone") from None


def _number(value, name, low=None, high=None):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or (low is not None and value < low) or (high is not None and value > high):
        raise CalibrationValidationError(f"{name}: invalid numeric value")


@dataclass(frozen=True)
class HumanReference:
    question_id: str
    kind: str  # label, level, yes_no, unavailable
    value: str | float | bool | None
    status: str  # confirmed, candidate, disputed
    revision: str
    reviewed_at: str | None = None

    def __post_init__(self):
        _text(self.question_id, "question_id")
        _text(self.revision, "revision")
        if self.status not in {"confirmed", "candidate", "disputed"}:
            raise CalibrationValidationError("status: unsupported reference status")
        if self.kind == "label":
            _text(self.value, "value")
        elif self.kind == "level":
            _number(self.value, "value", 0)
        elif self.kind == "yes_no":
            if type(self.value) is not bool:
                raise CalibrationValidationError("value: expected boolean reference")
        elif self.kind != "unavailable" or self.value is not None:
            raise CalibrationValidationError("kind: invalid reference")
        if self.reviewed_at is not None:
            _time(self.reviewed_at, "reviewed_at")
        if self.status == "confirmed" and self.reviewed_at is None:
            raise CalibrationValidationError("reviewed_at: required for confirmed reference")

    def to_dict(self):
        return dict(question_id=self.question_id, kind=self.kind, value=self.value,
                    status=self.status, revision=self.revision, reviewed_at=self.reviewed_at)


@dataclass(frozen=True)
class ObservedFeedback:
    """Caller-supplied observation, not a measured causal effect of a reply."""
    post_id: str
    action_id: str
    observed_at: str
    source_id: str
    metrics: tuple[tuple[str, float | None], ...] = ()

    def __post_init__(self):
        for name in ("post_id", "action_id", "source_id"):
            _text(getattr(self, name), name)
        _time(self.observed_at, "observed_at")
        if type(self.metrics) is not tuple:
            raise CalibrationValidationError("metrics: expected immutable pairs")
        names = set()
        for pair in self.metrics:
            if type(pair) is not tuple or len(pair) != 2:
                raise CalibrationValidationError("metrics: expected name/value pairs")
            name, value = pair
            _text(name, "metric name")
            if name in names:
                raise CalibrationValidationError("metrics: duplicate name")
            names.add(name)
            if value is not None:
                _number(value, "metric value")

    def to_dict(self):
        return dict(post_id=self.post_id, action_id=self.action_id, observed_at=self.observed_at,
                    source_id=self.source_id, metrics=dict(self.metrics), causal_attribution="not_established")


@dataclass(frozen=True)
class CalibrationSample:
    """Serializable normalized result plus provenance; text/raw excluded.

    Keep original content separately via the handoff/input contract if a new
    question version needs re-evaluation. This sample cannot reconstruct it.
    """
    _json: str = field(repr=False)

    @classmethod
    def capture(cls, case_id: str, stage: StageEvaluation, *, references=(), feedback=None):
        _text(case_id, "case_id")
        if type(stage) is not StageEvaluation or stage.parsed._packed is None:
            raise CalibrationValidationError("stage: expected bound stage")
        packed, output = stage.parsed._packed, stage.parsed.output
        if output is None or packed.status != "ready" or output.request_id != packed.request_id:
            raise CalibrationValidationError("stage: unavailable bound identity")
        if output.post_id != packed.targets[0].post_id or output.stage != packed.stage:
            raise CalibrationValidationError("stage: target mismatch")
        if type(references) is not tuple or any(type(r) is not HumanReference for r in references):
            raise CalibrationValidationError("references: expected immutable human references")
        if len({r.question_id for r in references}) != len(references):
            raise CalibrationValidationError("references: duplicate question")
        if feedback is not None:
            if type(feedback) is not ObservedFeedback or feedback.post_id != output.post_id:
                raise CalibrationValidationError("feedback: target mismatch")
            if _time(feedback.observed_at, "observed_at") < _time(output.evaluated_at, "evaluated_at"):
                raise CalibrationValidationError("feedback: observation predates evaluation")
        data = {"schema_version": "jev-calibration-sample.v1", "case_id": case_id,
            "request": packed.to_dict(), "output": output.to_dict(),
            "evaluation_source": stage.source, "new_attempt_count": stage.new_attempt_count,
            "references": [r.to_dict() for r in references], "feedback": feedback.to_dict() if feedback else None}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, payload):
        if type(payload) is not dict or set(payload) != {"schema_version", "case_id", "request", "output", "evaluation_source", "new_attempt_count", "references", "feedback"}:
            raise CalibrationValidationError("sample: invalid fields")
        if payload["schema_version"] != "jev-calibration-sample.v1":
            raise CalibrationValidationError("sample: unsupported version")
        _text(payload["case_id"], "case_id")
        if payload["evaluation_source"] not in {"network", "cache", "shared", "not_sent"}:
            raise CalibrationValidationError("evaluation_source: invalid provenance")
        if type(payload["new_attempt_count"]) is not int or payload["new_attempt_count"] < 0:
            raise CalibrationValidationError("new_attempt_count: invalid count")
        if payload["evaluation_source"] in {"cache", "shared", "not_sent"} and payload["new_attempt_count"] != 0:
            raise CalibrationValidationError("new_attempt_count: inconsistent provenance")
        if type(payload["output"]) is not dict or "raw_response" in payload["output"]:
            raise CalibrationValidationError("output: raw response is not a calibration field")
        output = JevOutput.from_dict(payload["output"])
        request = payload["request"]
        request_fields = {"schema_version", "status", "kind", "stage", "targets", "issues", "question_ids",
            "answer_bindings", "question_set_version", "question_set_fingerprint", "packing_fingerprint",
            "input_fingerprint", "request_id", "state_bytes", "request_bytes", "batch_index"}
        if (type(request) is not dict or request_fields - request.keys() or
            request.keys() - request_fields - {"injection_preflight"} or request.get("status") != "ready"):
            raise CalibrationValidationError("request: expected metadata only")
        if "injection_preflight" in request:
            check = request["injection_preflight"]
            keys = {"schema_version", "policy_fingerprint", "status", "complete", "limits_reached", "findings", "scope"}
            if (type(check) is not dict or set(check) != keys or
                check["schema_version"] != "jev-injection-preflight.v1" or
                check["status"] != "no_local_signal" or check["complete"] is not True or
                check["findings"] != [] or check["limits_reached"] != [] or
                check["scope"] != "local_heuristics_not_a_safety_guarantee" or
                type(check["policy_fingerprint"]) is not str or len(check["policy_fingerprint"]) != 64 or
                any(c not in "0123456789abcdef" for c in check["policy_fingerprint"])):
                raise CalibrationValidationError("request: invalid preflight metadata")
        targets = request.get("targets")
        if (type(targets) is not list or not targets or type(targets[0]) is not dict or
            targets[0].get("post_id") != output.post_id or targets[0].get("source_kind") != output.source_kind or
            request.get("request_id") != output.request_id or request.get("stage") != output.stage or
            request.get("question_set_version") != output.question_set_version or
            (request.get("kind"), request.get("stage")) not in {
                ("post", "triage"), ("post", "deep"), ("comparison", "deep"), ("response", "response")}):
            raise CalibrationValidationError("sample: provenance mismatch")
        for name in ("request_id", "question_set_fingerprint", "packing_fingerprint", "input_fingerprint"):
            value = request.get(name)
            if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise CalibrationValidationError("request: invalid fingerprint")
        refs = payload["references"]
        if type(refs) is not list:
            raise CalibrationValidationError("references: expected list")
        try:
            references = tuple(HumanReference(**r) for r in refs)
        except TypeError:
            raise CalibrationValidationError("references: invalid fields") from None
        if len({r.question_id for r in references}) != len(references):
            raise CalibrationValidationError("references: duplicate question")
        feedback = payload["feedback"]
        if feedback is not None:
            if type(feedback) is not dict or set(feedback) != {"post_id", "action_id", "observed_at", "source_id", "metrics", "causal_attribution"} or type(feedback["metrics"]) is not dict:
                raise CalibrationValidationError("feedback: invalid fields")
            observed = ObservedFeedback(feedback["post_id"], feedback["action_id"], feedback["observed_at"],
                                        feedback["source_id"], tuple(feedback["metrics"].items()))
            if (observed.post_id != output.post_id or feedback["causal_attribution"] != "not_established" or
                _time(observed.observed_at, "observed_at") < _time(output.evaluated_at, "evaluated_at")):
                raise CalibrationValidationError("feedback: provenance mismatch")
        return cls(json.dumps(payload, ensure_ascii=False, allow_nan=False))

    def to_dict(self):
        return json.loads(self._json)


def replay_routes(samples, baseline: RoutingPolicy, candidate: RoutingPolicy):
    """Only triage routing can be compared from a fixed triage assessment.

    A newly admitted item might lack deep answers: this reports that requirement,
    never predicts the final action or simulates a missing deep evaluation.
    """
    if type(baseline) is not RoutingPolicy or type(candidate) is not RoutingPolicy:
        raise CalibrationValidationError("policy: expected routing policy")
    rows, seen = [], set()
    for sample in samples:
        if type(sample) is not CalibrationSample:
            raise CalibrationValidationError("sample: expected calibration sample")
        data = sample.to_dict()
        if data["case_id"] in seen:
            raise CalibrationValidationError("samples: duplicate case identifier")
        seen.add(data["case_id"])
        if data["request"]["kind"] != "post" or data["request"]["stage"] != "triage":
            raise CalibrationValidationError("replay: requires post triage assessment")
        output = JevOutput.from_dict(data["output"])
        parsed = ParsingResult(output, (), output.error)
        old, new = baseline.decide(parsed), candidate.decide(parsed)
        rows.append({"case_id": data["case_id"], "evaluation_id": output.evaluation_id,
            "question_set_fingerprint": data["request"]["question_set_fingerprint"],
            "baseline": old.to_dict(), "candidate": new.to_dict(), "changed": old != new,
            "candidate_deep_evaluation_required": new.route == "deep",
            "feedback": data["feedback"]})
    return {"schema_version": "jev-route-replay.v1", "mode": "fixed_answers_triage_only",
            "baseline_policy": baseline.to_dict(), "baseline_fingerprint": baseline.fingerprint,
            "candidate_policy": candidate.to_dict(), "candidate_fingerprint": candidate.fingerprint,
            "sample_count": len(rows), "changed_count": sum(r["changed"] for r in rows),
            "adopted": False, "rows": rows}


def review_assessments(samples, questions: QuestionSet):
    """Per-field evaluation without turning draft labels into ground truth.

    Levels report absolute error against human anchors, not exact integer match.
    Boolean Noul references expose the returned signal for review; no unapproved
    0.5 threshold is used to claim classification accuracy.
    """
    if type(questions) is not QuestionSet:
        raise CalibrationValidationError("questions: expected question set")
    samples = tuple(samples)
    rows, seen = [], set()
    for sample in samples:
        if type(sample) is not CalibrationSample:
            raise CalibrationValidationError("sample: expected calibration sample")
        data = sample.to_dict()
        if data["case_id"] in seen:
            raise CalibrationValidationError("samples: duplicate case identifier")
        seen.add(data["case_id"])
        if (data["request"]["question_set_fingerprint"] != questions.fingerprint or
            data["request"]["question_set_version"] != questions.version):
            raise CalibrationValidationError("questions: reference version mismatch")
        specs = questions.for_stage(data["request"]["stage"])
        output = JevOutput.from_dict(data["output"])
        for raw in data["references"]:
            ref = HumanReference(**raw)
            spec = specs.get(ref.question_id)
            if spec is None:
                raise CalibrationValidationError("reference: question not in stage")
            if (ref.kind == "label" and (spec.type != "choice" or ref.value not in spec.criteria) or
                ref.kind == "level" and (spec.type != "score" or ref.value > len(spec.criteria) - 1) or
                ref.kind == "yes_no" and spec.type != "noul"):
                raise CalibrationValidationError("reference: incompatible value")
            answer = output.answers.get(ref.question_id)
            available = output.error is None and answer is not None and answer.status == "valid"
            row = {"case_id": data["case_id"], "question_id": ref.question_id, "reference": ref.to_dict(),
                   "answer_status": answer.status if answer else "missing", "actual": answer.value if available else None,
                   "match": None, "absolute_error": None, "eligible": ref.status == "confirmed"}
            if ref.status == "confirmed":
                if ref.kind == "unavailable":
                    # Missing/invalid are engineering failures, not context judgment.
                    row["match"] = output.error is None and answer is not None and answer.status == "insufficient_context"
                elif ref.kind == "label":
                    row["match"] = available and answer.value == ref.value
                elif ref.kind == "level" and available:
                    row["absolute_error"] = abs(answer.value - ref.value)
            rows.append(row)
    labels = [r for r in rows if r["eligible"] and r["reference"]["kind"] == "label"]
    levels = [r["absolute_error"] for r in rows if r["absolute_error"] is not None]
    return {"schema_version": "jev-review-report.v1", "question_set_version": questions.version,
            "question_set_fingerprint": questions.fingerprint, "rows": rows,
            "confirmed_label_count": len(labels), "label_match_count": sum(r["match"] for r in labels),
            "label_accuracy": sum(r["match"] for r in labels) / len(labels) if labels else None,
            "available_level_count": len(levels), "mean_absolute_level_error": sum(levels) / len(levels) if levels else None,
            "candidate_or_disputed_count": sum(not r["eligible"] for r in rows),
            "feedback_missing_count": sum(s.to_dict()["feedback"] is None for s in samples)}


def compare_assessment_runs(baseline_samples, candidate_samples):
    """Compare separately supplied assessments, including question changes.

    Matching case IDs alone is insufficient: full input fingerprints and target
    provenance must match. Different packing strategies remain visible. Added
    or removed answers stay absent; differences do not prove an improvement.
    """
    def indexed(samples):
        result = {}
        for sample in samples:
            if type(sample) is not CalibrationSample:
                raise CalibrationValidationError("sample: expected calibration sample")
            data = sample.to_dict()
            if data["case_id"] in result:
                raise CalibrationValidationError("samples: duplicate case identifier")
            result[data["case_id"]] = data
        return result

    old, new = indexed(baseline_samples), indexed(candidate_samples)
    rows = []
    for case_id in sorted(old.keys() | new.keys()):
        left, right = old.get(case_id), new.get(case_id)
        if left is None or right is None:
            rows.append({"case_id": case_id, "status": "missing_baseline" if left is None else "missing_candidate"})
            continue
        for name in ("input_fingerprint", "targets", "kind", "stage"):
            if left["request"][name] != right["request"][name]:
                raise CalibrationValidationError("comparison: input provenance mismatch")
        differences = []
        a, b = left["output"]["answers"], right["output"]["answers"]
        for question_id in sorted(a.keys() | b.keys()):
            if a.get(question_id) != b.get(question_id):
                differences.append({"question_id": question_id, "baseline": a.get(question_id), "candidate": b.get(question_id)})
        def provenance(data):
            return {"request_id": data["request"]["request_id"],
                "question_set_version": data["request"]["question_set_version"],
                "question_set_fingerprint": data["request"]["question_set_fingerprint"],
                "packing_fingerprint": data["request"]["packing_fingerprint"],
                "evaluation_id": data["output"]["evaluation_id"], "evaluated_at": data["output"]["evaluated_at"],
                "model": data["output"]["model"], "status": data["output"]["status"],
                "evaluation_source": data["evaluation_source"], "new_attempt_count": data["new_attempt_count"]}
        rows.append({"case_id": case_id, "status": "compared", "baseline": provenance(left), "candidate": provenance(right),
                     "answer_differences": differences, "same_evaluation": left["output"]["evaluation_id"] == right["output"]["evaluation_id"]})
    return {"schema_version": "jev-assessment-diff.v1", "adopted": False, "rows": rows,
            "compared_count": sum(r["status"] == "compared" for r in rows),
            "missing_count": sum(r["status"] != "compared" for r in rows)}
