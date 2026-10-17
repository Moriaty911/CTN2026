"""Synthetic file-backed SQLite benchmark. Never opens the project's runtime DB.

Reports warm-cache latency of real store methods, plus complete crawler ingest
and detector work. Bulk population is deliberately measured separately.
"""
from __future__ import annotations

import json
from pathlib import Path
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctn.models import Post
from ctn.moderation import BotDetector
from ctn.pipeline.crawl import _ingest
from ctn.storage import StateStore


def measure(call, repeats=40):
    values = []
    for _ in range(repeats):
        start = time.perf_counter()
        call()
        values.append((time.perf_counter() - start) * 1000)
    values.sort()
    return {'p50_ms': round(statistics.median(values), 4),
            'p95_ms': round(values[int(.95 * (len(values) - 1))], 4),
            'max_ms': round(max(values), 4), 'repeats': repeats}


def run_bench(n, directory):
    path = directory / f'posts-{n}.db'
    store = StateStore(path)
    now = 1800000000.
    authors = 1000
    for i in range(authors):
        store.record_author_observation(f'author-{i}', now=now - 200000)
    rng = random.Random(61)
    vocabulary = [f'w{i}' for i in range(300)]
    start = time.perf_counter()
    with store.batch_transaction():
        for i in range(n):
            parent = f'p{i-1}' if i % 5 == 1 else ''
            text = ' '.join(rng.sample(vocabulary, 20))
            store.upsert_observed_post({
                'post_id': f'p{i}', 'author': f'author-{i % authors}', 'text': text,
                'created_at': now - n + i, 'parent_id': parent,
                'likes': i % 50, 'replies': 0 if parent else i % 30,
            }, now=now - n + i)
    population_s = time.perf_counter() - start
    # Make 5% of authors confirmed, without pretending this is ground truth.
    with store.batch_transaction():
        for i in range(50):
            store.add_bot_evidence(f'author-{i}', force_status='confirmed_bot', now=now)

    last = store.get_observed_post(f'p{n-1}')
    methods = {
        'post_primary_key': lambda: store.get_observed_post(f'p{n-1}'),
        'author_last20': lambda: store.get_author_posts('author-1'),
        'norm_hash_lookup': lambda: store.find_norm_hash_collisions(last['norm_hash']),
        'comment_queue15': lambda: store.get_posts_needing_comment_crawl(limit=15, now=now),
        'confirmed_corpus150': lambda: store.get_confirmed_bot_posts(limit=150),
        'unconfirmed_candidates200': lambda: store.get_unconfirmed_candidate_posts(limit=200),
        'all_author_timestamps_update': lambda: store.record_author_observation('author-999', now=now),
    }
    timings = {name: measure(fn) for name, fn in methods.items()}
    timings['runtime_metrics'] = measure(store.runtime_metrics,repeats=10)
    timings['task_recovery_first'] = measure(lambda:store.claim_post_tasks(now=now),repeats=1)
    timings['task_claim8_warm'] = measure(lambda:store.claim_post_tasks(now=now),repeats=10)
    detector = BotDetector(store)
    timings['scan_one_confirmed_author'] = measure(
        lambda: detector.scan_similar_to_author('author-1', now=now), repeats=10)

    statements = []
    store._conn.set_trace_callback(statements.append)
    start = time.perf_counter()
    with store.batch_transaction():
        for i in range(30):
            post = Post(post_id=f'new-{i}', author=f'new-author-{i}',
                        text=' '.join(rng.sample(vocabulary, 20)), created_at=now+i)
            _ingest(store, post, now=now+i, detector=detector)
    ingest_ms = (time.perf_counter() - start) * 1000
    store._conn.set_trace_callback(None)

    plans = {}
    queries = {
        'comment_queue': ("SELECT p.* FROM comment_queue q JOIN observed_posts p USING(post_id) "
                          "WHERE q.priority>=1 AND q.next_fetch_at<=? ORDER BY q.priority DESC LIMIT 15", (now,)),
        'confirmed_corpus': ("SELECT p.* FROM observed_posts p INNER JOIN bot_registry b ON p.author=b.account_id "
                              "WHERE (b.status='confirmed_bot' OR b.is_suspended=1) AND p.author != '' "
                              "ORDER BY b.is_suspended DESC,p.first_seen_at DESC LIMIT 150", ()),
    }
    for name, (query, params) in queries.items():
        plans[name] = [str(r['detail']) for r in store._conn.execute('EXPLAIN QUERY PLAN '+query, params)]
    pragmas = {key: store._conn.execute('PRAGMA '+key).fetchone()[0]
               for key in ('journal_mode', 'synchronous', 'busy_timeout', 'page_size')}
    result = {'rows': n, 'authors': authors, 'confirmed_author_fraction': .05,
              'bulk_population_seconds': round(population_s, 3), 'warm_cache_latency': timings,
              'ingest30_with_detector_ms': round(ingest_ms, 3),
              'ingest30_sql_statements': len(statements),
              'ingest30_commits': sum(q.upper().startswith('COMMIT') for q in statements),
              'query_plans': plans, 'pragmas': pragmas,
              'database_mb': round(store._conn.execute('PRAGMA page_count').fetchone()[0] *
                                   store._conn.execute('PRAGMA page_size').fetchone()[0] / 1024**2, 2)}
    store.close()
    return result


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--rows',type=int,nargs='+',default=[100000,1000000])
    args=parser.parse_args()
    with tempfile.TemporaryDirectory() as td:
        result = {'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version,
                  'system': platform.platform(), 'storage': 'temporary local file; WAL/NORMAL; warm cache',
                  'benchmarks': [run_bench(n, Path(td)) for n in args.rows]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
