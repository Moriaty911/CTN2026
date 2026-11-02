"""Offline active-batch ingestion benchmark; temporary file DBs, WAL/FULL."""
from contextlib import closing
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ctn.storage import StateStore
from ctn.models import Post
from ctn.moderation import BotDetector
from ctn.pipeline.crawl import _ingest


def run():
    results=[]
    with tempfile.TemporaryDirectory() as directory:
        for count in (0,50,200,500):
            with closing(StateStore(Path(directory)/f'{count}.db')) as store:
                now=time.time()
                store.set_policy('active_persona_catalog','')
                with store.batch_transaction():
                    for i in range(count):
                        pid=f'old-{i}'
                        store.upsert_observed_post(Post(pid,f'author-{i}','Concrete ferry entry question.',now),now=now)
                        store.create_reply_batch(pid,1,{'catalog_hash':''},[{'persona_id':'synthetic','persona_version':1,'account_id':f'account-{i%50}'}],expires_at=now+1800,now=now)
                detector=BotDetector(store);times=[];sql=[]
                for repeat in range(5):
                    sql=[];store._conn.set_trace_callback(sql.append);start=time.perf_counter()
                    with store.batch_transaction():
                        for i in range(30):_ingest(store,Post(f'new-{repeat}-{i}',f'new-author-{repeat}-{i}',f'Question {repeat}/{i}: what do ferry entry conditions mean?',now),now=now,detector=detector)
                    times.append((time.perf_counter()-start)*1000);store._conn.set_trace_callback(None)
                results.append({'active_batches':count,'ingest30_p50_ms':round(statistics.median(times),3),'ingest30_max_ms':round(max(times),3),'sql_statements_last_repeat':len(sql)})
    return {'storage':'temporary local file WAL/FULL','results':results}

if __name__=='__main__':print(json.dumps(run(),indent=2))
