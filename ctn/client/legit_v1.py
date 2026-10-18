"""Official Capture the Narrative (Legit Social v1) adapter implementing `LegitClient`.

Bridges the official `capturethenarrative` package (`LegitSocial` and `Account`)
to our async `LegitClient` Protocol:

1. Runs synchronous `urllib.request` calls from `capturethenarrative` in a worker
   thread via `asyncio.to_thread` with `max_retries=0` on the inner client so
   HTTP 429s surface immediately as `Throttled(retry_after_s=...)` for `Actuator`
   and `ActionGate` to pace asynchronously.
2. Normalizes official wire payloads (`body` -> `text`, integer `post_id` /
   `author_id`, ISO-8601 `created_at` -> float epoch, `reply_to_post_id` /
   `root_post_id` -> `parent_id`, `Page.next_cursor` -> `NormalizedPage.next_cursor`).
3. Manages signed-in `Account` sessions (`account_from_token`, lazy `login`,
   optional `register`, and automatic 401 token refresh when passwords are known).
4. Maintains a bidirectional `username <-> account_id` cache so handle-based
   calls (`follow`, `unfollow`, `get_profile`, `account_posts`, `report_account`)
   seamlessly resolve integer `account_id`s required by Legit Social routes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import time
import urllib.error
from typing import Any, Callable, Iterable, Mapping

from .base import AccountSuspended, AuthError, LegitClient, PlatformError, Throttled

from capturethenarrative import (  # noqa: E402
    APIError,
    Account,
    DEFAULT_LEGIT_SOCIAL_URL,
    LegitSocial,
    POST_REPORT_REASONS,
    Page,
    RateLimit,
    RateLimited,
)

_HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")


class NormalizedPage(list):
    """A `list` of normalized dicts that preserves the server's `next_cursor`."""

    def __init__(self, items: Iterable[dict[str, Any]] = (), next_cursor: str | None = None) -> None:
        super().__init__(items)
        self.next_cursor: str | None = next_cursor


def parse_timestamp(value: Any) -> float:
    """Convert an epoch number or ISO-8601 timestamp string into float seconds."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def extract_hashtags(body: str, explicit_tags: Any = None) -> list[str]:
    """Extract `#tag` strings from body and merge any explicit tag objects/strings."""
    seen: list[str] = []
    seen_lower: set[str] = set()

    def _add(tag_str: str) -> None:
        clean = tag_str.strip()
        if not clean:
            return
        if not clean.startswith("#"):
            clean = "#" + clean
        low = clean.lower()
        if low not in seen_lower:
            seen_lower.add(low)
            seen.append(clean)

    for match in _HASHTAG_RE.finditer(body or ""):
        _add("#" + match.group(1))

    if isinstance(explicit_tags, (list, tuple)):
        for item in explicit_tags:
            if isinstance(item, str):
                _add(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("hashtag") or item.get("tag")
                if name:
                    _add(str(name))
    return seen


def _coerce_int_id(value: int | str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise PlatformError(
            f"expected integer ID for official Legit Social route, got {value!r}",
            retryable=False,
            status=400,
        ) from exc


class OfficialLegitClient(LegitClient):
    """Async adapter around `capturethenarrative.LegitSocial` and `Account`."""

    def __init__(
        self,
        url: str | None = None,
        *,
        team_key: str = "",
        default_password: str = "",
        timeout: float = 20.0,
        credentials: Mapping[str, str] | None = None,
        tokens: Mapping[str, str] | None = None,
        accounts: Mapping[str, Account] | None = None,
        store: Any | None = None,
        feed_mode: str = "latest",
        auto_register: bool = False,
        on_rate_limit: Callable[[RateLimit], None] | None = None,
        on_token_refreshed: Callable[[str, Account], None] | None = None,
        social: LegitSocial | None = None,
    ) -> None:
        self.api_url = (url or DEFAULT_LEGIT_SOCIAL_URL).rstrip("/")
        self.team_key = team_key.strip()
        self.default_password = default_password.strip()
        self.timeout = timeout
        self.store = store
        self.feed_mode = feed_mode if feed_mode in {"latest", "trending", "home"} else "latest"
        self.auto_register = auto_register
        self._user_on_rate_limit = on_rate_limit
        self._on_token_refreshed = on_token_refreshed
        self.last_rate_limit: RateLimit | None = None

        self.http_metered = False
        # Inner LegitSocial uses max_retries=0 so 429s immediately become Throttled
        # exceptions handled asynchronously by Actuator + ActionGate.
        self.social: LegitSocial = social or LegitSocial(
            self.api_url,
            timeout=self.timeout,
            max_retries=0,
            on_rate_limit=self._handle_rate_limit,
        )
        if social is not None and social.on_rate_limit is None:
            social.on_rate_limit = self._handle_rate_limit

        self._pacer = None
        if store is not None and social is None:
            from .pacing import HttpPacer
            from ..config import RateConfig, BudgetConfig
            self._pacer = HttpPacer(store, RateConfig(), BudgetConfig())
            self._pacer.attach(self.social)
            self.http_metered = True

        self._passwords: dict[str, str] = {}
        self._tokens: dict[str, str] = {}
        self._accounts: dict[str, Account] = dict(accounts or {})

        # Bidirectional caches for username <-> integer account_id and tag_name <-> integer tag_id
        self._id_to_username: dict[int, str] = {}
        self._username_to_id: dict[str, int] = {}
        self._verified_usernames: set[str] = set()
        self._tag_ids: dict[str, int] = {}

        # Load persisted account sessions (username, password, token, platform_account_id) from SQLite
        if self.store is not None and hasattr(self.store, "list_managed_accounts"):
            for row in self.store.list_managed_accounts():
                uname = str(row.get("username") or "").strip()
                if not uname:
                    continue
                pid = int(row.get("platform_account_id") or 0)
                if pid > 0:
                    self._id_to_username[pid] = uname
                    self._username_to_id[uname.lower()] = pid
                if str(row.get("status") or "active") == "deleted":
                    continue
                if row.get("password"):
                    self._passwords[uname] = str(row["password"])
                if row.get("token"):
                    self._tokens[uname] = str(row["token"])

        # Explicit constructor credentials/tokens override persisted values
        for u, p in (credentials or {}).items():
            uname = str(u).strip()
            if uname and p:
                self._passwords[uname] = str(p)
                if self.store is not None and hasattr(self.store, "upsert_managed_account"):
                    self.store.upsert_managed_account(uname, password=str(p))

        for u, t in (tokens or {}).items():
            uname = str(u).strip()
            tok = str(t).strip()
            if uname and tok:
                self._tokens[uname] = tok
                if self.store is not None and hasattr(self.store, "upsert_managed_account"):
                    self.store.upsert_managed_account(uname, token=tok)

        for username, acct in self._accounts.items():
            if self._pacer is not None:
                acct.max_retries=0
                self._pacer.attach(acct)
            self._cache_account_dict(acct.profile, fallback_username=username)

    def configure_pacing(self, rate, budget):
        if self.http_metered:
            self._pacer.rate, self._pacer.budget = rate, budget

    def _handle_rate_limit(self, limit: RateLimit) -> None:
        self.last_rate_limit = limit
        if self._user_on_rate_limit is not None:
            self._user_on_rate_limit(limit)

    # ---- Caches & Normalization ------------------------------------------ #
    def _cache_account_dict(
        self, raw: Mapping[str, Any] | None, *, fallback_username: str = ""
    ) -> str:
        if not isinstance(raw, Mapping):
            return fallback_username
        raw_id = raw.get("account_id") if "account_id" in raw else raw.get("id")
        username = str(raw.get("username") or raw.get("handle") or fallback_username or "").strip()
        if raw_id is not None:
            try:
                aid = int(raw_id)
                if username:
                    self._id_to_username[aid] = username
                    self._username_to_id[username.lower()] = aid
                elif aid in self._id_to_username:
                    username = self._id_to_username[aid]
            except (TypeError, ValueError):
                pass
        if raw.get("verified") and username:
            self._verified_usernames.add(username)
        return username

    def _cache_tag_dict(self, raw: Mapping[str, Any] | None) -> None:
        if not isinstance(raw, Mapping):
            return
        raw_id = raw.get("tag_id") if "tag_id" in raw else raw.get("id")
        name = str(raw.get("name") or raw.get("hashtag") or "").strip().lstrip("#").lower()
        if raw_id is not None and name:
            try:
                self._tag_ids[name] = int(raw_id)
            except (TypeError, ValueError):
                pass

    def normalize_post(
        self, raw: Mapping[str, Any], *, default_parent_id: str = ""
    ) -> dict[str, Any]:
        """Convert an official Legit Social post dict into our unified dict schema."""
        if not isinstance(raw, Mapping):
            return {}
        post_id_val = raw.get("post_id") if "post_id" in raw else raw.get("id")
        post_id = str(post_id_val).strip() if post_id_val is not None else ""

        # Resolve author handle and integer author_id
        author_obj = raw.get("author")
        author_id_val = raw.get("author_id")
        username = ""
        if isinstance(author_obj, Mapping):
            username = self._cache_account_dict(author_obj)
            if author_id_val is None:
                author_id_val = author_obj.get("account_id") or author_obj.get("id")
        elif isinstance(author_obj, str) and author_obj.strip():
            username = author_obj.strip()

        if not username:
            username = str(raw.get("username") or raw.get("author_username") or "").strip()

        author_id: int | None = None
        if author_id_val is not None:
            try:
                author_id = int(author_id_val)
            except (TypeError, ValueError):
                author_id = None

        if author_id is not None:
            if username:
                self._id_to_username[author_id] = username
                self._username_to_id[username.lower()] = author_id
            else:
                username = self._id_to_username.get(author_id, str(author_id))

        body = str(raw.get("body") if raw.get("body") is not None else (raw.get("text") or ""))
        counters = raw.get("counters") if isinstance(raw.get("counters"), Mapping) else {}

        def _count(*keys: str) -> int:
            for k in keys:
                val = raw.get(k)
                if val is None and counters:
                    val = counters.get(k)
                if val is not None:
                    try:
                        return int(val)
                    except (TypeError, ValueError):
                        pass
            return 0

        def _present(name):
            alias='reply_count' if name=='replies' else name[:-1]+'_count'
            for key in (name,alias):
                value=raw.get(key) if raw.get(key) is not None else counters.get(key)
                if type(value) is int and value>=0:return True
                if type(value) is str and value.isascii() and value.isdecimal():return True
            return False

        reply_to = raw.get("reply_to_post_id")
        root_post = raw.get("root_post_id")
        parent_id = str(reply_to if reply_to is not None else (raw.get('parent_id') or default_parent_id or root_post or '')).strip()

        hashtags = extract_hashtags(body, raw.get("tags") or raw.get("hashtags"))
        if isinstance(raw.get("tags"), list):
            for t in raw["tags"]:
                self._cache_tag_dict(t)

        is_verified = (
            (isinstance(author_obj, Mapping) and bool(author_obj.get("verified")))
            or bool(raw.get("verified"))
            or (bool(username) and username in self._verified_usernames)
        )
        platform_level = str(
            raw.get("platform_level")
            or raw.get("author_level")
            or ("verified" if is_verified else "unknown")
        ).strip()

        out: dict[str, Any] = {
            **dict(raw),
            "post_id": post_id,
            "author": username,
            "author_id": author_id,
            "text": body,
            "body": body,
            "created_at": parse_timestamp(raw.get("created_at")),
            "likes": _count("likes", "like_count"),
            "reposts": _count("reposts", "repost_count"),
            "replies": _count("replies", "reply_count"),
            "views": _count("views", "view_count"),
            "hashtags": hashtags,
            "kind": str(raw.get("kind") or ("reply" if parent_id else "post")),
            "platform_level": platform_level,
            "_metric_presence": [name for name in ('likes','reposts','replies','views') if _present(name)],
        }
        if parent_id:
            out["parent_id"] = parent_id
        if reply_to is not None:
            out["reply_to_post_id"] = str(reply_to)
        if root_post is not None:
            out["root_post_id"] = str(root_post)
        return out

    def _normalize_page(
        self, page: Any, *, default_parent_id: str = ""
    ) -> NormalizedPage:
        next_cursor = getattr(page, "next_cursor", None)
        items = [
            self.normalize_post(item, default_parent_id=default_parent_id)
            for item in (page or [])
            if isinstance(item, Mapping)
        ]
        return NormalizedPage(items, next_cursor=next_cursor)

    # ---- Error & Async Call Wrapper -------------------------------------- #
    def _translate_error(self, exc: Exception, *, account: str | None = None) -> PlatformError:
        if isinstance(exc, PlatformError):
            return exc
        if isinstance(exc, RateLimited):
            return Throttled(exc.message, retry_after_s=float(exc.retry_after))
        if isinstance(exc, APIError):
            msg_low = exc.message.lower()
            if exc.status == 401:
                return AuthError(exc.message)
            if exc.status == 403:
                reporting_denial="report" in msg_low and any(word in msg_low for word in ("suspend","ban","disabled","not allowed"))
                if reporting_denial:
                    return PlatformError(exc.message,retryable=False,status=403)
                if re.search(r"\baccount\s+(?:(?:is|has been)\s+)?suspended\b",msg_low):
                    if (
                        account
                        and self.store is not None
                        and hasattr(self.store, "mark_managed_account_status")
                    ):
                        self.store.mark_managed_account_status(account, "suspended")
                    return AccountSuspended(exc.message, account=account or "")
                return PlatformError(exc.message, retryable=False, status=403)
            body_retryable = isinstance(exc.json, Mapping) and bool(exc.json.get("retryable"))
            retryable = body_retryable or (
                exc.status in {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
            )
            if exc.status == 429:
                return Throttled(exc.message)
            return PlatformError(exc.message, retryable=retryable, status=exc.status)
        if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
            return PlatformError(f"network error: {exc}", retryable=True, status=None)
        return PlatformError(str(exc), retryable=False, status=None)

    async def _call(self, fn: Callable[[], Any], *, account: str | None = None) -> Any:
        try:
            return await asyncio.to_thread(fn)
        except Exception as exc:
            if (
                isinstance(exc, APIError)
                and exc.status == 401
                and account
                and account in self._passwords
            ):
                # Token expired; re-login once with stored password and retry
                await self._relogin(account)
                try:
                    return await asyncio.to_thread(fn)
                except Exception as retry_exc:
                    raise self._translate_error(retry_exc, account=account) from None
            raise self._translate_error(exc, account=account) from None

    # ---- Account Session Management -------------------------------------- #
    def bind_account(self, username: str, account_obj: Account, *, password: str = "") -> Account:
        """Register an already-instantiated `capturethenarrative.Account` and persist session if `store` is attached."""
        username = username.strip()
        if self._pacer is not None:
            account_obj.max_retries=0
            self._pacer.attach(account_obj)
        self._accounts[username] = account_obj
        if account_obj.token:
            self._tokens[username] = account_obj.token
        if password:
            self._passwords[username] = password
        self._cache_account_dict(account_obj.profile, fallback_username=username)
        if self.store is not None and hasattr(self.store, "upsert_managed_account"):
            self.store.upsert_managed_account(
                username,
                platform_account_id=account_obj.account_id,
                password=password if password else None,
                display_name=account_obj.display_name,
                token=account_obj.token,
                expires_at=account_obj.expires_at,
            )
        return account_obj

    def set_credentials(
        self, username: str, *, password: str = "", token: str = "", account_id: int | None = None
    ) -> None:
        """Store credentials/token for an account so it can be resumed or logged in lazily."""
        username = username.strip()
        if password:
            self._passwords[username] = password
        if token:
            self._tokens[username] = token
        if account_id is not None:
            self._id_to_username[int(account_id)] = username
            self._username_to_id[username.lower()] = int(account_id)
        if self.store is not None and hasattr(self.store, "upsert_managed_account"):
            self.store.upsert_managed_account(
                username,
                platform_account_id=account_id,
                password=password if password else None,
                token=token if token else None,
            )

    async def _relogin(self, username: str) -> Account:
        password = self._passwords.get(username, "")
        if not password:
            raise AuthError(f"no password configured to refresh expired token for {username!r}")
        try:
            acct = await asyncio.to_thread(self.social.login, username, password)
        except Exception as exc:
            raise self._translate_error(exc, account=username) from None
        self.bind_account(username, acct, password=password)
        if self._on_token_refreshed is not None:
            self._on_token_refreshed(username, acct)
        return acct

    async def get_or_login_account(self, username: str) -> Account:
        """Return a signed-in `Account` for `username`, resuming from token or logging in."""
        username = username.strip()
        existing = self._accounts.get(username)
        if existing is not None:
            return existing

        # Synchronize from SQLite in case another process (e.g. ctn.macro) registered/refreshed the token
        if (
            username not in self._tokens
            and self.store is not None
            and hasattr(self.store, "get_managed_account")
        ):
            row = self.store.get_managed_account(username)
            if isinstance(row, Mapping) and str(row.get("status") or "active") != "deleted":
                if row.get("token"):
                    self._tokens[username] = str(row["token"])
                if row.get("password"):
                    self._passwords[username] = str(row["password"])

        password = (
            self._passwords.get(username)
            or self.default_password
            or (f"ctn2026_{username}_{self.team_key[:12]}" if self.auto_register and self.team_key else "")
        )
        if password:
            self._passwords[username] = password

        saved_token = self._tokens.get(username)
        if saved_token:
            try:
                acct = await asyncio.to_thread(self.social.account_from_token, saved_token)
                return self.bind_account(username, acct, password=password)
            except APIError as exc:
                if exc.status != 401 or not password:
                    raise self._translate_error(exc, account=username) from None

        if password:
            try:
                acct = await asyncio.to_thread(self.social.login, username, password)
                self.bind_account(username, acct, password=password)
                if self._on_token_refreshed is not None:
                    self._on_token_refreshed(username, acct)
                return acct
            except APIError as exc:
                if exc.status in {401, 404} and self.auto_register and self.team_key:
                    acct = await asyncio.to_thread(
                        self.social.register, username, password, self.team_key
                    )
                    self.bind_account(username, acct, password=password)
                    if self._on_token_refreshed is not None:
                        self._on_token_refreshed(username, acct)
                    return acct
                raise self._translate_error(exc, account=username) from None

        raise AuthError(f"account {username!r} is not logged in and has no saved token or password")

    async def _call_as(self, username: str, fn: Callable[[Account], Any]) -> Any:
        """Execute `fn(account_obj)` with automatic 401 token refresh."""
        acct = await self.get_or_login_account(username)
        try:
            return await asyncio.to_thread(fn, acct)
        except Exception as exc:
            if (
                isinstance(exc, APIError)
                and exc.status == 401
                and username in self._passwords
            ):
                acct = await self._relogin(username)
                try:
                    return await asyncio.to_thread(fn, acct)
                except Exception as retry_exc:
                    raise self._translate_error(retry_exc, account=username) from None
            raise self._translate_error(exc, account=username) from None

    async def _resolve_account_id(self, target: int | str) -> int:
        """Resolve a username or numeric ID string to an integer `account_id`."""
        if isinstance(target, int):
            return target
        clean = str(target).strip().lstrip("@")
        if not clean:
            raise PlatformError("empty account target", retryable=False, status=400)
        if clean.isdigit():
            return int(clean)
        cached = self._username_to_id.get(clean.lower())
        if cached is not None:
            return cached

        if self.store is not None and hasattr(self.store, "resolve_platform_account_id"):
            stored_id = self.store.resolve_platform_account_id(clean)
            if stored_id is not None and stored_id > 0:
                self._id_to_username[stored_id] = clean
                self._username_to_id[clean.lower()] = stored_id
                return stored_id

        page = await self._call(lambda: self.social.accounts(q=clean, limit=20))
        for item in page or []:
            if isinstance(item, Mapping):
                uname = self._cache_account_dict(item)
                if uname.lower() == clean.lower():
                    return int(item.get("account_id") or item.get("id"))
        raise PlatformError(f"account {target!r} not found", retryable=False, status=404)

    async def _resolve_tag_id(self, tag_query: str) -> int | None:
        clean = tag_query.strip().lstrip("#").lower()
        if not clean:
            return None
        if clean in self._tag_ids:
            return self._tag_ids[clean]

        # Check popular_tags first (cheap 1-page read in 300/min bucket)
        pop = await self._call(self.social.popular_tags)
        for item in pop or []:
            self._cache_tag_dict(item)
        if clean in self._tag_ids:
            return self._tag_ids[clean]

        # Fallback: scan first page of /tags
        tags_page = await self._call(lambda: self.social.tags(limit=50))
        for item in tags_page or []:
            self._cache_tag_dict(item)
        return self._tag_ids.get(clean)

    # ---- LegitClient Protocol: lifecycle --------------------------------- #
    async def close(self) -> None:
        if self._pacer is not None: self._pacer.close()

    # ---- LegitClient Protocol: reads ------------------------------------- #
    async def trending(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch popular tags of the last 24h (`/tags/popular`, 300/min read bucket)."""
        page = await self._call(self.social.popular_tags)
        out: list[dict[str, Any]] = []
        for item in (page or [])[:limit]:
            if not isinstance(item, Mapping):
                continue
            self._cache_tag_dict(item)
            name = str(item.get("name") or item.get("hashtag") or "").strip()
            if not name:
                continue
            hashtag = name if name.startswith("#") else f"#{name}"
            vol = item.get("post_count") if "post_count" in item else item.get("volume", 0)
            out.append(
                {
                    "hashtag": hashtag,
                    "volume": int(vol or 0),
                    "tag_id": item.get("tag_id") or item.get("id"),
                    "raw": dict(item),
                }
            )
        return out

    async def search(
        self,
        query: str,
        *,
        limit: int = 50,
        sort: str | None = None,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search posts by tag (`/tags/{tag_id}/posts`, 10/min bucket) or fallback to feed filter."""
        tag_id = await self._resolve_tag_id(query)
        if tag_id is not None:
            page = await self._call(
                lambda: self.social.tag_posts(tag_id, limit=limit, cursor=cursor, sort=sort)
            )
            return self._normalize_page(page)

        # Fallback if query is a free-text keyword not matching any tag
        q_low = query.strip().lower().lstrip("#")
        feed_page = await self.feed_latest(limit=max(limit, 50), cursor=cursor)
        matched = [
            p
            for p in feed_page
            if q_low in p.get("text", "").lower()
            or any(q_low in h.lower() for h in p.get("hashtags", []))
        ]
        return NormalizedPage(matched[:limit], next_cursor=feed_page.next_cursor)

    async def get_post(self, post_id: str) -> dict[str, Any]:
        pid = _coerce_int_id(post_id)
        raw = await self._call(lambda: self.social.get_post(pid))
        return self.normalize_post(raw) if isinstance(raw, Mapping) else {}

    async def get_comments(
        self, post_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> NormalizedPage:
        """Fetch one page of replies for `post_id` (`/posts/{id}/replies`, 300/min bucket)."""
        pid = _coerce_int_id(post_id)
        page = await self._call(lambda: self.social.replies(pid, limit=limit, cursor=cursor))
        return self._normalize_page(page, default_parent_id=str(post_id))

    async def get_feed(
        self,
        account: str = "",
        *,
        limit: int = 50,
        mode: str | None = None,
        cursor: str | None = None,
    ) -> NormalizedPage:
        """Fetch feed posts (`/posts?sort=...`, 10/min shared feed bucket).

        `mode` selects `"latest"`, `"trending"`, or `"home"` (defaults to `self.feed_mode`).
        """
        chosen_mode = mode or self.feed_mode
        if chosen_mode == "home" and account:
            page = await self._call_as(
                account, lambda acct: acct.feed_home(limit=limit, cursor=cursor)
            )
            return self._normalize_page(page)
        if chosen_mode == "trending":
            return await self.feed_trending(limit=limit, cursor=cursor)
        return await self.feed_latest(limit=limit, cursor=cursor)

    async def feed_latest(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> NormalizedPage:
        page = await self._call(lambda: self.social.feed_latest(limit=limit, cursor=cursor))
        return self._normalize_page(page)

    async def feed_trending(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> NormalizedPage:
        page = await self._call(lambda: self.social.feed_trending(limit=limit, cursor=cursor))
        return self._normalize_page(page)

    async def get_profile(self, handle: str) -> dict[str, Any]:
        aid = await self._resolve_account_id(handle)
        raw = await self._call(lambda: self.social.get_account(aid))
        if not isinstance(raw, Mapping):
            return {}
        username = self._cache_account_dict(raw, fallback_username=str(handle))
        verified = bool(raw.get("verified"))
        return {
            **dict(raw),
            "account_id": aid,
            "handle": username,
            "username": username,
            "display_name": raw.get("display_name") or username,
            "verified": verified,
            "kind": "npc" if verified else str(raw.get("kind") or "player"),
        }

    async def get_mentions(
        self,
        account: str,
        *,
        limit: int = 50,
        unresolved: bool = False,
        cursor: str | None = None,
    ) -> NormalizedPage:
        """Fetch notifications (replies and mentions) for `account` (`/notifications`, 300/min bucket)."""
        page = await self._call_as(
            account,
            lambda acct: acct.notifications(unresolved=unresolved, limit=limit, cursor=cursor),
        )
        items: list[dict[str, Any]] = []
        for entry in page or []:
            if not isinstance(entry, Mapping):
                continue
            actor = entry.get("actor") if isinstance(entry.get("actor"), Mapping) else {}
            actor_username = self._cache_account_dict(actor)
            post_dict = entry.get("post") if isinstance(entry.get("post"), Mapping) else entry
            merged = dict(post_dict)
            if actor_username and not merged.get("username"):
                merged["username"] = actor_username
            if actor.get("account_id") is not None and merged.get("author_id") is None:
                merged["author_id"] = actor.get("account_id")
            norm = self.normalize_post(merged)
            norm["notification_id"] = entry.get("notification_id")
            norm["notification_kind"] = entry.get("kind")
            items.append(norm)
        return NormalizedPage(items, next_cursor=getattr(page, "next_cursor", None))

    # ---- Extra Official Legit Social Reads (for Stage 3+ crawlers) ------- #
    async def list_verified_accounts(self, *, limit: int = 50, max_items: int = 500) -> list[dict[str, Any]]:
        """Fetch all verified accounts (`/accounts?verified=true`, 300/min bucket)."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(out) < max_items:
            batch_limit = min(limit, max_items - len(out))
            page = await self._call(
                lambda c=cursor, bl=batch_limit: self.social.accounts(
                    verified=True, limit=bl, cursor=c
                )
            )
            for item in page or []:
                if isinstance(item, Mapping):
                    uname = self._cache_account_dict(item)
                    out.append({**dict(item), "username": uname, "handle": uname})
            if not getattr(page, "next_cursor", None) or not page:
                break
            cursor = page.next_cursor
        return out

    async def sync_verified_accounts(
        self, *, limit: int = 50, max_items: int = 500
    ) -> list[dict[str, Any]]:
        """Fetch all verified accounts from `/accounts?verified=true` and register them as
        `verified_npc` in the attached SQLite `StateStore`.
        """
        verified = await self.list_verified_accounts(limit=limit, max_items=max_items)
        if self.store is not None and hasattr(self.store, "register_verified_accounts"):
            self.store.register_verified_accounts(verified, status="verified_npc")
        return verified

    async def get_account_posts(
        self, target: int | str, *, limit: int = 50, cursor: str | None = None
    ) -> NormalizedPage:
        """Fetch posts authored by `target` (`/accounts/{id}/posts`, 300/min bucket)."""
        aid = await self._resolve_account_id(target)
        page = await self._call(
            lambda: self.social.account_posts(aid, limit=limit, cursor=cursor)
        )
        return self._normalize_page(page)

    async def get_counters(self, post_id: str) -> dict[str, int]:
        pid = _coerce_int_id(post_id)
        raw = await self._call(lambda: self.social.counters(pid))
        if not isinstance(raw, Mapping):
            return {"likes": 0, "replies": 0, "reposts": 0, "views": 0}
        return {
            "likes": int(raw.get("likes") or raw.get("like_count") or 0),
            "replies": int(raw.get("replies") or raw.get("reply_count") or 0),
            "reposts": int(raw.get("reposts") or raw.get("repost_count") or 0),
            "views": int(raw.get("views") or raw.get("view_count") or 0),
        }

    async def get_feed_lock(self) -> dict[str, Any]:
        raw = await self._call(self.social.feed_lock)
        return dict(raw) if isinstance(raw, Mapping) else {}

    # ---- LegitClient Protocol: writes ------------------------------------ #
    async def post(self, account: str, text: str, **kwargs: Any) -> dict[str, Any]:
        visibility = kwargs.get("visibility")
        raw = await self._call_as(account, lambda acct: acct.post(text, visibility=visibility))
        return self.normalize_post(raw) if isinstance(raw, Mapping) else {"ok": True}

    async def repost(
        self,
        account: str,
        post_id: str,
        *,
        comment: str | None = None,
        visibility: str | None = None,
    ) -> dict[str, Any]:
        pid = _coerce_int_id(post_id)
        raw = await self._call_as(
            account, lambda acct: acct.repost(pid, comment=comment, visibility=visibility)
        )
        if isinstance(raw, Mapping):
            return self.normalize_post(raw)
        return {"ok": True, "post_id": str(post_id)}

    async def reply(
        self,
        account: str,
        post_id: str,
        text: str,
        *,
        root_post_id: int | str | None = None,
        visibility: str | None = None,
    ) -> dict[str, Any]:
        pid = _coerce_int_id(post_id)
        root_id = _coerce_int_id(root_post_id) if root_post_id is not None else None
        raw = await self._call_as(
            account,
            lambda acct: acct.reply(pid, text, root_post_id=root_id, visibility=visibility),
        )
        return (
            self.normalize_post(raw, default_parent_id=str(pid))
            if isinstance(raw, Mapping)
            else {"ok": True, "post_id": ""}
        )

    async def like(self, account: str, post_id: str) -> dict[str, Any]:
        pid = _coerce_int_id(post_id)
        await self._call_as(account, lambda acct: acct.like(pid))
        return {"ok": True, "post_id": str(post_id)}

    async def follow(self, account: str, target: str) -> dict[str, Any]:
        aid = await self._resolve_account_id(target)
        await self._call_as(account, lambda acct: acct.follow(aid))
        return {"ok": True, "target": str(target), "account_id": aid}

    async def unfollow(self, account: str, target: str) -> dict[str, Any]:
        aid = await self._resolve_account_id(target)
        await self._call_as(account, lambda acct: acct.unfollow(aid))
        return {"ok": True, "target": str(target), "account_id": aid}

    async def report(
        self,
        account: str,
        post_id: str,
        *,
        reason: str = "inauthentic",
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Report a post via `Account.report_post(post_id, reason, detail)`.

        Official Legit Social requires `reason` to be one of
        `("hate_speech", "illegal_content", "inauthentic")`. If caller passes an
        evidence string in `reason` (e.g. `"confirmed_bot (score=100, ...)"`),
        we automatically set `reason="inauthentic"` and move the evidence into `detail`.
        """
        pid = _coerce_int_id(post_id)
        clean_reason = (reason or "").strip()
        if clean_reason in POST_REPORT_REASONS:
            api_reason = clean_reason
            api_detail = detail
        else:
            api_reason = "inauthentic"
            api_detail = detail if detail is not None else (clean_reason or None)

        raw = await self._call_as(
            account,
            lambda acct: acct.report_post(pid, api_reason, detail=api_detail),
        )
        out: dict[str, Any] = {
            "ok": True,
            "post_id": str(post_id),
            "reason": api_reason,
            "detail": api_detail,
        }
        if isinstance(raw, Mapping):
            out.update(raw)
            out["post_id"] = str(raw.get("post_id", post_id))
        return out

    async def report_account(
        self,
        account: str,
        target: int | str,
        *,
        reason: str = "inauthentic",
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Report an account directly (`/accounts/{id}/report`, independent 3/min bucket)."""
        aid = await self._resolve_account_id(target)
        raw = await self._call_as(
            account,
            lambda acct: acct.report_account(aid, reason or "inauthentic", detail=detail),
        )
        out: dict[str, Any] = {
            "ok": True,
            "account_id": aid,
            "target": str(target),
            "reason": reason,
            "detail": detail,
        }
        if isinstance(raw, Mapping):
            out.update(raw)
        return out

    async def delete_account(self, account: str) -> dict[str, Any]:
        """Delete a bot account (`DELETE /accounts/{id}`, `acct.delete()`) to free an active slot.

        Under 2026 competition rules (max 50 active accounts at once, max 200 total accounts),
        deleting a suspended or retired account frees its active slot while preserving its
        record in `managed_accounts` with `status='deleted'`.
        """
        uname = account.strip()
        await self._call_as(uname, lambda acct: acct.delete())
        self._accounts.pop(uname, None)
        self._tokens.pop(uname, None)
        if self.store is not None and hasattr(self.store, "mark_managed_account_status"):
            self.store.mark_managed_account_status(uname, "deleted")
        return {"ok": True, "account": uname, "status": "deleted"}

