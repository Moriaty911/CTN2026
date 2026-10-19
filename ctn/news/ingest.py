"""Poll sites.legitreal.com, store articles, extract and verify claims (policies/news-v1.json).

One `step()` does whatever is due: the site list every 6 h, each front page every 10 min, new and
recently published articles (re-fetched to catch edits), then a couple of extractions. Network and
model failures are logged and retried on later steps; they never stop other workers.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from ctn.jev.injection import InjectionPreflight
from ctn.storage.news import NEWS_POLICY


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0.0


class NewsIngestor:
    def __init__(self, store, *, news_sites=None, extractor=None, policy=NEWS_POLICY, notify=True):
        if news_sites is None:
            from capturethenarrative import NewsSites
            news_sites = NewsSites(policy['base_url'], timeout=20.0, max_retries=2)
        self.store, self.news, self.extractor, self.policy, self.notify = store, news_sites, extractor, policy, notify
        self.preflight = InjectionPreflight()
        self._seeded = False

    def _state(self, key, default=None):
        return self.store.policy('news:' + key, default)

    def _set(self, key, value):
        self.store.set_policy('news:' + key, value)

    def record_from(self, site, raw):
        sub = site['sub_url'] if isinstance(site, dict) else str(site)
        name = site.get('name', sub) if isinstance(site, dict) else sub
        category = str(raw.get('category') or '')
        record = {'id': f"{sub}:{raw.get('slug', '')}", 'site': sub, 'site_name': name if name.startswith('The ') else 'The ' + name,
                  'slug': str(raw.get('slug', '')), 'story_id': str(raw.get('story_id') or ''), 'title': str(raw.get('title') or raw.get('headline') or ''),
                  'category': category, 'byline': str(raw.get('byline') or ''), 'published_at': parse_time(raw.get('published_at')),
                  'url': str(raw.get('url') or ''), 'body': str(raw.get('body') or ''),
                  'kind': 'opinion' if category.lower() in self.policy['opinion_categories'] else 'news'}
        check = self.preflight.check({'title': record['title'], 'summary': str(raw.get('summary') or ''), 'body': record['body']})
        record['injection'] = 'flagged' if check.requires_review else 'clean'
        return record

    async def step(self, *, now=None):
        ts = time.time() if now is None else now
        rules = self.policy['intervals']
        report = {'sites': 0, 'fetched': 0, 'new': 0, 'changed': 0, 'extracted': 0, 'errors': []}
        if not self._seeded:
            await asyncio.to_thread(self.store.news_seed, self.policy)
            self._seeded = True
        sites = self._state('sites', {})
        if ts - sites.get('at', 0) >= rules['site_list_seconds'] or not sites.get('items'):
            try:
                items = await asyncio.to_thread(self.news.sites)
                sites = {'at': ts, 'items': [{'sub_url': s['sub_url'], 'name': s.get('name', s['sub_url'])} for s in items if s.get('sub_url')]}
                self._set('sites', sites)
            except Exception as exc:
                report['errors'].append('sites:' + type(exc).__name__)
        due = self._state('front', {})
        known = await asyncio.to_thread(self.store.news_known_articles)
        budget = int(rules['articles_per_step'])
        for site in sites.get('items', []):
            sub = site['sub_url']
            if ts - due.get(sub, 0) < rules['front_page_seconds']:continue
            try:
                listing = await asyncio.to_thread(self.news.articles, sub)
            except Exception as exc:
                report['errors'].append(f'front:{sub}:{type(exc).__name__}');continue
            due[sub] = ts;report['sites'] += 1
            for item in listing:
                ident = f"{sub}:{item.get('slug', '')}"
                old = known.get(ident)
                fresh = ts - parse_time(item.get('published_at')) <= rules['recheck_window_seconds']
                if old and not (fresh and ts - old['last_checked'] >= rules['recheck_recent_seconds']):continue
                if budget <= 0:break
                budget -= 1
                try:
                    raw = await asyncio.to_thread(self.news.article, sub, item['slug'])
                except Exception as exc:
                    report['errors'].append(f'article:{ident}:{type(exc).__name__}');continue
                outcome = await asyncio.to_thread(self.store.news_upsert_article, self.record_from(site, raw), now=ts)
                report['fetched'] += 1
                if outcome in ('new', 'changed'):report[outcome] += 1
        self._set('front', due)
        if self.extractor is not None:
            for article in await asyncio.to_thread(self.store.news_pending_extractions, int(rules['extractions_per_step'])):
                try:
                    accepted, rejected = await asyncio.to_thread(self.extractor.extract, article)
                    report['extracted'] += await asyncio.to_thread(self.store.news_record_claims, article, accepted, rejected)
                except Exception as exc:
                    await asyncio.to_thread(self.store.news_extraction_failed, article['id'], f'{type(exc).__name__}:{exc}')
                    report['errors'].append(f'extract:{article["id"]}:{type(exc).__name__}')
        if self.notify:await asyncio.to_thread(self.publish_alerts, ts)
        return report

    def publish_alerts(self, ts):
        """Discord: one digest per Sydney day after 08:00, and each conflict / quarantine / failed extraction once."""
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(ts, tz=ZoneInfo('Australia/Sydney'))
        last = int(self.store.policy('news:last_event', 0))
        with self.store._lock:
            events = self.store._conn.execute("SELECT id,kind,detail_json FROM news_events WHERE id>? AND kind IN "
                                              "('conflict','article_quarantined','extraction_failed') ORDER BY id", (last,)).fetchall()
            top = self.store._conn.execute('SELECT MAX(id) FROM news_events').fetchone()[0] or last
        import json
        with self.store.batch_transaction():
            for event in events:
                self.store.notify_discord(f'news_event:{event["id"]}', {'type': 'alert', 'kind': 'news_' + event['kind'],
                                                                       'detail': json.loads(event['detail_json'])})
            if local.hour >= 8:
                self.store.notify_discord(f'news_digest:{local.date().isoformat()}', {'type': 'alert', 'kind': 'news_digest',
                                                                                      'summary': self.store.news_summary(now=ts)})
            self.store.set_policy('news:last_event', top)
