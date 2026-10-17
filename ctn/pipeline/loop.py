"""ACT, and the loop that ties read -> react -> act together.

One cycle:

    perceive()  reads trending + keyword searches       -> Observation
    decide()    one Decision per new post               -> [Decision]
    execute     top-N actionable decisions, one bot each -> [ActionResult]

The loop owns cross-cycle memory (posts already acted on, recent texts, which
account goes next); the stages themselves stay stateless.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Sequence

from ..actuator import Actuator
from ..client.base import LegitClient
from ..config import Settings
from ..models import ActionKind, ActionResult, Decision, GuardVerdict, Post, Side, to_jsonable
from ..moderation import BlueTeamReporter, BotDetector, ReportBatchResult
from ..ratelimit import build_gate, build_ledger
from ..storage import StateStore
from ..telemetry import Telemetry
from .brain import JevLlmBrain
from .compose import check_text
from .crawl import CommentCrawler, CommentReport, FrontlineCrawler, FrontlineReport, run_periodic
from .decide import decide
from .perceive import DEFAULT_KEYWORDS, perceive

# How many recent published texts to remember for the repeat check.
RECENT_TEXTS = 200


@dataclass
class CycleReport:
    cycle: int
    observed: int = 0
    decisions: list[Decision] = field(default_factory=list)
    results: list[ActionResult] = field(default_factory=list)
    frontline: FrontlineReport | None = None
    moderation: ReportBatchResult | None = None

    @property
    def status(self) -> str:
        if not self.observed and (self.moderation is None or self.moderation.submitted_count == 0):
            return "idle"
        if any(r.ok for r in self.results) or (
            self.moderation is not None and self.moderation.submitted_count > 0
        ):
            return "acted"
        return "observed"


@dataclass
class CommentStepReport:
    comment_crawl: CommentReport
    moderation: ReportBatchResult | None = None


class BotLoop:
    def __init__(
        self,
        actuator: Actuator,
        *,
        side: Side,
        accounts: Sequence[str],
        keywords: Sequence[str] = DEFAULT_KEYWORDS,
        max_actions_per_cycle: int = 3,
        brain: "JevLlmBrain | None" = None,
        max_evaluations_per_cycle: int = 8,
        store: StateStore | None = None,
        verified_npcs: Sequence[str] = (),
        max_daily_reports_per_team: int = 10,
        telemetry: Telemetry | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if not accounts:
            raise ValueError("BotLoop needs at least one account")
        self.actuator = actuator
        self.side = side
        self.accounts = list(accounts)
        self.keywords = tuple(keywords)
        self.max_actions_per_cycle = max_actions_per_cycle
        # With a brain, JEV + LLM replace the keyword heuristics. Each evaluated
        # post costs JEV calls (and possibly one LLM call), so evaluation is capped.
        self.brain = brain
        self.max_evaluations_per_cycle = max_evaluations_per_cycle
        self._telemetry = telemetry
        self._rng = rng or random.Random()
        self._own = set(self.accounts)
        self._acted: set[str] = set()       # post ids we already acted on
        self._judged: set[str] = set()      # post ids the brain ruled out; never pay to re-judge
        self._recent_texts: list[str] = []
        self._next = 0
        self.cycle = 0

        # Stage 3: Full-platform SQLite mirror, two-layer crawlers, detector, and reporter.
        self.store: StateStore = store if store is not None else StateStore(":memory:")
        self.store.register_verified_accounts(self.accounts, status="own_bot")
        if verified_npcs:
            self.store.register_verified_accounts(verified_npcs, status="verified_npc")

        self.detector = BotDetector(self.store)
        self.reporter = BlueTeamReporter(
            self.actuator,
            self.store,
            accounts=self.accounts,
            side=self.side,
            max_daily_per_team=max_daily_reports_per_team,
        )
        self.frontline_crawler = FrontlineCrawler(
            self.actuator,
            self.store,
            feed_account=self.accounts[0],
            accounts=self.accounts,
            keywords=self.keywords,
            detector=self.detector,
        )
        self.comment_crawler = CommentCrawler(
            self.actuator,
            self.store,
            detector=self.detector,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        client: LegitClient,
        *,
        accounts: Sequence[str] | None = None,
        telemetry: Telemetry | None = None,
        **kwargs,
    ) -> "BotLoop":
        actuator = Actuator(
            client,
            build_gate(settings.rate),
            build_ledger(settings.budget),
            telemetry=telemetry,
            dry_run=settings.dry_run,
            max_retries=settings.platform.max_retries,
        )
        if accounts is None:
            brain_arg = kwargs.get("brain")
            if brain_arg is not None and hasattr(brain_arg, "router"):
                persona_accts = [
                    p.account_id
                    for p in brain_arg.router.registry.list_by_side(settings.side.value)
                ]
                if persona_accts:
                    accounts = persona_accts
            if not accounts:
                accounts = [f"bot_{i:03d}" for i in range(1, settings.fleet.bot_accounts + 1)]
        return cls(actuator, side=settings.side, accounts=accounts, telemetry=telemetry, **kwargs)

    # ---- shared REACT + ACT pipeline for candidate posts ----------------- #
    def _is_confirmed_enemy_bot(self, author: str) -> bool:
        if not author:
            return False
        return self.store.get_bot_profile(author)["status"] == "confirmed_bot"

    async def _react_and_execute(self, posts: Sequence[Post], report: CycleReport) -> None:
        candidates = [
            p
            for p in posts
            if p.post_id not in self._acted
            and p.author not in self._own
            and not self._is_confirmed_enemy_bot(p.author)
        ]
        if self.brain is not None:
            batch = [p for p in candidates if p.post_id not in self._judged][
                : self.max_evaluations_per_cycle
            ]
            report.decisions = list(
                await asyncio.gather(*(self.brain.decide(p, commit_state=False) for p in batch))
            )
            # A clean "no action" verdict is final. Errors and temporary cooldowns are retried next cycle.
            self._judged.update(
                d.post_id
                for d in report.decisions
                if d.action is ActionKind.NONE
                and d.scores.ok
                and not d.rationale.startswith("account_cooldown:")
            )
            actionable = [d for d in report.decisions if d.action is not ActionKind.NONE]
            # Replies are the scarce write; likes are cheap. Keep JEV's order otherwise.
            actionable.sort(key=lambda d: d.action is not ActionKind.REPLY)
        else:
            report.decisions = [
                decide(p, side=self.side, keywords=self.keywords, rng=self._rng)
                for p in candidates
            ]
            actionable = [d for d in report.decisions if d.action is not ActionKind.NONE]
            actionable.sort(key=lambda d: d.scores.score("relevance"), reverse=True)

        # ACT
        executed_count = 0
        for decision in actionable:
            if executed_count >= self.max_actions_per_cycle:
                break
            account, skip_reason = self._resolve_execution_account(decision)
            if skip_reason:
                decision.rationale += f" | {skip_reason}"
                self._log_decision(decision)
                continue
            if account is None:
                break
            decision.account = account
            if (
                decision.text
                and check_text(decision.text, self._recent_texts) is GuardVerdict.BLOCK
            ):
                decision.rationale += " | blocked by text check"
                self._log_decision(decision)
                continue
            self._log_decision(decision)
            result = await self._execute(decision)
            report.results.append(result)
            executed_count += 1
            if result.ok:
                if self.brain is not None:
                    self.brain.commit_decision(decision, account=decision.account)
                self._remember(decision, result)

    # ---- Stage 1/2 multi-query cycle (kept for backward compatibility) --- #
    async def run_cycle(self) -> CycleReport:
        self.cycle += 1
        report = CycleReport(cycle=self.cycle)
        throttles_before = self.actuator.throttles

        obs = await perceive(self.actuator, keywords=self.keywords)
        report.observed = len(obs.posts)
        await self._react_and_execute(obs.posts, report)

        if self.actuator.throttles == throttles_before:
            self.actuator.recover()
        return report

    # ---- Stage 3: Layer-1 1.5s Frontline Step + Layer-2 2.5s Comment Step - #
    async def run_frontline_step(self, *, now: float | None = None) -> CycleReport:
        """Execute one 1.5s Layer-1 frontline tick:
        1. Single read via `FrontlineCrawler.step()` -> SQLite `observed_posts` + `BotDetector`.
        2. Filter out already-known posts and confirmed enemy bots, then run JEV/LLM reaction on new posts.
        3. Sweep and report any `confirmed_bot` accounts via `BlueTeamReporter`.
        """
        self.cycle += 1
        report = CycleReport(cycle=self.cycle)
        throttles_before = self.actuator.throttles

        front = await self.frontline_crawler.step(now=now)
        report.frontline = front
        report.observed = len(front.new_posts)

        if front.new_posts:
            await self._react_and_execute(front.new_posts, report)

        priority_authors = [d.author for d in front.detections if d.is_confirmed_bot]
        report.moderation = await self.reporter.sweep_and_report(
            priority_authors=priority_authors, now=now
        )

        if self.actuator.throttles == throttles_before:
            self.actuator.recover()
        return report

    async def run_comment_step(self, *, now: float | None = None) -> CommentStepReport:
        """Execute one 2.5s Layer-2 background comment crawl tick:
        1. Single `get_comments` read on highest-priority thread with uncrawled replies.
        2. Run `BotDetector` on newly discovered comments (catching booster swarms & reply bots).
        3. Sweep and report any newly confirmed bots (retroactive 3-strike takedown).
        """
        c_rep = await self.comment_crawler.step(now=now)
        mod_batch: ReportBatchResult | None = None
        if c_rep.new_comments:
            priority_authors = [d.author for d in c_rep.detections if d.is_confirmed_bot]
            mod_batch = await self.reporter.sweep_and_report(
                priority_authors=priority_authors, now=now
            )
        return CommentStepReport(comment_crawl=c_rep, moderation=mod_batch)

    async def run_forever(
        self, *, interval_s: float = 30.0, stop: asyncio.Event | None = None
    ) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            report = await self.run_cycle()
            if self._telemetry is not None:
                self._telemetry.metric(
                    "cycle", cycle=report.cycle, status=report.status,
                    observed=report.observed, actions=len(report.results),
                    ok=sum(r.ok for r in report.results),
                    **self.actuator.ledger.snapshot(),
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass

    async def run_two_layer_forever(
        self,
        *,
        frontline_interval_s: float = 1.5,
        comment_interval_s: float = 2.5,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Run Layer-1 frontline stream (1.5s) and Layer-2 comment deep crawler (2.5s) concurrently."""
        stop = stop or asyncio.Event()

        async def _on_frontline(rep: CycleReport) -> None:
            if self._telemetry is not None:
                self._telemetry.metric(
                    "frontline_step",
                    cycle=rep.cycle,
                    status=rep.status,
                    observed=rep.observed,
                    actions=len(rep.results),
                    reports_submitted=rep.moderation.submitted_count if rep.moderation else 0,
                    **self.actuator.ledger.snapshot(),
                )

        async def _on_comment(rep: CommentStepReport) -> None:
            if self._telemetry is not None and not rep.comment_crawl.skipped:
                self._telemetry.metric(
                    "comment_step",
                    post_id=rep.comment_crawl.post_id,
                    new_comments=len(rep.comment_crawl.new_comments),
                    done=rep.comment_crawl.done,
                    reports_submitted=rep.moderation.submitted_count if rep.moderation else 0,
                    **self.actuator.ledger.snapshot(),
                )

        await asyncio.gather(
            run_periodic(
                self.run_frontline_step,
                interval_s=frontline_interval_s,
                stop=stop,
                on_result=_on_frontline,
            ),
            run_periodic(
                self.run_comment_step,
                interval_s=comment_interval_s,
                stop=stop,
                on_result=_on_comment,
            ),
        )

    # ---- internals ------------------------------------------------------- #
    async def _execute(self, d: Decision) -> ActionResult:
        a = self.actuator
        if d.action is ActionKind.LIKE:
            return await a.like(d.account, d.post_id, decision_id=d.decision_id)
        if d.action is ActionKind.REPOST:
            return await a.repost(d.account, d.post_id, decision_id=d.decision_id)
        if d.action is ActionKind.REPLY:
            root_id = d.root_post_id or str(d.scores.raw.get("root_post_id") or "")
            return await a.reply(
                d.account,
                d.post_id,
                d.text,
                root_post_id=root_id or None,
                decision_id=d.decision_id,
            )
        if d.action is ActionKind.POST:
            return await a.post(d.account, d.text, decision_id=d.decision_id)
        if d.action is ActionKind.FOLLOW:
            return await a.follow(d.account, d.post_id, decision_id=d.decision_id)
        if d.action is ActionKind.UNFOLLOW:
            return await a.unfollow(d.account, d.post_id, decision_id=d.decision_id)
        if d.action is ActionKind.REPORT:
            return await a.report(d.account, d.post_id, reason=d.rationale, decision_id=d.decision_id)
        raise ValueError(f"no executor for {d.action!r}")

    def _resolve_execution_account(self, d: Decision) -> tuple[str | None, str]:
        """Honor the persona account routed by JevLlmBrain when valid; otherwise round-robin."""
        routed = (d.account or "").strip()
        if routed and routed != "bot_default" and routed in self._own:
            if routed in self.actuator.suspended:
                return None, f"routed_account_suspended:{routed}"
            if self.brain is not None and d.action is ActionKind.REPLY:
                import time as _time
                persona = self.brain.router.registry.get(routed)
                state = self.brain.router.get_state(routed)
                if state.is_disabled():
                    return None, f"routed_account_disabled:{routed}"
                if persona is not None and state.is_in_cooldown(_time.time(), persona.cooldown_s):
                    return None, f"routed_account_cooldown:{routed}"
            return routed, ""
        return self._next_account(), ""

    def _next_account(self) -> str | None:
        """Round-robin over accounts that are not suspended."""
        for _ in range(len(self.accounts)):
            account = self.accounts[self._next % len(self.accounts)]
            self._next += 1
            if account not in self.actuator.suspended:
                return account
        return None

    def _remember(self, d: Decision, result: ActionResult) -> None:
        self._acted.add(d.post_id)
        new_id = str(result.response.get("post_id", ""))
        if new_id:
            self._acted.add(new_id)     # never react to our own reply
        if d.text:
            self._recent_texts.append(d.text)
            del self._recent_texts[:-RECENT_TEXTS]

    def _log_decision(self, d: Decision) -> None:
        if self._telemetry is not None:
            self._telemetry.decisions.write(to_jsonable(d))

