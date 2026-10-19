"""In-universe news as attributed, verified claims (policies/news-v1.json).

Articles come from sites.legitreal.com; claims are extracted by ctn.news.extract and must pass
verbatim checks before they are stored as `passed`. Claims are always used with attribution
("The Kingstonia Herald reported on 18 Oct that ..."), never as unattributed facts.
Statuses: passed, conflict (same measure, different numbers, same day), superseded (an older
reading of a time-varying measure, or an article revision), rejected, withdrawn.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

NEWS_POLICY = json.loads((Path(__file__).resolve().parents[2] / 'policies/news-v1.json').read_text())
SYDNEY = ZoneInfo('Australia/Sydney')

NEWS_SCHEMA = '''
CREATE TABLE IF NOT EXISTS news_articles(id TEXT PRIMARY KEY,site TEXT NOT NULL,site_name TEXT NOT NULL,slug TEXT NOT NULL,
 story_id TEXT NOT NULL DEFAULT '',title TEXT NOT NULL DEFAULT '',category TEXT NOT NULL DEFAULT '',byline TEXT NOT NULL DEFAULT '',
 published_at REAL NOT NULL DEFAULT 0,url TEXT NOT NULL DEFAULT '',body TEXT NOT NULL DEFAULT '',body_hash TEXT NOT NULL DEFAULT '',
 kind TEXT NOT NULL DEFAULT 'news',injection TEXT NOT NULL DEFAULT 'clean',status TEXT NOT NULL DEFAULT 'pending_extraction',
 attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 1,
 first_seen REAL NOT NULL DEFAULT 0,last_checked REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_news_articles_status ON news_articles(status,published_at);
CREATE TABLE IF NOT EXISTS news_claims(id TEXT PRIMARY KEY,article_id TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
 story_id TEXT NOT NULL DEFAULT '',site TEXT NOT NULL,source_name TEXT NOT NULL,kind TEXT NOT NULL,statement TEXT NOT NULL,
 attributed_text TEXT NOT NULL,evidence TEXT NOT NULL DEFAULT '',metric_key TEXT NOT NULL DEFAULT '',numbers_json TEXT NOT NULL DEFAULT '[]',
 entities_json TEXT NOT NULL DEFAULT '[]',topics_json TEXT NOT NULL DEFAULT '[]',yes_effect TEXT NOT NULL DEFAULT 'neutral',
 time_varying INTEGER NOT NULL DEFAULT 0,published_at REAL NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'passed',
 corroborated_json TEXT NOT NULL DEFAULT '[]',reason TEXT NOT NULL DEFAULT '',extra_json TEXT NOT NULL DEFAULT '{}',url TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_news_claims_status ON news_claims(status,published_at);
CREATE INDEX IF NOT EXISTS idx_news_claims_metric ON news_claims(metric_key);
CREATE TABLE IF NOT EXISTS news_events(id INTEGER PRIMARY KEY,created_at REAL NOT NULL,kind TEXT NOT NULL,detail_json TEXT NOT NULL);
'''

_NUMBER_WORDS = {'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6', 'seven': '7', 'eight': '8',
                 'nine': '9', 'ten': '10', 'eleven': '11', 'twelve': '12', 'fifteen': '15', 'twenty': '20', 'thirty': '30',
                 'forty': '40', 'fifty': '50', 'sixty': '60', 'seventy': '70', 'eighty': '80', 'ninety': '90',
                 'hundred': '100', 'thousand': '1000', 'fortnight': '14'}


_LOWER_LEADS = {'a', 'an', 'the', 'there', 'it', 'its', 'this', 'these', 'those', 'some', 'more', 'new', 'about', 'nearly', 'almost',
                'roughly', 'several', 'many', 'most', 'half', 'one', 'two', 'three', 'forty', 'officials', 'health', 'civil', 'local'}


def normalize_number(value):
    text = str(value).strip().lower().replace(',', '').rstrip('%')
    text = _NUMBER_WORDS.get(text, text)
    try:
        number = float(text)
        return str(int(number)) if number == int(number) else str(number)
    except ValueError:
        return text


def tokens(text):
    return set(re.findall(r'[a-z0-9]{3,}', str(text).lower().replace('_', ' ')))


def day_label(ts):
    local = datetime.fromtimestamp(ts, tz=SYDNEY)
    return f'{local.day} {local.strftime("%b")}'


def attribute(source_name, kind, statement, published_at, source_kind='news'):
    """Deterministic attribution: the model never writes the source line itself."""
    statement = statement.strip().rstrip('.')
    first = statement.split(' ', 1)[0]
    lead = statement[0].lower() + statement[1:] if first.lower() in _LOWER_LEADS and not first.isupper() else statement
    when = day_label(published_at) if published_at else 'recently'
    if source_kind == 'client_brief':
        return f'{source_name} says {lead} (campaign brief, {when}).'
    if kind in ('position', 'campaign_line', 'quote'):
        return f'According to {source_name} ({when}), {lead}.'
    return f'{source_name} reported on {when} that {lead}.'


def claim_id(article_id, revision, statement):
    return hashlib.sha256(f'{article_id}|{revision}|{statement.strip().lower()}'.encode()).hexdigest()[:24]


class NewsStoreMixin:
    def migrate_news(self):
        for statement in NEWS_SCHEMA.split(';'):
            if statement.strip():self._conn.execute(statement)

    def news_event(self, kind, detail):
        self._conn.execute('INSERT INTO news_events(created_at,kind,detail_json) VALUES(?,?,?)', (time.time(), kind, json.dumps(detail, ensure_ascii=False)))

    # ---- articles ----------------------------------------------------- #
    def news_upsert_article(self, record, *, now=None):
        """Store a fetched article. Returns 'new', 'changed' or 'same'."""
        ts = time.time() if now is None else now
        body = record.get('body') or ''
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        with self.batch_transaction():
            row = self._conn.execute('SELECT body_hash,revision FROM news_articles WHERE id=?', (record['id'],)).fetchone()
            if row and row['body_hash'] == body_hash:
                self._conn.execute('UPDATE news_articles SET last_checked=? WHERE id=?', (ts, record['id']))
                return 'same'
            revision = (row['revision'] + 1) if row else 1
            fields = (record['site'], record['site_name'], record['slug'], record.get('story_id', ''), record.get('title', ''),
                      record.get('category', ''), record.get('byline', ''), float(record.get('published_at') or 0), record.get('url', ''),
                      body, body_hash, record.get('kind', 'news'), record.get('injection', 'clean'),
                      'pending_extraction' if record.get('injection', 'clean') == 'clean' else 'quarantined')
            if row:
                self._conn.execute('UPDATE news_articles SET site=?,site_name=?,slug=?,story_id=?,title=?,category=?,byline=?,published_at=?,url=?,'
                                   'body=?,body_hash=?,kind=?,injection=?,status=?,attempts=0,error=\'\',revision=?,last_checked=? WHERE id=?',
                                   fields + (revision, ts, record['id']))
                self._conn.execute("UPDATE news_claims SET status='superseded',reason='article_revised' WHERE article_id=? AND status IN ('passed','conflict')",
                                   (record['id'],))
                self.news_event('article_revised', {'id': record['id'], 'revision': revision})
            else:
                self._conn.execute('INSERT INTO news_articles(site,site_name,slug,story_id,title,category,byline,published_at,url,body,body_hash,kind,'
                                   'injection,status,id,revision,first_seen,last_checked) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   fields + (record['id'], revision, ts, ts))
            if record.get('injection', 'clean') != 'clean':
                self.news_event('article_quarantined', {'id': record['id'], 'title': record.get('title', '')})
            for metric in self._metrics_of_article(record['id']):self._news_reconcile(metric)
            return 'changed' if row else 'new'

    def _metrics_of_article(self, article_id):
        return [r[0] for r in self._conn.execute("SELECT DISTINCT metric_key FROM news_claims WHERE article_id=? AND metric_key<>''", (article_id,))]

    def news_known_articles(self):
        with self._lock:
            return {r['id']: dict(r) for r in self._conn.execute('SELECT id,published_at,last_checked,body_hash FROM news_articles')}

    def news_pending_extractions(self, limit=2):
        rules = NEWS_POLICY['intervals']
        with self._lock:
            rows = self._conn.execute("SELECT * FROM news_articles WHERE status='pending_extraction' AND attempts<? ORDER BY published_at DESC LIMIT ?",
                                      (int(rules['extraction_max_attempts']), limit)).fetchall()
        return [dict(r) for r in rows]

    def news_extraction_failed(self, article_id, error):
        rules = NEWS_POLICY['intervals']
        with self.batch_transaction():
            self._conn.execute("UPDATE news_articles SET attempts=attempts+1,error=?,status=CASE WHEN attempts+1>=? THEN 'failed' ELSE status END WHERE id=?",
                               (str(error)[:200], int(rules['extraction_max_attempts']), article_id))
            row = self._conn.execute('SELECT status,title FROM news_articles WHERE id=?', (article_id,)).fetchone()
            if row and row['status'] == 'failed':self.news_event('extraction_failed', {'id': article_id, 'title': row['title'], 'error': str(error)[:200]})

    # ---- claims --------------------------------------------------------- #
    def news_record_claims(self, article, accepted, rejected, *, now=None):
        """Store verified claims (and rejected ones for audit) for one article revision, then reconcile."""
        ts = time.time() if now is None else now
        with self.batch_transaction():
            current = self._conn.execute('SELECT revision FROM news_articles WHERE id=?', (article['id'],)).fetchone()
            if not current or current['revision'] != article['revision']:return 0
            metrics = set()
            for claim, status in [(c, 'passed') for c in accepted] + [(c, 'rejected') for c in rejected]:
                ident = claim_id(article['id'], article['revision'], claim['statement'])
                attributed = (attribute(article['site_name'], claim['kind'], claim['statement'], article['published_at'], article.get('kind', 'news'))
                              if claim['kind'] != 'concern' else '')
                self._conn.execute('INSERT OR REPLACE INTO news_claims(id,article_id,revision,story_id,site,source_name,kind,statement,attributed_text,evidence,'
                                   'metric_key,numbers_json,entities_json,topics_json,yes_effect,time_varying,published_at,status,reason,extra_json,url,created_at) '
                                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (ident, article['id'], article['revision'], article.get('story_id', ''), article['site'], article['site_name'],
                                    claim['kind'], claim['statement'], attributed, claim.get('evidence', ''), claim.get('metric_key', ''),
                                    json.dumps([normalize_number(n) for n in claim.get('numbers', [])]), json.dumps(claim.get('entities', [])),
                                    json.dumps(claim.get('topics', [])), claim.get('yes_effect', 'neutral'), int(bool(claim.get('time_varying'))),
                                    float(article['published_at']), status, claim.get('reason', ''), json.dumps(claim.get('extra', {}), ensure_ascii=False),
                                    article.get('url', ''), ts))
                if status == 'passed' and claim.get('metric_key'):metrics.add(claim['metric_key'])
            self._conn.execute("UPDATE news_articles SET status='extracted',error='' WHERE id=?", (article['id'],))
            for metric in metrics:self._news_reconcile(metric)
            self.news_event('article_extracted', {'id': article['id'], 'accepted': len(accepted), 'rejected': len(rejected)})
            return len(accepted)

    def _news_reconcile(self, metric):
        """Corroboration, conflicts and supersession for one measure (e.g. kingstonia_central_hospital:nursing_vacancies)."""
        rows = [dict(r) for r in self._conn.execute(
            "SELECT id,site,numbers_json,time_varying,published_at,status FROM news_claims WHERE metric_key=? AND status IN ('passed','conflict','superseded') "
            "AND reason NOT IN ('article_revised')", (metric,))]
        if not rows:return
        for r in rows:r['nums'] = tuple(sorted(json.loads(r['numbers_json'])))
        newest = max(r['published_at'] for r in rows)
        current = [r for r in rows if not r['time_varying'] or abs(newest - r['published_at']) < 86400]
        stale = [r for r in rows if r not in current]
        numbered = {r['nums'] for r in current if r['nums']}
        conflict = len(numbered) > 1
        latest_nums = max(current, key=lambda r: r['published_at'])['nums']
        for r in current:
            agree = [o for o in current + stale if o['id'] != r['id'] and o['site'] != r['site'] and o['nums'] == r['nums']]
            status = 'conflict' if conflict and r['nums'] else 'passed'
            self._conn.execute('UPDATE news_claims SET status=?,corroborated_json=? WHERE id=?',
                               (status, json.dumps(sorted({o['site'] for o in agree})), r['id']))
        for r in stale:
            status = 'superseded' if r['nums'] != latest_nums else 'passed'
            agree = [o for o in current if o['site'] != r['site'] and o['nums'] == r['nums']]
            self._conn.execute('UPDATE news_claims SET status=?,corroborated_json=? WHERE id=?',
                               (status, json.dumps(sorted({o['site'] for o in agree})), r['id']))
        if conflict:
            key = f'conflict:{metric}:{sorted(numbered)}'
            if not self._conn.execute('SELECT 1 FROM news_events WHERE kind=? LIMIT 1', (key,)).fetchone():
                self._conn.execute('INSERT INTO news_events(created_at,kind,detail_json) VALUES(?,?,?)', (time.time(), key, '{}'))
                self.news_event('conflict', {'metric': metric, 'values': [list(n) for n in sorted(numbered)]})

    def news_seed(self, policy=NEWS_POLICY):
        """Insert curated sources such as the client brief (idempotent)."""
        for source in policy.get('seed_sources', []):
            published = datetime.fromisoformat(source['published_at']).timestamp()
            article = {'id': 'seed:' + source['id'], 'site': source['id'], 'site_name': source['attribution'], 'revision': 1, 'kind': source['kind'],
                       'published_at': published, 'story_id': '', 'url': ''}
            with self.batch_transaction():
                if self._conn.execute('SELECT 1 FROM news_articles WHERE id=?', (article['id'],)).fetchone():continue
                self._conn.execute("INSERT INTO news_articles(id,site,site_name,slug,title,published_at,kind,status,first_seen,last_checked) "
                                   "VALUES(?,?,?,?,?,?,?,?,?,?)", (article['id'], source['id'], source['attribution'], source['id'], source['name'],
                                                                   published, source['kind'], 'pending_extraction', time.time(), time.time()))
            self.news_record_claims(article, [{**c, 'evidence': source['note']} for c in source['claims']], [])

    def news_retrieve(self, text, *, purpose=None, now=None, policy=NEWS_POLICY):
        """Claims, opposing concerns and counter cards relevant to `text`."""
        ts = time.time() if now is None else now
        rules = policy['retrieval']
        query = tokens(text)
        lowered = str(text).lower()
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM news_claims WHERE status='passed' ORDER BY published_at DESC LIMIT 500").fetchall()]
        scored = []
        for r in rows:
            topics = tokens(' '.join(json.loads(r['topics_json'])) + ' ' + r['metric_key'])
            body = tokens(r['statement'])
            score = 3 * len(query & topics) + len(query & body)
            if not score:continue
            if ts - r['published_at'] <= rules['recent_hours'] * 3600:score += rules['recent_bonus']
            if json.loads(r['corroborated_json']):score += rules['corroborated_bonus']
            if purpose in ('P00', 'P01') and r['yes_effect'] == 'helps':score += rules['helps_bonus_for_originals']
            scored.append((score, r['published_at'], r))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        claims = [r for _, _, r in scored if r['kind'] != 'concern'][:rules['max_claims']]
        concerns = [r for _, _, r in scored if r['kind'] == 'concern'][:rules['max_concerns']]
        cards = [c for c in policy['counter_cards'] if any(t in lowered for t in c['triggers'])][:rules['max_counter_cards']]
        def public(r):
            item = {'id': r['id'], 'kind': r['kind'], 'text': r['attributed_text'] or r['statement'], 'source': r['source_name'],
                    'url': r['url'], 'published_at': r['published_at'], 'corroborated_by': json.loads(r['corroborated_json']),
                    'yes_effect': r['yes_effect']}
            if r['kind'] == 'concern':item['extra'] = json.loads(r['extra_json'])
            return item
        return {'claims': [public(r) for r in claims], 'concerns': [public(r) for r in concerns], 'counter_cards': cards}

    def news_recent_for_pi(self, *, now=None, policy=NEWS_POLICY):
        ts = time.time() if now is None else now
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM news_claims WHERE status='passed' AND kind<>'concern' AND published_at>=? ORDER BY published_at DESC",
                (ts - policy['pi_recent_hours'] * 3600,)).fetchall()]
        rank = {'helps': 0, 'neutral': 1, 'hurts': 2}
        rows.sort(key=lambda r: (rank.get(r['yes_effect'], 1), -r['published_at']))
        return [{'text': r['attributed_text'], 'yes_effect': r['yes_effect'], 'corroborated_by': json.loads(r['corroborated_json'])}
                for r in rows[:policy['pi_max_claims']]]

    def news_withdraw(self, ident, *, actor='cli', reason=''):
        with self.batch_transaction():
            changed = self._conn.execute("UPDATE news_claims SET status='withdrawn',reason=? WHERE id=? AND status<>'withdrawn'",
                                         ('withdrawn:' + (reason or actor), ident)).rowcount
            if changed:self.audit_operation(actor, 'news_withdraw', ident, {'reason': reason})
            return bool(changed)

    def news_list(self, *, status=None, limit=50):
        with self._lock:
            rows = self._conn.execute('SELECT id,kind,status,source_name,published_at,attributed_text,statement,corroborated_json,reason FROM news_claims'
                                      + (' WHERE status=?' if status else '') + ' ORDER BY published_at DESC LIMIT ?',
                                      ((status, limit) if status else (limit,))).fetchall()
        return [dict(r) for r in rows]

    def news_show(self, ident):
        with self._lock:
            row = self._conn.execute('SELECT * FROM news_claims WHERE id=?', (ident,)).fetchone()
        return dict(row) if row else None

    def news_summary(self, *, since=None, now=None):
        ts = time.time() if now is None else now
        start = ts - 86400 if since is None else since
        with self._lock:
            articles = self._conn.execute('SELECT status,COUNT(*) FROM news_articles WHERE first_seen>=? GROUP BY status', (start,)).fetchall()
            claims = self._conn.execute('SELECT status,COUNT(*) FROM news_claims WHERE created_at>=? GROUP BY status', (start,)).fetchall()
            best = self._conn.execute("SELECT attributed_text FROM news_claims WHERE status='passed' AND yes_effect='helps' AND created_at>=? "
                                      "ORDER BY published_at DESC LIMIT 3", (start,)).fetchall()
            worst = self._conn.execute("SELECT attributed_text FROM news_claims WHERE status='passed' AND yes_effect='hurts' AND created_at>=? "
                                       "ORDER BY published_at DESC LIMIT 3", (start,)).fetchall()
        return {'articles': {r[0]: r[1] for r in articles}, 'claims': {r[0]: r[1] for r in claims},
                'helps_yes': [r[0] for r in best], 'hurts_yes': [r[0] for r in worst]}

    # ---- publication guards --------------------------------------------- #
    def message_policy_gate(self, text, *, exclude_action='', policy=NEWS_POLICY):
        """Slogan frequency and overclaim checks applied to every post and reply."""
        lowered = text.lower()
        for pattern in policy['overclaim_patterns']:
            if re.search(pattern, lowered):return False, 'overclaim_promise'
        rules = policy['slogan']
        if any(p in lowered for p in rules['patterns']):
            recent = self._conn.execute("SELECT text FROM action_intents WHERE kind IN ('post','reply') AND status IN ('claimed','sending','unknown','succeeded') "
                                        "AND id<>? ORDER BY created_at DESC LIMIT ?", (exclude_action, int(rules['window_posts']))).fetchall()
            used = sum(any(p in (r[0] or '').lower() for p in rules['patterns']) for r in recent)
            if used >= int(rules['max_in_window']):return False, 'slogan_frequency'
        return True, 'ready'
