"""Tests that the real-table gates of s18_pipr_editions_verify fail on a short,
empty or unmigrated state. The gate bodies (real_*) take a spec, so they are
called here against the throwaway zz_s18_live / zz_s18_editions tables
(created inside a transaction that is always rolled back); no real table is
touched, and la_private_rents_editions is never created."""
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s18_pipr_editions_verify as v  # noqa: E402
from _db import get_conn  # noqa: E402

ZZ = v.ZZ
GATES = (v.real_edition1_present, v.real_latest_equals_live, v.real_coverage,
         v.real_null_vs_zero, v.real_provisional, v.real_no_nonpositive)


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
        v.apply(self.cur, v.P2, v.recs(v.P2, prov=True))
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

    def test_an_area_missing_one_block_is_caught_though_the_count_is_right(self):
        # 108 rows either way: one area lacks a block, another has a spare key
        full = v.recs(v.P1)
        v.seed_live(self.cur, full)
        odd = [r for r in full if not (r["lad24cd"] == v.CODES[0]
                                       and r["category"] == "flat_maisonette")]
        odd.append(dict(full[v.NB], category="spare_block"))
        self.assertEqual(len(odd), len(full))
        self._insert(odd, v.P1, None, "s1")
        ok, detail = v.real_coverage(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("lack a breakdown block", detail)

    def test_nonpositive_rent_fails_but_null_and_negative_change_do_not(self):
        over = {(0, "mean_rent"): None,
                (1, "annual_pct_change"): Decimal("-3.20")}
        v.apply(self.cur, v.P1, v.recs(v.P1, over=over))
        self.assertTrue(v.real_no_nonpositive(self.cur, ZZ)[0])
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET mean_rent = 0 "
                         "WHERE lad24cd = %s AND breakdown_type = 'all'",
                         (v.CODES[2],))
        ok, detail = v.real_no_nonpositive(self.cur, ZZ)
        self.assertFalse(ok, detail)

    def test_provisional_outside_the_latest_month_fails(self):
        v.apply(self.cur, v.P1, v.recs(v.P1, prov=True))
        v.apply(self.cur, v.P2, v.recs(v.P2, prov=True))
        ok, detail = v.real_provisional(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn(v.P1, detail)

    def test_null_vs_zero_differs_between_live_and_latest_edition(self):
        v.apply(self.cur, v.P1, v.recs(v.P1))
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET rent_index = NULL "
                         "WHERE lad24cd = %s AND breakdown_type = 'all'",
                         (v.CODES[0],))
        ok, _ = v.real_null_vs_zero(self.cur, ZZ)
        self.assertFalse(ok)

    def test_change_in_one_numeric_column_alone_is_revised(self):
        base = v.recs(v.P1)
        v.apply(self.cur, v.P1, base)
        for col in v.NUM_COLS:
            changed = v._with(base, 3, col, base[3 * v.NB][col] + 1)
            self.assertEqual(
                pe.classify_period(self.cur, v.prof(), v.P1, changed),
                "revised", col)
        self.assertEqual(
            pe.classify_period(self.cur, v.prof(), v.P1, base), "unchanged")

    def test_seeded_null_zero_covers_all_three_columns_both_ways(self):
        s = v.seeded_null_zero(self.cur)
        self.assertEqual(len(v.VARIANTS), 6)
        for label, _col, _area, new in v.VARIANTS:
            self.assertTrue(v.variant_ok(s[label], new), label)

    def test_seeded_provisional_flip_scenario(self):
        e = v.seeded_provisional(self.cur)
        self.assertTrue(v.provisional_ok(e), e)

    def test_pending_only_when_the_editions_table_is_absent(self):
        # the throwaway editions table exists, so a real gate run on it must
        # not report pending; the real name is never created by this script
        self.assertTrue(v.table_exists(self.cur, ZZ.editions_table))
        self.assertFalse(v.table_exists(self.cur, "zz_s18_never_made"))
        self.assertEqual(v.table_exists(self.cur), v.table_exists(
            self.cur, v.TABLE))


if __name__ == "__main__":
    unittest.main()
