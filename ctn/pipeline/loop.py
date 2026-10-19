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
import time
import json
from dataclasses import dataclass, field
from typing import Sequence

from ..actuator import Actuator
from ..client.base import LegitClient
from ..config import Settings
from ..models import ActionKind, ActionResult, Decision, GuardVerdict, Post, Side, Tier, JevAnswers, to_jsonable
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
        self.store = store or (brain.router.store if brain is not None else None) or actuator.store or StateStore(":memory:")
        if actuator.store is not None and actuator.store is not self.store and actuator.store.db_path != self.store.db_path:
            raise ValueError('loop and actuator must share SQLite')
        if brain is not None and brain.router.store is not self.store and brain.router.store.db_path != self.store.db_path:
            raise ValueError('brain and loop must share SQLite')
        actuator.store=self.store
        if brain:actuator.catalog_hash=brain.router.registry.catalog_hash
        self._processing_lock=asyncio.Lock()
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
            feed_account=self.accounts[0] if self.accounts else '',
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
        shared = kwargs.pop('store',None) or getattr(client,'store',None)
        if shared is None and kwargs.get('brain') is not None: shared=kwargs['brain'].router.store
        if shared is None: shared=StateStore(settings.database_path())
        actuator = Actuator(
            client,
            build_gate(settings.rate),
            build_ledger(settings.budget,store=shared),
            telemetry=telemetry,
            dry_run=settings.dry_run,
            max_retries=settings.platform.max_retries,store=shared,
        )
        if accounts is None:
            brain_arg = kwargs.get("brain")
            if brain_arg is not None and hasattr(brain_arg, "router"):
                persona_accts = [
                    p.account_id
                    for p in brain_arg.router.registry.list_by_side(settings.side.value) if p.account_id
                ]
                accounts = persona_accts
            if accounts is None:
                accounts = [f"bot_{i:03d}" for i in range(1, settings.fleet.bot_accounts + 1)]
        return cls(actuator, side=settings.side, accounts=accounts, telemetry=telemetry, store=shared,**kwargs)

    # ---- shared REACT + ACT pipeline for candidate posts ----------------- #
    def _is_confirmed_enemy_bot(self, author: str) -> bool:
        if not author:
            return False
        return self.store.get_bot_profile(author)["status"] == "confirmed_bot"

    async def _react_and_execute(self,posts,report):
        def ingest():
            with self.store.batch_transaction():
                for p in posts: self.store.upsert_observed_post(p)
        await asyncio.to_thread(ingest)
        await self.process_pending(report)

    @staticmethod
    def _restore_decision(data):
        return Decision(decision_id=data['decision_id'],post_id=data['post_id'],tier=Tier(data['tier']),
            account=data['account'],scores=JevAnswers(**data['scores']),rationale=data['rationale'],
            action=ActionKind(data['action']),text=data.get('text',''),root_post_id=data.get('root_post_id',''),disposition=data.get('disposition','auto'),needs=tuple(data.get('needs',())))

    async def process_pending(self,report=None):
        from .perceive import post_from_dict
        from .reply_batches import plan_reply_batch,run_reply_batches
        report=report or CycleReport(cycle=self.cycle)
        async with self._processing_lock:
            self.refresh_accounts()
            if isinstance(self.brain,JevLlmBrain):self.brain.refresh_registered_background()
            corpus_hash=getattr(getattr(self.brain,'corpus',None),'hash','')
            from .context import hydrate_waiting_context
            await hydrate_waiting_context(self.actuator,self.store)
            await asyncio.to_thread(self.store.resume_material_tasks,corpus_hash)
            await run_reply_batches(self,report,self.max_actions_per_cycle)
            tasks=await asyncio.to_thread(self.store.claim_post_tasks,self.max_evaluations_per_cycle)
            if not tasks: return report
            async def renew():
                while True:
                    await asyncio.sleep(20)
                    await asyncio.to_thread(self.store.renew_post_tasks,tasks)
            lease=asyncio.create_task(renew())
            try:
                from .scheduling import ordered_evaluations
                executed=len(report.results)
                async for task,decision in ordered_evaluations(self,tasks):
                    if not await asyncio.to_thread(self.store.post_task_send_ready,task):
                        await asyncio.to_thread(self.store.update_post_task,task,'expired',reason='expired_or_superseded_before_send')
                        continue
                    if decision is None:
                        await asyncio.to_thread(self.store.update_post_task,task,'skipped',reason='own_or_missing_post');continue
                    report.decisions.append(decision)
                    if decision.action is ActionKind.NONE:
                        if decision.disposition=='wait' or decision.rationale.startswith(('needs_materials:','llm:needs_materials','jev:needs_account_metadata','jev:needs_context')):
                            await asyncio.to_thread(self.store.wait_for_material,task,corpus_hash,decision.rationale)
                            continue
                        transient=decision.disposition=='retry' or not decision.scores.ok or decision.rationale.startswith((
                            'account_cooldown:','all_accounts_disabled','needs_materials:','jev:failed','jev:needs','llm:','brain_error'))
                        blocked_material=decision.rationale.startswith(('needs_materials:','llm:needs_materials','llm:needs_review'))
                        cooling=decision.rationale.startswith('account_cooldown:')
                        # Only "every eligible account is cooling down" waits without an attempt cap;
                        # "no eligible account" is retried twice, then dropped instead of re-running JEV until expiry.
                        if transient and task['attempts']>=2 and not cooling:
                            transient=False
                        delay=300 if blocked_material else 30
                        if cooling:
                            delay=min(300.,max(5.,float(decision.scores.raw.get('cooldown_remaining_s') or 30)))
                        await asyncio.to_thread(self.store.update_post_task,task,'queued' if transient else 'skipped',
                            reason=decision.rationale,delay=delay if transient else 0)
                        continue
                    if decision.action is ActionKind.REPLY and await plan_reply_batch(self,task,decision):
                        continue
                    draft=to_jsonable(decision)
                    if executed>=self.max_actions_per_cycle:
                        await asyncio.to_thread(self.store.update_post_task,task,'queued',reason='cycle_action_limit',
                            delay=1,draft=draft,draft_at=task['draft_at']);continue
                    account,reason=self._resolve_execution_account(decision)
                    if reason or account is None:
                        await asyncio.to_thread(self.store.update_post_task,task,'queued',reason=reason or 'no_account',
                            delay=15,draft=draft,draft_at=task['draft_at']);continue
                    decision.account=account
                    decision.scores.raw.update(target_version=task["version"],draft_expires_at=task["draft_at"]+300,opportunity_expires_at=task["expires_at"])
                    if decision.text and check_text(decision.text,self._recent_texts) is GuardVerdict.BLOCK:
                        await asyncio.to_thread(self.store.update_post_task,task,'skipped',reason='text_constraint');continue
                    self._log_decision(decision)
                    result=await self._execute(decision)
                    report.results.append(result);executed+=1
                    if result.ok:
                        self._remember(decision,result)
                        status='succeeded'
                    elif result.status in ('unknown','simulated'): status=result.status
                    elif result.detail in ('action_succeeded','action_unknown','action_sending','action_claimed'):
                        status='unknown' if result.detail!='action_succeeded' else 'succeeded'
                    elif result.response.get('failure_category')=='terminal' or result.detail in ('target_edited','duplicate_publication','near_duplicate_publication','rag_snapshot_withdrawn','draft_or_opportunity_expired','author_daily_cap','thread_daily_cap',
                        'like_needs_initialise','like_hourly_cap','like_daily_cap'):
                        status='skipped'
                    else: status='queued'
                    await asyncio.to_thread(self.store.update_post_task,task,status,reason=result.detail,
                        delay=30,draft=draft if status=='queued' else None,draft_at=task['draft_at'])
            finally:
                lease.cancel()
                await asyncio.gather(lease,return_exceptions=True)
            await run_reply_batches(self,report,self.max_actions_per_cycle-len(report.results))
        return report

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

        # Ingestion creates durable tasks. The independent worker runs models and writes.



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
        return CommentStepReport(comment_crawl=c_rep)

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
            run_periodic(self.process_pending,interval_s=1.,stop=stop),
            run_periodic(self.reporter.sweep_and_report,interval_s=10.,stop=stop),
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
        context={k:d.scores.raw[k] for k in ('target_version','draft_expires_at','opportunity_expires_at','required_account_level') if k in d.scores.raw}
        if d.action is ActionKind.LIKE:
            return await a.like(d.account, d.post_id, decision_id=d.decision_id,context=context)
        if d.action is ActionKind.REPOST:
            return await a.repost(d.account, d.post_id, decision_id=d.decision_id,context=context)
        if d.action is ActionKind.REPLY:
            root_id = d.root_post_id or str(d.scores.raw.get("root_post_id") or "")
            return await a.reply(
                d.account,
                d.post_id,
                d.text,
                root_post_id=root_id or None,
                decision_id=d.decision_id,
                context={'engine':'micro','target_author':d.scores.raw.get('target_author',''),
                    'target_stance':d.scores.raw.get('relative_stance',''),'purpose':d.scores.raw.get('purpose',''),
                    'allied_hourly_cap': self.brain.allied_reply_hourly_cap if self.brain else 3,
                    **{k:d.scores.raw[k] for k in ('target_version','draft_expires_at','opportunity_expires_at','required_account_level',
                        'persona_id','persona_version','occupation_group','catalog_hash','cooldown_s','voice_style','topic','rag_snapshot_id',
                        'reply_batch_id','reply_slot','slot_owner','prompt_version','material_version','post_rubric_version') if k in d.scores.raw}},
            )
        if d.action is ActionKind.POST:
            raise ValueError("original posts belong to Pi macro publisher")
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
        if self.brain is not None and routed and routed != "bot_default":
            return None,"routed_account_not_in_fleet"
        return self._next_account(), ""

    def refresh_accounts(self):
        if not self.brain or not self.brain.router.registry.catalog_hash:return
        self.accounts=[p.account_id for p in self.brain.router.registry.list_all() if p.account_id]
        self._own.update(self.accounts)
        with self.store._lock:
            self._own.update(r[0] for r in self.store._conn.execute("SELECT account_id FROM bot_registry WHERE status='own_bot'"))
        self.reporter.accounts=list(self.accounts)
        self.frontline_crawler.accounts=list(self.accounts)
        self.frontline_crawler.feed_account=self.accounts[0] if self.accounts else ''

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

