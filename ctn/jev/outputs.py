"""Normalized evaluation results consumed by decision and LLM-task modules.

These records validate our output contract, not the provider's HTTP response.
Provider parsing and matching answers against requested criteria belong to T06.
No result here authorizes an action or represents observed competition points.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from types import MappingProxyType
from typing import Literal

from .inputs import ContextIssue, JevInput


OUTPUT_SCHEMA_VERSION = "jev-output.v1"
_TOLERANCE = 1e-6


def _score_consistency_tolerance(value: float, probabilities: Mapping[str, float]) -> float:
    """Bound separately rounded wire scores/distributions; retain the score.

    Live JEV responses can expose two decimal places independently for both.
    Only that observed precision receives rounding tolerance. With sum(p)=1,
    centering indices bounds the weighted error by .005*sum(abs(i-center));
    the score's own rounding adds .005. This is not a decision-threshold margin.
    """
    if all(math.isclose(number * 100, round(number * 100), rel_tol=0, abs_tol=1e-8)
           for number in (value, *probabilities.values())):
        center = (len(probabilities) - 1) / 2
        return .005 * (1 + math.fsum(abs(int(key) - center) for key in probabilities)) + _TOLERANCE
    return _TOLERANCE


class OutputValidationError(ValueError):
    def __init__(self, path: str, code: str, message: str) -> None:
        self.path, self.code, self.message = path, code, message
        super().__init__(f"{path or '$'}: {message}")


def _text(value: object, path: str) -> None:
    if type(value) is not str or not value.strip():
        raise OutputValidationError(path, "value", "expected a nonblank string")


def _number(value: object, path: str, low: float, high: float = math.inf) -> None:
    if type(value) not in (int, float):
        raise OutputValidationError(path, "type", "expected a number")
    try:
        valid = math.isfinite(value) and low <= value <= high
    except OverflowError:
        valid = False
    if not valid:
        raise OutputValidationError(path, "value", "number outside its finite range")


def _option(value: object, options: set[str], path: str) -> None:
    if type(value) is not str or value not in options:
        raise OutputValidationError(path, "enum", "unsupported option")


def _object(payload: object, allowed: set[str], required: set[str]) -> dict:
    if type(payload) is not dict:
        raise OutputValidationError("", "type", "expected an object")
    if payload.keys() - allowed:
        raise OutputValidationError("", "unknown_field", "contains undefined fields")
    missing = required - payload.keys()
    if missing:
        raise OutputValidationError(sorted(missing)[0], "required", "field is required")
    return payload


def _at(path: str, factory, payload):
    try:
        return factory(payload)
    except OutputValidationError as error:
        location = f"{path}.{error.path}" if error.path else path
        raise OutputValidationError(location, error.code, error.message) from None


def _freeze_json(value: object, path: str = "raw_response") -> object:
    """Copy and freeze audit data without interpreting it as model instructions."""
    if isinstance(value, Mapping):
        copied = {}
        for key, item in value.items():
            if type(key) is not str:
                raise OutputValidationError(path, "type", "JSON keys must be strings")
            copied[key] = _freeze_json(item, path)
        return MappingProxyType(copied)
    if type(value) in (list, tuple):
        return tuple(_freeze_json(item, path) for item in value)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise OutputValidationError(path, "type", "expected finite JSON data")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class AnswerIssue:
    code: str
    message: str
    fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.code, "code")
        _text(self.message, "message")
        if type(self.fields) is not tuple:
            raise OutputValidationError("fields", "type", "expected field paths")
        for path in self.fields:
            _text(path, "fields")

    @classmethod
    def from_dict(cls, payload: object) -> AnswerIssue:
        data = _object(payload, {"code", "message", "fields"}, {"code", "message"})
        paths = data.get("fields", [])
        if type(paths) is not list:
            raise OutputValidationError("fields", "type", "expected an array")
        return cls(data["code"], data["message"], tuple(paths))

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "fields": list(self.fields)}


@dataclass(frozen=True)
class AssessmentAnswer:
    type: Literal["choice", "score", "noul"]
    status: Literal["valid", "missing", "invalid", "insufficient_context"]
    value: str | float | None = None
    probabilities: Mapping[str, float] | None = None
    confidence: float | None = None
    legend: Mapping[str, str] | None = None
    issue: AnswerIssue | None = None

    def __post_init__(self) -> None:
        _option(self.type, {"choice", "score", "noul"}, "type")
        _option(self.status, {"valid", "missing", "invalid", "insufficient_context"}, "status")
        if self.issue is not None and type(self.issue) is not AnswerIssue:
            raise OutputValidationError("issue", "type", "expected an answer issue")
        if self.status != "valid":
            if self.issue is None:
                raise OutputValidationError("issue", "required", "unavailable answer needs a reason")
            if any(v is not None for v in (self.value, self.probabilities, self.confidence, self.legend)):
                raise OutputValidationError("value", "relationship", "unavailable answers cannot expose usable values")
            return
        if self.issue is not None:
            raise OutputValidationError("issue", "relationship", "valid answer cannot carry a failure reason")
        if self.type == "noul":
            _number(self.value, "value", 0, 1)
            if any(v is not None for v in (self.probabilities, self.confidence, self.legend)):
                raise OutputValidationError("confidence", "relationship", "Noul has only its yes probability")
            return
        _number(self.confidence, "confidence", 0, 1)
        if not isinstance(self.probabilities, Mapping) or not self.probabilities:
            raise OutputValidationError("probabilities", "required", "expected a probability distribution")
        probabilities = dict(self.probabilities)
        for key, probability in probabilities.items():
            _text(key, "probabilities")
            _number(probability, "probabilities", 0, 1)
        if not math.isclose(math.fsum(probabilities.values()), 1, rel_tol=0, abs_tol=_TOLERANCE):
            raise OutputValidationError("probabilities", "value", "probabilities must sum to one")
        object.__setattr__(self, "probabilities", MappingProxyType(probabilities))
        if self.type == "choice":
            _text(self.value, "value")
            if len(probabilities) > 255 or self.value not in probabilities:
                raise OutputValidationError("value", "relationship", "choice must belong to its distribution")
            if probabilities[self.value] + _TOLERANCE < max(probabilities.values()):
                raise OutputValidationError("value", "relationship", "choice must be a highest-probability option")
            if self.legend is not None:
                raise OutputValidationError("legend", "relationship", "Choice does not use score levels")
            return
        if not isinstance(self.legend, Mapping) or not 2 <= len(self.legend) <= 10:
            raise OutputValidationError("legend", "value", "expected two to ten score levels")
        legend = dict(self.legend)
        if set(legend) != {str(i) for i in range(len(legend))} or set(legend) != set(probabilities):
            raise OutputValidationError("legend", "relationship", "levels and distribution must match zero-based indices")
        for description in legend.values():
            _text(description, "legend")
        _number(self.value, "value", 0, len(legend) - 1)
        expected = math.fsum(int(key) * probability for key, probability in probabilities.items())
        if not math.isclose(self.value, expected, rel_tol=0,
                            abs_tol=_score_consistency_tolerance(self.value, probabilities)):
            raise OutputValidationError("value", "relationship", "score must match the probability-weighted levels")
        object.__setattr__(self, "legend", MappingProxyType(legend))

    @classmethod
    def unavailable(cls, type: str, status: str, message: str, *, fields: tuple[str, ...] = ()) -> AssessmentAnswer:
        if status == "valid":
            raise OutputValidationError("status", "value", "unavailable status cannot be valid")
        return cls(type, status, issue=AnswerIssue(status, message, fields))

    @classmethod
    def from_dict(cls, payload: object) -> AssessmentAnswer:
        data = _object(payload, {"type", "status", "value", "probabilities", "confidence", "legend", "issue"}, {"type", "status"})
        issue = data.get("issue")
        if issue is not None:
            issue = _at("issue", AnswerIssue.from_dict, issue)
        return cls(data["type"], data["status"], data.get("value"), data.get("probabilities"),
                   data.get("confidence"), data.get("legend"), issue)

    def to_dict(self) -> dict:
        return {
            "type": self.type, "status": self.status, "value": self.value,
            "probabilities": _thaw_json(self.probabilities), "confidence": self.confidence,
            "legend": _thaw_json(self.legend),
            "issue": self.issue.to_dict() if self.issue else None,
        }


@dataclass(frozen=True)
class EvaluationError:
    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        _text(self.code, "code")
        _text(self.message, "message")
        if type(self.retryable) is not bool:
            raise OutputValidationError("retryable", "type", "expected a boolean")

    @classmethod
    def from_dict(cls, payload: object) -> EvaluationError:
        data = _object(payload, {"code", "message", "retryable"}, {"code", "message"})
        return cls(**data)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise OutputValidationError(name, "value", "expected a nonnegative integer")

    @classmethod
    def from_dict(cls, payload: object) -> TokenUsage:
        data = _object(payload, {"input_tokens", "output_tokens"}, {"input_tokens", "output_tokens"})
        return cls(**data)

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass(frozen=True)
class JevOutput:
    evaluation_id: str
    post_id: str
    source_kind: Literal["platform", "synthetic", "manual"]
    stage: Literal["triage", "deep", "response"]
    question_set_version: str
    answers: Mapping[str, AssessmentAnswer]
    request_id: str | None = None
    model: str | None = None
    usage: TokenUsage | None = None
    latency_ms: float | None = None
    context_issues: tuple[ContextIssue, ...] = ()
    error: EvaluationError | None = None
    raw_response: Mapping[str, object] | None = field(default=None, repr=False)
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        for name in ("evaluation_id", "post_id", "question_set_version", "evaluated_at"):
            _text(getattr(self, name), name)
        _option(self.source_kind, {"platform", "synthetic", "manual"}, "source_kind")
        _option(self.stage, {"triage", "deep", "response"}, "stage")
        for name in ("request_id", "model"):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)
        try:
            timestamp = datetime.fromisoformat(self.evaluated_at.replace("Z", "+00:00"))
            if "T" not in self.evaluated_at or timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise OutputValidationError("evaluated_at", "timestamp", "expected datetime with timezone") from None
        if not isinstance(self.answers, Mapping):
            raise OutputValidationError("answers", "type", "expected named answer records")
        answers = dict(self.answers)
        for key, answer in answers.items():
            _text(key, "answers")
            if type(answer) is not AssessmentAnswer:
                raise OutputValidationError("answers", "type", "expected validated answer records")
        object.__setattr__(self, "answers", MappingProxyType(answers))
        if self.usage is not None and type(self.usage) is not TokenUsage:
            raise OutputValidationError("usage", "type", "expected token usage")
        if self.error is not None and type(self.error) is not EvaluationError:
            raise OutputValidationError("error", "type", "expected evaluation error")
        if not self.answers and self.error is None:
            raise OutputValidationError("error", "required", "empty evaluation needs a failure reason")
        if self.latency_ms is not None:
            _number(self.latency_ms, "latency_ms", 0)
        if type(self.context_issues) is not tuple:
            raise OutputValidationError("context_issues", "type", "expected context issue records")
        for issue in self.context_issues:
            if type(issue) is not ContextIssue:
                raise OutputValidationError("context_issues", "type", "expected context issue records")
            _text(issue.path, "context_issues.path")
            _option(issue.reason, {"unknown", "partial", "truncated", "unverified"}, "context_issues.reason")
        if self.raw_response is not None:
            if not isinstance(self.raw_response, Mapping):
                raise OutputValidationError("raw_response", "type", "expected an object")
            object.__setattr__(self, "raw_response", _freeze_json(self.raw_response))

    @property
    def status(self) -> Literal["success", "partial", "failed"]:
        """Completeness, not certainty, safety approval, or permission to reply."""
        if self.error is not None or not self.answers:
            return "failed"
        valid = sum(answer.status == "valid" for answer in self.answers.values())
        if valid == len(self.answers):
            return "success"
        return "partial" if valid else "failed"

    @property
    def usable_answers(self) -> Mapping[str, AssessmentAnswer]:
        if self.error is not None:
            return MappingProxyType({})
        return MappingProxyType({key: answer for key, answer in self.answers.items() if answer.status == "valid"})

    @classmethod
    def for_input(cls, content: JevInput, **kwargs) -> JevOutput:
        """Carry input identity and limitations forward without recomputing them."""
        if type(content) is not JevInput:
            raise OutputValidationError("content", "type", "expected validated JevInput")
        reserved = {"post_id", "source_kind", "context_issues"}
        if reserved & kwargs.keys():
            raise OutputValidationError("content", "relationship", "input identity and issues cannot be overridden")
        return cls(post_id=content.post.post_id, source_kind=content.source.kind,
                   context_issues=content.context_issues, **kwargs)

    def to_dict(self, *, include_raw: bool = False) -> dict:
        """Raw provider output is opt-in audit data, excluded from default handoff."""
        result = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "evaluation_id": self.evaluation_id, "post_id": self.post_id,
            "source_kind": self.source_kind, "stage": self.stage,
            "question_set_version": self.question_set_version, "status": self.status,
            "answers": {key: answer.to_dict() for key, answer in self.answers.items()},
            "request_id": self.request_id, "model": self.model,
            "usage": self.usage.to_dict() if self.usage else None,
            "latency_ms": self.latency_ms, "evaluated_at": self.evaluated_at,
            "context_issues": [{"path": issue.path, "reason": issue.reason} for issue in self.context_issues],
            "error": self.error.to_dict() if self.error else None,
        }
        if include_raw:
            result["raw_response"] = _thaw_json(self.raw_response)
        return result

    @classmethod
    def from_dict(cls, payload: object) -> JevOutput:
        """Read normalized output, not a raw response from the Jev API."""
        required = {"schema_version", "evaluation_id", "post_id", "source_kind", "stage",
                    "question_set_version", "status", "answers", "evaluated_at"}
        allowed = required | {"request_id", "model", "usage", "latency_ms", "evaluated_at",
                              "context_issues", "error", "raw_response"}
        data = dict(_object(payload, allowed, required))
        if data.pop("schema_version") != OUTPUT_SCHEMA_VERSION:
            raise OutputValidationError("schema_version", "enum", "unsupported output version")
        declared_status = data.pop("status")
        if type(data["answers"]) is not dict:
            raise OutputValidationError("answers", "type", "expected an object")
        data["answers"] = {key: _at(f"answers.{key}", AssessmentAnswer.from_dict, value)
                           for key, value in data["answers"].items()}
        for name, record_type in (("error", EvaluationError), ("usage", TokenUsage)):
            if data.get(name) is not None:
                data[name] = _at(name, record_type.from_dict, data[name])
        issues = data.get("context_issues", [])
        if type(issues) is not list:
            raise OutputValidationError("context_issues", "type", "expected an array")
        parsed_issues = []
        for i, issue in enumerate(issues):
            issue = _at(f"context_issues[{i}]", lambda p: _object(p, {"path", "reason"}, {"path", "reason"}), issue)
            parsed_issues.append(ContextIssue(**issue))
        data["context_issues"] = tuple(parsed_issues)
        output = cls(**data)
        if declared_status != output.status:
            raise OutputValidationError("status", "relationship", "status disagrees with answer availability or error")
        return output
