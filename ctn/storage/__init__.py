"""SQLite-backed state store for accounts, interaction logs, and atomic counters."""

from .sqlite_store import DEFAULT_DB_PATH, StateStore

__all__ = ["DEFAULT_DB_PATH", "StateStore"]
