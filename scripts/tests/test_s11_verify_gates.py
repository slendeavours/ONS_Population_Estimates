"""Tests that the real-table gates of s11_cqc_editions_verify fail on a short,
empty, drifted or unmigrated state. The gate bodies (real_*) take a spec, so
they are called here against the throwaway zz_s11_live / zz_s11_editions /
zz_s11_editions_file_checks / zz_s11_live_unresolved tables (created inside a
transaction that is always rolled back); no real table is touched, and
cqc_location_snapshots_editions is never created."""
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import period_editions as pe  # noqa: E402
import s11_cqc_editions as m  # noqa: E402
import s11_cqc_editions_verify as v  # noqa: E402
from _db import get_conn  # noqa: E402

ZZ = v.ZZ
VALID = set(v.AUTH)


def real_coverage(cur, spec, **kw):
    return v.real_coverage(cur, spec, valid=kw.get("valid", VALID),
                           want_n=kw.get("want_n", 3))


GATES = (v.real_edition1_and_latest, v.real_source_uniform, real_coverage,
         v.real_one_row, v.real_null_vs_zero, v.real_markers,
         v.real_unresolved_absent, v.real_on_disk)


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
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.e = v.Env(self.cur, self.tmpdir.name)

    def tearDown(self):
        self.cur.close()
        self.conn.rollback()

    def seed(self, dates=(v.D1, v.D2)):
        self.e.seed(dates)

    def copy_live_row(self, location_id, snapshot=v.D2, extra_set=""):
        self.cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE "
            "table_schema = 'public' AND table_name = %s AND column_name "
            "NOT IN ('location_id', 'loaded_at')", (v.ZL,))
        cols = ", ".join(r[0] for r in self.cur.fetchall())
        self.cur.execute(f"INSERT INTO public.{v.ZL} (location_id, {cols}) "
                         f"SELECT %s, {cols} FROM public.{v.ZL} WHERE "
                         "snapshot_date = %s LIMIT 1", (location_id, snapshot))

    def test_empty_state_fails_every_real_gate(self):
        for gate in GATES:
            ok, detail = gate(self.cur, ZZ)
            self.assertFalse(ok, f"{gate.__name__}: {detail}")
            self.assertIn("no ", detail)

    def test_complete_state_passes(self):
        self.seed()
        for gate in GATES[:-1]:
            ok, detail = gate(self.cur, ZZ)
            self.assertTrue(ok, f"{gate.__name__}: {detail}")

    def test_live_differing_from_the_latest_edition_fails(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{v.ZL} SET latest_overall_rating = "
                         "'Outstanding' WHERE location_id = '1-1' AND "
                         "snapshot_date = %s", (v.D1,))
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("edition-only", detail)
        self.assertFalse(v.real_null_vs_zero(self.cur, ZZ)[0])

    def test_live_source_that_is_not_the_tips_fails(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{v.ZL} SET source_file = 'other' "
                         "WHERE snapshot_date = %s AND location_id = '1-2'",
                         (v.D1,))
        ok, detail = v.real_source_uniform(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("not uniformly", detail)
        self.assertIn(v.D1, detail)

    def test_a_live_snapshot_without_edition_1_fails(self):
        self.seed((v.D1,))
        self.copy_live_row("1-NEW", v.D1)
        self.cur.execute(f"UPDATE public.{v.ZL} SET snapshot_date = %s WHERE "
                         "location_id = '1-NEW'", (v.D3,))
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("no edition 1", detail)

    def test_coverage_catches_a_stray_code_and_a_short_snapshot(self):
        self.seed()
        ok, detail = v.real_coverage(self.cur, ZZ, valid={v.A, v.B},
                                     want_n=3)
        self.assertFalse(ok)
        self.assertIn("not in la_boundaries", detail)
        ok, detail = v.real_coverage(self.cur, ZZ, valid=VALID, want_n=4)
        self.assertFalse(ok)
        self.assertIn("3 authorities, expected 4", detail)

    def test_two_rows_cannot_share_a_location_and_a_blank_id_fails(self):
        self.seed()
        self.assertTrue(v.real_one_row(self.cur, ZZ)[0])
        self.copy_live_row(" ")
        ok, detail = v.real_one_row(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("blank location_id", detail)

    def test_a_stored_brand_dash_or_unknown_method_fails(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{v.ZL} SET brand_name = '-' WHERE "
                         "location_id = '1-3' AND snapshot_date = %s",
                         (v.D1,))
        ok, detail = v.real_markers(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("brand '-'", detail)
        self.cur.execute(f"UPDATE public.{v.ZL} SET brand_name = NULL, "
                         "mapping_method = 'guess' WHERE location_id = "
                         "'1-3' AND snapshot_date = %s", (v.D1,))
        ok, detail = v.real_markers(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("unknown mapping_method", detail)

    def test_null_versus_zero_differs_between_live_and_latest_edition(self):
        self.seed()
        self.assertTrue(v.real_null_vs_zero(self.cur, ZZ)[0])
        self.cur.execute(f"UPDATE public.{v.ZL} SET care_homes_beds = NULL "
                         "WHERE location_id = '1-4' AND snapshot_date = %s",
                         (v.D2,))
        ok, detail = v.real_null_vs_zero(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn(v.D2, detail)

    def test_unresolved_location_found_in_live_fails(self):
        self.seed()
        self.assertTrue(v.real_unresolved_absent(self.cur, ZZ)[0])
        self.copy_live_row(v.NOXY, v.D2)
        ok, detail = v.real_unresolved_absent(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("in live", detail)

    def _on_disk_fixture(self, mutate=False):
        e = self.e
        path = e.make(v.D1)
        rows, _ = m.parse_locations(path)
        mapped, unres = m.map_locations(rows, v.BOUNDS, {}, {}, api=v.Api())
        recs = [{**r, "snapshot_date": v.D1, "source_file": v.url(v.D1)}
                for r in mapped]
        if mutate:
            recs[0]["provider_name"] = "Other Ltd"
        info = m.FileInfo(v.url(v.D1), path, m.content_sha256(path), "t")
        m.apply_snapshot(self.cur, m._profile(ZZ), v.D1, recs,
                         fetched_on=v.FETCHED, info=info, unresolved=unres)
        return path.parent

    def test_on_disk_file_that_is_the_tip_must_match_it_exactly(self):
        ok, detail = v.real_on_disk(self.cur, ZZ, self._on_disk_fixture())
        self.assertTrue(ok, detail)
        self.assertIn("1 on-disk", detail)

    def test_on_disk_file_that_differs_from_the_tip_fails(self):
        ok, detail = v.real_on_disk(self.cur, ZZ,
                                    self._on_disk_fixture(mutate=True))
        self.assertFalse(ok)
        self.assertIn("differs from the tip in 1 location", detail)

    def test_on_disk_with_no_matching_file_is_not_a_pass(self):
        self.seed((v.D1,))
        with tempfile.TemporaryDirectory() as tmp:
            ok, detail = v.real_on_disk(self.cur, ZZ, tmp)
        self.assertFalse(ok)
        self.assertIn("empty comparison", detail)

    def test_shape_checks_catch_missing_tables_and_triggers(self):
        self.assertEqual(v.shape_problems(self.cur, ZZ), [])
        gone = dataclasses.replace(ZZ, live_table="zz_s11_no_such_live")
        self.assertTrue(any("does not exist" in p
                            for p in v.shape_problems(self.cur, gone)))
        self.cur.execute(f"DROP TRIGGER zz_s11_unresolved_immutable ON "
                         f"public.{v.ZUNRES}")
        self.assertTrue(any("append-only triggers wrong" in p
                            for p in v.shape_problems(self.cur, ZZ)))

    def test_the_real_tables_are_not_created_so_real_gates_are_pending(self):
        self.assertTrue(v.table_exists(self.cur, ZZ.editions_table))
        self.assertFalse(v.table_exists(self.cur, "zz_s11_never_made"))
        self.assertEqual(v.table_exists(self.cur),
                         pe.table_exists(self.cur, v.TABLE))
        with mock.patch.object(v, "RESULTS", []) as res, \
                mock.patch.object(v, "OUTPUT", []) as out, \
                mock.patch.object(v, "table_exists", return_value=False), \
                mock.patch("builtins.print"):
            v.gate_2_edition1_and_latest(self.cur)
            v.gate_21_on_disk(self.cur)
        self.assertEqual(res, [False, False])
        self.assertTrue(all("FAIL (pending migration)" in x
                            and "not yet migrated" in x for x in out), out)

    def test_private_dict_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.py"
            p.write_text('"""Mentions E08000038 in a docstring."""\n'
                         'X = {"E08000038": "E08000016"}\n',
                         encoding="utf-8")
            self.assertEqual(len(v._private_dicts(p)), 2)
            p.write_text('"""Mentions E08000038 in a docstring."""\n'
                         'import re\nR = re.compile(r"E080000(16|19)")\n',
                         encoding="utf-8")
            self.assertEqual(v._private_dicts(p), [])
        self.assertEqual(v._private_dicts(Path(m.__file__)), [])

    def test_w1_subquery_is_read_from_the_w1_file(self):
        sql = v.w1_subquery("zz_s11_legacy")
        self.assertIn("FROM public.zz_s11_legacy", sql)
        self.assertIn("supported_living AND is_active AND NOT dormant", sql)

    def test_the_view_gate_passes_after_a_seeded_migration_and_fails_on_drift(
            self):
        e = self.e
        files, runs, _ = v.seed_legacy(self.cur, e)
        rc, text, _, _ = v.migrate(e, files, runs)
        self.assertEqual(rc, 0, text)
        ok, detail = v.real_view(self.cur, v.ZLEG, v.ZREN, ZZ)
        self.assertTrue(ok, detail)
        self.assertIn("equals", detail)
        # a snapshot newer than the migration: the is_active branch
        e.make(v.D3, **v.D3_RETURN)
        rc, text, _, _ = e.run(["load", "--commit"], page=v.url(v.D3))
        self.assertEqual(rc, 0, text)
        ok, detail = v.real_view(self.cur, v.ZLEG, v.ZREN, ZZ)
        self.assertTrue(ok, detail)
        self.assertIn("is_active count", detail)
        # drift while the migrated state is current is caught
        self.cur.execute(f"DELETE FROM public.{v.ZL} WHERE snapshot_date = "
                         "%s", (v.D3,))
        self.cur.execute(f"UPDATE public.{v.ZREN} SET provider_name = 'x' "
                         "WHERE location_id = '1-5'")
        ok, detail = v.real_view(self.cur, v.ZLEG, v.ZREN, ZZ)
        self.assertFalse(ok)
        self.assertIn("only in", detail)

    def test_the_view_gate_is_not_met_while_cqc_locations_is_a_table(self):
        with mock.patch.object(m, "relkind", return_value="r"):
            ok, detail = v.real_view(self.cur)
        self.assertFalse(ok)
        self.assertIn("not yet migrated", detail)


if __name__ == "__main__":
    unittest.main()
