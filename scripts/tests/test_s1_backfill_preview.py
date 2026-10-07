"""S1 backfill previews by default: without --commit it must not commit.

Stubs only; no database is touched.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s1_editions as s1  # noqa: E402


class Args:
    def __init__(self, commit=False, simulate=False):
        self.commit, self.simulate = commit, simulate


class BackfillPreview(unittest.TestCase):
    def run_cmd(self, args):
        conn = mock.MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (10,)
        with mock.patch("_db.get_conn", return_value=conn), \
                mock.patch.object(s1, "create_schema"), \
                mock.patch.object(s1, "backfill",
                                  return_value=[("2025Q1", 1, 5, True)]):
            s1.cmd_backfill(args)
        return conn

    def test_default_does_not_commit(self):
        conn = self.run_cmd(Args())
        conn.commit.assert_not_called()
        conn.rollback.assert_called()

    def test_simulate_does_not_commit(self):
        conn = self.run_cmd(Args(simulate=True))
        conn.commit.assert_not_called()

    def test_commit_commits(self):
        conn = self.run_cmd(Args(commit=True))
        conn.commit.assert_called_once()

    def test_parser_accepts_flags_and_rejects_both(self):
        for argv in (["backfill"], ["backfill", "--commit"]):
            with mock.patch.object(s1, "cmd_backfill") as m:
                # func is bound at parser build; patched name is looked up then
                s1.main(argv)
                self.assertEqual(m.call_count, 1)
        with self.assertRaises(SystemExit):
            s1.main(["backfill", "--commit", "--simulate"])


if __name__ == "__main__":
    unittest.main()
