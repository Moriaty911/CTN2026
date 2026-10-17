"""The actuator: the only code allowed to hold the platform client.

Every read and every write in the fleet goes through here, which is what makes
the pacing guarantee in `ratelimit.ActionGate` real. Each call:

1.  waits on the gate (team bucket, plus the account bucket for writes),
2.  records the spend in the budget ledger,
3.  retries transient failures with backoff, and tells the gate to slow down on
    a platform throttle instead of hammering it,
4.  converts the outcome into an `ActionResult` (writes) or a safe default
    (reads), so one bad call never kills a cycle.

One failure is deliberately *not* swallowed: `AuthError` (credentials are
wrong -- fail loudly). A suspended account is recorded in `suspended` so the
loop stops scheduling it.

Dry-run mode (the config default) logs writes without sending them.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable, TypeVar

from .client.base import AccountSuspended, AuthError, LegitClient, PlatformError, Throttled
from .models import ActionKind, ActionResult, to_jsonable
from .ratelimit import ActionGate, BudgetLedger
from .telemetry import Telemetry

T = TypeVar("T")


class Actuator:
    def __init__(
        self,
        client: LegitClient,
        gate: ActionGate,
        ledger: BudgetLedger,
        *,
        telemetry: Telemetry | None = None,
        dry_run: bool = True,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._client = client
        self._gate = gate
        self._ledger = ledger
        self._telemetry = telemetry
        self.dry_run = dry_run
        self._max_retries = max(0, max_retries)
        self._backoff_s = backoff_s
        self._max_backoff_s = max_backoff_s
        self.suspended: set[str] = set()
        self.throttles = 0

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger

    def recover(self) -> None:
        """Call after a throttle-free interval so the gate climbs back toward
        its configured rate. Without this, every throttle is permanent."""
        self._gate.on_clean_interval(ActionGate.READ)
        self._gate.on_clean_interval(ActionGate.WRITE)

    # ---- reads ----------------------------------------------------------- #
    async def trending(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._read("trending", lambda: self._client.trending(limit=limit), [])

    async def search(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._read("search", lambda: self._client.search(query, limit=limit), [])

    async def get_post(self, post_id: str) -> dict[str, Any]:
        return await self._read("get_post", lambda: self._client.get_post(post_id), {})

    async def get_comments(
        self, post_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> list[dict[str, Any]]:
        async def _call() -> list[dict[str, Any]]:
            if cursor:
                try:
                    return await self._client.get_comments(post_id, limit=limit, cursor=cursor)
                except TypeError:
                    pass
            return await self._client.get_comments(post_id, limit=limit)

        return await self._read("get_comments", _call, [])

    async def get_account_posts(
        self, target: str, *, limit: int = 50, cursor: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch recent posts authored by `target` (`/accounts/{id}/posts`, 300/min bucket)."""
        getter = getattr(self._client, "get_account_posts", None)
        if getter is None:
            return []

        async def _call() -> list[dict[str, Any]]:
            if cursor:
                try:
                    return await getter(target, limit=limit, cursor=cursor)
                except TypeError:
                    pass
            return await getter(target, limit=limit)

        return await self._read("get_account_posts", _call, [])

    async def get_feed(self, account: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._read(
            "get_feed", lambda: self._client.get_feed(account, limit=limit), [], account=account
        )

    async def get_mentions(self, account: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._read(
            "get_mentions",
            lambda: self._client.get_mentions(account, limit=limit),
            [],
            account=account,
        )

    async def get_profile(self, handle: str) -> dict[str, Any]:
        return await self._read("get_profile", lambda: self._client.get_profile(handle), {})

    # ---- writes ---------------------------------------------------------- #
    async def like(self, account: str, post_id: str, *, decision_id: str = "") -> ActionResult:
        return await self._write(
            ActionKind.LIKE, account, post_id,
            lambda: self._client.like(account, post_id), decision_id,
        )

    async def repost(self, account: str, post_id: str, *, decision_id: str = "") -> ActionResult:
        return await self._write(
            ActionKind.REPOST, account, post_id,
            lambda: self._client.repost(account, post_id), decision_id,
        )

    async def reply(
        self,
        account: str,
        post_id: str,
        text: str,
        *,
        root_post_id: int | str | None = None,
        decision_id: str = "",
    ) -> ActionResult:
        async def _call() -> dict[str, Any]:
            if root_post_id is not None and str(root_post_id).strip():
                try:
                    return await self._client.reply(
                        account, post_id, text, root_post_id=root_post_id
                    )
                except TypeError:
                    pass
            return await self._client.reply(account, post_id, text)

        return await self._write(
            ActionKind.REPLY, account, post_id, _call, decision_id,
        )

    async def post(self, account: str, text: str, *, decision_id: str = "", **kwargs: Any) -> ActionResult:
        return await self._write(
            ActionKind.POST, account, "",
            lambda: self._client.post(account, text, **kwargs), decision_id,
        )

    async def follow(self, account: str, target: str, *, decision_id: str = "") -> ActionResult:
        return await self._write(
            ActionKind.FOLLOW, account, target,
            lambda: self._client.follow(account, target), decision_id,
        )

    async def unfollow(self, account: str, target: str, *, decision_id: str = "") -> ActionResult:
        return await self._write(
            ActionKind.UNFOLLOW, account, target,
            lambda: self._client.unfollow(account, target), decision_id,
        )

    async def report(
        self, account: str, post_id: str, *, reason: str = "", decision_id: str = ""
    ) -> ActionResult:
        return await self._write(
            ActionKind.REPORT, account, post_id,
            lambda: self._client.report(account, post_id, reason=reason), decision_id,
        )

    async def delete_account(self, account: str) -> dict[str, Any]:
        """Delete a suspended or retired bot account (`DELETE /accounts/{id}`) to free its active slot."""
        deleter = getattr(self._client, "delete_account", None)
        if deleter is None or self.dry_run:
            self.suspended.add(account)
            return {"ok": True, "account": account, "status": "deleted", "dry_run": self.dry_run}
        try:
            res = await self._attempt(ActionGate.WRITE, account, lambda: deleter(account))
            self.suspended.add(account)
            return res if isinstance(res, dict) else {"ok": True, "account": account, "status": "deleted"}
        except PlatformError as exc:
            self._warn(f"delete_account {account} failed: {exc}", status=exc.status)
            return {"ok": False, "account": account, "detail": str(exc)}

    # The team's vocabulary.
    comment = reply
    retweet = repost

    # ---- internals ------------------------------------------------------- #
    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_s * (2 ** attempt), self._max_backoff_s)

    async def _attempt(
        self, kind: str, account: str | None, call: Callable[[], Awaitable[T]]
    ) -> T:
        """Gate, spend, call; retry transient failures. Every attempt is a real
        request, so every attempt pays the gate and the ledger."""
        attempts = self._max_retries + 1
        last: PlatformError | None = None
        for attempt in range(attempts):
            await self._gate.acquire(kind, account)
            self._ledger.record(kind)
            try:
                return await call()
            except (AuthError, AccountSuspended):
                raise
            except Throttled as exc:
                self._gate.on_throttled(kind, account)
                self.throttles += 1
                self._warn(f"throttled on {kind}", account=account, attempt=attempt)
                last = exc
                delay = exc.retry_after_s or self._backoff(attempt)
            except PlatformError as exc:
                if not exc.retryable:
                    raise
                last = exc
                delay = self._backoff(attempt)
            if attempt + 1 < attempts:
                await asyncio.sleep(min(delay, self._max_backoff_s))
        assert last is not None
        raise last

    async def _read(
        self,
        name: str,
        call: Callable[[], Awaitable[T]],
        default: T,
        *,
        account: str | None = None,
    ) -> T:
        try:
            return await self._attempt(ActionGate.READ, account, call)
        except AuthError:
            raise
        except AccountSuspended:
            if account:
                self.suspended.add(account)
            return default
        except PlatformError as exc:
            self._warn(f"read {name} failed: {exc}", status=exc.status)
            return default

    async def _write(
        self,
        kind: ActionKind,
        account: str,
        target_id: str,
        call: Callable[[], Awaitable[dict[str, Any]]],
        decision_id: str,
    ) -> ActionResult:
        decision_id = decision_id or uuid.uuid4().hex[:12]

        def result(ok: bool, detail: str = "", latency_ms: float = 0.0,
                   response: dict[str, Any] | None = None) -> ActionResult:
            out = ActionResult(
                decision_id=decision_id, kind=kind.value, account=account,
                target_id=target_id, ok=ok, detail=detail, latency_ms=latency_ms,
                response=response or {},
            )
            if self._telemetry is not None:
                self._telemetry.actions.write(to_jsonable(out))
            return out

        if account in self.suspended:
            return result(False, "account suspended")
        if self.dry_run:
            return result(True, "dry run: not sent")
        if not self._ledger.can_write():
            return result(False, "write budget exhausted")

        start = time.monotonic()
        try:
            response = await self._attempt(ActionGate.WRITE, account, call)
        except AuthError:
            raise
        except AccountSuspended:
            self.suspended.add(account)
            self._warn("account suspended", account=account)
            return result(False, "account suspended", _ms(start))
        except Throttled:
            return result(False, "throttled", _ms(start))
        except PlatformError as exc:
            return result(False, str(exc), _ms(start))
        return result(True, "", _ms(start), response if isinstance(response, dict) else {})

    def _warn(self, msg: str, **fields: Any) -> None:
        if self._telemetry is not None:
            self._telemetry.warn(msg, **fields)


def _ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000.0, 1)
