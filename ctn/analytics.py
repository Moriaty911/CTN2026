"""Budgeted, restartable observation sampling; never estimates competition revenue."""
import asyncio
import time


class PerformanceCollector:
    def __init__(self,actuator,router):
        self.actuator=actuator;self.router=router;self.store=router.store

    async def step(self):
        if self.actuator.dry_run:return
        accounts=[p.account_id for p in self.router.registry.list_all() if p.account_id]
        await asyncio.to_thread(self.store.ensure_performance_jobs,accounts)
        jobs=await asyncio.to_thread(self.store.claim_performance_jobs,limit=4)
        if not jobs:return
        async def renew():
            while True:
                await asyncio.sleep(20)
                await asyncio.to_thread(self.store.renew_performance_jobs,jobs)
        lease=asyncio.create_task(renew())
        try:
            for job in jobs:
                raw=None;source='platform'
                if job['kind']=='post':
                    # Live posts carry no counts; only /posts/{id}/counters has them (a 404 comes back empty = unavailable).
                    raw=await self.actuator.get_counters(job['subject'])
                    observed_at=time.time()
                elif job['kind']=='account':
                    raw=await self.actuator._read('metrics_account',lambda:self.actuator._client.get_profile(job['subject']),None)
                    observed_at=time.time()
                else:
                    recent=await asyncio.to_thread(self.store.recent_feed_sample,since=job['scheduled_at'])
                    if recent:
                        await asyncio.to_thread(self.store.record_performance_observation,job,recent['posts'],source='shared_ranked_sample',now=recent['observed_at'])
                        continue
                    getter=getattr(self.actuator._client,'feed_trending',None)
                    if getter:
                        raw=await self.actuator._read('metrics_ranked_feed',lambda:getter(limit=20),None)
                    else:
                        raw=await self.actuator._read('metrics_ranked_feed',lambda:self.actuator._client.get_feed('',limit=20),None)
                    source='ranked_feed';observed_at=time.time()
                await asyncio.to_thread(self.store.record_performance_observation,job,raw,source=source,now=observed_at)
        finally:
            lease.cancel();await asyncio.gather(lease,return_exceptions=True)


def render_report(report):
    lines=['# Persona performance sample report','',
        'Missing fields remain unknown. Feed positions describe the sampled range. Interaction totals do not identify NPCs.',
        '', '| Group | Published | Sampled | Coverage | Median likes | Like change | Best sampled rank | Follower net |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    show=lambda x:'unknown' if x is None else str(x)
    for r in report['groups']:
        lines.append('| '+ ' | '.join((r['group'],str(r['publications']),str(r['sampled_posts']),
            f"{r['coverage']:.1%}",show(r['metrics']['likes']['median']),show(r['observed_like_change']['total']),
            show(r['ranked_feed']['best_observed_rank']),show(r['followers_net_change']['total'])))+' |')
    lines+=['','Follower changes are account-level observations and cannot be attributed to one post.',
        'No inferred persuasion, NPC engagement or competition score. Routing weights are unchanged.','']
    return '\n'.join(lines)
