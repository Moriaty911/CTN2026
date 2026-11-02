"""Shared review application service; used by CLI and Discord, never sends reports."""
import hashlib
import json
import time


class ReviewService:
    def __init__(self,store):self.store=store

    def dashboard(self,limit=25,*,offset=0,status=None):
        if status not in (None,'review','approved','deferred','rejected','stale','sending','pending','unknown','ruled'):
            raise ValueError('invalid review filter')
        limit=min(25,max(1,int(limit)));offset=max(0,int(offset))
        self.store.refresh_report_reviews()
        with self.store._lock:
            counts={r['status']:r['n'] for r in self.store._conn.execute('SELECT status,COUNT(*) AS n FROM report_reviews GROUP BY status')}
            where=' WHERE r.status=?' if status else ''
            args=((status,) if status else ())+(limit,offset)
            rows=self.store._conn.execute('SELECT c.id,c.subject_kind,c.subject_id,c.author,r.status,r.revision,r.resume_at FROM report_candidates c JOIN report_reviews r ON r.candidate_id=c.id'+where+' ORDER BY c.created_at DESC,c.id LIMIT ? OFFSET ?',args).fetchall()
            pending=self.store._conn.execute("SELECT id,subject_kind,subject_id,author,status,submitted_at,report_id FROM report_attempts WHERE status IN ('pending','unknown','sending','claimed') ORDER BY submitted_at").fetchall()
            clearance=self.store.policy('report_clearance',{})
            suspended_until=self.store.get_counter('report_suspended_until_ms')/1000
        return {'state':self.store.report_state_ready(),'automation':self.store.report_state_ready(automatic=True),'mode':self.store.policy('report_mode','off'),'candidates':[dict(r) for r in rows],'pending':[dict(r) for r in pending],
                'candidate_counts':counts,'candidate_total':counts.get(status,0) if status else sum(counts.values()),'offset':offset,'limit':limit,'filter':status,
                'clearance_expires_at':clearance.get('expires_at',0),'clearance_source':clearance.get('source',''),'suspended_until':suspended_until}

    def attempt(self,ident):
        with self.store._lock:
            row=self.store._conn.execute('SELECT * FROM report_attempts WHERE id=?',(ident,)).fetchone()
        if not row:raise ValueError('unknown report attempt')
        return dict(row)

    def candidate(self,ident):
        self.store.refresh_report_reviews()
        with self.store._lock:
            r=self.store._conn.execute('SELECT * FROM report_candidates WHERE id=?',(ident,)).fetchone()
            affiliation=self.store._conn.execute('SELECT * FROM report_affiliations WHERE author=?',(r['author'],)).fetchone() if r else None
        if not r:raise ValueError('unknown candidate')
        end=self.store.policy('campaign_ends_at')
        horizon='unknown' if end is None else ('less_than_24h' if end-time.time()<86400 else 'at_least_24h')
        blockers=[]
        clearance=self.store.report_state_ready()
        if not clearance[0]:blockers.append(clearance[1])
        if self.store.policy('report_mode','off')=='off':blockers.append('reports_disabled')
        with self.store._lock:
            pending=self.store._conn.execute("SELECT author FROM report_attempts WHERE status IN ('claimed','sending','pending','unknown')").fetchall()
        if len(pending)>=int(self.store.policy('max_pending_reports',40)):blockers.append('pending_team_limit')
        if sum(row['author']==r['author'] for row in pending)>=int(self.store.policy('max_pending_per_author',2)):blockers.append('pending_author_limit')
        if r['subject_kind']=='post':
            from .decision import policy,expected_genuine
            rules=policy(self.store)
            if expected_genuine(self.store,rules,extra_p=rules['p_floor'])>rules['max_expected_genuine']:blockers.append('genuine_risk_budget')
        if r['subject_kind']=='account':
            activity=self.store.confirmed_activity(r['author'])
            if not activity or activity['action_count']<100 or self.store.get_bot_profile(r['author'])['upheld_strikes']<1:blockers.append('account_report_not_eligible')
        with self.store._lock:
            decision=self.store._conn.execute('SELECT * FROM report_decisions WHERE candidate_id=?',(ident,)).fetchone()
        return {**dict(r),'payload':json.loads(r['payload_json']),'review':self.store.report_review(ident),'decision':dict(decision) if decision else None,'affiliation':dict(affiliation) if affiliation else None,'clearance':clearance,'blockers':blockers,'ruling_horizon':horizon,'score_is_probability':False}

    def review(self,ident,operation,**kwargs):return self.store.review_report(ident,operation,**kwargs)

    def clearance(self,source,actor):
        from .decision import policy
        self.store.set_report_clearance(source,actor=actor)
        return {'allowed':True,'expires_in_s':int(policy(self.store)['manual_clearance_ttl_seconds'])}

    def pause(self,actor):
        with self.store.batch_transaction():
            self.store.set_policy('reporting_paused',True)
            self.store.set_policy('report_clearance',{})
            self.store.audit_operation(actor,'reporting_pause','campaign',{})
        return {'paused':True}

    def prepare_ruling(self,attempt_id,outcome,ruled_at,source):
        with self.store._lock:r=self.store._conn.execute('SELECT * FROM report_attempts WHERE id=?',(attempt_id,)).fetchone()
        if not r or r['status'] not in ('pending','unknown','ruled'):raise ValueError('pending or unknown report attempt required')
        if outcome not in ('upheld','rejected','dismissed') or not source.strip() or ruled_at<r['submitted_at'] or ruled_at>time.time()+300:raise ValueError('invalid official ruling or observation time')
        if (r['subject_kind']=='post' and outcome=='dismissed') or (r['subject_kind']=='account' and outcome=='rejected'):raise ValueError('outcome incompatible with subject')
        body={'attempt_id':attempt_id,'report_id':r['report_id'],'subject_kind':r['subject_kind'],'subject_id':r['subject_id'],'outcome':outcome,'ruled_at':ruled_at,'source':source}
        body['event_id']='manual:'+hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
        return body

    def ruling(self,event,actor):return self.store.import_ruling(event,actor=actor)

    def affiliation(self,author,side,team_id,source,observed_at=None):
        if side not in ('yes','no','unknown') or not source.strip():raise ValueError('explicit side and source required')
        with self.store.batch_transaction():
            self.store._conn.execute('INSERT INTO report_affiliations VALUES(?,?,?,?,?) ON CONFLICT(author) DO UPDATE SET side=excluded.side,team_id=excluded.team_id,source=excluded.source,observed_at=excluded.observed_at',(author,side,team_id,source,observed_at or time.time()))
            if team_id:self.store._conn.execute('UPDATE bot_registry SET true_team_id=? WHERE account_id=?',(team_id,author))
            self.store.audit_operation('cli','report_affiliation',author,{'side':side,'team_id':team_id,'source':source})
