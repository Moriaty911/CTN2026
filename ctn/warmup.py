"""Compatibility module: one confirmed daily initialise post per new account."""
import hashlib
import time
from .macro.publisher import StrategicPublisher
from .persona.warmup_seeds import get_warmup_seed
from .pipeline.targeting import launch_rule

ALERT_AFTER=2


async def initialise_step(actuator,router,postflight):
    router.registry.attach(router.store)
    for persona in router.registry.list_all():
        if not persona.account_id:continue
        state=router.get_state(persona.account_id)
        now=time.time()
        if state.is_disabled() or state.is_in_cooldown(now,persona.cooldown_s):continue
        lifecycle=router.store.lifecycle(persona.account_id)
        if lifecycle and lifecycle['initialise_status']=='waived' and router.store.effective_phase(now=now)!='climax':
            with router.store.batch_transaction():router.store._conn.execute("UPDATE account_lifecycle SET initialise_status='pending',waiver_reason='' WHERE account_id=?",(persona.account_id,))
            lifecycle=router.store.lifecycle(persona.account_id)
        if lifecycle and (lifecycle['initialise_status'] in ('ready','waived','sending','unknown') or lifecycle['next_attempt_at']>now):continue
        if router.store.effective_phase(now=now)=='climax' and router.store.account_initialise_ready(persona.account_id,now=now):continue
        with router.store._lock:
            if router.store._conn.execute('SELECT 1 FROM account_leases WHERE account_id=? AND lease_until>?',(persona.account_id,now)).fetchone():continue
        # Spread first posts over hours in a random order: 80 new accounts posting within one hour looks like a farm.
        spread=launch_rule(router.store,'initialise_spread')
        schedule=router.store.policy('initialise_schedule',{}) or {}
        if persona.account_id not in schedule:
            offset=int(hashlib.sha256(persona.account_id.encode()).hexdigest(),16)%10_000/10_000*float(spread['window_seconds'])
            schedule[persona.account_id]=now+offset
            router.store.set_policy('initialise_schedule',schedule)
        if schedule[persona.account_id]>now:continue
        if now-float(router.store.policy('initialise_last_at',0))<float(spread['min_gap_seconds']):
            return {'status':'idle','reason':'initialise_spacing'}
        router.store.set_policy('initialise_last_at',now)
        publisher=StrategicPublisher(actuator,postflight,router,side=persona.assigned_side)
        failures=int(lifecycle['initialise_failures']) if lifecycle else 0
        # A rejected seed is never retried verbatim: each content failure moves to the next daily example.
        seed=get_warmup_seed(persona.account_id,index=failures,registry=router.registry)
        result=await publisher.publish(persona.account_id,seed,purpose='initialise')
        if result.get('status') not in ('published','simulated'):
            content_failure=result.get('status') in ('rejected','failed')
            with router.store.batch_transaction():
                router.store._conn.execute('INSERT OR IGNORE INTO account_lifecycle(account_id) VALUES(?)',(persona.account_id,))
                router.store._conn.execute('UPDATE account_lifecycle SET next_attempt_at=?,initialise_failures=initialise_failures+? WHERE account_id=?',
                    (now+300,int(content_failure),persona.account_id))
                if content_failure and failures+1==ALERT_AFTER:
                    router.store.notify_discord(f'initialise_failing:{persona.account_id}',{'type':'alert','kind':'initialise_failing',
                        'account_id':persona.account_id,'failures':failures+1,'reason':str(result.get('reason') or result.get('status'))})
        return result
    return {'status':'idle','reason':'all_accounts_initialised_or_unavailable'}

warmup_step=initialise_step
