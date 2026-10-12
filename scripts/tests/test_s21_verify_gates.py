"""Tests that the real-table gates of s21_neighbours_editions_verify fail on a
short, empty, drifted or unmigrated state. The gate bodies (real_*) take a
spec, so they are called here against the throwaway zz_s21_* tables (created
inside a transaction that is always rolled back); no real table is written
(test_the_gates_leave_the_real_tables_as_they_were compares their row counts
and hash). Every test ends in a rollback (also on an exception), and
tearDownClass reads the database on a separate read-only connection and
fails if a zz_s21 relation or function was left committed.

test_the_real_gates_pass_on_a_copy_of_the_real_data builds the editions in
the throwaway tables from a copy of the real live rows and the real held
file (when the file is on disk) and runs every real gate on it: the gates
that stay pending against the real tables until Task 3's migration are
exercised on the real data here, with nothing real written.

Nothing here imports or runs the old build (the retired S21 build script);
test_no_old_build_is_imported_or_run checks the sources by AST."""
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

import editions_core as core  # noqa: E402
import s21_neighbours_editions as m  # noqa: E402
import s21_neighbours_editions_verify as v  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s21_neighbours_pure import write_s21  # noqa: E402

ZZ = v.ZZ
P = v.P
P27 = v.P27
N = v.N
N_LIVE = v.N_LIVE
CHANGED = v.CHANGED
LOADER_TEXT = v.LOADER.read_text(encoding="utf-8")


class VerifyGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()
        # a second, read-only view of committed state: what other sessions
        # see, taken before any scenario runs
        cls.conn2 = get_conn()
        cls.conn2.set_session(readonly=True)
        cls.raw_second = cls.conn2.cursor()
        cls.real_before = v.real_snapshot(cls.raw_second)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.rollback()
            cls.conn.close()
            cls.raw_second.close()
            cls.conn2.rollback()
            cls.conn2.close()
        finally:
            left = [t for t in v.leftovers() if " zz_s21" in t]
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
        self.stack.enter_context(v.specs())
        self.addCleanup(self.stack.close)
        self.e = v.Env(self.cur, self.tmpdir.name)

    def rollback(self):
        self.raw.close()
        self.conn.rollback()

    def seed(self):
        self.e.seed(self.cur)

    def gates(self):
        return (lambda c, s: v.real_live_rule(c, s, subjects=N),
                lambda c, s: v.real_codes(c, s),
                lambda c, s: v.real_counts(c, s, subjects=N),
                lambda c, s: v.real_nulls(c, s),
                lambda c, s: v.real_not_live(c, s))

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
        self.assertEqual(v.release_column_problems(self.cur, ZZ), [])
        self.assertEqual(v.constraint_refusals(self.cur, ZZ), [])

    def test_an_empty_state_is_not_a_pass(self):
        for fn in (lambda c, s: v.real_live_rule(c, s, subjects=N),
                   lambda c, s: v.real_counts(c, s, subjects=N)):
            ok, detail = fn(self.cur, ZZ)
            self.assertFalse(ok)
            self.assertIn("no live release", detail)
        ok, detail = v.real_nulls(self.cur, ZZ)
        self.assertTrue(ok, detail)                # nothing to contradict
        ok, detail = v.real_not_live(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("holds release(s) []", detail)

    def test_an_unmigrated_state_fails_with_a_clear_message(self):
        gone = dataclasses.replace(ZZ, editions_table="zz_s21_not_there")
        for fn in self.gates():
            with self.assertRaises(ValueError) as e:
                fn(self.cur, gone)
            self.assertIn("does not exist", str(e.exception))
        self.assertTrue(v.shape_problems(self.cur, gone))

    def test_a_live_table_without_the_release_column_is_pending_ddl(self):
        self.seed()
        self.cur.execute(f"ALTER TABLE public.{ZZ.live_table} DROP COLUMN "
                         "release_date")
        problems = v.release_column_problems(self.cur, ZZ)
        self.assertIn("does not exist", problems[0])
        ok, detail = v.real_live_rule(self.cur, ZZ, subjects=N)
        self.assertFalse(ok)
        self.assertIn("release_date does not exist", detail)
        with self.assertRaises(ValueError) as e:
            v.real_counts(self.cur, ZZ, subjects=N)
        self.assertIn("no release_date column", str(e.exception))

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

    def test_the_gates_leave_the_real_tables_as_they_were(self):
        """The real tables (live, editions, ledger) hold the same rows and
        the same content after the throwaway scenarios as before them; only
        the zz_s21 copies were touched."""
        self.assertTrue(v.exists(self.cur, ZZ))
        self.assertNotEqual(ZZ.editions_table, v.REAL.editions_table)
        self.assertTrue(ZZ.editions_table.startswith("zz_s21_"))
        self.assertTrue(ZZ.live_table.startswith("zz_s21_"))
        self.seed()
        self.assertEqual(v.real_snapshot(self.cur), type(self).real_before)
        self.assertEqual(v.real_snapshot(self.raw_second),
                         type(self).real_before)

    def plant(self, sql, fn, needle):
        """The statement runs in a savepoint that is rolled back, so each
        planted defect is the only one."""
        self.seed()
        ok, detail = v.plant(self.cur, sql, lambda: fn(self.cur, ZZ))
        self.assertFalse(ok, detail)
        self.assertIn(needle, detail)

    def test_live_drift_fails(self):
        live = ZZ.live_table
        rule = lambda c, s: v.real_live_rule(c, s, subjects=N)  # noqa: E731
        self.plant(f"UPDATE public.{live} SET neighbour_rank = 5 WHERE "
                   "lad24cd = 'E06000002' AND neighbour_rank = 4", rule,
                   "another rank")
        self.plant(f"DELETE FROM public.{live} WHERE lad24cd = 'E06000002'"
                   " AND neighbour_rank = 3", rule, "missing")
        self.plant(f"UPDATE public.{live} SET statistical_neighbour_lad24cd "
                   "= 'W06000022' WHERE lad24cd = 'E06000002' AND "
                   "neighbour_rank = 1", rule, "not English authorities")

    def test_a_second_release_in_live_fails(self):
        self.seed()
        v.drop_live_release_check(self.cur, ZZ)
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET release_date = "
                         "'2030-01-01'")
        ok, detail = v.real_live_rule(self.cur, ZZ, subjects=N)
        self.assertFalse(ok)
        self.assertIn("live holds release", detail)
        self.assertIn("no edition 1", detail)

    def test_the_live_check_refuses_a_second_release(self):
        self.seed()
        self.cur.execute("SAVEPOINT t")
        with self.assertRaises(Exception):
            self.cur.execute(f"UPDATE public.{ZZ.live_table} SET "
                             "release_date = '2027-03-04'")
        self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_each_release_column_defect_is_reported(self):
        self.seed()
        live = ZZ.live_table
        for sql in (f"ALTER TABLE public.{live} ALTER COLUMN release_date "
                    "DROP DEFAULT",
                    f"ALTER TABLE public.{live} ALTER COLUMN release_date "
                    "DROP NOT NULL"):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            problems = v.release_column_problems(self.cur, ZZ)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")
            self.assertTrue(problems and "expected" in problems[0], problems)
        self.cur.execute("SAVEPOINT t")
        v.drop_live_release_check(self.cur, ZZ)
        problems = v.release_column_problems(self.cur, ZZ)
        self.cur.execute("ROLLBACK TO SAVEPOINT t")
        self.assertIn("CHECK", problems[0])

    def test_a_held_release_is_information_not_a_fault(self):
        self.seed()
        self.e.file("4 March 2027", overrides=CHANGED)
        rc, text, _ = self.e.cmd(["load", "--commit"])
        self.assertEqual(rc, 0, text)
        ok, detail = v.real_not_live(self.cur, ZZ)
        self.assertTrue(ok, detail)
        self.assertIn(P27, detail)
        for fn in self.gates():
            ok, detail = fn(self.cur, ZZ)
            self.assertTrue(ok, detail)

    def test_codes_retired_or_unknown_fail(self):
        self.seed()
        live, ed = ZZ.live_table, ZZ.editions_table
        for sql, needle in (
                (f"UPDATE public.{live} SET statistical_neighbour_lad24cd = "
                 "'E08000038' WHERE lad24cd = 'E06000002' AND "
                 "neighbour_rank = 1", "E08000038"),
                (f"UPDATE public.{live} SET statistical_neighbour_lad24cd = "
                 "'E06009999' WHERE lad24cd = 'E06000002' AND "
                 "neighbour_rank = 1", "not in la_boundaries")):
            ok, detail = v.plant(self.cur, sql,
                                 lambda: v.real_codes(self.cur, ZZ))
            self.assertFalse(ok)
            self.assertIn(needle, detail)
        ok, detail = v.plant_ed(
            self.cur, ZZ, f"UPDATE public.{ed} SET "
            "statistical_neighbour_lad24cd = 'E08000039' WHERE lad24cd = "
            "'E06000002' AND publisher_rank = 2",
            lambda: v.real_codes(self.cur, ZZ))
        self.assertFalse(ok)
        self.assertIn("E08000039", detail)

    def test_counts_that_do_not_add_up_fail(self):
        self.seed()
        ok, detail = v.plant(
            self.cur, f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = "
            "'E06000002' AND neighbour_rank = 5",
            lambda: v.real_counts(self.cur, ZZ, subjects=N))
        self.assertFalse(ok)
        self.assertIn("rows each", detail)
        ok, detail = v.plant_ed(
            self.cur, ZZ, f"DELETE FROM public.{ZZ.editions_table} WHERE "
            "lad24cd = 'E06000002' AND publisher_rank = 7",
            lambda: v.real_counts(self.cur, ZZ, subjects=N))
        self.assertFalse(ok)
        self.assertIn("expected 20 rows", detail)
        ok, detail = v.real_counts(self.cur, ZZ, subjects=N + 1)
        self.assertFalse(ok)
        self.assertIn("subjects, expected", detail)

    def test_null_faults_fail(self):
        self.seed()
        ed = ZZ.editions_table
        for sqls, needle in (
                ([f"UPDATE public.{ed} SET england_rank = NULL WHERE "
                  "lad24cd = 'E06000002' AND publisher_rank = 2"],
                 "NULL england_rank"),
                ([f"UPDATE public.{ed} SET england_rank = 1 WHERE lad24cd = "
                  "'E06000001' AND publisher_rank = 3"], "not English"),
                ([f"UPDATE public.{ed} SET neighbour_rank = NULL, "
                  "null_reasons = NULL WHERE lad24cd = 'E06000002' AND "
                  "neighbour_rank = 2"], "outside the live rule"),
                ([f"ALTER TABLE public.{ed} ALTER COLUMN neighbour_name "
                  "DROP NOT NULL",
                  f"UPDATE public.{ed} SET neighbour_name = NULL WHERE "
                  "lad24cd = 'E06000002' AND publisher_rank = 3"],
                 "NULL neighbour_name"),
                ([f"UPDATE public.{ed} SET null_reasons = 'x' WHERE "
                  "lad24cd = 'E06000001' AND publisher_rank = 3"],
                 "null_reasons")):
            with self.subTest(needle=needle):
                ok, detail = v.plant_ed(self.cur, ZZ, sqls,
                                        lambda: v.real_nulls(self.cur, ZZ))
                self.assertFalse(ok, detail)
                self.assertIn(needle, detail)

    def test_shape_problems_name_a_missing_trigger_and_column(self):
        self.seed()
        self.cur.execute(f"DROP TRIGGER {ZZ.trigger} ON public."
                         f"{ZZ.editions_table}")
        self.assertTrue(any("append-only triggers" in p for p in
                            v.shape_problems(self.cur, ZZ)))
        self.cur.execute("SAVEPOINT t")
        self.cur.execute(f"ALTER TABLE public.{ZZ.editions_table} DROP "
                         "COLUMN publisher_rank CASCADE")
        self.assertTrue(any("publisher_rank missing" in p for p in
                            v.shape_problems(self.cur, ZZ)))
        self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_the_checks_refuse_each_bad_row_and_a_dropped_one_is_seen(self):
        self.seed()
        self.assertEqual(v.constraint_refusals(self.cur, ZZ), [])
        self.cur.execute("SAVEPOINT t")
        names = v._constraint_names(self.cur, ZZ.editions_table, "c")
        self.assertGreaterEqual(len(names), 5)
        for n in names:
            self.cur.execute(f"ALTER TABLE public.{ZZ.editions_table} DROP "
                             f"CONSTRAINT {n}")
        problems = v.constraint_refusals(self.cur, ZZ)
        self.cur.execute("ROLLBACK TO SAVEPOINT t")
        self.assertTrue(any("was accepted" in p for p in problems), problems)

    def test_the_note_lines_are_scan_safe_and_read_by_regex(self):
        self.seed()
        lines = v.note_lines_for(self.cur, ZZ)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith(
            f"before-state la_statistical_neighbours {m.LIVE_RELEASE} "
            f"rows={N_LIVE} sha256-first32="))
        self.assertTrue(lines[1].startswith("before-state-all "))
        self.assertTrue(lines[2].startswith("live-read "))
        for line in lines:
            self.assertIsNone(re.search(r"[0-9a-f]{64}", line), line)
            self.assertTrue(re.search(r"sha256-first32=[0-9a-f]{32} "
                                      r"sha256-last32=[0-9a-f]{32}$", line))
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("intro\n" + "\n".join(lines) + "\n",
                        encoding="utf-8")
        self.assertEqual(list(v.note_hashes(note)), [m.LIVE_RELEASE])
        self.assertEqual(list(v.all_note_hashes(note)), [m.LIVE_RELEASE])
        self.assertEqual(list(v.live_read_hashes(note)), [None])
        self.assertEqual(v.note_hashes(Path(self.tmpdir.name) / "none"), {})
        # the three hashes are different things
        hs = {x.split(" sha256-first32=")[1] for x in lines}
        self.assertEqual(len(hs), 3)
        # a 64-hex run (what the credential scan flags) does not parse
        note.write_text(f"before-state la_statistical_neighbours "
                        f"{m.LIVE_RELEASE} rows={N_LIVE} sha256="
                        + "a" * 64 + "\n", encoding="utf-8")
        self.assertEqual(v.note_hashes(note), {})

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
        note = Path(self.tmpdir.name) / "n.md"
        line = lines[0]
        note.write_text(line + "\n", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, N_LIVE)
        self.assertTrue(ok, detail)
        first = line.split("sha256-first32=")[1][:32]
        for text, needle in (
                (line.replace(first, "0" * 32), "differs"),
                (line.replace(f"rows={N_LIVE}", f"rows={N_LIVE + 1}"),
                 "differs"),
                (lines[1], "records no"),
                (lines[2], "records no"),
                (line + "\n" + line.replace(m.LIVE_RELEASE, "2019-01-01"),
                 "no edition 1 as loaded"),
                ("", "records no")):
            note.write_text(text, encoding="utf-8")
            ok, detail = v.real_edition1_hash(self.cur, ZZ, note, N_LIVE)
            self.assertFalse(ok, text)
            self.assertIn(needle, detail)
        ok, detail = v.real_edition1_hash(self.cur, ZZ,
                                          Path(self.tmpdir.name) / "x",
                                          N_LIVE)
        self.assertFalse(ok)
        self.assertIn("not written", detail)
        note.write_text(line + "\n", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, N_LIVE + 3)
        self.assertFalse(ok)
        self.assertIn("surveyed", detail)

    def test_edition_1_hash_fails_before_any_migration(self):
        self.seed()      # a load, not a migration: no edition 1 'as loaded'
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, N_LIVE)
        self.assertFalse(ok)
        self.assertIn("migrate-legacy not run", detail)

    def test_live_read_against_the_note(self):
        self.seed()
        lines = v.note_lines_for(self.cur, ZZ)
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, detail = v.real_live_read(self.cur, ZZ, note, rows=N_LIVE)
        self.assertTrue(ok, detail)
        # a changed source is not what the readers read; it is a stored
        # column, so before-state-all sees it and live-read does not
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET source = 'x'")
        ok, detail = v.real_live_read(self.cur, ZZ, note, rows=N_LIVE)
        self.assertFalse(ok)
        self.assertIn("before-state-all", detail)
        self.assertNotIn("live-read", detail)
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET neighbour_rank "
                         "= 9 WHERE lad24cd = 'E06000001' AND "
                         "neighbour_rank = 5")
        ok, detail = v.real_live_read(self.cur, ZZ, note, rows=N_LIVE)
        self.assertFalse(ok)
        self.assertIn("differs from the note's live-read", detail)
        for text in ("", lines[0], lines[2], lines[1]):
            note.write_text(text, encoding="utf-8")
            ok, detail = v.real_live_read(self.cur, ZZ, note, rows=N_LIVE)
            self.assertFalse(ok)
            self.assertIn("records no", detail)
        ok, detail = v.real_live_read(self.cur, ZZ,
                                      Path(self.tmpdir.name) / "x",
                                      rows=N_LIVE)
        self.assertIn("not written", detail)

    def test_the_migration_proof_reruns_from_the_file_found_by_sha256(self):
        path, files = self.migrated()
        raw = Path(self.tmpdir.name) / "raw"
        raw.mkdir()
        (raw / path.name).write_bytes(path.read_bytes())
        ok, detail = v.real_migration_proof(self.cur, ZZ, files, raw,
                                            subjects=N)
        self.assertTrue(ok, detail)
        self.assertIn("0 differences", detail)
        ok, detail = v.real_held_reread(self.cur, ZZ, files, raw, subjects=N)
        self.assertTrue(ok, detail)
        ok, detail = v.real_identity(self.cur, files, raw, ZZ, subjects=N,
                                     nations={"England": N, "Wales": 1,
                                              "Scotland": 1})
        self.assertTrue(ok, detail)
        ok, detail = v.real_rank(self.cur, files, raw, ZZ, subjects=N)
        self.assertTrue(ok, detail)
        self.assertIn("edition 1 records the same rank", detail)
        (raw / path.name).write_bytes(path.read_bytes() + b"\0")
        for fn in (v.real_migration_proof, v.real_held_reread):
            ok, detail = fn(self.cur, ZZ, files, raw, subjects=N)
            self.assertFalse(ok)
            self.assertIn("no file", detail)

    def test_a_file_that_is_not_edition_1_fails_the_proof(self):
        path, files = self.migrated()
        other = write_s21(Path(self.tmpdir.name) / "o" / path.name,
                          overrides=CHANGED)
        fx = {**files, "sha256": m.content_sha256(other)}
        ok, detail = v.real_migration_proof(self.cur, ZZ, fx, other.parent,
                                            subjects=N)
        self.assertFalse(ok)
        self.assertIn("differences", detail)
        ok, detail = v.real_held_reread(self.cur, ZZ, fx, other.parent,
                                        subjects=N)
        self.assertFalse(ok)
        self.assertIn("differs", detail)

    def test_identity_and_rank_from_a_file_on_disk(self):
        d = Path(self.tmpdir.name) / "disk"
        good = write_s21(d / "good.xlsx")
        fx = {"sha256": m.content_sha256(good)}
        nations = {"England": N, "Wales": 1, "Scotland": 1}
        ok, detail = v.real_identity(None, fx, d, subjects=N, nations=nations)
        self.assertTrue(ok, detail)
        ok, detail = v.real_rank(None, fx, d)
        self.assertTrue(ok, detail)
        # the surveyed nations (296 English subjects) are not this file's
        ok, detail = v.real_identity(None, fx, d)
        self.assertFalse(ok)
        self.assertIn("subjects by nation", detail)
        other = write_s21(d / "other.xlsx", published="12 March 2025")
        ok, detail = v.real_identity(None, {"sha256": m.content_sha256(other)},
                                     d, subjects=N, nations=nations)
        self.assertFalse(ok)
        self.assertIn("Cover date", detail)
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
        self.assertEqual(nums, list(range(1, 22)))

    def test_print_note_lines_rejects_other_arguments(self):
        with self.assertRaises(SystemExit):
            v.main(["--nope"])

    def test_the_throwaway_tables_exist_only_inside_the_transaction(self):
        self.seed()
        self.assertEqual(m.live_equals_tip(self.cur, ZZ, [P]), [])

    def test_the_rule_source_check_passes_the_loader_and_catches_breaks(self):
        self.assertEqual(v.rule_source_problems(LOADER_TEXT), [])
        for label, text in (
                ("a second assignment", LOADER_TEXT + "\nLIVE_RULE = None\n"),
                ("a global", LOADER_TEXT + "\ndef _x():\n    global "
                 "LIVE_RULE\n    LIVE_RULE = 1\n"),
                ("another reader", LOADER_TEXT + "\ndef _y(r):\n    return "
                 "r.take\n"),
                ("the English filter dropped", LOADER_TEXT.replace(
                    'english_prefixes=("E06", "E07", "E08", "E09")',
                    'english_prefixes=("E06", "W06", "S12")', 1)),
                ("a SQL filter", LOADER_TEXT + '\n_Q = "SELECT x FROM t '
                 'WHERE england_rank <= 5"\n'),
                ("a live writer without live_rows", LOADER_TEXT.replace(
                    "rows = live_rows(records)", "rows = []", 1))):
            with self.subTest(label=label):
                self.assertNotEqual(text, LOADER_TEXT)
                self.assertTrue(v.rule_source_problems(text))

    def test_the_geography_source_check(self):
        self.assertEqual(v.geography_source_problems(LOADER_TEXT), [])
        for label, text in (
                ("a private dict", LOADER_TEXT + '\nCODE_REMAP = '
                 '{"E08000038": "E08000016"}\n'),
                ("a query", LOADER_TEXT + '\n_Q = "SELECT * FROM '
                 'la_code_lookup"\n'),
                ("no resolve", LOADER_TEXT.replace("geography.resolve",
                                                   "geography.other"))):
            with self.subTest(label=label):
                self.assertTrue(v.geography_source_problems(text))
        doc = LOADER_TEXT + '\n\ndef _z():\n    """E08000038 la_code_lookup"""\n'
        self.assertEqual(v.geography_source_problems(doc), [])

    def test_no_old_build_is_imported_or_run(self):
        old = v.OLD_BUILD
        for label, text, strict in (
                ("an import", f"import {old}\n", False),
                ("a from import", f"from {old} import main\n", False),
                ("a subprocess", f"import subprocess\nsubprocess.run(['python', "
                 f"'scripts/{old}.py'])\n", False),
                ("runpy", f"import runpy\nrunpy.run_path('{old}.py')\n",
                 False),
                ("importlib", f"import importlib\nimportlib.import_module("
                 f"'{old}')\n", False),
                ("a string in a test", f"X = '{old}'\n", True)):
            with self.subTest(label=label):
                self.assertTrue(v.old_build_problems(text, strict))
        # a docstring or a label in the loader is not a run
        self.assertEqual(v.old_build_problems(
            f'"""the {old} script"""\nX = "{old} run log"\n'), [])
        self.assertEqual(v.old_build_problems(
            f'"""the {old} script"""\n', True), [])
        for rel, strict in v.OLD_BUILD_FILES:
            with self.subTest(file=rel):
                path = v.HERE / rel
                self.assertTrue(path.exists(), rel)
                self.assertEqual(v.old_build_problems(
                    path.read_text(encoding="utf-8"), strict), [])
        self.assertEqual([k for k in sys.modules if old in k], [])

    def test_every_gate_runs_and_only_a_pending_migration_fails(self):
        """The 21 gates on the throwaway tables. Against the real tables, a
        gate with a real part that has no editions table to read fails as
        pending migration, with the words 'not yet migrated'; after the
        migration none fails."""
        out = io.StringIO()
        before = len(v.RESULTS)
        mark = len(v.OUTPUT)
        # the leakage gate looks at what was printed: do not leave it in
        # the module's list
        with contextlib.redirect_stdout(out), \
                mock.patch.object(v, "REAL_BEFORE", {}):
            for gate in v.GATES:
                v.run_gate(self.cur, gate)
        results = v.RESULTS[before:]
        del v.RESULTS[before:]
        del v.OUTPUT[mark:]
        lines = [x for x in out.getvalue().splitlines()
                 if x.startswith("GATE ")]
        self.assertEqual(len(lines), 21, out.getvalue())
        failing = [x for x in lines if ": FAIL" in x]
        if v.exists(self.cur):
            self.assertEqual(failing, [])
        else:
            for x in failing:
                self.assertIn("FAIL (pending migration)", x)
                self.assertIn("not yet migrated", x)
        self.assertEqual(len(results), 21)

    def test_the_real_gates_pass_on_a_copy_of_the_real_data(self):
        """The editions built in the throwaway tables from a copy of the
        real live rows and the real held file (read-only on the real
        data): every real gate passes on 296 subjects, 5,920 editions rows
        and 1,480 live rows, as it must once Task 3 has migrated."""
        raw = m.RAW_DIR
        if v.held_file(raw_dir=raw) is None:
            self.skipTest("the held file is not on disk")
        cols = "lad24cd, statistical_neighbour_lad24cd, neighbour_rank, " \
               "source, loaded_at"
        real_live = v.REAL.live_table
        self.cur.execute(f"INSERT INTO public.{ZZ.live_table} ({cols}) "
                         f"SELECT {cols} FROM public.{real_live}")
        if self.cur.rowcount != m.LEGACY_LIVE[0]:
            self.skipTest("the live table is not as surveyed")
        with mock.patch.object(m, "SPEC", ZZ), \
                mock.patch.object(m, "EXPECTED_SUBJECTS", v.SUBJECTS):
            lines = v.note_lines_for(self.cur, ZZ)
            with contextlib.redirect_stdout(io.StringIO()):
                m.migrate_legacy(self.cur, v.held_file(raw_dir=raw),
                                 write=True)
            note = Path(self.tmpdir.name) / "note.md"
            note.write_text("\n".join(lines) + "\n", encoding="utf-8")
            checks = {
                "live rule": v.real_live_rule(self.cur, ZZ),
                "codes": v.real_codes(self.cur, ZZ),
                "counts": v.real_counts(self.cur, ZZ),
                "nulls": v.real_nulls(self.cur, ZZ),
                "identity": v.real_identity(self.cur, spec=ZZ),
                "rank": v.real_rank(self.cur, spec=ZZ),
                "re-read": v.real_held_reread(self.cur, ZZ),
                "not live": v.real_not_live(self.cur, ZZ),
                "edition 1": v.real_edition1_hash(self.cur, ZZ, note),
                "live read": v.real_live_read(self.cur, ZZ, note),
                "proof": v.real_migration_proof(self.cur, ZZ)}
            for name, (ok, detail) in checks.items():
                self.assertTrue(ok, f"{name}: {detail}")
            self.assertEqual(v.shape_problems(self.cur, ZZ), [])
            self.assertIn("1,480", checks["live read"][1])
            self.assertIn("296 subjects", checks["live rule"][1])
            self.assertIn("5,920", checks["counts"][1])
            self.assertIn("473 rows", checks["nulls"][1])
        self.assertEqual(v.real_snapshot(self.raw_second),
                         type(self).real_before)

    def test_core_is_the_engine_the_spec_uses(self):
        self.assertTrue(core.AS_LOADED_LABEL)
        self.assertIs(ZZ.live_single_period, True)


if __name__ == "__main__":
    unittest.main()
