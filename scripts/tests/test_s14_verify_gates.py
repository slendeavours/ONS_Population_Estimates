"""Tests that the real-table gates of s14_lha_editions_verify fail on a short,
empty, drifted or unmigrated state and pass on a seeded complete one. The gate
bodies (real_*) take specs, so they are called here against the throwaway
zz_s14_* tables (created inside a transaction that is always rolled back); no
real table is touched, and the S14 editions tables are never created. Every
test ends in a rollback (also on an exception), and tearDownClass reads the
database on a separate read-only connection and fails if a zz_s14 table, view
or function was left committed. No retired script is imported or run (the AST
check of the verify script is itself tested here, with planted probes)."""
import ast
import contextlib
import dataclasses
import io
import json
import re
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import s14_lha_editions as m  # noqa: E402
import s14_lha_editions_verify as v  # noqa: E402
import test_s14_lha_loader as tl  # noqa: E402
from _db import get_conn  # noqa: E402

ZR, ZM = v.ZR, v.ZM
FY, N, NA = tl.FY, tl.N, tl.NA
HERE = Path(__file__).resolve()


class VerifyGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.rollback()
            cls.conn.close()
        finally:
            left = [t for t in v.leftovers() if t.startswith("zz_s14")]
            if left:
                raise AssertionError(f"committed throwaway objects: {left}")

    def setUp(self):
        self.raw = self.conn.cursor()
        self.cur = v.SafeCur(self.raw)
        self.addCleanup(self.rollback)
        v.setup_throwaway(self.cur)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.e = v.Env(self.cur, self.tmpdir.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(self.e.specs())
        self.addCleanup(self.stack.close)

    def rollback(self):
        self.raw.close()
        self.conn.rollback()

    def migrated(self):
        """A seeded legacy state, migrated: (path, legacy)."""
        path, legacy = self.e.seed_legacy(self.cur)
        with v.patched_legacy(legacy, path):
            rc, text, _ = self.e.cmd(["migrate-legacy", "--commit"])
        self.assertEqual(rc, 0, text)
        return path, legacy

    def test_a_cursor_cannot_commit_or_roll_back_the_transaction(self):
        with self.assertRaises(RuntimeError):
            self.cur.connection.commit()
        with self.assertRaises(RuntimeError):
            self.cur.connection.rollback()

    def test_a_seeded_complete_state_passes_the_real_gates(self):
        self.e.seed(self.cur)
        path = self.e.local()
        sha = m.content_sha256(path)
        for fn in (
                lambda: v.real_edition1_and_latest(self.cur, ZR, ZM),
                lambda: v.real_brma_sets(self.cur, ZR, ZM, N),
                lambda: v.real_rule_1(self.cur, ZR, path.parent, sha),
                lambda: v.real_weekly(self.cur, ZR, path.parent, sha),
                lambda: v.real_w1_join(self.cur, ZR, ZM, ZM.live_table, NA),
                lambda: v.real_mapping(self.cur, ZR, ZM, self.e.gml, NA,
                                       False)):
            ok, detail = fn()
            self.assertTrue(ok, detail)
        self.assertEqual(v.shape_problems(self.cur, ZR), [])
        self.assertEqual(v.shape_problems(self.cur, ZM), [])

    def test_an_empty_state_is_not_a_pass(self):
        ok, detail = v.real_edition1_and_latest(self.cur, ZR, ZM)
        self.assertFalse(ok)
        self.assertIn("no live periods", detail)
        ok, detail = v.real_brma_sets(self.cur, ZR, ZM, N)
        self.assertFalse(ok)

    def test_an_unmigrated_state_fails_with_a_clear_message(self):
        gr = dataclasses.replace(ZR, editions_table="zz_s14_not_there")
        gm = dataclasses.replace(ZM, editions_table="zz_s14_not_there2")
        for fn in (lambda: v.real_edition1_and_latest(self.cur, gr, gm),
                   lambda: v.real_brma_sets(self.cur, gr, gm),
                   lambda: v.real_rule_1(self.cur, gr),
                   lambda: v.real_weekly(self.cur, gr),
                   lambda: v.real_held_reread(self.cur, gr),
                   lambda: v.real_vintages(self.cur, gm),
                   lambda: v.real_edition1_hash(self.cur, gr, gm),
                   lambda: v.real_migration_proof(self.cur, gr, gm),
                   lambda: v.real_correction(self.cur, gm)):
            with self.assertRaises(ValueError) as e:
                fn()
            self.assertIn("does not exist", str(e.exception))
        self.assertTrue(v.shape_problems(self.cur, gr))

    def test_a_pending_real_part_prints_not_yet_migrated(self):
        out = io.StringIO()
        before = len(v.RESULTS)
        with mock.patch.object(v, "exists", return_value=False), \
                contextlib.redirect_stdout(out):
            v.mixed(99, "x", True, "seeded fine", self.cur,
                    lambda c: (True, "never called"))
        del v.RESULTS[before:]
        self.assertIn("FAIL (pending migration)", out.getvalue())
        self.assertIn("not yet migrated", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(v, "exists", return_value=True), \
                contextlib.redirect_stdout(out):
            v.mixed(98, "y", False, "seeded broke", self.cur,
                    lambda c: (True, "never called"))
        del v.RESULTS[before:]
        self.assertIn("GATE 98 y: FAIL", out.getvalue())
        self.assertNotIn("pending", out.getvalue())

    def test_drifted_live_cells_fail(self):
        self.e.seed(self.cur)
        for sql in (
                f"UPDATE public.{ZR.live_table} SET sar_monthly = "
                "sar_monthly + 1 WHERE brma_name = 'Ashford'",
                f"UPDATE public.{ZR.live_table} SET four_bed_weekly = "
                "four_bed_weekly + 0.01 WHERE brma_name = 'Ashford'",
                f"DELETE FROM public.{ZR.live_table} WHERE brma_name = "
                "'Ashford'",
                f"UPDATE public.{ZM.live_table} SET brma_name = 'Ashford' "
                "WHERE lad24cd = 'E06000010'",
                f"UPDATE public.{ZM.live_table} SET mapping_method = 'NaN' "
                "WHERE lad24cd = 'E06000010'"):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            ok, detail = v.real_edition1_and_latest(self.cur, ZR, ZM)
            self.assertFalse(ok, sql)
            self.assertIn("edition-only", detail)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_rule_1_fails_on_a_zero_a_null_or_a_skipped_row(self):
        self.e.seed(self.cur)
        path = self.e.local()
        sha = m.content_sha256(path)
        for sql, needle in (
                (f"UPDATE public.{ZR.live_table} SET three_bed_monthly = 0 "
                 "WHERE brma_name = 'Ashford'", "zero or negative"),
                (f"UPDATE public.{ZR.live_table} SET sar_weekly = NULL "
                 "WHERE brma_name = 'Ashford'", "NULL")):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            ok, detail = v.real_rule_1(self.cur, ZR, path.parent, sha)
            self.assertFalse(ok, sql)
            self.assertIn(needle, detail)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")
        self.cur.execute(f"ALTER TABLE public.{ZR.editions_table} DISABLE "
                         "TRIGGER USER")
        self.cur.execute(f"DELETE FROM public.{ZR.editions_table} WHERE "
                         "brma_name = 'Ashford'")
        ok, detail = v.real_rule_1(self.cur, ZR, path.parent, sha)
        self.assertFalse(ok)
        self.assertIn("data rows", detail)

    def test_the_weekly_gate_is_independent_decimal_half_up(self):
        for a in ("393.24", "1097.80", "0.01", "3000.00", "1234.56"):
            self.assertEqual(v.expected_weekly(a), m.weekly(Decimal(a)), a)
        self.assertEqual(v.expected_weekly("0.195"), Decimal("0.05"))
        self.assertEqual(round(0.195 * 12 / 52, 2), 0.04)    # float differs
        self.assertEqual(v.weekly_ast_problems(), [])
        p = Path(self.tmpdir.name) / "x.py"
        p.write_text("def weekly(monthly):\n    return round(float(monthly) "
                     "* 12 / 52, 2)\n", encoding="utf-8")
        self.assertGreaterEqual(len(v.weekly_ast_problems(p)), 3)
        self.e.seed(self.cur)
        path = self.e.local()
        self.cur.execute(f"UPDATE public.{ZR.live_table} SET three_bed_weekly"
                         " = three_bed_weekly + 0.01 WHERE brma_name = "
                         "'Ashford'")
        ok, detail = v.real_weekly(self.cur, ZR, path.parent,
                                   m.content_sha256(path))
        self.assertFalse(ok)
        self.assertIn("!=", detail)

    def test_the_w1_join_gate_catches_each_way_to_lose_an_authority(self):
        self.e.seed(self.cur)
        for sql, needle in (
                (f"UPDATE public.{ZM.live_table} SET brma_name = 'Hull & "
                 "East Riding' WHERE lad24cd = 'E06000010'",
                 "no rate row for Hull"),
                (f"DELETE FROM public.{ZR.live_table} WHERE brma_name = "
                 "'Ashford'", "no rate row for Ashford"),
                (f"UPDATE public.{ZR.live_table} SET two_bed_weekly = NULL "
                 "WHERE brma_name = 'Ashford'", "NULL weekly")):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            ok, detail = v.real_w1_join(self.cur, ZR, ZM, ZM.live_table, NA)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")
            self.assertFalse(ok, sql)
            self.assertIn(needle, detail)
        ok, detail = v.real_w1_join(self.cur, ZR, ZM, ZM.live_table, NA + 1)
        self.assertFalse(ok)
        # driven from la_boundaries (as the real gate is) an authority with
        # no crosswalk row is listed
        un = v.w1_join_unmatched(self.cur, ZR, ZM)
        self.assertTrue(un)
        self.assertEqual({why for _, why in un}, {"no crosswalk row"})

    def test_the_mapping_gate_reproduces_the_crosswalk_and_catches_drift(self):
        self.e.seed(self.cur)
        self.cur.execute(f"UPDATE public.{ZM.live_table} SET la_name = 'x' "
                         "WHERE lad24cd = 'E06000010'")
        ok, detail = v.real_mapping(self.cur, ZR, ZM, self.e.gml, NA, False)
        self.assertFalse(ok)
        self.assertIn("la_name reproduced", detail)

    def test_note_lines_are_scan_safe_and_verified(self):
        path, legacy = self.migrated()
        lines = v.note_lines_for(self.cur, ZR, ZM)
        self.assertEqual(len(lines), 6)
        for line in lines:
            self.assertIsNone(re.search(r"[0-9a-f]{64}", line))
            self.assertRegex(line, r"sha256-first32=[0-9a-f]{32} "
                                   r"sha256-last32=[0-9a-f]{32}$")
        self.assertEqual([x.split()[0] for x in lines],
                         ["before-state", "before-state",
                          "before-state-all", "before-state-all", "w1-read",
                          "w1-read"])
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("\n".join(lines) + "\n", encoding="utf-8")
        legacy_n = (legacy["rates"][0], legacy["mapping"][0])
        ok, detail = v.real_edition1_hash(self.cur, ZR, ZM, note, legacy_n)
        self.assertTrue(ok, detail)
        ok, detail = v.real_w1_read(self.cur, ZR, ZM, note)
        self.assertTrue(ok, detail)
        # tampering with a hash is caught
        first = lines[0].split()[-2]
        note.write_text(note.read_text().replace(first[-12:], "0" * 12),
                        encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZR, ZM, note, legacy_n)
        self.assertFalse(ok)
        self.assertIn("differs", detail)

    def test_the_w1_read_lines_ignore_monthly_rates_and_provenance(self):
        self.e.seed(self.cur)
        lines = [x for x in v.note_lines_for(self.cur, ZR, ZM)
                 if x.startswith("w1-read")]
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.cur.execute(f"UPDATE public.{ZR.live_table} SET source = 'x', "
                         "sar_monthly = sar_monthly + 1")
        self.cur.execute(f"UPDATE public.{ZM.live_table} SET mapping_method "
                         "= 'NaN', source = 'x'")
        ok, detail = v.real_w1_read(self.cur, ZR, ZM, note)
        self.assertTrue(ok, detail)
        self.cur.execute(f"UPDATE public.{ZR.live_table} SET two_bed_weekly "
                         "= two_bed_weekly + 0.01 WHERE brma_name = "
                         "'Ashford'")
        ok, detail = v.real_w1_read(self.cur, ZR, ZM, note)
        self.assertFalse(ok)
        self.assertIn("differs", detail)

    def test_the_w1_read_gate_needs_the_note_and_its_labels(self):
        self.e.seed(self.cur)
        ok, detail = v.real_w1_read(self.cur, ZR, ZM,
                                    Path(self.tmpdir.name) / "none.md")
        self.assertFalse(ok)
        self.assertIn("not written", detail)
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("before-state brma_lha_rates 2026-27 rows=5 "
                        + v.hash_fields("a" * 64) + "\n", encoding="utf-8")
        ok, detail = v.real_w1_read(self.cur, ZR, ZM, note)
        self.assertFalse(ok)
        self.assertIn("no 'w1-read", detail)

    def test_the_held_reread_and_proof_gates_read_the_files_on_disk(self):
        path, legacy = self.migrated()
        raw = Path(self.tmpdir.name) / "raw"
        raw.mkdir()
        (raw / path.name).write_bytes(path.read_bytes())
        sha = m.content_sha256(path)
        ok, detail = v.real_held_reread(self.cur, ZR, raw, sha)
        self.assertTrue(ok, detail)
        ok, detail = v.real_migration_proof(self.cur, ZR, ZM, raw, sha,
                                            self.e.gml, legacy["method_form"])
        self.assertTrue(ok, detail)
        self.assertIn("0 differences", detail)
        ok, detail = v.real_migration_proof(
            self.cur, ZR, ZM, raw, sha, self.e.gml,
            (legacy["method_form"][0], legacy["method_form"][1],
             legacy["method_form"][2] + 1))
        self.assertFalse(ok)
        self.assertIn("expected", detail)
        self.cur.execute(f"ALTER TABLE public.{ZR.editions_table} DISABLE "
                         "TRIGGER USER")
        self.cur.execute(f"UPDATE public.{ZR.editions_table} SET sar_weekly "
                         "= sar_weekly + 0.01 WHERE brma_name = 'Ashford'")
        ok, detail = v.real_migration_proof(self.cur, ZR, ZM, raw, sha,
                                            self.e.gml, legacy["method_form"])
        self.assertFalse(ok)
        ok, detail = v.real_held_reread(self.cur, ZR, raw, sha)
        self.assertFalse(ok)
        self.assertIn("differs", detail)

    def test_the_correction_gate_needs_edition_2_and_nothing_more(self):
        self.migrated()
        corr = dict(m.MAPPING_CORRECTIONS[v.CORRECTION], count=NA - 1)
        ok, detail = v.real_correction(self.cur, ZM, corr)
        self.assertFalse(ok)
        self.assertIn("not loaded yet", detail)
        with mock.patch.dict(m.MAPPING_CORRECTIONS, {v.CORRECTION: corr}):
            self.e.ok(self.cur, ["load", "--only", "mapping", "--acknowledge",
                                 tl.V, "--commit"])
            ok, detail = v.real_correction(self.cur, ZM, corr)
            self.assertFalse(ok)
            self.assertIn("live still holds", detail)
            self.e.ok(self.cur, ["refresh-latest", "--commit"])
            ok, detail = v.real_correction(self.cur, ZM, corr)
            self.assertTrue(ok, detail)
        ok, detail = v.real_correction(self.cur, ZM, dict(corr, count=NA))
        self.assertFalse(ok)

    def test_the_vintage_gate_holds_a_second_vintage_out_of_live(self):
        self.e.seed(self.cur)
        ok, detail = v.real_vintages(self.cur, ZM)
        self.assertTrue(ok, detail)
        self.cur.execute("SAVEPOINT t")
        with self.assertRaises(Exception):
            self.cur.execute(f"UPDATE public.{ZM.live_table} SET vintage = "
                             "'brma-2027-05_lad-2024-05'")
        self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_the_alias_check_is_exact_and_catches_regex_and_fuzzy(self):
        self.assertEqual(v.alias_problems(), [])
        good = json.loads(m.NAMES_FILE.read_text(encoding="utf-8"))
        first = next(iter(good["aliases"]))
        cases = {
            "fewer": {k: x for k, x in list(good["aliases"].items())[1:]},
            "wrong target": dict(good["aliases"], **{first: {
                "name": "Elsewhere", "seen": "x"}}),
            "no source": dict(good["aliases"], **{first: {
                "name": good["aliases"][first]["name"], "seen": ""}}),
        }
        for label, aliases in cases.items():
            p = Path(self.tmpdir.name) / f"{label}.json"
            p.write_text(json.dumps({"aliases": aliases}), encoding="utf-8")
            try:
                bad = v.alias_problems(p)
            except ValueError:
                bad = ["raised"]
            self.assertTrue(bad, label)
        fuzzy = Path(self.tmpdir.name) / "loader.py"
        fuzzy.write_text("import re\n\ndef brma_aliases(path=None):\n"
                         "    return {}\n\ndef convert_name(name, aliases):\n"
                         "    return re.sub(r' & ', ' and ', name)\n",
                         encoding="utf-8")
        bad = v.alias_problems(loader=fuzzy)
        self.assertTrue([x for x in bad if "uses" in x])
        # a target that is not a held BRMA name is caught
        bad = v.alias_problems(rate_names={"Ashford"})
        self.assertTrue([x for x in bad if "not BRMAs of the held" in x])

    def test_source_check_catches_a_zero_coercion(self):
        self.assertEqual(v.zero_coercions(v.LOADER), [])
        p = Path(self.tmpdir.name) / "x.py"
        p.write_text("def f(v):\n    return v or 0\n"
                     "def g(v):\n    return v or 0  # not a source value\n",
                     encoding="utf-8")
        self.assertEqual(len(v.zero_coercions(p)), 1)

    @unittest.skipUnless(m.RATES_FILE.exists() and m.GML_FILE.exists(),
                         "the held CSV and layer are not on disk")
    def test_the_real_files_pass_the_file_only_gates(self):
        self.stack.close()          # the real VINTAGES, not the fixture's
        ok, detail = v.real_identity()
        self.assertTrue(ok, detail)
        path, facts = v.read_held_csv()
        self.assertEqual(len(facts["rows"]), m.EXPECTED_POLYGONS)
        self.assertEqual(v.alias_problems(
            rate_names={r[1] for r in facts["rows"]}), [])

    def test_the_retired_script_check_finds_use_and_ignores_history(self):
        a, b = v.RETIRED
        probe = Path(self.tmpdir.name) / "probe.py"
        probe.write_text(
            f"import {b}_v2\n"
            f"cmd = ['python', 'scripts/{a}.py']\n"
            f"import subprocess\nsubprocess.run(['python -I {b}.py'])\n"
            f"note = '{b} ran by accident once'\n", encoding="utf-8")
        self.assertEqual(len(v.retired_problems([probe])), 3)
        self.assertEqual(v.retired_problems(v.retired_files()), [])
        self.assertEqual(v.retired_problems([HERE]), [])

    def test_no_gate_commits_or_rolls_back_the_real_connection(self):
        tree = ast.parse(Path(v.__file__).read_text(encoding="utf-8"))
        bad = []
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            if cls.name == "_NoCommit":
                continue
            for n in ast.walk(cls):
                if isinstance(n, ast.Attribute) and n.attr in (
                        "commit", "rollback"):
                    bad.append((cls.name, n.attr))
        for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
            if fn.name in ("main", "leftovers", "print_note_lines"):
                continue
            for n in ast.walk(fn):
                if isinstance(n, ast.Attribute) and n.attr in (
                        "commit", "rollback"):
                    bad.append((fn.name, n.attr))
        self.assertEqual(bad, [])

    def test_refresh_map_discovery_check_flags_a_missing_input(self):
        names = {"brma_lha_rates", "la_brma_mapping", "la_boundaries",
                 "brma_lha_rates_editions", "la_brma_mapping_editions"}
        bad, got = v.discovery_problems(names)
        self.assertEqual(bad, [])
        self.assertEqual(got, ["brma_lha_rates", "la_brma_mapping"])
        bad, _ = v.discovery_problems({"la_boundaries"})
        self.assertEqual(len(bad), 2)

    def test_the_main_rejects_unknown_arguments(self):
        with self.assertRaises(SystemExit) as e:
            v.main(["--bogus"])
        self.assertIn("usage", str(e.exception))

    def test_a_gate_that_raises_is_a_fail_not_a_crash(self):
        def gate_77_boom(cur):
            raise RuntimeError("boom")
        out = io.StringIO()
        before = len(v.RESULTS)
        with contextlib.redirect_stdout(out):
            v.run_gate(self.cur, gate_77_boom)
        del v.RESULTS[before:]
        self.assertIn("GATE 77", out.getvalue())
        self.assertIn("FAIL", out.getvalue())


if __name__ == "__main__":
    unittest.main()
