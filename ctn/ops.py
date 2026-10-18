"""Local operations; only explicit fleet register/check commands access platform authentication."""
from __future__ import annotations
import argparse
from contextlib import closing
from dataclasses import fields
import json
import os
from pathlib import Path
import sqlite3
import time
from .config import Settings
from .persona import AccountState
from .storage import StateStore

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-file')
    subs=parser.add_subparsers(dest='command',required=True)
    p=subs.add_parser('backup');p.add_argument('destination')
    p=subs.add_parser('restore');p.add_argument('source');p.add_argument('destination')
    subs.add_parser('status')
    subs.add_parser('preflight')
    p=subs.add_parser('fleet');p.add_argument('operation',choices=['plan','register','check','import'])
    p.add_argument('--size',type=int,choices=[30,50],default=30);p.add_argument('--account');p.add_argument('--source')
    p.add_argument('--execute',action='store_true',help='explicitly execute the reviewed registration plan')
    p=subs.add_parser('reports');p.add_argument('operation',choices=['list','approve','clearance'])
    p.add_argument('--candidate');p.add_argument('--source')
    p=subs.add_parser('rag-withdraw');p.add_argument('snapshot_id',nargs='?');p.add_argument('--reference');p.add_argument('--corpus-hash')
    p=subs.add_parser('account');p.add_argument('account_id');p.add_argument('--mode',choices=['active','standby','dormant'])
    p.add_argument('--tier',choices=['standard','high']);p.add_argument('--original-locked-until',type=float)
    p=subs.add_parser('account-activity');p.add_argument('source',help='confirmed account_id/action_count/observed_at/source JSON')
    p=subs.add_parser('import-events');p.add_argument('source',help='JSON array of explicit, verified ruling events')
    p=subs.add_parser('import-accounts');p.add_argument('source',help='legacy JSON account-state mapping')
    p=subs.add_parser('background');p.add_argument('source',help='operator-verified BackgroundReference JSON array')
    p=subs.add_parser('resolve-action');p.add_argument('action_id');p.add_argument('evidence',help='explicit operator review JSON')
    p=subs.add_parser('phase');p.add_argument('phase',choices=['warmup','shaping','climax'])
    p=subs.add_parser('performance');p.add_argument('--group-by',choices=['persona_id','occupation_group','kind','slot'],default='occupation_group')
    p.add_argument('--hours',type=float,default=24);p.add_argument('--format',choices=['json','markdown'],default='json')
    p=subs.add_parser('persona');p.add_argument('operation',choices=['validate','preview','list','show','apply','bind','activate','standby','dormant'])
    p.add_argument('--file');p.add_argument('--id',action='append',default=[]);p.add_argument('--group',action='append',default=[])
    p.add_argument('--account');p.add_argument('--output')
    args=parser.parse_args(argv)
    os.umask(0o077)
    if args.command=='restore':
        source=Path(args.source).resolve();dest=Path(args.destination).resolve()
        if not source.is_file() or dest.exists(): parser.error('source must exist; destination must be new')
        dest.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(f'file:{source}?mode=ro',uri=True)) as src:
            if src.execute('PRAGMA integrity_check').fetchone()[0]!='ok': parser.error('backup integrity check failed')
            with closing(sqlite3.connect(dest)) as dst: src.backup(dst)
        dest.chmod(0o600)
        print(json.dumps({'restored':str(dest),'source':str(source)}));return 0
    settings=Settings.load()
    # Pure content validation/preview need neither a database nor credentials.
    if args.command=='persona' and args.operation in ('validate','preview'):
        from .persona.catalog import load_catalog,render_preview,digest
        data=load_catalog(args.file)
        if args.operation=='preview':
            preview=render_preview(data)
            if args.output:Path(args.output).write_text(preview)
            else:print(preview)
        else:print(json.dumps({'valid':True,'personas':80,'version':data['catalog_version'],'hash':digest(data)}))
        return 0
    path=settings.database_path(args.state_file)
    store=StateStore(path)
    try:
        if args.command=='preflight':
            from .accounts import preflight
            from .rag.corpus import Corpus
            result=preflight(settings,store,corpus=Corpus(settings.rag_corpus or None))
        elif args.command=='fleet':
            from .accounts import fleet_plan,register_one,check_registration
            from .persona.registry import PersonaRegistry
            registry=PersonaRegistry().attach(store)
            if args.operation=='plan':
                result={'requested':args.size,'selected':fleet_plan(args.size,phase=store.policy('phase','warmup')),
                    'platform_counts':store.get_managed_account_counts(),'note':'standby platform accounts also occupy server slots'}
            elif args.operation=='import':
                if not args.source:parser.error('fleet import requires --source')
                records=json.loads(Path(args.source).read_text())
                with store.batch_transaction():
                    for row in records:
                        store.bind_persona(row['persona_id'],row['account_id'])
                        store.upsert_managed_account(row['account_id'],password=row.get('password'),token=row.get('token'),platform_account_id=row.get('platform_account_id'))
                result={'imported':len(records),'mode':'standby'}
            else:
                if args.operation=='register' and not args.execute:parser.error('registration requires --execute; inspect fleet plan first')
                from .client.legit_v1 import OfficialLegitClient
                import asyncio
                if not settings.platform.api_key:parser.error('platform credentials required')
                client=OfficialLegitClient(url=settings.platform.base_url or None,team_key=settings.platform.api_key,
                    default_password=settings.platform.bot_password,auto_register=False,store=store)
                client.configure_pacing(settings.rate,settings.budget)
                async def perform():
                    try:
                        if args.operation=='check':
                            if not args.account:raise ValueError('check requires --account')
                            return await check_registration(store,client,args.account)
                        outcomes=[]
                        for row in fleet_plan(args.size,phase=store.policy('phase','warmup')):
                            if args.account and row['account_id']!=args.account:continue
                            outcome=await register_one(store,client,row['persona_id'],row['account_id'],settings.platform.bot_password,
                                max_active=settings.fleet.bot_accounts,max_total=settings.fleet.max_total_accounts)
                            outcomes.append(outcome)
                            if outcome['status'] in ('unknown','failed'):break
                        return {'results':outcomes}
                    finally:await client.close()
                result=asyncio.run(perform())
        elif args.command=='reports':
            if args.operation=='approve':
                if not args.candidate:parser.error('approve requires --candidate')
                result={'approval_id':store.approve_report(args.candidate),'expires_in_s':300}
            elif args.operation=='clearance':
                if not args.source:parser.error('clearance requires operator evidence JSON')
                proof=json.loads(Path(args.source).read_text())
                if not proof.get('source') or proof.get('allowed') is not True:parser.error('clearance needs source and allowed=true')
                store.set_policy('report_clearance',{'source':proof['source'],'allowed':True,
                    'expires_at':time.time()+300,'live_source_verified':False})
                result={'manual_clearance_expires_in_s':300,'automatic_capability':False}
            else:
                with store._lock:
                    result={'state':store.report_state_ready(),'candidates':[dict(r) for r in store._conn.execute('SELECT * FROM report_candidates ORDER BY created_at DESC LIMIT 50')],
                        'attempts':[dict(r) for r in store._conn.execute('SELECT * FROM report_attempts ORDER BY submitted_at DESC LIMIT 50')]}
        elif args.command=='rag-withdraw':
            if args.reference:
                if args.snapshot_id or not args.corpus_hash:parser.error('reference withdrawal requires --corpus-hash and no snapshot ID')
                result={'withdrawn':store.revoke_rag_reference(args.reference,args.corpus_hash)}
            else:
                if not args.snapshot_id:parser.error('snapshot ID or --reference required')
                store.revoke_rag_snapshot(args.snapshot_id);result={'withdrawn':args.snapshot_id}
        elif args.command=='backup':
            store.backup(args.destination);result={'backup':str(Path(args.destination).resolve())}
        elif args.command=='account':
            bindings=store.persona_bindings()
            selected=[pid for pid,account in bindings.items() if account==args.account_id]
            if args.mode and selected:
                store.set_persona_modes(selected,args.mode,max_active=settings.fleet.bot_accounts)
            store.set_account_policy(args.account_id,mode=args.mode,capability_tier=args.tier,
                original_locked_until=args.original_locked_until)
            result=store.get_account_metrics(args.account_id)
        elif args.command=='phase':
            store.set_policy('phase',args.phase);result={'phase':args.phase,'activated':0}
        elif args.command=='performance':
            if args.hours<=0:parser.error('--hours must be positive')
            result=store.performance_report(since=time.time()-args.hours*3600,group_by=args.group_by)
            if args.format=='markdown':
                from .analytics import render_report
                print(render_report(result));return 0
        elif args.command=='persona':
            from .persona.registry import PersonaRegistry
            registry=PersonaRegistry(path=args.file).attach(store)
            operation=args.operation
            if operation in ('bind','activate','standby','dormant') and registry.catalog_hash!=store.policy('active_persona_catalog'):
                parser.error('persona catalog mismatch; explicitly apply the intended catalog first')
            selected=set(args.id)
            groups=set(args.group)
            known_groups={p.occupation_group for p in registry.list_all()}
            if groups-known_groups:parser.error('unknown occupation group')
            if any(registry.get(pid) is None for pid in selected):parser.error('unknown persona ID')
            selected.update(p.persona_id for p in registry.list_all() if p.occupation_group in groups and p.account_id)
            if operation=='apply':
                result={'active_catalog':store.register_persona_catalog(registry.catalog,activate=True),'personas':80}
            elif operation=='bind':
                if len(args.id)!=1 or not args.account:parser.error('bind requires one --id and --account')
                result={'account':store.bind_persona(args.id[0],args.account),'mode':'standby'}
            elif operation in ('activate','standby','dormant'):
                mode='active' if operation=='activate' else operation
                result={'changed':store.set_persona_modes(sorted(selected),mode,max_active=settings.fleet.bot_accounts),'mode':mode}
            elif operation=='show':
                if len(args.id)!=1:parser.error('show requires one --id')
                result=registry.get(args.id[0]).to_dict()
            else:
                states=store.account_states_for(p.account_id for p in registry.list_all())
                result={'catalog_version':registry.catalog['catalog_version'],'hash':registry.catalog_hash,
                    'active_hash':store.policy('active_persona_catalog'),'personas':[{
                        'persona_id':p.persona_id,'version':p.version,'name':p.name,'occupation_group':p.occupation_group,
                        'account_id':p.account_id or None,'mode':states[p.account_id].mode if p.account_id in states else 'unbound'
                    } for p in registry.list_all() if not selected or p.persona_id in selected]}
        elif args.command=='resolve-action':
            proof=json.loads(Path(args.evidence).read_text())
            outcome=proof.get('outcome')
            if outcome not in ('succeeded','failed') or not proof.get('source') or not proof.get('reason'):
                parser.error('resolution needs outcome, source and reason')
            if outcome=='failed' and proof.get('confirmed_not_sent') is not True:
                parser.error('failure requires explicit confirmed_not_sent; absence from limited history is insufficient')
            if outcome=='succeeded' and not (proof.get('response') or {}).get('post_id'):
                parser.error('success requires positive platform post evidence')
            unknown={r['id'] for r in store.list_actions('unknown')}
            if args.action_id not in unknown: parser.error('action is not unknown')
            result={'resolved':store.finish_action(args.action_id,outcome,
                response={**proof.get('response',{}),'operator_resolution':proof},detail='operator verified: '+proof['source'])}
        elif args.command=='account-activity':
            proof=json.loads(Path(args.source).read_text())
            store.record_confirmed_activity(proof['account_id'],proof['action_count'],observed_at=proof['observed_at'],source=proof['source'])
            result={'recorded':store.confirmed_activity(proof['account_id'])}
        elif args.command=='import-events':
            events=json.loads(Path(args.source).read_text())
            applied=0;invalid=0
            for event in events:
                try:applied+=bool(store.apply_moderation_event(event))
                except (ValueError,TypeError,KeyError):invalid+=1
            result={'applied':applied,'events':len(events),'invalid':invalid}
        elif args.command=='background':
            from .jev import BackgroundReference,JevPacker
            data=json.loads(Path(args.source).read_text())
            refs=tuple(BackgroundReference(**row) for row in data)
            JevPacker(verified_references=refs)  # schema and duplicate validation
            from .persona.catalog import digest
            with store.batch_transaction():
                store.set_policy('trusted_background',data)
                store.set_policy('background_version','trusted-background.v1:'+digest(data))
            result={'references':len(data),'restart_service_to_apply':True}
        elif args.command=='import-accounts':
            data=json.loads(Path(args.source).read_text())
            allowed={f.name for f in fields(AccountState)}
            with store.batch_transaction():
                for account,value in data.items():
                    store.save_account_state(AccountState(**{k:v for k,v in {**value,'account_id':account}.items() if k in allowed}))
            result={'imported':len(data),'source':str(Path(args.source).resolve())}
        else:
            result={**store.runtime_metrics(),'database':str(path),'unknown':[
                {k:r[k] for k in ('id','kind','account_id','target_id','created_at','detail')} for r in store.list_actions('unknown')]}
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except ValueError as exc:
        parser.error(str(exc))
    finally: store.close()

if __name__=='__main__':raise SystemExit(main())
