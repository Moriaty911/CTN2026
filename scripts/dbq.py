"""Read-only SQL against the live state database, for operators and the on-call Claude session.

    docker compose exec fleet python scripts/dbq.py "SELECT kind,status,COUNT(*) FROM action_intents GROUP BY 1,2"
    docker compose exec fleet python scripts/dbq.py --limit 200 "SELECT ..."
    docker compose exec fleet python scripts/dbq.py --tables          # list tables
    docker compose exec fleet python scripts/dbq.py --schema publications

The database is opened with SQLite's read-only URI mode, so no statement can change it, and only a
single SELECT / WITH / PRAGMA table_info statement is accepted. Output is one JSON object per row.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('sql', nargs='?')
    p.add_argument('--db', default=os.environ.get('CTN_STATE_FILE', '/state/ctn.sqlite3'))
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--tables', action='store_true')
    p.add_argument('--schema')
    a = p.parse_args(argv)
    if a.tables:
        sql = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    elif a.schema:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', a.schema):p.error('bad table name')
        sql = f'PRAGMA table_info({a.schema})'
    elif a.sql:
        sql = a.sql.strip().rstrip(';')
        if ';' in sql or not re.match(r'(?is)^\s*(select|with)\b', sql):
            p.error('only one SELECT or WITH statement is allowed')
    else:
        p.error('give SQL, --tables or --schema')
    conn = sqlite3.connect(f'file:{a.db}?mode=ro', uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(a.limit + 1)
        for row in rows[:a.limit]:
            print(json.dumps(dict(row), ensure_ascii=False, default=str))
        if len(rows) > a.limit:
            print(f'... more rows; raise --limit (now {a.limit})', file=sys.stderr)
    except sqlite3.Error as exc:
        print(f'sqlite error: {exc}', file=sys.stderr);return 1
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
