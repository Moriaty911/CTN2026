"""Map the existing pipeline Post type; never use mock-only hidden NPC fields."""

from datetime import datetime, timezone

from ..jev.inputs import JevInput
from ..models import Post
from .domain import CitizenError, number, utc_now


def content_from_post(post: Post, *, source_kind, author_id=None, observed_at=None):
    """Caller supplies stable author ID or leaves it unknown; handle isn't an ID.

    Use source_kind='synthetic' for MockLegitClient. No API endpoint or identity
    labels are invented, and raw NPC stance/kind fields are deliberately ignored.
    """
    if type(post) is not Post:
        raise CitizenError("expected_pipeline_post")
    number(post.created_at, 0, 253402300799)
    created_at = datetime.fromtimestamp(post.created_at, timezone.utc).isoformat() if post.created_at else None
    raw = post.raw
    data = {"schema_version": "jev-input.v1", "post": {"post_id": post.post_id, "text": post.text,
            "author_id": author_id, "created_at": created_at, "kind": raw.get("kind", "unknown"),
            "parent_id": raw.get("parent_id"), "text_complete": raw.get("text_complete")},
            "source": {"kind": source_kind, "observed_at": observed_at or utc_now()}}
    # The existing Post type defaults missing counts to zero; do not propagate
    # those defaults as observations unless the adapter actually supplied them.
    metrics = {key: raw[key] for key in ("likes", "reposts", "replies") if key in raw}
    if metrics:
        data["metrics"] = {**metrics, "observed_at": data["source"]["observed_at"]}
    return JevInput.from_dict(data)
