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
from ..models import ActionKind, ActionResult, Decision, GuardVerdict, Side, to_jsonable
from ..ratelimit import build_gate, build_ledger
from ..telemetry import Telemetry
from .compose import check_text
from .brain import JevLlmBrain
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

    @property
    def status(self) -> str:
        if not self.observed:
            return "idle"
        return "acted" if any(r.ok for r in self.results) else "observed"


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
            accounts = [f"bot_{i:03d}" for i in range(1, settings.fleet.bot_accounts + 1)]
        return cls(actuator, side=settings.side, accounts=accounts, telemetry=telemetry, **kwargs)

    # ---- one cycle ------------------------------------------------------- #
    async def run_cycle(self) -> CycleReport:
        self.cycle += 1
        report = CycleReport(cycle=self.cycle)
        throttles_before = self.actuator.throttles

        # READ
        obs = await perceive(self.actuator, keywords=self.keywords)
        report.observed = len(obs.posts)

        # REACT
        candidates = [
            p for p in obs.posts
            if p.post_id not in self._acted and p.author not in self._own
        ]
        if self.brain is not None:
            batch = [p for p in candidates if p.post_id not in self._judged][: self.max_evaluations_per_cycle]
            report.decisions = list(await asyncio.gather(*(self.brain.decide(p) for p in batch)))
            # A clean "no action" verdict is final. Errors are retried next cycle.
            self._judged.update(
                d.post_id for d in report.decisions if d.action is ActionKind.NONE and d.scores.ok
            )
            actionable = [d for d in report.decisions if d.action is not ActionKind.NONE]
            # Replies are the scarce write; likes are cheap. Keep JEV's order otherwise.
            actionable.sort(key=lambda d: d.action is not ActionKind.REPLY)
        else:
            report.decisions = [
                decide(p, side=self.side, keywords=self.keywords, rng=self._rng) for p in candidates
            ]
            actionable = [d for d in report.decisions if d.action is not ActionKind.NONE]
            actionable.sort(key=lambda d: d.scores.score("relevance"), reverse=True)

        # ACT
        for decision in actionable[: self.max_actions_per_cycle]:
            account = self._next_account()
            if account is None:
                break
            decision.account = account
            if decision.text and check_text(decision.text, self._recent_texts) is GuardVerdict.BLOCK:
                decision.rationale += " | blocked by text check"
                self._log_decision(decision)
                continue
            self._log_decision(decision)
            result = await self._execute(decision)
            report.results.append(result)
            if result.ok:
                self._remember(decision, result)

        if self.actuator.throttles == throttles_before:
            self.actuator.recover()
        return report

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

    # ---- internals ------------------------------------------------------- #
    async def _execute(self, d: Decision) -> ActionResult:
        a = self.actuator
        if d.action is ActionKind.LIKE:
            return await a.like(d.account, d.post_id, decision_id=d.decision_id)
        if d.action is ActionKind.REPOST:
            return await a.repost(d.account, d.post_id, decision_id=d.decision_id)
        if d.action is ActionKind.REPLY:
            return await a.reply(d.account, d.post_id, d.text, decision_id=d.decision_id)
        if d.action is ActionKind.POST:
            return await a.post(d.account, d.text, decision_id=d.decision_id)
        if d.action is ActionKind.FOLLOW:
            return await a.follow(d.account, d.post_id, decision_id=d.decision_id)
        if d.action is ActionKind.UNFOLLOW:
            return await a.unfollow(d.account, d.post_id, decision_id=d.decision_id)
        raise ValueError(f"no executor for {d.action!r}")

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
