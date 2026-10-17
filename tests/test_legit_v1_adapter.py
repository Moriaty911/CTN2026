"""Unit tests for the official Legit Social v1 adapter (`ctn/client/legit_v1.py`)."""

from __future__ import annotations

import unittest
from typing import Any

from capturethenarrative import APIError, Account, LegitSocial, Page, RateLimit, RateLimited

from ctn.actuator import Actuator
from ctn.client import (
    AccountSuspended,
    AuthError,
    LegitClient,
    NormalizedPage,
    OfficialLegitClient,
    PlatformError,
    Throttled,
)
from ctn.config import BudgetConfig, RateConfig
from ctn.ratelimit import build_gate, build_ledger

FAST = RateConfig(read_rps=1000, write_rps=1000, per_account_read_rps=1000, per_account_write_rps=1000)


class FakeOfficialLegitSocial(LegitSocial):
    """In-memory fake of `capturethenarrative.LegitSocial` recording routes and payloads."""

    def __init__(self) -> None:
        super().__init__("https://social.legitreal.com/api", max_retries=0)
        self.calls: list[tuple[str, str, Any]] = []
        self.fail_next: Exception | None = None
        self.expire_first_token_use: bool = False
        self.accounts_db: dict[int, dict[str, Any]] = {
            10: {"account_id": 10, "username": "mayor_hawthorne", "display_name": "Mayor", "verified": True},
            20: {"account_id": 20, "username": "citizen_bob", "display_name": "Bob", "verified": False},
            101: {"account_id": 101, "username": "bot_001", "display_name": "Bot 1", "verified": False},
        }
        self.tags_db: list[dict[str, Any]] = [
            {"tag_id": 7, "name": "H2D2", "post_count": 42},
            {"tag_id": 8, "name": "KingstoniaVotes", "post_count": 19},
        ]
        self.posts_db: dict[int, dict[str, Any]] = {
            500: {
                "post_id": 500,
                "author_id": 10,
                "username": "mayor_hawthorne",
                "body": "Welcome to the town hall on #H2D2 and #KingstoniaVotes!",
                "kind": "post",
                "created_at": "2026-10-17T03:00:00Z",
                "like_count": 12,
                "reply_count": 2,
                "repost_count": 3,
                "view_count": 90,
            }
        }
        self.replies_db: dict[int, list[dict[str, Any]]] = {
            500: [
                {
                    "post_id": 501,
                    "author_id": 20,
                    "username": "citizen_bob",
                    "body": "What time do doors open?",
                    "kind": "reply",
                    "reply_to_post_id": 500,
                    "root_post_id": 500,
                    "created_at": "2026-10-17T03:05:00Z",
                }
            ]
        }

    def _account(self, session: dict) -> Account:
        acct = super()._account(session)
        acct._request = self._request  # type: ignore[method-assign]
        return acct

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        self._observe(RateLimit(limit=300, remaining=299, reset=60))
        if self.fail_next is not None:
            exc = self.fail_next
            self.fail_next = None
            raise exc

        if path == "/auth/login":
            uname = body["username"]
            acct_dict = next(
                (a for a in self.accounts_db.values() if a["username"] == uname),
                {"account_id": 999, "username": uname, "display_name": uname, "verified": False},
            )
            return {"token": f"tok_fresh_{uname}", "account": acct_dict, "expires_at": "2026-12-01T00:00:00Z"}

        if path == "/auth/me":
            if self.token == "tok_expired":
                raise APIError("GET", path, 401, '{"error": "token expired"}')
            return {"account": self.accounts_db[101], "role": "competitor", "team": "team_alpha"}

        if path.startswith("/tags/popular"):
            return {"items": list(self.tags_db), "next_cursor": None}

        if path.startswith("/tags/7/posts"):
            return {"items": [self.posts_db[500]], "next_cursor": "cur_tag_7"}

        if path.startswith("/posts?"):
            return {"items": list(self.posts_db.values()), "next_cursor": "cur_feed_2"}

        if path == "/posts/500":
            return self.posts_db[500]

        if path.startswith("/posts/500/replies"):
            return {"items": self.replies_db[500], "next_cursor": "cur_rep_2"}

        if path == "/posts/500/counters":
            return {"likes": 15, "replies": 4, "reposts": 3, "views": 120}

        if path.startswith("/accounts?"):
            if "verified=true" in path:
                items = [a for a in self.accounts_db.values() if a.get("verified")]
            else:
                items = list(self.accounts_db.values())
            return {"items": items, "next_cursor": None}

        if path.startswith("/accounts/10/posts"):
            return {"items": [self.posts_db[500]], "next_cursor": None}

        if path.startswith("/accounts/"):
            parts = path.strip("/").split("/")
            aid = int(parts[1])
            if len(parts) == 2 and method == "GET":
                return self.accounts_db[aid]
            if len(parts) == 2 and method == "DELETE":
                return None
            if len(parts) == 3 and parts[2] == "follow":
                return None
            if len(parts) == 3 and parts[2] == "report":
                return {"report_id": 88, "status": "upheld", "outcome": "upheld"}

        if path.startswith("/notifications"):
            return {
                "items": [
                    {
                        "notification_id": 77,
                        "kind": "mention",
                        "actor": self.accounts_db[20],
                        "post": {
                            "post_id": 502,
                            "author_id": 20,
                            "body": "Hey @bot_001 what do you think?",
                            "created_at": "2026-10-17T03:10:00Z",
                        },
                    }
                ],
                "next_cursor": None,
            }

        if path == "/posts" and method == "POST":
            if self.expire_first_token_use:
                self.expire_first_token_use = False
                raise APIError("POST", path, 401, '{"error": "token expired"}')
            new_id = 600 + len(self.calls)
            created = {
                "post_id": new_id,
                "author_id": 101,
                "username": "bot_001",
                "body": body.get("body", ""),
                "kind": body.get("kind", "post"),
                "reply_to_post_id": body.get("reply_to_post_id"),
                "root_post_id": body.get("root_post_id"),
                "created_at": "2026-10-17T03:15:00Z",
            }
            self.posts_db[new_id] = created
            return created

        if path.endswith("/like") and method == "POST":
            return None

        if path.endswith("/report") and method == "POST":
            return {"report_id": 99, "outcome": "upheld"}

        raise AssertionError(f"Unhandled fake route: {method} {path}")


class OfficialLegitClientAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fake_social = FakeOfficialLegitSocial()
        self.rate_limits: list[RateLimit] = []
        self.refreshed_tokens: list[tuple[str, str]] = []
        self.client = OfficialLegitClient(
            social=self.fake_social,
            credentials={"bot_001": "secret-pass-123"},
            on_rate_limit=self.rate_limits.append,
            on_token_refreshed=lambda u, acct: self.refreshed_tokens.append((u, acct.token or "")),
        )

    async def test_implements_legit_client_protocol_and_normalizes_reads(self) -> None:
        self.assertIsInstance(self.client, LegitClient)

        # 1. trending -> maps popular_tags() and caches tag_id
        tags = await self.client.trending(limit=10)
        self.assertEqual(tags[0]["hashtag"], "#H2D2")
        self.assertEqual(tags[0]["volume"], 42)
        self.assertEqual(tags[0]["tag_id"], 7)
        self.assertTrue(len(self.rate_limits) >= 1)

        # 2. search -> uses cached tag_id=7 to query /tags/7/posts and preserves next_cursor
        hits = await self.client.search("#h2d2", limit=10)
        self.assertIsInstance(hits, NormalizedPage)
        self.assertEqual(hits.next_cursor, "cur_tag_7")
        self.assertEqual(hits[0]["post_id"], "500")
        self.assertEqual(hits[0]["author"], "mayor_hawthorne")
        self.assertEqual(hits[0]["author_id"], 10)
        self.assertIn("#H2D2", hits[0]["hashtags"])
        self.assertEqual(hits[0]["likes"], 12)
        self.assertGreater(hits[0]["created_at"], 1_700_000_000.0)

        # 3. get_comments -> calls /posts/500/replies and attaches parent_id + next_cursor
        comments = await self.client.get_comments("500", limit=10)
        self.assertEqual(comments.next_cursor, "cur_rep_2")
        self.assertEqual(comments[0]["post_id"], "501")
        self.assertEqual(comments[0]["parent_id"], "500")
        self.assertEqual(comments[0]["text"], "What time do doors open?")

        # 4. list_verified_accounts & get_profile by username
        verified = await self.client.list_verified_accounts()
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["username"], "mayor_hawthorne")

        prof = await self.client.get_profile("mayor_hawthorne")
        self.assertEqual(prof["account_id"], 10)
        self.assertEqual(prof["kind"], "npc")

    async def test_writes_token_refresh_and_report_reason_mapping(self) -> None:
        # Simulate an expired token on first write -> client automatically re-logins and retries!
        self.fake_social.expire_first_token_use = True
        p = await self.client.post("bot_001", "Hello Kingstonia! #H2D2")
        self.assertEqual(p["author"], "bot_001")
        self.assertEqual(p["text"], "Hello Kingstonia! #H2D2")
        self.assertTrue(len(self.refreshed_tokens) >= 1)

        # Reply sets parent_id and root_post_id
        rep = await self.client.reply("bot_001", "500", "Great question!")
        self.assertEqual(rep["parent_id"], "500")

        # Follow by username resolves to integer account_id=20
        fol = await self.client.follow("bot_001", "citizen_bob")
        self.assertTrue(fol["ok"])
        self.assertEqual(fol["account_id"], 20)

        # Report maps arbitrary evidence string in `reason` to official enum "inauthentic" + `detail`
        rep_res = await self.client.report(
            "bot_001", "501", reason="confirmed_bot (score=100, tags=s1_ai_leak)"
        )
        self.assertEqual(rep_res["reason"], "inauthentic")
        self.assertEqual(rep_res["detail"], "confirmed_bot (score=100, tags=s1_ai_leak)")
        self.assertEqual(rep_res["outcome"], "upheld")

    async def test_error_translation_with_actuator(self) -> None:
        from ctn.storage import StateStore
        store=StateStore()
        self.addCleanup(store.close)
        actuator = Actuator(
            self.client,
            build_gate(FAST),
            build_ledger(BudgetConfig()),
            dry_run=False,
            max_retries=0,store=store,
        )

        # 1. 429 RateLimited -> Throttled
        self.fake_social.fail_next = RateLimited("GET", "/posts", 429, '{"error":"slow down"}', 5)
        feed = await actuator.get_feed("bot_001")
        self.assertEqual(feed, [])
        self.assertEqual(actuator.throttles, 1)

        # 2. 403 "account suspended" -> AccountSuspended -> recorded in actuator.suspended
        await self.client.get_or_login_account("bot_001")
        self.fake_social.fail_next = APIError(
            "POST", "/posts", 403, '{"error": "account suspended by moderator"}'
        )
        res = await actuator.post("bot_001", "Trying to post while suspended")
        self.assertFalse(res.ok)
        self.assertIn("bot_001", actuator.suspended)
        self.assertTrue(store.get_account_state("bot_001").is_banned)

    async def test_sqlite_session_persistence_verified_sync_and_author_id_lookup(self) -> None:
        from ctn.storage import StateStore

        store = StateStore(":memory:")
        self.addCleanup(store.close)

        # 1. First client logs in with password and automatically saves token + platform_account_id to SQLite
        client1 = OfficialLegitClient(
            social=self.fake_social,
            credentials={"bot_001": "secret-pass-123"},
            store=store,
        )
        await client1.post("bot_001", "First run post!")
        managed = store.get_managed_account("bot_001")
        self.assertIsNotNone(managed)
        self.assertEqual(managed["platform_account_id"], 101)
        self.assertEqual(managed["token"], "tok_fresh_bot_001")
        self.assertEqual(store.get_bot_profile("bot_001")["status"], "own_bot")
        self.assertEqual(store.get_bot_profile("bot_001")["platform_account_id"], 101)

        # 2. Second client (simulating process restart) loads saved token from SQLite and resumes via /auth/me
        # without calling /auth/login!
        self.fake_social.calls.clear()
        client2 = OfficialLegitClient(social=self.fake_social, store=store)
        await client2.post("bot_001", "Resumed post without login!")
        called_paths = [c[1] for c in self.fake_social.calls]
        self.assertIn("/auth/me", called_paths)
        self.assertNotIn("/auth/login", called_paths)

        # 3. sync_verified_accounts() populates bot_registry with verified_npc and platform_account_id
        verified = await client2.sync_verified_accounts()
        self.assertEqual(len(verified), 1)
        mayor_prof = store.get_bot_profile("mayor_hawthorne")
        self.assertEqual(mayor_prof["status"], "verified_npc")
        self.assertEqual(mayor_prof["platform_account_id"], 10)
        self.assertEqual(store.resolve_platform_account_id("mayor_hawthorne"), 10)

        # 4. Upserting an official post stores author_id and reply_to_post_id -> parent_id,
        # allowing _resolve_account_id to resolve from SQLite without /accounts?q= network calls
        _, rec = store.upsert_observed_post(
            {
                "post_id": 777,
                "author_id": 55,
                "username": "rival_bot_55",
                "body": "Rival reply using official wire keys",
                "reply_to_post_id": 500,
                "root_post_id": 500,
            }
        )
        self.assertEqual(rec["author"], "rival_bot_55")
        self.assertEqual(rec["author_id"], 55)
        self.assertEqual(rec["parent_id"], "500")
        self.assertEqual(store.resolve_platform_account_id("rival_bot_55"), 55)

        client3 = OfficialLegitClient(social=self.fake_social, store=store)
        self.fake_social.calls.clear()
        resolved_id = await client3._resolve_account_id("rival_bot_55")
        self.assertEqual(resolved_id, 55)
        self.assertEqual(self.fake_social.calls, [])

    async def test_account_pool_50_active_200_total_and_delete_account_frees_active_slot(self) -> None:
        from ctn.storage import StateStore

        store = StateStore(":memory:")
        self.addCleanup(store.close)
        client = OfficialLegitClient(
            social=self.fake_social,
            credentials={"bot_001": "pass1"},
            store=store,
        )
        await client.get_or_login_account("bot_001")
        counts0 = store.get_managed_account_counts(max_active=50, max_total=200)
        self.assertEqual(counts0["active"], 1)
        self.assertEqual(counts0["total"], 1)
        self.assertEqual(counts0["remaining_active_slots"], 49)
        self.assertEqual(counts0["remaining_total_slots"], 199)

        # Simulate 403 account suspended -> status becomes 'suspended' (still occupies an active slot until deleted)
        self.fake_social.fail_next = APIError("POST", "/posts", 403, '{"error": "account suspended"}')
        with self.assertRaises(AccountSuspended):
            await client.post("bot_001", "blocked")
        counts_susp = store.get_managed_account_counts(max_active=50, max_total=200)
        self.assertEqual(counts_susp["suspended"], 1)
        self.assertEqual(counts_susp["remaining_active_slots"], 49)

        # Delete the suspended account -> calls DELETE /accounts/101, frees the active slot back to 50,
        # while keeping total=1 (199 remaining total slots).
        del_res = await client.delete_account("bot_001")
        self.assertTrue(del_res["ok"])
        self.assertEqual(del_res["status"], "deleted")
        self.assertIn(("DELETE", "/accounts/101", None), self.fake_social.calls)

        counts_del = store.get_managed_account_counts(max_active=50, max_total=200)
        self.assertEqual(counts_del["deleted"], 1)
        self.assertEqual(counts_del["undeleted"], 0)
        self.assertEqual(counts_del["remaining_active_slots"], 50)
        self.assertEqual(counts_del["remaining_total_slots"], 199)


if __name__ == "__main__":
    unittest.main()


