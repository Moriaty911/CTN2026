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
        store=None,
    ) -> None:
        self.store = store if store is not None else getattr(client, "store", None)
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

    @staticmethod
    async def _paged_read(getter,*args,limit=50,cursor=None,sort=None):
        import inspect
        params=inspect.signature(getter).parameters
        kwargs={'limit':limit}
        var_kw=any(p.kind==p.VAR_KEYWORD for p in params.values())
        if cursor is not None and ('cursor' in params or var_kw):kwargs['cursor']=cursor
        if sort is not None and ('sort' in params or var_kw):kwargs['sort']=sort
        return await getter(*args,**kwargs)

    # ---- reads ----------------------------------------------------------- #
    async def trending_posts(self, *, limit=50):
        getter = getattr(self._client, 'feed_trending', None)
        if getter is not None:
            return await self._read('feed_trending', lambda: getter(limit=limit), [])
        # Mock implements the same ranked feed semantics through get_feed.
        return await self.get_feed('', limit=limit)

    async def trending(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._read("trending", lambda: self._client.trending(limit=limit), [])

    async def search(self, query: str, *, limit: int = 50, cursor: str | None = None, sort: str | None = None) -> list[dict[str, Any]]:
        return await self._read("search", lambda: self._paged_read(self._client.search,query,limit=limit,cursor=cursor,sort=sort), [])

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

    async def get_counters(self, post_id: str) -> dict[str, Any]:
        """Like/reply/repost/view counts for one post (`/posts/{id}/counters`, 300/min route)."""
        getter = getattr(self._client, "get_counters", None)
        if getter is None:
            return {}
        return await self._read("get_counters", lambda: getter(post_id), {})

    async def get_account_likes(
        self, target: str, *, limit: int = 50, cursor: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch posts liked by `target` (`/accounts/{id}/likes`); used only to count actions."""
        getter = getattr(self._client, "get_account_likes", None)
        if getter is None:
            return []
        return await self._read("get_account_likes", lambda: self._paged_read(getter, target, limit=limit, cursor=cursor), [])

    async def get_feed(self, account: str, *, limit: int = 50, cursor: str | None = None) -> list[dict[str, Any]]:
        return await self._read(
            "get_feed", lambda: self._paged_read(self._client.get_feed,account,limit=limit,cursor=cursor), [], account=account
        )

    async def get_mentions(self, account: str, *, limit: int = 50, cursor: str | None = None) -> list[dict[str, Any]]:
        return await self._read(
            "get_mentions",
            lambda: self._paged_read(self._client.get_mentions,account,limit=limit,cursor=cursor),
            [],
            account=account,
        )

    async def get_profile(self, handle: str) -> dict[str, Any]:
        return await self._read("get_profile", lambda: self._client.get_profile(handle), {})

    # ---- writes ---------------------------------------------------------- #
    async def like(self, account: str, post_id: str, *, decision_id: str = "", context=None) -> ActionResult:
        return await self._write(
            ActionKind.LIKE, account, post_id,
            lambda: self._client.like(account, post_id), decision_id,metadata=context,
        )

    async def repost(self, account: str, post_id: str, *, decision_id: str = "", context=None) -> ActionResult:
        return await self._write(
            ActionKind.REPOST, account, post_id,
            lambda: self._client.repost(account, post_id), decision_id,metadata=context,
        )

    async def reply(
        self,
        account: str,
        post_id: str,
        text: str,
        *,
        root_post_id: int | str | None = None,
        decision_id: str = "",
        context: dict | None = None,
    ) -> ActionResult:
        async def _call() -> dict[str, Any]:
            if root_post_id is not None and str(root_post_id).strip():
                import inspect
                if 'root_post_id' in inspect.signature(self._client.reply).parameters:
                    return await self._client.reply(
                        account, post_id, text, root_post_id=root_post_id
                    )
            return await self._client.reply(account, post_id, text)

        return await self._write(
            ActionKind.REPLY, account, post_id, _call, decision_id, text=text,
            metadata={**(context or {}),'root_post_id':str(root_post_id or post_id)},
        )

    async def post(self, account: str, text: str, *, decision_id: str = "", context=None, **kwargs: Any) -> ActionResult:
        return await self._write(
            ActionKind.POST, account, "",
            lambda: self._client.post(account, text, **kwargs), decision_id, text=text, metadata=context,
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

    async def report(self, account, post_id, *, reason="inauthentic", detail="", decision_id="", context=None):
        return await self._write(ActionKind.REPORT, account, post_id,
            lambda: self._client.report(account, post_id, reason=reason, detail=detail), decision_id,
            metadata={**(context or {}),'detail':detail,'reason':reason,
                'target_author':(context or {}).get('target_author') or ((self.store.get_observed_post(post_id) or {}).get('author','') if self.store else '')})

    async def report_account(self, account, target, *, detail, decision_id="", context=None):
        return await self._write(ActionKind.REPORT, account, str(target),
            lambda: self._client.report_account(account,target,reason='inauthentic',detail=detail),decision_id,
            metadata={**(context or {}),'subject_kind':'account','target_author':str(target),'detail':detail})

    async def delete_account(self, account: str) -> dict[str, Any]:
        """Delete a suspended or retired bot account (`DELETE /accounts/{id}`) to free its active slot."""
        deleter = getattr(self._client, "delete_account", None)
        if deleter is None:
            return {"ok":False,"status":"blocked","detail":"delete capability unavailable"}
        res=await self._write(ActionKind.DELETE_ACCOUNT,account,account,lambda:deleter(account),'')
        if res.ok:
            self.suspended.add(account)
        return {"ok":res.ok,"status":res.status,"action_id":res.action_id,"detail":res.detail}

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
            if not getattr(self._client, 'http_metered', False):
                await self._gate.acquire(kind, account)
                if kind==ActionGate.WRITE and self.store:
                    from .client.pacing import CURRENT_ACTION
                    ident=CURRENT_ACTION.get()
                    if ident:
                        ready,reason=await asyncio.to_thread(self.store.guard_wire_action,ident)
                        if not ready: raise PlatformError(reason,retryable=False,status=409)
                if not (self._ledger.store and self._client.__class__.__name__=="MockLegitClient"):
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
                if not exc.retryable or kind == ActionGate.WRITE:
                    raise
                last = exc
                delay = self._backoff(attempt)
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
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
                if self.store:
                    await asyncio.to_thread(self.store.set_account_banned,account,True)
            if name in ('get_comments','get_feed','get_mentions','search','get_account_posts'):
                from .client.base import ReadFailurePage
                return ReadFailurePage()
            return default
        except PlatformError as exc:
            self._warn(f"read {name} failed: {exc}", status=exc.status)
            if name in ('get_comments','get_feed','get_mentions','search','get_account_posts'):
                from .client.base import ReadFailurePage
                return ReadFailurePage()
            return default

    async def _write(
        self,
        kind: ActionKind,
        account: str,
        target_id: str,
        call: Callable[[], Awaitable[dict[str, Any]]],
        decision_id: str,
        *, text: str = "", metadata: dict | None = None,
    ) -> ActionResult:
        decision_id = decision_id or uuid.uuid4().hex[:12]

        ident = ''
        meta = dict(metadata or {})
        if getattr(self,'catalog_hash',''):
            meta['process_catalog_hash']=self.catalog_hash
        action_kind = 'report_account' if kind is ActionKind.REPORT and meta.get('subject_kind') == 'account' else kind.value

        def result(status, detail='', latency_ms=0.0, response=None):
            out = ActionResult(decision_id,kind.value,account,target_id,status=='succeeded',
                detail,latency_ms,response or {},status=status,action_id=ident)
            if self._telemetry is not None:
                self._telemetry.actions.write(to_jsonable(out))
            return out

        if account in self.suspended and kind is not ActionKind.DELETE_ACCOUNT:
            return result('blocked','account suspended')
        if self.dry_run:
            if self.store:
                await asyncio.to_thread(self.store.log_simulation,action_kind,account,target_id,{'text':text})
            return result('simulated','dry run: not sent')
        if not self._ledger.can_write():
            return result('blocked','write budget exhausted')
        if self.store:
            ident, reason = await asyncio.to_thread(self.store.claim_action,action_kind,account,target_id,text,metadata=meta)
            if not ident:
                return result('blocked',reason)
            await asyncio.to_thread(lambda:self.store.start_action(ident,wire_tracked=bool(getattr(self._client,'http_metered',False))))
        async def renew():
            while True:
                await asyncio.sleep(20)
                await asyncio.to_thread(self.store.renew_action,ident)
        renewal=asyncio.create_task(renew()) if self.store else None
        from .client.pacing import CURRENT_ACTION
        token=CURRENT_ACTION.set(ident or None)
        start=time.monotonic()
        status, detail, response = 'succeeded','',{}
        try:
            response=await self._attempt(ActionGate.WRITE,account,call)
        except AuthError:
            if self.store:
                await asyncio.to_thread(self.store.finish_action,ident,'failed',detail='authentication failed')
                await asyncio.to_thread(self.store.set_account_frozen,account,True)
            raise
        except AccountSuspended:
            self.suspended.add(account)
            if self.store:
                await asyncio.to_thread(self.store.set_account_banned,account,True)
            status,detail='failed','account suspended'
        except Throttled:
            status,detail='failed','throttled'
            response={'error_status':429,'failure_category':'transient'}
        except PlatformError as exc:
            status='unknown' if exc.retryable or exc.status is None else 'failed'
            detail=str(exc)
            if kind is ActionKind.REPORT and exc.status==403 and self.store:
                # A denial pauses automation and clears manual availability; suspension wording
                # also starts a local six-hour stop. It never marks the reporting account suspended.
                from .moderation.decision import record_report_forbidden
                await asyncio.to_thread(record_report_forbidden,self.store,str(exc))
            response={'error_status':exc.status,'failure_category':'uncertain' if status=='unknown' else 'terminal' if exc.status in (400,404) else 'transient'}
        except (TimeoutError,OSError):
            status,detail='unknown','network response unavailable'
        except asyncio.CancelledError:
            if self.store:
                await asyncio.shield(asyncio.to_thread(self.store.finish_action,ident,'unknown',detail='cancelled during send'))
            raise
        except Exception:
            status,detail='unknown','unexpected send interruption'
        finally:
            CURRENT_ACTION.reset(token)
            if renewal:
                renewal.cancel()
                await asyncio.gather(renewal,return_exceptions=True)
        response=response if isinstance(response,dict) else {}
        if status=='succeeded' and kind in (ActionKind.POST,ActionKind.REPLY):
            post_id=response.get('post_id') or response.get('id')
            if post_id:response={**response,'post_id':str(post_id)}
            else:status,detail='unknown','publication confirmation missing post_id'
        if self.store:
            try:
                await asyncio.to_thread(self.store.finish_action,ident,status,response=response,detail=detail)
            except ValueError:
                status,detail='unknown','publication confirmation conflicts with saved attribution'
                await asyncio.to_thread(self.store.finish_action,ident,status,response=response,detail=detail)
            if status=='unknown':
                # The store turns an "unknown" that never reached the wire into a definite failure.
                row=await asyncio.to_thread(self.store.get_action,ident)
                if row and row['status']=='failed':status,detail='failed',row['detail']
        return result(status,detail,_ms(start),response)

    def _warn(self, msg: str, **fields: Any) -> None:
        if self._telemetry is not None:
            self._telemetry.warn(msg, **fields)


def _ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000.0, 1)
