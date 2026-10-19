"""Explicit fleet operations; service startup never registers accounts."""
import asyncio
import time
from .persona.catalog import load_catalog

INITIAL={'transport_logistics':4,'retail_food':4,'tourism_leisure':4,'education_sport':4,
    'healthcare_support':3,'technology_digital':3,'agriculture_trades':3,'office_community':3,
    'independent_commentary':2}
EXPANDED={**{g:6 for g in ('transport_logistics','retail_food','tourism_leisure','education_sport','healthcare_support','technology_digital')},
    'agriculture_trades':5,'office_community':5,'independent_commentary':2,'medical_public_health':1,'law_policy':1}


def fleet_plan(size=30, *, phase='warmup', persona_id=None, account_id=None):
    if size not in (30,50):raise ValueError('fleet stages are 30 and 50')
    if persona_id:
        import re
        profile=next((p for p in load_catalog()['personas'] if p['persona_id']==persona_id),None)
        if not profile:raise ValueError('unknown replacement persona')
        handle=account_id or profile['candidate_username']
        if not re.fullmatch(r'[a-z][a-z0-9_]{2,63}',handle):raise ValueError('invalid account handle')
        return [{'persona_id':persona_id,'account_id':handle,'occupation_group':profile['occupation_group'],'display_name':profile['name']}]
    limits=INITIAL if size==30 else EXPANDED
    selected=[];used={}
    for profile in load_catalog()['personas']:
        group=profile['occupation_group']
        if used.get(group,0)>=limits.get(group,0):continue
        selected.append({'persona_id':profile['persona_id'],'account_id':profile['candidate_username'],'occupation_group':group,
            'display_name':profile['name']})
        used[group]=used.get(group,0)+1
    return selected


async def register_one(store,client,persona_id,account_id,password,*,max_active=50,max_total=200,display_name=None):
    if not password:raise ValueError('registration password is missing')
    with store.batch_transaction():
        if store.get_managed_account(account_id):return {'account':account_id,'status':'exists'}
        old=store._conn.execute('SELECT status FROM registration_intents WHERE account_id=?',(account_id,)).fetchone()
        if old and old[0] in ('sending','unknown','succeeded'):
            return {'account':account_id,'status':old[0],'reason':'check login before any new registration'}
        counts=store.get_managed_account_counts(max_active=max_active,max_total=max_total)
        pending=store._conn.execute("SELECT COUNT(*) FROM registration_intents WHERE status IN ('sending','unknown')").fetchone()[0]
        if counts['undeleted']+pending>=max_active or counts['total']+pending>=max_total:
            raise ValueError('platform registration capacity exhausted')
        if store._conn.execute("SELECT 1 FROM registration_intents WHERE persona_id=? AND account_id<>? AND status IN ('sending','unknown','succeeded')",(persona_id,account_id)).fetchone():
            raise ValueError('persona has a reserved registration')
        store.bind_persona(persona_id,account_id)
        ts=time.time()
        store._conn.execute("INSERT INTO registration_intents VALUES(?,?,'sending',?,?,'') ON CONFLICT(account_id) DO UPDATE SET status='sending',updated_at=excluded.updated_at,detail=''",(account_id,persona_id,ts,ts))
        # Save credentials before sending, so an interrupted response can be reconciled.
        store.set_policy('registration_password:'+account_id,password)
    try:
        account=await asyncio.to_thread(client.social.register,account_id,password,client.team_key,display_name=display_name)
        client.bind_account(account_id,account,password=password)
    except Exception as exc:
        translated=client._translate_error(exc,account=account_id)
        status='unknown' if translated.status is None or translated.retryable else 'failed'
        with store.batch_transaction():
            store._conn.execute('UPDATE registration_intents SET status=?,updated_at=?,detail=? WHERE account_id=?',
                (status,time.time(),type(exc).__name__,account_id))
        return {'account':account_id,'status':status}
    with store.batch_transaction():
        store._conn.execute("UPDATE registration_intents SET status='succeeded',updated_at=? WHERE account_id=?",(time.time(),account_id))
        store._conn.execute("DELETE FROM runtime_policy WHERE key=?",('registration_password:'+account_id,))
        store.initialise_registered_account(account_id)
    return {'account':account_id,'status':'succeeded'}


async def check_registration(store,client,account_id):
    """Positive login resolves unknown; a failed login never proves non-registration."""
    password=store.policy('registration_password:'+account_id) or client.default_password
    if not password:raise ValueError('check requires saved credentials')
    try:account=await asyncio.to_thread(client.social.login,account_id,password)
    except Exception:return {'account':account_id,'status':'unknown','reason':'login did not confirm registration'}
    client.bind_account(account_id,account,password=password)
    with store.batch_transaction():
        store._conn.execute("UPDATE registration_intents SET status='succeeded',updated_at=? WHERE account_id=?",(time.time(),account_id))
        store._conn.execute('DELETE FROM runtime_policy WHERE key=?',('registration_password:'+account_id,))
    return {'account':account_id,'status':'succeeded'}


def preflight(settings,store,*,corpus=None):
    from .persona.registry import PersonaRegistry
    registry=PersonaRegistry().attach(store)
    states=store.account_states_for(store.persona_bindings().values())
    active=[a for a,s in states.items() if not s.is_disabled()]
    missing=[]
    if settings.side.value!='yes':missing.append('assigned YES side')
    for value,name in ((settings.platform.api_key,'platform key'),(settings.platform.bot_password,'account password'),
        (settings.jev.enabled and settings.jev.api_key,'enabled JEV'),(settings.llm.api_key,'LLM')):
        if not value:missing.append(name)
    if not active:missing.append('bound active account')
    if registry.catalog_hash!=store.policy('active_persona_catalog'):missing.append('matching persona catalog')
    ready,reason=store.report_state_ready(automatic=settings.report_mode=='hybrid')
    return {'offline':{'ready':corpus is not None},'live':{'ready':not missing,'blocking':missing},
        'reporting':{'mode':settings.report_mode,'ready':settings.report_mode!='off' and ready,'reason':reason},
        'side':settings.side.value,'active_accounts':len(active),'bound_accounts':len(states),
        'initialised_accounts':store._conn.execute("SELECT COUNT(*) FROM account_lifecycle WHERE initialise_status='ready'").fetchone()[0],
        'high_accounts':sum(s.capability_tier=='high' for s in states.values() if not s.is_disabled()),
        'high_supply_warning':not any(s.capability_tier=='high' for s in states.values() if not s.is_disabled()),
        'phase':store.effective_phase(),
        'platform_counts':store.get_managed_account_counts(),'catalog_hash':registry.catalog_hash,
        'rag_version':corpus.data['corpus_version'] if corpus else None,
        'unknown_actions':len(store.list_actions('unknown'))}
