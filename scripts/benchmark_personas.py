"""Synthetic local routing/report benchmark, never opens a runtime database."""
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ctn.persona import PersonaRouter
from ctn.storage import StateStore


def measured(call,n=100):
    values=[]
    for _ in range(n):
        start=time.perf_counter();call();values.append((time.perf_counter()-start)*1000)
    values.sort()
    return {'p50_ms':round(statistics.median(values),3),'p95_ms':round(values[int(.95*(n-1))],3),'repeats':n}


def run():
    with tempfile.TemporaryDirectory() as td:
        store=StateStore(Path(td)/'synthetic.db');router=PersonaRouter(store=store)
        store.set_policy('phase','shaping')
        personas=router.registry.list_all()
        for p in personas:store.bind_persona(p.persona_id,'synthetic_'+p.persona_id)
        store.set_persona_modes([p.persona_id for p in personas],'active',max_active=80)
        routing=measured(lambda:router.route({},'How would shared ferry terminal checks work?'))
        now=time.time();profiles=router.registry.list_all()
        pubs=[];snapshots=[]
        for i in range(10000):
            p=profiles[i%80];published=now-172800+i
            pubs.append((f'a{i}',f'p{i}',p.account_id,p.persona_id,p.version,p.occupation_group,'reply','target','root',f'b{i//4}',i%4,published,published,'{}'))
            for window in (300,900,3600,21600,86400):
                snapshots.append((f'j{i}:{window}',f'p{i}',published+window,published+window,window,i%20,1,2,None,'synthetic','available'))
        with store.batch_transaction():
            store._conn.executemany('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',pubs)
            store._conn.executemany('INSERT INTO post_metric_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)',snapshots)
        report=measured(lambda:store.performance_report(since=now-200000,group_by='persona_id'),n=5)
        result={'profiles':80,'synthetic_publications':10000,'post_samples':50000,'routing':routing,'report_by_persona':report,
            'scope':'Local synthetic metadata; no model or platform latency, no behavioral effectiveness measurement.'}
        store.close();return result

if __name__=='__main__':print(json.dumps(run(),indent=2))
