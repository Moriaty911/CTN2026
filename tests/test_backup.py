"""ops backup: leaving the audit tables out and compressing the result."""
import contextlib
import gzip
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ctn.ops import main
from ctn.storage import StateStore
from ctn.storage.sqlite_store import BACKUP_AUDIT_TABLES, gzip_backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.db = str(Path(self.dir.name) / 'live.sqlite3')
        store = StateStore(self.db)
        store.set_policy('probe', {'kept': 1})
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for i in range(20):
                conn.execute('INSERT INTO rag_snapshots(id,body_json,created_at) VALUES(?,?,?)',
                             (f'snap-{i}', '{"schema_version":"x"}', 1.0))
                conn.execute('INSERT INTO reply_batches(id,target_id,target_version,catalog_hash,plan_json,created_at,'
                             'expires_at,status) VALUES(?,?,?,?,?,?,?,?)',
                             (f'batch-{i}', str(1000 + i), 1, 'h', '{"a":1}', 1.0, 2.0, 'ready'))
            conn.commit()
        store.close()

    def run_ops(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['--state-file', self.db, *args])
        return code, out.getvalue()

    def rows(self, path, table):
        with contextlib.closing(sqlite3.connect(path)) as conn:
            return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    def test_slim_backup_drops_audit_tables_and_keeps_state(self):
        target = Path(self.dir.name) / 'slim.sqlite3'
        self.assertEqual(self.run_ops('backup', str(target), '--slim')[0], 0)
        for table in BACKUP_AUDIT_TABLES:
            self.assertEqual(self.rows(target, table), 0, table)
        with contextlib.closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute("SELECT value_json FROM runtime_policy WHERE key='probe'").fetchone()[0],
                             json.dumps({'kept': 1}))
        # The live database keeps its audit history.
        self.assertEqual(self.rows(self.db, 'rag_snapshots'), 20)

    def test_full_backup_keeps_audit_tables(self):
        target = Path(self.dir.name) / 'full.sqlite3'
        self.assertEqual(self.run_ops('backup', str(target))[0], 0)
        self.assertEqual(self.rows(target, 'rag_snapshots'), 20)

    def test_gzip_backup_is_readable_and_replaces_the_original(self):
        target = Path(self.dir.name) / 'gz.sqlite3'
        code, out = self.run_ops('backup', str(target), '--slim', '--gzip')
        self.assertEqual(code, 0)
        result = json.loads(out)
        packed = Path(result['backup'])
        self.assertEqual(packed.suffix, '.gz')
        self.assertEqual(result['bytes'], packed.stat().st_size)
        self.assertFalse(target.exists())
        restored = Path(self.dir.name) / 'restored.sqlite3'
        with gzip.open(packed, 'rb') as src:
            restored.write_bytes(src.read())
        with contextlib.closing(sqlite3.connect(restored)) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute("SELECT value_json FROM runtime_policy WHERE key='probe'").fetchone()[0],
                             json.dumps({'kept': 1}))

    def test_gzip_helper_refuses_to_overwrite_and_keeps_original_on_failure(self):
        target = Path(self.dir.name) / 'plain.sqlite3'
        target.write_bytes(b'x' * 1000)
        packed = gzip_backup(target)
        self.assertEqual(target.exists(), False)
        self.assertEqual(gzip.open(packed, 'rb').read(), b'x' * 1000)
        again = Path(self.dir.name) / 'again.sqlite3'
        again.write_bytes(b'y')
        with self.assertRaises(FileExistsError):
            gzip_backup(Path(self.dir.name) / 'plain.sqlite3')
        self.assertTrue(again.exists())

    def test_existing_destination_is_refused(self):
        target = Path(self.dir.name) / 'taken.sqlite3'
        target.write_bytes(b'')
        with self.assertRaises(FileExistsError):
            self.run_ops('backup', str(target), '--slim')


if __name__ == '__main__':
    unittest.main()
