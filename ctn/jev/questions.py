"""Configurable JEV questions, independent of competition rubrics and HTTP.

Business criteria live in JSON supplied to ``QuestionSet.load``. This module
validates their structure and compiles provider questions; it does not decide
which dimensions to use, apply action thresholds, or classify content itself.
Instructions use the literal ``{content_path}`` to address one input record.
See https://docs.typesafe.ai/api for the supported provider shapes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Literal


QUESTION_SCHEMA_VERSION = "jev-questions.v1"
_MARKER = "{content_path}"
_PATH = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\[[0-9]+\])*(?:\.[A-Za-z_][A-Za-z_0-9]*(?:\[[0-9]+\])*)*")
_ID = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


class QuestionValidationError(ValueError):
    """Safe configuration error with a location, not the supplied text."""

    def __init__(self, path: str, code: str, message: str) -> None:
        self.path, self.code, self.message = path, code, message
        super().__init__(f"{path or '$'}: {message}")


def _text(value: object, path: str) -> None:
    if type(value) is not str or not value.strip():
        raise QuestionValidationError(path, "value", "expected a nonblank string")


def _object(payload: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    if type(payload) is not dict:
        raise QuestionValidationError("", "type", "expected an object")
    if payload.keys() - required - optional:
        raise QuestionValidationError("", "unknown_field", "contains undefined fields")
    if required - payload.keys():
        raise QuestionValidationError(sorted(required - payload.keys())[0], "required", "field is required")
    return payload


def _option(value: object, options: set[str], path: str) -> None:
    if type(value) is not str or value not in options:
        raise QuestionValidationError(path, "enum", "unsupported option")


@dataclass(frozen=True)
class QuestionSpec:
    type: Literal["choice", "score", "noul"]
    title: str
    instructions: str
    stages: tuple[Literal["triage", "deep", "response"], ...]
    context_limits: str
    criteria: Mapping[str, str] | tuple[str, ...] | None = None
    unknown_option: str | None = None
    availability_question_id: str | None = None
    available_when: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _option(self.type, {"choice", "score", "noul"}, "type")
        for name in ("title", "instructions", "context_limits"):
            _text(getattr(self, name), name)
        if _MARKER not in self.instructions:
            raise QuestionValidationError("instructions", "reference", "must explicitly reference {content_path}")
        if type(self.stages) is not tuple or not self.stages:
            raise QuestionValidationError("stages", "type", "expected a nonempty tuple of stages")
        for stage in self.stages:
            _option(stage, {"triage", "deep", "response"}, "stages")
        if len(set(self.stages)) != len(self.stages):
            raise QuestionValidationError("stages", "duplicate", "stages must be unique")
        if self.type == "score":
            if type(self.criteria) is not tuple or not 2 <= len(self.criteria) <= 10:
                raise QuestionValidationError("criteria", "value", "expected two to ten ordered score levels")
            for index, description in enumerate(self.criteria):
                _text(description, f"criteria[{index}]")
        elif self.type == "choice" or self.criteria is not None:
            if not isinstance(self.criteria, Mapping):
                raise QuestionValidationError("criteria", "type", "expected an option-to-description mapping")
            criteria = dict(self.criteria)
            if self.type == "choice" and not 1 <= len(criteria) <= 255:
                raise QuestionValidationError("criteria", "value", "expected one to 255 choice options")
            if self.type == "noul" and set(criteria) != {"true", "false"}:
                raise QuestionValidationError("criteria", "value", "Noul descriptions require true and false keys")
            for option, description in criteria.items():
                _text(option, "criteria")
                _text(description, "criteria")
            object.__setattr__(self, "criteria", MappingProxyType(criteria))
        if self.type == "choice":
            _text(self.unknown_option, "unknown_option")
            if self.unknown_option not in self.criteria:
                raise QuestionValidationError("unknown_option", "relationship", "unknown option must belong to criteria")
        elif self.unknown_option is not None:
            raise QuestionValidationError("unknown_option", "relationship", "only Choice has an unknown option")
        if type(self.available_when) is not tuple:
            raise QuestionValidationError("available_when", "type", "expected a tuple of gate options")
        if self.availability_question_id is None:
            if self.available_when:
                raise QuestionValidationError("available_when", "relationship", "gate options require an availability question")
        else:
            if type(self.availability_question_id) is not str or _ID.fullmatch(self.availability_question_id) is None:
                raise QuestionValidationError("availability_question_id", "reference", "expected a question identifier")
            if not self.available_when:
                raise QuestionValidationError("available_when", "required", "availability question requires accepted options")
            for option in self.available_when:
                _text(option, "available_when")
            if len(set(self.available_when)) != len(self.available_when):
                raise QuestionValidationError("available_when", "duplicate", "accepted options must be unique")

    @classmethod
    def from_dict(cls, payload: object) -> QuestionSpec:
        data = _object(payload, {"type", "title", "instructions", "stages", "context_limits"},
                       {"criteria", "unknown_option", "availability_question_id", "available_when"})
        if type(data["stages"]) is not list:
            raise QuestionValidationError("stages", "type", "expected an array")
        criteria = data.get("criteria")
        if data["type"] == "score":
            if type(criteria) is not list:
                raise QuestionValidationError("criteria", "type", "expected an ordered array")
            criteria = tuple(criteria)
        accepted = data.get("available_when", [])
        if type(accepted) is not list:
            raise QuestionValidationError("available_when", "type", "expected an array")
        return cls(**{**data, "stages": tuple(data["stages"]), "criteria": criteria,
                      "available_when": tuple(accepted)})

    def to_dict(self) -> dict:
        criteria = self.criteria
        return {
            "type": self.type, "title": self.title,
            "instructions": self.instructions, "stages": list(self.stages),
            "context_limits": self.context_limits,
            "criteria": dict(criteria) if isinstance(criteria, Mapping) else list(criteria) if criteria is not None else None,
            "unknown_option": self.unknown_option,
            "availability_question_id": self.availability_question_id,
            "available_when": list(self.available_when),
        }

    @property
    def score_legend(self) -> Mapping[str, str] | None:
        if self.type != "score":
            return None
        return MappingProxyType({str(i): description for i, description in enumerate(self.criteria)})


@dataclass(frozen=True)
class QuestionSet:
    version: str
    status: Literal["draft", "confirmed"]
    shared_instructions: str
    questions: Mapping[str, QuestionSpec]
    schema_version: str = QUESTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _option(self.schema_version, {QUESTION_SCHEMA_VERSION}, "schema_version")
        _text(self.version, "version")
        _option(self.status, {"draft", "confirmed"}, "status")
        _text(self.shared_instructions, "shared_instructions")
        if not isinstance(self.questions, Mapping) or not self.questions:
            raise QuestionValidationError("questions", "type", "expected a nonempty question mapping")
        questions = dict(self.questions)
        for question_id, spec in questions.items():
            if type(question_id) is not str or _ID.fullmatch(question_id) is None:
                raise QuestionValidationError("questions", "value", "question IDs must be identifiers")
            if type(spec) is not QuestionSpec:
                raise QuestionValidationError(f"questions.{question_id}", "type", "expected a question specification")
        for question_id, spec in questions.items():
            if spec.availability_question_id is None:
                continue
            location = f"questions.{question_id}.availability_question_id"
            gate = questions.get(spec.availability_question_id)
            if gate is None or spec.availability_question_id == question_id:
                raise QuestionValidationError(location, "reference", "availability question must reference another question")
            if gate.type != "choice" or gate.availability_question_id is not None:
                raise QuestionValidationError(location, "relationship", "availability gate must be an ungated Choice question")
            if not set(spec.stages) <= set(gate.stages):
                raise QuestionValidationError(location, "relationship", "availability gate must run in all stages of the dependent question")
            if not set(spec.available_when) <= set(gate.criteria) or gate.unknown_option in spec.available_when:
                raise QuestionValidationError(f"questions.{question_id}.available_when", "relationship", "accepted gate options must exist and exclude unknown")
        object.__setattr__(self, "questions", MappingProxyType(questions))

    @classmethod
    def from_dict(cls, payload: object) -> QuestionSet:
        data = _object(payload, {"schema_version", "version", "status", "shared_instructions", "questions"})
        if type(data["questions"]) is not dict:
            raise QuestionValidationError("questions", "type", "expected an object")
        questions = {}
        for question_id, question in data["questions"].items():
            try:
                questions[question_id] = QuestionSpec.from_dict(question)
            except QuestionValidationError as error:
                path = f"questions.{question_id}" + (f".{error.path}" if error.path else "")
                raise QuestionValidationError(path, error.code, error.message) from None
        return cls(**{**data, "questions": questions})

    @classmethod
    def load(cls, path: str | Path) -> QuestionSet:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise QuestionValidationError("", "duplicate", "JSON object keys must be unique")
                result[key] = value
            return result

        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        except (json.JSONDecodeError, UnicodeError):
            raise QuestionValidationError("", "json", "expected a UTF-8 JSON configuration") from None
        return cls.from_dict(payload)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version, "version": self.version,
            "status": self.status, "shared_instructions": self.shared_instructions,
            "questions": {key: spec.to_dict() for key, spec in self.questions.items()},
        }

    @property
    def fingerprint(self) -> str:
        """Content identity alongside the human version; catches reused versions."""
        encoded = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def for_stage(self, stage: Literal["triage", "deep", "response"]) -> Mapping[str, QuestionSpec]:
        _option(stage, {"triage", "deep", "response"}, "stage")
        selected = {key: spec for key, spec in self.questions.items() if stage in spec.stages}
        if not selected:
            raise QuestionValidationError("stage", "empty", "no questions configured for this stage")
        return MappingProxyType(selected)

    def to_provider_questions(self, stage: Literal["triage", "deep", "response"], *, content_path: str = "content") -> dict:
        """Compile only question fields. Does not send requests or enforce routing.

        The caller owns state packing and must supply this path in that state.
        Draft status remains local metadata; synthetic evaluation can use drafts.
        Numeric answers cannot encode missing context. Availability metadata is
        local: T06/T07 must combine the separately evaluated gate answer with the
        dependent answer, fail closed on an unavailable gate, and retain its
        insufficiency. No question sees another answer in the same request.
        """
        if type(content_path) is not str or _PATH.fullmatch(content_path) is None:
            raise QuestionValidationError("content_path", "reference", "expected an explicit object/array path")
        compiled = {}
        for key, spec in self.for_stage(stage).items():
            instructions = {
                "shared_rules": self.shared_instructions,
                "question": spec.instructions,
                "context_limits": spec.context_limits,
            }
            instructions = {name: value.replace(_MARKER, content_path) for name, value in instructions.items()}
            question = {"type": spec.type, "instructions": instructions}
            if spec.criteria is not None:
                if isinstance(spec.criteria, Mapping):
                    question["criteria"] = {
                        option: description.replace(_MARKER, content_path)
                        for option, description in spec.criteria.items()
                    }
                else:
                    question["criteria"] = [description.replace(_MARKER, content_path) for description in spec.criteria]
            compiled[key] = question
        return compiled
