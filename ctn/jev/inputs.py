"""Validated upstream content for Jev; no platform or model calls.

Use ``JevInput.from_dict(payload)`` at the module boundary. Missing context
stays unknown, while malformed supplied data is rejected. Background entries
are only references here: their trust must be checked before model use.
"""

from __future__ import annotations

from dataclasses import MISSING, dataclass, fields
from datetime import datetime
from functools import lru_cache
from types import UnionType
from typing import Literal, get_args, get_origin, get_type_hints


INPUT_SCHEMA_VERSION = "jev-input.v1"


class InputValidationError(ValueError):
    """A safe field-level error that never includes the supplied value."""

    def __init__(self, path: str, code: str, message: str) -> None:
        self.path = path
        self.code = code
        self.message = message
        super().__init__(f"{path or '$'}: {message}")


def _path(parent: str, child: str) -> str:
    return f"{parent}.{child}" if parent else child


@lru_cache(maxsize=None)
def _hints(cls: type) -> dict:
    return get_type_hints(cls)


def _decode(hint: object, value: object, path: str, *, wire: bool) -> object:
    origin, args = get_origin(hint), get_args(hint)
    if origin is UnionType:
        if value is None and type(None) in args:
            return None
        # The input contract uses only T | None, never ambiguous unions.
        return _decode(next(t for t in args if t is not type(None)), value, path, wire=wire)
    if origin is Literal:
        if type(value) is not str or value not in args:
            raise InputValidationError(path, "enum", "unsupported option")
        return value
    if origin is tuple:
        if (wire and type(value) is not list) or (not wire and type(value) is not tuple):
            raise InputValidationError(path, "type", "expected an array")
        return tuple(
            _decode(args[0], item, f"{path}[{i}]", wire=wire)
            for i, item in enumerate(value)
        )
    if isinstance(hint, type) and issubclass(hint, _Record):
        if wire:
            return _load(hint, value, path)
        if type(value) is not hint:
            raise InputValidationError(path, "type", "expected a content record")
        return value
    if type(value) is not hint:
        raise InputValidationError(path, "type", f"expected {hint.__name__}")
    return value


def _load(cls: type, payload: object, path: str):
    if type(payload) is not dict:
        raise InputValidationError(path, "type", "expected an object")
    definitions = {f.name: f for f in fields(cls)}
    if payload.keys() - definitions.keys():
        raise InputValidationError(path, "unknown_field", "contains undefined fields")
    values = {}
    for name, definition in definitions.items():
        if name not in payload:
            if definition.default is MISSING and definition.default_factory is MISSING:
                raise InputValidationError(_path(path, name), "required", "field is required")
            continue
        values[name] = _decode(_hints(cls)[name], payload[name], _path(path, name), wire=True)
    try:
        return cls(**values)
    except InputValidationError as error:
        raise InputValidationError(_path(path, error.path), error.code, error.message) from None


def _encode(value: object) -> object:
    if isinstance(value, _Record):
        return {f.name: _encode(getattr(value, f.name)) for f in fields(value)}
    if type(value) is tuple:
        return [_encode(item) for item in value]
    return value


class _Record:
    def __post_init__(self) -> None:
        for definition in fields(self):
            name = definition.name
            value = getattr(self, name)
            _decode(_hints(type(self))[name], value, name, wire=False)
            if value is None:
                continue
            if name.endswith("_id") or name == "post_id":
                _nonblank(value, name)
            if name in {"created_at", "observed_at", "acted_at"}:
                _timestamp(value, name)
            if type(value) is int and value < 0:
                raise InputValidationError(name, "value", "count must be nonnegative")


def _nonblank(value: str, path: str) -> None:
    if not value.strip():
        raise InputValidationError(path, "value", "must not be blank")


def _timestamp(value: str, path: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise InputValidationError(path, "timestamp", "expected ISO 8601 datetime with timezone") from None


@dataclass(frozen=True)
class ContentSource(_Record):
    kind: Literal["platform", "synthetic", "manual"]
    observed_at: str
    source_id: str | None = None


@dataclass(frozen=True)
class InputPost(_Record):
    post_id: str
    text: str
    author_id: str | None = None
    created_at: str | None = None
    kind: Literal["original", "reply", "quote", "repost", "unknown"] | None = None
    parent_id: str | None = None
    hashtags: tuple[str, ...] | None = None
    text_complete: bool | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _nonblank(self.text, "text")
        if self.parent_id == self.post_id:
            raise InputValidationError("parent_id", "relationship", "post cannot be its own parent")


@dataclass(frozen=True)
class AuthorContext(_Record):
    """Upstream public metadata; platform_level is an explicit platform label.

    Missing level stays None. Profiles, followers and engagement must not be
    used to synthesize it. Platform API mapping remains an upstream concern.
    """

    profile_text: str | None = None
    followers_count: int | None = None
    observed_at: str | None = None
    platform_level: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.platform_level is not None:
            _nonblank(self.platform_level, "platform_level")


@dataclass(frozen=True)
class InteractionMetrics(_Record):
    likes: int | None = None
    reposts: int | None = None
    replies: int | None = None
    observed_at: str | None = None


@dataclass(frozen=True)
class ConversationItem(_Record):
    role: Literal["parent", "quoted", "comment", "related"]
    post_id: str
    text: str
    source: ContentSource
    author_id: str | None = None
    created_at: str | None = None
    text_complete: bool | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _nonblank(self.text, "text")


@dataclass(frozen=True)
class ConversationContext(_Record):
    status: Literal["available", "partial", "not_provided", "not_applicable"]
    items: tuple[ConversationItem, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.status in {"not_provided", "not_applicable"} and self.items:
            raise InputValidationError("items", "relationship", "status requires an empty array")


@dataclass(frozen=True)
class InteractionEvent(_Record):
    kind: Literal["reply", "like", "repost"]
    target_post_id: str
    acted_at: str
    text: str | None = None


@dataclass(frozen=True)
class InteractionHistory(_Record):
    status: Literal["available", "partial", "not_provided"]
    events: tuple[InteractionEvent, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.status == "not_provided" and self.events:
            raise InputValidationError("events", "relationship", "status requires an empty array")


@dataclass(frozen=True)
class TaskContext(_Record):
    assigned_side: Literal["yes", "no", "unknown"] | None = None


@dataclass(frozen=True)
class BackgroundReference(_Record):
    """Supplied reference, not verified trusted knowledge."""

    reference_id: str
    text: str
    source_id: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _nonblank(self.text, "text")


@dataclass(frozen=True)
class BackgroundContext(_Record):
    items: tuple[BackgroundReference, ...]


@dataclass(frozen=True)
class ContextIssue:
    """Descriptive limitation, not a classification or action verdict."""

    path: str
    reason: Literal["unknown", "partial", "truncated", "unverified"]


@dataclass(frozen=True)
class JevInput(_Record):
    schema_version: Literal["jev-input.v1"]
    post: InputPost
    source: ContentSource
    author: AuthorContext | None = None
    metrics: InteractionMetrics | None = None
    conversation: ConversationContext | None = None
    interaction_history: InteractionHistory | None = None
    task_context: TaskContext | None = None
    trusted_background: BackgroundContext | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.conversation is not None:
            parents = [item for item in self.conversation.items if item.role == "parent"]
            if len(parents) > 1:
                raise InputValidationError("conversation.items", "relationship", "multiple direct parents")
            for parent in parents:
                if parent.post_id == self.post.post_id:
                    raise InputValidationError("conversation.items", "relationship", "post cannot be its own parent")
                if self.post.parent_id is not None and parent.post_id != self.post.parent_id:
                    raise InputValidationError("conversation.items", "relationship", "parent does not match parent_id")

    @classmethod
    def from_dict(cls, payload: object) -> JevInput:
        """Validate JSON-compatible upstream data without coercing values."""
        return _load(cls, payload, "")

    def to_dict(self) -> dict:
        """Return detached JSON data; omitted optionals normalize to null."""
        return _encode(self)

    @property
    def context_issues(self) -> tuple[ContextIssue, ...]:
        """Expose missing context without rejecting an otherwise valid post.

        Evaluators must decide which issues matter for each question. No
        absence of history or metadata here grants permission to publish.
        """
        issues = []
        for path, value in (
            ("post.author_id", self.post.author_id),
            ("post.created_at", self.post.created_at),
            ("author", self.author),
            ("metrics", self.metrics),
            ("conversation", self.conversation),
            ("interaction_history", self.interaction_history),
            ("trusted_background", self.trusted_background),
        ):
            if value is None:
                issues.append(ContextIssue(path, "unknown"))
        if self.post.kind in {None, "unknown"}:
            issues.append(ContextIssue("post.kind", "unknown"))
        if self.post.text_complete is not True:
            reason = "truncated" if self.post.text_complete is False else "unknown"
            issues.append(ContextIssue("post.text_complete", reason))
        for name, record in (("author", self.author), ("metrics", self.metrics)):
            if record is not None:
                for definition in fields(record):
                    if getattr(record, definition.name) is None:
                        issues.append(ContextIssue(f"{name}.{definition.name}", "unknown"))
        if self.task_context is None or self.task_context.assigned_side in {None, "unknown"}:
            issues.append(ContextIssue("task_context.assigned_side", "unknown"))
        for name, record in (
            ("conversation", self.conversation),
            ("interaction_history", self.interaction_history),
        ):
            if record is not None and record.status in {"partial", "not_provided"}:
                reason = "partial" if record.status == "partial" else "unknown"
                issues.append(ContextIssue(name, reason))
        items = self.conversation.items if self.conversation is not None else ()
        if (self.post.kind == "reply" or self.post.parent_id is not None) and not any(
            item.role == "parent" for item in items
        ):
            issues.append(ContextIssue("conversation.parent", "unknown"))
        for i, item in enumerate(items):
            if item.text_complete is not True:
                reason = "truncated" if item.text_complete is False else "unknown"
                issues.append(ContextIssue(f"conversation.items[{i}].text_complete", reason))
        if self.trusted_background is not None:
            reason = "unverified" if self.trusted_background.items else "unknown"
            issues.append(ContextIssue("trusted_background", reason))
        return tuple(issues)
