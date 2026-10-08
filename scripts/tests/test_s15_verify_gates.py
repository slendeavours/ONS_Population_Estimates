"""Tests that the real-table gates of s15_hpi_editions_verify fail on a short,
empty or unmigrated state. The gate bodies (real_*) take a spec, so they are
called here against the throwaway zz_s15_live / zz_s15_editions tables
(created inside a transaction that is always rolled back); no real table is
touched."""
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s15_hpi_editions_verify as v  # noqa: E402
from _db import get_conn  # noqa: E402

ZZ = v.ZZ
GATES = (v.real_edition1_present, v.real_latest_equals_live, v.real_coverage,
         v.real_null_vs_zero, v.real_suppressed_listing,
         v.real_no_nonpositive)


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

    def _insert(self, records, period, supersedes, sha):
        return core.insert_edition(
            self.cur, ZZ, records, period, release_label="t",
            published_date=date(2026, 10, 8), source_file="t",
            source_sha256=sha, supersedes=supersedes)

    def test_empty_state_fails_every_real_gate(self):
        for gate in GATES:
            ok, detail = gate(self.cur, ZZ)
            self.assertFalse(ok, f"{gate.__name__}: {detail}")
            self.assertIn("no ", detail)

    def test_complete_state_passes(self):
        v.apply(self.cur, v.P1, v.recs(v.P1))
        for gate in GATES:
            ok, detail = gate(self.cur, ZZ)
            self.assertTrue(ok, f"{gate.__name__}: {detail}")

    def test_short_edition_1_fails_even_when_edition_2_is_complete(self):
        full = v.recs(v.P1)
        v.seed_live(self.cur, full)
        self._insert(v.recs(v.P1, n=v.N - 1), v.P1, None, "s1")
        self._insert(full, v.P1, 1, "s2")
        ok, detail = v.real_coverage(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("ed1", detail)
        self.assertNotIn("ed2", detail)

    def test_short_middle_edition_is_caught(self):
        full = v.recs(v.P1)
        v.seed_live(self.cur, full)
        self._insert(full, v.P1, None, "s1")
        self._insert(v.recs(v.P1, n=v.N - 1), v.P1, 1, "s2")
        self._insert(full, v.P1, 2, "s3")
        ok, detail = v.real_coverage(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("ed2", detail)

    def test_nonpositive_price_fails_but_null_and_negative_change_do_not(self):
        over = {(0, "avg_price_flat"): None,
                (1, "annual_change_pct"): Decimal("-3.20")}
        v.apply(self.cur, v.P1, v.recs(v.P1, over=over))
        self.assertTrue(v.real_no_nonpositive(self.cur, ZZ)[0])
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET avg_price_semi = 0"
                         " WHERE lad24cd = %s", (v.CODES[2],))
        ok, detail = v.real_no_nonpositive(self.cur, ZZ)
        self.assertFalse(ok, detail)

    def test_suppressed_property_type_cells_are_listed_not_failed(self):
        over = {(0, "avg_price_flat"): None, (1, "avg_price_flat"): None,
                (2, "avg_price_detached"): None}
        v.apply(self.cur, v.P1, v.recs(v.P1, over=over))
        ok, detail = v.real_suppressed_listing(self.cur, ZZ)
        self.assertTrue(ok, detail)
        self.assertIn("avg_price_flat 2 suppressed cells", detail)
        self.assertIn("avg_price_detached 1 suppressed cells", detail)

    def test_change_in_one_property_type_column_alone_is_revised(self):
        v.apply(self.cur, v.P1, v.recs(v.P1))
        base = v.recs(v.P1)
        for col in v.PT_COLS:
            changed = [dict(r, **{col: r[col] + 1})
                       if r["lad24cd"] == v.CODES[3] else r for r in base]
            self.assertEqual(
                pe.classify_period(self.cur, v.prof(), v.P1, changed),
                "revised", col)
        self.assertEqual(
            pe.classify_period(self.cur, v.prof(), v.P1, base), "unchanged")

    def test_null_to_zero_in_a_property_type_column_is_revised(self):
        over = {(0, "avg_price_terraced"): None}
        base = v.recs(v.P1, over=over)
        v.apply(self.cur, v.P1, base)
        zero = [dict(r, avg_price_terraced=Decimal("0.00"))
                if r["lad24cd"] == v.CODES[0] else r for r in base]
        self.assertEqual(pe.classify_period(self.cur, v.prof(), v.P1, zero),
                         "revised")

    def test_seeded_null_zero_covers_all_seven_columns_both_ways(self):
        s = v.seeded_null_zero(self.cur)
        self.assertEqual(len(v.VARIANTS), 14)
        for label, _col, _area, new in v.VARIANTS:
            self.assertTrue(v.variant_ok(s[label], new), label)

    def test_property_type_only_scenario(self):
        e = v.seeded_property_type_only(self.cur)
        self.assertTrue(v.property_type_only_ok(e), e)

    def test_pending_only_when_the_editions_table_is_absent(self):
        # the throwaway editions table exists, so a real gate run on it must
        # not report pending; on the real name (absent) it reports pending
        self.assertTrue(v.table_exists(self.cur, ZZ.editions_table))
        self.assertEqual(v.table_exists(self.cur), v.table_exists(
            self.cur, v.TABLE))


if __name__ == "__main__":
    unittest.main()
