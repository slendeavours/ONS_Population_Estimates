"""Tests that the real-table gates of s9a_drd_editions_verify fail on a short,
empty or unmigrated state. The gate bodies (real_*) take a spec, so they are
called here against the throwaway zz_s9a_live / zz_s9a_editions /
zz_s9a_editions_file_checks tables (created inside a transaction that is
always rolled back); no real table is touched, and
nhs_drd_discharge_delays_editions is never created."""
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s9a_drd_editions as m  # noqa: E402
import s9a_drd_editions_verify as v  # noqa: E402
from _db import get_conn  # noqa: E402

ZZ = v.ZZ
VALID = set(v.CODES) | {v.SPARE}


def real_coverage(cur, spec):
    return v.real_coverage(cur, spec, valid=VALID)


GATES = (v.real_edition1_present, v.real_latest_equals_live, real_coverage,
         v.real_null_vs_zero, v.real_no_negative, v.real_total_discharges,
         v.real_on_disk)


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

    def seed_two(self):
        v.apply(self.cur, v.P1, v.recs(v.P1))
        v.apply(self.cur, v.P2, v.recs(v.P2))

    def test_empty_state_fails_every_real_gate(self):
        for gate in GATES:
            ok, detail = gate(self.cur, ZZ)
            self.assertFalse(ok, f"{gate.__name__}: {detail}")
            self.assertIn("no ", detail)

    def test_complete_state_passes(self):
        self.seed_two()
        for gate in GATES[:-1]:
            ok, detail = gate(self.cur, ZZ)
            self.assertTrue(ok, f"{gate.__name__}: {detail}")

    def test_short_edition_1_fails_even_when_edition_2_is_complete(self):
        full = v.recs(v.P1)
        v.apply(self.cur, v.P1, full)
        # plant a short edition 1 in another month and a full edition 2
        short = [dict(r, source="s") for r in v.recs(v.P2, n=v.N - 1)]
        core.insert_edition(self.cur, ZZ, short, v.P2, release_label="t",
                            published_date=None, source_file="s",
                            source_sha256="s1", supersedes=None)
        core.insert_edition(self.cur, ZZ, [dict(r, source="s")
                                           for r in v.recs(v.P2)], v.P2,
                            release_label="t", published_date=None,
                            source_file="s", source_sha256="s2",
                            supersedes=1)
        v.apply(self.cur, v.P3, v.recs(v.P3))
        ok, detail = real_coverage(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("ed1", detail)

    def test_utla_outside_the_mapping_fails(self):
        self.seed_two()
        ok, detail = v.real_coverage(self.cur, ZZ, valid=set(v.CODES[:-1]))
        self.assertFalse(ok)
        self.assertIn("not in utla_lad_mapping", detail)

    def test_negative_count_and_percentage_outside_range_fail(self):
        self.seed_two()
        self.assertTrue(v.real_no_negative(self.cur, ZZ)[0])
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET "
                         "pct_same_day_discharge = 1.5 WHERE utla_code = %s",
                         (v.CODES[0],))
        ok, detail = v.real_no_negative(self.cur, ZZ)
        self.assertFalse(ok, detail)
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET "
                         "pct_same_day_discharge = 0.5, total_bed_days_lost "
                         "= -3 WHERE utla_code = %s", (v.CODES[0],))
        self.assertFalse(v.real_no_negative(self.cur, ZZ)[0])

    def test_live_differs_from_the_latest_edition_fails(self):
        self.seed_two()
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET "
                         "total_bed_days_lost = 1 WHERE utla_code = %s",
                         (v.CODES[0],))
        ok, detail = v.real_latest_equals_live(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("edition-only", detail)

    def test_live_source_that_is_not_the_tips_file_fails(self):
        self.seed_two()
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET source = 'other' "
                         "WHERE reporting_period = %s", (v.P1,))
        ok, detail = v.real_latest_equals_live(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn(v.P1, detail)
        ok, detail = v.real_edition1_present(self.cur, ZZ)
        self.assertFalse(ok)

    def test_missing_edition_1_for_a_live_month_fails(self):
        v.apply(self.cur, v.P1, v.recs(v.P1))
        self.cur.execute(
            f"INSERT INTO public.{ZZ.live_table} (utla_code, "
            "reporting_period, utla_name, source) VALUES ('E06000001', %s, "
            "'x', 's')", (v.P2,))
        ok, detail = v.real_edition1_present(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("no edition 1", detail)

    def test_null_vs_zero_differs_between_live_and_latest_edition(self):
        v.apply(self.cur, v.P1, v.recs(v.P1))
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET "
                         "discharged_1_day = NULL WHERE utla_code = %s",
                         (v.CODES[0],))
        self.assertFalse(v.real_null_vs_zero(self.cur, ZZ)[0])

    def test_total_discharges_filled_is_listed_not_failed(self):
        v.apply(self.cur, v.P1, v.recs(v.P1, over={
            (0, "total_discharges"): 5000}))
        ok, detail = v.real_total_discharges(self.cur, ZZ)
        self.assertTrue(ok, detail)
        self.assertIn("LISTED", detail)
        self.assertIn(v.P1, detail)

    def test_change_in_any_one_value_column_is_revised(self):
        base = v.recs(v.P1)
        v.apply(self.cur, v.P1, base)
        for col in v.VALUE_COLS:
            if col == "total_discharges":
                new = 5
            elif col in m.COUNT_COLUMNS:
                new = base[3][col] + 1
            else:
                new = base[3][col] + Decimal("0.01")
            changed = v.with_cell(base, 3, col, new)
            self.assertEqual(
                pe.classify_period(self.cur, v.prof(), v.P1, changed),
                "revised", col)
        self.assertEqual(
            pe.classify_period(self.cur, v.prof(), v.P1, base), "unchanged")

    def test_seeded_null_zero_covers_every_column_both_ways(self):
        s = v.seeded_null_zero(self.cur)
        self.assertEqual(len(v.VARIANTS), 2 * len(m.VALUE_COLUMNS))
        for label, _col, _area, new in v.VARIANTS:
            self.assertTrue(v.variant_ok(s[label], new), label)

    def test_a_dash_through_the_loader_is_null_not_zero(self):
        rc, nulls, zeros = v.seeded_dash_through_loader(self.cur)
        k = len(m.VALUE_COLUMNS)
        self.assertEqual(rc, 0)
        self.assertTrue(all(n == (1,) * k for n in nulls))
        self.assertEqual(zeros[0], (0,) * k)

    def _on_disk_fixture(self, tmp, bump=0):
        e = v.Env(self.cur, tmp)
        link = v.orig(v.P1)
        path = e.make(link, v.P1)
        rows, _ = m.parse_workbook(path)
        records = m.build_records(rows, {}, v.P1)
        if bump:
            records[0]["total_bed_days_lost"] += bump
        v.apply(self.cur, v.P1, records, link=link)
        return path.parent

    def test_on_disk_file_that_is_the_tip_must_match_it_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._on_disk_fixture(tmp)
            ok, detail = v.real_on_disk(self.cur, ZZ, d)
            self.assertTrue(ok, detail)
            self.assertIn("1 on-disk", detail)

    def test_on_disk_file_that_differs_from_the_tip_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._on_disk_fixture(tmp, bump=1)
            ok, detail = v.real_on_disk(self.cur, ZZ, d)
            self.assertFalse(ok)
            self.assertIn("differs from the tip in 1 area", detail)

    def test_on_disk_with_no_matching_file_is_not_a_pass(self):
        self.seed_two()
        with tempfile.TemporaryDirectory() as tmp:
            ok, detail = v.real_on_disk(self.cur, ZZ, tmp)
            self.assertFalse(ok)
            self.assertIn("no webfile", detail)

    def test_shape_checks_catch_a_missing_ledger_and_a_foreign_key(self):
        self.assertEqual(v.shape_problems(self.cur, ZZ, v.ZLEDGER), [])
        self.assertTrue(any("does not exist" in p for p in v.shape_problems(
            self.cur, ZZ, "zz_s9a_no_such_ledger")))

    def test_pending_only_when_the_editions_table_is_absent(self):
        # the throwaway editions table exists, so a real gate run on it must
        # not report pending; the real name is never created by this script
        self.assertTrue(v.table_exists(self.cur, ZZ.editions_table))
        self.assertFalse(v.table_exists(self.cur, "zz_s9a_never_made"))
        self.assertEqual(v.table_exists(self.cur), v.table_exists(
            self.cur, v.TABLE))


if __name__ == "__main__":
    unittest.main()
