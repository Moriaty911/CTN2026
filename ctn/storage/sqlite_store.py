"""Lightweight SQLite state store supporting multi-process WAL concurrency.

Maintains three core tables:
1. account_states: Per-account cooldown timestamps and post/reply counters.
2. interaction_log: Complete audit and deduplication log across macro and micro engines.
3. counters: Atomic integer counters for modulo sampling (e.g. allied reply throttling).
"""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any

from ctn.persona.models import AccountState

DEFAULT_DB_PATH = ".ctn_state.db"

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
"""


def normalize_text(text: str) -> str:
    """Normalize text for duplicate detection."""
    cleaned = text.strip().lower()
    if not cleaned:
        return ""
    return re.sub(r"\s+", " ", cleaned)


class StateStore:
    """SQLite-backed store for account cooldowns, suspension status, interaction history, and rate gates."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.executescript(_SCHEMA_SQL)
            self._ensure_columns()
            self._conn.commit()

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

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ---- Account States -------------------------------------------------- #
    def get_account_state(self, account_id: str) -> AccountState:
        """Fetch or initialize account state from SQLite."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT account_id, last_posted_at, post_count, is_frozen, is_banned
                FROM account_states WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO account_states (
                        account_id, last_posted_at, post_count, is_frozen, is_banned
                    ) VALUES (?, 0.0, 0, 0, 0)
                    """,
                    (account_id,),
                )
                self._conn.commit()
                return AccountState(account_id=account_id)
            return AccountState(
                account_id=str(row["account_id"]),
                last_posted_at=float(row["last_posted_at"]),
                post_count=int(row["post_count"]),
                is_frozen=bool(row["is_frozen"]),
                is_banned=bool(row["is_banned"]),
            )

    def save_account_state(
        self,
        state: AccountState,
        *,
        target_stance: str = "",
    ) -> None:
        """Upsert an AccountState into SQLite, optionally incrementing stance reply counters."""
        allied_inc = 1 if target_stance == "aligned" else 0
        opposing_inc = 1 if target_stance == "opposing" else 0
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO account_states (
                    account_id, last_posted_at, post_count,
                    allied_reply_count, opposing_reply_count,
                    is_frozen, is_banned
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    last_posted_at = excluded.last_posted_at,
                    post_count = excluded.post_count,
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
            self._conn.commit()

    def set_account_frozen(self, account_id: str, frozen: bool = True) -> AccountState:
        """Mark an account as temporarily frozen (or unfreeze it)."""
        state = self.get_account_state(account_id)
        state.is_frozen = bool(frozen)
        self.save_account_state(state)
        return state

    def set_account_banned(self, account_id: str, banned: bool = True) -> AccountState:
        """Mark an account as permanently banned (or unban it)."""
        state = self.get_account_state(account_id)
        state.is_banned = bool(banned)
        self.save_account_state(state)
        return state

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
            rows = self._conn.execute(
                "SELECT account_id, last_posted_at, post_count, is_frozen, is_banned FROM account_states"
            ).fetchall()
            return {
                str(r["account_id"]): AccountState(
                    account_id=str(r["account_id"]),
                    last_posted_at=float(r["last_posted_at"]),
                    post_count=int(r["post_count"]),
                    is_frozen=bool(r["is_frozen"]),
                    is_banned=bool(r["is_banned"]),
                )
                for r in rows
            }

    def reset_accounts(self) -> None:
        """Clear all account states, interaction logs, and counters."""
        with self._lock:
            self._conn.execute("DELETE FROM account_states;")
            self._conn.execute("DELETE FROM interaction_log;")
            self._conn.execute("DELETE FROM counters;")
            self._conn.commit()

    # ---- Atomic Counters ------------------------------------------------- #
    def increment_counter(self, name: str, delta: int = 1) -> int:
        """Atomically increment a named integer counter and return its new value."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO counters (name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = counters.value + ?
                """,
                (name, delta, delta),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT value FROM counters WHERE name = ?",
                (name,),
            ).fetchone()
            return int(row["value"]) if row else delta

    def get_counter(self, name: str) -> int:
        """Return the current value of a named counter (0 if not set)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM counters WHERE name = ?",
                (name,),
            ).fetchone()
            return int(row["value"]) if row else 0

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
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO interaction_log (
                    created_at, engine, account_id, action, target_post_id,
                    target_author, target_stance, purpose, normalized_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    norm,
                ),
            )
            self._conn.commit()

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
        seen_count = self.increment_counter("allied_qualifying_seen", 1)
        if (seen_count - 1) % step_n != 0:
            return False, f"allied_modulo_throttled:{seen_count}%{step_n}"

        return True, f"allied_reply_approved:{seen_count}"
