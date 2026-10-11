"""Tests that the real-table gates of s5_imd_editions_verify fail on a short,
empty, drifted or unmigrated state. The gate bodies (real_*) take a spec, so
they are called here against the throwaway zz_s5_* tables (created inside a
transaction that is always rolled back); no real table is touched, and the S5
editions table is never created. Every test ends in a rollback (also on an
exception), and tearDownClass reads the database on a separate read-only
connection and fails if a zz_s5 table was left committed."""
import contextlib
import dataclasses
import io
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import s5_imd_editions as m  # noqa: E402
import s5_imd_editions_verify as v  # noqa: E402
import test_s5_imd_loader as tl  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s5_imd_pure import write_file10  # noqa: E402

ZZ = v.ZZ
P = tl.P
P30 = tl.P30
N = tl.N
UTC = timezone.utc


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
            left = [t for t in v.leftovers() if t.startswith("zz_s5")]
            if left:
                raise AssertionError(f"committed throwaway tables: {left}")

    def setUp(self):
        self.raw = self.conn.cursor()
        self.cur = v.SafeCur(self.raw)
        self.addCleanup(self.rollback)
        v.setup_throwaway(self.cur)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(v.specs())
        self.addCleanup(self.stack.close)
        self.e = v.Env(self.cur, self.tmpdir.name)

    def rollback(self):
        self.raw.close()
        self.conn.rollback()

    def seed(self):
        self.e.seed(self.cur)

    def gates(self):
        return (v.real_edition1_and_latest,
                lambda c, s: v.real_single_vintage(c, s, n=N),
                lambda c, s: v.real_rank_ranges(c, s, n=N))

    def test_a_cursor_cannot_commit_or_roll_back_the_transaction(self):
        with self.assertRaises(RuntimeError):
            self.cur.connection.commit()
        with self.assertRaises(RuntimeError):
            self.cur.connection.rollback()

    def test_a_seeded_complete_state_passes_every_real_gate(self):
        self.seed()
        for fn in self.gates():
            ok, detail = fn(self.cur, ZZ)
            self.assertTrue(ok, detail)
        self.assertEqual(v.shape_problems(self.cur, ZZ), [])
        self.assertEqual(v.vintage_column_problems(self.cur, ZZ), [])

    def test_an_empty_state_is_not_a_pass(self):
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("no live vintages", detail)
        ok, detail = v.real_rank_ranges(self.cur, ZZ, n=N)
        self.assertFalse(ok)
        self.assertIn("no live vintages", detail)
        ok, detail = v.real_single_vintage(self.cur, ZZ, n=N)
        self.assertFalse(ok)
        self.assertIn("holds vintage(s) []", detail)

    def test_an_unmigrated_state_fails_with_a_clear_message(self):
        gone = dataclasses.replace(ZZ, editions_table="zz_s5_not_there")
        for fn in self.gates():
            with self.assertRaises(ValueError) as e:
                fn(self.cur, gone)
            self.assertIn("does not exist", str(e.exception))
        self.assertTrue(v.shape_problems(self.cur, gone))

    def test_a_live_table_without_the_vintage_column_is_pending_ddl(self):
        self.seed()
        self.cur.execute(f"ALTER TABLE public.{ZZ.live_table} DROP COLUMN "
                         "vintage")
        problems = v.vintage_column_problems(self.cur, ZZ)
        self.assertIn("does not exist", problems[0])
        with self.assertRaises(ValueError) as e:
            v.real_edition1_and_latest(self.cur, ZZ)
        self.assertIn("no vintage column", str(e.exception))
        ok, detail = v.real_single_vintage(self.cur, ZZ, n=N)
        self.assertFalse(ok)
        self.assertIn("vintage does not exist", detail)

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
            v.mixed(99, "x", True, "seeded fine", self.cur,
                    lambda c: (True, "real fine"))
            v.mixed(98, "y", False, "seeded broke", self.cur,
                    lambda c: (True, "never called"))
        del v.RESULTS[before:]
        text = out.getvalue()
        self.assertIn("GATE 99 x: PASS", text)
        self.assertIn("GATE 98 y: FAIL", text)
        self.assertNotIn("pending", text)

    def test_the_real_tables_do_not_exist_yet_so_gates_print_pending(self):
        # read-only: the real editions table has not been created
        self.assertFalse(v.exists(self.cur))

    def plant(self, sql, fn, needle):
        self.seed()
        self.cur.execute(sql)
        ok, detail = fn(self.cur, ZZ)
        self.assertFalse(ok, detail)
        self.assertIn(needle, detail)

    def test_drifted_live_cells_fail(self):
        live = ZZ.live_table
        self.plant(f"UPDATE public.{live} SET imd_average_score = "
                   "imd_average_score + 1 WHERE lad24cd = 'E06000001'",
                   v.real_edition1_and_latest, "edition-only")

    def test_a_missing_live_row_fails(self):
        self.plant(f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = "
                   "'E06000001'", v.real_edition1_and_latest, "live-only")

    def test_a_vintage_without_edition_1_fails(self):
        self.seed()
        v.drop_live_vintage_check(self.cur, ZZ)
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET vintage = "
                         "'IoD2030'")
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("no edition 1", detail)

    def test_a_second_vintage_in_live_fails(self):
        self.seed()
        v.drop_live_vintage_check(self.cur, ZZ)
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET vintage = "
                         "'IoD2030' WHERE lad24cd = 'E06000001'")
        ok, detail = v.real_single_vintage(self.cur, ZZ, n=N)
        self.assertFalse(ok)
        self.assertIn("live holds vintage", detail)

    def test_the_live_check_refuses_a_second_vintage(self):
        self.seed()
        self.cur.execute("SAVEPOINT t")
        with self.assertRaises(Exception):
            self.cur.execute(f"UPDATE public.{ZZ.live_table} SET vintage = "
                             "'IoD2030'")
        self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_each_vintage_column_defect_is_reported(self):
        self.seed()
        live = ZZ.live_table
        for sql, needle in (
                (f"ALTER TABLE public.{live} ALTER COLUMN vintage DROP "
                 "DEFAULT", "expected"),
                (f"ALTER TABLE public.{live} ALTER COLUMN vintage DROP NOT "
                 "NULL", "expected")):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            problems = v.vintage_column_problems(self.cur, ZZ)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")
            self.assertTrue(problems and needle in problems[0], problems)
        self.cur.execute("SAVEPOINT t")
        v.drop_live_vintage_check(self.cur, ZZ)
        problems = v.vintage_column_problems(self.cur, ZZ)
        self.cur.execute("ROLLBACK TO SAVEPOINT t")
        self.assertIn("CHECK", problems[0])

    def test_a_held_vintage_is_information_not_a_fault(self):
        self.seed()
        self.e.file(P30, modified=datetime(2030, 11, 1, tzinfo=UTC))
        rc, text, _ = self.e.cmd(["load", "--commit"])
        self.assertEqual(rc, 0, text)
        ok, detail = v.real_single_vintage(self.cur, ZZ, n=N)
        self.assertTrue(ok, detail)
        self.assertIn(P30, detail)
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertTrue(ok, detail)
        ok, detail = v.real_rank_ranges(self.cur, ZZ, n=N)
        self.assertTrue(ok, detail)

    def test_rank_and_range_faults_fail(self):
        self.seed()
        live = ZZ.live_table
        for sql, needle in (
                (f"UPDATE public.{live} SET imd_rank_of_average_rank = 1 "
                 "WHERE lad24cd = 'E06000002'", "imd_rank_of_average_rank"),
                (f"UPDATE public.{live} SET imd_rank_of_extent = 9 WHERE "
                 "lad24cd = 'E06000002'", "imd_rank_of_extent"),
                (f"UPDATE public.{live} SET imd_extent = 1.5 WHERE "
                 "lad24cd = 'E06000002'", "imd_extent"),
                (f"UPDATE public.{live} SET imd_proportion_most_deprived_10"
                 " = -0.5 WHERE lad24cd = 'E06000002'",
                 "imd_proportion_most_deprived_10")):
            with self.subTest(needle=needle):
                self.cur.execute("SAVEPOINT t")
                self.cur.execute(sql)
                ok, detail = v.real_rank_ranges(self.cur, ZZ, n=N)
                self.cur.execute("ROLLBACK TO SAVEPOINT t")
                self.assertFalse(ok, detail)
                self.assertIn(needle, detail)
        ok, detail = v.real_rank_ranges(self.cur, ZZ, n=N + 1)
        self.assertFalse(ok)
        self.assertIn("rows, expected", detail)

    def test_shape_problems_name_a_missing_trigger_and_column(self):
        self.seed()
        self.cur.execute(f"DROP TRIGGER {ZZ.trigger} ON public."
                         f"{ZZ.editions_table}")
        self.assertTrue(any("append-only triggers" in p for p in
                            v.shape_problems(self.cur, ZZ)))

    def test_the_note_lines_are_scan_safe_and_read_by_regex(self):
        self.seed()
        lines = v.note_lines_for(self.cur, ZZ)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith(f"before-state la_imd_2025 {P} "
                                            f"rows={N} sha256-first32="))
        self.assertTrue(lines[1].startswith("before-state-all "))
        self.assertTrue(lines[2].startswith("w1-read "))
        for line in lines:
            self.assertIsNone(re.search(r"[0-9a-f]{64}", line), line)
            self.assertTrue(re.search(r"sha256-first32=[0-9a-f]{32} "
                                      r"sha256-last32=[0-9a-f]{32}$", line))
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("intro\n" + "\n".join(lines) + "\n",
                        encoding="utf-8")
        self.assertEqual(list(v.note_hashes(note)), [P])
        self.assertEqual(list(v.w1_note_hashes(note)), [P])
        self.assertEqual(v.note_hashes(Path(self.tmpdir.name) / "none"), {})
        # the three hashes are different things
        hs = {x.split(" sha256-first32=")[1] for x in lines}
        self.assertEqual(len(hs), 3)

    def migrated(self):
        path, files = self.e.seed_legacy(self.cur)
        with mock.patch.object(m, "LEGACY_LIVE", self.e.legacy(self.cur)), \
                mock.patch.object(m, "LEGACY_FILE", files):
            rc, text, _ = self.e.cmd(["migrate-legacy", path, "--commit"])
        self.assertEqual(rc, 0, text)
        return path, files

    def test_edition_1_hash_against_the_note(self):
        path, files = self.e.seed_legacy(self.cur)
        lines = v.note_lines_for(self.cur, ZZ)
        with mock.patch.object(m, "LEGACY_LIVE", self.e.legacy(self.cur)), \
                mock.patch.object(m, "LEGACY_FILE", files):
            rc, text, _ = self.e.cmd(["migrate-legacy", path, "--commit"])
        self.assertEqual(rc, 0, text)
        h = m.rows_content_sha(m.records(self.cur, ZZ, P, 1))
        note = Path(self.tmpdir.name) / "n.md"
        line = lines[0]
        note.write_text(line + "\n", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, N)
        self.assertTrue(ok, detail)
        for text, needle in (
                (line.replace(h[:32], "0" * 32), "differs"),
                (line.replace(f"rows={N}", f"rows={N + 1}"), "differs"),
                (lines[1], "records no"),
                (line + "\n" + line.replace(P, "IoD2019"),
                 "no edition 1 as loaded"),
                ("", "records no")):
            note.write_text(text, encoding="utf-8")
            ok, detail = v.real_edition1_hash(self.cur, ZZ, note, N)
            self.assertFalse(ok, text)
            self.assertIn(needle, detail)
        ok, detail = v.real_edition1_hash(self.cur, ZZ,
                                          Path(self.tmpdir.name) / "x", N)
        self.assertFalse(ok)
        self.assertIn("not written", detail)
        note.write_text(line + "\n", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, N + 3)
        self.assertFalse(ok)
        self.assertIn("surveyed", detail)

    def test_edition_1_hash_fails_before_any_migration(self):
        self.seed()      # a load, not a migration: no edition 1 'as loaded'
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, N)
        self.assertFalse(ok)
        self.assertIn("migrate-legacy not run", detail)

    def test_w1_read_against_the_note(self):
        self.seed()
        lines = v.note_lines_for(self.cur, ZZ)
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text(lines[2] + "\n", encoding="utf-8")
        ok, detail = v.real_w1_read(self.cur, ZZ, note, rows=N)
        self.assertTrue(ok, detail)
        # a score is not what W1 reads; a rank is
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET "
                         "imd_average_score = imd_average_score + 1")
        self.assertTrue(v.real_w1_read(self.cur, ZZ, note, rows=N)[0])
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET "
                         "imd_rank_of_average_rank = 99 WHERE lad24cd = "
                         "'E06000001'")
        ok, detail = v.real_w1_read(self.cur, ZZ, note, rows=N)
        self.assertFalse(ok)
        self.assertIn("differs", detail)
        for text in ("", lines[0], lines[1]):
            note.write_text(text, encoding="utf-8")
            ok, detail = v.real_w1_read(self.cur, ZZ, note, rows=N)
            self.assertFalse(ok)
            self.assertIn("records no", detail)
        ok, detail = v.real_w1_read(self.cur, ZZ,
                                    Path(self.tmpdir.name) / "x", rows=N)
        self.assertIn("not written", detail)

    def test_the_migration_proof_reruns_from_the_file_found_by_sha256(self):
        path, files = self.migrated()
        raw = Path(self.tmpdir.name) / "raw"
        raw.mkdir()
        (raw / path.name).write_bytes(path.read_bytes())
        ok, detail = v.real_migration_proof(self.cur, ZZ, files, raw,
                                            areas=N)
        self.assertTrue(ok, detail)
        self.assertIn("0 differences", detail)
        ok, detail = v.real_held_reread(self.cur, ZZ, files, raw, areas=N)
        self.assertTrue(ok, detail)
        ok, detail = v.real_identity(self.cur, files, raw, ZZ, areas=N)
        self.assertTrue(ok, detail)
        ok, detail = v.real_rank(self.cur, files, raw, ZZ, areas=N)
        self.assertTrue(ok, detail)
        self.assertIn("edition 1 records the same rank", detail)
        (raw / path.name).write_bytes(path.read_bytes() + b"\0")
        for fn in (v.real_migration_proof, v.real_held_reread):
            ok, detail = fn(self.cur, ZZ, files, raw, areas=N)
            self.assertFalse(ok)
            self.assertIn("no file", detail)

    def test_a_file_that_is_not_edition_1_fails_the_proof(self):
        path, files = self.migrated()
        other = write_file10(Path(self.tmpdir.name) / "o" / path.name,
                             overrides={("E06000002", "imd_average_score"):
                                        35.6})
        fx = {**files, "sha256": m.content_sha256(other)}
        ok, detail = v.real_migration_proof(self.cur, ZZ, fx, other.parent,
                                            areas=N)
        self.assertFalse(ok)
        self.assertIn("differences", detail)
        ok, detail = v.real_held_reread(self.cur, ZZ, fx, other.parent,
                                        areas=N)
        self.assertFalse(ok)
        self.assertIn("differs", detail)

    def test_identity_and_rank_from_a_file_on_disk(self):
        d = Path(self.tmpdir.name) / "disk"
        good = write_file10(d / "good.xlsx")
        fx = {"sha256": m.content_sha256(good)}
        # the fixture's docProps modified is the rank the loader surveyed
        ok, detail = v.real_identity(None, fx, d, areas=N)
        self.assertTrue(ok, detail)
        ok, detail = v.real_rank(None, fx, d, areas=N)
        self.assertTrue(ok, detail)
        with self.assertRaises(ValueError):                 # 296 expected
            v.real_identity(None, fx, d)
        old = write_file10(d / "old.xlsx",
                           modified=datetime(2025, 10, 28, tzinfo=UTC))
        ok, detail = v.real_identity(None, {"sha256": m.content_sha256(old)},
                                     d, areas=N)
        self.assertFalse(ok)
        self.assertIn("docProps modified", detail)
        ok, detail = v.real_identity(None, {"sha256": "0" * 64}, d)
        self.assertFalse(ok)
        self.assertIn("no file", detail)

    def test_a_gate_that_raises_is_a_fail_of_that_gate(self):
        def gate_99_boom(cur):
            raise ValueError("boom")
        out = io.StringIO()
        before = len(v.RESULTS)
        with contextlib.redirect_stdout(out):
            v.run_gate(self.cur, gate_99_boom)
        del v.RESULTS[before:]
        self.assertIn("GATE 99 gate_99_boom: FAIL", out.getvalue())
        self.assertIn("boom", out.getvalue())

    def test_every_gate_is_numbered_once(self):
        nums = [int(re.match(r"gate_(\d+)_", g.__name__).group(1))
                for g in v.GATES]
        self.assertEqual(nums, list(range(1, 18)))

    def test_print_note_lines_rejects_other_arguments(self):
        with self.assertRaises(SystemExit):
            v.main(["--nope"])

    def test_the_throwaway_tables_exist_only_inside_the_transaction(self):
        self.seed()
        self.assertEqual(core.rows_differing(self.cur, ZZ, P, 1), 0)


if __name__ == "__main__":
    unittest.main()
