"""The platform boundary.

Everything in the fleet talks to Legit Social through this Protocol and nothing
else. That single rule is what makes the missing official client template a
non-blocker today: `mock.py` implements this Protocol, so the entire pipeline --
Jev decisions, rate limiting, guardrails, persistence, crash recovery -- can be
built and load-tested now, and when the real template lands we write one adapter
(`legit_v1.py`) and change nothing else.

Do not hand-roll HTTPS against guessed endpoints. The action *set* is documented
(post / repost / reply / like / follow / unfollow / search / hashtags); the wire
shapes are not, and guessing them wastes the days before kickoff.

Return-shape convention: every method returns plain dicts/stars and raises
`PlatformError` on failure, with `retryable` telling the caller whether backing
off is worth it. Rate-limit responses are surfaced as `Throttled`, distinct from
generic errors, so the gate can slow down rather than blindly retry.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class PlatformError(Exception):
    """Any platform failure. `retryable` drives the actuator's backoff."""

    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class Throttled(PlatformError):
    """Explicit rate-limit response (HTTP 429 or platform equivalent)."""

    def __init__(self, message: str = "rate limited", *, retry_after_s: float | None = None):
        super().__init__(message, retryable=True, status=429)
        self.retry_after_s = retry_after_s


class AuthError(PlatformError):
    """Credential problem. Never retry blindly; surface it loudly."""

    def __init__(self, message: str = "authentication failed"):
        super().__init__(message, retryable=False, status=401)


class AccountSuspended(PlatformError):
    """The acting account is gone. Retire it and try a different one."""

    def __init__(self, message: str = "account suspended", *, account: str = ""):
        super().__init__(message, retryable=False, status=403)
        self.account = account


@runtime_checkable
class LegitClient(Protocol):
    """The full documented action surface.

    Reads and writes are grouped because the rate limiter treats them as
    different currencies -- see `ratelimit.ActionGate`.
    """

    # ---- lifecycle ------------------------------------------------------- #
    async def close(self) -> None: ...

    # ---- reads ----------------------------------------------------------- #
    async def trending(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Trending hashtags and posts. High information density per request:
        preferred over timeline polling because reads are metered."""
        ...

    async def search(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        ...

    async def get_post(self, post_id: str) -> dict[str, Any]: ...

    async def get_comments(
        self, post_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def get_feed(self, account: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """The feed *as a given bot account sees it*. Used to measure how much of
        a target citizen's information diet we actually occupy."""
        ...

    async def get_profile(self, handle: str) -> dict[str, Any]: ...

    async def get_mentions(self, account: str, *, limit: int = 50) -> list[dict[str, Any]]: ...

    # ---- writes ---------------------------------------------------------- #
    async def post(self, account: str, text: str, **kwargs: Any) -> dict[str, Any]: ...

    async def repost(self, account: str, post_id: str) -> dict[str, Any]: ...

    async def reply(
        self,
        account: str,
        post_id: str,
        text: str,
        *,
        root_post_id: int | str | None = None,
    ) -> dict[str, Any]: ...

    async def like(self, account: str, post_id: str) -> dict[str, Any]: ...

    async def follow(self, account: str, target: str) -> dict[str, Any]: ...

    async def unfollow(self, account: str, target: str) -> dict[str, Any]: ...

    async def report(self, account: str, post_id: str, *, reason: str = "") -> dict[str, Any]: ...

