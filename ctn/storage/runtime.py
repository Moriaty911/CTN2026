"""Transactional runtime state shared by the Pi CLI and Python service.

No transaction in this module spans network I/O. A sending action whose owner
dies is UNKNOWN, never automatically replayed. Scores are evidence points,
not probabilities or platform truth.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

DEFAULT_EVIDENCE_POLICY=json.loads((Path(__file__).resolve().parents[2]/"policies/evidence-v1.json").read_text())
SEND_RECOVERY=json.loads((Path(__file__).resolve().parents[2]/"policies/send-recovery-v1.json").read_text())


def original_gap():
    """Random wait before an account's next original (policies/originals-v1.json `per_account`)."""
    from ctn.macro.schedule import original_gap as gap  # lazy: ctn.macro imports the storage layer
    return gap()

SCHEMA_VERSION = 6
RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_policy(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS action_intents(
 id TEXT PRIMARY KEY, action_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 account_id TEXT NOT NULL,target_id TEXT NOT NULL,text TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL,owner TEXT NOT NULL,lease_until REAL NOT NULL,
 created_at REAL NOT NULL,updated_at REAL NOT NULL,detail TEXT NOT NULL DEFAULT '',
 metadata_json TEXT NOT NULL DEFAULT '{}',response_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_intents_status ON action_intents(status,lease_until);
CREATE INDEX IF NOT EXISTS idx_intents_account ON action_intents(account_id,status);
CREATE TABLE IF NOT EXISTS unknown_checks(
 action_id TEXT PRIMARY KEY,unknown_at REAL NOT NULL,next_check_at REAL NOT NULL,state TEXT NOT NULL DEFAULT 'open',
 checks INTEGER NOT NULL DEFAULT 0,absent_checks INTEGER NOT NULL DEFAULT 0,last_checked_at REAL NOT NULL DEFAULT 0,
 last_detail TEXT NOT NULL DEFAULT '',abandoned_at REAL NOT NULL DEFAULT 0,resolved TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_unknown_due ON unknown_checks(state,next_check_at);
CREATE TABLE IF NOT EXISTS account_leases(
 account_id TEXT PRIMARY KEY,action_id TEXT NOT NULL,lease_until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS post_tasks(
 post_id TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'queued',
 next_attempt_at REAL NOT NULL,expires_at REAL NOT NULL,lease_until REAL NOT NULL DEFAULT 0,
 owner TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,
 draft_json TEXT NOT NULL DEFAULT '',draft_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(post_id,version));
CREATE INDEX IF NOT EXISTS idx_tasks_ready ON post_tasks(status,next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_tasks_expiry ON post_tasks(status,expires_at);
CREATE TABLE IF NOT EXISTS comment_queue(
 post_id TEXT PRIMARY KEY,priority INTEGER NOT NULL,next_fetch_at REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_comment_priority ON comment_queue(priority DESC,next_fetch_at);
CREATE TABLE IF NOT EXISTS request_slots(
 bucket TEXT PRIMARY KEY,next_at REAL NOT NULL DEFAULT 0,blocked_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS wire_slots(bucket TEXT PRIMARY KEY,next_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS request_attempts(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,buckets_json TEXT NOT NULL,status TEXT NOT NULL,
 due_at REAL NOT NULL,expires_at REAL NOT NULL,created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_request_reservations ON request_attempts(kind,status,expires_at);
CREATE TABLE IF NOT EXISTS bot_evidence(
 account_id TEXT NOT NULL,evidence_key TEXT NOT NULL,family TEXT NOT NULL,
 points REAL NOT NULL,detail_json TEXT NOT NULL,created_at REAL NOT NULL,policy_version TEXT NOT NULL,
 PRIMARY KEY(account_id,evidence_key));
CREATE INDEX IF NOT EXISTS idx_evidence_author ON bot_evidence(account_id,family);
CREATE TABLE IF NOT EXISTS moderation_events(
 event_id TEXT PRIMARY KEY,subject_kind TEXT NOT NULL,subject_id TEXT NOT NULL,
 outcome TEXT NOT NULL,ruled_at REAL NOT NULL,source TEXT NOT NULL,raw_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account_reports(
 target_author TEXT PRIMARY KEY,report_id TEXT NOT NULL DEFAULT '',outcome TEXT NOT NULL,
 submitted_at REAL NOT NULL,ruled_at REAL NOT NULL DEFAULT 0,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS simulation_log(
 id INTEGER PRIMARY KEY,created_at REAL NOT NULL,kind TEXT NOT NULL,account_id TEXT NOT NULL,
 target_id TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS confirmed_account_activity(
 account_id TEXT PRIMARY KEY,action_count INTEGER NOT NULL,observed_at REAL NOT NULL,source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_bot_profiles(account_id TEXT PRIMARY KEY,snapshot_json TEXT NOT NULL);
"""


class RuntimeStoreMixin:
    def migrate_runtime(self):
        old_version=self._conn.execute('PRAGMA user_version').fetchone()[0]
        columns = {
            'account_states': {'mode': "TEXT NOT NULL DEFAULT 'active'",
                'last_original_at': 'REAL NOT NULL DEFAULT 0', 'last_reply_at': 'REAL NOT NULL DEFAULT 0',
                'original_locked_until': 'REAL NOT NULL DEFAULT 0', 'capability_tier': "TEXT NOT NULL DEFAULT 'standard'"},
            'observed_posts': {'version': 'INTEGER NOT NULL DEFAULT 1','root_post_id': "TEXT NOT NULL DEFAULT ''"},
            'interaction_log': {'text': "TEXT NOT NULL DEFAULT ''"},
            'bot_registry': {'verified': 'INTEGER NOT NULL DEFAULT 0',
                'true_team_id': "TEXT NOT NULL DEFAULT ''", 'evidence_groups': 'INTEGER NOT NULL DEFAULT 0'},
            'report_log': {'report_id': "TEXT NOT NULL DEFAULT ''", 'ruled_at': 'REAL NOT NULL DEFAULT 0',
                'ruling_day': "TEXT NOT NULL DEFAULT ''"},
        }
        for table, definitions in columns.items():
            existing = {r['name'] for r in self._conn.execute(f'PRAGMA table_info({table})')}
            for col, ddl in definitions.items():
                if col not in existing:
                    self._conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}')
        for statement in RUNTIME_SCHEMA.split(';'):
            if statement.strip():
                self._conn.execute(statement)
        existing={r['name'] for r in self._conn.execute('PRAGMA table_info(action_intents)')}
        for col,ddl in (('wire_at','REAL'),('wire_tracked','INTEGER NOT NULL DEFAULT 0')):
            if col not in existing:
                self._conn.execute(f'ALTER TABLE action_intents ADD COLUMN {col} {ddl}')
                if col=='wire_at':
                    # Sends started by an older version may already be on the wire.
                    self._conn.execute("UPDATE action_intents SET wire_at=updated_at WHERE status IN ('sending','unknown','succeeded')")
        self._conn.execute('INSERT OR IGNORE INTO comment_queue(post_id,priority,next_fetch_at) '
            "SELECT post_id,MAX(0,replies-replies_crawled),comments_crawled_at FROM observed_posts "
            "WHERE replies>replies_crawled")
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_observed_root ON observed_posts(root_post_id,created_at)')
        if old_version<3:
            self._conn.execute("UPDATE observed_posts SET root_post_id=COALESCE(CAST(json_extract(raw_json,'$.root_post_id') AS TEXT),parent_id),"
                "parent_id=COALESCE(CAST(json_extract(raw_json,'$.reply_to_post_id') AS TEXT),parent_id)")
            # Old scores had different weights and rejected reports used to certify
            # NPC identity. Preserve the original data, but do not carry that inference forward.
            for row in self._conn.execute('SELECT * FROM bot_registry').fetchall():
                self._conn.execute('INSERT OR IGNORE INTO legacy_bot_profiles VALUES(?,?)',
                    (row['account_id'],json.dumps(dict(row))))
            self._conn.execute("UPDATE bot_registry SET status='unknown',suspicion_score=0,evidence_json='[]' "
                "WHERE status IN ('confirmed_bot','suspected_bot') OR (status='verified_npc' AND account_id IN "
                "(SELECT target_author FROM report_log WHERE outcome='rejected'))")
            # Legacy logs cannot distinguish a dry run from an actual send. Quarantine
            # those actions until positive history evidence (or an operator review) resolves them.
            for row in self._conn.execute("SELECT * FROM interaction_log WHERE action IN ('post','reply','like','repost','report')").fetchall():
                text=row['text'] or row['normalized_text']
                key=self.action_key(row['action'],row['account_id'],row['target_post_id'],text)
                ident=uuid.uuid4().hex
                meta={'engine':row['engine'],'target_author':row['target_author'],
                    'target_stance':row['target_stance'],'purpose':row['purpose'],'accounting_already_applied':True}
                self._conn.execute("INSERT OR IGNORE INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,lease_until,created_at,updated_at,detail,metadata_json) "
                    "VALUES(?,?,?,?,?,?,'unknown','legacy',0,?,?,?,?)",
                    (ident,key,row['action'],row['account_id'],row['target_post_id'],text,row['created_at'],row['created_at'],
                     'legacy result requires reconciliation',json.dumps(meta)))
                if row['action'] in ('post','reply'):
                    actual=self._conn.execute('SELECT id FROM action_intents WHERE action_key=?',(key,)).fetchone()[0]
                    now_ts=time.time()
                    self._conn.execute('INSERT OR IGNORE INTO unknown_checks(action_id,unknown_at,next_check_at) VALUES(?,?,?)',(actual,now_ts,now_ts))
                    self._conn.execute('INSERT OR IGNORE INTO account_leases VALUES(?,?,?)',(row['account_id'],actual,now_ts+SEND_RECOVERY['unknown_account_hold_seconds']))
            ts=time.time()
            self._conn.execute("INSERT OR IGNORE INTO post_tasks(post_id,version,status,next_attempt_at,expires_at,reason) "
                "SELECT p.post_id,p.version,CASE WHEN EXISTS(SELECT 1 FROM action_intents a WHERE a.kind='reply' "
                "AND a.target_id=p.post_id AND a.status='unknown') THEN 'unknown' "
                "WHEN p.first_seen_at+1800<=? THEN 'expired' ELSE 'queued' END,MAX(p.first_seen_at,?),"
                "p.first_seen_at+1800,'legacy_observation_import' FROM observed_posts p",(ts,ts))
        self._conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def policy(self, key, default=None):
        with self._lock:
            r = self._conn.execute('SELECT value_json FROM runtime_policy WHERE key=?', (key,)).fetchone()
            return json.loads(r[0]) if r else default

    def set_policy(self, key, value):
        with self.batch_transaction():
            self._conn.execute('INSERT INTO runtime_policy VALUES(?,?) ON CONFLICT(key) '
                'DO UPDATE SET value_json=excluded.value_json', (key, json.dumps(value, allow_nan=False)))

    def seed_campaign_policy(self, personas):
        """Once-only defaults from the existing harness; operator changes survive restart."""
        with self.batch_transaction():
            if self.policy('campaign_seeded', False):
                return
            for p in personas:
                if not p.account_id: continue
                exists=self._conn.execute('SELECT 1 FROM account_states WHERE account_id=?',(p.account_id,)).fetchone()
                if not exists:
                    self.get_account_state(p.account_id)
                    self._conn.execute("UPDATE account_states SET mode='standby' WHERE account_id=?",(p.account_id,))
            for key,value in (('original_lock_seconds',86400),('daily_original_limit',10),('phase','startup')):
                if self.policy(key) is None:self.set_policy(key,value)
            if self.policy('evidence_policy') is None: self.set_policy('evidence_policy',DEFAULT_EVIDENCE_POLICY)
            self.set_policy('campaign_seeded',True)

    def bind_campaign_side(self, side):
        if side not in ('yes','no'): raise ValueError('campaign side is unassigned')
        with self.batch_transaction():
            previous=self.policy('assigned_side')
            if previous and previous!=side: raise ValueError('assigned side conflicts with shared database')
            self.set_policy('assigned_side',side)

    def bind_data_source(self, source):
        if source not in ('mock','real'): raise ValueError('invalid dataset source')
        with self.batch_transaction():
            previous=self.policy('data_source')
            if previous and previous!=source:
                raise ValueError('mock and real observations require separate databases; select --state-file')
            self.set_policy('data_source',source)

    def set_account_policy(self, account, *, mode=None, capability_tier=None, original_locked_until=None):
        with self.batch_transaction():
            self.get_account_state(account)
            for name, value, allowed in (
                ('mode',mode,{'active','standby','dormant'}),
                ('capability_tier',capability_tier,{'standard','high'}),
                ('original_locked_until',original_locked_until,None)):
                if value is not None:
                    if allowed is not None and value not in allowed:
                        raise ValueError(f'invalid {name}')
                    if allowed is None and (not isinstance(value,(int,float)) or value<0):
                        raise ValueError('invalid lock timestamp')
                    self._conn.execute(f'UPDATE account_states SET {name}=? WHERE account_id=?',(value,account))

    def mark_account_used(self, account, *, now=None, kind='reply', target_stance=''):
        ts = time.time() if now is None else float(now)
        with self.batch_transaction():
            self.get_account_state(account)
            self._conn.execute('UPDATE account_states SET last_posted_at=MAX(last_posted_at,?), '
                'post_count=post_count+1, allied_reply_count=allied_reply_count+?, '
                'opposing_reply_count=opposing_reply_count+?,last_original_at=CASE WHEN ? THEN ? ELSE last_original_at END,'
                'last_reply_at=CASE WHEN ? THEN ? ELSE last_reply_at END,'
                'original_locked_until=CASE WHEN ? THEN ? ELSE original_locked_until END WHERE account_id=?',
                (ts,int(kind=='reply' and target_stance=='aligned'),int(kind=='reply' and target_stance=='opposing'),
                 kind=='post',ts,kind=='reply',ts,kind=='post',
                 ts+original_gap(),account))

    def action_key(self, kind, account, target, text='', metadata=None):
        from .sqlite_store import normalize_text,compute_post_norm_hash
        subject = (compute_post_norm_hash(text) or normalize_text(text)) if kind=='post' else str(target)
        owner = account if kind in ('like','repost','follow','unfollow','delete_account') else 'team'
        if kind in ('report','report_account') and (metadata or {}).get('report_attempt_id'):
            subject=[str(target),metadata['report_attempt_id']]
        if kind=='post' and (metadata or {}).get('purpose') in ('warmup','initialise'):
            subject='initialise';owner=account
        if kind=='reply' and (metadata or {}).get('reply_batch_id'):
            subject=[str(target),metadata['reply_batch_id'],metadata['reply_slot']]
        return hashlib.sha256(json.dumps([self.policy('campaign_id','ctn2026'),kind,owner,subject]).encode()).hexdigest()

    def claim_action(self, kind, account, target='', text='', *, metadata=None, now=None):
        from .sqlite_store import current_sydney_day
        ts=time.time() if now is None else float(now)
        meta=metadata or {}
        with self.batch_transaction():
            self.recover_actions(now=ts)
            if meta.get('macro_job_id'):
                job=self._conn.execute('SELECT status,owner,lease_until FROM macro_jobs WHERE id=?',(meta['macro_job_id'],)).fetchone()
                if not job or job['status']!='claimed' or job['owner']!=meta.get('macro_owner') or job['lease_until']<=ts:return None,'macro_lease_lost'
            ready,reason=self.catalog_write_ready(account,meta)
            if not ready:return None,reason
            if kind in ('post','reply'):
                ready,reason=self.persona_action_ready(account,meta)
                if not ready:return None,reason
            if kind=='reply':
                ready,reason=self.reply_slot_ready(account,target,meta)
                if not ready:return None,reason
            if kind=='post' and meta.get('purpose') in ('warmup','initialise'):
                lifecycle=self.lifecycle(account)
                if lifecycle and lifecycle['initialise_status'] in ('ready','waived'):return None,'already_initialised'
            key=self.action_key(kind,account,target,text,meta)
            old=self._conn.execute('SELECT * FROM action_intents WHERE action_key=?',(key,)).fetchone()
            if old and old['status']!='failed':
                return None, 'action_'+old['status']
            if kind in ('post','reply'):
                ready,reason=self.publication_gate(text,meta)
                if not ready:return None,reason
                if self.policy('require_warmup',False) and meta.get('purpose') not in ('warmup','initialise'):
                    if not self.account_initialise_ready(account,now=ts):return None,'account_needs_initialise'
            st=self.get_account_state(account)
            if st.is_disabled() and kind!='delete_account':
                return None,'account_disabled'
            if kind in ('post','reply'):
                if st.is_in_cooldown(ts,float(meta.get('cooldown_s',60))):
                    return None,'account_cooldown'
                leased=self._conn.execute('SELECT 1 FROM account_leases WHERE account_id=? AND lease_until>?',
                    (account,ts)).fetchone()
                if leased:
                    return None,'account_claimed'
                occupied=self._conn.execute("SELECT batch_id,slot FROM reply_slots WHERE account_id=? AND status IN ('claimed','sending','unknown') AND lease_until>?",(account,ts)).fetchall()
                if any(r['batch_id']!=meta.get('reply_batch_id') or r['slot']!=meta.get('reply_slot') for r in occupied):
                    return None,'account_occupied_by_reply_slot'
            if kind in ('post','reply') and meta.get('purpose') not in ('warmup','initialise'):
                # Team-wide anti-pile-on caps per target author and thread, plus per-account daily volume.
                from ctn.pipeline.targeting import cap_reason
                capped=cap_reason(self,target_author=meta.get('target_author','') if kind=='reply' else '',
                    root_post_id=str(meta.get('root_post_id') or target) if kind=='reply' else '',account=account,now=ts)
                if capped:return None,capped
            if kind=='like':
                # A new account liking every few seconds before its first post reads as a bot,
                # and likes count towards the 100 actions that make an account suspendable.
                from ctn.pipeline.targeting import launch_rule
                rule=launch_rule(self,'like_pacing')
                if rule['require_initialised'] and self.policy('require_warmup',False) and not self.account_initialise_ready(account,now=ts):
                    return None,'like_needs_initialise'
                stamps=[r[0] for r in self._conn.execute("SELECT created_at FROM action_intents WHERE kind='like' AND account_id=? "
                    "AND status IN ('claimed','sending','unknown','succeeded') AND created_at>=?",(account,ts-86400))]
                if sum(t>=ts-3600 for t in stamps)>=int(rule['per_hour']):return None,'like_hourly_cap'
                day=current_sydney_day(ts)
                if sum(current_sydney_day(t)==day for t in stamps)>=int(rule['per_day']):return None,'like_daily_cap'
            if kind=='reply' and meta.get('target_stance')=='aligned':
                sent=self.count_recent_allied_replies(now=ts)
                reserved=self._conn.execute("SELECT COUNT(*) FROM action_intents WHERE kind='reply' "
                    "AND status IN ('claimed','sending','unknown') AND json_extract(metadata_json,'$.target_stance')='aligned'").fetchone()[0]
                if sent+reserved>=int(meta.get('allied_hourly_cap',3)):
                    return None,'allied_hourly_cap'
            if kind=='post' and meta.get('purpose') not in ('warmup','initialise'):
                if st.original_locked_until>ts:
                    return None,'original_locked'
                day=current_sydney_day(ts)
                # Reservations and uncertain sends consume quota until reconciled.
                rows=self._conn.execute("SELECT created_at,metadata_json FROM action_intents WHERE kind='post' "
                    "AND status IN ('claimed','sending','unknown','succeeded')").fetchall()
                warmup=meta.get('purpose')=='warmup'
                limit=self.policy('daily_warmup_limit',15) if warmup else self.policy('daily_original_limit',10)
                if sum(json.loads(r['metadata_json']).get('execution_day',current_sydney_day(r['created_at']))==day and (json.loads(r['metadata_json']).get('purpose') in ('warmup','initialise'))==warmup for r in rows)>=int(limit):
                    return None,'daily_original_limit'
            if kind in ('report','report_account'):
                if self.is_reporting_suspended(now=ts)[0]:
                    return None,'reporting_suspended'
                author=str(meta.get('target_author') or target)
                prof=self.get_bot_profile(author)
                if prof['status'] in ('own_bot','verified_npc') or prof.get('verified'):
                    return None,'protected_account'
                if not self.report_evidence_ready(prof) or not meta.get('detail'):
                    return None,'insufficient_report_evidence'
                pending=self._conn.execute("SELECT target_author FROM report_log WHERE outcome='pending' UNION ALL "
                    "SELECT target_author FROM account_reports WHERE outcome='pending'").fetchall()
                intents=self._conn.execute("SELECT metadata_json FROM action_intents WHERE kind IN ('report','report_account') "
                    "AND (status IN ('claimed','sending') OR (status='unknown' AND updated_at>?))",(ts-SEND_RECOVERY['report_unknown_release_seconds'],)).fetchall()
                authors=[r[0] for r in pending]+[json.loads(r[0]).get('target_author') for r in intents]
                if len(authors)>=int(self.policy('max_pending_reports',40)):
                    return None,'pending_team_limit'
                if authors.count(author)>=int(self.policy('max_pending_per_author',2)):
                    return None,'pending_author_limit'
                if kind=='report':
                    # Every unresolved post report can still come back genuine and stop reporting for six hours.
                    from ctn.moderation.decision import policy as decision_policy,expected_genuine
                    rules=decision_policy(self)
                    if expected_genuine(self,rules,extra_p=meta.get('decision_p',rules['p_floor']),now=ts)>float(rules['max_expected_genuine'])+1e-9:
                        return None,'genuine_risk_budget'
            if kind in ('report','report_account'):
                if kind=='report' and self._conn.execute("SELECT 1 FROM action_intents WHERE kind='report' AND target_id=? AND status!='failed' LIMIT 1",(target,)).fetchone():return None,'report_target_already_claimed'
                ready,reason=self.report_authorization(kind,target,meta,now=ts)
                if not ready:return None,reason
            ident=old['id'] if old else uuid.uuid4().hex
            self._conn.execute('INSERT INTO action_intents(id,action_key,kind,account_id,target_id,text,status,owner,'
                'lease_until,created_at,updated_at,metadata_json) VALUES(?,?,?,?,?,?,\'claimed\',?,?,?,?,?) '
                'ON CONFLICT(action_key) DO UPDATE SET status=\'claimed\',owner=excluded.owner,'
                'lease_until=excluded.lease_until,updated_at=excluded.updated_at,detail=\'\','
                'account_id=excluded.account_id,text=excluded.text,metadata_json=excluded.metadata_json,response_json=\'{}\','
                'wire_at=NULL,wire_tracked=0',
                (ident,key,kind,account,target,text,uuid.uuid4().hex,ts+SEND_RECOVERY['send_lease_seconds'],ts,ts,json.dumps(meta)))
            if kind in ('post','reply'):
                from .sqlite_store import normalize_text
                normalized=normalize_text(text)
                self._conn.execute('INSERT INTO publication_texts VALUES(?,?,?) ON CONFLICT(action_id) DO UPDATE SET text_hash=excluded.text_hash,normalized_text=excluded.normalized_text',(ident,hashlib.sha256(normalized.encode()).hexdigest(),normalized))
            if kind in ('report','report_account'):
                attempt=meta.get('report_attempt_id') or ident
                self._conn.execute("INSERT INTO report_attempts VALUES(?,?,?,?,?,?,'claimed',?,0,'',?) ON CONFLICT(action_id) DO UPDATE SET status='claimed',payload_json=excluded.payload_json",
                    (attempt,ident,'account' if kind=='report_account' else 'post',target,meta.get('target_author',target),'',ts,json.dumps(meta)))
                review=self.report_review(meta.get('candidate_id',''))
                if review:
                    self._conn.execute("UPDATE report_reviews SET status='sending',revision=revision+1,updated_at=? WHERE candidate_id=?",(ts,meta['candidate_id']))
                    self.notify_discord(f"sending:{ident}",{'type':'candidate','candidate_id':meta['candidate_id']})
                if meta.get('approval_id'):
                    self._conn.execute("UPDATE report_approvals SET consumed_by=? WHERE id=? AND consumed_by=''",(ident,meta['approval_id']))
            if kind in ('post','reply'):
                self._conn.execute('INSERT INTO account_leases VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE '
                    'SET action_id=excluded.action_id,lease_until=excluded.lease_until',(account,ident,ts+SEND_RECOVERY['send_lease_seconds']))
            if kind=='reply' and meta.get('reply_batch_id'):
                self._conn.execute('UPDATE reply_slots SET action_id=? WHERE batch_id=? AND slot=?',
                    (ident,meta['reply_batch_id'],meta['reply_slot']))
            self.initialise_action_changed(self._conn.execute('SELECT * FROM action_intents WHERE id=?',(ident,)).fetchone(),'claimed',ts)
            return ident,'claimed'

    def recover_actions(self, *, now=None):
        ts=time.time() if now is None else float(now)
        with self.batch_transaction():
            # A dead owner's send either never reached the wire (definitely not sent) or is uncertain.
            self._conn.execute("UPDATE action_intents SET status='failed',detail='owner_lost_before_wire',updated_at=? "
                "WHERE status='sending' AND lease_until<? AND wire_at IS NULL",(ts,ts))
            self._conn.execute("UPDATE action_intents SET status='unknown',detail='owner_lost_during_send',updated_at=? "
                "WHERE status='sending' AND lease_until<?",(ts,ts))
            self._conn.execute("UPDATE action_intents SET status='failed',detail='claim_expired_before_send' "
                "WHERE status='claimed' AND lease_until<?",(ts,))
            self._conn.execute("UPDATE report_attempts SET status=(SELECT status FROM action_intents WHERE id=action_id) WHERE status IN ('claimed','sending') AND action_id IN (SELECT id FROM action_intents WHERE status IN ('unknown','failed'))")
            self._conn.execute("UPDATE account_lifecycle SET initialise_status=CASE (SELECT status FROM action_intents WHERE id=initialise_action) WHEN 'failed' THEN 'pending' WHEN 'unknown' THEN 'unknown' ELSE initialise_status END WHERE initialise_status='sending'")
            self._conn.execute("UPDATE post_tasks SET status=CASE WHEN EXISTS(SELECT 1 FROM action_intents a WHERE a.kind='reply' AND a.target_id=post_tasks.post_id AND a.status='succeeded') THEN 'succeeded' "
                "WHEN expires_at<=? THEN 'expired' ELSE 'queued' END,reason='action_recovered',next_attempt_at=? "
                "WHERE status='unknown' AND NOT EXISTS(SELECT 1 FROM action_intents a WHERE a.kind='reply' AND a.target_id=post_tasks.post_id AND a.status IN ('claimed','sending','unknown')) "
                "AND NOT EXISTS(SELECT 1 FROM reply_batches b WHERE b.target_id=post_tasks.post_id AND b.status IN ('active','paused'))",(ts,ts))
            # An uncertain report stops occupying pending capacity and the genuine-risk budget after a while;
            # its target is still never reported again (claim_action blocks any non-failed report on it).
            self._conn.execute("UPDATE report_attempts SET status='unknown_released' WHERE status='unknown' AND action_id IN "
                "(SELECT id FROM action_intents WHERE status='unknown' AND updated_at<=?)",(ts-SEND_RECOVERY['report_unknown_release_seconds'],))
            self.sync_unknown_holds(ts)

    def sync_unknown_holds(self, ts):
        """Every uncertain post/reply holds its account for a bounded time and is tracked until resolved."""
        self._conn.execute("INSERT OR IGNORE INTO unknown_checks(action_id,unknown_at,next_check_at) "
            "SELECT id,updated_at,updated_at+? FROM action_intents WHERE status='unknown' AND kind IN ('post','reply')",
            (SEND_RECOVERY['check_after_seconds'][0],))
        self._conn.execute("DELETE FROM account_leases WHERE action_id IN (SELECT action_id FROM unknown_checks WHERE state<>'open')")
        self._conn.execute("INSERT INTO account_leases(account_id,action_id,lease_until) SELECT a.account_id,a.id,c.unknown_at+? "
            "FROM unknown_checks c JOIN action_intents a ON a.id=c.action_id WHERE c.state='open' AND a.status='unknown' "
            "AND c.unknown_at+?>? AND NOT EXISTS(SELECT 1 FROM account_leases l WHERE l.account_id=a.account_id AND l.action_id<>a.id AND l.lease_until>?) "
            "ON CONFLICT(account_id) DO UPDATE SET action_id=excluded.action_id,lease_until=excluded.lease_until",
            (SEND_RECOVERY['unknown_account_hold_seconds'],SEND_RECOVERY['unknown_account_hold_seconds'],ts,ts))
        self._conn.execute('DELETE FROM account_leases WHERE lease_until<?',(ts,))
        for row in self._conn.execute("SELECT action_id FROM unknown_checks WHERE state='open' AND unknown_at+?<=?",
                                      (SEND_RECOVERY['abandon_after_seconds'],ts)).fetchall():
            self.abandon_unknown(row[0],ts)

    def abandon_unknown(self, ident, ts):
        """Stop waiting for an unresolved send: free the account, never resend to its target."""
        r=self._conn.execute('SELECT * FROM action_intents WHERE id=?',(ident,)).fetchone()
        self._conn.execute("UPDATE unknown_checks SET state='abandoned',abandoned_at=?,next_check_at=? WHERE action_id=? AND state='open'",
            (ts,ts+SEND_RECOVERY['late_check_every_seconds'],ident))
        self._conn.execute('DELETE FROM account_leases WHERE action_id=?',(ident,))
        if not r or r['status']!='unknown':return
        self._conn.execute("UPDATE action_intents SET detail='abandoned: '||detail WHERE id=? AND detail NOT LIKE 'abandoned:%'",(ident,))
        meta=json.loads(r['metadata_json'])
        if r['kind']=='reply':
            self._conn.execute("UPDATE post_tasks SET status='skipped',reason='unknown_abandoned',lease_until=0 WHERE post_id=? AND status='unknown'",(r['target_id'],))
        if r['kind']=='post' and meta.get('purpose') in ('warmup','initialise'):
            # A second initialise post is harmless; an account that never initialises is not.
            self._conn.execute("UPDATE account_lifecycle SET initialise_status='pending',next_attempt_at=? WHERE account_id=? AND initialise_status='unknown'",(ts,r['account_id']))
        if meta.get('macro_job_id'):
            self._conn.execute("UPDATE macro_jobs SET status='failed',reason='unknown_abandoned' WHERE id=? AND status='unknown'",(meta['macro_job_id'],))

    def due_unknown_checks(self, *, now=None, limit=4):
        ts=time.time() if now is None else float(now)
        with self.batch_transaction():
            self.sync_unknown_holds(ts)
            self._conn.execute("UPDATE unknown_checks SET state='closed' WHERE state='abandoned' AND unknown_at+?<=?",
                (SEND_RECOVERY['late_check_until_seconds'],ts))
            rows=self._conn.execute("SELECT c.*,a.kind,a.account_id,a.target_id,a.text,a.created_at,a.wire_at,a.metadata_json FROM unknown_checks c "
                "JOIN action_intents a ON a.id=c.action_id WHERE c.state IN ('open','abandoned') AND a.status='unknown' AND c.next_check_at<=? "
                "ORDER BY c.state='abandoned',c.next_check_at LIMIT ?",(ts,limit)).fetchall()
            return [dict(r) for r in rows]

    def record_unknown_check(self, ident, outcome, *, complete, detail='', now=None):
        """Store one reconciliation attempt; `outcome` is 'found', 'absent' or 'incomplete'."""
        ts=time.time() if now is None else float(now)
        with self.batch_transaction():
            c=self._conn.execute('SELECT * FROM unknown_checks WHERE action_id=?',(ident,)).fetchone()
            if not c:return 'missing'
            absent=c['absent_checks']+(1 if outcome=='absent' and complete else 0)
            age=ts-c['unknown_at']
            if c['state']=='abandoned':
                nxt=ts+SEND_RECOVERY['late_check_every_seconds']
            else:
                later=[c['unknown_at']+d for d in SEND_RECOVERY['check_after_seconds'] if c['unknown_at']+d>ts+1]
                nxt=later[0] if later else c['unknown_at']+SEND_RECOVERY['abandon_after_seconds']
            self._conn.execute('UPDATE unknown_checks SET checks=checks+1,absent_checks=?,next_check_at=?,last_detail=?,last_checked_at=? WHERE action_id=?',
                (absent,nxt,detail[:300],ts,ident))
            if outcome=='absent' and complete and absent>=int(SEND_RECOVERY['absent_required_checks']) and age>=float(SEND_RECOVERY['absent_min_age_seconds']):
                return 'fail'
            return 'wait'

    def start_action(self, ident, *, wire_tracked=False):
        """`wire_tracked` means the HTTP pacer records wire_at; otherwise the send counts as on the wire now."""
        ts=time.time()
        with self.batch_transaction():
            self._conn.execute("UPDATE report_attempts SET status='sending' WHERE action_id=? AND status='claimed'",(ident,))
            return self._conn.execute("UPDATE action_intents SET status='sending',updated_at=?,wire_tracked=?,wire_at=? "
                "WHERE id=? AND status='claimed'",(ts,int(bool(wire_tracked)),None if wire_tracked else ts,ident)).rowcount==1

    def renew_action(self, ident):
        with self.batch_transaction():
            lease=time.time()+SEND_RECOVERY['send_lease_seconds']
            if self._conn.execute("UPDATE action_intents SET lease_until=? WHERE id=? AND status='sending'",(lease,ident)).rowcount:
                self._conn.execute('UPDATE account_leases SET lease_until=? WHERE action_id=?',(lease,ident))

    def guard_wire_action(self, ident, *, now=None):
        """Recheck after all pacing/login waits and immediately before the actual write."""
        from .sqlite_store import current_sydney_day
        ts=time.time() if now is None else float(now)
        with self.batch_transaction():
            row=self._conn.execute('SELECT * FROM action_intents WHERE id=?',(ident,)).fetchone()
            if not row or row['status']!='sending': return False,'action_not_sending'
            meta=json.loads(row['metadata_json'])
            if row['kind'] in ('post','reply'):
                ready,reason=self.publication_gate(row['text'],meta,exclude_action=ident)
                if not ready:return False,reason
            if meta.get('macro_job_id'):
                job=self._conn.execute('SELECT status,owner,lease_until FROM macro_jobs WHERE id=?',(meta['macro_job_id'],)).fetchone()
                if not job or job['status']!='claimed' or job['owner']!=meta.get('macro_owner') or job['lease_until']<=ts:return False,'macro_lease_lost'
            ready,reason=self.catalog_write_ready(row['account_id'],meta)
            if not ready:return False,reason
            if row['kind'] in ('post','reply'):
                ready,reason=self.persona_action_ready(row['account_id'],meta)
                if not ready:return False,reason
            if row['kind'] in ('post','reply') and self.policy('require_warmup',False) and meta.get('purpose') not in ('warmup','initialise'):
                if not self.account_initialise_ready(row['account_id'],now=ts):return False,'account_needs_initialise'
            if row['kind']=='reply':
                ready,reason=self.reply_slot_ready(row['account_id'],row['target_id'],meta,sending=True)
                if not ready:return False,reason
            if ts>min(meta.get('draft_expires_at',1e20),meta.get('opportunity_expires_at',1e20)):
                return False,'draft_or_opportunity_expired'
            state=self.get_account_state(row['account_id'])
            if state.is_disabled() and row['kind']!='delete_account': return False,'account_disabled'
            # required_account_level is a routing preference and drafting hint, not a send gate.
            if row['kind'] in ('reply','like','repost') and meta.get('target_version'):
                post=self.get_observed_post(row['target_id'])
                if not post or post['version']!=meta['target_version']: return False,'target_edited'
            if row['kind']=='post' and meta.get('purpose') not in ('warmup','initialise'):
                if state.original_locked_until>ts: return False,'original_locked'
                day=current_sydney_day(ts)
                others=self._conn.execute("SELECT created_at,metadata_json FROM action_intents WHERE kind='post' "
                    "AND status IN ('claimed','sending','unknown','succeeded') AND id<>?",(ident,)).fetchall()
                warmup=meta.get('purpose')=='warmup'
                limit=self.policy('daily_warmup_limit',15) if warmup else self.policy('daily_original_limit',10)
                if sum(json.loads(r['metadata_json']).get('execution_day',current_sydney_day(r['created_at']))==day and (json.loads(r['metadata_json']).get('purpose') in ('warmup','initialise'))==warmup for r in others)>=int(limit):
                    return False,'daily_original_limit'
                self._conn.execute("UPDATE action_intents SET metadata_json=json_set(metadata_json,'$.execution_day',?) WHERE id=?",(day,ident))
            if row['kind'] in ('report','report_account'):
                if self.is_reporting_suspended(now=ts)[0]: return False,'reporting_suspended'
                prof=self.get_bot_profile(meta.get('target_author',row['target_id']))
                if prof.get('verified') or prof['status'] in ('own_bot','verified_npc'): return False,'protected_account'
                if not self.report_evidence_ready(prof): return False,'evidence_changed'
                ready,reason=self.report_authorization(row['kind'],row['target_id'],{**meta,'_action_id':ident},now=ts,sending=True)
                if not ready:return False,reason
            return True,'ready'

    def finish_action(self, ident, status, *, response=None, detail='', now=None):
        if status not in ('succeeded','failed','unknown'):
            raise ValueError('invalid action terminal state')
        ts=time.time() if now is None else float(now)
        with self.batch_transaction():
            r=self._conn.execute('SELECT * FROM action_intents WHERE id=?',(ident,)).fetchone()
            if not r or r['status'] in ('succeeded','failed'):
                return False
            if status=='unknown' and r['wire_at'] is None and r['status'] in ('claimed','sending'):
                # The request never left this host (shutdown or cancellation during pacing): definitely not sent.
                status,detail='failed',(detail+'; ' if detail else '')+'never reached the wire'
            self._conn.execute('UPDATE action_intents SET status=?,updated_at=?,detail=?,response_json=? WHERE id=?',
                (status,ts,detail,json.dumps(response or {}),ident))
            self._conn.execute("UPDATE report_attempts SET status=?,report_id=?,submitted_at=CASE WHEN ?='succeeded' THEN ? ELSE submitted_at END WHERE action_id=?",
                ('pending' if status=='succeeded' else status,str((response or {}).get('report_id','')),status,ts,ident))
            self.set_counter('business_progress_ms',max(self.get_counter('business_progress_ms'),int(ts*1000)))
            if status != 'unknown':
                self._conn.execute('DELETE FROM account_leases WHERE action_id=?',(ident,))
                self._conn.execute("UPDATE unknown_checks SET state='closed',resolved=? WHERE action_id=?",(status,ident))
            elif r['kind'] in ('post','reply'):
                self._conn.execute('INSERT OR REPLACE INTO unknown_checks(action_id,unknown_at,next_check_at) VALUES(?,?,?)',
                    (ident,ts,ts+SEND_RECOVERY['check_after_seconds'][0]))
                self._conn.execute('INSERT INTO account_leases VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET action_id=excluded.action_id,lease_until=excluded.lease_until',
                    (r['account_id'],ident,ts+SEND_RECOVERY['unknown_account_hold_seconds']))
            if r['kind']=='reply' and status in ('succeeded','failed'):
                self._conn.execute("UPDATE post_tasks SET status=CASE WHEN ?='succeeded' THEN 'succeeded' "
                    "WHEN expires_at<=? THEN 'expired' ELSE 'queued' END,reason='action_reconciled',next_attempt_at=? "
                    "WHERE post_id=? AND status='unknown'",(status,ts,ts,r['target_id']))
            if r['kind'] in ('report','report_account'):
                meta=json.loads(r['metadata_json'])
                candidate=meta.get('candidate_id','')
                state={'succeeded':'pending','unknown':'unknown','failed':'review'}[status]
                self._conn.execute('UPDATE report_reviews SET status=?,revision=revision+1,updated_at=? WHERE candidate_id=?',(state,ts,candidate))
                self.notify_discord(f"report:{ident}:{status}",{'type':'candidate','candidate_id':candidate})
            macro_id=json.loads(r['metadata_json']).get('macro_job_id')
            if macro_id:self._conn.execute("UPDATE macro_jobs SET status=? WHERE id=? AND status='unknown'",('succeeded' if status=='succeeded' else ('failed' if status=='failed' else 'unknown'),macro_id))
            self.initialise_action_changed(r,status,ts)
            if status=='succeeded':
                meta=json.loads(r['metadata_json'])
                if meta.get('accounting_already_applied'):
                    if r['kind']=='post':
                        self._conn.execute('UPDATE account_states SET original_locked_until=MAX(original_locked_until,?),last_original_at=MAX(last_original_at,?) WHERE account_id=?',
                            (ts+original_gap(),ts,r['account_id']))
                    return True
                if r['kind']=='report':
                    self.log_report(r['target_id'], meta.get('target_author',''),
                        target_team=meta.get('true_team_id',''),reporter_account=r['account_id'],
                        reason=meta.get('detail',''),now=ts)
                    self._conn.execute('UPDATE report_log SET report_id=? WHERE post_id=?',
                        (str((response or {}).get('report_id','')),r['target_id']))
                elif r['kind']=='report_account':
                    self._conn.execute("INSERT INTO account_reports VALUES(?,?,'pending',?,0,?) ON CONFLICT(target_author) DO UPDATE SET report_id=excluded.report_id,outcome='pending',submitted_at=excluded.submitted_at,ruled_at=0,reason=excluded.reason",
                        (r['target_id'],str((response or {}).get('report_id','')),ts,meta.get('detail','')))
                if r['kind'] in ('post','reply'):
                    self.mark_account_used(r['account_id'],now=ts,kind='initialise' if r['kind']=='post' and meta.get('purpose') in ('warmup','initialise') else r['kind'],target_stance=meta.get('target_stance',''))
                    if r['kind']=='post' and meta.get('purpose') in ('warmup','initialise'):
                        self._conn.execute('INSERT INTO account_readiness VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET warmed_at=excluded.warmed_at',(r['account_id'],ts))
                self.log_interaction(engine=meta.get('engine','micro'),account_id=r['account_id'],action=r['kind'],
                    target_post_id=str(((response or {}).get('post_id') or r['target_id']) if r['kind']=='post' else r['target_id']),
                    target_author=meta.get('target_author',''),target_stance=meta.get('target_stance',''),
                    purpose=meta.get('purpose',''),text=r['text'],now=ts)
            return True

    def get_action(self, ident):
        with self._lock:
            row=self._conn.execute('SELECT * FROM action_intents WHERE id=?',(ident,)).fetchone()
            return dict(row) if row else None

    def list_actions(self, status=None):
        with self._lock:
            rows=self._conn.execute('SELECT * FROM action_intents'+(' WHERE status=?' if status else '')+
                ' ORDER BY created_at', (status,) if status else ()).fetchall()
            return [dict(r) for r in rows]

    def log_simulation(self, kind, account, target, payload):
        with self.batch_transaction():
            self._conn.execute('INSERT INTO simulation_log(created_at,kind,account_id,target_id,payload_json) '
                'VALUES(?,?,?,?,?)',(time.time(),kind,account,target,json.dumps(payload)))

    def enqueue_post(self, post_id, version=1, *, now=None, ttl=1800, post=None):
        ts=time.time() if now is None else float(now)
        schedule=None
        if post is not None:
            from ctn.pipeline.targeting import initial_schedule
            schedule=initial_schedule(self,post,now=ts)
        with self.batch_transaction():
            # The persistent plan owns this target, including after a restart or edit.
            self._expire_reply_batches(ts, target_id=post_id)
            planned=self._conn.execute("SELECT target_version FROM reply_batches WHERE target_id=? AND (target_version=? OR status IN ('active','paused') OR EXISTS (SELECT 1 FROM action_intents WHERE kind='reply' AND target_id=? AND status IN ('sending','unknown','succeeded'))) ORDER BY created_at DESC LIMIT 1",(post_id,version,post_id)).fetchone()
            self._conn.execute("UPDATE post_tasks SET status='skipped',reason='superseded' WHERE post_id=? "
                "AND version<>? AND status IN ('queued','claimed','waiting_materials')",(post_id,version))
            self._conn.execute('INSERT OR IGNORE INTO post_tasks(post_id,version,next_attempt_at,expires_at) VALUES(?,?,?,?)',
                (post_id,version,ts,ts+ttl))
            self._conn.execute('UPDATE post_tasks SET queued_at=CASE WHEN queued_at=0 THEN ? ELSE queued_at END WHERE post_id=? AND version=?',(ts,post_id,version))
            if schedule is not None and not schedule[0].get('own'):
                info,start=schedule
                self._conn.execute("UPDATE post_tasks SET audience=?,priority=jev_score+?,next_attempt_at=MAX(next_attempt_at,?),"
                    "assessment_json=? WHERE post_id=? AND version=? AND status='queued'",
                    (info['priority'],info['priority'],start,json.dumps({'targeting':info}),post_id,version))
                from ctn.pipeline.targeting import POLICY as TARGETING
                created=float(post.get('created_at') or 0) or ts
                for stage,offset in enumerate(TARGETING['refresh']['stages_seconds']):
                    self._conn.execute('INSERT OR IGNORE INTO post_refresh(post_id,stage,due_at) VALUES(?,?,?)',(post_id,stage,created+offset))
            if planned:
                self._conn.execute("UPDATE post_tasks SET status=?,reason='target_already_planned' WHERE post_id=? AND version=? AND status IN ('queued','claimed')",
                    ('planned' if planned[0]==version else 'skipped',post_id,version))

    def refresh_task_audience(self, post, *, now=None):
        """Re-score an open reply task when fresher counts or trending status arrive."""
        from ctn.pipeline.targeting import audience
        info=audience(self,post,now=now)
        if info.get('own'):return None
        with self.batch_transaction():
            self._conn.execute("UPDATE post_tasks SET audience=?,priority=jev_score+? WHERE post_id=? "
                "AND status IN ('queued','waiting_materials')",(info['priority'],info['priority'],post['post_id']))
        return info

    def request_backlog(self, bucket, *, now=None):
        """Seconds until the shared request schedule for this bucket is free."""
        ts=time.time() if now is None else float(now)
        with self._lock:
            row=self._conn.execute('SELECT next_at,blocked_until FROM request_slots WHERE bucket=?',(bucket,)).fetchone()
        return max(0.,(max(row[0],row[1])-ts) if row else 0.)

    def claim_refreshes(self, limit, *, now=None, stale_after=300):
        ts=time.time() if now is None else float(now)
        open_task="EXISTS(SELECT 1 FROM post_tasks t WHERE t.post_id=post_refresh.post_id AND t.status IN ('queued','claimed','waiting_materials'))"
        with self.batch_transaction():
            self._conn.execute(f"UPDATE post_refresh SET status='dropped' WHERE status='queued' AND due_at<=? AND NOT {open_task}",(ts,))
            self._conn.execute("UPDATE post_refresh SET status='stale' WHERE status='queued' AND due_at<?",(ts-stale_after,))
            rows=self._conn.execute("SELECT r.post_id,r.stage FROM post_refresh r JOIN post_tasks t ON t.post_id=r.post_id "
                "AND t.status IN ('queued','claimed','waiting_materials') WHERE r.status='queued' AND r.due_at<=? "
                "GROUP BY r.post_id,r.stage ORDER BY MAX(t.priority) DESC,r.due_at LIMIT ?",(ts,max(1,int(limit)))).fetchall()
            for r in rows:
                self._conn.execute("UPDATE post_refresh SET status='claimed' WHERE post_id=? AND stage=?",(r['post_id'],r['stage']))
            return [dict(r) for r in rows]

    def finish_refresh(self, row, counters, *, now=None):
        """Apply fresh counts: update the mirror, the comment queue and the task's audience score."""
        ts=time.time() if now is None else float(now)
        status='done' if counters else 'failed'
        with self.batch_transaction():
            self._conn.execute('UPDATE post_refresh SET status=? WHERE post_id=? AND stage=?',(status,row['post_id'],row['stage']))
            if not counters:return None
            cur=self._conn.execute('SELECT * FROM observed_posts WHERE post_id=?',(row['post_id'],)).fetchone()
            if cur is None:return None
            likes=int(counters.get('likes',cur['likes']) or 0);replies=int(counters.get('replies',cur['replies']) or 0)
            reposts=int(counters.get('reposts',cur['reposts']) or 0)
            self._conn.execute('UPDATE observed_posts SET likes=?,replies=?,reposts=?,last_seen_at=MAX(last_seen_at,?) WHERE post_id=?',
                (likes,replies,reposts,ts,row['post_id']))
            if replies>int(cur['replies_crawled']):
                self._conn.execute('INSERT INTO comment_queue(post_id,priority,next_fetch_at) VALUES(?,?,?) '
                    'ON CONFLICT(post_id) DO UPDATE SET priority=excluded.priority',(row['post_id'],replies-int(cur['replies_crawled']),float(cur['comments_crawled_at'])))
            record=self.get_observed_post(row['post_id'])
            return self.refresh_task_audience(record,now=ts)

    def claim_post_tasks(self, limit=8, *, now=None, lease_s=120):
        ts=time.time() if now is None else float(now)
        owner=uuid.uuid4().hex
        with self.batch_transaction():
            self._conn.execute("UPDATE post_tasks SET status='expired',reason='opportunity_expired' "
                "WHERE status IN ('queued','claimed') AND expires_at<=?",(ts,))
            self._conn.execute("UPDATE post_tasks SET status='queued' WHERE status='claimed' AND lease_until<=?",(ts,))
            # Freshness is part of the audience score; waiting longer no longer raises priority.
            rows=self._conn.execute("SELECT * FROM post_tasks WHERE status='queued' AND next_attempt_at<=? "
                'ORDER BY priority DESC,next_attempt_at,post_id LIMIT ?',(ts,max(1,limit))).fetchall()
            for r in rows:
                self._conn.execute("UPDATE post_tasks SET status='claimed',owner=?,lease_until=?,attempts=attempts+1 "
                    'WHERE post_id=? AND version=?',(owner,ts+lease_s,r['post_id'],r['version']))
            return [{**dict(r),'owner':owner} for r in rows]

    def update_post_task(self, task, status, *, reason='', delay=0, draft=None, draft_at=None, now=None):
        if status not in ('queued','planned','succeeded','skipped','expired','unknown','simulated','waiting_materials'):
            raise ValueError('invalid task status')
        ts=time.time() if now is None else float(now)
        with self.batch_transaction():
            changed=self._conn.execute('UPDATE post_tasks SET status=?,reason=?,next_attempt_at=?,lease_until=0,'
                'draft_json=?,draft_at=? WHERE post_id=? AND version=? AND owner=? AND status=\'claimed\'',
                (status,reason,ts+delay,json.dumps(draft) if draft else '',(draft_at or ts) if draft else 0,
                 task['post_id'],task['version'],task['owner'])).rowcount==1
            if changed and status!='queued':
                self.set_counter('business_progress_ms',max(self.get_counter('business_progress_ms'),int(ts*1000)))
            return changed

    def post_task_send_ready(self, task, *, now=None):
        ts=time.time() if now is None else float(now)
        with self._lock:
            r=self._conn.execute('SELECT t.status,t.owner,t.lease_until,t.expires_at,p.version '
                'FROM post_tasks t JOIN observed_posts p USING(post_id) WHERE t.post_id=? AND t.version=?',
                (task['post_id'],task['version'])).fetchone()
            return bool(r and r['status']=='claimed' and r['owner']==task['owner'] and r['version']==task['version']
                and r['lease_until']>ts and r['expires_at']>ts)

    def renew_post_tasks(self, tasks, *, now=None):
        ts=time.time() if now is None else float(now)
        with self.batch_transaction():
            for t in tasks:
                self._conn.execute('UPDATE post_tasks SET lease_until=? WHERE post_id=? AND version=? '
                    "AND owner=? AND status='claimed'",(ts+120,t['post_id'],t['version'],t['owner']))

    def task_counts(self):
        with self._lock:
            return {r[0]:r[1] for r in self._conn.execute('SELECT status,COUNT(*) FROM post_tasks GROUP BY status')}

    def runtime_metrics(self, *, now=None):
        ts=time.time() if now is None else float(now)
        with self._lock:
            oldest=self._conn.execute("SELECT MIN(expires_at-1800) FROM post_tasks WHERE status='queued'").fetchone()[0]
            pending=self._conn.execute("SELECT (SELECT COUNT(*) FROM report_log WHERE outcome='pending')+"
                "(SELECT COUNT(*) FROM account_reports WHERE outcome='pending')").fetchone()[0]
            unknown=self._conn.execute("SELECT COUNT(*) FROM action_intents WHERE status='unknown'").fetchone()[0]
            batches=dict(self._conn.execute('SELECT status,COUNT(*) FROM reply_batches GROUP BY status'))
            slots=dict(self._conn.execute('SELECT status,COUNT(*) FROM reply_slots GROUP BY status'))
            performance=dict(self._conn.execute('SELECT status,COUNT(*) FROM performance_jobs GROUP BY status'))
            return {'tasks':self.task_counts(),'oldest_task_age_s':max(0,ts-oldest) if oldest else 0,
                'reply_batches':batches,'reply_slots':slots,'performance_jobs':performance,
                'pending_reports':pending,'unknown_actions':unknown,
                'reporting_suspended_s':self.is_reporting_suspended(now=ts)[1]}

    def record_confirmed_activity(self, account, count, *, observed_at, source):
        import math
        if (type(count) is not int or count<0 or not isinstance(account,str) or not account.strip() or
            not isinstance(source,str) or not source.strip() or type(observed_at) not in (int,float) or not math.isfinite(observed_at) or observed_at<=0):
            raise ValueError('activity requires explicit count, observation time and source')
        with self.batch_transaction():
            self._conn.execute('INSERT INTO confirmed_account_activity VALUES(?,?,?,?) ON CONFLICT(account_id) '
                'DO UPDATE SET action_count=excluded.action_count,observed_at=excluded.observed_at,source=excluded.source '
                'WHERE excluded.observed_at>confirmed_account_activity.observed_at',(account,count,observed_at,source))

    def confirmed_activity(self, account):
        with self._lock:
            row=self._conn.execute('SELECT * FROM confirmed_account_activity WHERE account_id=?',(account,)).fetchone()
            return dict(row) if row else None

    def reserve_requests(self, buckets, *, kind, cap, now=None):
        return self.reserve_http_request(buckets,kind=kind,cap=cap,now=now)[1]

    def reserve_http_request(self, buckets, *, kind, cap, now=None):
        """Atomic multi-bucket virtual-time reservation, one actual HTTP attempt."""
        ts=time.time() if now is None else float(now)
        with self.batch_transaction():
            used=self.get_counter('http_'+kind)+self._conn.execute("SELECT COUNT(*) FROM request_attempts "
                "WHERE kind=? AND status='reserved' AND expires_at>?",(kind,ts)).fetchone()[0]
            if used>=cap:
                raise RuntimeError(kind+' request budget exhausted')
            due=ts
            for key,rate in buckets:
                if rate<=0:
                    raise ValueError('request rate must be positive')
                r=self._conn.execute('SELECT * FROM request_slots WHERE bucket=?',(key,)).fetchone()
                if r:
                    due=max(due,r['next_at'],r['blocked_until'])
            for key,rate in buckets:
                self._conn.execute('INSERT INTO request_slots(bucket,next_at) VALUES(?,?) ON CONFLICT(bucket) '
                    'DO UPDATE SET next_at=excluded.next_at',(key,due+1/rate))
            self.increment_counter('http_reserved_'+kind)
            ident=uuid.uuid4().hex
            self._conn.execute("INSERT INTO request_attempts VALUES(?,?,?,'reserved',?,?,?)",
                (ident,kind,json.dumps(buckets),due,due+300,ts))
            return ident,due-ts

    def mark_http_sent(self, ident, *, cap):
        return self.try_mark_http_sent(ident,cap=cap)[0]

    def try_mark_http_sent(self, ident, *, cap, now=None, action=None):
        """Mark a reserved request as sent; with `action`, record wire_at in the same transaction."""
        ts=time.time() if now is None else float(now)
        with self.batch_transaction():
            row=self._conn.execute('SELECT kind,status,buckets_json FROM request_attempts WHERE id=?',(ident,)).fetchone()
            if not row or row['status']!='reserved': return False,None
            if action and self._conn.execute("SELECT status FROM action_intents WHERE id=?",(action,)).fetchone()[0]!='sending':
                return False,'action_no_longer_sending' 
            if self.get_counter('http_'+row['kind'])>=cap: return False,None
            buckets=json.loads(row['buckets_json'])
            due=ts
            for key,rate in buckets:
                slot=self._conn.execute('SELECT next_at FROM wire_slots WHERE bucket=?',(key,)).fetchone()
                if slot: due=max(due,slot[0])
                due=max(due,self.request_blocked_until(key))
            if due>ts+1e-8: return False,due-ts
            for key,rate in buckets:
                self._conn.execute('INSERT INTO wire_slots VALUES(?,?) ON CONFLICT(bucket) DO UPDATE SET next_at=excluded.next_at',
                    (key,ts+1/rate))
            self._conn.execute("UPDATE request_attempts SET status='sent' WHERE id=?",(ident,))
            if action:
                self._conn.execute('UPDATE action_intents SET wire_at=COALESCE(wire_at,?) WHERE id=?',(ts,action))
            self.increment_counter('http_'+row['kind'])
            return True,0.

    def cancel_http_reservation(self, ident):
        with self.batch_transaction():
            self._conn.execute("UPDATE request_attempts SET status='cancelled' WHERE id=? AND status='reserved'",(ident,))

    def reschedule_http_request(self, ident, buckets, *, now=None):
        """A later Retry-After requeues existing waiters instead of waking them together."""
        ts=time.time() if now is None else float(now)
        with self.batch_transaction():
            due=ts
            for key,rate in buckets:
                row=self._conn.execute('SELECT next_at,blocked_until FROM request_slots WHERE bucket=?',(key,)).fetchone()
                if row: due=max(due,row['next_at'],row['blocked_until'])
            for key,rate in buckets:
                self._conn.execute('UPDATE request_slots SET next_at=? WHERE bucket=?',(due+1/rate,key))
            self._conn.execute("UPDATE request_attempts SET due_at=?,expires_at=? WHERE id=? AND status='reserved'",
                (due,due+300,ident))
            return due-ts

    def block_request_bucket(self, bucket, until):
        with self.batch_transaction():
            self._conn.execute('INSERT INTO request_slots(bucket,blocked_until) VALUES(?,?) ON CONFLICT(bucket) '
                'DO UPDATE SET blocked_until=MAX(blocked_until,excluded.blocked_until)',(bucket,until))

    def request_blocked_until(self, bucket):
        with self._lock:
            r=self._conn.execute('SELECT blocked_until FROM request_slots WHERE bucket=?',(bucket,)).fetchone()
            return float(r[0]) if r else 0.

    def add_weighted_evidence(self, account, family, points, detail, *, evidence_key, now=None):
        policy=self.policy('evidence_policy',DEFAULT_EVIDENCE_POLICY)
        caps=policy['caps']
        if family not in caps:
            raise ValueError('unknown evidence family')
        ts=time.time() if now is None else float(now)
        with self.batch_transaction():
            prof=self.get_bot_profile(account)
            if prof['status'] in ('own_bot','verified_npc') or prof.get('verified'):
                return prof
            self._conn.execute('INSERT OR IGNORE INTO bot_evidence VALUES(?,?,?,?,?,?,?)',
                (account,evidence_key,family,float(points),json.dumps(detail),ts,policy['version']))
            return self._recompute_evidence(account)

    def _recompute_evidence(self,account):
        protected=self.get_bot_profile(account)
        if protected.get('verified') or protected['status'] in ('own_bot','verified_npc'):
            return protected
        policy=self.policy('evidence_policy',DEFAULT_EVIDENCE_POLICY)
        caps=policy['caps']
        with self.batch_transaction():
            rows=self._conn.execute('SELECT family,SUM(points) FROM bot_evidence WHERE account_id=? GROUP BY family',
                (account,)).fetchall()
            grouped={r[0]:max(-caps[r[0]],min(caps[r[0]],r[1])) for r in rows}
            score=max(0,min(100,sum(grouped.values())))
            groups=sum(v>0 for k,v in grouped.items() if k not in ('weak','moderator'))
            status='confirmed_bot' if score>=policy['report_threshold'] and groups>=policy['minimum_substantive_groups'] else ('suspected_bot' if score>=policy['observe_below'] else 'unknown')
            tags=[r[0] for r in self._conn.execute('SELECT evidence_key FROM bot_evidence WHERE account_id=? ORDER BY created_at LIMIT 100',
                (account,))]
            self._conn.execute('INSERT INTO bot_registry(account_id,status,suspicion_score,evidence_groups,evidence_json) '
                'VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET status=excluded.status,'
                'suspicion_score=excluded.suspicion_score,evidence_groups=excluded.evidence_groups,evidence_json=excluded.evidence_json',
                (account,status,score,groups,json.dumps(tags)))
            return self.get_bot_profile(account)

    def report_evidence_ready(self, profile):
        policy=self.policy('evidence_policy',DEFAULT_EVIDENCE_POLICY)
        return (profile['suspicion_score']>=policy['report_threshold'] and
            profile.get('evidence_groups',0)>=policy['minimum_substantive_groups'])

    def invalidate_post_evidence(self,post_id):
        with self.batch_transaction():
            rows=self._conn.execute("SELECT account_id,evidence_key FROM bot_evidence WHERE family<>'moderator' AND "
                "(json_extract(detail_json,'$.post_id')=? OR EXISTS(SELECT 1 FROM json_each(detail_json,'$.related_posts') WHERE value=?))",
                (post_id,post_id)).fetchall()
            for row in rows:
                self._conn.execute('DELETE FROM bot_evidence WHERE account_id=? AND evidence_key=?',tuple(row))
            for account in {r[0] for r in rows}: self._recompute_evidence(account)

    def evidence_for(self, account):
        with self._lock:
            return [dict(r) for r in self._conn.execute('SELECT * FROM bot_evidence WHERE account_id=? ORDER BY created_at',
                (account,))]

    def apply_moderation_event(self, event):
        """Consume an explicit ruling, not a disappearance/404. Idempotent and ordered."""
        from .sqlite_store import current_sydney_day
        required={'event_id','subject_kind','subject_id','outcome','ruled_at','source'}
        if not required<=event.keys() or event['subject_kind'] not in ('post','account') or event['outcome'] not in (
                'upheld','rejected','dismissed') or not event['source'] or float(event['ruled_at'])<=0:
            raise ValueError('invalid explicit moderation event')
        with self.batch_transaction():
            table,col=('report_log','post_id') if event['subject_kind']=='post' else ('account_reports','target_author')
            r=self._conn.execute(f'SELECT * FROM {table} WHERE {col}=?',(str(event['subject_id']),)).fetchone()
            if not r:
                raise ValueError('ruling has no submitted report')
            submitted = float(r['created_at'] if table=='report_log' else r['submitted_at'])
            if float(event['ruled_at']) < submitted:
                raise ValueError('ruling predates report submission')
            prior=self._conn.execute('SELECT 1 FROM moderation_events WHERE subject_kind=? AND subject_id=?',
                (event['subject_kind'],str(event['subject_id']))).fetchone()
            if prior:
                return False
            cur=self._conn.execute('INSERT OR IGNORE INTO moderation_events VALUES(?,?,?,?,?,?,?)',
                (event['event_id'],event['subject_kind'],str(event['subject_id']),event['outcome'],
                 float(event['ruled_at']),event['source'],json.dumps(event)))
            if not cur.rowcount:
                return False
            author=r['target_author']
            ruled=float(event['ruled_at'])
            if table=='report_log':
                self._conn.execute('UPDATE report_log SET outcome=?,ruled_at=?,ruling_day=? WHERE post_id=?',
                    (event['outcome'],ruled,current_sydney_day(ruled),str(event['subject_id'])))
                if event['outcome']=='upheld':
                    self._conn.execute('UPDATE bot_registry SET upheld_strikes=upheld_strikes+1,'
                        'is_suspended=CASE WHEN upheld_strikes+1>=3 THEN 1 ELSE is_suspended END WHERE account_id=?',(author,))
            else:
                self._conn.execute('UPDATE account_reports SET outcome=?,ruled_at=? WHERE target_author=?',
                    (event['outcome'],ruled,author))
                if event['outcome']=='upheld':
                    self._conn.execute('UPDATE bot_registry SET is_suspended=1 WHERE account_id=?',(author,))
            if event['outcome']=='rejected':
                until=int((ruled+21600)*1000)
                self.set_counter('report_suspended_until_ms',max(self.get_counter('report_suspended_until_ms'),until))
            if event['outcome'] in ('upheld','rejected'):
                self.add_weighted_evidence(author,'moderator',5 if event['outcome']=='upheld' else -5,
                    {'event_id':event['event_id'],'outcome':event['outcome']},evidence_key='ruling:'+event['event_id'],now=ruled)
            return True
