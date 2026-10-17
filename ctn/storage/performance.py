"""Publication attribution and sampled metrics; missing observations remain missing."""
from datetime import datetime
import json
import statistics
import time
import uuid

PERFORMANCE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS publications(action_id TEXT PRIMARY KEY,post_id TEXT UNIQUE,account_id TEXT NOT NULL,persona_id TEXT NOT NULL,persona_version INTEGER NOT NULL,occupation_group TEXT NOT NULL,kind TEXT NOT NULL,target_id TEXT NOT NULL,root_post_id TEXT NOT NULL,batch_id TEXT NOT NULL,slot INTEGER NOT NULL,published_at REAL NOT NULL,confirmed_at REAL NOT NULL,metadata_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_publications_persona_time ON publications(persona_id,published_at);
CREATE TABLE IF NOT EXISTS performance_jobs(job_key TEXT PRIMARY KEY,kind TEXT NOT NULL,subject TEXT NOT NULL,scheduled_at REAL NOT NULL,window_s INTEGER NOT NULL DEFAULT 0,next_attempt_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'queued',owner TEXT NOT NULL DEFAULT '',lease_until REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_performance_due ON performance_jobs(status,next_attempt_at);
CREATE TABLE IF NOT EXISTS post_metric_snapshots(job_key TEXT PRIMARY KEY,post_id TEXT NOT NULL,scheduled_at REAL NOT NULL,observed_at REAL NOT NULL,window_s INTEGER NOT NULL,likes INTEGER,reposts INTEGER,replies INTEGER,views INTEGER,source TEXT NOT NULL,status TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_post_snapshots_time ON post_metric_snapshots(post_id,observed_at);
CREATE TABLE IF NOT EXISTS account_metric_snapshots(job_key TEXT PRIMARY KEY,account_id TEXT NOT NULL,scheduled_at REAL NOT NULL,observed_at REAL NOT NULL,followers INTEGER,following INTEGER,source TEXT NOT NULL,status TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_account_snapshots_time ON account_metric_snapshots(account_id,observed_at);
CREATE TABLE IF NOT EXISTS feed_scans(scan_id TEXT PRIMARY KEY,observed_at REAL NOT NULL,source TEXT NOT NULL,requested INTEGER NOT NULL,received INTEGER NOT NULL,status TEXT NOT NULL,scope TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feed_entries(scan_id TEXT NOT NULL,post_id TEXT NOT NULL,rank INTEGER NOT NULL,PRIMARY KEY(scan_id,post_id));
CREATE INDEX IF NOT EXISTS idx_feed_post ON feed_entries(post_id,scan_id);
CREATE TABLE IF NOT EXISTS metric_observation_events(event_id TEXT PRIMARY KEY,job_key TEXT NOT NULL,observed_at REAL NOT NULL,source TEXT NOT NULL,status TEXT NOT NULL,metrics_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_metric_events_job ON metric_observation_events(job_key,observed_at);
'''
POST_WINDOWS=(300,900,3600,21600,86400)


def metric(raw, name):
    aliases={'likes':('likes','like_count'),'replies':('replies','reply_count'),
        'reposts':('reposts','repost_count'),'views':('views','view_count'),
        'followers':('followers','follower_count','followers_count'),
        'following':('following','following_count')}
    if not isinstance(raw,dict): return None
    if '_metric_presence' in raw and name not in raw['_metric_presence']: return None
    for key in aliases[name]:
        value=raw.get(key)
        if type(value) is int and value>=0: return value
    return None


class PerformanceStoreMixin:
    def record_publication(self, action, meta, response, *, now=None):
        ts=time.time() if now is None else now
        post_id=str(response.get('post_id') or response.get('id') or '')
        at=response.get('created_at',action['created_at'])
        if isinstance(at,str):
            try: at=datetime.fromisoformat(at.replace('Z','+00:00')).timestamp()
            except ValueError: at=action['created_at']
        if type(at) not in (int,float) or at<=0: at=action['created_at']
        self._conn.execute('INSERT OR IGNORE INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (action['id'],post_id or None,action['account_id'],meta.get('persona_id',''),meta.get('persona_version',0),
             meta.get('occupation_group','unknown'),action['kind'],action['target_id'],meta.get('root_post_id',''),
             meta.get('reply_batch_id',''),meta.get('reply_slot',0),at,ts,json.dumps(meta,ensure_ascii=False)))
        if post_id:
            for window in POST_WINDOWS:
                due=at+window
                self._conn.execute("INSERT OR IGNORE INTO performance_jobs(job_key,kind,subject,scheduled_at,window_s,next_attempt_at) VALUES(?,'post',?,?,?,?)",
                    (f'post:{post_id}:{window}',post_id,due,window,due))

    def ensure_performance_jobs(self, accounts, *, now=None):
        ts=time.time() if now is None else now
        with self.batch_transaction():
            for account in accounts:
                bucket=int(ts//21600)
                self._conn.execute("INSERT OR IGNORE INTO performance_jobs(job_key,kind,subject,scheduled_at,next_attempt_at) VALUES(?,'account',?,?,?)",
                    (f'account:{account}:{bucket}',account,bucket*21600,ts))
            bucket=int(ts//300)
            self._conn.execute("INSERT OR IGNORE INTO performance_jobs(job_key,kind,subject,scheduled_at,next_attempt_at) VALUES(?,'feed','',?,?)",
                (f'feed:{bucket}',bucket*300,ts))

    def claim_performance_jobs(self, *, limit=4, now=None):
        ts=time.time() if now is None else now
        with self.batch_transaction():
            self._conn.execute("UPDATE performance_jobs SET status='queued',owner='' WHERE status='claimed' AND lease_until<=?",(ts,))
            rows=self._conn.execute("SELECT * FROM performance_jobs WHERE status='queued' AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?",(ts,limit)).fetchall()
            owner=uuid.uuid4().hex
            for row in rows:self._conn.execute("UPDATE performance_jobs SET status='claimed',owner=?,lease_until=?,attempts=attempts+1 WHERE job_key=?",(owner,ts+180,row['job_key']))
            return [{**dict(r),'owner':owner} for r in rows]

    def renew_performance_jobs(self, jobs):
        with self.batch_transaction():
            for j in jobs:self._conn.execute("UPDATE performance_jobs SET lease_until=? WHERE job_key=? AND owner=? AND status='claimed'",(time.time()+180,j['job_key'],j['owner']))

    def record_performance_observation(self, job, raw, *, source, now=None):
        ts=time.time() if now is None else now
        available=isinstance(raw,list) if job['kind']=='feed' else isinstance(raw,dict) and bool(raw)
        status='available' if available else 'unavailable'
        with self.batch_transaction():
            owned=self._conn.execute("SELECT 1 FROM performance_jobs WHERE job_key=? AND owner=? AND status='claimed'",(job['job_key'],job['owner'])).fetchone()
            if not owned: return False
            values=({k:metric(raw,k) for k in ('likes','reposts','replies','views')} if job['kind']=='post' else
                {k:metric(raw,k) for k in ('followers','following')} if job['kind']=='account' else
                {'received':len(raw) if available else None})
            self._conn.execute('INSERT OR IGNORE INTO metric_observation_events VALUES(?,?,?,?,?,?)',
                (job['job_key']+':'+job['owner'],job['job_key'],ts,source,status,json.dumps(values)))
            if job['kind']=='post':
                self._conn.execute('INSERT OR REPLACE INTO post_metric_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (job['job_key'],job['subject'],job['scheduled_at'],ts,job['window_s'],
                     *(metric(raw,k) for k in ('likes','reposts','replies','views')),source,status))
            elif job['kind']=='account':
                self._conn.execute('INSERT OR REPLACE INTO account_metric_snapshots VALUES(?,?,?,?,?,?,?,?)',
                    (job['job_key'],job['subject'],job['scheduled_at'],ts,metric(raw,'followers'),metric(raw,'following'),source,status))
            else:
                self.record_feed_scan(job['job_key'],raw if available else [],source=source,status=status,now=ts)
            self._conn.execute("UPDATE performance_jobs SET status=?,owner='',lease_until=0,next_attempt_at=?,reason=? WHERE job_key=?",
                ('done' if available else 'queued',ts+300,'' if available else 'observation_unavailable',job['job_key']))
            return True

    def record_feed_scan(self, scan_id, posts, *, source='ranked_feed',status='available',requested=20,now=None):
        ts=time.time() if now is None else now
        with self.batch_transaction():
            self._conn.execute('INSERT OR IGNORE INTO feed_scans VALUES(?,?,?,?,?,?,?)',
                (scan_id,ts,source,requested,len(posts),status,'sample_only'))
            for rank,p in enumerate(posts,1):
                if isinstance(p,dict) and p.get('post_id'):
                    self._conn.execute('INSERT OR IGNORE INTO feed_entries VALUES(?,?,?)',(scan_id,str(p['post_id']),rank))

    def recent_feed_sample(self, *, since, now=None):
        ts=time.time() if now is None else now
        with self._lock:
            row=self._conn.execute("SELECT * FROM feed_scans WHERE status='available' AND observed_at>=? ORDER BY observed_at DESC LIMIT 1",(max(since,ts-60),)).fetchone()
            if not row:return None
            posts=[{'post_id':r[0]} for r in self._conn.execute('SELECT post_id FROM feed_entries WHERE scan_id=? ORDER BY rank',(row['scan_id'],))]
            return {'observed_at':row['observed_at'],'posts':posts}

    def performance_report(self, *, since=None, group_by='occupation_group', now=None):
        ts=time.time() if now is None else now
        since=ts-86400 if since is None else since
        fields={'occupation_group','persona_id','kind','slot'}
        if group_by not in fields: raise ValueError('unsupported report grouping')
        with self._lock:
            publications=[dict(r) for r in self._conn.execute('SELECT * FROM publications WHERE published_at>=? ORDER BY published_at',(since,))]
            snapshots=[dict(r) for r in self._conn.execute('SELECT s.* FROM post_metric_snapshots s JOIN publications p ON p.post_id=s.post_id WHERE p.published_at>=? ORDER BY s.observed_at',(since,))]
            accounts=[dict(r) for r in self._conn.execute('SELECT * FROM account_metric_snapshots WHERE observed_at>=? ORDER BY observed_at',(since,))]
            scans=[dict(r) for r in self._conn.execute('SELECT * FROM feed_scans WHERE observed_at>=? ORDER BY observed_at',(since,))]
            entries=[dict(r) for r in self._conn.execute('SELECT e.* FROM feed_entries e JOIN feed_scans s ON s.scan_id=e.scan_id WHERE s.observed_at>=?',(since,))]
        grouped={}
        for p in publications:grouped.setdefault(str(p[group_by]),[]).append(p)
        by_post={};by_account={};ranks_by_post={}
        for s in snapshots:
            if s['status']=='available':by_post.setdefault(s['post_id'],[]).append(s)
        for s in accounts:by_account.setdefault(s['account_id'],[]).append(s)
        for e in entries:ranks_by_post.setdefault(e['post_id'],[]).append(e)
        rows=[]
        for group,posts in grouped.items():
            ids={p['post_id'] for p in posts}
            sampled=[s for pid in ids for s in by_post.get(pid,())]
            sampled.sort(key=lambda s:s['observed_at'])
            latest={s['post_id']:s for s in sampled}
            row={'group':group,'publications':len(posts),'sampled_posts':len(latest),
                 'coverage':len(latest)/len(posts),'metrics':{},'windows':{},
                 'profile_versions':sorted({p['persona_version'] for p in posts}),
                 'content_types':{kind:sum(p['kind']==kind for p in posts) for kind in ('post','reply')}}
            for key in ('likes','reposts','replies','views'):
                values=[s[key] for s in latest.values() if s[key] is not None]
                row['metrics'][key]={'known_posts':len(values),'median':statistics.median(values) if values else None,'total':sum(values) if values else None}
            for window in POST_WINDOWS:
                bucket=[s for s in sampled if s['window_s']==window]
                by_metric={k:[s[k] for s in bucket if s[k] is not None] for k in ('likes','reposts','replies')}
                published={p['post_id']:p['published_at'] for p in posts}
                ages=[s['observed_at']-published[s['post_id']] for s in bucket]
                matured=sum(p['published_at']+window<=ts for p in posts)
                row['windows'][str(window)]={'sampled_posts':len(bucket),'eligible_posts':matured,
                    'coverage':len(bucket)/matured if matured else None,
                    'actual_age_seconds':{'median':statistics.median(ages) if ages else None,'min':min(ages,default=None),'max':max(ages,default=None)},
                    'metrics':{k:{'known_posts':len(v),'median':statistics.median(v) if v else None} for k,v in by_metric.items()},
                    'median_likes':statistics.median(by_metric['likes']) if by_metric['likes'] else None}
            matched=[e for pid in ids for e in ranks_by_post.get(pid,())]
            applicable=[s for s in scans if s['status']=='available' and any(p['published_at']<=s['observed_at'] for p in posts)]
            row['ranked_feed']={'scan_count':len(applicable),'appearances':len(matched),'best_observed_rank':min((e['rank'] for e in matched),default=None),'scope':'sample_only'}
            deltas=[]
            for account in {p['account_id'] for p in posts}:
                known=[s for s in by_account.get(account,()) if s['followers'] is not None]
                if len(known)>=2:deltas.append(known[-1]['followers']-known[0]['followers'])
            row['followers_net_change']={'known_accounts':len(deltas),'total':sum(deltas) if deltas else None,'attribution':'account_level_not_per_post'}
            changes=[]
            for pid in ids:
                known=[s for s in by_post.get(pid,()) if s['likes'] is not None]
                if len(known)>=2:changes.append(known[-1]['likes']-known[0]['likes'])
            row['observed_like_change']={'known_posts':len(changes),'total':sum(changes) if changes else None}
            rows.append(row)
        return {'schema_version':'ctn-performance.v1','since':since,'observed_at':ts,'group_by':group_by,'groups':rows,
            'limits':['Aggregated interactions do not identify NPCs.','Missing data is not zero.','Feed ranks describe sampled results only.','No inferred persuasion or competition score.'],
            'automatic_routing_adjustment':False}
