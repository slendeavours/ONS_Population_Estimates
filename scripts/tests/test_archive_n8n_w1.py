"""archive_n8n_w1 with a stub connection: no real n8n access."""
import datetime
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import archive_n8n_w1 as a  # noqa: E402


class Cur:
    def __init__(self, row):
        self.row, self.updates, self.rowcount = row, [], 1

    def execute(self, sql, params=None):
        if sql.lstrip().upper().startswith("UPDATE"):
            self.updates.append((sql, params))
            self.row = (self.row[0], params[0], False, True, self.row[4], self.row[5])

    def fetchone(self):
        return self.row


class Conn:
    def __init__(self, name, archived):
        self.c = Cur((a.W1_ID, name, False, archived, [{"n": 1}], {}))
        self.commits = 0

    def cursor(self):
        return self.c

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.file = self.dir / f"W1_n8n_workflow_backup_{datetime.date.today():%Y-%m-%d}.json"

    def test_already_archived_does_nothing_in_both_modes(self):
        for argv in ([], ["--commit"]):
            conn = Conn(a.NEW_NAME, True)
            self.assertEqual(a.main(argv, conn, self.dir), 0)
            self.assertEqual(conn.c.updates, [])
            self.assertFalse(self.file.exists())

    def test_existing_backup_is_never_overwritten(self):
        self.file.write_text("ORIGINAL", encoding="utf-8")
        conn = Conn("Workflow 1 - Pre-Computation", False)
        self.assertNotEqual(a.main(["--commit"], conn, self.dir), 0)
        self.assertEqual(self.file.read_text(encoding="utf-8"), "ORIGINAL")
        self.assertEqual(conn.c.updates, [])

    def test_preview_writes_nothing(self):
        conn = Conn("Workflow 1 - Pre-Computation", False)
        self.assertEqual(a.main([], conn, self.dir), 0)
        self.assertEqual(conn.c.updates, [])
        self.assertFalse(self.file.exists())

    def test_normal_archive_backs_up_then_updates_once(self):
        conn = Conn("Workflow 1 - Pre-Computation", False)
        self.assertEqual(a.main(["--commit"], conn, self.dir), 0)
        self.assertIn("Workflow 1 - Pre-Computation", self.file.read_text(encoding="utf-8"))
        self.assertEqual(len(conn.c.updates), 1)
        self.assertEqual(conn.commits, 1)


if __name__ == "__main__":
    unittest.main()
