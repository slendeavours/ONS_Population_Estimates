"""Tests for w1_compare_runs.compare_runs.

Every test runs on a connection from _db.get_conn() inside a transaction that
is always rolled back. Nothing here commits. The only tables touched are
zz_w1_cmp_sig (keyed like staging_la_signals), zz_w1_cmp_nat (keyed by
run_id alone, like staging_national) and zz_w1_cmp_nokey (no primary key),
all created inside that transaction. No real table is read or written.
"""
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import w1_compare_runs  # noqa: E402
from _db import get_conn  # noqa: E402

TABLES = ("zz_w1_cmp_sig", "zz_w1_cmp_nat")


@contextmanager
def rolled_back(conn):
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_w1_cmp_sig'), "
                    "to_regclass('public.zz_w1_cmp_nat'), "
                    "to_regclass('public.zz_w1_cmp_nokey')")
        if cur.fetchone() != (None, None, None):
            raise RuntimeError("a zz_w1_cmp_* table already exists as a real "
                               "table; refusing to run")
        cur.execute("""
            CREATE TABLE public.zz_w1_cmp_sig (
                run_id     INTEGER,
                lad24cd    VARCHAR(9),
                population INTEGER,
                ta_yoy_pct NUMERIC,
                label      TEXT,
                dq         JSONB,
                created_at TIMESTAMPTZ DEFAULT clock_timestamp(),
                PRIMARY KEY (run_id, lad24cd))""")
        cur.execute("""
            CREATE TABLE public.zz_w1_cmp_nat (
                run_id  INTEGER PRIMARY KEY,
                total   INTEGER)""")
        cur.execute("""
            CREATE TABLE public.zz_w1_cmp_nokey (
                run_id  INTEGER,
                label   TEXT)""")
        for run in (1, 2):
            cur.execute("""
                INSERT INTO zz_w1_cmp_sig
                    (run_id, lad24cd, population, ta_yoy_pct, label, dq)
                VALUES (%(r)s, 'E1', 100, 1.5, 'rising', '{"v": 1}'),
                       (%(r)s, 'E2', NULL, NULL, NULL, NULL)""", {"r": run})
            cur.execute("INSERT INTO zz_w1_cmp_nat VALUES (%s, 7)", (run,))
            cur.execute("INSERT INTO zz_w1_cmp_nokey VALUES (%s, 'a'), "
                        "(%s, 'a')", (run, run))
        yield cur
    finally:
        cur.close()
        conn.rollback()


class CompareRunsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def compare(self, cur, **kw):
        return w1_compare_runs.compare_runs(cur, 1, 2, tables=TABLES, **kw)

    def test_identical_runs_report_no_differences(self):
        with rolled_back(self.conn) as cur:
            result = self.compare(cur)
        self.assertEqual(result, {t: [] for t in TABLES})

    def test_changed_cell_is_reported_with_key_and_column(self):
        with rolled_back(self.conn) as cur:
            cur.execute("UPDATE zz_w1_cmp_sig SET population = 101 "
                        "WHERE run_id = 2 AND lad24cd = 'E1'")
            result = self.compare(cur)
        diffs = result["zz_w1_cmp_sig"]
        self.assertEqual(len(diffs), 1, diffs)
        self.assertIn("lad24cd=E1", diffs[0])
        self.assertIn("population", diffs[0])
        self.assertIn("100", diffs[0])
        self.assertIn("101", diffs[0])
        self.assertEqual(result["zz_w1_cmp_nat"], [])

    def test_table_keyed_by_run_id_alone_is_compared(self):
        with rolled_back(self.conn) as cur:
            cur.execute("UPDATE zz_w1_cmp_nat SET total = 8 WHERE run_id = 2")
            changed = self.compare(cur)
            cur.execute("DELETE FROM zz_w1_cmp_nat WHERE run_id = 2")
            missing = self.compare(cur)
        self.assertEqual(len(changed["zz_w1_cmp_nat"]), 1, changed)
        self.assertIn("total: 7 -> 8", changed["zz_w1_cmp_nat"][0])
        self.assertTrue(any("only in run 1" in d
                            for d in missing["zz_w1_cmp_nat"]), missing)

    def test_both_runs_absent_is_a_difference(self):
        with rolled_back(self.conn) as cur:
            result = w1_compare_runs.compare_runs(cur, 98, 99, tables=TABLES)
        for t in TABLES:
            self.assertTrue(any("run 98 has no rows" in d
                                for d in result[t]), result)
            self.assertTrue(any("run 99 has no rows" in d
                                for d in result[t]), result)

    def test_one_run_absent_is_a_difference(self):
        with rolled_back(self.conn) as cur:
            result = w1_compare_runs.compare_runs(cur, 1, 99, tables=TABLES)
        for t in TABLES:
            self.assertTrue(any("run 99 has no rows" in d
                                for d in result[t]), result)
            self.assertFalse(any("run 1 has no rows" in d
                                 for d in result[t]), result)

    def test_two_populated_identical_runs_still_identical(self):
        with rolled_back(self.conn) as cur:
            result = self.compare(cur)
        self.assertEqual(result, {t: [] for t in TABLES})

    def test_null_versus_zero_is_a_difference(self):
        with rolled_back(self.conn) as cur:
            cur.execute("UPDATE zz_w1_cmp_sig SET ta_yoy_pct = 0 "
                        "WHERE run_id = 2 AND lad24cd = 'E2'")
            result = self.compare(cur)
        diffs = result["zz_w1_cmp_sig"]
        self.assertEqual(len(diffs), 1, diffs)
        self.assertIn("lad24cd=E2", diffs[0])
        self.assertIn("ta_yoy_pct", diffs[0])
        self.assertIn("NULL", diffs[0])

    def test_extra_row_is_reported(self):
        with rolled_back(self.conn) as cur:
            cur.execute("INSERT INTO zz_w1_cmp_sig (run_id, lad24cd) "
                        "VALUES (2, 'E3')")
            result = self.compare(cur)
        diffs = result["zz_w1_cmp_sig"]
        self.assertTrue(any("row count" in d and "2" in d and "3" in d
                            for d in diffs), diffs)
        self.assertTrue(any("lad24cd=E3" in d and "only in run 2" in d
                            for d in diffs), diffs)

    def test_ignored_columns_do_not_count(self):
        with rolled_back(self.conn) as cur:
            # created_at differs between the runs (clock_timestamp), and is
            # ignored by default. Pushing it further apart must not matter.
            cur.execute("UPDATE zz_w1_cmp_sig SET created_at = created_at "
                        "+ interval '1 day' WHERE run_id = 2")
            cur.execute("UPDATE zz_w1_cmp_sig SET label = 'falling' "
                        "WHERE run_id = 2 AND lad24cd = 'E1'")
            default = self.compare(cur)
            ignoring_label = self.compare(
                cur, ignore=frozenset({"run_id", "created_at", "label"}))
        self.assertEqual(len(default["zz_w1_cmp_sig"]), 1, default)
        self.assertIn("label", default["zz_w1_cmp_sig"][0])
        self.assertNotIn("created_at", default["zz_w1_cmp_sig"][0])
        self.assertEqual(ignoring_label["zz_w1_cmp_sig"], [])

    def test_table_without_primary_key_is_compared_as_a_multiset(self):
        with rolled_back(self.conn) as cur:
            same = w1_compare_runs.compare_runs(
                cur, 1, 2, tables=("zz_w1_cmp_nokey",))
            cur.execute("UPDATE zz_w1_cmp_nokey SET label = 'b' WHERE ctid = "
                        "(SELECT ctid FROM zz_w1_cmp_nokey WHERE run_id = 2 "
                        "LIMIT 1)")
            changed = w1_compare_runs.compare_runs(
                cur, 1, 2, tables=("zz_w1_cmp_nokey",))
        self.assertEqual(same, {"zz_w1_cmp_nokey": []})
        self.assertTrue(changed["zz_w1_cmp_nokey"])
        self.assertTrue(any("no primary key" in d
                            for d in changed["zz_w1_cmp_nokey"]))


if __name__ == "__main__":
    unittest.main()
