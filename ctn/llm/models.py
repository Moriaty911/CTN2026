"""Drafts are content artifacts, never authorization to publish."""

from dataclasses import dataclass, field


DRAFT_SCHEMA_VERSION = "ctn-reply-draft.v1"
LANGUAGES = {"follow_post", "en", "zh", "es", "fr", "de", "pt", "ar", "ru", "ja", "ko", "hi"}
MODEL_NEEDS = {"supporting_evidence", "source_clarification", "scope_clarification",
               "comparison_material", "human_verification", "comment_sample", "content_review"}


class LlmValidationError(ValueError):
    """Messages never echo untrusted content or credentials."""


@dataclass(frozen=True)
class DraftOptions:
    language: str = "follow_post"
    max_reply_chars: int | None = None

    def __post_init__(self):
        if type(self.language) is not str or self.language not in LANGUAGES:
            raise LlmValidationError("options: unsupported language")
        if self.max_reply_chars is not None and (type(self.max_reply_chars) is not int or self.max_reply_chars <= 0):
            raise LlmValidationError("options: invalid character limit")


@dataclass(frozen=True)
class ReplyDraftResult:
    status: str
    reason: str
    target_post_id: str | None = None
    prompt_type: str | None = None
    request_id: str | None = None
    parent_request_id: str | None = None
    prompt_version: str = "ctn-reply-prompts.v1"
    material_version: str = "jev-generation-material.v1"
    model: str = "deepseek-flash"
    reply: str | None = field(default=None, repr=False)
    needs: tuple[str, ...] = ()
    attempt_count: int = 0
    usage: tuple[tuple[str, int], ...] = ()

    def to_dict(self, *, include_text=False):
        if type(include_text) is not bool:
            raise LlmValidationError("include_text: expected boolean")
        data = {"schema_version": DRAFT_SCHEMA_VERSION, "status": self.status, "reason": self.reason,
                "target_post_id": self.target_post_id, "prompt_type": self.prompt_type,
                "request_id": self.request_id, "parent_request_id": self.parent_request_id,
                "prompt_version": self.prompt_version, "material_version": self.material_version,
                "model": self.model, "needs": list(self.needs), "attempt_count": self.attempt_count,
                "usage": dict(self.usage), "execution_authorized": False,
                "semantic_review_required": True}
        if include_text:
            data["reply"] = self.reply
        return data
