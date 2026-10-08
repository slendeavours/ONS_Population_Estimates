"""S1 refresh-latest halts when the W1 equivalence gate reports a problem.

The core refresh, the W1 snapshots and the database connection are stubbed,
so no table is read or written. The fake core refresh runs S1's after-update
hook the way editions_core.refresh_latest does, then returns a result.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s1_editions as s1  # noqa: E402

PLAN = {"2025Q1": {"edition": 2, "kind": "newer", "rows": 3,
                   "one_sided": False}}
SUMMARY = {"authorities_differing": [], "columns": {},
           "national_before": {}, "national_after": {},
           "national_columns_changed": [], "ta_changed_authorities": []}


class Args:
    def __init__(self, commit=False, simulate=False):
        self.commit, self.simulate = commit, simulate
        self.accept_drift = None


def fake_core_refresh(cur, spec, accept_drift=(), _after_update_hook=None):
    _after_update_hook(cur, PLAN)
    return {"updated": {"2025Q1": 3}, "rows": 3, "drift_accepted": []}


class W1Gate(unittest.TestCase):
    def stubs(self, w1_problems):
        return [
            mock.patch.object(s1, "_plan", return_value=(PLAN, [])),
            mock.patch.object(s1, "_unrepairable", return_value=[]),
            mock.patch.object(s1.core, "_unrepairable", return_value=[]),
            mock.patch.object(s1, "w1_snapshot", return_value={}),
            mock.patch.object(s1.core, "refresh_latest",
                              side_effect=fake_core_refresh),
            mock.patch.object(s1, "check_w1_equivalence",
                              return_value=(w1_problems, SUMMARY)),
            mock.patch.object(s1, "reproduction_update",
                              return_value=[("2025Q1", 0, True)]),
        ]

    def run_with(self, w1_problems, fn):
        patches = self.stubs(w1_problems)
        for p in patches:
            p.start()
        try:
            return fn()
        finally:
            for p in reversed(patches):
                p.stop()

    def run_cmd(self, args, w1_problems):
        conn = mock.MagicMock()

        def go():
            with mock.patch("_db.get_conn", return_value=conn), \
                    mock.patch("_db.get_readonly_conn", return_value=conn):
                s1.cmd_refresh_latest(args)
        return conn, lambda: self.run_with(w1_problems, go)

    # the function itself
    def test_refresh_latest_halts_on_w1_problem(self):
        cur = mock.MagicMock()
        with self.assertRaises(SystemExit) as cm:
            self.run_with(["E06000001: non-TA signal columns changed ['x']"],
                          lambda: s1.refresh_latest(cur))
        self.assertIn("W1:", str(cm.exception.code))
        self.assertIn("rolled back", str(cm.exception.code))

    def test_refresh_latest_passes_without_w1_problem(self):
        cur = mock.MagicMock()
        res = self.run_with([], lambda: s1.refresh_latest(cur))
        self.assertEqual(res["rows"], 3)
        self.assertEqual(res["w1"], SUMMARY)
        self.assertEqual(res["reproduction"], [("2025Q1", 0, True)])

    # the command, with --commit: a W1 problem must not commit
    def test_commit_with_w1_problem_halts_and_does_not_commit(self):
        conn, go = self.run_cmd(Args(commit=True),
                                ["national aggregates changed outside TA"])
        with self.assertRaises(SystemExit) as cm:
            go()
        self.assertIn("W1:", str(cm.exception.code))
        conn.commit.assert_not_called()
        conn.rollback.assert_called()

    def test_commit_without_w1_problem_commits(self):
        conn, go = self.run_cmd(Args(commit=True), [])
        go()
        conn.commit.assert_called_once()

    def test_simulate_with_w1_problem_halts(self):
        conn, go = self.run_cmd(Args(simulate=True), ["signals authority sets differ"])
        with self.assertRaises(SystemExit) as cm:
            go()
        self.assertIn("W1:", str(cm.exception.code))
        conn.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
