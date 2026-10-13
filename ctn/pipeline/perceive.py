"""READ: turn platform reads into a list of candidate posts.

Reads are metered, so perception is a spend decision. One cycle costs one
`trending` call plus one `search` per query; as the budget ledger's read
pressure rises, the lowest-priority queries are shed first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..actuator import Actuator
from ..models import Post

DEFAULT_KEYWORDS = ("kingstonia", "h2d2", "vaccine", "public spaces")

# How many trending hashtags to add as extra search queries.
TRENDING_QUERIES = 2


@dataclass
class Observation:
    posts: list[Post] = field(default_factory=list)
    trending: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)


def post_from_dict(row: dict[str, Any]) -> Post | None:
    """Platform dict -> `Post`. Returns None for rows without an id."""
    post_id = str(row.get("post_id", "")).strip()
    if not post_id:
        return None

    def as_int(key: str) -> int:
        try:
            return int(row.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    try:
        created_at = float(row.get("created_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        created_at = 0.0

    return Post(
        post_id=post_id,
        author=str(row.get("author", "")),
        text=str(row.get("text", "")),
        created_at=created_at,
        likes=as_int("likes"),
        reposts=as_int("reposts"),
        replies=as_int("replies"),
        hashtags=[str(tag) for tag in row.get("hashtags", []) or []],
        raw=dict(row),
    )


def _shed(queries: list[str], pressure: float) -> list[str]:
    """Keep fewer queries as read pressure rises. Always keep at least one."""
    if pressure <= 0 or not queries:
        return queries
    keep = max(1, int(len(queries) * max(0.0, 1.0 - pressure)))
    return queries[:keep]


async def perceive(
    actuator: Actuator,
    *,
    keywords: Sequence[str] = DEFAULT_KEYWORDS,
    limit: int = 20,
    use_trending: bool = True,
) -> Observation:
    obs = Observation()

    if use_trending:
        rows = await actuator.trending(limit=10)
        obs.trending = [str(r["hashtag"]) for r in rows if isinstance(r, dict) and r.get("hashtag")]

    queries = [str(k).strip() for k in keywords if str(k).strip()]
    known = {q.lower() for q in queries}
    queries += [t for t in obs.trending[:TRENDING_QUERIES] if t.lower() not in known]
    obs.queries = _shed(queries, actuator.ledger.read_pressure)

    seen: set[str] = set()
    for query in obs.queries:
        for row in await actuator.search(query, limit=limit):
            if not isinstance(row, dict):
                continue
            post = post_from_dict(row)
            if post is None or post.post_id in seen:
                continue
            seen.add(post.post_id)
            obs.posts.append(post)
    return obs
