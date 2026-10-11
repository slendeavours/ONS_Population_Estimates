"""Tests that the real-table gates of s3_mye_editions_verify fail on a short,
empty, drifted or unmigrated state and pass on a seeded complete one. The gate
bodies (real_*) take specs, so they are called here against the throwaway
zz_s3_* tables (created inside a transaction that is always rolled back); no
real table is touched, and the S3 editions tables are never created. Every
test ends in a rollback (also on an exception), and tearDownClass reads the
database on a separate read-only connection and fails if a zz_s3 table or
function was left committed. No retired script is imported or run (the AST
check of the verify script is itself tested here, with planted probes)."""
import ast
import contextlib
import dataclasses
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import s3_mye_editions as m  # noqa: E402
import s3_mye_editions_verify as v  # noqa: E402
import test_s3_mye_loader as tl  # noqa: E402
from _db import get_conn  # noqa: E402

Z = v.ZZ
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
            left = [t for t in v.leftovers() if t.startswith("zz_s3")]
            if left:
                raise AssertionError(f"committed throwaway objects: {left}")

    def setUp(self):
        self.raw = self.conn.cursor()
        self.cur = v.SafeCur(self.raw)
        self.addCleanup(self.rollback)
        v.setup_throwaway(self.cur)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(tl.specs())
        self.addCleanup(self.stack.close)
        self.e = v.Env(self.cur, self.tmpdir.name)

    def rollback(self):
        self.raw.close()
        self.conn.rollback()

    def migrated(self):
        """A seeded legacy state, migrated: (paths, files, loaded)."""
        paths, files, loaded = self.e.seed_legacy(self.cur)
        with v.patched_legacy(self.e, loaded, files):
            rc, text, _ = self.e.cmd(["migrate-legacy", *paths, "--commit"])
        self.assertEqual(rc, 0, text)
        return paths, files, loaded

    def test_a_cursor_cannot_commit_or_roll_back_the_transaction(self):
        with self.assertRaises(RuntimeError):
            self.cur.connection.commit()
        with self.assertRaises(RuntimeError):
            self.cur.connection.rollback()

    def test_a_seeded_complete_state_passes_the_real_gates(self):
        self.e.seed(self.cur)
        for fn in (lambda: v.real_edition1_and_latest(self.cur, Z),
                   lambda: v.real_row_counts(self.cur, Z, tl.N)):
            ok, detail = fn()
            self.assertTrue(ok, detail)
        self.assertEqual(v.shape_problems(self.cur, Z), [])

    def test_an_empty_state_is_not_a_pass(self):
        ok, detail = v.real_edition1_and_latest(self.cur, Z)
        self.assertFalse(ok)
        self.assertIn("no live periods", detail)
        ok, detail = v.real_row_counts(self.cur, Z, tl.N)
        self.assertFalse(ok)

    def test_an_unmigrated_state_fails_with_a_clear_message(self):
        gone = dataclasses.replace(Z, editions_table="zz_s3_not_there")
        for fn in (lambda: v.real_edition1_and_latest(self.cur, gone),
                   lambda: v.real_row_counts(self.cur, gone),
                   lambda: v.real_values(self.cur, gone),
                   lambda: v.real_codes(self.cur, gone),
                   lambda: v.real_held_reread(self.cur, gone),
                   lambda: v.real_edition1_hash(self.cur, gone),
                   lambda: v.real_migration_proof(self.cur, gone),
                   lambda: v.real_revision(self.cur, gone)):
            with self.assertRaises(ValueError) as e:
                fn()
            self.assertIn("does not exist", str(e.exception))
        self.assertTrue(v.shape_problems(self.cur, gone))

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
        for sql in (f"UPDATE public.{Z.live_table} SET population = "
                    "population + 1 WHERE lad24cd = 'E06000002'",
                    f"UPDATE public.{Z.live_table} SET la_name = 'x' WHERE "
                    "lad24cd = 'E06000002'",
                    f"DELETE FROM public.{Z.live_table} WHERE lad24cd = "
                    "'E06000002'"):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            ok, detail = v.real_edition1_and_latest(self.cur, Z)
            self.assertFalse(ok, sql)
            self.assertIn("edition-only", detail)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_a_short_period_fails_the_authority_count(self):
        self.e.seed(self.cur)
        self.cur.execute(f"DELETE FROM public.{Z.live_table} WHERE lad24cd "
                         "= 'E08000019'")
        ok, detail = v.real_row_counts(self.cur, Z, tl.N)
        self.assertFalse(ok)
        self.assertIn(str(tl.N), detail)
        ok, detail = v.real_row_counts(self.cur, Z, tl.N + 1)
        self.assertFalse(ok)

    def test_a_zero_or_null_population_fails_rule_1(self):
        path = self.e.file()
        self.e.ok(self.cur, ["load", "--commit"])
        files = {"2025": {"sha256": m.content_sha256(path)}}
        ok, detail = v.real_values(self.cur, Z, files, path.parent)
        self.assertTrue(ok, detail)
        for sql, needle in ((f"UPDATE public.{Z.live_table} SET population "
                             "= 0 WHERE lad24cd = 'E06000002'", "zero"),
                            (f"UPDATE public.{Z.live_table} SET population "
                             "= NULL WHERE lad24cd = 'E06000002'", "NULL")):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            ok, detail = v.real_values(self.cur, Z, files, path.parent)
            self.assertFalse(ok, sql)
            self.assertIn(needle, detail)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_the_national_sum_must_equal_the_workbooks_england_row(self):
        path = self.e.file()
        self.e.ok(self.cur, ["load", "--commit"])
        other = tl.write_file(Path(self.tmpdir.name) / "o" / path.name, 2025,
                              values={("E06000002", 2025): 99999})
        ok, detail = v.real_values(self.cur, Z, {"2025": {
            "sha256": m.content_sha256(other)}}, other.parent)
        self.assertFalse(ok)
        self.assertIn("England row", detail)

    def test_codes_gate_catches_a_new_barnsley_code_and_an_unknown_one(self):
        path = self.e.file()
        self.e.ok(self.cur, ["load", "--commit"])
        files = {"2025": {"sha256": m.content_sha256(path)}}
        ok, detail = v.real_codes(self.cur, Z, files=files,
                                  raw_dir=path.parent)
        self.assertTrue(ok, detail)
        self.cur.execute(f"UPDATE public.{Z.live_table} SET lad24cd = "
                         "'E08000038' WHERE lad24cd = 'E08000016'")
        ok, detail = v.real_codes(self.cur, Z, files=files,
                                  raw_dir=path.parent)
        self.assertFalse(ok)
        self.assertIn("Barnsley/Sheffield", detail)

    def test_source_checks_catch_a_private_dict_and_a_zero_coercion(self):
        self.assertEqual(v._source_problems(v.LOADER, zeros=True), [])
        p = Path(self.tmpdir.name) / "x.py"
        p.write_text('X = {"a": "E08000038"}\n'
                     'def f(v):\n    return v or 0\n', encoding="utf-8")
        self.assertEqual(len(v._source_problems(p, zeros=True)), 2)

    def test_note_lines_are_scan_safe_and_verified(self):
        paths, files, loaded = self.migrated()
        lines = v.note_lines_for(self.cur, Z)
        self.assertEqual(len(lines), 3)       # one year, all, w1-read
        for line in lines:
            self.assertIsNone(re.search(r"[0-9a-f]{64}", line))
            self.assertRegex(line, r"sha256-first32=[0-9a-f]{32} "
                                   r"sha256-last32=[0-9a-f]{32}$")
        self.assertTrue(lines[0].startswith("before-state la_population "))
        self.assertTrue(lines[1].startswith("before-state-all "))
        self.assertTrue(lines[2].startswith("w1-read la_population "))
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, Z, note, tl.N)
        self.assertTrue(ok, detail)
        self.assertIn("before-state-all agrees", detail)
        ok, detail = v.real_w1_read(self.cur, Z, note, "2025")
        self.assertTrue(ok, detail)
        note.write_text(note.read_text().replace(
            lines[0].split()[-2][-10:], "0" * 10), encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, Z, note, tl.N)
        self.assertFalse(ok)
        self.assertIn("differs", detail)

    def test_the_w1_read_line_is_about_lad24cd_and_population_only(self):
        self.e.seed(self.cur)
        before = v.w1_read_sha(m.records(self.cur, Z, "2025"))
        self.cur.execute(f"UPDATE public.{Z.live_table} SET la_name = 'x'")
        self.assertEqual(v.w1_read_sha(m.records(self.cur, Z, "2025")),
                         before)
        self.cur.execute(f"UPDATE public.{Z.live_table} SET population = "
                         "population + 1 WHERE lad24cd = 'E06000002'")
        self.assertNotEqual(v.w1_read_sha(m.records(self.cur, Z, "2025")),
                            before)

    def test_the_w1_read_gate_needs_the_note_and_its_label(self):
        self.e.seed(self.cur)
        ok, detail = v.real_w1_read(self.cur, Z,
                                    Path(self.tmpdir.name) / "none.md")
        self.assertFalse(ok)
        self.assertIn("not written", detail)
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("before-state la_population 2025 rows=5 "
                        + v.hash_fields("a" * 64) + "\n", encoding="utf-8")
        ok, detail = v.real_w1_read(self.cur, Z, note, "2025")
        self.assertFalse(ok)
        self.assertIn("no 'w1-read", detail)

    def test_the_held_reread_and_proof_gates_read_the_workbook_on_disk(self):
        paths, files, loaded = self.migrated()
        raw = Path(self.tmpdir.name) / "raw"
        raw.mkdir()
        for p in paths:
            (raw / p.name).write_bytes(p.read_bytes())
        ok, detail = v.real_held_reread(self.cur, Z, files, raw, forms={})
        self.assertTrue(ok, detail)
        ok, detail = v.real_migration_proof(self.cur, Z, files, raw,
                                            forms={})
        self.assertTrue(ok, detail)
        self.assertIn("0 differences", detail)
        self.cur.execute(f"ALTER TABLE public.{Z.editions_table} DISABLE "
                         "TRIGGER USER")
        self.cur.execute(f"UPDATE public.{Z.editions_table} SET population "
                         "= population + 1 WHERE lad24cd = 'E06000002'")
        ok, detail = v.real_migration_proof(self.cur, Z, files, raw,
                                            forms={})
        self.assertFalse(ok)
        ok, detail = v.real_held_reread(self.cur, Z, files, raw, forms={})
        self.assertFalse(ok)
        self.assertIn("differs", detail)

    def test_the_revision_gate_needs_edition_2_and_the_listed_counts(self):
        paths, files, loaded = self.migrated()
        newer = self.e.file(2026, values={
            ("E06000002", 2025): tl.value("E06000002", 2025) + 5})
        raw = Path(self.tmpdir.name) / "raw"
        raw.mkdir()
        (raw / newer.name).write_bytes(newer.read_bytes())
        sel = {"2025": {"sha256": m.content_sha256(paths[0])},
               "2026": {"sha256": m.content_sha256(newer)}}
        (raw / paths[0].name).write_bytes(paths[0].read_bytes())
        ok, detail = v.real_revision(self.cur, Z, sel, raw, "2025", "2026",
                                     (1, 5))
        self.assertFalse(ok)
        self.assertIn("not loaded yet", detail)
        self.e.ok(self.cur, ["load", "--commit", "--acknowledge", "2025"])
        ok, detail = v.real_revision(self.cur, Z, sel, raw, "2025", "2026",
                                     (1, 5))
        self.assertTrue(ok, detail)
        ok, detail = v.real_revision(self.cur, Z, sel, raw, "2025", "2026",
                                     (2, 5))
        self.assertFalse(ok)
        self.assertIn("expected 2 authorities", detail)

    @unittest.skipUnless((m.RAW_DIR / "mye24tablesew.xlsx").exists()
                         and (m.RAW_DIR / "mye25tablesew.xlsx").exists(),
                         "the held workbooks are not on disk")
    def test_the_real_workbooks_give_the_listed_revision(self):
        ok, detail = v.revision_facts()
        self.assertTrue(ok, detail)
        self.assertIn("295 of 296", detail)
        self.assertIn("-12,931", detail)
        ok, detail = v.real_cells()
        self.assertTrue(ok, detail)
        ok, detail = v.real_identity()
        self.assertTrue(ok, detail)

    def test_the_retired_script_check_finds_use_and_ignores_history(self):
        a, b = v.RETIRED
        probe = Path(self.tmpdir.name) / "probe.py"
        probe.write_text(
            f"import {a}\n"
            f"cmd = ['python', 'scripts/{b}_v2.py']\n"
            f"import subprocess\nsubprocess.run(['python -I {a}.py'])\n"
            f"note = '{a} was how it was loaded once'\n", encoding="utf-8")
        self.assertEqual(len(v.retired_problems([probe])), 3)
        self.assertEqual(v.retired_problems(v.retired_files()), [])
        # this test module neither imports nor runs a retired script
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
        bad, got = v.discovery_problems({"la_population", "la_boundaries"})
        self.assertEqual(bad, [])
        self.assertIn("la_population", got)
        self.assertNotIn("la_boundaries", got)
        bad, _ = v.discovery_problems({"la_boundaries"})
        self.assertTrue(bad)

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
