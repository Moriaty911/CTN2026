"""Python owns one neutral warmup per new account, through the common publisher."""
import asyncio
import time
from .macro.publisher import StrategicPublisher
from .persona.warmup_seeds import get_warmup_seed


async def warmup_step(actuator,router,postflight):
    router.registry.attach(router.store)
    for persona in router.registry.list_all():
        if not persona.account_id:continue
        state=router.get_state(persona.account_id)
        now=time.time()
        if state.is_disabled() or state.original_locked_until>now or state.is_in_cooldown(now,persona.cooldown_s):continue
        if router.store.policy('warmup_next:'+persona.account_id,0)>now:continue
        with router.store._lock:
            if router.store._conn.execute('SELECT 1 FROM account_leases WHERE account_id=? AND lease_until>?',(persona.account_id,now)).fetchone():continue
        with router.store._lock:
            warmed=router.store._conn.execute('SELECT warmed_at FROM account_readiness WHERE account_id=?',(persona.account_id,)).fetchone()
        if warmed and warmed[0]:continue
        publisher=StrategicPublisher(actuator,postflight,router,side=persona.assigned_side)
        seed=get_warmup_seed(persona.account_id,index=0,registry=router.registry)
        result=await publisher.publish(persona.account_id,seed,purpose='warmup')
        if result.get('status') not in ('published','simulated'):
            router.store.set_policy('warmup_next:'+persona.account_id,now+300)
        return result
    return {'status':'idle','reason':'all_active_accounts_warmed_or_unavailable'}
