"""scripts/dbq.py: read-only queries for the on-call session."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from ctn.storage import StateStore
from scripts.dbq import main


class DbqTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.db = str(Path(self.dir.name) / 't.sqlite3')
        store = StateStore(self.db); store.set_policy('probe', 1); store.close()

    def run_dbq(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:code = main(['--db', self.db, *args])
            except SystemExit as exc:code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_selects_rows_as_json(self):
        code, out, _ = self.run_dbq("SELECT value_json FROM runtime_policy WHERE key='probe'")
        self.assertEqual((code, out.strip()), (0, '{"value_json": "1"}'))

    def test_cannot_write(self):
        self.assertEqual(self.run_dbq('DELETE FROM runtime_policy')[0], 2)
        self.assertEqual(self.run_dbq('SELECT 1; DELETE FROM runtime_policy')[0], 2)
        code, _, err = self.run_dbq('WITH x AS (SELECT 1) DELETE FROM runtime_policy')
        self.assertEqual(code, 1); self.assertIn('readonly', err)
        self.assertEqual(self.run_dbq("SELECT COUNT(*) n FROM runtime_policy WHERE key='probe'")[1].strip(), '{"n": 1}')


if __name__ == '__main__':
    unittest.main()
