"""Evidence-first reporting with bounded pending work and separate delayed rulings."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..actuator import Actuator
from ..models import ActionResult, Side
from ..storage import StateStore, current_sydney_day
from .detector import BotDetector
from .events import UnavailableModerationSource


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
    """Evidence-first submissions. Only an explicit adapter can apply later rulings."""
    def __init__(self,actuator,store,*,accounts,side=Side.UNASSIGNED,max_daily_per_team=10,
                 max_strikes_per_account=3,max_reports_per_step=3,max_reports_per_minute=None,
                 backfill_account_posts=True,backfill_cooldown_s=60,detector=None,
                 enabled=False,event_source=None):
        self.actuator,self.store=actuator,store
        if actuator.store is None: actuator.store=store
        self.accounts=list(accounts);self.side=side
        self.max_reports_per_step=max_reports_per_step
        self.enabled=enabled
        self.event_source=event_source or UnavailableModerationSource()
        self.detector=detector or BotDetector(store)
        self._next_idx=0
        self.backfill_account_posts=backfill_account_posts
        self.backfill_cooldown_s=max(30.,backfill_cooldown_s)

    async def _history_candidates(self,author,now):
        posts=self.store.get_author_posts(author,limit=10,unreported_only=True)
        if posts or not self.backfill_account_posts: return posts
        if now-self.store.policy('report_backfill:'+author,0)<self.backfill_cooldown_s: return []
        with self.store._lock:
            pending=self.store._conn.execute("SELECT 1 FROM report_log WHERE target_author=? AND outcome='pending' LIMIT 1",(author,)).fetchone()
        if pending: return []
        self.store.set_policy('report_backfill:'+author,now)
        rows=await self.actuator.get_account_posts(author,limit=10)
        from ..pipeline.perceive import post_from_dict
        from ..pipeline.crawl import _ingest
        import asyncio
        def ingest():
            with self.store.batch_transaction():
                for row in rows:
                    post=post_from_dict(row)
                    if post and post.author==author: _ingest(self.store,post,now=now,detector=self.detector)
        await asyncio.to_thread(ingest)
        return self.store.get_author_posts(author,limit=10,unreported_only=True)

    def _next_account(self):
        for _ in self.accounts:
            account=self.accounts[self._next_idx%len(self.accounts)];self._next_idx+=1
            if account not in self.actuator.suspended and not self.store.get_account_state(account).is_disabled():
                return account
        return None

    def _candidate_priority(self,prof,*,sydney_day,now):
        # Unknown team remains unknown. Never use inferred wording clusters for scoring.
        team=prof.get('true_team_id','')
        count=self.store.count_daily_reports_against(target_team=team,sydney_day=sydney_day) if team else 0
        tier=0 if count<5 else (1 if count<10 else 2)
        return tier,-float(prof['suspicion_score']),float(prof.get('first_seen_at',now))

    async def _submit_candidate(self, kind, target, author):
        from .policy import candidate_payload,automatic_evidence
        import asyncio,json,uuid
        mode=self.store.policy('report_mode','off')
        if not self.enabled or mode=='off':return None
        payload=await asyncio.to_thread(candidate_payload,self.store,kind,target,author)
        if not payload:return None
        candidate=await asyncio.to_thread(self.store.store_report_candidate,payload)
        automatic=mode=='hybrid' and automatic_evidence(self.store,payload) and self.store.report_state_ready(automatic=True)[0]
        ready,reason=self.store.report_state_ready(automatic=automatic)
        if not ready:return None
        with self.store._lock:
            approvals=self.store._conn.execute("SELECT id FROM report_approvals WHERE candidate_id=? AND consumed_by='' AND expires_at>? ORDER BY expires_at DESC LIMIT 1",(candidate,time.time())).fetchone()
        if not automatic and not approvals:return None
        account=self._next_account()
        if not account:return None
        context={'target_author':author,'evidence_hash':payload['evidence_hash'],
            'candidate_id':candidate,'approval_id':approvals[0] if approvals and not automatic else '',
            'report_attempt_id':uuid.uuid4().hex,'policy_version':payload['policy_version'],
            'true_team_id':self.store.get_bot_profile(author).get('true_team_id','')}
        if kind=='account':
            return await self.actuator.report_account(account,author,detail=payload['detail'],context=context)
        return await self.actuator.report(account,target,reason='inauthentic',detail=payload['detail'],context=context)

    async def report_account_candidate(self,author,*,confirmed_action_count):
        if confirmed_action_count is None or confirmed_action_count<100:return None
        if self.store.get_bot_profile(author)['upheld_strikes']<1:return None
        return await self._submit_candidate('account',author,author)

    async def sweep_and_report(self,*,priority_authors=(),sydney_day=None,now=None):
        ts=time.time() if now is None else float(now)
        day=sydney_day or current_sydney_day(ts)
        batch=ReportBatchResult(sydney_day=day)
        batch.suspended,batch.suspended_remaining_s=self.store.is_reporting_suspended(now=ts)
        if batch.suspended or not self.enabled or not self.accounts:return batch
        end=self.store.policy('campaign_ends_at',None)
        if end is not None and ts>=end:return batch
        profiles=self.store.list_bots_by_status('confirmed_bot')
        profiles.sort(key=lambda p:self._candidate_priority(p,sydney_day=day,now=ts))
        for prof in profiles:
            if len(batch.attempts)>=self.max_reports_per_step:break
            author=prof['account_id']
            if prof.get('verified') or prof.get('is_suspended'):continue
            posts=await self._history_candidates(author,ts)
            activity=self.store.confirmed_activity(author)
            result=None;kind='post';target=''
            if activity and activity['action_count']>=100 and prof['upheld_strikes']>=1:
                kind='account';target=author
                result=await self.report_account_candidate(author,confirmed_action_count=activity['action_count'])
            else:
                for post in posts:
                    target=post['post_id']
                    result=await self._submit_candidate('post',target,author)
                    if result:break
            if result and (result.ok or result.status=='unknown'):
                batch.attempts.append(ReportAttempt(target,author,prof.get('true_team_id',''),
                    result.account,'inauthentic',result.ok,'pending' if result.ok else result.status,result))
        return batch
