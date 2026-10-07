"""Tests for load_checks (the gates every load runs).

Database tests follow test_editions_core: a transaction that is always rolled
back, throwaway zz_core_* tables, no commit(). The two code-resolution tests
read the real la_code_lookup and la_boundaries tables read-only through
_db.get_readonly_conn().
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import load_checks as lc  # noqa: E402
from _db import get_conn, get_readonly_conn  # noqa: E402
from test_editions_core import SPEC, rolled_back  # noqa: E402


class BlankReport(unittest.TestCase):
    def test_blank_report_counts(self):
        rows = [{"a": None, "b": 0, "f": "suppressed"},
                {"a": 0, "b": 5, "f": None},
                {"a": None, "b": None, "f": "missing"},
                {"a": 3, "b": 0, "f": None}]
        rep = lc.blank_report(rows, ["a", "b"], flag_col="f")
        self.assertEqual(rep["a"], {"null": 2, "zero": 1, "flagged": 2})
        self.assertEqual(rep["b"], {"null": 1, "zero": 2, "flagged": 2})
        self.assertEqual(lc.blank_report(rows, ["a"])["a"]["flagged"], 0)

    def test_blank_regression_flags_null_to_zero_and_back(self):
        old = [{"k": "A", "v": None}, {"k": "B", "v": 0}, {"k": "C", "v": 1}]
        new = [{"k": "A", "v": 0}, {"k": "B", "v": None}, {"k": "C", "v": 1}]
        out = lc.blank_regression(new, old, ("k",), ["v"])
        self.assertEqual(len(out), 2)
        self.assertTrue(any("A" in m and "NULL" in m and "0" in m for m in out))
        self.assertTrue(any("B" in m for m in out))

    def test_blank_regression_ignores_unchanged(self):
        old = [{"k": "A", "v": None}, {"k": "B", "v": 0}, {"k": "C", "v": 4}]
        new = [{"k": "A", "v": None}, {"k": "B", "v": 0}, {"k": "C", "v": 9},
               {"k": "D", "v": 0}, {"k": "E", "v": None}]
        self.assertEqual(lc.blank_regression(new, old, ("k",), ["v"]), [])


class Identity(unittest.TestCase):
    def test_check_identity_mismatch_lists_key(self):
        out = lc.check_identity({"period": "2025Q1", "title": "X"},
                                {"period": "2025Q2", "title": "X"},
                                ("period", "title"))
        self.assertEqual(len(out), 1)
        self.assertIn("period", out[0])
        self.assertIn("2025Q1", out[0])
        self.assertIn("2025Q2", out[0])

    def test_check_identity_missing_key_is_mismatch(self):
        out = lc.check_identity({}, {"period": "2025Q2"}, ("period",))
        self.assertEqual(len(out), 1)
        self.assertIn("period", out[0])
        self.assertEqual(lc.check_identity({"p": 1}, {"p": 1}, ("p",)), [])


class Codes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_readonly_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_check_codes_unresolved_is_unexplained(self):
        with self.conn.cursor() as cur:
            out = lc.check_codes(cur, ["E99999999"])
        self.assertEqual(out, ["UNEXPLAINED E99999999"])

    def test_check_codes_resolves_old_code(self):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT l.old_code FROM public.la_code_lookup l
                           JOIN public.la_boundaries b ON b.lad24cd = l.new_code
                           WHERE l.old_code <> l.new_code
                             AND l.old_code NOT IN
                                 (SELECT lad24cd FROM public.la_boundaries)
                           ORDER BY 1 LIMIT 1""")
            old = cur.fetchone()[0]
            self.assertEqual(lc.check_codes(cur, [old]), [])
            cur.execute("SELECT lad24cd FROM public.la_boundaries LIMIT 1")
            self.assertEqual(lc.check_codes(cur, [cur.fetchone()[0]]), [])


class Coverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def _ins(self, cur, rows, sha="a", supersedes=None):
        return core.insert_edition(
            cur, SPEC, rows, "2025Q1", release_label="t",
            published_date=date(2026, 1, 1), source_file="f.ods",
            source_sha256=sha, supersedes=supersedes)

    def test_check_coverage_short_period(self):
        rows = [{"lad24cd": "E06000001", "value": 1},
                {"lad24cd": "E06000002", "value": 2}]
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, SPEC)
            ed = self._ins(cur, rows)
            self.assertEqual(lc.check_coverage(cur, SPEC, "2025Q1", ed, 2), [])
            out = lc.check_coverage(cur, SPEC, "2025Q1", ed, 3)
            self.assertEqual(len(out), 1)
            self.assertIn("2/3", out[0].replace(" ", ""))
            out = lc.check_coverage(cur, SPEC, "2025Q2", 1, 2)
            self.assertEqual(len(out), 1)

    def test_check_latest_equals_live(self):
        rows = [{"lad24cd": "E06000001", "value": 1},
                {"lad24cd": "E06000002", "value": None}]
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, SPEC)
            self._ins(cur, rows)
            for r in rows:
                cur.execute("INSERT INTO public.zz_core_live "
                            "(lad24cd, period, value) VALUES (%s,'2025Q1',%s)",
                            (r["lad24cd"], r["value"]))
            self.assertEqual(lc.check_latest_equals_live(cur, SPEC), [])
            cur.execute("UPDATE public.zz_core_live SET value = 0 "
                        "WHERE value IS NULL")
            out = lc.check_latest_equals_live(cur, SPEC)
            self.assertEqual(len(out), 1)
            self.assertIn("2025Q1", out[0])


if __name__ == "__main__":
    unittest.main()
