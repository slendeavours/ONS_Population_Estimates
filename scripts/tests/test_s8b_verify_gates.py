"""Tests that the real-table gates of s8b_hb_editions_verify fail on a short
or empty state. The gate bodies (real_*) take a spec, so they are called here
against the throwaway zz_s8b_live / zz_s8b_editions tables (created inside a
transaction that is always rolled back); no real table is touched."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import s8b_hb_editions as m  # noqa: E402
import s8b_hb_editions_verify as v  # noqa: E402
from _db import get_conn  # noqa: E402

ZZ = v.ZZ
GATES = (v.real_latest_equals_live, v.real_coverage, v.real_null_vs_zero,
         v.real_zero_listing)


class VerifyGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def setUp(self):
        self.cur = self.conn.cursor()
        v.setup_throwaway(self.cur)

    def tearDown(self):
        self.cur.close()
        self.conn.rollback()

    def _insert(self, records, month, supersedes, sha):
        return core.insert_edition(
            self.cur, ZZ, records, month, release_label="t",
            published_date=date(2026, 10, 8), source_file="t",
            source_sha256=sha, supersedes=supersedes)

    def test_empty_state_fails_gates_3_4_6_7_and_2(self):
        for gate in GATES + (v.real_edition1_present,):
            ok, detail = gate(self.cur, ZZ)
            self.assertFalse(ok, f"{gate.__name__}: {detail}")
            self.assertIn("no ", detail)

    def test_complete_state_passes(self):
        full = v.recs("202601")
        v.seed_live(self.cur, full)
        m.apply_month(self.cur, ZZ, "202601", full, fetched_on=v.FETCHED)
        for gate in GATES + (v.real_edition1_present, v.real_no_negatives):
            ok, detail = gate(self.cur, ZZ)
            self.assertTrue(ok, f"{gate.__name__}: {detail}")

    def test_short_edition_1_fails_even_when_edition_2_is_complete(self):
        full = v.recs("202601")
        short = v.recs("202601", n=m.EXPECTED_AREAS - 1)
        v.seed_live(self.cur, full)
        self._insert(short, "202601", None, "s1")
        self._insert(full, "202601", 1, "s2")
        ok, detail = v.real_coverage(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("ed1", detail)
        self.assertNotIn("ed2", detail)

    def test_short_middle_edition_is_caught(self):
        full = v.recs("202601")
        v.seed_live(self.cur, full)
        self._insert(full, "202601", None, "s1")
        # a second edition one row short, with the tip complete
        self._insert(v.recs("202601", n=m.EXPECTED_AREAS - 1), "202601", 1, "s2")
        self._insert(full, "202601", 2, "s3")
        ok, detail = v.real_coverage(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("ed2", detail)


if __name__ == "__main__":
    unittest.main()
