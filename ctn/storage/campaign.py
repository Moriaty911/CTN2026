"""Catalog, account bindings and durable reply slots. All writes are short transactions."""
from dataclasses import asdict
import json
import time
import uuid

from .performance import PerformanceStoreMixin, PERFORMANCE_SCHEMA

CAMPAIGN_SCHEMA = '''
CREATE TABLE IF NOT EXISTS persona_catalogs(hash TEXT PRIMARY KEY,version TEXT NOT NULL,body_json TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS persona_revisions(persona_id TEXT NOT NULL,version INTEGER NOT NULL,profile_hash TEXT NOT NULL,occupation_group TEXT NOT NULL,body_json TEXT NOT NULL,PRIMARY KEY(persona_id,version));
CREATE TABLE IF NOT EXISTS persona_bindings(persona_id TEXT PRIMARY KEY,account_id TEXT NOT NULL UNIQUE,source TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reply_batches(id TEXT PRIMARY KEY,target_id TEXT NOT NULL UNIQUE,target_version INTEGER NOT NULL,catalog_hash TEXT NOT NULL,plan_json TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'active',reason TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_batches_state ON reply_batches(status,expires_at);
CREATE TABLE IF NOT EXISTS reply_slots(batch_id TEXT NOT NULL,slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 3),persona_id TEXT NOT NULL,persona_version INTEGER NOT NULL,account_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',owner TEXT NOT NULL DEFAULT '',lease_until REAL NOT NULL DEFAULT 0,next_attempt_at REAL NOT NULL DEFAULT 0,decision_json TEXT NOT NULL DEFAULT '',draft_at REAL NOT NULL DEFAULT 0,action_id TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',PRIMARY KEY(batch_id,slot),UNIQUE(batch_id,account_id));
CREATE INDEX IF NOT EXISTS idx_slots_ready ON reply_slots(status,next_attempt_at,lease_until);
'''


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


class CampaignStoreMixin(PerformanceStoreMixin):
    def migrate_campaign(self):
        for statement in (CAMPAIGN_SCHEMA+PERFORMANCE_SCHEMA).split(';'):
            if statement.strip(): self._conn.execute(statement)

    def register_persona_catalog(self, catalog, *, activate=False):
        from ctn.persona.catalog import digest
        fingerprint=digest(catalog)
        with self.batch_transaction():
            self._conn.execute('INSERT OR IGNORE INTO persona_catalogs VALUES(?,?,?,?)',
                (fingerprint,catalog['catalog_version'],_json(catalog),time.time()))
            for profile in catalog['personas']:
                previous=self._conn.execute('SELECT profile_hash FROM persona_revisions WHERE persona_id=? AND version=?',
                    (profile['persona_id'],profile['version'])).fetchone()
                if previous and previous[0] != digest(profile):
                    raise ValueError('changed persona content requires a new profile version')
                self._conn.execute('INSERT OR IGNORE INTO persona_revisions VALUES(?,?,?,?,?)',
                    (profile['persona_id'],profile['version'],digest(profile),profile['occupation_group'],_json(profile)))
            if activate or not self.policy('active_persona_catalog'):
                self.set_policy('active_persona_catalog',fingerprint)
        return fingerprint

    def persona_bindings(self):
        with self._lock:
            return dict(self._conn.execute('SELECT persona_id,account_id FROM persona_bindings'))

    def bind_persona(self, persona_id, account_id, *, source='operator'):
        if (type(account_id) is not str or not account_id.strip() or account_id!=account_id.strip()
            or any(c.isspace() for c in account_id) or len(account_id)>128 or not source):
            raise ValueError('account must be a nonblank handle or ID, with a binding source')
        with self.batch_transaction():
            catalog=self._conn.execute('SELECT body_json FROM persona_catalogs WHERE hash=?',(self.policy('active_persona_catalog'),)).fetchone()
            profiles=json.loads(catalog[0])['personas'] if catalog else []
            if not any(p['persona_id']==persona_id for p in profiles): raise ValueError('persona not in active catalog')
            existing=self._conn.execute('SELECT * FROM persona_bindings WHERE persona_id=? OR account_id=?',(persona_id,account_id)).fetchall()
            if existing:
                if len(existing)==1 and existing[0]['persona_id']==persona_id and existing[0]['account_id']==account_id: return
                raise ValueError('persona and account bindings are immutable')
            used=self._conn.execute("SELECT 1 FROM action_intents WHERE account_id=? AND kind IN ('post','reply') AND status!='failed' LIMIT 1",(account_id,)).fetchone()
            history=self._conn.execute("SELECT 1 FROM interaction_log WHERE account_id=? AND action IN ('post','reply') LIMIT 1",(account_id,)).fetchone()
            if used or history: raise ValueError('cannot assign a new identity to an account with publication history')
            self._conn.execute('INSERT INTO persona_bindings VALUES(?,?,?,?)',(persona_id,account_id,source,time.time()))
            self.get_account_state(account_id)
            self._conn.execute("UPDATE account_states SET mode='standby' WHERE account_id=?",(account_id,))
            self.register_verified_accounts([account_id],status='own_bot')
        return account_id

    def set_persona_modes(self, persona_ids, mode, *, max_active=50):
        if mode not in ('active','standby','dormant'): raise ValueError('invalid mode')
        with self.batch_transaction():
            catalog=self._conn.execute('SELECT body_json FROM persona_catalogs WHERE hash=?',(self.policy('active_persona_catalog'),)).fetchone()
            profiles={p['persona_id']:p for p in json.loads(catalog[0])['personas']} if catalog else {}
            bindings=self.persona_bindings()
            if not persona_ids or len(set(persona_ids))!=len(persona_ids): raise ValueError('select unique personas')
            if any(p not in profiles or p not in bindings for p in persona_ids): raise ValueError('all selected personas must be in the active catalog and bound')
            if mode=='active':
                if self.policy('phase','warmup')=='warmup' and any(profiles[p]['occupation_group'] in ('medical_public_health','law_policy') for p in persona_ids):
                    raise ValueError('professional personas remain on standby during warmup')
                current={r[0] for r in self._conn.execute("SELECT b.persona_id FROM persona_bindings b JOIN account_states a ON a.account_id=b.account_id WHERE a.mode='active'")}
                if len(current | set(persona_ids)) > max_active: raise ValueError('active persona limit exceeded')
                if any(self.get_account_state(bindings[p]).is_frozen or self.get_account_state(bindings[p]).is_banned for p in persona_ids):
                    raise ValueError('cannot activate frozen or banned accounts')
            for p in persona_ids: self.set_account_policy(bindings[p],mode=mode)
        return len(persona_ids)

    def account_states_for(self, accounts):
        accounts=tuple(set(accounts)-{''})
        if not accounts: return {}
        from ctn.persona.models import AccountState
        with self._lock:
            rows=self._conn.execute('SELECT * FROM account_states WHERE account_id IN ('+','.join('?' for _ in accounts)+')',accounts).fetchall()
        keys=set(AccountState.__dataclass_fields__)
        return {r['account_id']:AccountState(**{k:r[k] for k in keys if k in r.keys()}) for r in rows}

    def persona_action_ready(self, account, meta):
        active=self.policy('active_persona_catalog')
        if not active: return True,'historical_or_uncatalogued_action'
        if meta.get('catalog_hash')!=active: return False,'persona_catalog_mismatch'
        binding=self._conn.execute('SELECT persona_id FROM persona_bindings WHERE account_id=?',(account,)).fetchone()
        if not binding or binding[0]!=meta.get('persona_id'): return False,'persona_unbound'
        profile=self._conn.execute('SELECT body_json FROM persona_revisions WHERE persona_id=? AND version=?',
            (binding[0],meta.get('persona_version'))).fetchone()
        if not profile: return False,'persona_version_missing'
        catalog=json.loads(self._conn.execute('SELECT body_json FROM persona_catalogs WHERE hash=?',(active,)).fetchone()[0])
        if not any(p['persona_id']==binding[0] and p['version']==meta.get('persona_version') for p in catalog['personas']):
            return False,'persona_version_inactive'
        profile=json.loads(profile[0])
        if self.policy('phase','warmup')=='warmup' and profile['occupation_group'] in ('medical_public_health','law_policy'):
            return False,'professional_persona_warmup'
        return True,'ready'

    def catalog_write_ready(self,account,meta):
        active=self.policy('active_persona_catalog')
        if not active:return True,'legacy'
        if meta.get('process_catalog_hash',meta.get('catalog_hash'))!=active:
            return False,'persona_catalog_mismatch'
        row=self._conn.execute("SELECT json_extract(p.value,'$.occupation_group') FROM persona_bindings b "
            "JOIN persona_catalogs c ON c.hash=? JOIN json_each(c.body_json,'$.personas') p "
            "ON json_extract(p.value,'$.persona_id')=b.persona_id WHERE b.account_id=?",(active,account)).fetchone()
        if not row:return False,'persona_unbound'
        if self.policy('phase','warmup')=='warmup' and row[0] in ('medical_public_health','law_policy'):
            return False,'professional_persona_warmup'
        return True,'ready'

    def create_reply_batch(self, target, version, plan, slots, *, expires_at, now=None):
        ts=time.time() if now is None else now
        if not 1<=len(slots)<=4 or len({s['account_id'] for s in slots})!=len(slots):
            raise ValueError('reply batch needs 1-4 distinct accounts')
        with self.batch_transaction():
            if plan.get('catalog_hash')!=self.policy('active_persona_catalog'):
                raise ValueError('persona_catalog_mismatch')
            for slot in slots:
                ready,reason=self.persona_action_ready(slot['account_id'],{
                    'persona_id':slot['persona_id'],'persona_version':slot['persona_version'],
                    'catalog_hash':plan['catalog_hash']})
                if not ready:raise ValueError(reason)
            self._expire_reply_batches(ts)
            previous=self._conn.execute('SELECT id FROM reply_batches WHERE target_id=? AND target_version=?',(target,version)).fetchone()
            if previous:return previous[0],False
            if self._conn.execute("SELECT 1 FROM action_intents WHERE kind='reply' AND target_id=? AND status!='failed' LIMIT 1",(target,)).fetchone():
                return None,False
            if self.has_replied_to_post(target): return None,False
            row=self.get_observed_post(target)
            if not row or row['version']!=version or expires_at<=ts: return None,False
            ident=uuid.uuid4().hex
            self._conn.execute('INSERT INTO reply_batches(id,target_id,target_version,catalog_hash,plan_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?)',
                (ident,target,version,plan.get('catalog_hash',''),_json(plan),ts,expires_at))
            for index,s in enumerate(slots):
                self._conn.execute('INSERT INTO reply_slots(batch_id,slot,persona_id,persona_version,account_id,decision_json,draft_at) VALUES(?,?,?,?,?,?,?)',
                    (ident,index,s['persona_id'],s['persona_version'],s['account_id'],_json(s.get('decision',{})) if s.get('decision') else '',s.get('draft_at',0)))
            self._conn.execute("UPDATE post_tasks SET status='planned',reason='reply_batch_created',lease_until=0 WHERE post_id=? AND version=?",(target,version))
        return ident,True

    def claim_reply_slot(self, *, now=None, lease_s=180):
        ts=time.time() if now is None else now
        with self.batch_transaction():
            self._expire_reply_batches(ts)
            self._conn.execute("UPDATE reply_slots SET status='pending',owner='',lease_until=0 WHERE status='claimed' AND lease_until<=? AND action_id=''",(ts,))
            rows=self._conn.execute("SELECT s.*,b.target_id,b.target_version,b.plan_json,b.expires_at,b.catalog_hash FROM reply_slots s JOIN reply_batches b ON b.id=s.batch_id "
                "WHERE b.status='active' AND s.status='pending' AND s.next_attempt_at<=? AND NOT EXISTS (SELECT 1 FROM reply_slots x WHERE x.batch_id=s.batch_id AND (x.status IN ('claimed','sending','unknown') OR (x.slot<s.slot AND x.status NOT IN ('succeeded','cancelled','failed')))) "
                "AND NOT EXISTS(SELECT 1 FROM reply_slots other WHERE other.account_id=s.account_id AND other.status IN ('claimed','sending','unknown') AND (other.batch_id<>s.batch_id OR other.slot<>s.slot)) "
                "AND NOT EXISTS(SELECT 1 FROM account_leases a WHERE a.account_id=s.account_id AND a.lease_until>?) "
                "AND (s.slot=0 OR EXISTS(SELECT 1 FROM reply_slots p WHERE p.batch_id=s.batch_id AND p.slot=0 AND p.status='succeeded')) ORDER BY b.created_at,s.slot LIMIT 1",(ts,ts)).fetchone()
            if not rows: return None
            owner=uuid.uuid4().hex
            self._conn.execute("UPDATE reply_slots SET status='claimed',owner=?,lease_until=? WHERE batch_id=? AND slot=?",(owner,ts+lease_s,rows['batch_id'],rows['slot']))
            return {**dict(rows),'owner':owner}

    def renew_reply_slot(self, slot):
        with self.batch_transaction():
            self._conn.execute("UPDATE reply_slots SET lease_until=? WHERE batch_id=? AND slot=? AND owner=? AND status IN ('claimed','sending')",(time.time()+180,slot['batch_id'],slot['slot'],slot['owner']))

    def save_slot_draft(self, slot, decision, *, now=None):
        with self.batch_transaction():
            return self._conn.execute("UPDATE reply_slots SET decision_json=?,draft_at=? WHERE batch_id=? AND slot=? AND owner=? AND status='claimed'",
                (_json(decision),time.time() if now is None else now,slot['batch_id'],slot['slot'],slot['owner'])).rowcount==1

    def defer_reply_slot(self, slot, reason, *, delay=15, cancel=False):
        with self.batch_transaction():
            changed=self._conn.execute("UPDATE reply_slots SET status=?,reason=?,next_attempt_at=?,lease_until=0,owner='' WHERE batch_id=? AND slot=? AND owner=? AND status='claimed' AND action_id=''",
                ('cancelled' if cancel else 'pending',reason,time.time()+delay,slot['batch_id'],slot['slot'],slot['owner'])).rowcount
            if changed:
                if cancel and slot['slot']==0: self._cancel_batch(slot['batch_id'],reason)
                self._complete_batch(slot['batch_id'])
            return bool(changed)

    def successful_batch_replies(self, batch_id):
        with self._lock:
            rows=self._conn.execute("SELECT s.slot,s.account_id,a.id AS action_id,a.text,a.response_json FROM reply_slots s JOIN action_intents a ON a.id=s.action_id WHERE s.batch_id=? AND s.status='succeeded' AND a.status='succeeded' ORDER BY s.slot",(batch_id,)).fetchall()
            return [{'slot':r['slot'],'account_id':r['account_id'],'action_id':r['action_id'],'text':r['text'],
                'post_id':json.loads(r['response_json']).get('post_id','')} for r in rows]

    def _cancel_batch(self, batch, reason):
        self._conn.execute("UPDATE reply_batches SET status='cancelled',reason=? WHERE id=?",(reason,batch))
        self._conn.execute("UPDATE reply_slots SET status='cancelled',reason=?,owner='',lease_until=0 WHERE batch_id=? AND status IN ('pending','claimed') AND action_id=''",(reason,batch))
        self._update_batch_task(batch,reason)

    def _complete_batch(self, batch):
        pending=self._conn.execute("SELECT 1 FROM reply_slots WHERE batch_id=? AND status NOT IN ('succeeded','failed','cancelled') LIMIT 1",(batch,)).fetchone()
        if not pending: self._conn.execute("UPDATE reply_batches SET status='completed' WHERE id=? AND status='active'",(batch,))
        self._update_batch_task(batch,'batch_progress')

    def _update_batch_task(self,batch,reason):
        row=self._conn.execute('SELECT target_id,target_version,status FROM reply_batches WHERE id=?',(batch,)).fetchone()
        if not row:return
        if self._conn.execute("SELECT 1 FROM reply_slots WHERE batch_id=? AND status='unknown'",(batch,)).fetchone():status='unknown'
        elif row['status'] in ('active','paused'):status='planned'
        elif self._conn.execute("SELECT 1 FROM reply_slots WHERE batch_id=? AND slot=0 AND status='succeeded'",(batch,)).fetchone():status='succeeded'
        else:status='skipped'
        self._conn.execute('UPDATE post_tasks SET status=?,reason=? WHERE post_id=? AND version=?',(status,reason,row['target_id'],row['target_version']))

    def _expire_reply_batches(self, ts):
        for b in self._conn.execute("SELECT * FROM reply_batches WHERE status IN ('active','paused')").fetchall():
            p=self.get_observed_post(b['target_id'])
            if b['expires_at']<=ts or not p or p['version']!=b['target_version']:
                self._cancel_batch(b['id'],'opportunity_expired' if b['expires_at']<=ts else 'target_edited')

    def reply_slot_ready(self, account, target, meta, *, sending=False):
        batch=meta.get('reply_batch_id');index=meta.get('reply_slot')
        if not batch:
            if self._conn.execute('SELECT 1 FROM reply_batches WHERE target_id=?',(target,)).fetchone(): return False,'target_has_reply_batch'
            return True,'legacy'
        s=self._conn.execute('SELECT s.*,b.status AS batch_status,b.target_id,b.target_version,b.expires_at FROM reply_slots s JOIN reply_batches b ON b.id=s.batch_id WHERE s.batch_id=? AND s.slot=?',(batch,index)).fetchone()
        if not s or s['account_id']!=account or s['target_id']!=target or s['batch_status']!='active': return False,'invalid_reply_slot'
        if s['persona_id']!=meta.get('persona_id') or s['persona_version']!=meta.get('persona_version'):
            return False,'invalid_reply_slot_identity'
        p=self.get_observed_post(target)
        if not p or p['version']!=s['target_version']:return False,'target_edited'
        expected='sending' if sending else 'claimed'
        if s['status']!=expected or s['owner']!=meta.get('slot_owner') or (not sending and s['lease_until']<=time.time()): return False,'reply_slot_not_owned'
        if s['expires_at']<=time.time(): return False,'opportunity_expired'
        if index and not self._conn.execute("SELECT 1 FROM reply_slots WHERE batch_id=? AND slot=0 AND status='succeeded'",(batch,)).fetchone(): return False,'primary_not_succeeded'
        if self._conn.execute("SELECT 1 FROM reply_slots WHERE batch_id=? AND slot<>? AND status IN ('sending','unknown')",(batch,index)).fetchone(): return False,'batch_send_in_progress'
        return True,'ready'

    def finish_action(self, ident, status, *, response=None, detail='', now=None):
        with self.batch_transaction():
            incoming=self._conn.execute('SELECT kind,status FROM action_intents WHERE id=?',(ident,)).fetchone()
            post_id=(response or {}).get('post_id') or (response or {}).get('id')
            if incoming and incoming['status'] not in ('succeeded','failed') and status=='succeeded' and incoming['kind'] in ('post','reply') and post_id:
                attributed=self._conn.execute('SELECT action_id FROM publications WHERE post_id=?',(str(post_id),)).fetchone()
                if attributed and attributed[0]!=ident:
                    raise ValueError('publication already attributed to another action')
            changed=super().finish_action(ident,status,response=response,detail=detail,now=now)
            if not changed: return False
            row=self._conn.execute('SELECT * FROM action_intents WHERE id=?',(ident,)).fetchone()
            meta=json.loads(row['metadata_json'])
            if status=='succeeded' and row['kind'] in ('post','reply'):
                self.record_publication(dict(row),meta,response or {},now=now)
            if row['kind']=='reply' and meta.get('reply_batch_id'):
                batch=meta['reply_batch_id']
                self._conn.execute('UPDATE reply_slots SET status=?,reason=?,lease_until=0 WHERE batch_id=? AND slot=? AND action_id=?',
                    (status,detail,batch,meta['reply_slot'],ident))
                if status=='unknown': self._conn.execute("UPDATE reply_batches SET status='paused',reason='unknown_action' WHERE id=?",(batch,))
                elif meta['reply_slot']==0 and status=='failed': self._cancel_batch(batch,'primary_failed')
                elif not self._conn.execute("SELECT 1 FROM reply_slots WHERE batch_id=? AND status='unknown'",(batch,)).fetchone():
                    self._conn.execute("UPDATE reply_batches SET status='active',reason='' WHERE id=? AND status='paused'",(batch,))
                self._complete_batch(batch)
            return True

    def start_action(self, ident):
        with self.batch_transaction():
            changed=super().start_action(ident)
            if changed:
                self._conn.execute("UPDATE reply_slots SET status='sending' WHERE action_id=? AND status='claimed'",(ident,))
            return changed

    def recover_actions(self, *, now=None):
        with self.batch_transaction():
            super().recover_actions(now=now)
            rows=self._conn.execute("SELECT s.batch_id,s.slot,a.status FROM reply_slots s JOIN action_intents a ON a.id=s.action_id WHERE s.status IN ('claimed','sending') AND a.status IN ('failed','unknown')").fetchall()
            for row in rows:
                self._conn.execute('UPDATE reply_slots SET status=?,owner=\'\',lease_until=0,reason=\'action_recovered\' WHERE batch_id=? AND slot=?',
                    (row['status'],row['batch_id'],row['slot']))
                if row['status']=='unknown':self._conn.execute("UPDATE reply_batches SET status='paused',reason='unknown_action' WHERE id=?",(row['batch_id'],))
                elif row['slot']==0:self._cancel_batch(row['batch_id'],'primary_failed_before_send')
                self._complete_batch(row['batch_id'])
