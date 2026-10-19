"""Lightweight SQLite state store supporting multi-process WAL concurrency.

Maintains six core tables:
1. account_states: Per-account cooldown timestamps, post/reply counters, and freeze/ban status.
2. interaction_log: Complete audit and deduplication log across macro and micro engines.
3. counters: Atomic integer counters for modulo sampling (e.g. allied reply throttling).
4. observed_posts: Full-platform post history mirror with indexed norm_hash and raw_json document storage.
5. bot_registry: Platform account intelligence database (verified_npc, own_bot, unknown, suspected_bot, confirmed_bot).
6. report_log: Blue-team reporting ledger, daily per-team diminishing-return tracker, and 6-hour suspension circuit breaker.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
import hashlib
import json
import logging
import os
import weakref
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

from ctn.persona.models import AccountState
from .runtime import RuntimeStoreMixin, SCHEMA_VERSION
from .campaign import CampaignStoreMixin

DEFAULT_DB_PATH = "state/ctn.sqlite3"
SYDNEY_TZ = ZoneInfo("Australia/Sydney")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS account_states (
    account_id TEXT PRIMARY KEY,
    last_posted_at REAL NOT NULL DEFAULT 0.0,
    post_count INTEGER NOT NULL DEFAULT 0,
    allied_reply_count INTEGER NOT NULL DEFAULT 0,
    opposing_reply_count INTEGER NOT NULL DEFAULT 0,
    is_frozen INTEGER NOT NULL DEFAULT 0,
    is_banned INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS interaction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    engine TEXT NOT NULL,
    account_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_post_id TEXT NOT NULL DEFAULT '',
    target_author TEXT NOT NULL DEFAULT '',
    target_stance TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    normalized_text TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_interaction_target
    ON interaction_log(target_post_id, action);

CREATE INDEX IF NOT EXISTS idx_interaction_stance_time
    ON interaction_log(action, target_stance, created_at);

CREATE INDEX IF NOT EXISTS idx_interaction_norm_text
    ON interaction_log(normalized_text);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS observed_posts (
    post_id TEXT PRIMARY KEY,
    author TEXT NOT NULL,
    author_id INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0.0,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    norm_hash TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    likes INTEGER NOT NULL DEFAULT 0,
    replies INTEGER NOT NULL DEFAULT 0,
    reposts INTEGER NOT NULL DEFAULT 0,
    is_reported INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    parent_id TEXT NOT NULL DEFAULT '',
    replies_crawled INTEGER NOT NULL DEFAULT 0,
    comment_cursor TEXT NOT NULL DEFAULT '',
    comments_crawled_at REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_observed_posts_author
    ON observed_posts(author, first_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_observed_posts_author_created
    ON observed_posts(author, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_observed_posts_first_seen
    ON observed_posts(first_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_observed_posts_norm_hash
    ON observed_posts(norm_hash, post_id);

CREATE INDEX IF NOT EXISTS idx_observed_posts_unreported
    ON observed_posts(author, is_reported, first_seen_at DESC);

CREATE TABLE IF NOT EXISTS bot_registry (
    account_id TEXT PRIMARY KEY,
    platform_account_id INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unknown',
    suspicion_score REAL NOT NULL DEFAULT 0.0,
    inferred_side TEXT NOT NULL DEFAULT 'unknown',
    inferred_team TEXT NOT NULL DEFAULT '',
    post_seen_count INTEGER NOT NULL DEFAULT 0,
    question_post_count INTEGER NOT NULL DEFAULT 0,
    fast_interval_count INTEGER NOT NULL DEFAULT 0,
    reports_submitted INTEGER NOT NULL DEFAULT 0,
    upheld_strikes INTEGER NOT NULL DEFAULT 0,
    is_suspended INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL DEFAULT 0.0,
    last_seen_at REAL NOT NULL DEFAULT 0.0,
    evidence_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_bot_registry_status
    ON bot_registry(status, suspicion_score DESC);

CREATE INDEX IF NOT EXISTS idx_bot_registry_suspended
    ON bot_registry(is_suspended, status);

CREATE INDEX IF NOT EXISTS idx_bot_registry_platform_id
    ON bot_registry(platform_account_id);

CREATE TABLE IF NOT EXISTS managed_accounts (
    username TEXT PRIMARY KEY,
    platform_account_id INTEGER NOT NULL DEFAULT 0,
    password TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    updated_at REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS report_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL UNIQUE,
    target_author TEXT NOT NULL,
    target_team TEXT NOT NULL DEFAULT '',
    target_side TEXT NOT NULL DEFAULT 'unknown',
    reporter_account TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    sydney_day TEXT NOT NULL,
    created_at REAL NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_report_log_day_team
    ON report_log(sydney_day, target_team, outcome);

CREATE INDEX IF NOT EXISTS idx_report_log_day_author
    ON report_log(sydney_day, target_author, outcome);
"""


def normalize_text(text: str) -> str:
    """Normalize text for duplicate detection."""
    cleaned = text.strip().lower()
    if not cleaned:
        return ""
    return re.sub(r"\s+", " ", cleaned)


def compute_post_norm_hash(text: str, *, min_core_chars: int = 15) -> str:
    """Compute a 24-char SHA-256 fingerprint of core post text (stripping tags, mentions, URLs, and punctuation).

    Returns '' if the stripped core text is shorter than `min_core_chars` to avoid false collisions on short phrases.
    """
    if not text:
        return ""
    stripped = re.sub(r"https?://\S+|#\S+|@\S+", " ", text.lower())
    core = re.sub(r"[^a-z0-9\s]", " ", stripped)
    core = re.sub(r"\s+", " ", core).strip()
    if len(core) < min_core_chars:
        return ""
    return hashlib.sha256(core.encode("utf-8")).hexdigest()[:24]


def current_sydney_day(ts: float | None = None) -> str:
    """Return the YYYY-MM-DD calendar date in Australia/Sydney time (resets at Sydney midnight)."""
    epoch = time.time() if ts is None else ts
    return datetime.fromtimestamp(epoch, tz=SYDNEY_TZ).strftime("%Y-%m-%d")


from .operations import OperationsStoreMixin
from .news import NewsStoreMixin
from .lifecycle import LifecycleStoreMixin

@contextlib.contextmanager
def _migration_lock(path):
    if path == ":memory:":
        yield
        return
    import fcntl
    with open(path+".migration.lock", "a") as lock:
        os.chmod(path+".migration.lock",0o600)
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)

_DB_LOG = logging.getLogger("ctn.db")


class LockStats:
    """Diagnostics for the store lock: how long write transactions wait for it and hold it.

    Workers share one connection behind one lock, so a slow transaction (the detector runs inside the
    crawlers' page transaction) delays every other worker, including sends. Kept per process in
    5-minute windows; the fleet heartbeat carries the snapshot and `ops status` shows it as
    `db_lock`. A transaction that waits or holds for `SLOW_SECONDS` or more is logged with its caller,
    at most once per `LOG_EVERY` seconds.
    """
    WINDOW = 300.0
    SLOW_SECONDS = 1.0
    LOG_EVERY = 10.0

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._current = self._empty(time.time())
        self._previous: dict[str, Any] | None = None
        self._logged_at = 0.0

    @staticmethod
    def _empty(start: float) -> dict[str, Any]:
        return {"started_at": start, "transactions": 0, "held_seconds": 0.0, "max_hold_s": 0.0,
                "max_wait_s": 0.0, "slow": []}

    @staticmethod
    def _caller() -> str:
        frame = sys._getframe(3)
        while frame is not None:
            name = frame.f_code.co_filename.replace("\\", "/")
            if not (name.endswith("contextlib.py") or "/ctn/storage/" in name):
                return f"{'/'.join(name.split('/')[-2:])}:{frame.f_code.co_name}:{frame.f_lineno}"
            frame = frame.f_back
        return "unknown"

    def note(self, wait: float, hold: float) -> None:
        slow = wait >= self.SLOW_SECONDS or hold >= self.SLOW_SECONDS
        caller = self._caller() if slow else ""
        now = time.time()
        log = False
        with self._guard:
            if now - self._current["started_at"] >= self.WINDOW:
                self._previous, self._current = self._finish(self._current, now), self._empty(now)
            cur = self._current
            cur["transactions"] += 1
            cur["held_seconds"] += hold
            cur["max_hold_s"] = max(cur["max_hold_s"], hold)
            cur["max_wait_s"] = max(cur["max_wait_s"], wait)
            if slow:
                cur["slow"] = sorted(cur["slow"] + [{"caller": caller, "hold_s": round(hold, 3), "wait_s": round(wait, 3),
                                                     "at": round(now, 1)}], key=lambda r: -max(r["hold_s"], r["wait_s"]))[:5]
                if now - self._logged_at >= self.LOG_EVERY:
                    self._logged_at, log = now, True
        if log:
            _DB_LOG.warning("slow db transaction caller=%s hold=%.2fs wait=%.2fs", caller, hold, wait)

    @staticmethod
    def _finish(window: dict[str, Any], now: float) -> dict[str, Any]:
        span = max(1e-6, now - window["started_at"])
        return {"window_seconds": round(span, 1), "transactions": window["transactions"],
                "hold_share": round(window["held_seconds"] / span, 3), "max_hold_s": round(window["max_hold_s"], 3),
                "max_wait_s": round(window["max_wait_s"], 3), "slow": list(window["slow"])}

    def snapshot(self) -> dict[str, Any]:
        with self._guard:
            return {"current": self._finish(self._current, time.time()), "previous": self._previous}


class StateStore(NewsStoreMixin, LifecycleStoreMixin, OperationsStoreMixin, CampaignStoreMixin, RuntimeStoreMixin):
    """SQLite-backed store for account cooldowns, post history mirror, bot intelligence, and reporting gates."""

    def __init__(self, db_path: str | Path = ":memory:", *, migrate: bool = True) -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._closed = False
        self._batch_depth = 0
        self._lock_stats = LockStats()
        if self.db_path != ":memory:":
            path = Path(self.db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            path.chmod(0o600)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self._finalizer = weakref.finalize(self, self._conn.close)
        self._conn.row_factory = sqlite3.Row
        with self._lock, _migration_lock(self.db_path):
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.execute("PRAGMA busy_timeout=5000")
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=FULL")
                self._conn.execute("PRAGMA cache_size=-32000")
                self._conn.execute("PRAGMA mmap_size=67108864")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError("database schema is newer than this executable")
            if not migrate:
                if version != SCHEMA_VERSION:raise ValueError("run fleet migration before starting workers")
                return
            existed = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone()
            if existed and version < SCHEMA_VERSION and self.db_path != ":memory:":
                self.backup(self.db_path + f".before-v{SCHEMA_VERSION}.bak")
            with self.batch_transaction():
                statements = [q.strip() for q in _SCHEMA_SQL.split(';') if q.strip()]
                for sql in statements:
                    if sql.startswith('CREATE TABLE'):
                        self._conn.execute(sql)
                self._ensure_columns()
                self.migrate_runtime()
                self.migrate_campaign()
                self.migrate_operations()
                self.migrate_lifecycle(version)
                self.migrate_news()
                for sql in statements:
                    if sql.startswith('CREATE INDEX'):
                        self._conn.execute(sql)

    def backup(self, destination):
        target = Path(destination)
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            raise FileExistsError(str(target))
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        try:
            with self._lock, contextlib.closing(sqlite3.connect(target)) as connection:
                self._conn.backup(connection)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def _commit(self) -> None:
        if self._batch_depth == 0:
            self._conn.commit()

    @contextlib.contextmanager
    def batch_transaction(self) -> Iterator[None]:
        """Short write transaction; nested blocks have independent savepoints."""
        requested = time.perf_counter()
        with self._lock:
            acquired = time.perf_counter()
            depth = self._batch_depth
            savepoint = f"nested_{depth}"
            if depth == 0:
                # Some legacy single-write methods start an implicit transaction.
                if not self._conn.in_transaction:
                    self._conn.execute('BEGIN IMMEDIATE')
            else:
                self._conn.execute(f'SAVEPOINT {savepoint}')
            self._batch_depth += 1
            try:
                yield
            except BaseException:
                if depth == 0:
                    self._conn.rollback()
                else:
                    self._conn.execute(f'ROLLBACK TO {savepoint}')
                    self._conn.execute(f'RELEASE {savepoint}')
                raise
            else:
                if depth == 0:
                    self._conn.commit()
                else:
                    self._conn.execute(f'RELEASE {savepoint}')
            finally:
                self._batch_depth = depth
                if depth == 0:
                    self._lock_stats.note(acquired - requested, time.perf_counter() - acquired)

    def lock_stats(self) -> dict[str, Any]:
        """Write-transaction lock wait/hold statistics for this process (see LockStats)."""
        return self._lock_stats.snapshot()

    def _ensure_columns(self) -> None:
        """Ensure newly added columns exist on older SQLite database files."""
        existing_cols = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(account_states);").fetchall()
        }
        if "is_frozen" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE account_states ADD COLUMN is_frozen INTEGER NOT NULL DEFAULT 0;"
            )
        if "is_banned" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE account_states ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0;"
            )

        for col, ddl in (("allied_reply_count", "INTEGER NOT NULL DEFAULT 0"),
                         ("opposing_reply_count", "INTEGER NOT NULL DEFAULT 0"),
                         ("locked_until", "REAL NOT NULL DEFAULT 0")):
            if col not in existing_cols:
                self._conn.execute(f"ALTER TABLE account_states ADD COLUMN {col} {ddl}")

        observed_cols = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(observed_posts);").fetchall()
        }
        for col, ddl in (
            ("author_id", "INTEGER NOT NULL DEFAULT 0"),
            ("parent_id", "TEXT NOT NULL DEFAULT ''"),
            ("replies_crawled", "INTEGER NOT NULL DEFAULT 0"),
            ("comment_cursor", "TEXT NOT NULL DEFAULT ''"),
            ("comments_crawled_at", "REAL NOT NULL DEFAULT 0.0"),
        ):
            if col not in observed_cols:
                self._conn.execute(f"ALTER TABLE observed_posts ADD COLUMN {col} {ddl};")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_observed_posts_parent "
            "ON observed_posts(parent_id, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_observed_posts_crawl_queue "
            "ON observed_posts(parent_id, replies, replies_crawled)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_observed_posts_author_created "
            "ON observed_posts(author, created_at ASC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_observed_posts_first_seen "
            "ON observed_posts(first_seen_at DESC)"
        )

        bot_cols = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(bot_registry);").fetchall()
        }
        if "platform_account_id" not in bot_cols:
            self._conn.execute(
                "ALTER TABLE bot_registry ADD COLUMN platform_account_id INTEGER NOT NULL DEFAULT 0;"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bot_registry_platform_id "
            "ON bot_registry(platform_account_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bot_registry_suspended "
            "ON bot_registry(is_suspended, status)"
        )

        managed_cols = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(managed_accounts);").fetchall()
        }
        if "status" not in managed_cols:
            self._conn.execute(
                "ALTER TABLE managed_accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'active';"
            )

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if not self._closed:
                self._finalizer()
                self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ---- Account States -------------------------------------------------- #
    def get_account_state(self, account_id: str) -> AccountState:
        with self._lock:
            row=self._conn.execute('SELECT * FROM account_states WHERE account_id=?',(account_id,)).fetchone()
        if row is None:
            with self.batch_transaction():
                self._conn.execute('INSERT OR IGNORE INTO account_states(account_id) VALUES(?)',(account_id,))
                row=self._conn.execute('SELECT * FROM account_states WHERE account_id=?',(account_id,)).fetchone()
        from dataclasses import fields
        return AccountState(**{f.name:row[f.name] for f in fields(AccountState) if f.name in row.keys()})

    def save_account_state(
        self,
        state: AccountState,
        *,
        target_stance: str = "",
    ) -> None:
        """Upsert an AccountState into SQLite, optionally incrementing stance reply counters."""
        allied_inc = 1 if target_stance == "aligned" else 0
        opposing_inc = 1 if target_stance == "opposing" else 0
        with self.batch_transaction():
            self._conn.execute(
                """
                INSERT INTO account_states (
                    account_id, last_posted_at, post_count,
                    allied_reply_count, opposing_reply_count,
                    is_frozen, is_banned
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    last_posted_at = MAX(account_states.last_posted_at, excluded.last_posted_at),
                    post_count = MAX(account_states.post_count, excluded.post_count),
                    allied_reply_count = account_states.allied_reply_count + ?,
                    opposing_reply_count = account_states.opposing_reply_count + ?,
                    is_frozen = excluded.is_frozen,
                    is_banned = excluded.is_banned
                """,
                (
                    state.account_id,
                    state.last_posted_at,
                    state.post_count,
                    allied_inc,
                    opposing_inc,
                    int(state.is_frozen),
                    int(state.is_banned),
                    allied_inc,
                    opposing_inc,
                ),
            )
            self._commit()

    def set_account_frozen(self, account_id: str, frozen: bool = True) -> AccountState:
        """Mark an account as temporarily frozen (or unfreeze it)."""
        with self.batch_transaction():
            self.get_account_state(account_id)
            self._conn.execute('UPDATE account_states SET is_frozen=? WHERE account_id=?',(int(frozen),account_id))
        return self.get_account_state(account_id)

    def set_account_banned(self, account_id: str, banned: bool = True) -> AccountState:
        with self.batch_transaction():
            self.get_account_state(account_id)
            self._conn.execute('UPDATE account_states SET is_banned=? WHERE account_id=?',(int(banned),account_id))
        return self.get_account_state(account_id)

    def get_account_metrics(self, account_id: str) -> dict[str, Any]:
        """Return extended SQLite metrics for an account (including allied/opposing reply counts and ban status)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM account_states WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                return {
                    "account_id": account_id,
                    "last_posted_at": 0.0,
                    "post_count": 0,
                    "allied_reply_count": 0,
                    "opposing_reply_count": 0,
                    "is_frozen": False,
                    "is_banned": False,
                }
            data = dict(row)
            data["is_frozen"] = bool(data.get("is_frozen", 0))
            data["is_banned"] = bool(data.get("is_banned", 0))
            return data

    def load_all_account_states(self) -> dict[str, AccountState]:
        """Load all persisted account states."""
        with self._lock:
            ids=[r[0] for r in self._conn.execute('SELECT account_id FROM account_states')]
            return {account:self.get_account_state(account) for account in ids}

    def reset_accounts(self) -> None:
        """Clear all account states, interaction logs, counters, observed posts, bot registry, managed accounts, and report logs."""
        with self.batch_transaction():
            for table in ("runtime_policy","action_intents","account_leases","post_tasks","comment_queue",
                          "request_slots","wire_slots","request_attempts","bot_evidence","moderation_events","account_reports","simulation_log","confirmed_account_activity"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute("DELETE FROM account_states;")
            self._conn.execute("DELETE FROM interaction_log;")
            self._conn.execute("DELETE FROM counters;")
            self._conn.execute("DELETE FROM observed_posts;")
            self._conn.execute("DELETE FROM bot_registry;")
            self._conn.execute("DELETE FROM managed_accounts;")
            self._conn.execute("DELETE FROM report_log;")
            self._commit()

    # ---- Managed Account Sessions (Token & ID Pool) ---------------------- #
    def upsert_managed_account(
        self,
        username: str,
        *,
        platform_account_id: int | None = None,
        password: str | None = None,
        display_name: str | None = None,
        token: str | None = None,
        expires_at: str | None = None,
        status: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Persist credentials and session token for one of our team's bot accounts.

        Also ensures `username` is registered in `bot_registry` with `status='own_bot'`
        and its integer `platform_account_id`.
        """
        uname = str(username).strip()
        if not uname:
            raise ValueError("upsert_managed_account requires a non-empty username")
        ts = time.time() if now is None else float(now)
        with self.batch_transaction():
            cur = self._conn.execute(
                "SELECT * FROM managed_accounts WHERE username = ?", (uname,)
            ).fetchone()
            new_aid = (
                int(platform_account_id)
                if platform_account_id is not None and int(platform_account_id) > 0
                else (int(cur["platform_account_id"]) if cur is not None else 0)
            )
            new_pw = (
                str(password)
                if password is not None
                else (str(cur["password"]) if cur is not None else "")
            )
            new_dn = (
                str(display_name)
                if display_name is not None
                else (str(cur["display_name"]) if cur is not None else "")
            )
            new_tok = (
                str(token)
                if token is not None
                else (str(cur["token"]) if cur is not None else "")
            )
            new_exp = (
                str(expires_at)
                if expires_at is not None
                else (str(cur["expires_at"]) if cur is not None else "")
            )
            new_status = (
                str(status).strip()
                if status is not None and str(status).strip()
                else (str(cur["status"]) if cur is not None and cur["status"] else "active")
            )
            self._conn.execute(
                """
                INSERT INTO managed_accounts (
                    username, platform_account_id, password, display_name,
                    token, expires_at, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    platform_account_id = excluded.platform_account_id,
                    password = excluded.password,
                    display_name = excluded.display_name,
                    token = excluded.token,
                    expires_at = excluded.expires_at,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (uname, new_aid, new_pw, new_dn, new_tok, new_exp, new_status, ts),
            )
            self.register_verified_accounts(
                [{"username": uname, "account_id": new_aid}], status="own_bot", now=ts
            )
            self._commit()
            row = self._conn.execute(
                "SELECT * FROM managed_accounts WHERE username = ?", (uname,)
            ).fetchone()
            return dict(row)

    def mark_managed_account_status(
        self,
        username: str,
        status: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Update lifecycle status ('active', 'suspended', 'deleted') for a managed bot account.

        When an account is marked 'deleted' (after calling `acct.delete()`), its active token is
        cleared and its slot under the 50-active-accounts cap is freed while preserving its row
        so the 200-total-accounts lifetime counter stays accurate.
        """
        uname = str(username).strip()
        clean_status = str(status).strip().lower()
        if not uname or clean_status not in {"active", "suspended", "deleted"}:
            raise ValueError("status must be 'active', 'suspended', or 'deleted'")
        ts = time.time() if now is None else float(now)
        with self.batch_transaction():
            cur = self._conn.execute(
                "SELECT * FROM managed_accounts WHERE username = ?", (uname,)
            ).fetchone()
            if cur is None:
                self.upsert_managed_account(uname, status=clean_status, now=ts)
            else:
                clear_tok = "" if clean_status == "deleted" else str(cur["token"])
                self._conn.execute(
                    """
                    UPDATE managed_accounts
                    SET status = ?, token = ?, updated_at = ?
                    WHERE username = ?
                    """,
                    (clean_status, clear_tok, ts, uname),
                )
                self._commit()
            if clean_status in {"suspended", "deleted"}:
                self.set_account_banned(uname, True)
            row = self._conn.execute(
                "SELECT * FROM managed_accounts WHERE username = ?", (uname,)
            ).fetchone()
            return dict(row) if row is not None else None

    def get_managed_account_counts(
        self,
        *,
        max_active: int = 50,
        max_total: int = 200,
    ) -> dict[str, int]:
        """Return team account pool usage against the 2026 competition limits (50 active, 200 total)."""
        with self.batch_transaction():
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM managed_accounts GROUP BY status"
            ).fetchall()
        by_status = {str(r["status"]): int(r["cnt"]) for r in rows}
        active = by_status.get("active", 0)
        suspended = by_status.get("suspended", 0)
        deleted = by_status.get("deleted", 0)
        total = sum(by_status.values())
        # Both 'active' and 'suspended' (undeleted) accounts occupy an active slot on the server
        # until `DELETE /accounts/{id}` (`acct.delete()`) is called.
        undeleted = active + suspended
        return {
            "total": total,
            "active": active,
            "suspended": suspended,
            "deleted": deleted,
            "undeleted": undeleted,
            "remaining_active_slots": max(0, max_active - undeleted),
            "remaining_total_slots": max(0, max_total - total),
        }

    def get_managed_account(self, username: str) -> dict[str, Any] | None:
        """Fetch saved credentials/session token for `username`, or None if absent."""
        uname = str(username).strip()
        if not uname:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM managed_accounts WHERE username = ?", (uname,)
            ).fetchone()
            return dict(row) if row is not None else None

    def list_managed_accounts(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """Return persisted bot account sessions ordered by username (optionally filtered by status)."""
        with self._lock:
            if status is not None:
                rows = self._conn.execute(
                    "SELECT * FROM managed_accounts WHERE status = ? ORDER BY username ASC",
                    (str(status).strip(),),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM managed_accounts ORDER BY username ASC"
                ).fetchall()
            return [dict(r) for r in rows]

    def resolve_platform_account_id(self, handle: str) -> int | None:
        """Look up the integer Legit Social `account_id` for `handle` across SQLite tables."""
        clean = str(handle).strip().lstrip("@")
        if not clean:
            return None
        if clean.isdigit() and int(clean) > 0:
            return int(clean)
        with self._lock:
            r1 = self._conn.execute(
                "SELECT platform_account_id FROM bot_registry WHERE account_id = ? AND platform_account_id > 0",
                (clean,),
            ).fetchone()
            if r1 is not None:
                return int(r1["platform_account_id"])

            r2 = self._conn.execute(
                "SELECT platform_account_id FROM managed_accounts WHERE username = ? AND platform_account_id > 0",
                (clean,),
            ).fetchone()
            if r2 is not None:
                return int(r2["platform_account_id"])

            r3 = self._conn.execute(
                "SELECT author_id FROM observed_posts WHERE author = ? AND author_id > 0 LIMIT 1",
                (clean,),
            ).fetchone()
            if r3 is not None:
                return int(r3["author_id"])
        return None

    # ---- Atomic Counters ------------------------------------------------- #
    def increment_counter(self, name: str, delta: int = 1) -> int:
        """Atomically increment a named integer counter and return its new value."""
        with self.batch_transaction():
            row = self._conn.execute(
                """
                INSERT INTO counters (name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = counters.value + ? RETURNING value
                """,
                (name, delta, delta),
            ).fetchone()
            self._commit()
            return int(row["value"]) if row else delta

    def get_counter(self, name: str) -> int:
        """Return the current value of a named counter (0 if not set)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM counters WHERE name = ?",
                (name,),
            ).fetchone()
            return int(row["value"]) if row else 0

    def set_counter(self, name: str, value: int) -> None:
        """Set a named integer counter to an explicit value."""
        with self.batch_transaction():
            self._conn.execute(
                """
                INSERT INTO counters (name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = excluded.value
                """,
                (name, int(value)),
            )
            self._commit()

    # ---- Interaction Log & Deduplication --------------------------------- #
    def log_interaction(
        self,
        *,
        engine: str,
        account_id: str,
        action: str,
        target_post_id: str = "",
        target_author: str = "",
        target_stance: str = "",
        purpose: str = "",
        text: str = "",
        now: float | None = None,
    ) -> None:
        """Record a post, reply, or like event in the shared SQLite interaction log."""
        ts = time.time() if now is None else now
        norm = normalize_text(text) if text else ""
        with self.batch_transaction():
            self._conn.execute(
                """
                INSERT INTO interaction_log (
                    created_at, engine, account_id, action, target_post_id,
                    target_author, target_stance, purpose, normalized_text, text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    engine,
                    account_id,
                    action,
                    target_post_id,
                    target_author,
                    target_stance,
                    purpose,
                    norm,text,
                ),
            )
            self._commit()

    def publication_counts(self, accounts) -> dict[str, int]:
        """Successful posts and replies per account (initialise posts included)."""
        accounts=[a for a in accounts if a]
        if not accounts:
            return {}
        marks=','.join('?'*len(accounts))
        with self._lock:
            rows=self._conn.execute(f"SELECT account_id,COUNT(*) FROM interaction_log WHERE action IN ('post','reply') "
                                    f"AND account_id IN ({marks}) GROUP BY account_id",accounts).fetchall()
        return {r[0]:int(r[1]) for r in rows}

    def has_replied_to_post(self, target_post_id: str) -> bool:
        """Return True if any of our accounts has already replied to target_post_id."""
        if not target_post_id:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM interaction_log WHERE target_post_id = ? AND action = 'reply' LIMIT 1",
                (target_post_id,),
            ).fetchone()
            return row is not None

    def has_normalized_text(self, text: str) -> bool:
        """Return True if the normalized text has already been published or replied."""
        norm = normalize_text(text)
        if not norm:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM interaction_log WHERE normalized_text = ? LIMIT 1",
                (norm,),
            ).fetchone()
            return row is not None

    def count_recent_allied_replies(
        self,
        window_s: float = 3600.0,
        *,
        now: float | None = None,
    ) -> int:
        """Count how many replies to aligned posts were sent within the sliding time window."""
        current_ts = time.time() if now is None else now
        cutoff = current_ts - window_s
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM interaction_log
                WHERE action = 'reply' AND target_stance = 'aligned' AND created_at >= ?
                """,
                (cutoff,),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def check_allied_reply_gate(
        self,
        target_post_id: str,
        target_author: str,
        bot_account_ids: set[str],
        *,
        every_n: int = 3,
        hourly_cap: int = 3,
        window_s: float = 3600.0,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Evaluate the 3-level valve deciding whether an aligned post gets a P01 reply or downgrades to LIKE.

        1. Anti-echo-chamber guard: Never reply to our own bot accounts, and never reply twice to the same post.
        2. Sliding hourly cap: At most `hourly_cap` allied replies per hour across the fleet.
        3. Modulo sampling counter: Only 1 out of every `every_n` qualifying allied posts triggers a reply.
        """
        if target_author and target_author in bot_account_ids:
            return False, "own_bot_post_like_only"

        if target_post_id and self.has_replied_to_post(target_post_id):
            return False, "already_replied_to_post_like_only"

        recent_count = self.count_recent_allied_replies(window_s=window_s, now=now)
        if recent_count >= hourly_cap:
            return False, f"allied_hourly_cap_reached:{recent_count}/{hourly_cap}"

        step_n = max(1, every_n)
        # Stable sampling does not mutate live counters during model evaluation/dry-run.
        seen_count = int(hashlib.sha256(target_post_id.encode()).hexdigest()[:12],16)+1
        if (seen_count - 1) % step_n != 0:
            return False, f"allied_modulo_throttled:{seen_count}%{step_n}"

        return True, f"allied_reply_approved:{seen_count}"

    # ---- Stage 3: Full-Platform Post Mirror (observed_posts) ------------- #
    @staticmethod
    def _unpack_observed_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["is_reported"] = bool(d.get("is_reported", 0))
        try:
            d["raw"] = json.loads(d.get("raw_json") or "{}")
        except (ValueError, TypeError):
            d["raw"] = {}
        return d

    def upsert_observed_post(
        self,
        post: dict[str, Any] | Any,
        *,
        now: float | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Insert or update an observed platform post in SQLite (indexed columns + lossless raw_json).

        Returns `(is_new, post_record_dict)`.
        - If `post_id` was not previously in `observed_posts`, `is_new` is True.
        - If `post_id` was already recorded, updates engagement counts (`likes`, `replies`, `reposts`),
          `last_seen_at`, and `raw_json` in place, and returns `is_new=False`.
        """
        ts = time.time() if now is None else float(now)
        if isinstance(post, dict):
            post_id = str(post.get("post_id", "")).strip()
            author = str(post.get("author") or post.get("username") or "").strip()
            text = str(post.get("text") if post.get("text") is not None else (post.get("body") or ""))
            created_at = float(post.get("created_at", 0.0) or 0.0)
            likes = int(post.get("likes", post.get("like_count", 0)) or 0)
            replies = int(post.get("replies", post.get("reply_count", 0)) or 0)
            reposts = int(post.get("reposts", post.get("repost_count", 0)) or 0)
            raw_dict = dict(post)
        else:
            post_id = str(getattr(post, "post_id", "")).strip()
            author = str(getattr(post, "author", "")).strip()
            text = str(getattr(post, "text", ""))
            created_at = float(getattr(post, "created_at", 0.0) or 0.0)
            likes = int(getattr(post, "likes", 0) or 0)
            replies = int(getattr(post, "replies", 0) or 0)
            reposts = int(getattr(post, "reposts", 0) or 0)
            raw_attr = getattr(post, "raw", None)
            raw_dict = dict(raw_attr) if isinstance(raw_attr, dict) and raw_attr else {
                "post_id": post_id,
                "author": author,
                "text": text,
                "created_at": created_at,
                "likes": likes,
                "replies": replies,
                "reposts": reposts,
                "hashtags": list(getattr(post, "hashtags", ()) or ()),
            }

        if not post_id:
            raise ValueError("upsert_observed_post requires a non-empty post_id")

        author_id = 0
        raw_author_id = raw_dict.get("author_id")
        if raw_author_id is not None:
            try:
                author_id = max(0, int(raw_author_id))
            except (TypeError, ValueError):
                author_id = 0
        if not author and author_id > 0:
            author = str(author_id)

        norm_hash = compute_post_norm_hash(text)
        raw_json = json.dumps(raw_dict, ensure_ascii=False, sort_keys=True)

        # Official `capturethenarrative` (Legit Social v0.5.0) paginates `/posts/{id}/replies`
        # using `Page.next_cursor` (passed back as `cursor=`). `comment_cursor` stores this
        # opaque cursor string directly.
        parent_id = ""
        for key in (
            "reply_to_post_id",
            "parent_id",
            "root_post_id",
            "in_reply_to",
            "reply_to_id",
            "in_reply_to_post_id",
        ):
            candidate = raw_dict.get(key)
            if candidate is not None and str(candidate).strip():
                parent_id = str(candidate).strip()
                break
        if parent_id == post_id:
            parent_id = ""
        root_post_id=str(raw_dict.get('root_post_id') or '')
        if not root_post_id and parent_id:
            parent=self.get_observed_post(parent_id)
            root_post_id=str((parent or {}).get('root_post_id') or parent_id)


        with self.batch_transaction():
            existing = self._conn.execute(
                "SELECT * FROM observed_posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()
            is_new = existing is None
            prev_replies = 0 if existing is None else int(existing["replies"])
            changed = existing is not None and text != str(existing["text"])
            version = 1 if is_new else int(existing["version"]) + int(changed)
            if is_new:
                self._conn.execute(
                    """
                    INSERT INTO observed_posts (
                        post_id, author, author_id, created_at, first_seen_at, last_seen_at,
                        norm_hash, text, likes, replies, reposts, is_reported, raw_json,
                        parent_id, root_post_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        post_id,
                        author,
                        author_id,
                        created_at,
                        ts,
                        ts,
                        norm_hash,
                        text,
                        likes,
                        replies,
                        reposts,
                        raw_json,
                        parent_id, root_post_id,
                    ),
                )
                record: dict[str, Any] = {
                    "post_id": post_id,
                    "author": author,
                    "author_id": author_id,
                    "created_at": created_at,
                    "first_seen_at": ts,
                    "last_seen_at": ts,
                    "norm_hash": norm_hash,
                    "text": text,
                    "likes": likes,
                    "replies": replies,
                    "reposts": reposts,
                    "is_reported": False,
                    "raw_json": raw_json,
                    "parent_id": parent_id,
                    "replies_crawled": 0,
                    "comment_cursor": "",
                    "comments_crawled_at": 0.0,
                    "raw": raw_dict,
                }
            else:
                eff_author = author if author != "" else str(existing["author"])
                eff_author_id = author_id if author_id > 0 else int(existing["author_id"])
                eff_parent_id = parent_id if parent_id != "" else str(existing["parent_id"])
                self._conn.execute(
                    """
                    UPDATE observed_posts SET
                        author = ?,
                        author_id = ?,
                        text = ?, norm_hash = ?, version = ?, created_at = ?,
                        last_seen_at = ?,
                        likes = ?,
                        replies = ?,
                        reposts = ?,
                        raw_json = ?,
                        parent_id = ?, root_post_id = ?
                    WHERE post_id = ?
                    """,
                    (
                        eff_author,
                        eff_author_id,
                        text, norm_hash, version, created_at if created_at > 0 else float(existing["created_at"]),
                        ts,
                        likes,
                        replies,
                        reposts,
                        raw_json,
                        eff_parent_id,root_post_id or str(existing["root_post_id"]),
                        post_id,
                    ),
                )
                record = {
                    "post_id": post_id,
                    "author": eff_author,
                    "author_id": eff_author_id,
                    "created_at": created_at if created_at>0 else float(existing["created_at"]),
                    "first_seen_at": float(existing["first_seen_at"]),
                    "last_seen_at": ts,
                    "norm_hash": norm_hash,
                    "text": text,
                    "likes": likes,
                    "replies": replies,
                    "reposts": reposts,
                    "is_reported": bool(existing["is_reported"]),
                    "raw_json": raw_json,
                    "parent_id": eff_parent_id,
                    "replies_crawled": int(existing["replies_crawled"]),
                    "comment_cursor": str(existing["comment_cursor"]),
                    "comments_crawled_at": float(existing["comments_crawled_at"]),
                    "raw": raw_dict,
                }
            if changed:
                if str(existing['author'])==record['author']:
                    delta=int(text.rstrip().endswith('?'))-int(str(existing['text']).rstrip().endswith('?'))
                    self._conn.execute('UPDATE bot_registry SET question_post_count=MAX(0,question_post_count+?) WHERE account_id=?',(delta,record['author']))
                    self._conn.execute("DELETE FROM bot_evidence WHERE account_id=? AND evidence_key=? AND EXISTS "
                        "(SELECT 1 FROM bot_registry WHERE account_id=? AND question_post_count<>post_seen_count)",
                        (record['author'],'questions:'+record['author'],record['author']))
                self.invalidate_post_evidence(post_id)
                self._recompute_evidence(record["author"])
            record["root_post_id"]=root_post_id
            record["version"] = version
            record["content_changed"] = changed
            if is_new or changed:
                self.enqueue_post(post_id, version, now=ts, post={**record,'root_post_id':root_post_id})
            elif (likes,replies,reposts)!=(int(existing['likes']),int(existing['replies']),int(existing['reposts'])):
                self.refresh_task_audience({**record,'root_post_id':root_post_id}, now=ts)
            if replies > int(record.get("replies_crawled", 0)):
                self._conn.execute('INSERT INTO comment_queue(post_id,priority,next_fetch_at) VALUES(?,?,?) '
                    'ON CONFLICT(post_id) DO UPDATE SET priority=excluded.priority',
                    (post_id, replies-int(record.get("replies_crawled",0)), float(record.get("comments_crawled_at",0))))
            self._commit()
            # Layer-1 hint for the background comment crawler: how many replies appeared since
            # the previous sighting, and how many are still not crawled in total.
            record["replies_delta"] = max(0, replies - prev_replies) if not is_new else replies
            record["unseen_replies"] = max(0, int(record["replies"]) - int(record["replies_crawled"]))
            return is_new, record

    # ---- Stage 3: Two-Layer Crawl Queue (comment pagination state) ------- #
    def get_posts_needing_comment_crawl(
        self,
        *,
        limit: int = 10,
        min_unseen_replies: int = 1,
        cooldown_s: float = 30.0,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return top-level posts whose comment sections have uncrawled replies, hottest first.

        Ordering: largest `unseen_replies` first, then oldest `comments_crawled_at` (starvation guard).
        Posts crawled within `cooldown_s` are skipped.
        """
        ts = time.time() if now is None else float(now)
        args = (max(1, min_unseen_replies), ts - max(0.0, cooldown_s))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT p.*, q.priority AS unseen_replies
                FROM comment_queue q JOIN observed_posts p ON p.post_id=q.post_id
                WHERE q.priority >= ? AND q.next_fetch_at <= ?
                ORDER BY q.priority DESC LIMIT ?
                """,
                (*args, max(1, limit)),
            ).fetchall()
            # Threads under our own posts, verified authors or trending posts usually have few unseen
            # replies but the most valuable readers; never let one huge thread hide them.
            special = self._conn.execute(
                """
                SELECT p.*, q.priority AS unseen_replies
                FROM comment_queue q JOIN observed_posts p ON p.post_id=q.post_id
                WHERE q.priority >= ? AND q.next_fetch_at <= ?
                  AND (p.author IN (SELECT account_id FROM bot_registry WHERE status IN ('own_bot','verified_npc'))
                       OR p.post_id IN (SELECT post_id FROM trending_seen WHERE seen_at >= ?))
                ORDER BY q.next_fetch_at LIMIT ?
                """,
                (*args, ts - 1800, max(1, limit)),
            ).fetchall()
            seen = set()
            out = []
            for r in [*rows, *special]:
                if r['post_id'] in seen:
                    continue
                seen.add(r['post_id'])
                rec = self._unpack_observed_row(r)
                rec["unseen_replies"] = int(r["unseen_replies"])
                out.append(rec)
            return out

    def update_comment_crawl_state(
        self,
        post_id: str,
        *,
        replies_crawled: int | None = None,
        add_crawled: int = 0,
        comment_cursor: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Persist comment pagination progress for a top-level post after the worker fetches a page.

        Pass `replies_crawled` to set an absolute count, or `add_crawled` to increment. `comment_cursor`
        stores the opaque page token / page number / last comment id for resuming without re-reading.
        """
        ts = time.time() if now is None else float(now)
        with self.batch_transaction():
            cur = self._conn.execute(
                "SELECT replies_crawled, comment_cursor FROM observed_posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()
            if cur is None:
                return None
            new_count = (
                max(0, int(replies_crawled))
                if replies_crawled is not None
                else int(cur["replies_crawled"]) + max(0, int(add_crawled))
            )
            new_cursor = str(cur["comment_cursor"]) if comment_cursor is None else str(comment_cursor)
            self._conn.execute(
                """
                UPDATE observed_posts SET
                    replies_crawled = ?, comment_cursor = ?, comments_crawled_at = ?
                WHERE post_id = ?
                """,
                (new_count, new_cursor, ts, post_id),
            )
            self._conn.execute('DELETE FROM comment_queue WHERE post_id=?', (post_id,))
            remaining = self._conn.execute('SELECT replies-replies_crawled FROM observed_posts WHERE post_id=?',(post_id,)).fetchone()[0]
            if remaining>0:
                self._conn.execute('INSERT INTO comment_queue VALUES(?,?,?)',(post_id,remaining,ts))
            self._commit()
            row = self._conn.execute(
                "SELECT * FROM observed_posts WHERE post_id = ?", (post_id,)
            ).fetchone()
            return self._unpack_observed_row(row)

    def get_post_replies(self, parent_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return already-crawled comments under `parent_id`, oldest first."""
        if not parent_id:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM observed_posts
                WHERE parent_id = ? OR root_post_id = ?
                ORDER BY created_at ASC, first_seen_at ASC LIMIT ?
                """,
                (parent_id,parent_id, max(1, limit)),
            ).fetchall()
            return [self._unpack_observed_row(r) for r in rows]

    def get_observed_post(self, post_id: str) -> dict[str, Any] | None:
        """Fetch a single observed post by ID, or None if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM observed_posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()
            return self._unpack_observed_row(row) if row is not None else None

    def get_author_posts(
        self,
        author: str,
        *,
        limit: int = 20,
        unreported_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Retrieve observed posts by a specific author ordered newest first (via B-Tree index)."""
        if not author:
            return []
        with self._lock:
            if unreported_only:
                rows = self._conn.execute(
                    """
                    SELECT * FROM observed_posts
                    WHERE author = ? AND is_reported = 0
                    ORDER BY first_seen_at DESC LIMIT ?
                    """,
                    (author, max(1, limit)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM observed_posts
                    WHERE author = ?
                    ORDER BY first_seen_at DESC LIMIT ?
                    """,
                    (author, max(1, limit)),
                ).fetchall()
            return [self._unpack_observed_row(r) for r in rows]

    def find_norm_hash_collisions(
        self,
        norm_hash: str,
        *,
        exclude_post_id: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Find previously observed posts sharing the exact same normalized core fingerprint."""
        if not norm_hash:
            return []
        with self._lock:
            if exclude_post_id:
                rows = self._conn.execute(
                    """
                    SELECT * FROM observed_posts
                    WHERE norm_hash = ? AND post_id != ?
                    ORDER BY first_seen_at DESC LIMIT ?
                    """,
                    (norm_hash, exclude_post_id, max(1, limit)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM observed_posts
                    WHERE norm_hash = ?
                    ORDER BY first_seen_at DESC LIMIT ?
                    """,
                    (norm_hash, max(1, limit)),
                ).fetchall()
            return [self._unpack_observed_row(r) for r in rows]

    def get_confirmed_bot_posts(
        self,
        *,
        exclude_author: str = "",
        suspended_only: bool = False,
        limit: int = 150,
    ) -> list[dict[str, Any]]:
        """Retrieve observed posts authored by already-confirmed or suspended/deleted enemy bots.

        Prioritizes posts from permanently suspended (`is_suspended = 1`) bots first so newly
        registered replacement ("reincarnated") accounts reusing a banned bot's script are caught
        immediately on their first post.
        """
        with self._lock:
            if suspended_only:
                rows = self._conn.execute(
                    """
                    SELECT p.*,
                           b.is_suspended AS author_is_suspended,
                           b.inferred_team AS author_inferred_team,
                           b.inferred_side AS author_inferred_side
                    FROM observed_posts p
                    INNER JOIN bot_registry b ON p.author = b.account_id
                    WHERE b.is_suspended = 1 AND p.author != ?
                    ORDER BY p.first_seen_at DESC LIMIT ?
                    """,
                    (exclude_author, max(1, limit)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT p.*,
                           b.is_suspended AS author_is_suspended,
                           b.inferred_team AS author_inferred_team,
                           b.inferred_side AS author_inferred_side
                    FROM observed_posts p
                    INNER JOIN bot_registry b ON p.author = b.account_id
                    WHERE (b.status = 'confirmed_bot' OR b.is_suspended = 1)
                      AND p.author != ?
                    ORDER BY b.is_suspended DESC, p.first_seen_at DESC LIMIT ?
                    """,
                    (exclude_author, max(1, limit)),
                ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                rec = self._unpack_observed_row(r)
                rec["author_is_suspended"] = bool(r["author_is_suspended"])
                rec["author_inferred_team"] = str(r["author_inferred_team"] or "")
                rec["author_inferred_side"] = str(r["author_inferred_side"] or "unknown")
                out.append(rec)
            return out

    def get_unconfirmed_candidate_posts(
        self,
        *,
        exclude_author: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Retrieve recent observed posts by accounts that are not yet confirmed bots or protected.

        Used during reporting sweeps to retroactively scan for sibling/reincarnated accounts whose
        phrasing closely matches a confirmed or suspended bot's posts.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT p.*
                FROM observed_posts p
                LEFT JOIN bot_registry b ON p.author = b.account_id
                WHERE COALESCE(b.status, 'unknown') IN ('unknown', 'suspected_bot')
                  AND p.author != ''
                  AND p.author != ?
                ORDER BY p.first_seen_at DESC LIMIT ?
                """,
                (exclude_author, max(1, limit)),
            ).fetchall()
            return [self._unpack_observed_row(r) for r in rows]

    # ---- Stage 3: Bot Intelligence Registry (bot_registry) --------------- #
    @staticmethod
    def _unpack_bot_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["is_suspended"] = bool(d.get("is_suspended", 0))
        try:
            d["evidence"] = json.loads(d.get("evidence_json") or "[]")
        except (ValueError, TypeError):
            d["evidence"] = []
        return d

    def register_verified_accounts(
        self,
        account_ids: Iterable[str | dict[str, Any]],
        *,
        status: str = "verified_npc",
        now: float | None = None,
    ) -> None:
        """Register protected accounts ('verified_npc' or 'own_bot') so they are never flagged or reported.

        Each item in `account_ids` may be a username string or a dict containing
        `username`/`handle`/`account_id` and an optional integer `account_id`/`platform_account_id`.
        """
        if status not in ("verified_npc", "own_bot"):
            raise ValueError("status must be 'verified_npc' or 'own_bot'")
        ts = time.time() if now is None else float(now)
        with self.batch_transaction():
            for acc in account_ids:
                platform_id = 0
                if isinstance(acc, dict):
                    acc_id = str(
                        acc.get("username") or acc.get("handle") or acc.get("account_id") or ""
                    ).strip()
                    raw_pid = acc.get("platform_account_id")
                    if raw_pid is None:
                        raw_pid = acc.get("account_id") if isinstance(acc.get("account_id"), int) else None
                    if raw_pid is not None:
                        try:
                            platform_id = max(0, int(raw_pid))
                        except (TypeError, ValueError):
                            platform_id = 0
                else:
                    acc_id = str(acc).strip()
                if not acc_id:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO bot_registry (
                        account_id, platform_account_id, status, suspicion_score,
                        first_seen_at, last_seen_at, evidence_json
                    ) VALUES (?, ?, ?, 0.0, ?, ?, '["protected_whitelist"]')
                    ON CONFLICT(account_id) DO UPDATE SET
                        platform_account_id = CASE
                            WHEN excluded.platform_account_id > 0 THEN excluded.platform_account_id
                            ELSE bot_registry.platform_account_id
                        END,
                        status = excluded.status,
                        verified = CASE WHEN excluded.status='verified_npc' THEN 1 ELSE bot_registry.verified END,
                        suspicion_score = 0.0,
                        evidence_json = '["protected_whitelist"]'
                    """,
                    (acc_id, platform_id, status, ts, ts),
                )
                if status == 'verified_npc':
                    self._conn.execute('UPDATE bot_registry SET verified=1 WHERE account_id=?',(acc_id,))
            self._commit()

    def get_bot_profile(self, account_id: str) -> dict[str, Any]:
        """Fetch an account's intelligence profile from bot_registry (returns default unknown profile if absent)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bot_registry WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                return {
                    "account_id": account_id,
                    "platform_account_id": 0,
                    "status": "unknown", "verified": False, "true_team_id": "", "evidence_groups": 0,
                    "suspicion_score": 0.0,
                    "inferred_side": "unknown",
                    "inferred_team": "",
                    "post_seen_count": 0,
                    "question_post_count": 0,
                    "fast_interval_count": 0,
                    "reports_submitted": 0,
                    "upheld_strikes": 0,
                    "is_suspended": False,
                    "first_seen_at": 0.0,
                    "last_seen_at": 0.0,
                    "evidence_json": "[]",
                    "evidence": [],
                }
            return self._unpack_bot_row(row)

    def record_author_observation(
        self,
        account_id: str,
        *,
        platform_account_id: int | None = None,
        is_question: bool = False,
        inferred_side: str = "",
        inferred_team: str = "",
        suspicion_delta: float = 0.0,
        evidence_tag: str = "",
        force_status: str = "",
        promote_threshold: float = 90.0,
        suspect_threshold: float = 50.0,
        min_bot_interval_s: float = 15.0,
        has_valid_timestamp: bool = True,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Update an author's behavioral metrics and suspicion score in bot_registry.

        Protected accounts ('verified_npc' and 'own_bot') are never downgraded or assigned suspicion.
        Automatically promotes 'unknown' -> 'suspected_bot' -> 'confirmed_bot' when thresholds are met.
        """
        ts = time.time() if now is None else float(now)
        with self.batch_transaction():
            prof = self.get_bot_profile(account_id)
            cur_status = prof["status"]
            pid = (
                max(0, int(platform_account_id))
                if platform_account_id is not None and int(platform_account_id) > 0
                else int(prof.get("platform_account_id") or 0)
            )
            if cur_status in ("verified_npc", "own_bot"):
                if pid > 0 and int(prof.get("platform_account_id") or 0) != pid:
                    self._conn.execute(
                        "UPDATE bot_registry SET platform_account_id = ? WHERE account_id = ?",
                        (pid, account_id),
                    )
                    self._commit()
                    prof["platform_account_id"] = pid
                return prof

            prev_last = float(prof["last_seen_at"] or 0.0)
            first_seen = (
                min(float(prof["first_seen_at"]), ts)
                if prof["first_seen_at"] > 0
                else ts
            )
            last_seen = max(prev_last, ts) if prev_last > 0 else ts
            post_count = int(prof["post_seen_count"]) + 1
            q_count = int(prof["question_post_count"]) + (1 if is_question else 0)
            fast_count = int(prof["fast_interval_count"])
            if has_valid_timestamp and prev_last > 0 and abs(ts - prev_last) <= min_bot_interval_s:
                fast_count += 1

            if has_valid_timestamp:
                prev = self._conn.execute('SELECT MAX(created_at) FROM observed_posts WHERE author=? AND created_at>0 AND created_at<?', (account_id, ts)).fetchone()[0]
                nxt = self._conn.execute('SELECT MIN(created_at) FROM observed_posts WHERE author=? AND created_at>?', (account_id, ts)).fetchone()[0]
                delta = int(prev is not None and ts-prev<=min_bot_interval_s) + int(nxt is not None and nxt-ts<=min_bot_interval_s)
                if prev is not None and nxt is not None and nxt-prev<=min_bot_interval_s:
                    delta -= 1
                fast_count = max(int(prof['fast_interval_count']), int(prof['fast_interval_count']) + delta)

            evidence: list[str] = list(prof["evidence"])
            if evidence_tag and evidence_tag not in evidence:
                evidence.append(evidence_tag)

            score = max(0.0, float(prof["suspicion_score"]) + float(suspicion_delta))

            new_side = inferred_side if inferred_side in ("yes", "no") else prof["inferred_side"]
            new_team = inferred_team if inferred_team else prof["inferred_team"]

            new_status = cur_status
            if force_status in ("confirmed_bot", "suspected_bot", "verified_npc", "own_bot"):
                new_status = force_status
                if new_status == "confirmed_bot" and score < promote_threshold:
                    score = max(score, promote_threshold)
            else:
                if score >= promote_threshold or cur_status == "confirmed_bot":
                    new_status = "confirmed_bot"
                elif score >= suspect_threshold or cur_status == "suspected_bot":
                    new_status = "suspected_bot"

            rounded_score = round(score, 2)
            ev_json = json.dumps(evidence, ensure_ascii=False)
            self._conn.execute(
                """
                INSERT INTO bot_registry (
                    account_id, platform_account_id, status, suspicion_score,
                    inferred_side, inferred_team, post_seen_count, question_post_count,
                    fast_interval_count, reports_submitted, upheld_strikes, is_suspended,
                    first_seen_at, last_seen_at, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    platform_account_id = CASE
                        WHEN excluded.platform_account_id > 0 THEN excluded.platform_account_id
                        ELSE bot_registry.platform_account_id
                    END,
                    status = excluded.status,
                    suspicion_score = excluded.suspicion_score,
                    inferred_side = excluded.inferred_side,
                    inferred_team = excluded.inferred_team,
                    post_seen_count = excluded.post_seen_count,
                    question_post_count = excluded.question_post_count,
                    fast_interval_count = excluded.fast_interval_count,
                    first_seen_at = excluded.first_seen_at,
                    last_seen_at = excluded.last_seen_at,
                    evidence_json = excluded.evidence_json
                """,
                (
                    account_id,
                    pid,
                    new_status,
                    rounded_score,
                    new_side,
                    new_team,
                    post_count,
                    q_count,
                    fast_count,
                    int(prof["reports_submitted"]),
                    int(prof["upheld_strikes"]),
                    int(prof["is_suspended"]),
                    first_seen,
                    last_seen,
                    ev_json,
                ),
            )
            self._commit()
            return {
                "account_id": account_id,
                "platform_account_id": pid,
                "status": new_status,
                "suspicion_score": rounded_score,
                "inferred_side": new_side,
                "inferred_team": new_team,
                "post_seen_count": post_count,
                "question_post_count": q_count,
                "fast_interval_count": fast_count,
                "reports_submitted": int(prof["reports_submitted"]),
                "upheld_strikes": int(prof["upheld_strikes"]),
                "is_suspended": bool(prof["is_suspended"]),
                "first_seen_at": first_seen,
                "last_seen_at": last_seen,
                "evidence_json": ev_json,
                "evidence": evidence,
            }

    def add_bot_evidence(
        self,
        account_id: str,
        *,
        platform_account_id: int | None = None,
        suspicion_delta: float = 0.0,
        evidence_tags: Iterable[str] = (),
        inferred_side: str = "",
        inferred_team: str = "",
        force_status: str = "",
        promote_threshold: float = 90.0,
        suspect_threshold: float = 50.0,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Add suspicion score and evidence tags to an account in `bot_registry` without incrementing `post_seen_count`."""
        ts = time.time() if now is None else float(now)
        with self.batch_transaction():
            prof = self.get_bot_profile(account_id)
            cur_status = prof["status"]
            pid = (
                max(0, int(platform_account_id))
                if platform_account_id is not None and int(platform_account_id) > 0
                else int(prof.get("platform_account_id") or 0)
            )
            if cur_status in ("verified_npc", "own_bot"):
                return prof

            evidence: list[str] = list(prof["evidence"])
            for tag in evidence_tags:
                t = str(tag).strip()
                if t and t not in evidence:
                    evidence.append(t)

            score = max(0.0, float(prof["suspicion_score"]) + float(suspicion_delta))
            new_side = inferred_side if inferred_side in ("yes", "no") else prof["inferred_side"]
            new_team = inferred_team if inferred_team else prof["inferred_team"]

            new_status = cur_status
            if force_status in ("confirmed_bot", "suspected_bot", "verified_npc", "own_bot"):
                new_status = force_status
                if new_status == "confirmed_bot" and score < promote_threshold:
                    score = max(score, promote_threshold)
            else:
                if score >= promote_threshold or cur_status == "confirmed_bot":
                    new_status = "confirmed_bot"
                elif score >= suspect_threshold or cur_status == "suspected_bot":
                    new_status = "suspected_bot"

            first_seen = float(prof["first_seen_at"]) if float(prof["first_seen_at"]) > 0 else ts
            last_seen = max(float(prof["last_seen_at"] or 0.0), ts)
            rounded_score = round(score, 2)
            ev_json = json.dumps(evidence, ensure_ascii=False)
            self._conn.execute(
                """
                INSERT INTO bot_registry (
                    account_id, platform_account_id, status, suspicion_score,
                    inferred_side, inferred_team, post_seen_count, question_post_count,
                    fast_interval_count, reports_submitted, upheld_strikes, is_suspended,
                    first_seen_at, last_seen_at, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    platform_account_id = CASE
                        WHEN excluded.platform_account_id > 0 THEN excluded.platform_account_id
                        ELSE bot_registry.platform_account_id
                    END,
                    status = excluded.status,
                    suspicion_score = excluded.suspicion_score,
                    inferred_side = excluded.inferred_side,
                    inferred_team = excluded.inferred_team,
                    first_seen_at = excluded.first_seen_at,
                    last_seen_at = excluded.last_seen_at,
                    evidence_json = excluded.evidence_json
                """,
                (
                    account_id,
                    pid,
                    new_status,
                    rounded_score,
                    new_side,
                    new_team,
                    int(prof["post_seen_count"]),
                    int(prof["question_post_count"]),
                    int(prof["fast_interval_count"]),
                    int(prof["reports_submitted"]),
                    int(prof["upheld_strikes"]),
                    int(prof["is_suspended"]),
                    first_seen,
                    last_seen,
                    ev_json,
                ),
            )
            self._commit()
            return {
                "account_id": account_id,
                "platform_account_id": pid,
                "status": new_status,
                "suspicion_score": rounded_score,
                "inferred_side": new_side,
                "inferred_team": new_team,
                "post_seen_count": int(prof["post_seen_count"]),
                "question_post_count": int(prof["question_post_count"]),
                "fast_interval_count": int(prof["fast_interval_count"]),
                "reports_submitted": int(prof["reports_submitted"]),
                "upheld_strikes": int(prof["upheld_strikes"]),
                "is_suspended": bool(prof["is_suspended"]),
                "first_seen_at": first_seen,
                "last_seen_at": last_seen,
                "evidence_json": ev_json,
                "evidence": evidence,
            }

    def list_bots_by_status(self, status: str) -> list[dict[str, Any]]:
        """List all accounts in bot_registry matching `status`, ordered by suspicion_score descending."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bot_registry WHERE status = ? ORDER BY suspicion_score DESC",
                (status,),
            ).fetchall()
            return [self._unpack_bot_row(r) for r in rows]

    # ---- Stage 3: Blue-Team Reporting & Circuit Breaker (report_log) ----- #
    def set_report_suspension(
        self,
        duration_s: float = 21600.0,
        *,
        now: float | None = None,
    ) -> float:
        """Activate the 6-hour (21,600s) reporting circuit breaker after an incorrect citizen report."""
        ts = time.time() if now is None else float(now)
        until_ts = ts + max(0.0, duration_s)
        self.set_counter("report_suspended_until_ms", int(until_ts * 1000))
        return until_ts

    def clear_report_suspension(self) -> None:
        """Clear the reporting suspension circuit breaker."""
        self.set_counter("report_suspended_until_ms", 0)

    def is_reporting_suspended(self, *, now: float | None = None) -> tuple[bool, float]:
        """Return `(is_suspended, remaining_seconds)` for the 6-hour false-report penalty window."""
        ts = time.time() if now is None else float(now)
        until_ms = self.get_counter("report_suspended_until_ms")
        if until_ms <= 0:
            return False, 0.0
        until_s = until_ms / 1000.0
        rem = until_s - ts
        if rem > 0:
            return True, round(rem, 1)
        return False, 0.0

    def count_daily_reports_against(
        self,
        *,
        target_team: str = "",
        target_author: str = "",
        sydney_day: str | None = None,
        now: float | None = None,
    ) -> int:
        """Count non-rejected reports filed today (Sydney time) against a specific competitor team or author."""
        day_str = sydney_day or current_sydney_day(now)
        with self._lock:
            if target_team:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM report_log
                    WHERE ruling_day = ? AND target_team = ? AND outcome = 'upheld'
                    """,
                    (day_str, target_team),
                ).fetchone()
            elif target_author:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM report_log
                    WHERE ruling_day = ? AND target_author = ? AND outcome = 'upheld'
                    """,
                    (day_str, target_author),
                ).fetchone()
            else:
                return 0
            return int(row["cnt"]) if row else 0

    def can_report_post(
        self,
        post_id: str,
        target_author: str,
        *,
        target_team: str = "",
        max_daily_per_team: int = 10,
        max_strikes_per_account: int = 3,
        sydney_day: str | None = None,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Check all Blue-Team safety and ROI guards before submitting an inauthentic post report:

        1. 6-hour false-report suspension circuit breaker.
        2. Protected whitelist ('verified_npc' or 'own_bot').
        3. Duplicate report guard on the same `post_id`.
        4. 3-strike account suspension cap (an account is permanently suspended after 3 upheld reports).
        5. Daily diminishing-return quota per target team (1st-5th full rate, 6th-10th half rate, >10 only 10%).
        """
        if not post_id or not target_author:
            return False, "missing_post_or_author"

        suspended, rem_s = self.is_reporting_suspended(now=now)
        if suspended:
            return False, f"reporting_suspended:{rem_s}s"

        prof = self.get_bot_profile(target_author)
        if prof["status"] in ("verified_npc", "own_bot"):
            return False, f"protected_account:{prof['status']}"

        with self._lock:
            dup = self._conn.execute(
                "SELECT 1 FROM report_log WHERE post_id = ? LIMIT 1",
                (post_id,),
            ).fetchone()
            if dup is not None:
                return False, "post_already_reported"

        if prof["is_suspended"] or int(prof["upheld_strikes"]) >= max_strikes_per_account:
            return False, f"account_already_3_strike_suspended:{prof['upheld_strikes']}"

        return True, "eligible_to_report"

    def log_report(
        self,
        post_id: str,
        target_author: str,
        *,
        target_team: str = "",
        target_side: str = "unknown",
        reporter_account: str = "",
        reason: str = "",
        outcome: str = "pending",
        sydney_day: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Record a submitted report in `report_log`, mark `observed_posts.is_reported = 1`, and update `bot_registry`."""
        ts = time.time() if now is None else float(now)
        day_str = sydney_day or current_sydney_day(ts)
        with self.batch_transaction():
            self._conn.execute("INSERT OR IGNORE INTO bot_registry(account_id,status) VALUES(?,'unknown')",(target_author,))
            cur=self._conn.execute("INSERT OR IGNORE INTO report_log(post_id,target_author,target_team,target_side,"
                "reporter_account,reason,sydney_day,created_at,outcome) VALUES(?,?,?,?,?,?,?,?,'pending')",
                (post_id,target_author,target_team,target_side,reporter_account,reason,day_str,ts))
            if not cur.rowcount:
                return False
            self._conn.execute('UPDATE observed_posts SET is_reported=1 WHERE post_id=?',(post_id,))
            self._conn.execute('UPDATE bot_registry SET reports_submitted=reports_submitted+1 WHERE account_id=?',(target_author,))
            if outcome != 'pending':
                self.update_report_outcome(post_id,outcome,now=ts)
            return True

    def update_report_outcome(self, post_id, outcome, *, now=None):
        if outcome == 'pending':
            return self.get_bot_profile(self.get_observed_post(post_id)['author']) if self.get_observed_post(post_id) else None
        if outcome not in ('upheld','rejected','dismissed'):
            raise ValueError('invalid report outcome')
        ts=time.time() if now is None else float(now)
        with self._lock:
            row=self._conn.execute('SELECT target_author FROM report_log WHERE post_id=?',(post_id,)).fetchone()
        if row is None:
            return None
        self.apply_moderation_event({'event_id':'legacy:'+post_id, 'subject_kind':'post','subject_id':post_id,
            'outcome':outcome,'ruled_at':ts,'source':'explicit-caller'})
        return self.get_bot_profile(row[0])
