"""Two-level, read/write-separated rate limiting and request budgeting.

Why this module exists
----------------------
The naive bot architecture is a loop that fires requests as fast as it can and
sleeps on 429. That fails here for three reasons:

1.  Reads are metered too. The 2025 published telemetry implies roughly
    0.61 requests/second per team across four weeks. Perception is therefore a
    *spend decision*, not a free operation, and a bot that polls greedily has no
    budget left to act when a trend appears.

2.  Reads and writes must not share a bucket. A burst of feed reading that
    exhausts the budget will prevent us from publishing into the exact trending
    window we were reading in order to find. Separate buckets make that
    structurally impossible.

3.  Rate limits are per-account as well as per-team (LS.5.1: limits vary by
    trust signals and recent activity). One hot account must not be able to
    consume the whole team's allowance, and a low-trust account must not be
    driven at the same speed as a warmed one.

The limiter uses virtual-time token buckets. Waiting tasks reserve future
tokens under a lock, so the pacing is fair (FIFO-ish) and no task busy-waits.
Cancellation loses the reservation, which errs toward politeness - the correct
failure direction against a platform that can suspend us.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .config import RateConfig


class TokenBucket:
    """Async token bucket with virtual-time reservation.

    `rate` is tokens per second, `capacity` the burst size in tokens.
    """

    __slots__ = ("rate", "capacity", "_tokens", "_updated", "_lock", "waited_s", "acquired")

    def __init__(self, rate: float, capacity: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = max(capacity, 1.0)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self.waited_s = 0.0
        self.acquired = 0

    async def acquire(self, n: float = 1.0) -> float:
        """Block until `n` tokens are available. Returns seconds spent waiting."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated
            if elapsed > 0:
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._updated = now

            if self._tokens >= n:
                self._tokens -= n
                wait = 0.0
            else:
                deficit = n - self._tokens
                wait = deficit / self.rate
                # Reserve into the future so concurrent waiters queue up fairly
                # instead of all waking at once.
                self._tokens = 0.0
                self._updated = now + wait

            self.acquired += 1
            self.waited_s += wait

        if wait > 0:
            await asyncio.sleep(wait)
        return wait

    def slow_down(self, factor: float, max_seconds: float) -> None:
        """Shrink the rate after a platform throttle. Idempotent-ish: applying it
        twice compounds, which is what we want for repeated 429s."""
        floor = 1.0 / max(max_seconds, 1.0)
        self.rate = max(self.rate / factor, floor)
        self.capacity = max(self.capacity / factor, 1.0)
        self._tokens = 0.0
        self._updated = time.monotonic()

    def speed_up(self, factor: float, ceiling: float) -> None:
        """Recover cautiously after a clean interval. Never exceed `ceiling`."""
        self.rate = min(self.rate * factor, ceiling)

    @property
    def effective_rps(self) -> float:
        return self.rate


@dataclass
class GateStats:
    waits: int = 0
    total_wait_s: float = 0.0
    slow_downs: int = 0


class ActionGate:
    """The only sanctioned path to a platform write or read.

    Every platform call goes through `acquire()`. If a call site bypasses this,
    the pacing guarantee is void, so the actuator (and only the actuator) is
    allowed to hold a reference to the platform client.
    """

    READ = "read"
    WRITE = "write"

    def __init__(self, cfg: RateConfig) -> None:
        self._cfg = cfg
        burst = cfg.burst_s
        self._team = {
            self.READ: TokenBucket(cfg.read_rps, cfg.read_rps * burst),
            self.WRITE: TokenBucket(cfg.write_rps, cfg.write_rps * burst),
        }
        self._per_account: dict[tuple[str, str], TokenBucket] = {}
        self._ceilings = {
            self.READ: cfg.read_rps,
            self.WRITE: cfg.write_rps,
        }
        self.stats: dict[str, GateStats] = {
            self.READ: GateStats(),
            self.WRITE: GateStats(),
        }
        self._lock = asyncio.Lock()

    def _account_bucket(self, kind: str, account: str) -> TokenBucket:
        key = (kind, account)
        bucket = self._per_account.get(key)
        if bucket is None:
            rate = (
                self._cfg.per_account_read_rps
                if kind == self.READ
                else self._cfg.per_account_write_rps
            )
            bucket = TokenBucket(rate, max(rate * self._cfg.burst_s, 1.0))
            self._per_account[key] = bucket
        return bucket

    async def acquire(self, kind: str, account: str | None = None) -> float:
        """Wait for capacity on both the team bucket and (if given) the account
        bucket. Returns total seconds waited."""
        if kind not in (self.READ, self.WRITE):
            raise ValueError(f"unknown gate kind: {kind!r}")

        waited = 0.0
        if account is not None:
            # Account bucket first: it is the tighter of the two in practice, so
            # taking it first avoids holding team tokens while queued per-account.
            waited += await self._account_bucket(kind, account).acquire()
        waited += await self._team[kind].acquire()

        stat = self.stats[kind]
        stat.waits += 1
        stat.total_wait_s += waited
        return waited

    def on_throttled(self, kind: str, account: str | None = None) -> None:
        """Called when the platform returns a rate-limit response."""
        self._team[kind].slow_down(self._cfg.penalty_factor, self._cfg.penalty_max_s)
        if account is not None:
            self._account_bucket(kind, account).slow_down(
                self._cfg.penalty_factor, self._cfg.penalty_max_s
            )
        self.stats[kind].slow_downs += 1

    def on_clean_interval(self, kind: str) -> None:
        """Called periodically when we have had no throttling for a while."""
        self._team[kind].speed_up(1.05, self._ceilings[kind])

    def snapshot(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for kind, bucket in self._team.items():
            out[f"team_{kind}_rps"] = round(bucket.effective_rps, 4)
            out[f"team_{kind}_waits"] = self.stats[kind].waits
            out[f"team_{kind}_wait_s"] = round(self.stats[kind].total_wait_s, 2)
        return out


# --------------------------------------------------------------------------- #
# Budget ledger
# --------------------------------------------------------------------------- #
@dataclass
class BudgetLedger:
    """Tracks how much of the platform request allowance we have spent.

    Gives the perception loop permission to shed low-value reads as the budget
    tightens, instead of discovering the ceiling by being cut off.
    """

    total: int
    read_share: float
    warn_at: float
    reads: int = 0
    writes: int = 0

    @property
    def read_cap(self) -> int:
        return int(self.total * self.read_share)

    @property
    def write_cap(self) -> int:
        return self.total - self.read_cap

    @property
    def spent(self) -> int:
        return self.reads + self.writes

    @property
    def read_fraction(self) -> float:
        return self.reads / self.read_cap if self.read_cap else 1.0

    @property
    def write_fraction(self) -> float:
        return self.writes / self.write_cap if self.write_cap else 1.0

    def record(self, kind: str) -> None:
        if kind == ActionGate.READ:
            self.reads += 1
        else:
            self.writes += 1

    @property
    def read_pressure(self) -> float:
        """0.0 = plenty of room, 1.0 = at the warn threshold, >1.0 = over.

        The perception loop uses this to raise its triage bar: as pressure
        rises, only reads likely to produce an action are worth paying for.
        """
        if self.warn_at <= 0:
            return 0.0
        return max(0.0, (self.read_fraction - self.warn_at) / (1.0 - self.warn_at))

    def can_write(self) -> bool:
        return self.write_fraction < 1.0

    def snapshot(self) -> dict[str, float]:
        return {
            "spent": self.spent,
            "reads": self.reads,
            "writes": self.writes,
            "read_fraction": round(self.read_fraction, 4),
            "write_fraction": round(self.write_fraction, 4),
            "read_pressure": round(self.read_pressure, 4),
        }


def build_gate(cfg: RateConfig) -> ActionGate:
    return ActionGate(cfg)


def build_ledger(cfg) -> BudgetLedger:
    return BudgetLedger(
        total=cfg.total_requests,
        read_share=cfg.read_share,
        warn_at=cfg.warn_at,
    )
