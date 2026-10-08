"""Tests for w1_run: step order, one transaction, guards, contract gate.

The database tests run on a connection from _db.get_conn() inside a
transaction that is always rolled back (rolled_back below). Nothing here
commits. run_steps is given a wrapper round that connection whose commit() is
only counted, never sent, and whose rollback() goes back to a savepoint taken
after the throwaway tables were set up, so "rolled back" can be observed
inside the outer transaction. The only tables touched are zz_w1_runs (standing
in for staging_runs) and zz_w1_out, created inside that transaction.

The remaining tests use a mock connection and touch no database.
"""
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg2  # noqa: E402
import psycopg2.errors  # noqa: E402

import w1_run  # noqa: E402
import w1_steps  # noqa: E402
from _db import get_conn  # noqa: E402

NULL_BRANCH = "WHEN ta_cur.households_in_ta IS NULL THEN 'no_current_data'"


def real_create_run():
    """The real Create Run step, pointed at the throwaway runs table."""
    step = next(s for s in w1_steps.STEPS if s.name == "Create Run")
    sql = w1_steps.read_step(step)
    assert "staging_runs" in sql, "Create Run no longer names staging_runs"
    return sql.replace("staging_runs", "zz_w1_runs")


def out(name):
    return f"INSERT INTO zz_w1_out (run_id, name) VALUES ($1, '{name}')"


def fake_steps(fail_at=None):
    """Eight steps shaped like W1's, each recording its name in zz_w1_out.

    Steps before Create Run run raw (no run id), as the real ones do.
    fail_at is a 1-based position whose SQL is replaced by a division by zero.
    """
    steps = [
        ("Create Staging Tables",
         "INSERT INTO zz_w1_out (name) VALUES ('Create Staging Tables')"),
        ("Signal Column Pre-flight",
         "INSERT INTO zz_w1_out (name) VALUES ('Signal Column Pre-flight')"),
        ("Create Run", real_create_run()),
        ("National Aggregates", out("National Aggregates")),
        ("LA Signals", out("LA Signals")),
        ("Tenant Type Rankings", out("Tenant Type Rankings")),
        ("Section 3 Top 3 LAs", out("Section 3 Top 3 LAs")),
        ("Mark Run Complete",
         "UPDATE zz_w1_runs SET status = 'complete' WHERE run_id = $1; "
         + out("Mark Run Complete")),
    ]
    if fail_at is not None:
        name, _ = steps[fail_at - 1]
        steps[fail_at - 1] = (name, "SELECT 1/0")
    return steps


class SavepointConn:
    """A real connection whose commit is counted and never sent.

    rollback() returns to the savepoint taken after set-up, so the test can
    look at the tables after run_steps has rolled back. The outer transaction
    is still rolled back by rolled_back().
    """

    def __init__(self, conn):
        self._conn = conn
        self.commits = 0
        self.rollbacks = 0
        self.notices = conn.notices

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        with self._conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT w1_test")


@contextmanager
def rolled_back(conn, seed_completed_run_today=False):
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_w1_runs'), "
                    "to_regclass('public.zz_w1_out')")
        if cur.fetchone() != (None, None):
            raise RuntimeError("zz_w1_runs or zz_w1_out already exists as a "
                               "real table; refusing to run")
        cur.execute("""
            CREATE TABLE public.zz_w1_runs (
                run_id   SERIAL PRIMARY KEY,
                run_date TIMESTAMPTZ DEFAULT NOW(),
                status   TEXT DEFAULT 'in_progress',
                notes    TEXT)""")
        cur.execute("CREATE TABLE public.zz_w1_out (seq SERIAL PRIMARY KEY, "
                    "run_id INTEGER, name TEXT NOT NULL)")
        if seed_completed_run_today:
            cur.execute("INSERT INTO zz_w1_runs (status, notes) "
                        "VALUES ('complete', 'earlier run today')")
        cur.execute("SAVEPOINT w1_test")
        yield cur, SavepointConn(conn)
    finally:
        cur.close()
        conn.rollback()


def snapshot(cur):
    cur.execute("SELECT run_id, status FROM zz_w1_runs ORDER BY run_id")
    runs = cur.fetchall()
    cur.execute("SELECT seq, run_id, name FROM zz_w1_out ORDER BY seq")
    return runs, cur.fetchall()


class RunStepsDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_steps_run_in_order(self):
        steps = fake_steps()
        with rolled_back(self.conn) as (cur, wrapped):
            run_id = w1_run.run_steps(wrapped, steps)
            cur.execute("SELECT run_id, name FROM zz_w1_out ORDER BY seq")
            rows = cur.fetchall()
            self.assertEqual([n for _, n in rows],
                             [n for n, _ in steps if n != "Create Run"])
            self.assertEqual({r for r, _ in rows[2:]}, {run_id})
            self.assertEqual([r for r, _ in rows[:2]], [None, None])
            cur.execute("SELECT status FROM zz_w1_runs WHERE run_id = %s",
                        (run_id,))
            self.assertEqual(cur.fetchone(), ("complete",))
            self.assertEqual((wrapped.commits, wrapped.rollbacks), (1, 0))

    def test_failing_step_rolls_back_everything(self):
        with rolled_back(self.conn) as (cur, wrapped):
            before = snapshot(cur)
            # Step 4 (the first after Create Run) fails, so a runs row and two
            # output rows exist at the moment of failure.
            with self.assertRaises(psycopg2.errors.DivisionByZero):
                w1_run.run_steps(wrapped, fake_steps(fail_at=4))
            self.assertEqual(snapshot(cur), before)
            self.assertEqual((wrapped.commits, wrapped.rollbacks), (0, 1))
        with rolled_back(self.conn) as (cur, wrapped):
            before = snapshot(cur)
            with self.assertRaises(psycopg2.errors.DivisionByZero):
                w1_run.run_steps(wrapped, fake_steps(fail_at=3))
            self.assertEqual(snapshot(cur), before)
            self.assertEqual(wrapped.commits, 0)

    def test_second_completed_run_same_day_refused(self):
        with rolled_back(self.conn, seed_completed_run_today=True) as (
                cur, wrapped):
            before = snapshot(cur)
            self.assertEqual(len(before[0]), 1)
            with self.assertRaises(psycopg2.errors.RaiseException) as cm:
                w1_run.run_steps(wrapped, fake_steps())
            self.assertIn("A completed run already exists for today",
                          str(cm.exception))
            self.assertEqual(snapshot(cur), before)
            self.assertEqual((wrapped.commits, wrapped.rollbacks), (0, 1))


def mock_conn(events=None):
    conn = mock.MagicMock()
    cur = conn.cursor.return_value
    cur.fetchone.return_value = (7,)
    cur.rowcount = 1
    if events is not None:
        cur.execute.side_effect = lambda *a, **k: events.append("execute")
        conn.commit.side_effect = lambda: events.append("commit")
        conn.rollback.side_effect = lambda: events.append("rollback")
    return conn


class RunStepsMock(unittest.TestCase):
    def test_simulate_never_commits(self):
        conn = mock_conn()
        self.assertEqual(w1_run.run_steps(conn, fake_steps(), simulate=True), 7)
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()

    def test_commit_only_after_all_steps(self):
        events = []
        conn = mock_conn(events)
        w1_run.run_steps(conn, fake_steps())
        self.assertEqual(events, ["execute"] * 8 + ["commit"])

    def test_run_id_steps_are_parameterised_and_early_steps_raw(self):
        conn = mock_conn()
        steps = [("Pre", "DO $$ BEGIN RAISE NOTICE '%', 1; END $$"),
                 ("Create Run", "SELECT 7"),
                 ("Mark Run Complete", "UPDATE t SET x = '5%' WHERE id = $1")]
        w1_run.run_steps(conn, steps)
        calls = conn.cursor.return_value.execute.call_args_list
        self.assertEqual(calls[0], mock.call(steps[0][1]))
        self.assertEqual(calls[1], mock.call("SELECT 7"))
        self.assertEqual(calls[2], mock.call(
            "UPDATE t SET x = '5%%' WHERE id = %(run_id)s", {"run_id": 7}))

    def test_mark_run_complete_must_be_last(self):
        conn = mock_conn()
        steps = fake_steps()
        steps[-1], steps[-2] = steps[-2], steps[-1]
        with self.assertRaises(ValueError):
            w1_run.run_steps(conn, steps)
        conn.cursor.return_value.execute.assert_not_called()
        conn.commit.assert_not_called()


class Guards(unittest.TestCase):
    def test_check_guards_flags_missing_null_branch(self):
        problems = w1_run.check_guards(
            {"LA Signals": "CASE WHEN x > 1 THEN 'rising' END"})
        self.assertEqual(len(problems), 1)
        self.assertIn("NULL-first", problems[0])

    def test_check_guards_flags_catch_all_else(self):
        problems = w1_run.check_guards(
            {"LA Signals": f"CASE {NULL_BRANCH} ELSE 'falling_strongly' END"})
        self.assertEqual(len(problems), 1)
        self.assertIn("falling_strongly", problems[0])

    def test_check_guards_flags_missing_la_signals(self):
        self.assertTrue(w1_run.check_guards({}))

    def test_guards_pass_on_the_real_la_signals_file(self):
        step = next(s for s in w1_steps.STEPS if s.name == "LA Signals")
        self.assertEqual(step.filename, "05_la_signals.sql")
        self.assertEqual(
            w1_run.check_guards({"LA Signals": w1_steps.read_step(step)}), [])


class Main(unittest.TestCase):
    def test_contract_failure_aborts_before_create_run(self):
        with mock.patch.object(w1_run.w1_contract_check, "check",
                               return_value=(["a column diverges"], [], [])), \
                mock.patch.object(w1_run, "run_steps") as run_steps, \
                mock.patch.object(w1_run, "get_conn") as get_conn_, \
                mock.patch.object(w1_run, "_contract_snapshot",
                                  return_value=None):
            rc = w1_run.main([])
        self.assertNotEqual(rc, 0)
        run_steps.assert_not_called()
        get_conn_.assert_not_called()

    def test_dry_run_and_simulate_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as cm, \
                mock.patch("sys.stderr"):
            w1_run.main(["--dry-run", "--simulate"])
        self.assertNotEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
