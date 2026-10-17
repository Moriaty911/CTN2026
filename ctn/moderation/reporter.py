"""Blue-Team Reporter: Retroactive 3-strike takedowns, Sydney-day quota hoarding, and 6h circuit breaker.

Scoring & safety rules implemented (CTN 2026):
- First report on a confirmed bot post: +25 Blue points (2nd-5th: +5 points).
- 3 upheld reports permanently suspend a competitor bot account (`max_strikes_per_account = 3`).
- Diminishing returns per target team per Sydney day (`Australia/Sydney` 00:00 reset):
    * 1st-5th reports against Team X today: 100% rate (+25 pts)
    * 6th-10th reports against Team X today: 50% rate (+12.5 pts)
    * 11th+ reports against Team X today: 10% rate (+2.5 pts)
  By default, `max_daily_per_team = 10` hoards any excess confirmed bots beyond 10 for the next
  Sydney midnight reset, turning a 2.5-pt report today into a 25-pt report tomorrow.
- 6-hour false-positive circuit breaker: if any report returns `outcome == "rejected"`, reporting
  is immediately halted for 21,600 seconds and the target author is whitelisted as `verified_npc`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..actuator import Actuator
from ..models import ActionResult, Side
from ..storage import StateStore, current_sydney_day
from .detector import BotDetector


@dataclass
class ReportAttempt:
    post_id: str
    target_author: str
    target_team: str
    reporter_account: str
    reason: str
    ok: bool
    outcome: str = "pending"           # "upheld" | "rejected" | "pending" | "skipped:<reason>"
    action_result: ActionResult | None = None


@dataclass
class ReportBatchResult:
    sydney_day: str
    suspended: bool = False
    suspended_remaining_s: float = 0.0
    attempts: list[ReportAttempt] = field(default_factory=list)
    hoarded_for_tomorrow: list[str] = field(default_factory=list)

    @property
    def submitted_count(self) -> int:
        return sum(1 for a in self.attempts if a.ok)

    @property
    def upheld_count(self) -> int:
        return sum(1 for a in self.attempts if a.outcome == "upheld")


class BlueTeamReporter:
    """Executes verified Blue-Team reports with 3-strike history sweeps, on-demand `/accounts/{id}/posts`
    backfill, 3/min `report_post` rate pacing, and daily team quota optimization.
    """

    def __init__(
        self,
        actuator: Actuator,
        store: StateStore,
        *,
        accounts: Sequence[str],
        side: Side = Side.UNASSIGNED,
        max_daily_per_team: int = 10,
        max_strikes_per_account: int = 3,
        max_reports_per_step: int = 3,
        max_reports_per_minute: int | None = None,
        backfill_account_posts: bool = True,
        backfill_cooldown_s: float = 60.0,
        detector: BotDetector | None = None,
    ) -> None:
        if not accounts:
            raise ValueError("BlueTeamReporter requires at least one reporter account")
        self.actuator = actuator
        self.store = store
        self.accounts = list(accounts)
        self.side = side
        self.max_daily_per_team = max_daily_per_team
        self.max_strikes_per_account = max_strikes_per_account
        self.max_reports_per_step = max(1, max_reports_per_step)
        if max_reports_per_minute is None:
            is_official = actuator._client.__class__.__name__ == "OfficialLegitClient"
            self.max_reports_per_minute = 3 if is_official else 0
        else:
            self.max_reports_per_minute = max(0, int(max_reports_per_minute))
        self.backfill_account_posts = backfill_account_posts
        self.backfill_cooldown_s = max(0.0, float(backfill_cooldown_s))
        self.detector: BotDetector = detector if detector is not None else BotDetector(store)
        self._next_idx = 0
        self._report_timestamps: list[float] = []
        self._backfilled_at: dict[str, float] = {}

    def _next_account(self) -> str | None:
        """Round-robin across non-suspended bot accounts."""
        for _ in range(len(self.accounts)):
            acc = self.accounts[self._next_idx % len(self.accounts)]
            self._next_idx += 1
            if acc not in self.actuator.suspended:
                state = self.store.get_account_state(acc)
                if not state.is_banned and not state.is_frozen:
                    return acc
        return None

    def _remaining_minute_budget(self, now: float) -> int:
        """Return how many `report_post` calls are allowed right now under the rolling 60s bucket."""
        if self.max_reports_per_minute <= 0:
            return self.max_reports_per_step
        cutoff = now - 60.0
        self._report_timestamps = [t for t in self._report_timestamps if t > cutoff and t <= now + 1e-6]
        return max(0, self.max_reports_per_minute - len(self._report_timestamps))

    def _candidate_priority(
        self,
        prof: dict[str, Any],
        *,
        sydney_day: str,
        now: float,
    ) -> tuple[int, int, float]:
        """Sort key (lower tuple sorts first):
        1. Diminishing-return tier for `inferred_team` today:
           - tier 0: 0..4 reports today (100% full 25-pt reward)
           - tier 1: 5..9 reports today (50% 12.5-pt reward)
           - tier 2: >=10 reports today (capped/hoarded)
        2. Opposing referendum side first (0 for opponent side, 1 for unknown/same side).
        3. Highest suspicion_score first (`-suspicion_score`).
        """
        team = str(prof.get("inferred_team") or "")
        daily_cnt = (
            self.store.count_daily_reports_against(
                target_team=team, sydney_day=sydney_day, now=now
            )
            if team
            else 0
        )
        tier = 0 if daily_cnt < 5 else (1 if daily_cnt < 10 else 2)
        opponent_side = self.side.opponent.value if self.side is not Side.UNASSIGNED else ""
        is_opponent = 0 if (opponent_side and prof.get("inferred_side") == opponent_side) else 1
        return (tier, is_opponent, -float(prof.get("suspicion_score") or 0.0))

    async def _backfill_author_posts_if_needed(
        self,
        author: str,
        needed_strikes: int,
        *,
        now: float,
    ) -> list[dict[str, Any]]:
        """Ensure we have at least `needed_strikes` unreported posts for `author` in `observed_posts`.

        If local `observed_posts` has fewer than `needed_strikes` unreported posts for a `confirmed_bot`,
        calls `actuator.get_account_posts(author, limit=10)` (300/min read bucket) at most once per
        `backfill_cooldown_s` to fetch the bot's historical posts so we can complete a 3-strike takedown.
        """
        unreported = self.store.get_author_posts(
            author, limit=needed_strikes, unreported_only=True
        )
        if len(unreported) >= needed_strikes or not self.backfill_account_posts:
            return unreported
        if getattr(self.actuator._client, "get_account_posts", None) is None:
            return unreported
        last_fetch = self._backfilled_at.get(author)
        if last_fetch is not None and (now - last_fetch) < self.backfill_cooldown_s:
            return unreported

        self._backfilled_at[author] = now
        fetch_limit = max(10, needed_strikes * 3)
        remote_posts = await self.actuator.get_account_posts(author, limit=fetch_limit)
        for rp in remote_posts:
            if isinstance(rp, dict) and rp.get("post_id"):
                rp_author = str(rp.get("author") or rp.get("username") or author).strip()
                if rp_author == author:
                    self.store.upsert_observed_post(rp, now=now)
        return self.store.get_author_posts(
            author, limit=needed_strikes, unreported_only=True
        )

    async def sweep_and_report(
        self,
        *,
        priority_authors: Sequence[str] = (),
        sydney_day: str | None = None,
        now: float | None = None,
    ) -> ReportBatchResult:
        """Scan `confirmed_bot` accounts and submit up to `max_reports_per_step` high-ROI reports.

        For each confirmed bot, pulls up to `3 - reports_submitted` unreported posts/comments from
        `observed_posts` (backfilling via `get_account_posts` if fewer than 3 are stored locally).
        Halts immediately if the 6-hour circuit breaker trips.
        """
        ts = time.time() if now is None else float(now)
        day_str = sydney_day or current_sydney_day(ts)
        batch = ReportBatchResult(sydney_day=day_str)

        suspended, rem_s = self.store.is_reporting_suspended(now=ts)
        if suspended:
            batch.suspended = True
            batch.suspended_remaining_s = rem_s
            return batch

        step_budget = min(self.max_reports_per_step, self._remaining_minute_budget(ts))
        if step_budget <= 0:
            return batch

        confirmed_profiles = self.store.list_bots_by_status("confirmed_bot")
        by_handle: dict[str, dict[str, Any]] = {
            str(p["account_id"]): p for p in confirmed_profiles
        }
        for handle in priority_authors:
            h = str(handle).strip()
            if h and h not in by_handle:
                prof = self.store.get_bot_profile(h)
                if prof["status"] == "confirmed_bot":
                    by_handle[h] = prof

        # Retroactively scan `observed_posts` for any unconfirmed sibling/reincarnated account
        # using phrasing highly similar to our confirmed or suspended/deleted bots.
        for seed_handle in list(by_handle.keys()):
            promoted_siblings = self.detector.scan_similar_to_author(seed_handle, now=ts)
            if promoted_siblings:
                by_handle[seed_handle] = self.store.get_bot_profile(seed_handle)
                for sib in promoted_siblings:
                    by_handle[sib] = self.store.get_bot_profile(sib)

        # Filter to accounts that still need strikes to reach permanent 3-strike suspension
        remaining_candidates = [
            p
            for p in by_handle.values()
            if not p.get("is_suspended")
            and int(p.get("upheld_strikes") or 0) < self.max_strikes_per_account
            and int(p.get("reports_submitted") or 0) < self.max_strikes_per_account
        ]

        reports_sent = 0
        while remaining_candidates and reports_sent < step_budget:
            remaining_candidates.sort(
                key=lambda p: self._candidate_priority(p, sydney_day=day_str, now=ts)
            )
            prof = remaining_candidates.pop(0)

            author = str(prof["account_id"])
            team = str(prof.get("inferred_team") or "")
            side_str = str(prof.get("inferred_side") or "unknown")
            evidence_list = prof.get("evidence") or []
            reason = ",".join(str(e) for e in evidence_list[:3]) or "confirmed_automated_bot"

            # Check if this team's daily high-ROI quota is already full today
            if team and self.max_daily_per_team > 0:
                daily_cnt = self.store.count_daily_reports_against(
                    target_team=team, sydney_day=day_str, now=ts
                )
                if daily_cnt >= self.max_daily_per_team:
                    batch.hoarded_for_tomorrow.append(author)
                    continue

            needed_strikes = max(
                0, self.max_strikes_per_account - int(prof.get("reports_submitted") or 0)
            )
            if needed_strikes <= 0:
                continue

            unreported_posts = await self._backfill_author_posts_if_needed(
                author, needed_strikes, now=ts
            )
            for post_rec in unreported_posts:
                if reports_sent >= step_budget:
                    break

                post_id = str(post_rec["post_id"])
                can_rep, gate_reason = self.store.can_report_post(
                    post_id,
                    author,
                    target_team=team,
                    max_daily_per_team=self.max_daily_per_team,
                    max_strikes_per_account=self.max_strikes_per_account,
                    sydney_day=day_str,
                    now=ts,
                )
                if not can_rep:
                    if gate_reason.startswith("daily_team_quota_reached"):
                        batch.hoarded_for_tomorrow.append(author)
                        break
                    if gate_reason.startswith("reporting_suspended"):
                        batch.suspended = True
                        _, batch.suspended_remaining_s = self.store.is_reporting_suspended(now=ts)
                        return batch
                    continue

                reporter_acc = self._next_account()
                if reporter_acc is None:
                    return batch

                res = await self.actuator.report(reporter_acc, post_id, reason=reason)
                if not res.ok:
                    batch.attempts.append(
                        ReportAttempt(
                            post_id=post_id,
                            target_author=author,
                            target_team=team,
                            reporter_account=reporter_acc,
                            reason=reason,
                            ok=False,
                            outcome=f"failed:{res.detail}",
                            action_result=res,
                        )
                    )
                    continue

                reports_sent += 1
                self._report_timestamps.append(ts)
                raw_outcome = str(res.response.get("outcome") or "pending").lower()
                initial_outcome = (
                    raw_outcome if raw_outcome in ("upheld", "rejected", "pending") else "pending"
                )
                self.store.log_report(
                    post_id,
                    author,
                    target_team=team,
                    target_side=side_str,
                    reporter_account=reporter_acc,
                    reason=reason,
                    outcome="pending",
                    sydney_day=day_str,
                    now=ts,
                )
                if initial_outcome in ("upheld", "rejected"):
                    self.store.update_report_outcome(post_id, initial_outcome, now=ts)

                batch.attempts.append(
                    ReportAttempt(
                        post_id=post_id,
                        target_author=author,
                        target_team=team,
                        reporter_account=reporter_acc,
                        reason=reason,
                        ok=True,
                        outcome=initial_outcome,
                        action_result=res,
                    )
                )

                # If the moderator rejected the report, the 6-hour circuit breaker is now active!
                if initial_outcome == "rejected":
                    batch.suspended = True
                    _, batch.suspended_remaining_s = self.store.is_reporting_suspended(now=ts)
                    return batch

        return batch
