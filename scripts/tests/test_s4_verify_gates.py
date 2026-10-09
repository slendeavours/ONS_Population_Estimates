"""Tests that the real-table gates of s4_care_leaver_editions_verify fail on a
short, empty, drifted or unmigrated state. The gate bodies (real_*) take a
spec, so they are called here against the throwaway zz_s4_live /
zz_s4_editions / zz_s4_editions_file_checks tables (created inside a
transaction that is always rolled back); no real table is touched, and
care_leaver_accommodation_editions is never created."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s4_care_leaver_editions as m  # noqa: E402
import s4_care_leaver_editions_verify as v  # noqa: E402
from _db import get_conn  # noqa: E402

ZZ, ZL, ZE = v.ZZ, v.ZL, v.ZE
CODES = v.CODES


def w1(cur, spec):
    return v.real_w1(cur, spec)


GATES = (v.real_edition1_and_latest, v.real_source_uniform, v.real_codes,
         v.real_one_row, v.real_null_vs_zero, v.real_built_null,
         v.real_sums_22_25, w1)


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

    def seed(self, **kw):
        self.e.seed(**kw)

    def copy_live_row(self, code, year=2024, cohort="17-21", **over):
        """Copy a live row of the year/cohort to another authority code."""
        self.cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE "
            "table_schema = 'public' AND table_name = %s AND column_name "
            "NOT IN ('lad24cd', 'loaded_at')", (ZL,))
        cols = [r[0] for r in self.cur.fetchall()]
        sel = ", ".join(f"%s AS {c}" if c in over else c for c in cols)
        self.cur.execute(
            f"INSERT INTO public.{ZL} (lad24cd, {', '.join(cols)}) SELECT %s,"
            f" {sel} FROM public.{ZL} WHERE reporting_year = %s AND "
            "age_group = %s LIMIT 1",
            [code] + [over[c] for c in cols if c in over] + [year, cohort])

    # ------------------------------------------------------------- states

    def test_empty_state_fails_every_real_gate(self):
        for gate in GATES:
            ok, detail = gate(self.cur, ZZ)
            self.assertFalse(ok, f"{gate.__name__}: {detail}")
            self.assertRegex(detail, "no |empty")

    def test_complete_state_passes(self):
        self.seed()
        for gate in GATES:
            ok, detail = gate(self.cur, ZZ)
            self.assertTrue(ok, f"{gate.__name__}: {detail}")

    def test_pending_when_the_editions_table_does_not_exist(self):
        out = io.StringIO()
        before = len(v.RESULTS)
        with mock.patch.object(v, "table_exists", return_value=False), \
                contextlib.redirect_stdout(out):
            v.gate_2_edition1_and_latest(self.cur)
            v.gate_3_source_uniform(self.cur)
        del v.RESULTS[before:]
        text = out.getvalue()
        self.assertEqual(text.count("FAIL (pending migration)"), 2)
        self.assertIn("not yet migrated", text)
        # a mixed gate whose seeded part passes is still pending
        out = io.StringIO()
        with mock.patch.object(v, "table_exists", return_value=False), \
                contextlib.redirect_stdout(out):
            v.mixed(99, "x", True, "seeded fine", self.cur, lambda c: (True,
                                                                       ""))
            v.mixed(98, "y", False, "seeded broke", self.cur,
                    lambda c: (True, ""))
        del v.RESULTS[before:]
        lines = out.getvalue().splitlines()
        self.assertIn("FAIL (pending migration)", lines[0])
        self.assertTrue(lines[1].startswith("GATE 98 y: FAIL  ["))

    # ------------------------------------------------------------- drifts

    def test_live_differing_from_the_latest_edition_fails(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{ZL} SET foyers = foyers + 1 WHERE "
                         "lad24cd = %s AND reporting_year = 2023 AND "
                         "age_group = '17-21'", (CODES[1],))
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("edition-only", detail)
        self.assertFalse(v.real_null_vs_zero(self.cur, ZZ)[0])

    def test_live_source_that_is_not_the_tips_fails(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{ZL} SET source = 'other' WHERE "
                         "reporting_year = 2022 AND lad24cd = %s",
                         (CODES[2],))
        ok, detail = v.real_source_uniform(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("not uniformly", detail)
        self.assertIn("2022", detail)

    def test_a_live_year_without_edition_1_fails(self):
        self.seed(years17=(2024,), years22=(2024,))
        self.copy_live_row(CODES[0], 2024, reporting_year=2030)
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("no edition 1", detail)

    def test_codes_catches_stray_county_barnsley_and_missing_attribution(
            self):
        self.seed()
        self.assertTrue(v.real_codes(self.cur, ZZ)[0])
        for code, needle in (("E06099999", "not in la_boundaries"),
                             ("E08000038", "Barnsley/Sheffield"),
                             ("E10000023", "county council"),
                             ("E06000028", "without attribution")):
            self.cur.execute("SAVEPOINT t")
            self.copy_live_row(code)
            ok, detail = v.real_codes(self.cur, ZZ)
            self.assertFalse(ok, code)
            self.assertIn(needle, detail, code)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_a_predecessor_with_its_attribution_passes(self):
        self.seed()
        self.copy_live_row(
            "E06000028", attribution="predecessor",
            successor_codes=["E06000058"], attribution_note="Bournemouth")
        self.assertTrue(v.real_codes(self.cur, ZZ)[0],
                        v.real_codes(self.cur, ZZ)[1])

    def test_an_all_null_row_fails_one_row(self):
        self.seed()
        self.assertTrue(v.real_one_row(self.cur, ZZ)[0])
        sets = ", ".join(f"{c} = NULL" for c in m.COUNT_COLUMNS)
        self.cur.execute(f"UPDATE public.{ZL} SET {sets} WHERE lad24cd = %s "
                         "AND reporting_year = 2024 AND age_group = '17-21'",
                         (CODES[0],))
        ok, detail = v.real_one_row(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("every count is NULL", detail)

    def test_an_all_null_row_with_reasons_is_real_data(self):
        self.seed()
        sets = ", ".join(f"{c} = NULL" for c in m.COUNT_COLUMNS)
        self.cur.execute(f"UPDATE public.{ZL} SET {sets}, null_reasons = "
                         "'total_care_leavers=suppressed' WHERE lad24cd = %s "
                         "AND reporting_year = 2024 AND age_group = '17-21'",
                         (CODES[0],))
        ok, detail = v.real_one_row(self.cur, ZZ)
        self.assertTrue(ok, detail)

    def test_null_vs_zero_catches_an_unexplained_null_and_a_written_pct(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{ZL} SET foyers = NULL WHERE "
                         "lad24cd = %s AND reporting_year = 2024 AND "
                         "age_group = '17-21'", (CODES[0],))
        ok, detail = v.real_null_vs_zero(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("foyers=NULL but null_reasons", detail)
        self.conn.rollback()
        self.setUp()
        self.seed()
        self.cur.execute(f"UPDATE public.{ZL} SET unsuitable_pct = 0.5 "
                         "WHERE lad24cd = %s AND reporting_year = 2024",
                         (CODES[0],))
        ok, detail = v.real_null_vs_zero(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("unsuitable_pct written", detail)

    def test_built_null_catches_a_total_built_from_a_null_part(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{ZL} SET independent_living = NULL "
                         "WHERE lad24cd = %s AND reporting_year = 2024 AND "
                         "age_group = '17-21'", (CODES[0],))
        ok, detail = v.real_built_null(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("total_care_leavers built from a NULL part", detail)

    def test_built_null_catches_a_semi_independent_built_from_a_null_part(
            self):
        self.seed()
        self.cur.execute(f"UPDATE public.{ZL} SET foyers = NULL WHERE "
                         "lad24cd = %s AND reporting_year = 2024 AND "
                         "age_group = '17-21'", (CODES[0],))
        ok, detail = v.real_built_null(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("semi_independent built from a NULL part", detail)

    def test_sums_22_25_needs_counties_and_a_consistent_total(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{ZL} SET total_care_leavers = "
                         "total_care_leavers + 1 WHERE reporting_year = 2024 "
                         "AND age_group = '22-25' AND lad24cd = %s",
                         (CODES[0],))
        ok, detail = v.real_sums_22_25(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("differs from the published total", detail)
        self.cur.execute(f"DELETE FROM public.{ZL} WHERE lad24cd LIKE "
                         "'E10%' AND age_group = '22-25'")
        ok, detail = v.real_sums_22_25(self.cur, ZZ)
        self.assertIn("no county council", detail)

    def test_w1_join_catches_a_drifted_live_value(self):
        self.seed()
        self.assertTrue(w1(self.cur, ZZ)[0])
        self.cur.execute(f"UPDATE public.{ZL} SET semi_independent_published = "
                         "semi_independent_published + 1 WHERE lad24cd = %s "
                         "AND reporting_year = 2024 AND age_group = '17-21'",
                         (CODES[0],))
        ok, detail = w1(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("1 differ", detail)

    def test_w1_query_is_read_from_the_w1_sql_file(self):
        q = v.w1_query("zz_s4_live")
        self.assertIn("public.zz_s4_live cl", q)
        self.assertIn("MAX(reporting_year)", q)
        with mock.patch.object(Path, "read_text", return_value="nothing"):
            with self.assertRaises(ValueError):
                v.w1_query("zz_s4_live")

    # -------------------------------------------------------------- shape

    def test_shape_is_clean_and_catches_missing_pieces(self):
        self.assertEqual(v.shape_problems(self.cur, ZZ), [])
        self.cur.execute(f"DROP TRIGGER {ZZ.trigger} ON public.{ZE}")
        self.assertTrue(any("append-only triggers wrong" in p
                            for p in v.shape_problems(self.cur, ZZ)))
        self.conn.rollback()
        self.setUp()
        self.cur.execute(f"ALTER TABLE public.{ZL} DROP COLUMN null_reasons")
        self.assertTrue(any("null_reasons" in p
                            for p in v.shape_problems(self.cur, ZZ)))
        self.conn.rollback()
        self.setUp()
        self.cur.execute(f"DROP TABLE public.{v.ZLEDGER}")
        self.assertTrue(any("does not exist" in p
                            for p in v.shape_problems(self.cur, ZZ)))

    def test_ast_check_finds_a_planted_code_but_not_a_docstring(self):
        p = Path(self.tmpdir.name) / "x.py"
        p.write_text('"""mentions E08000038 in prose"""\n'
                     'X = {"E08000038": "E08000016"}\n', encoding="utf-8")
        self.assertEqual(len(v._private_dicts(p)), 2)
        p.write_text('"""E08000038 prose"""\nX = 1\n', encoding="utf-8")
        self.assertEqual(v._private_dicts(p), [])
        self.assertEqual(
            v._private_dicts(Path(m.__file__)), [])

    # ---------------------------------------------- migration-based gates

    def test_edition1_hash_matches_the_before_state_and_the_note(self):
        files = self.e.files()
        self.e.seed_legacy(self.cur, *files)
        n, h = m.live_state(self.cur, ZZ)
        rc, text, _, _ = self.e.migrate_cmd(files)
        self.assertEqual(rc, 0, text)
        self.assertEqual(v.edition1_hash(self.cur, ZZ), (n, h))
        note = Path(self.tmpdir.name) / "note.md"
        note.write_text(f"- before-state row hash: {h}\n", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, n, h)
        self.assertTrue(ok, detail)
        note.write_text("- before-state row hash: " + "0" * 32 + "\n",
                        encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, n, h)
        self.assertFalse(ok)
        self.assertIn("differs from the note's", detail)
        note.write_text("no hash here\n", encoding="utf-8")
        self.assertIn("records no before-state hash",
                      v.real_edition1_hash(self.cur, ZZ, note, n, h)[1])
        ok, detail = v.real_edition1_hash(
            self.cur, ZZ, Path(self.tmpdir.name) / "missing.md", n, h)
        self.assertFalse(ok)
        self.assertIn("not written", detail)
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, n + 1, h)
        self.assertIn("expected", detail)

    def test_correction_proof_reruns_from_the_files_and_fails_without_them(
            self):
        files = self.e.files()
        self.e.seed_legacy(self.cur, *files)
        self.assertEqual(self.e.migrate_cmd(files)[0], 0)
        root = files[0].parent
        ok, detail = v.real_correction_proof(self.cur, ZZ, files=self.e.fx,
                                             raw_dir=root)
        self.assertTrue(ok, detail)
        self.assertIn("0 differences", detail)
        ok, detail = v.real_correction_proof(
            self.cur, ZZ, files=self.e.fx, raw_dir=root, cells_expected=1)
        self.assertFalse(ok)
        self.assertIn("proof cells, expected 1", detail)
        files[1].unlink()
        ok, detail = v.real_correction_proof(self.cur, ZZ, files=self.e.fx,
                                             raw_dir=root)
        self.assertFalse(ok)
        self.assertIn("no file", detail)

    def run_gate(self, gate):
        before, lines = len(v.RESULTS), len(v.OUTPUT)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            gate(self.cur)
        ok = v.RESULTS[before:]
        del v.RESULTS[before:]
        del v.OUTPUT[lines:]
        return ok, out.getvalue()

    def test_gate_12_retries_a_year_that_failed_part_way(self):
        ok, text = self.run_gate(v.gate_12_one_transaction)
        self.assertEqual(ok, [True], text)
        self.assertIn("retried on the rerun", text)
        # the old skip (the pair in the ledger for SOME year) fails the gate

        def old(metas, src, checked, tips, *, allow_older=False):
            seen = {}
            for y, pairs in checked.items():
                for pr in pairs:
                    seen.setdefault(pr, set()).add(y)
            return [seen.get((x["source"], x["sha"])) for x in metas]
        with mock.patch.object(m, "ledger_complete", side_effect=old):
            ok, text = self.run_gate(v.gate_12_one_transaction)
        self.assertEqual(ok, [False], text)
        self.assertIn("did not store the failed year once", text)


class Pure(unittest.TestCase):
    def test_gate_list_is_numbered_in_order(self):
        names = [g.__name__ for g in v.GATES]
        self.assertEqual([int(n.split("_")[1]) for n in names],
                         list(range(1, 24)))

    def test_planted_cell_columns_are_what_the_category_feeds(self):
        self.assertEqual(v.affected("17-21", "Foyers"),
                         {"semi_independent", "foyers", "total_care_leavers"})
        self.assertEqual(v.affected("17-21", "Total"), {"total_published"})
        self.assertEqual(
            v.affected("22-25", "Accommodation considered suitable"),
            {"suitable_count", "total_care_leavers"})

    def test_parts_of_covers_every_built_column(self):
        for cohort in ("17-21", "22-25"):
            for col in m.BUILT[cohort]:
                self.assertTrue(v.parts_of(cohort, col), (cohort, col))


if __name__ == "__main__":
    unittest.main()
