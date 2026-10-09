"""Tests that the real-table gates of s22_ctb_editions_verify fail on a short,
empty, drifted or unmigrated state. The gate bodies (real_*) take a spec
triple, so they are called here against the throwaway zz_s22_* tables
(created inside a transaction that is always rolled back); no real table is
touched, and the S22 editions tables are never created."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s22_ctb_editions as m  # noqa: E402
import s22_ctb_editions_verify as v  # noqa: E402
import test_s22_ctb_loader as tl  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s22_ctb_pure import PUB_FIRST  # noqa: E402

ZZ = v.ZZ
Z_CTB, Z_CLS, Z_615 = ZZ


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
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(tl.specs())
        self.addCleanup(self.stack.close)
        self.e = v.Env(self.cur, self.tmpdir.name)
        v.make_view(self.cur, "zz_s22_rates", Z_CTB.live_table)

    def tearDown(self):
        self.cur.close()
        self.conn.rollback()

    def seed(self):
        self.e.seed_ctb(self.cur)
        self.e.seed_615(self.cur, (2023, 2024, 2025))

    def gates(self):
        return (v.real_edition1_and_latest, v.real_pair_tips,
                v.real_source_uniform, v.real_codes, v.real_null_vs_zero,
                v.real_derived,
                lambda c, s: v.real_row_counts(c, s, n=self.e.n_auth),
                lambda c, s: v.real_w1(c, s, view="zz_s22_rates"))

    def plant_edition(self, spec, period, edition, mutate):
        recs = m.records(self.cur, spec, period, edition - 1)
        mutate(recs)
        core.insert_edition(self.cur, spec, recs, period,
                            release_label="planted", published_date=None,
                            source_file="x", source_sha256="planted",
                            supersedes=edition - 1)

    # ------------------------------------------------------------- states

    def test_empty_state_fails_every_real_gate(self):
        for gate in self.gates():
            ok, detail = gate(self.cur, ZZ)
            self.assertFalse(ok, f"{gate}: {detail}")
            self.assertRegex(detail, "no |empty|differ")
        ok, detail = v.real_cross_615_ctb(self.cur, ZZ)
        self.assertFalse(ok)
        ok, detail = v.real_migration_proof(
            self.cur, ZZ, files=m.LEGACY_FILES, raw_dir=self.tmpdir.name)
        self.assertFalse(ok)
        self.assertIn("no file", detail)

    def test_complete_state_passes(self):
        self.seed()
        for gate in self.gates():
            ok, detail = gate(self.cur, ZZ)
            self.assertTrue(ok, f"{gate}: {detail}")

    def test_pending_when_the_editions_tables_do_not_exist(self):
        out = io.StringIO()
        before = len(v.RESULTS)
        with mock.patch.object(v, "none_exist", return_value=True), \
                contextlib.redirect_stdout(out):
            v.gate_2_edition1_and_latest(self.cur)
            v.gate_6_row_counts(self.cur)
        del v.RESULTS[before:]
        text = out.getvalue()
        self.assertEqual(text.count("FAIL (pending migration)"), 2)
        self.assertIn("not yet migrated", text)
        # a mixed gate whose seeded part passes is still pending; one whose
        # seeded part fails is a plain FAIL
        out = io.StringIO()
        with mock.patch.object(v, "none_exist", return_value=True), \
                contextlib.redirect_stdout(out):
            v.mixed(99, "x", True, "seeded fine", self.cur,
                    lambda c: (True, ""))
            v.mixed(98, "y", False, "seeded broke", self.cur,
                    lambda c: (True, ""))
        del v.RESULTS[before:]
        lines = out.getvalue().splitlines()
        self.assertIn("FAIL (pending migration)", lines[0])
        self.assertTrue(lines[1].startswith("GATE 98 y: FAIL  ["))

    def test_the_real_editions_tables_are_not_created(self):
        self.assertEqual(v.none_exist(self.cur),
                         not any(pe.table_exists(self.cur, s.editions_table)
                                 for s in v.REAL))
        self.assertTrue(all(s.editions_table.startswith("zz_s22_")
                            for s in ZZ))
        self.assertFalse(set(s.live_table for s in ZZ) & v.LIVE_NAMES)

    # ------------------------------------------------------------- drifts

    def test_live_differing_from_the_latest_edition_fails(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{Z_CTB.live_table} SET second_homes "
                         "= second_homes + 1 WHERE lad24cd = 'E06000001'")
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("edition-only", detail)

    def test_a_live_period_without_edition_1_fails(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{Z_615.live_table} SET year = 2030 "
                         "WHERE year = 2023")
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("no edition 1", detail)

    def test_main_and_classes_tips_that_differ_fail(self):
        self.seed()
        self.plant_edition(Z_CLS, "2025", 2,
                           lambda r: r[0].update(dwellings=999))
        ok, detail = v.real_pair_tips(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("classes tip edition 2", detail)

    def test_classes_short_of_eleven_per_authority_fail(self):
        self.seed()
        self.cur.execute(f"DELETE FROM public.{Z_CLS.live_table} WHERE "
                         "lad24cd = 'E06000001' AND exemption_class = 'K'")
        ok, detail = v.real_row_counts(self.cur, ZZ, n=self.e.n_auth)
        self.assertFalse(ok)
        self.assertIn("rows, expected", detail)
        ok, detail = v.real_row_counts(self.cur, ZZ, n=self.e.n_auth + 1)
        self.assertFalse(ok)

    def test_live_source_that_is_not_the_tips_fails(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{Z_CTB.live_table} SET "
                         "source_publication = 'other' WHERE lad24cd = "
                         "'E06000002'")
        ok, detail = v.real_source_uniform(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("not uniformly", detail)

    def test_codes_catches_new_codes_unmapped_mismatch_and_strays(self):
        self.seed()
        self.assertTrue(v.real_codes(self.cur, ZZ)[0])
        for sql, needle in (
            (f"UPDATE public.{Z_CTB.live_table} SET lad24cd = 'E08000039' "
             "WHERE lad24cd = 'E06000001'", "Barnsley/Sheffield"),
            (f"UPDATE public.{Z_615.live_table} SET lad24cd = NULL WHERE "
             "published_la_code = 'E06000001' AND year = 2023",
             "disagree"),
            (f"UPDATE public.{Z_615.live_table} SET mapping_status = "
             "'unmapped' WHERE published_la_code = 'E06000001' AND year = "
             "2023", "disagree"),
            (f"UPDATE public.{Z_615.live_table} SET lad24cd = 'E06000002' "
             "WHERE published_la_code = 'E08000038' AND year = 2025",
             "disagree"),
            (f"UPDATE public.{Z_CLS.live_table} SET lad24cd = 'E06999999' "
             "WHERE lad24cd = 'E06000001'", "not in la_boundaries")):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            ok, detail = v.real_codes(self.cur, ZZ)
            self.assertFalse(ok, sql)
            self.assertIn(needle, detail)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_a_null_without_a_reason_or_a_number_with_one_fails(self):
        self.seed()
        self.assertTrue(v.real_null_vs_zero(self.cur, ZZ)[0])
        self.plant_edition(Z_CTB, "2025", 2,
                           lambda r: r[0].update(second_homes=None,
                                                 null_reasons=None))
        ok, detail = v.real_null_vs_zero(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("second_homes is NULL without a reason", detail)

    def test_a_number_that_carries_a_reason_fails(self):
        self.seed()
        self.plant_edition(Z_CTB, "2025", 2, lambda r: r[0].update(
            null_reasons="second_homes=suppressed_or_not_available"))
        ok, detail = v.real_null_vs_zero(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("holds", detail)

    def test_an_unknown_reason_fails(self):
        self.seed()
        self.plant_edition(Z_CTB, "2025", 2, lambda r: r[0].update(
            second_homes=None, null_reasons="second_homes=because"))
        ok, detail = v.real_null_vs_zero(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("unknown null_reasons entry", detail)

    def test_derived_columns_wrong_in_live_or_the_tip_fail(self):
        self.seed()
        self.assertTrue(v.real_derived(self.cur, ZZ)[0])
        self.cur.execute("SAVEPOINT t")
        self.cur.execute(f"UPDATE public.{Z_CTB.live_table} SET "
                         "unoccupied_exemptions_total = 0 WHERE lad24cd = "
                         "'E06000001'")
        ok, detail = v.real_derived(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("live", detail)
        self.cur.execute("ROLLBACK TO SAVEPOINT t")
        self.plant_edition(Z_CTB, "2025", 2, lambda r: r[0].update(
            empty_under_6_months=r[0]["empty_under_6_months"] + 1))
        ok, detail = v.real_derived(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("2025 ed2", detail)

    def test_a_suppressed_class_makes_the_total_null_not_a_partial_sum(self):
        self.e.ctb(values={"E06000003": {"class K": "[x]"}})
        self.e.ok(self.cur, ["load", "--commit"])
        self.cur.execute(f"SELECT unoccupied_exemptions_total, null_reasons "
                         f"FROM public.{Z_CTB.live_table} WHERE lad24cd = "
                         "'E06000003'")
        self.assertEqual(self.cur.fetchone(), (
            None, "unoccupied_exemptions_total=built_from_null"))
        self.assertTrue(v.real_derived(self.cur, ZZ)[0])
        self.cur.execute(f"UPDATE public.{Z_CTB.live_table} SET "
                         "unoccupied_exemptions_total = 70 WHERE lad24cd = "
                         "'E06000003'")
        self.assertFalse(v.real_derived(self.cur, ZZ)[0])

    def test_cross_check_615_ctb_catches_a_difference(self):
        e = self.e
        vals = {("All_vacants", c, 2025): 1280 + 120 * i
                for i, c in enumerate(e.codes)}
        e.ctb()
        e.ok(self.cur, ["load", "--commit"])
        e.t615((2025,), values=vals, codes=e.codes + ["E08000016"])
        e.ok(self.cur, ["load-615", "--commit"])
        self.assertTrue(v.real_cross_615_ctb(self.cur, ZZ)[0])
        self.cur.execute(f"UPDATE public.{Z_615.live_table} SET "
                         "vacant_dwellings = vacant_dwellings + 1 WHERE "
                         "published_la_code = 'E06000002'")
        ok, detail = v.real_cross_615_ctb(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("E06000002", detail)

    # ---------------------------------------------------- shape and checks

    def test_shape_checks_catch_missing_tables_triggers_and_columns(self):
        self.assertEqual(v.shape_problems(self.cur, ZZ), [])
        self.cur.execute("SAVEPOINT t")
        self.cur.execute(f"DROP TRIGGER {Z_CLS.trigger} ON public."
                         f"{Z_CLS.editions_table}")
        self.assertTrue(any("append-only triggers wrong" in p
                            for p in v.shape_problems(self.cur, ZZ)))
        self.cur.execute("ROLLBACK TO SAVEPOINT t")
        self.cur.execute(f"ALTER TABLE public.{Z_615.live_table} DROP COLUMN "
                         "null_reasons")
        self.assertTrue(any("null_reasons" in p and "run ddl" in p
                            for p in v.shape_problems(self.cur, ZZ)))

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

    def test_w1_subqueries_are_read_from_the_w1_file(self):
        sql = v.w1_query("zz_s22_ctb_live", "zz_s22_rates")
        self.assertIn("LEFT JOIN public.zz_s22_ctb_live ctb", sql)
        self.assertIn("LEFT JOIN public.zz_s22_rates ctbr", sql)
        self.assertIn("SELECT MAX(taxbase_year)", sql)

    def test_w1_gate_catches_drift_a_rate_input_and_a_missing_authority(
            self):
        self.seed()
        self.assertTrue(v.real_w1(self.cur, ZZ, view="zz_s22_rates")[0])
        for sql in (
            f"UPDATE public.{Z_CTB.live_table} SET second_homes = "
            "second_homes + 1 WHERE lad24cd = 'E06000001'",
            f"UPDATE public.{Z_CTB.live_table} SET empty_6_months_plus = "
            "empty_6_months_plus + 5 WHERE lad24cd = 'E06000001'",
            f"DELETE FROM public.{Z_CTB.live_table} WHERE lad24cd = "
                "'E06000002'"):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            ok, detail = v.real_w1(self.cur, ZZ, view="zz_s22_rates")
            self.assertFalse(ok, sql)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")

    # ------------------------------------------------- migration gates

    def test_edition_1_hash_and_the_note(self):
        e = self.e
        files = e.seed_legacy(self.cur)
        pre = {lab: m.live_state(self.cur, ZZ[i])[:2]
               for lab, i in v.HASH_LABELS}
        with mock.patch("builtins.print"):
            rc, text, _ = e.migrate_cmd(files)
        self.assertEqual(rc, 0, text)
        for lab, i in v.HASH_LABELS:
            self.assertEqual(v.edition1_hash(self.cur, ZZ[i]), pre[lab], lab)
        note = Path(self.tmpdir.name) / "note.md"
        note.write_text("\n".join(f"before-state hash ({lab}): {pre[lab][1]}"
                                  for lab, _ in v.HASH_LABELS),
                        encoding="utf-8")
        self.assertTrue(v.real_edition1_hash(self.cur, ZZ, note, pre)[0])
        self.assertEqual(v.note_hashes(note),
                         {lab: pre[lab][1] for lab, _ in v.HASH_LABELS})
        for text, needle in (
                (note.read_text().replace(pre["ctb"][1], "0" * 32),
                 "differs from the note"),
                ("\n".join(note.read_text().splitlines()[:2]),
                 "records no")):
            note.write_text(text, encoding="utf-8")
            ok, detail = v.real_edition1_hash(self.cur, ZZ, note, pre)
            self.assertFalse(ok)
            self.assertIn(needle, detail)
        ok, detail = v.real_edition1_hash(
            self.cur, ZZ, Path(self.tmpdir.name) / "none.md", pre)
        self.assertFalse(ok)
        self.assertIn("not written", detail)
        wrong = {**pre, "615": (pre["615"][0], "0" * 32)}
        note.write_text("", encoding="utf-8")
        self.assertFalse(v.real_edition1_hash(self.cur, ZZ, note, wrong)[0])

    def test_migration_proof_reruns_from_files_found_by_sha256(self):
        e = self.e
        ctb, t615, fx = e.seed_legacy(self.cur)
        with mock.patch("builtins.print"):
            rc, text, _ = e.migrate_cmd((ctb, t615, fx))
        self.assertEqual(rc, 0, text)
        raw = Path(self.tmpdir.name) / "raw"
        raw.mkdir()
        for p in (ctb, t615):
            (raw / p.name).write_bytes(p.read_bytes())
        ok, detail = v.real_migration_proof(self.cur, ZZ, files=fx,
                                            raw_dir=raw)
        self.assertTrue(ok, detail)
        self.assertIn("0 differences", detail)
        (raw / ctb.name).write_bytes(ctb.read_bytes() + b"\0")
        ok, detail = v.real_migration_proof(self.cur, ZZ, files=fx,
                                            raw_dir=raw)
        self.assertFalse(ok)
        self.assertIn("no file", detail)

    def test_the_edition_1_proof_catches_an_edition_that_is_not_the_file(
            self):
        e = self.e
        ctb, t615, fx = e.seed_legacy(self.cur)
        # a held cell differs from the file, but the surveyed hash is the
        # planted state's: the migration's own proof stops
        self.cur.execute(f"UPDATE public.{Z_CLS.live_table} SET dwellings = "
                         "dwellings + 1 WHERE lad24cd = 'E06000001' AND "
                         "exemption_class = 'K'")
        legacy = dict(e.legacy(self.cur))
        rc, text, _ = e.migrate_cmd((ctb, t615, fx), legacy=legacy)
        self.assertEqual(rc, "halt")
        self.assertIn("proof failed", text)
        self.assertEqual(tl.count(self.cur, Z_CLS.editions_table), 0)


if __name__ == "__main__":
    unittest.main()
