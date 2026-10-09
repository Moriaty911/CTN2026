"""Pack validated content into provider request bodies without making calls.

Targets remain verbatim. Optional materials are selected as whole records;
omissions and unknown context travel with the request. Budgets measure UTF-8
JSON bytes, not model tokens or official service limits. A batch is a sequence
of independently packed requests; shared-state batching/scheduling belongs to
T07. Caller-owned credentials belong to T05, never to state or this module.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
import re
from typing import Literal

from .inputs import BackgroundReference, InputValidationError, JevInput
from .questions import QuestionSet
from .rubrics import load_rubric


PACKING_SCHEMA_VERSION = "jev-packing.v1"
_MATERIAL_KINDS = ("parent", "quoted", "verified_background", "comment", "related", "history", "profile", "hashtags")


class PackingValidationError(ValueError):
    """Configuration error that never echoes supplied values."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _reference_data(reference: BackgroundReference) -> dict:
    return {"reference_id": reference.reference_id, "source_id": reference.source_id, "text": reference.text}


def _wire_limitations(issues) -> list[dict]:
    """Bound omission descriptions by group; exact original paths stay local."""
    groups = {}
    for issue in issues:
        group_path = re.sub(r"\[\d+\]$", "", issue.path)
        key = (group_path, issue.reason)
        if key not in groups:
            groups[key] = {"path": group_path, "reason": issue.reason, "count": 0}
        groups[key]["count"] += 1
    return list(groups.values())


@dataclass(frozen=True)
class PackingLimits:
    """Adjustable engineering defaults; no claim about provider token limits."""

    max_state_bytes: int = 32_768
    max_request_bytes: int = 131_072
    max_conversation_items: int = 20
    max_history_events: int = 20
    context_priority: tuple[str, ...] = _MATERIAL_KINDS

    def __post_init__(self) -> None:
        for name in ("max_state_bytes", "max_request_bytes", "max_conversation_items", "max_history_events"):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name.endswith("bytes") else 0):
                raise PackingValidationError(f"{name}: invalid packing limit")
        if type(self.context_priority) is not tuple or any(type(v) is not str for v in self.context_priority) or (
            len(self.context_priority) != len(_MATERIAL_KINDS) or set(self.context_priority) != set(_MATERIAL_KINDS)
        ):
            raise PackingValidationError("context_priority: expected every material kind exactly once")

    def to_dict(self) -> dict:
        return {**{name: getattr(self, name) for name in (
            "max_state_bytes", "max_request_bytes", "max_conversation_items", "max_history_events")},
            "context_priority": list(self.context_priority)}


@dataclass(frozen=True)
class PackingIssue:
    """Paths use original input indices; no omitted text is copied here."""

    path: str
    reason: str

    def to_dict(self) -> dict:
        return {"path": self.path, "reason": self.reason}


@dataclass(frozen=True)
class PackedTarget:
    role: str
    source_kind: str
    post_id: str | None
    content_path: str

    def to_dict(self) -> dict:
        return {"role": self.role, "source_kind": self.source_kind,
                "post_id": self.post_id, "content_path": self.content_path}


@dataclass(frozen=True)
class PackingResult:
    """Immutable request snapshot. Default serialization is metadata only.

    ready means that the body fits the packing rules, not that any question is
    answerable or that a platform action is permitted. T06/T07 interpret gates.
    """

    status: Literal["ready", "deferred"]
    kind: Literal["post", "comparison"]
    stage: str
    targets: tuple[PackedTarget, ...]
    issues: tuple[PackingIssue, ...]
    question_ids: tuple[str, ...]
    question_set_version: str
    question_set_fingerprint: str
    packing_fingerprint: str
    input_fingerprint: str | None
    request_id: str | None = None
    state_bytes: int | None = None
    request_bytes: int | None = None
    batch_index: int | None = None
    _body_json: str | None = field(default=None, repr=False)

    def to_provider_bytes(self) -> bytes:
        """Exact UTF-8 transport body used for the request byte budget."""
        if self.status != "ready" or self._body_json is None:
            raise PackingValidationError("deferred material has no provider body")
        return self._body_json.encode("utf-8")

    def to_provider_body(self) -> dict:
        """Return a detached body containing only model, state and questions."""
        return json.loads(self.to_provider_bytes())

    def to_dict(self, *, include_body: bool = False) -> dict:
        result = {
            "schema_version": PACKING_SCHEMA_VERSION,
            "status": self.status, "kind": self.kind, "stage": self.stage,
            "targets": [t.to_dict() for t in self.targets],
            "issues": [i.to_dict() for i in self.issues],
            "question_ids": list(self.question_ids),
            "answer_bindings": {question_id: {
                "question_id": question_id,
                "content_path": "content",
                "targets": [t.to_dict() for t in self.targets],
            } for question_id in self.question_ids},
            "question_set_version": self.question_set_version,
            "question_set_fingerprint": self.question_set_fingerprint,
            "packing_fingerprint": self.packing_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "request_id": self.request_id,
            "state_bytes": self.state_bytes, "request_bytes": self.request_bytes,
            "batch_index": self.batch_index,
        }
        if include_body and self.status == "ready":
            result["body"] = self.to_provider_body()
        return result


# Pattern checks supplement exact caller-supplied secrets; they cannot identify
# every arbitrary secret. They do not strip text or claim to stop injections.
_SECRET_PATTERNS = (
    re.compile(r"\bapikey_[A-Za-z0-9_-]{24,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password|authorization)\s*[:=]\s*[\"']?[^\s\"',;]{8,}", re.IGNORECASE),
)


@dataclass(frozen=True)
class _Candidate:
    path: str
    record_index: int
    kind: str
    value: object = field(repr=False)
    source_index: int | None = None


class JevPacker:
    """Explicit local policy and trusted registry, independent of env/network.

    verified_references must be registered by trusted local configuration, not
    copied from each upstream record. All of ID, source and text must match.
    Registry matching verifies provenance against that configuration; it is not
    a scientific fact check. Unmatched references never enter the trusted area.
    """

    def __init__(self, *, limits: PackingLimits | None = None,
                 verified_references: tuple[BackgroundReference, ...] = (),
                 forbidden_values: tuple[str, ...] = (), model: str = "jev-latest") -> None:
        self.limits = limits if limits is not None else PackingLimits()
        if type(self.limits) is not PackingLimits:
            raise PackingValidationError("limits: expected packing limits")
        if type(forbidden_values) is not tuple or any(type(v) is not str or not v for v in forbidden_values):
            raise PackingValidationError("forbidden_values: expected nonempty strings in a tuple")
        self._forbidden_values = forbidden_values
        if type(model) is not str or not model.strip() or self._secret(model) or self._unicode_paths(model):
            raise PackingValidationError("model: expected a model identifier without credentials")
        self.model = model
        if type(verified_references) is not tuple or any(type(r) is not BackgroundReference for r in verified_references):
            raise PackingValidationError("verified_references: expected local reference records")
        self._registry = {}
        for reference in verified_references:
            key = (reference.reference_id, reference.source_id)
            if key in self._registry:
                raise PackingValidationError("verified_references: duplicate local reference")
            if self._secret_paths(_reference_data(reference)) or self._unicode_paths(_reference_data(reference)):
                raise PackingValidationError("verified_references: credential-like material is not allowed")
            self._registry[key] = reference

    @property
    def fingerprint(self) -> str:
        return _digest({"schema_version": PACKING_SCHEMA_VERSION,
                        "limits": self.limits.to_dict(),
                        "priority": list(self.limits.context_priority),
                        "registry": [_reference_data(r) for _, r in sorted(self._registry.items())]})

    def _secret(self, text: str) -> bool:
        return any(v in text for v in self._forbidden_values) or any(p.search(text) for p in _SECRET_PATTERNS)

    def _secret_paths(self, value: object, path: str = "") -> list[str]:
        if type(value) is str:
            return [path] if self._secret(value) else []
        if type(value) is dict:
            return [found for key, child in value.items()
                    for found in self._secret_paths(child, f"{path}.{key}" if path else key)]
        if type(value) is list:
            return [found for index, child in enumerate(value)
                    for found in self._secret_paths(child, f"{path}[{index}]")]
        return []

    def _unicode_paths(self, value: object, path: str = "") -> list[str]:
        if type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return [path]
        elif type(value) is dict:
            return [found for key, child in value.items()
                    for found in self._unicode_paths(child, f"{path}.{key}" if path else key)]
        elif type(value) is list:
            return [found for index, child in enumerate(value)
                    for found in self._unicode_paths(child, f"{path}[{index}]")]
        return []

    def pack_post(self, content: JevInput | dict, *, stage: Literal["triage", "deep"] = "triage",
                  question_set: QuestionSet | None = None) -> PackingResult:
        return self._pack((content,), kind="post", stage=stage,
                          question_set=question_set if question_set is not None else load_rubric("post"))

    def pack_comparison(self, current: JevInput | dict, historical: JevInput | dict, *,
                        question_set: QuestionSet | None = None) -> PackingResult:
        return self._pack((current, historical), kind="comparison", stage="deep",
                          question_set=question_set if question_set is not None else load_rubric("comparison"))

    def pack_batch(self, contents: list[JevInput | dict] | tuple[JevInput | dict, ...], *,
                   stage: Literal["triage", "deep"] = "triage",
                   question_set: QuestionSet | None = None) -> tuple[PackingResult, ...]:
        """Keep input order and failures isolated; does not send or schedule."""
        if type(contents) not in {list, tuple}:
            raise PackingValidationError("contents: expected a list or tuple")
        rubric = question_set if question_set is not None else load_rubric("post")
        return tuple(replace(self.pack_post(record, stage=stage, question_set=rubric), batch_index=index)
                     for index, record in enumerate(contents))

    def _pack(self, contents: tuple, *, kind: str, stage: str, question_set: QuestionSet) -> PackingResult:
        if type(question_set) is not QuestionSet or question_set.status != "confirmed":
            raise PackingValidationError("question_set: expected a confirmed baseline")
        questions = question_set.to_provider_questions(stage, content_path="content")
        configuration = question_set.to_dict()
        if self._secret_paths(configuration) or self._unicode_paths(configuration):
            raise PackingValidationError("question_set: expected valid text without credentials")
        common = {"kind": kind, "stage": stage,
                  "question_ids": tuple(questions), "question_set_version": question_set.version,
                  "question_set_fingerprint": question_set.fingerprint,
                  "packing_fingerprint": self.fingerprint}
        records = []
        for index, supplied in enumerate(contents):
            try:
                record = supplied if type(supplied) is JevInput else JevInput.from_dict(supplied)
            except InputValidationError as error:
                prefix = "content" if kind == "post" else f"content.{('current', 'historical')[index]}"
                return PackingResult(status="deferred", targets=(), input_fingerprint=None,
                                     issues=(PackingIssue(f"{prefix}.{error.path}", f"invalid_input:{error.code}"),), **common)
            records.append(record)
        original = [r.to_dict() for r in records]
        targets = tuple(PackedTarget(
            role="target" if kind == "post" else ("current", "historical")[index],
            source_kind=r.source.kind,
            post_id=None if self._secret(r.post.post_id) or self._unicode_paths(r.post.post_id) else r.post.post_id,
            content_path="content" if kind == "post" else f"content.{('current', 'historical')[index]}",
        ) for index, r in enumerate(records))
        unicode_issues = tuple(PackingIssue(f"{targets[index].content_path}.{path}", "invalid_unicode")
                               for index, record in enumerate(original) for path in self._unicode_paths(record))
        if unicode_issues:
            return PackingResult(status="deferred", targets=targets, issues=unicode_issues,
                                 input_fingerprint=None, **common)
        input_fingerprint = _digest(original)
        credential_issues = tuple(PackingIssue(
            f"{targets[index].content_path}.{path}", "credential_detected")
            for index, data in enumerate(original) for path in self._secret_paths(data))
        if credential_issues:
            return PackingResult(status="deferred", targets=targets, issues=credential_issues,
                                 input_fingerprint=input_fingerprint, **common)
        if kind == "comparison" and (records[0].source.kind, records[0].post.post_id) == (records[1].source.kind, records[1].post.post_id):
            return PackingResult(status="deferred", targets=targets,
                                 issues=(PackingIssue("content", "same_post_pair"),),
                                 input_fingerprint=input_fingerprint, **common)

        data = deepcopy(original)
        backgrounds = [[] for _ in records]
        issues: dict[str, PackingIssue] = {}
        candidates: list[_Candidate] = []
        for index, record in enumerate(records):
            prefix = targets[index].content_path
            packed = data[index]
            packed["trusted_background"] = None
            if record.conversation is not None:
                packed["conversation"]["items"] = []
                if record.conversation.items:
                    packed["conversation"]["status"] = "partial"
                ranked = sorted(enumerate(record.conversation.items), key=lambda pair: (
                    self.limits.context_priority.index(pair[1].role), pair[0]))
                for rank, (item_index, item) in enumerate(ranked):
                    path = f"{prefix}.conversation.items[{item_index}]"
                    issues[path] = PackingIssue(path, "item_limit" if rank >= self.limits.max_conversation_items else "budget_omitted")
                    if rank < self.limits.max_conversation_items:
                        candidates.append(_Candidate(path, index, item.role, original[index]["conversation"]["items"][item_index], item_index))
            if record.trusted_background is not None:
                seen = set()
                for ref_index, reference in enumerate(record.trusted_background.items):
                    path = f"{prefix}.trusted_background.items[{ref_index}]"
                    key = (reference.reference_id, reference.source_id)
                    if self._registry.get(key) != reference:
                        issues[path] = PackingIssue(path, "unverified_reference")
                    elif key in seen:
                        issues[path] = PackingIssue(path, "duplicate_reference")
                    else:
                        seen.add(key)
                        issues[path] = PackingIssue(path, "budget_omitted")
                        candidates.append(_Candidate(path, index, "verified_background", _reference_data(reference)))
            if record.interaction_history is not None:
                packed["interaction_history"]["events"] = []
                if record.interaction_history.events:
                    packed["interaction_history"]["status"] = "partial"
                for event_index, event in enumerate(record.interaction_history.events):
                    path = f"{prefix}.interaction_history.events[{event_index}]"
                    issues[path] = PackingIssue(path, "item_limit" if event_index >= self.limits.max_history_events else "budget_omitted")
                    if event_index < self.limits.max_history_events:
                        candidates.append(_Candidate(path, index, "history", original[index]["interaction_history"]["events"][event_index]))
            if record.author is not None and record.author.profile_text is not None:
                packed["author"]["profile_text"] = None
                path = f"{prefix}.author.profile_text"
                issues[path] = PackingIssue(path, "budget_omitted")
                candidates.append(_Candidate(path, index, "profile", record.author.profile_text))
            if record.post.hashtags is not None:
                packed["post"]["hashtags"] = None
                path = f"{prefix}.post.hashtags"
                issues[path] = PackingIssue(path, "budget_omitted")
                candidates.append(_Candidate(path, index, "hashtags", list(record.post.hashtags)))

        def state_for(packed_records, verified, limitations):
            context_issues = []
            for index, packed in enumerate(packed_records):
                for issue in JevInput.from_dict(packed).context_issues:
                    # Background is intentionally supplied in a separate area.
                    if issue.path == "trusted_background":
                        continue
                    context_issues.append({"path": f"{targets[index].content_path}.{issue.path}", "reason": issue.reason})
            def background_for(index):
                supplied = records[index].trusted_background
                count = len(supplied.items) if supplied is not None else 0
                status = "not_provided" if supplied is None else "empty" if count == 0 else (
                    "verified" if len(verified[index]) == count else "partial" if verified[index] else "unavailable")
                return {"items": verified[index], "status": status, "verification": "local_registry_match"}

            return {
                "content": packed_records[0] if kind == "post" else dict(zip(("current", "historical"), packed_records)),
                "verified_background": background_for(0)
                    if kind == "post" else {role: background_for(index)
                                            for index, role in enumerate(("current", "historical"))},
                "packing": {"schema_version": PACKING_SCHEMA_VERSION,
                            "policy_fingerprint": common["packing_fingerprint"],
                            "limitations": _wire_limitations(limitations.values()),
                            "context_issues": context_issues},
            }

        def encode(packed_records, verified, limitations):
            state = state_for(packed_records, verified, limitations)
            state_size = len(_json(state).encode("utf-8"))
            encoded = _json({"model": self.model, "state": state, "questions": questions})
            request_size = len(encoded.encode("utf-8"))
            return encoded, state_size, request_size

        def fits(state_size, request_size):
            return state_size <= self.limits.max_state_bytes and request_size <= self.limits.max_request_bytes

        encoded, state_size, request_size = encode(data, backgrounds, issues)
        if not fits(state_size, request_size):
            reason = "core_state_over_budget" if state_size > self.limits.max_state_bytes else "core_request_over_budget"
            return PackingResult(status="deferred", targets=targets, issues=(PackingIssue("content", reason),),
                                 input_fingerprint=input_fingerprint, state_bytes=state_size, request_bytes=request_size, **common)
        candidates.sort(key=lambda c: self.limits.context_priority.index(c.kind))
        selected_context = [{} for _ in records]
        for candidate in candidates:
            trial_data, trial_backgrounds, trial_issues = deepcopy(data), deepcopy(backgrounds), dict(issues)
            trial_selected = deepcopy(selected_context)
            packed = trial_data[candidate.record_index]
            if candidate.kind in {"parent", "quoted", "comment", "related"}:
                trial_selected[candidate.record_index][candidate.source_index] = candidate.value
                packed["conversation"]["items"] = [value for _, value in sorted(trial_selected[candidate.record_index].items())]
                if len(packed["conversation"]["items"]) == len(records[candidate.record_index].conversation.items):
                    packed["conversation"]["status"] = records[candidate.record_index].conversation.status
            elif candidate.kind == "verified_background":
                trial_backgrounds[candidate.record_index].append(candidate.value)
            elif candidate.kind == "history":
                packed["interaction_history"]["events"].append(candidate.value)
                if len(packed["interaction_history"]["events"]) == len(records[candidate.record_index].interaction_history.events):
                    packed["interaction_history"]["status"] = records[candidate.record_index].interaction_history.status
            elif candidate.kind == "profile":
                packed["author"]["profile_text"] = candidate.value
            elif candidate.kind == "hashtags":
                packed["post"]["hashtags"] = candidate.value
            del trial_issues[candidate.path]
            trial_encoded, trial_state_size, trial_request_size = encode(trial_data, trial_backgrounds, trial_issues)
            if fits(trial_state_size, trial_request_size):
                data, backgrounds, issues = trial_data, trial_backgrounds, trial_issues
                selected_context = trial_selected
                encoded, state_size, request_size = trial_encoded, trial_state_size, trial_request_size
        return PackingResult(status="ready", targets=targets, issues=tuple(issues.values()),
                             input_fingerprint=input_fingerprint,
                             request_id=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                             state_bytes=state_size, request_bytes=request_size, _body_json=encoded, **common)
