"""Tests that the real-table gates of s9b_crfd_editions_verify fail on a short,
empty or unmigrated state. The gate bodies (real_*) take a spec, so they are
called here against the throwaway zz_s9b_live / zz_s9b_editions /
zz_s9b_editions_file_checks tables (created inside a transaction that is
always rolled back); no real table is touched, and nhs_mh_crfd_editions is
never created."""
import ast
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s9b_crfd_editions as m  # noqa: E402
import s9b_crfd_editions_verify as v  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s9b_crfd_pure import write_data_file  # noqa: E402

ZZ = v.ZZ
VALID = set(v.CODES) | {v.SPARE}


def real_coverage(cur, spec):
    return v.real_coverage(cur, spec, valid=VALID, expected=v.N)


def real_on_disk(cur, spec):
    with tempfile.TemporaryDirectory() as tmp:
        return v.real_on_disk(cur, spec, tmp, recodes={})


GATES = (v.real_edition1_present, v.real_latest_equals_live, real_coverage,
         v.real_null_vs_zero, v.real_no_negative, v.real_suppression,
         real_on_disk)


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

    def test_held_area_count_must_be_the_expected_count(self):
        self.seed_two()
        ok, detail = v.real_coverage(self.cur, ZZ, valid=VALID,
                                     expected=v.N + 1)
        self.assertFalse(ok)
        self.assertIn("expected", detail)

    def test_short_edition_1_fails_even_when_edition_2_is_complete(self):
        v.apply(self.cur, v.P1, v.recs(v.P1))
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

    def test_code_outside_la_boundaries_fails(self):
        self.seed_two()
        ok, detail = v.real_coverage(self.cur, ZZ, valid=set(v.CODES[:-1]),
                                     expected=v.N)
        self.assertFalse(ok)
        self.assertIn("not in la_boundaries", detail)

    def test_negative_value_fails(self):
        self.seed_two()
        self.assertTrue(v.real_no_negative(self.cur, ZZ)[0])
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET measure_value "
                         "= -3 WHERE lad24cd = %s", (v.CODES[0],))
        ok, detail = v.real_no_negative(self.cur, ZZ)
        self.assertFalse(ok, detail)

    def test_live_differs_from_the_latest_edition_fails(self):
        self.seed_two()
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET measure_value "
                         "= 1 WHERE lad24cd = %s", (v.CODES[0],))
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

    def test_refreshed_month_must_carry_one_source_on_every_row(self):
        base = v.recs(v.P1)
        v.apply(self.cur, v.P1, base)
        v.apply(self.cur, v.P1, v.with_cell(
            base, 5, "measure_value", base[5]["measure_value"] + 1000),
            link_=v.perf(v.P1, 2))
        core.refresh_latest(self.cur, ZZ)
        ok, detail = v.real_latest_equals_live(self.cur, ZZ)
        self.assertTrue(ok, detail)
        # one row left on the older file (a source that is not uniform) fails
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET source = %s "
                         "WHERE lad24cd = %s", (v.perf(v.P1), v.CODES[0]))
        ok, detail = v.real_latest_equals_live(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("not uniformly", detail)

    def test_missing_edition_1_for_a_live_month_fails(self):
        v.apply(self.cur, v.P1, v.recs(v.P1))
        self.cur.execute(
            f"INSERT INTO public.{ZZ.live_table} (lad24cd, measure_id, "
            "reporting_period, measure_value, source) VALUES "
            "('E06000001', 'MHS26', %s, 1, 's')", (v.P2,))
        ok, detail = v.real_edition1_present(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("no edition 1", detail)

    def test_null_vs_zero_differs_between_live_and_latest_edition(self):
        v.apply(self.cur, v.P1, v.recs(v.P1))
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET measure_value "
                         "= NULL WHERE lad24cd = %s", (v.CODES[0],))
        self.assertFalse(v.real_null_vs_zero(self.cur, ZZ)[0])

    def test_suppression_listed_and_a_held_zero_fails(self):
        v.apply(self.cur, v.P1, v.recs(v.P1, over={
            (0, "measure_value"): None, (1, "measure_value"): None}))
        ok, detail = v.real_suppression(self.cur, ZZ)
        self.assertTrue(ok, detail)
        self.assertIn("2026-01 2", detail)
        v.apply(self.cur, v.P2, v.recs(v.P2, over={
            (3, "measure_value"): 0}))
        ok, detail = v.real_suppression(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("zero value(s) held", detail)

    def test_suppression_null_count_must_match_live(self):
        v.apply(self.cur, v.P1, v.recs(v.P1, over={
            (0, "measure_value"): None}))
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET measure_value "
                         "= 5 WHERE lad24cd = %s", (v.CODES[0],))
        ok, detail = v.real_suppression(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("NULL count", detail)

    def test_a_change_in_the_value_is_revised(self):
        base = v.recs(v.P1)
        v.apply(self.cur, v.P1, base)
        changed = v.with_cell(base, 3, "measure_value",
                              base[3]["measure_value"] + 1)
        self.assertEqual(pe.classify_period(self.cur, v.prof(), v.P1, changed),
                         "revised")
        self.assertEqual(pe.classify_period(self.cur, v.prof(), v.P1, base),
                         "unchanged")

    def test_seeded_null_zero_covers_both_ways(self):
        s = v.seeded_null_zero(self.cur)
        self.assertEqual(len(v.VARIANTS), 2 * len(m.VALUE_COLUMNS))
        for label, _col, _area, new in v.VARIANTS:
            self.assertTrue(v.variant_ok(s[label], new), label)

    def test_a_star_and_a_blank_through_the_loader_are_null_not_zero(self):
        rc, held = v.seeded_marker_through_loader(self.cur)
        want = [(True, None), (True, None), (False, 0)]
        self.assertEqual(rc, 0)
        self.assertEqual(held, [want, want])

    def _on_disk_fixture(self, tmp, bump=0):
        link = v.perf(v.P1)
        path = write_data_file(
            Path(tmp) / m._name(link), v.P1,
            [(c, v.base_value(i, v.P1)) for i, c in enumerate(v.CODES)])
        rows, _ = m.parse_file(path)
        records = m.build_records(rows, {}, v.P1)
        if bump:
            records[0]["measure_value"] += bump
        v.apply(self.cur, v.P1, records, link_=link)
        return path.parent

    def test_on_disk_file_that_is_the_tip_must_match_it_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._on_disk_fixture(tmp)
            ok, detail = v.real_on_disk(self.cur, ZZ, d, recodes={})
            self.assertTrue(ok, detail)
            self.assertIn("1 on-disk", detail)

    def test_on_disk_file_that_differs_from_the_tip_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._on_disk_fixture(tmp, bump=1)
            ok, detail = v.real_on_disk(self.cur, ZZ, d, recodes={})
            self.assertFalse(ok)
            self.assertIn("differs from the tip in 1 area", detail)

    def test_on_disk_with_no_matching_file_is_not_a_pass(self):
        self.seed_two()
        with tempfile.TemporaryDirectory() as tmp:
            ok, detail = v.real_on_disk(self.cur, ZZ, tmp, recodes={})
            self.assertFalse(ok)
            self.assertIn("no data file", detail)

    def test_shape_checks_catch_a_missing_ledger_and_a_missing_key(self):
        self.assertEqual(v.shape_problems(self.cur, ZZ, v.ZLEDGER), [])
        self.assertTrue(any("does not exist" in p for p in v.shape_problems(
            self.cur, ZZ, "zz_s9b_no_such_ledger")))
        # the throwaway copy has no la_boundaries key; the real table must
        self.assertTrue(any("foreign key" in p for p in v.shape_problems(
            self.cur, ZZ, v.ZLEDGER, expect_fk=True)))

    def test_pending_only_when_the_editions_table_is_absent(self):
        self.assertTrue(v.table_exists(self.cur, ZZ.editions_table))
        self.assertFalse(v.table_exists(self.cur, "zz_s9b_never_made"))
        self.assertEqual(v.table_exists(self.cur), v.table_exists(
            self.cur, v.TABLE))

    def test_private_recode_dict_check_sees_a_planted_dict(self):
        planted = ast.parse('R = {"E08000038": "E08000016"}\n'
                            '"""E08000039 in prose"""\n')
        self.assertEqual(v._literal_recode_codes(planted), {"E08000038",
                                                            "E08000016"})
        clean = ast.parse('"""E08000038 in the docstring"""\nx = 1\n')
        self.assertEqual(v._literal_recode_codes(clean), set())
        self.assertEqual(v._literal_recode_codes(
            ast.parse(Path(m.__file__).read_text(encoding="utf-8"))), set())

    def test_big_zip_helper_streams_a_large_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = v._big_zip(Path(tmp) / "b.zip", 2000, ["E06000001"])
            rows, meta = m.parse_file(f)
        self.assertEqual([r["lad24cd"] for r in rows], ["E06000001"])
        self.assertEqual(meta["mhs26_rows"], 1)


if __name__ == "__main__":
    unittest.main()
