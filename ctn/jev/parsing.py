"""Provider JSON -> validated assessments bound to the exact packed request.

No network, environment reads, action thresholds, reply generation or inferred
defaults. Transport success alone is insufficient. Unknown Choice labels remain
valid labels; failed availability gates remove usable dependent values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math

from .client import JevCallResult
from .inputs import ContextIssue, JevInput
from .injection import InjectionPreflight
from .outputs import AnswerIssue, AssessmentAnswer, EvaluationError, JevOutput, OutputValidationError, TokenUsage
from .packing import PackingResult
from .questions import QuestionSet, QuestionValidationError
from .rubrics import load_rubric


PARSING_SCHEMA_VERSION = "jev-parsing.v1"


class ParsingValidationError(ValueError):
    """Caller/configuration error; never includes supplied values."""


@dataclass(frozen=True)
class ParsingIssue:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class ParsingResult:
    """Assessment plus local provenance, including both comparison targets.

    Keep this wrapper when handing off comparisons/batches. output is None only
    when packing has no safe target identity; never invent a post ID. The packed
    body and optional raw response are excluded from default serialization.
    """

    output: JevOutput | None
    issues: tuple[ParsingIssue, ...]
    error: EvaluationError | None = None
    _packed: PackingResult | None = field(default=None, repr=False)

    @property
    def status(self) -> str:
        return self.output.status if self.output is not None else "failed"

    @property
    def targets(self):
        return self._packed.targets

    def to_dict(self, *, include_raw: bool = False) -> dict:
        return {"schema_version": PARSING_SCHEMA_VERSION, "status": self.status,
                "output": self.output.to_dict(include_raw=include_raw) if self.output else None,
                "request": self._packed.to_dict(),
                "issues": [issue.to_dict() for issue in self.issues],
                "error": self.error.to_dict() if self.error else None}


_CALL_ERRORS = {
    "disabled", "packing_deferred", "missing_credentials", "credentials_in_request", "model_mismatch",
    "invalid_packed_request", "queue_wait_exceeded", "wait_budget_exceeded", "retry_wait_exceeds_budget",
    "authentication_error", "request_rejected", "rate_limited", "overloaded", "service_error",
    "redirect_rejected", "http_error", "timeout", "network_error", "tls_error", "response_too_large",
    "invalid_response_json", "credentials_in_response", "transport_error",
}


def _unavailable(spec, status, code, message, *fields):
    return AssessmentAnswer(spec.type, status, issue=AnswerIssue(code, message, tuple(fields)))


def _answer(payload: object, spec, wire_question: dict) -> AssessmentAnswer:
    expected_fields = {"noul": {"type", "noul"},
                       "choice": {"type", "choice", "probabilities", "confidence"},
                       "score": {"type", "score", "probabilities", "confidence", "legend"}}[spec.type]
    if type(payload) is not dict:
        raise OutputValidationError("", "type", "expected an answer object")
    if payload.get("type") != spec.type:
        raise OutputValidationError("type", "type_mismatch", "answer type differs from requested type")
    if expected_fields - payload.keys():
        raise OutputValidationError(sorted(expected_fields - payload.keys())[0], "required", "answer field is missing")
    if payload.keys() - expected_fields:
        raise OutputValidationError("", "unknown_field", "answer contains unsupported fields")
    if spec.type == "noul":
        return AssessmentAnswer("noul", "valid", payload["noul"])
    probabilities = payload["probabilities"]
    expected_options = set(wire_question["criteria"]) if spec.type == "choice" else {
        str(i) for i in range(len(wire_question["criteria"]))}
    if type(probabilities) is not dict or set(probabilities) != expected_options:
        raise OutputValidationError("probabilities", "criteria_mismatch", "distribution must match every requested option")
    if spec.type == "choice":
        return AssessmentAnswer("choice", "valid", payload["choice"], probabilities, payload["confidence"])
    expected_legend = {str(i): description for i, description in enumerate(wire_question["criteria"])}
    if type(payload["legend"]) is not dict or payload["legend"] != expected_legend:
        raise OutputValidationError("legend", "criteria_mismatch", "legend must match the requested ordered levels")
    return AssessmentAnswer("score", "valid", payload["score"], probabilities, payload["confidence"], payload["legend"])


class JevResponseParser:
    def parse(self, call: JevCallResult, *, question_set: QuestionSet | None = None,
              expected_request_id: str | None = None, retain_raw: bool = False) -> ParsingResult:
        """Consume the T05 result, not an unrelated dictionary of answers.

        Optional expected_request_id lets a dispatcher check its work item. The
        provider does not echo a post/request identity, so local binding is the
        guarantee; arbitrary responses cannot be proven authentic by their IDs.
        Usage problems are audit warnings, never fabricated zero-token usage.
        Invalid model/answers envelopes fail the assessment; extra top-level
        fields or unrequested answers are ignored with safe warnings.
        """
        if type(call) is not JevCallResult or type(call.packed_request) is not PackingResult:
            raise ParsingValidationError("call: expected a bound JEV call result")
        if type(retain_raw) is not bool:
            raise ParsingValidationError("retain_raw: expected boolean")
        if expected_request_id is not None and (type(expected_request_id) is not str or not expected_request_id):
            raise ParsingValidationError("expected_request_id: expected nonempty identifier")
        if question_set is not None and type(question_set) is not QuestionSet:
            raise ParsingValidationError("question_set: expected validated questions")
        if type(call.call_id) is not str or not call.call_id.strip():
            raise ParsingValidationError("call_id: expected local call identifier")
        packed = call.packed_request
        if (packed.kind, packed.stage) not in {("post", "triage"), ("post", "deep"), ("comparison", "deep"), ("response", "response"), ("postflight", "response")}:
            raise ParsingValidationError("packed: unsupported local request kind/stage")
        rubric = question_set if question_set is not None else load_rubric(packed.kind,
            version="v2" if packed.kind == "post" and packed.question_set_version == "ctn-post.v2" else "v1")
        issues = []
        context_issues = ()
        specs = {}
        wire_questions = {}

        def finish(answers=None, error=None, model=None, usage=None, raw=None):
            latency = call.elapsed_s * 1000 if type(call.elapsed_s) in (int, float) and (
                math.isfinite(call.elapsed_s) and call.elapsed_s >= 0) else None
            target = packed.targets[0] if packed.targets else None
            output = None
            if target is not None and target.post_id is not None:
                output = JevOutput(evaluation_id=call.call_id, post_id=target.post_id,
                    source_kind=target.source_kind, stage=packed.stage,
                    question_set_version=packed.question_set_version, request_id=packed.request_id,
                    answers=answers or {}, model=model, usage=usage, latency_ms=latency,
                    context_issues=context_issues, error=error, raw_response=raw)
            return ParsingResult(output, tuple(issues), error, packed)

        def fail(code, message):
            answers = {key: _unavailable(spec, "invalid", code, message, "request") for key, spec in specs.items()}
            return finish(answers, EvaluationError(code, message))

        if call.request_id != packed.request_id or (expected_request_id is not None and expected_request_id != call.request_id):
            return fail("request_binding_mismatch", "Call does not match the expected packed request.")
        try:
            specs = dict(rubric.for_stage(packed.stage))
            wire_questions = rubric.to_provider_questions(packed.stage)
        except QuestionValidationError:
            return fail("question_set_mismatch", "Question set cannot describe the packed stage.")
        if (rubric.status != "confirmed" or rubric.version != packed.question_set_version or
            rubric.fingerprint != packed.question_set_fingerprint or tuple(specs) != packed.question_ids):
            return fail("question_set_mismatch", "Question version, fingerprint or IDs differ from the packed request.")

        if packed.status == "ready":
            try:
                body_bytes = packed.to_provider_bytes()
                body = packed.to_provider_body()
                if hashlib.sha256(body_bytes).hexdigest() != packed.request_id or body["questions"] != wire_questions:
                    return fail("request_binding_mismatch", "Packed request body differs from its recorded identity/questions.")
                expected_roles = ("target",) if packed.kind in {"post", "response", "postflight"} else ("current", "historical")
                if tuple(t.role for t in packed.targets) != expected_roles:
                    return fail("request_binding_mismatch", "Packed target roles do not match the request kind.")
                collected_issues = []
                if packed.kind == "postflight":
                    for target in packed.targets:
                        target_data = body.get("state", {}).get("content", {}).get("target", {})
                        if target.content_path != "content" or target.post_id != target_data.get("post_id"):
                            return fail("request_binding_mismatch", "Packed target identity differs from the supplied state.")
                else:
                    for target in packed.targets:
                        path = "content" if packed.kind in {"post", "response"} else "content." + target.role
                        record = body["state"]["content"] if packed.kind in {"post", "response"} else body["state"]["content"][target.role]
                        content = JevInput.from_dict(record)
                        if (target.content_path != path or target.post_id != content.post.post_id or
                            target.source_kind != content.source.kind):
                            return fail("request_binding_mismatch", "Packed target identity differs from the supplied state.")
                        collected_issues.extend(ContextIssue(path + "." + item.path, item.reason)
                                                for item in content.context_issues if item.path != "trusted_background")
                        background = body["state"]["verified_background"] if packed.kind in {"post", "response"} else (
                            body["state"]["verified_background"][target.role])
                        if background["status"] not in {"empty", "verified"}:
                            reason = "unknown" if background["status"] == "not_provided" else "unverified"
                            collected_issues.append(ContextIssue(path + ".trusted_background", reason))
                context_issues = tuple(collected_issues)
            except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
                return fail("invalid_packed_request", "Packed request could not be validated against its local binding.")

        if call.status != "success":
            code = call.error_code if call.error_code in _CALL_ERRORS else "call_failed"
            message = "JEV transport did not return a usable response."
            answers = {key: _unavailable(spec, "missing", code, message) for key, spec in specs.items()}
            return finish(answers, EvaluationError(code, message, call.retryable))
        if packed.status != "ready":
            return fail("request_binding_mismatch", "A successful response requires a ready packed request.")
        try:
            raw = call.provider_response()
        except (ValueError, UnicodeError, RecursionError):
            return fail("invalid_response_json", "Provider response is not a valid JSON object.")
        if type(raw.get("model")) is not str or not raw["model"].strip():
            return fail("invalid_response_envelope", "Provider response needs its actual model identifier.")
        try:
            raw["model"].encode("utf-8")
        except UnicodeError:
            return fail("invalid_response_envelope", "Provider model identifier is not valid UTF-8 text.")
        # The provider's free-text model identifier is not an instruction. Other
        # accepted answer strings are bound to our exact options/score legends.
        if InjectionPreflight().check(raw["model"], path="response.model").requires_review:
            return fail("response_metadata_requires_review", "Provider metadata requires review before use.")
        if type(raw.get("answers")) is not dict:
            return fail("invalid_response_envelope", "Provider response needs a named answers object.")
        if set(raw) - {"model", "answers", "usage"}:
            issues.append(ParsingIssue("response", "extra_fields", "Unrecognized envelope fields were ignored."))
        if set(raw["answers"]) - set(specs):
            issues.append(ParsingIssue("answers", "unexpected_answers", "Unrequested answers were ignored."))
        usage = None
        try:
            usage = TokenUsage.from_dict(raw.get("usage"))
        except OutputValidationError:
            issues.append(ParsingIssue("usage", "invalid_usage", "Token usage is missing or invalid; it remains unknown."))
        answers = {}
        for key, spec in specs.items():
            if key not in raw["answers"]:
                answers[key] = _unavailable(spec, "missing", "missing_answer", "Requested answer was not returned.", "answers." + key)
                continue
            try:
                answers[key] = _answer(raw["answers"][key], spec, wire_questions[key])
                if spec.type == "score":
                    expected = math.fsum(int(level) * probability
                                         for level, probability in answers[key].probabilities.items())
                    if not math.isclose(answers[key].value, expected, rel_tol=0, abs_tol=1e-6):
                        issues.append(ParsingIssue("answers." + key, "score_rounding_tolerance",
                            "Separately rounded score/distribution accepted within a bounded tolerance; provider score retained."))
            except OutputValidationError as error:
                path = "answers." + key + ("." + error.path if error.path else "")
                answers[key] = _unavailable(spec, "invalid", error.code, error.message, path)
        # All provider answers are independent. Combine only the already
        # configured availability dependencies, after validating every answer.
        for key, spec in specs.items():
            if spec.availability_question_id is None or answers[key].status != "valid":
                continue
            gate = answers[spec.availability_question_id]
            if gate.status != "valid" or gate.value not in spec.available_when:
                answers[key] = _unavailable(spec, "insufficient_context", "availability_gate",
                    "Availability evidence does not permit using this answer.",
                    "answers." + spec.availability_question_id)
        return finish(answers, model=raw["model"], usage=usage, raw=raw if retain_raw else None)
