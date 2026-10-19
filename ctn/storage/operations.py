"""Additive v5 source archives, reviewed report attempts and shared publication gates."""
from __future__ import annotations
import hashlib
import json
import time
import uuid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS rag_snapshots(id TEXT PRIMARY KEY,body_json TEXT NOT NULL,created_at REAL NOT NULL,revoked_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS source_archives(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,version TEXT NOT NULL,text TEXT NOT NULL,metadata_json TEXT NOT NULL,created_at REAL NOT NULL,UNIQUE(source_id,version));
CREATE TABLE IF NOT EXISTS publication_texts(action_id TEXT PRIMARY KEY,text_hash TEXT NOT NULL,normalized_text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_publication_text_hash ON publication_texts(text_hash);
CREATE TABLE IF NOT EXISTS rag_claims(id TEXT PRIMARY KEY,kind TEXT NOT NULL,text TEXT NOT NULL,scope TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claim_provenance(claim_id TEXT NOT NULL,archive_id TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,PRIMARY KEY(claim_id,archive_id,start,end));
CREATE TABLE IF NOT EXISTS task_dependencies(post_id TEXT NOT NULL,version INTEGER NOT NULL,fingerprint TEXT NOT NULL,PRIMARY KEY(post_id,version));
CREATE TABLE IF NOT EXISTS registration_intents(account_id TEXT PRIMARY KEY,persona_id TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,detail TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS account_readiness(account_id TEXT PRIMARY KEY,warmed_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS report_candidates(id TEXT PRIMARY KEY,subject_kind TEXT NOT NULL,subject_id TEXT NOT NULL,author TEXT NOT NULL,payload_json TEXT NOT NULL,evidence_hash TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS report_approvals(id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,evidence_hash TEXT NOT NULL,expires_at REAL NOT NULL,consumed_by TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS report_attempts(id TEXT PRIMARY KEY,action_id TEXT NOT NULL UNIQUE,subject_kind TEXT NOT NULL,subject_id TEXT NOT NULL,author TEXT NOT NULL,report_id TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,submitted_at REAL NOT NULL,ruled_at REAL NOT NULL DEFAULT 0,outcome TEXT NOT NULL DEFAULT '',payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_report_attempt_pending ON report_attempts(status,author);
CREATE INDEX IF NOT EXISTS idx_report_attempt_subject ON report_attempts(subject_kind,subject_id,submitted_at);
CREATE TABLE IF NOT EXISTS ruling_receipts(event_id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL UNIQUE,outcome TEXT NOT NULL,ruled_at REAL NOT NULL,source TEXT NOT NULL,raw_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quarantined_events(event_hash TEXT PRIMARY KEY,reason TEXT NOT NULL,created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_actions_text_state ON action_intents(kind,status);
'''


class OperationsStoreMixin:
    def migrate_operations(self):
        for statement in SCHEMA.split(';'):
            if statement.strip(): self._conn.execute(statement)
        from .sqlite_store import normalize_text
        for row in self._conn.execute("SELECT id,text FROM action_intents WHERE kind IN ('post','reply') AND id NOT IN (SELECT action_id FROM publication_texts)").fetchall():
            normalized=normalize_text(row['text'])
            self._conn.execute('INSERT INTO publication_texts VALUES(?,?,?)',(row['id'],hashlib.sha256(normalized.encode()).hexdigest(),normalized))
        # Replace target-only uniqueness with version uniqueness, preserving slots and IDs.
        sql=self._conn.execute("SELECT sql FROM sqlite_master WHERE name='reply_batches'").fetchone()[0]
        if 'target_id TEXT NOT NULL UNIQUE' in sql:
            self._conn.execute('ALTER TABLE reply_batches RENAME TO reply_batches_v4')
            self._conn.execute(sql.replace('target_id TEXT NOT NULL UNIQUE','target_id TEXT NOT NULL').replace(
                "reason TEXT NOT NULL DEFAULT ''", "reason TEXT NOT NULL DEFAULT '', UNIQUE(target_id,target_version)"))
            self._conn.execute('INSERT INTO reply_batches SELECT * FROM reply_batches_v4')
            self._conn.execute('DROP TABLE reply_batches_v4')
            self._conn.execute('CREATE INDEX IF NOT EXISTS idx_batches_state ON reply_batches(status,expires_at)')

    def dependency_fingerprint(self, post_id, version, corpus_hash):
        from ctn.rag.corpus import digest
        row=self.get_observed_post(post_id) or {}
        raw=row.get('raw') or {}
        related=[]
        for key in ('parent_id','root_post_id','quoted_post_id'):
            ident=str(row.get(key) or raw.get(key) or '')
            other=self.get_observed_post(ident) if ident else None
            related.append([ident,other.get('version') if other else None,other.get('text') if other else None,(other.get('raw') or {}).get('text_complete') if other else None])
        prof=self.get_bot_profile(row.get('author','')) if row.get('author') else {}
        return digest([post_id,version,corpus_hash,related,
            [self.policy(k) for k in ('background_version','trusted_background','platform_levels','capabilities_version')],
            raw.get('platform_level'),raw.get('author_level'),raw.get('verified'),raw.get('text_complete'),prof.get('verified')])

    def wait_for_material(self, task, corpus_hash, reason):
        with self.batch_transaction():
            fingerprint=self.dependency_fingerprint(task['post_id'],task['version'],corpus_hash)
            self._conn.execute('INSERT INTO task_dependencies VALUES(?,?,?) ON CONFLICT(post_id,version) DO UPDATE SET fingerprint=excluded.fingerprint',
                (task['post_id'],task['version'],fingerprint))
            row=self.get_observed_post(task['post_id']) or {};raw=row.get('raw') or {}
            keys={'post:'+task['post_id'],'author:'+row.get('author',''),'corpus','policy'}
            keys.update('post:'+str(row.get(k) or raw.get(k)) for k in ('parent_id','root_post_id','quoted_post_id') if row.get(k) or raw.get(k))
            self._conn.execute('DELETE FROM task_dependency_objects WHERE post_id=? AND version=?',(task['post_id'],task['version']))
            self._conn.executemany('INSERT OR IGNORE INTO task_dependency_objects VALUES(?,?,?)',[(task['post_id'],task['version'],k) for k in keys])
            self.update_post_task(task,'waiting_materials',reason=reason)
            self._conn.execute('UPDATE post_tasks SET attempts=MAX(0,attempts-1) WHERE post_id=? AND version=?',(task['post_id'],task['version']))

    def resume_material_tasks(self, corpus_hash, *, now=None):
        ts=time.time() if now is None else now
        with self.batch_transaction():
            if self.policy('resume_corpus_hash')!=corpus_hash:
                self.set_policy('resume_corpus_hash',corpus_hash)
                self._conn.execute("INSERT OR IGNORE INTO dirty_dependencies VALUES('corpus')")
            self._conn.execute("UPDATE post_tasks SET status='expired',reason='opportunity_expired' WHERE status='waiting_materials' AND expires_at<=?",(ts,))
            rows=self._conn.execute("SELECT DISTINCT t.post_id,t.version,d.fingerprint FROM dirty_dependencies k JOIN task_dependency_objects o USING(object_key) JOIN post_tasks t USING(post_id,version) JOIN task_dependencies d USING(post_id,version) WHERE t.status='waiting_materials'").fetchall()
            for row in rows:
                if row['fingerprint']!=self.dependency_fingerprint(row['post_id'],row['version'],corpus_hash):
                    self._conn.execute("UPDATE post_tasks SET status='queued',reason='dependency_updated',next_attempt_at=? WHERE post_id=? AND version=?",(ts,row['post_id'],row['version']))
            self._conn.execute('DELETE FROM dirty_dependencies')

    def save_rag_snapshot(self, snapshot):
        from ctn.rag.corpus import RagSnapshot
        if not isinstance(snapshot,RagSnapshot): raise ValueError('typed RAG snapshot required')
        with self.batch_transaction():
            self._conn.execute('INSERT OR IGNORE INTO rag_snapshots(id,body_json,created_at) VALUES(?,?,?)',
                (snapshot.snapshot_id,snapshot.body_json,time.time()))
        return snapshot.snapshot_id

    def load_rag_snapshot(self, ident):
        from ctn.rag.corpus import RagSnapshot
        with self._lock:
            row=self._conn.execute('SELECT * FROM rag_snapshots WHERE id=?',(ident,)).fetchone()
        if not row or row['revoked_at']: raise ValueError('RAG snapshot missing or withdrawn')
        snapshot=RagSnapshot(row['body_json'])
        if snapshot.snapshot_id!=ident: raise ValueError('RAG snapshot hash mismatch')
        return snapshot

    def revoke_rag_snapshot(self, ident):
        with self.batch_transaction():
            self._conn.execute('UPDATE rag_snapshots SET revoked_at=? WHERE id=?',(time.time(),ident))

    def revoke_rag_reference(self, reference_id, corpus_hash):
        if not reference_id or not corpus_hash:raise ValueError('reference ID and corpus hash required')
        affected=[]
        with self.batch_transaction():
            rows=self._conn.execute("SELECT id,body_json FROM rag_snapshots WHERE revoked_at=0 AND json_extract(body_json,'$.corpus_hash')=?",(corpus_hash,)).fetchall()
            for row in rows:
                data=json.loads(row['body_json'])
                if any(record['id']==reference_id for key in ('facts','arguments','unknowns','styles') for record in data[key]):
                    self.revoke_rag_snapshot(row['id']);affected.append(row['id'])
        return affected

    def archive_source(self, source_id, version, text, metadata):
        if not all(isinstance(v,str) and v.strip() for v in (source_id,version,text)):
            raise ValueError('source, version and original text required')
        body=json.dumps(metadata,sort_keys=True,ensure_ascii=False,allow_nan=False)
        ident=hashlib.sha256(json.dumps([source_id,version,text,body]).encode()).hexdigest()
        with self.batch_transaction():
            old=self._conn.execute('SELECT id FROM source_archives WHERE source_id=? AND version=?',(source_id,version)).fetchone()
            if old and old[0]!=ident: raise ValueError('source version is immutable')
            self._conn.execute('INSERT OR IGNORE INTO source_archives VALUES(?,?,?,?,?,?)',
                (ident,source_id,version,text,body,time.time()))
        return ident

    def record_claim(self, kind, text, scope, archive_id, start, end):
        """Register a curated atomic claim, merging exact claims and retaining provenance.

        Scope includes speaker and time for attributed claims. This validates
        source locations, not the truth of an arbitrary curator's paraphrase.
        """
        from .sqlite_store import normalize_text
        from ctn.rag.corpus import digest
        if kind not in ('official_fact','attributed_claim') or not text.strip() or not scope.strip():
            raise ValueError('typed claim and explicit scope required')
        with self.batch_transaction():
            source=self._conn.execute('SELECT * FROM source_archives WHERE id=?',(archive_id,)).fetchone()
            if not source or type(start) is not int or type(end) is not int or not 0<=start<end<=len(source['text']):
                raise ValueError('invalid original source location')
            if kind=='official_fact' and json.loads(source['metadata_json']).get('kind')!='official_setting':
                raise ValueError('news and editorial claims cannot become official facts')
            ident=digest([kind,normalize_text(text),scope])
            self._conn.execute('INSERT OR IGNORE INTO rag_claims VALUES(?,?,?,?)',(ident,kind,text,scope))
            self._conn.execute('INSERT OR IGNORE INTO claim_provenance VALUES(?,?,?,?)',(ident,archive_id,start,end))
        return ident

    def publication_references(self, *, exclude_action='', limit=1000):
        with self._lock:
            rows=self._conn.execute("SELECT text FROM action_intents WHERE kind IN ('post','reply') AND status IN ('claimed','sending','unknown','succeeded') AND id<>? ORDER BY updated_at DESC LIMIT ?",(exclude_action,limit)).fetchall()
        return [r[0] for r in rows]

    def publication_gate(self, text, metadata, *, exclude_action=''):
        from ctn.storage.sqlite_store import normalize_text
        from ctn.rag.corpus import too_close
        if not text.strip() or len(text)>280: return False,'text_constraint'
        ok,reason=self.message_policy_gate(text,exclude_action=exclude_action)
        if not ok:return False,reason
        normalized=normalize_text(text)
        fingerprint=hashlib.sha256(normalized.encode()).hexdigest()
        if self._conn.execute("SELECT 1 FROM publication_texts t JOIN action_intents a ON a.id=t.action_id WHERE t.text_hash=? AND a.id<>? AND a.status IN ('claimed','sending','unknown','succeeded') LIMIT 1",(fingerprint,exclude_action)).fetchone():
            return False,'duplicate_publication'
        refs=self.publication_references(exclude_action=exclude_action)
        if any(normalize_text(r)==normalized for r in refs): return False,'duplicate_publication'
        if too_close(text,refs): return False,'near_duplicate_publication'
        if metadata.get('rag_snapshot_id'):
            try:
                snap=self.load_rag_snapshot(metadata['rag_snapshot_id']).to_dict()
                if snap['target_id']:
                    target=self.get_observed_post(snap['target_id'])
                    if not target or target['version']!=snap['target_version']:return False,'target_edited'
            except ValueError:return False,'rag_snapshot_withdrawn'
        return True,'ready'

    def report_state_ready(self, *, now=None, automatic=False):
        """Automatic sends need hybrid mode, shadow off and no recent report 403.
        Human-approved sends additionally need an operator availability confirmation."""
        ts=time.time() if now is None else now
        if self.is_reporting_suspended(now=ts)[0]: return False,'reporting_suspended'
        if automatic:
            from ctn.moderation.decision import automatic_ready
            return automatic_ready(self,now=ts)
        state=self.policy('report_clearance',{})
        if not state.get('source') or state.get('allowed') is not True or state.get('expires_at',0)<=ts:
            return False,'reporting_state_unknown'
        return True,'ready'

    @staticmethod
    def report_candidate_id(payload):
        from ctn.rag.corpus import digest
        return digest([payload['subject_kind'],payload['subject_id'],payload['author'],payload['evidence_hash'],payload['target_version'],payload['policy_version']])

    def store_report_candidate(self, payload):
        ident=self.report_candidate_id(payload)
        with self.batch_transaction():
            self._conn.execute('INSERT OR IGNORE INTO report_candidates VALUES(?,?,?,?,?,?,?)',
                (ident,payload['subject_kind'],payload['subject_id'],payload['author'],
                 json.dumps(payload,sort_keys=True),payload['evidence_hash'],time.time()))
        return ident

    def approve_report(self, candidate_id, *, now=None):
        from ctn.moderation.policy import POLICY
        ts=time.time() if now is None else now
        with self.batch_transaction():
            row=self._conn.execute('SELECT * FROM report_candidates WHERE id=?',(candidate_id,)).fetchone()
            if not row: raise ValueError('unknown report candidate')
            ident=uuid.uuid4().hex
            self._conn.execute('INSERT INTO report_approvals VALUES(?,?,?,?,?)',
                (ident,candidate_id,row['evidence_hash'],ts+POLICY['approval_ttl_seconds'],''))
        return ident

    def report_authorization(self, kind, target, meta, *, now=None, sending=False):
        from ctn.moderation.policy import candidate_payload
        ts=time.time() if now is None else now
        mode=self.policy('report_mode','off')
        if mode=='off': return False,'reports_disabled'
        if meta.get('reason','inauthentic')!='inauthentic':return False,'invalid_report_reason'
        subject='account' if kind=='report_account' else 'post'
        author=meta.get('target_author') or target
        current=candidate_payload(self,subject,target,author)
        if not current or current['evidence_hash']!=meta.get('evidence_hash') or current['detail']!=meta.get('detail'):
            return False,'report_evidence_changed'
        from ctn.moderation.decision import automatic_authorized
        auto=mode=='hybrid' and meta.get('decision_action')=='auto' and automatic_authorized(self,kind,target,author,meta,now=ts)
        ready,reason=self.report_state_ready(now=ts,automatic=auto)
        if not ready:return False,reason
        if subject=='account':
            activity=self.confirmed_activity(author)
            if not activity or activity['action_count']<100 or self.get_bot_profile(author)['upheld_strikes']<1:
                return False,'account_report_not_eligible'
        if subject=='account':
            prior=self._conn.execute("SELECT payload_json,ruled_at FROM report_attempts WHERE subject_kind='account' AND subject_id=? AND outcome='dismissed' ORDER BY ruled_at DESC LIMIT 1",(target,)).fetchone()
            if prior and (ts<prior['ruled_at']+300 or json.loads(prior['payload_json']).get('evidence_hash')==current['evidence_hash']):
                return False,'account_report_wait_for_new_evidence'
        if auto:return True,'strong_evidence'
        approval=self._conn.execute('SELECT * FROM report_approvals WHERE id=?',(meta.get('approval_id',''),)).fetchone()
        if (not approval or approval['expires_at']<=ts or approval['candidate_id']!=meta.get('candidate_id')
            or approval['evidence_hash']!=current['evidence_hash']
            or (approval['consumed_by'] and (not sending or approval['consumed_by']!=meta.get('_action_id')))):
            return False,'human_approval_required'
        return True,'approved'

    def apply_moderation_event(self, event):
        """Prefer attempt-bound v5 events; retain legacy history without inventing IDs."""
        import math
        required={'event_id','subject_kind','subject_id','outcome','ruled_at','source'}
        if (not isinstance(event,dict) or not required<=event.keys() or not event['event_id'] or
            event['subject_kind'] not in ('post','account') or event['outcome'] not in ('upheld','rejected','dismissed') or
            not event['source'] or not isinstance(event['ruled_at'],(int,float)) or not math.isfinite(event['ruled_at']) or event['ruled_at']<=0):
            raise ValueError('invalid explicit moderation event')
        with self.batch_transaction():
            if self._conn.execute('SELECT 1 FROM moderation_events WHERE event_id=?',(event['event_id'],)).fetchone():return False
            attempts=self._conn.execute('SELECT * FROM report_attempts WHERE subject_kind=? AND subject_id=?',
                (event['subject_kind'],str(event['subject_id']))).fetchall()
            if not attempts:return super().apply_moderation_event(event)
            matches=[r for r in attempts if (event.get('attempt_id')==r['id'] or
                (event.get('report_id') and event['report_id']==r['report_id']))]
            if not matches and len(attempts)==1 and not event.get('attempt_id') and not event.get('report_id'):matches=attempts
            if len(matches)!=1:raise ValueError('ambiguous ruling attempt')
            r=matches[0]
            if event['ruled_at']<r['submitted_at']:raise ValueError('ruling predates submission')
            if r['status'] not in ('pending','unknown','ruled'):raise ValueError('report was not sent')
            if r['status']=='ruled':return False
            action=self._conn.execute('SELECT status FROM action_intents WHERE id=?',(r['action_id'],)).fetchone()
            if action and action[0]=='unknown':
                self.finish_action(r['action_id'],'succeeded',response={'report_id':r['report_id'] or event.get('report_id','')},detail='submission confirmed by explicit ruling',now=r['submitted_at'])
            self._conn.execute('INSERT INTO ruling_receipts VALUES(?,?,?,?,?,?)',
                (event['event_id'],r['id'],event['outcome'],event['ruled_at'],event['source'],json.dumps(event)))
            self._conn.execute("UPDATE report_attempts SET status='ruled',outcome=?,ruled_at=? WHERE id=?",
                (event['outcome'],event['ruled_at'],r['id']))
            if r['subject_kind']=='post':
                from ctn.moderation.decision import record_ruling
                try:record_ruling(self,json.loads(r['payload_json']),event['outcome'])
                except ValueError:pass
            self._conn.execute('INSERT OR IGNORE INTO moderation_events VALUES(?,?,?,?,?,?,?)',(event['event_id'],event['subject_kind'],str(event['subject_id']),event['outcome'],event['ruled_at'],event['source'],json.dumps(event)))
            # Historical mirror is updated only if it still represents this attempt.
            if r['subject_kind']=='post':
                from .sqlite_store import current_sydney_day
                self._conn.execute('UPDATE report_log SET outcome=?,ruled_at=?,ruling_day=? WHERE post_id=?',
                    (event['outcome'],event['ruled_at'],current_sydney_day(event['ruled_at']),r['subject_id']))
                if event['outcome']=='upheld':
                    self._conn.execute('UPDATE bot_registry SET upheld_strikes=upheld_strikes+1,is_suspended=CASE WHEN upheld_strikes+1>=3 THEN 1 ELSE is_suspended END WHERE account_id=?',(r['author'],))
            else:
                self._conn.execute('UPDATE account_reports SET outcome=?,ruled_at=? WHERE target_author=? AND submitted_at=?',
                    (event['outcome'],event['ruled_at'],r['author'],r['submitted_at']))
                if event['outcome']=='upheld':self._conn.execute('UPDATE bot_registry SET is_suspended=1 WHERE account_id=?',(r['author'],))
            if event['outcome']=='rejected':
                self.set_counter('report_suspended_until_ms',max(self.get_counter('report_suspended_until_ms'),int((event['ruled_at']+21600)*1000)))
                self.set_policy('report_clearance',{})
            if event['outcome'] in ('upheld','rejected'):
                self.add_weighted_evidence(r['author'],'moderator',5 if event['outcome']=='upheld' else -5,
                    {'event_id':event['event_id'],'outcome':event['outcome']},evidence_key='ruling:'+event['event_id'],now=event['ruled_at'])
            return True
