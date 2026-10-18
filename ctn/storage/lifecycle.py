"""v6 lifecycle, reviewed operations and durable worker queues.

The database is the source of execution policy. No method performs network I/O.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo('Australia/Sydney')
PHASE_TIMES = {'startup': '2026-10-19T18:00:00', 'shaping': '2026-10-20T17:00:00', 'climax': '2026-11-01T00:00:00'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS quality_observations(id INTEGER PRIMARY KEY,account_id TEXT NOT NULL,post_id TEXT NOT NULL,passed INTEGER NOT NULL,reason TEXT NOT NULL,stage TEXT NOT NULL,created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_quality_account ON quality_observations(account_id,created_at);
CREATE TABLE IF NOT EXISTS account_lifecycle(account_id TEXT PRIMARY KEY,initialise_status TEXT NOT NULL DEFAULT 'pending',initialise_action TEXT NOT NULL DEFAULT '',initialised_at REAL NOT NULL DEFAULT 0,waiver_reason TEXT NOT NULL DEFAULT '',maintenance INTEGER NOT NULL DEFAULT 0,next_attempt_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS operation_audit(id INTEGER PRIMARY KEY,created_at REAL NOT NULL,actor TEXT NOT NULL,operation TEXT NOT NULL,subject TEXT NOT NULL,detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_reviews(candidate_id TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'review',resume_at REAL NOT NULL DEFAULT 0,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_interactions(interaction_id TEXT PRIMARY KEY,result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS discord_outbox(id INTEGER PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,payload_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',next_attempt_at REAL NOT NULL DEFAULT 0,owner TEXT NOT NULL DEFAULT '',lease_until REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_outbox_ready ON discord_outbox(status,next_attempt_at,lease_until);
CREATE TABLE IF NOT EXISTS discord_messages(candidate_id TEXT NOT NULL,message_id TEXT NOT NULL,channel_id TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(candidate_id,message_id));
CREATE TABLE IF NOT EXISTS macro_jobs(id TEXT PRIMARY KEY,scheduled_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'queued',owner TEXT NOT NULL DEFAULT '',lease_until REAL NOT NULL DEFAULT 0,payload_json TEXT NOT NULL DEFAULT '{}',reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS task_dependency_objects(post_id TEXT NOT NULL,version INTEGER NOT NULL,object_key TEXT NOT NULL,PRIMARY KEY(post_id,version,object_key));
CREATE INDEX IF NOT EXISTS idx_dependency_object ON task_dependency_objects(object_key);
CREATE TABLE IF NOT EXISTS dirty_dependencies(object_key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS report_affiliations(author TEXT PRIMARY KEY,side TEXT NOT NULL,team_id TEXT NOT NULL,source TEXT NOT NULL,observed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS report_decisions(candidate_id TEXT PRIMARY KEY,subject_kind TEXT NOT NULL,subject_id TEXT NOT NULL,author TEXT NOT NULL,bucket TEXT NOT NULL,p REAL NOT NULL,p_upheld REAL NOT NULL,reward REAL NOT NULL,cost REAL NOT NULL,expected_value REAL NOT NULL,action TEXT NOT NULL,reason TEXT NOT NULL,shadow INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_report_decisions_time ON report_decisions(updated_at);
CREATE TABLE IF NOT EXISTS report_calibration(bucket TEXT PRIMARY KEY,upheld INTEGER NOT NULL DEFAULT 0,rejected INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS report_target_observations(id INTEGER PRIMARY KEY,attempt_id TEXT NOT NULL,subject_id TEXT NOT NULL,observed_at REAL NOT NULL,found INTEGER NOT NULL,fields_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_report_target_obs ON report_target_observations(attempt_id,observed_at);
CREATE TABLE IF NOT EXISTS trending_seen(post_id TEXT PRIMARY KEY,seen_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS post_refresh(post_id TEXT NOT NULL,stage INTEGER NOT NULL,due_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'queued',PRIMARY KEY(post_id,stage));
CREATE INDEX IF NOT EXISTS idx_post_refresh_due ON post_refresh(status,due_at);
CREATE INDEX IF NOT EXISTS idx_batches_target_version ON reply_batches(target_id,target_version,status);
'''


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class LifecycleStoreMixin:
    def migrate_lifecycle(self, old_version):
        for statement in SCHEMA.split(';'):
            if statement.strip(): self._conn.execute(statement)
        columns = {r['name'] for r in self._conn.execute('PRAGMA table_info(post_tasks)')}
        for name, ddl in {'priority':'REAL NOT NULL DEFAULT 0','queued_at':'REAL NOT NULL DEFAULT 0','failure_count':'INTEGER NOT NULL DEFAULT 0','assessment_json':"TEXT NOT NULL DEFAULT ''",
                          'audience':'REAL NOT NULL DEFAULT 0','jev_score':'REAL NOT NULL DEFAULT 0'}.items():
            if name not in columns:self._conn.execute(f'ALTER TABLE post_tasks ADD COLUMN {name} {ddl}')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_priority ON post_tasks(status,next_attempt_at,priority)')
        if 'initialise_failures' not in {r['name'] for r in self._conn.execute('PRAGMA table_info(account_lifecycle)')}:
            self._conn.execute('ALTER TABLE account_lifecycle ADD COLUMN initialise_failures INTEGER NOT NULL DEFAULT 0')
        if old_version < 6:
            self._conn.execute('UPDATE post_tasks SET queued_at=MAX(0,expires_at-1800) WHERE queued_at=0')
            self._conn.execute("INSERT OR IGNORE INTO account_lifecycle(account_id,initialise_status,initialised_at) SELECT account_id,'ready',warmed_at FROM account_readiness WHERE warmed_at>0")
            # Release only locks equal to a proven successful warmup-generated lock.
            for row in self._conn.execute("SELECT account_id,MAX(updated_at) AS at FROM action_intents WHERE kind='post' AND status='succeeded' AND json_extract(metadata_json,'$.purpose')='warmup' GROUP BY account_id").fetchall():
                later=self._conn.execute("SELECT 1 FROM action_intents WHERE account_id=? AND kind='post' AND status='succeeded' AND COALESCE(json_extract(metadata_json,'$.purpose'),'') NOT IN ('warmup','initialise') AND updated_at>=?",(row['account_id'],row['at'])).fetchone()
                if not later:self._conn.execute('UPDATE account_states SET original_locked_until=0 WHERE account_id=? AND ABS(original_locked_until-?)<0.001',(row['account_id'],row['at']+float(self.policy('original_lock_seconds',86400))))
            for key, value in (('daily_original_limit',int(os.environ.get('CTN_DAILY_ORIGINAL_LIMIT','10'))),('phase_auto',True),('phase_times',PHASE_TIMES)):
                if self.policy(key) is None:self.set_policy(key,value)
            self.set_policy('phase_auto',True)
        self._conn.execute("INSERT OR IGNORE INTO report_reviews(candidate_id,status,updated_at) SELECT c.id,CASE WHEN EXISTS(SELECT 1 FROM report_attempts a WHERE json_extract(a.payload_json,'$.candidate_id')=c.id AND a.status IN ('pending','unknown','sending')) THEN 'pending' WHEN EXISTS(SELECT 1 FROM report_approvals a WHERE a.candidate_id=c.id AND a.consumed_by='' AND a.expires_at>?) THEN 'approved' ELSE 'review' END,? FROM report_candidates c",(time.time(),time.time()))
        if old_version<6:
            for candidate in self._conn.execute('SELECT candidate_id FROM report_reviews').fetchall():self.notify_discord('migrated:'+candidate[0],{'type':'candidate','candidate_id':candidate[0]})
        # Triggers notify material waiters only for meaningful changes.
        self._conn.execute("CREATE TRIGGER IF NOT EXISTS dep_post_insert AFTER INSERT ON observed_posts BEGIN INSERT OR IGNORE INTO dirty_dependencies VALUES('post:'||NEW.post_id); END")
        self._conn.execute("CREATE TRIGGER IF NOT EXISTS dep_post_update AFTER UPDATE OF version,raw_json ON observed_posts WHEN OLD.version<>NEW.version OR json_extract(OLD.raw_json,'$.text_complete') IS NOT json_extract(NEW.raw_json,'$.text_complete') OR json_extract(OLD.raw_json,'$.platform_level') IS NOT json_extract(NEW.raw_json,'$.platform_level') OR json_extract(OLD.raw_json,'$.author_level') IS NOT json_extract(NEW.raw_json,'$.author_level') OR json_extract(OLD.raw_json,'$.verified') IS NOT json_extract(NEW.raw_json,'$.verified') BEGIN INSERT OR IGNORE INTO dirty_dependencies VALUES('post:'||NEW.post_id); END")
        self._conn.execute("CREATE TRIGGER IF NOT EXISTS dep_author_update AFTER UPDATE OF verified ON bot_registry WHEN OLD.verified<>NEW.verified BEGIN INSERT OR IGNORE INTO dirty_dependencies VALUES('author:'||NEW.account_id); END")
        self._conn.execute("CREATE TRIGGER IF NOT EXISTS dep_policy_update AFTER UPDATE OF value_json ON runtime_policy WHEN OLD.value_json<>NEW.value_json AND NEW.key IN ('background_version','trusted_background','platform_levels','capabilities_version') BEGIN INSERT OR IGNORE INTO dirty_dependencies VALUES('policy'); END")
        self._conn.execute("CREATE TRIGGER IF NOT EXISTS dep_policy_insert AFTER INSERT ON runtime_policy WHEN NEW.key IN ('background_version','trusted_background','platform_levels','capabilities_version') BEGIN INSERT OR IGNORE INTO dirty_dependencies VALUES('policy'); END")
        self._conn.execute('PRAGMA user_version=6')

    def audit_operation(self, actor, operation, subject, detail):
        self._conn.execute('INSERT INTO operation_audit(created_at,actor,operation,subject,detail_json) VALUES(?,?,?,?,?)',(time.time(),str(actor),operation,str(subject),_json(detail)))

    def effective_phase(self, *, now=None):
        ts=time.time() if now is None else now
        override=self.policy('phase_override',{})
        if override.get('until',0)>ts:return override['phase']
        if self.policy('phase_auto',False):
            times=self.policy('phase_times',PHASE_TIMES)
            chosen='startup'
            for phase in ('startup','shaping','climax'):
                if ts >= datetime.fromisoformat(times[phase]).replace(tzinfo=SYDNEY).timestamp():chosen=phase
            return chosen
        phase=self.policy('phase','startup')
        return 'startup' if phase=='warmup' else phase

    def override_phase(self, phase, until, *, actor='cli', now=None):
        ts=time.time() if now is None else now
        phase='startup' if phase=='warmup' else phase
        if phase not in PHASE_TIMES or not math.isfinite(until) or until<=ts:raise ValueError('valid phase and future override expiry required')
        with self.batch_transaction():
            self.set_policy('phase_override',{'phase':phase,'until':until})
            self.audit_operation(actor,'phase_override',phase,{'until':until})

    def configure_campaign(self, daily_original_limit, *, actor='cli'):
        if type(daily_original_limit) is not int or daily_original_limit<0:raise ValueError('nonnegative integer daily limit required')
        with self.batch_transaction():
            self.set_policy('daily_original_limit',daily_original_limit)
            self.audit_operation(actor,'daily_original_limit','campaign',{'value':daily_original_limit})

    def set_account_policy(self, account, *, mode=None, capability_tier=None, original_locked_until=None):
        with self.batch_transaction():
            super().set_account_policy(account,mode=mode,capability_tier=capability_tier,original_locked_until=original_locked_until)
            if mode is not None:self.set_policy('manual_stop:'+account,mode!='active')

    def initialise_registered_account(self, account):
        with self.batch_transaction():
            if not self._conn.execute('SELECT 1 FROM persona_bindings WHERE account_id=?',(account,)).fetchone():raise ValueError('bound account required')
            self._conn.execute('INSERT OR IGNORE INTO account_lifecycle(account_id) VALUES(?)',(account,))
            # Only registration's explicit transition activates a new binding.
            if not self.policy('manual_stop:'+account,False):
                super().set_account_policy(account,mode='active')

    def lifecycle(self, account):
        with self._lock:
            row=self._conn.execute('SELECT * FROM account_lifecycle WHERE account_id=?',(account,)).fetchone()
            return dict(row) if row else None

    def account_initialise_ready(self, account, *, now=None):
        ts=time.time() if now is None else now
        state=self.lifecycle(account)
        if state and state['initialise_status']=='ready':return True
        if self.effective_phase(now=ts)!='climax':return False
        with self.batch_transaction():
            if self._conn.execute("SELECT 1 FROM action_intents WHERE account_id=? AND kind IN ('post','reply') AND status IN ('claimed','sending','unknown')",(account,)).fetchone():return False
            self._conn.execute("INSERT INTO account_lifecycle(account_id,initialise_status,waiver_reason) VALUES(?,'waived','campaign_climax') ON CONFLICT(account_id) DO UPDATE SET initialise_status='waived',waiver_reason='campaign_climax' WHERE initialise_status NOT IN ('ready','sending','unknown')",(account,))
        return bool(self.lifecycle(account)['initialise_status'] in ('ready','waived'))

    def initialise_action_changed(self, action, status, ts):
        meta=json.loads(action['metadata_json'])
        if action['kind']!='post' or meta.get('purpose') not in ('warmup','initialise'):return
        state={'succeeded':'ready','unknown':'unknown','failed':'pending','sending':'sending','claimed':'sending'}.get(status,'pending')
        self._conn.execute('INSERT INTO account_lifecycle(account_id,initialise_status,initialise_action,initialised_at,next_attempt_at) VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET initialise_status=excluded.initialise_status,initialise_action=excluded.initialise_action,initialised_at=MAX(initialised_at,excluded.initialised_at),next_attempt_at=excluded.next_attempt_at',
            (action['account_id'],state,action['id'],ts if status=='succeeded' else 0,ts+300 if status=='failed' else 0))

    def set_maintenance(self, account, enabled, reason, *, actor='cli'):
        if not reason.strip():raise ValueError('reason required')
        state=self.lifecycle(account)
        if not state or state['initialise_status'] not in ('ready','waived'):raise ValueError('eligible account required')
        with self.batch_transaction():
            self._conn.execute('UPDATE account_lifecycle SET maintenance=? WHERE account_id=?',(int(enabled),account))
            self.audit_operation(actor,'maintenance_admit' if enabled else 'maintenance_release',account,{'reason':reason})

    def record_quality(self,account,post_id,passed,reason,stage):
        with self.batch_transaction():
            self._conn.execute('INSERT INTO quality_observations(account_id,post_id,passed,reason,stage,created_at) VALUES(?,?,?,?,?,?)',(account,post_id,int(passed),reason,stage,time.time()))

    def runtime_metrics(self):
        result=super().runtime_metrics()
        with self._lock:
            for key,table,column in [('initialisation','account_lifecycle','initialise_status'),('macro_jobs','macro_jobs','status'),('report_reviews','report_reviews','status')]:
                result[key]={r[0]:r[1] for r in self._conn.execute(f'SELECT {column},COUNT(*) FROM {table} GROUP BY {column}')}
        result['phase']=self.effective_phase()
        return result

    def maintenance_recommendations(self):
        report=self.performance_report(group_by='persona_id')
        bindings=self.persona_bindings()
        rows=[]
        for group in report['groups']:
            account=bindings.get(group['group'],'')
            group['account_id']=account;group['lifecycle']=self.lifecycle(account)
            now=time.time();midnight=datetime.now(SYDNEY).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            with self._lock:
                quality=self._conn.execute("SELECT COUNT(*) AS count,SUM(1-passed) AS rejected FROM quality_observations WHERE account_id=? AND created_at>=? AND stage<>'initialise'",(account,now-86400)).fetchone()
                activity=self._conn.execute("SELECT action,purpose,COUNT(*) AS count FROM interaction_log WHERE account_id=? AND created_at>=? AND action IN ('post','reply') GROUP BY action,purpose",(account,midnight)).fetchall()
            group['quality_review']={'samples':quality['count'],'rejected':quality['rejected']}
            counts={'initialise':0,'original':0,'reply':0}
            for row in activity:counts['reply' if row['action']=='reply' else ('initialise' if row['purpose'] in ('initialise','warmup') else 'original')]+=row['count']
            group['activity_today']={**counts,'total':sum(counts.values())}
            rows.append(group)
        rows.sort(key=lambda r:(-(r['followers_net_change']['total'] or 0),-(r['metrics']['likes']['median'] or 0),r['account_id']))
        return {'recommendations':rows,'automatic_promotion':False,'limits':report['limits']}

    def notify_discord(self, key, payload):
        self._conn.execute('INSERT OR IGNORE INTO discord_outbox(event_key,payload_json) VALUES(?,?)',(key,_json(payload)))

    def claim_discord_outbox(self, *, now=None):
        ts=time.time() if now is None else now;owner=uuid.uuid4().hex
        with self.batch_transaction():
            row=self._conn.execute("SELECT * FROM discord_outbox WHERE (status='queued' AND next_attempt_at<=?) OR (status='claimed' AND lease_until<=?) ORDER BY id LIMIT 1",(ts,ts)).fetchone()
            if not row:return None
            self._conn.execute("UPDATE discord_outbox SET status='claimed',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",(owner,ts+120,row['id']))
            return {**dict(row),'owner':owner}

    def finish_discord_outbox(self, job, *, ok, now=None):
        ts=time.time() if now is None else now
        with self.batch_transaction():
            return self._conn.execute("UPDATE discord_outbox SET status=?,next_attempt_at=?,lease_until=0 WHERE id=? AND owner=? AND status='claimed'",('sent' if ok else 'queued',ts+min(300,2**min(job['attempts']+1,8)),job['id'],job['owner'])).rowcount==1

    def report_review(self, candidate):
        with self._lock:
            r=self._conn.execute('SELECT * FROM report_reviews WHERE candidate_id=?',(candidate,)).fetchone()
            return dict(r) if r else None

    def refresh_report_reviews(self, *, now=None):
        ts=time.time() if now is None else now
        with self.batch_transaction():
            rows=self._conn.execute("SELECT * FROM report_reviews r WHERE (status='deferred' AND resume_at<=?) OR (status='approved' AND NOT EXISTS(SELECT 1 FROM report_approvals a WHERE a.candidate_id=r.candidate_id AND (a.expires_at>? OR a.consumed_by<>'')))",(ts,ts)).fetchall()
            for row in rows:
                self._conn.execute("UPDATE report_reviews SET status='review',revision=revision+1,updated_at=? WHERE candidate_id=?",(ts,row['candidate_id']))
                self.notify_discord(f"review:{row['candidate_id']}:{row['revision']+1}",{'type':'candidate','candidate_id':row['candidate_id']})
            for row in self._conn.execute("SELECT id FROM report_attempts WHERE status='pending' AND submitted_at<?",(ts-86400,)).fetchall():
                self.notify_discord('aged:'+row['id'],{'type':'aged_pending','attempt_id':row['id']})

    def review_report(self, candidate, operation, *, revision=None, actor='cli', interaction_id=None, reason='', resume_at=None, now=None):
        ts=time.time() if now is None else now
        if operation not in ('approve','reject','defer'):raise ValueError('invalid review operation')
        if operation in ('reject','defer') and not reason.strip():raise ValueError('review reason required')
        with self.batch_transaction():
            if interaction_id:
                prior=self._conn.execute('SELECT result_json FROM review_interactions WHERE interaction_id=?',(str(interaction_id),)).fetchone()
                if prior:return json.loads(prior[0])
            self.refresh_report_reviews(now=ts)
            row=self.report_review(candidate)
            if not row or row['status']!='review' or revision is not None and row['revision']!=revision:raise ValueError('stale or already reviewed candidate')
            from ctn.moderation.policy import candidate_payload
            stored=self._conn.execute('SELECT * FROM report_candidates WHERE id=?',(candidate,)).fetchone()
            current=candidate_payload(self,stored['subject_kind'],stored['subject_id'],stored['author'])
            if not current or current['evidence_hash']!=stored['evidence_hash']:raise ValueError('report evidence changed')
            approval=''
            if operation=='approve':approval=super().approve_report(candidate,now=ts)
            state={'approve':'approved','reject':'rejected','defer':'deferred'}[operation]
            until=ts+1800 if resume_at is None else resume_at
            if not math.isfinite(until) or until<=ts:raise ValueError('future defer time required')
            self._conn.execute('UPDATE report_reviews SET status=?,resume_at=?,revision=revision+1,updated_at=? WHERE candidate_id=?',(state,until if operation=='defer' else 0,ts,candidate))
            result={'candidate_id':candidate,'status':state,'revision':row['revision']+1,'approval_id':approval}
            self.audit_operation(actor,'report_'+operation,candidate,{'reason':reason,**result})
            self.notify_discord(f"review:{candidate}:{row['revision']+1}",{'type':'candidate','candidate_id':candidate})
            if interaction_id:self._conn.execute('INSERT INTO review_interactions VALUES(?,?)',(str(interaction_id),_json(result)))
            return result

    def approve_report(self, candidate_id, *, now=None):
        # CLI compatibility: repeated approval returns the original receipt.
        with self.batch_transaction():
            self.refresh_report_reviews(now=now)
            row=self.report_review(candidate_id)
            if row and row['status'] in ('approved','sending','pending','unknown'):
                prior=self._conn.execute('SELECT id FROM report_approvals WHERE candidate_id=? ORDER BY expires_at DESC LIMIT 1',(candidate_id,)).fetchone()
                if prior:return prior[0]
            return self.review_report(candidate_id,'approve',now=now)['approval_id']

    def store_report_candidate(self, payload):
        with self.batch_transaction():
            ident=super().store_report_candidate(payload)
            stale=self._conn.execute("SELECT candidate_id,revision FROM report_reviews WHERE candidate_id IN (SELECT id FROM report_candidates WHERE subject_kind=? AND subject_id=? AND id<>?) AND status IN ('review','approved','deferred')",(payload['subject_kind'],payload['subject_id'],ident)).fetchall()
            self._conn.execute("UPDATE report_reviews SET status='stale',revision=revision+1,updated_at=? WHERE candidate_id IN (SELECT id FROM report_candidates WHERE subject_kind=? AND subject_id=? AND id<>?) AND status IN ('review','approved','deferred')",(time.time(),payload['subject_kind'],payload['subject_id'],ident))
            for row in stale:self.notify_discord(f"stale:{row['candidate_id']}:{row['revision']+1}",{'type':'candidate','candidate_id':row['candidate_id']})
            created=self._conn.execute('INSERT OR IGNORE INTO report_reviews(candidate_id,updated_at) VALUES(?,?)',(ident,time.time())).rowcount
            if created:self.notify_discord('candidate:'+ident,{'type':'candidate','candidate_id':ident})
            return ident

    def report_state_ready(self, *, now=None, automatic=False):
        if self.policy('reporting_paused',False):return False,'reporting_paused'
        return super().report_state_ready(now=now,automatic=automatic)

    def report_authorization(self, kind, target, meta, *, now=None, sending=False):
        row=self.report_review(meta.get('candidate_id',''))
        if row and row['status'] not in ('approved','sending'):
            from ctn.moderation.decision import automatic_authorized
            author=meta.get('target_author',target)
            auto=(self.policy('report_mode','off')=='hybrid' and row['status']=='review' and meta.get('decision_action')=='auto'
                  and automatic_authorized(self,kind,target,author,meta,now=now))
            if not auto:return False,'human_approval_required'
        return super().report_authorization(kind,target,meta,now=now,sending=sending)

    def set_report_clearance(self, source, *, actor='cli', now=None):
        ts=time.time() if now is None else now
        if not source.strip():raise ValueError('official observation source required')
        if self.is_reporting_suspended(now=ts)[0]:raise ValueError('reporting suspension is active')
        with self.batch_transaction():
            from ctn.moderation.decision import policy as decision_policy
            ttl=float(decision_policy(self)['manual_clearance_ttl_seconds'])
            self.set_policy('report_clearance',{'source':source,'allowed':True,'expires_at':ts+ttl})
            self.set_policy('report_forbidden',{})
            self.set_policy('reporting_paused',False)
            self.audit_operation(actor,'report_clearance','campaign',{'source':source,'expires_at':ts+ttl})

    def apply_moderation_event(self, event):
        # Explicit attempt binding is mandatory for v6 imports against known attempts.
        with self.batch_transaction():
            if isinstance(event,dict):
                existing=self._conn.execute('SELECT * FROM ruling_receipts WHERE attempt_id=?',(event.get('attempt_id',''),)).fetchone()
                if existing and (existing['outcome']!=event.get('outcome') or existing['ruled_at']!=event.get('ruled_at')):
                    raise ValueError('conflicting ruling')
            result=super().apply_moderation_event(event)
            if result:
                attempt=self._conn.execute('SELECT payload_json FROM report_attempts WHERE id=?',(event.get('attempt_id',''),)).fetchone()
                if attempt:
                    candidate=json.loads(attempt['payload_json']).get('candidate_id','')
                    self._conn.execute("UPDATE report_reviews SET status='ruled',revision=revision+1,updated_at=? WHERE candidate_id=?",(time.time(),candidate))
                    self.notify_discord('ruled_candidate:'+event['event_id'],{'type':'candidate','candidate_id':candidate})
                self.audit_operation(event.get('actor','official'),'ruling',event.get('attempt_id',event['subject_id']),event)
                self.notify_discord('ruling:'+event['event_id'],{'type':'ruling','event':event})
            return result

    def import_ruling(self, event, *, actor='cli'):
        if not event.get('attempt_id') and not event.get('report_id'):raise ValueError('explicit attempt_id or report_id required')
        event={**event,'actor':actor}
        try:
            if not event.get('attempt_id'):
                with self._lock:matches=self._conn.execute('SELECT id FROM report_attempts WHERE report_id=?',(event['report_id'],)).fetchall()
                if len(matches)!=1:raise ValueError('ambiguous report ID')
                event['attempt_id']=matches[0]['id']
            if not isinstance(event.get('ruled_at'),(int,float)) or not math.isfinite(event['ruled_at']) or event['ruled_at']>time.time()+300:raise ValueError('invalid ruling time')
            return self.apply_moderation_event(event)
        except (ValueError,TypeError,KeyError):
            with self.batch_transaction():
                key=hashlib.sha256(_json(event).encode()).hexdigest()
                self._conn.execute('INSERT OR IGNORE INTO quarantined_events VALUES(?,?,?)',(key,'invalid_or_conflicting_ruling',time.time()))
            raise

    def claim_macro_job(self, *, now=None, mode="live"):
        ts=time.time() if now is None else now
        bucket=int(ts//7200)
        if mode not in ('live','simulated'):raise ValueError('invalid macro execution mode')
        ident=f'macro:{mode}:{bucket}'
        with self.batch_transaction():
            self._conn.execute("UPDATE macro_jobs SET status='expired',reason='pulse_expired' WHERE status='queued' AND scheduled_at<?",(bucket*7200,))
            self._conn.execute("UPDATE macro_jobs SET status='queued',reason='worker_recovered' WHERE status='claimed' AND lease_until<=?",(ts,))
            self._conn.execute('INSERT OR IGNORE INTO macro_jobs(id,scheduled_at) VALUES(?,?)',(ident,ts))
            row=self._conn.execute("SELECT * FROM macro_jobs WHERE status='queued' AND id LIKE ? ORDER BY scheduled_at LIMIT 1",(f'macro:{mode}:%',)).fetchone()
            if not row:return None
            owner=uuid.uuid4().hex
            self._conn.execute("UPDATE macro_jobs SET status='claimed',owner=?,lease_until=? WHERE id=?",(owner,ts+600,row['id']))
            return {**dict(row),'owner':owner}

    def finish_macro_job(self, job, status, payload=None, reason=''):
        if status not in ('succeeded','skipped','failed','unknown','simulated'):raise ValueError('invalid macro result')
        with self.batch_transaction():
            return self._conn.execute('UPDATE macro_jobs SET status=?,payload_json=?,reason=? WHERE id=? AND owner=? AND status=\'claimed\'',(status,_json(payload or {}),reason,job['id'],job['owner'])).rowcount==1
