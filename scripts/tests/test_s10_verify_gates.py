"""Tests that the real-table gates of s10_rough_sleeping_editions_verify fail
on a short, empty, drifted or unmigrated state. The gate bodies (real_*) take
a spec, so they are called here against the throwaway zz_s10_* tables
(created inside a transaction that is always rolled back); no real table is
touched, and the S10 editions table is never created. Every test ends in a
rollback (also on an exception), and tearDownClass reads the database on a
separate read-only connection and fails if a zz_s10 table was left
committed."""
import contextlib
import dataclasses
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import s10_rough_sleeping_editions as m  # noqa: E402
import s10_rough_sleeping_editions_verify as v  # noqa: E402
import test_s10_rough_sleeping_loader as tl  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s10_rough_sleeping_pure import CODES, write_file  # noqa: E402

ZZ = v.ZZ
P = tl.P
N = tl.N


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
            left = [t for t in v.leftovers() if t.startswith("zz_s10")]
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
        self.stack.enter_context(tl.specs())
        self.addCleanup(self.stack.close)
        self.e = v.Env(self.cur, self.tmpdir.name)

    def rollback(self):
        self.raw.close()
        self.conn.rollback()

    def seed(self):
        """A loaded 2025; returns (path, files, raw dir)."""
        path = self.e.file()
        self.e.ok(self.cur, ["load", "--commit"])
        return path, {"sha256": m.content_sha256(path)}, path.parent

    def migrated(self):
        path, files = self.e.seed_legacy(self.cur)
        with mock.patch.object(m, "LEGACY_LIVE", self.e.legacy(self.cur)), \
                mock.patch.object(m, "LEGACY_FILE", files):
            rc, text, _ = self.e.cmd(["migrate-legacy", path, "--commit"])
        self.assertEqual(rc, 0, text)
        raw = Path(self.tmpdir.name) / "raw"
        raw.mkdir()
        (raw / path.name).write_bytes(path.read_bytes())
        return path, files, raw

    def test_a_cursor_cannot_commit_or_roll_back_the_transaction(self):
        with self.assertRaises(RuntimeError):
            self.cur.connection.commit()
        with self.assertRaises(RuntimeError):
            self.cur.connection.rollback()

    def test_a_seeded_complete_state_passes_every_real_gate(self):
        _, files, raw = self.seed()
        for fn in (v.real_edition1_and_latest,
                   lambda c, s: v.real_codes(c, s, files=files, raw_dir=raw),
                   lambda c, s: v.real_row_counts(c, s, n=len(CODES)),
                   lambda c, s: v.real_values(c, s, files, raw)):
            ok, detail = fn(self.cur, ZZ)
            self.assertTrue(ok, detail)
        self.assertEqual(v.shape_problems(self.cur, ZZ), [])

    def test_an_empty_state_is_not_a_pass(self):
        for fn in (v.real_edition1_and_latest, v.real_codes,
                   v.real_row_counts, v.real_values):
            ok, detail = fn(self.cur, ZZ)
            self.assertFalse(ok, detail)
            self.assertIn("no live periods", detail)

    def test_an_unmigrated_state_fails_with_a_clear_message(self):
        gone = dataclasses.replace(ZZ, editions_table="zz_s10_not_there")
        for fn in (v.real_edition1_and_latest, v.real_codes,
                   v.real_row_counts, v.real_values,
                   v.real_migration_proof, v.real_held_reread,
                   v.real_edition1_hash):
            with self.assertRaises(ValueError) as e:
                fn(self.cur, gone)
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
            v.mixed(99, "x", True, "seeded fine", self.cur,
                    lambda c: (True, "real fine"))
            v.mixed(98, "y", False, "seeded broke", self.cur,
                    lambda c: (True, "never called"))
        del v.RESULTS[before:]
        text = out.getvalue()
        self.assertIn("GATE 99 x: PASS", text)
        self.assertIn("GATE 98 y: FAIL", text)
        self.assertNotIn("pending", text)

    def plant(self, sql, fn, needle):
        self.seed()
        self.cur.execute(sql)
        ok, detail = fn(self.cur, ZZ)
        self.assertFalse(ok, detail)
        self.assertIn(needle, detail)

    def test_drifted_live_cells_fail(self):
        self.seed()
        live = ZZ.live_table
        for col in ("rough_sleeping", "rough_sleeping_prev_year"):
            with self.subTest(col=col):
                self.cur.execute("SAVEPOINT t")
                self.cur.execute(f"UPDATE public.{live} SET {col} = {col} + "
                                 "1 WHERE lad24cd = 'E06000002'")
                ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
                self.cur.execute("ROLLBACK TO SAVEPOINT t")
                self.assertFalse(ok, detail)
                self.assertIn("edition-only", detail)

    def test_a_missing_live_row_and_a_missing_edition_1_fail(self):
        self.plant(f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = "
                   "'E06000002'", v.real_edition1_and_latest, "edition-only")

    def test_a_period_without_edition_1_fails(self):
        self.plant(f"UPDATE public.{ZZ.live_table} SET snapshot_year = 2030",
                   v.real_edition1_and_latest, "no edition 1")

    def test_new_barnsley_code_and_a_stray_code_fail(self):
        _, files, raw = self.seed()
        live = ZZ.live_table
        for sql, needle in (
                (f"UPDATE public.{live} SET lad24cd = 'E08000038' WHERE "
                 "lad24cd = 'E08000016'", "Barnsley/Sheffield"),
                (f"UPDATE public.{live} SET lad24cd = 'E07999999' WHERE "
                 "lad24cd = 'E06000001'", "not in la_boundaries")):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            ok, detail = v.real_codes(self.cur, ZZ, files=files, raw_dir=raw)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")
            self.assertFalse(ok, detail)
            self.assertIn(needle, detail)

    def test_the_declared_form_and_the_held_file_are_checked(self):
        _, files, raw = self.seed()
        ok, detail = v.real_codes(self.cur, ZZ, form="old", files=files,
                                  raw_dir=raw)
        self.assertFalse(ok)
        self.assertIn("declared", detail)
        ok, detail = v.real_codes(self.cur, ZZ, files={"sha256": "0" * 64},
                                  raw_dir=raw)
        self.assertFalse(ok)
        self.assertIn("no file", detail)

    def test_a_stray_authority_count_fails(self):
        self.seed()
        ok, _ = v.real_row_counts(self.cur, ZZ, n=len(CODES))
        self.assertTrue(ok)
        ok, detail = v.real_row_counts(self.cur, ZZ, n=len(CODES) + 1)
        self.assertFalse(ok)
        self.assertIn("authorities in", detail)
        self.cur.execute(f"DELETE FROM public.{ZZ.live_table} WHERE "
                         "lad24cd = 'E08000019'")
        self.assertFalse(v.real_row_counts(self.cur, ZZ, n=len(CODES))[0])

    def test_nulls_negatives_and_other_sums_fail(self):
        _, files, raw = self.seed()
        live = ZZ.live_table
        for sql, needle in (
                (f"UPDATE public.{live} SET rough_sleeping = NULL WHERE "
                 "lad24cd = 'E06000002'", "NULL value"),
                (f"UPDATE public.{live} SET rough_sleeping_prev_year = NULL "
                 "WHERE lad24cd = 'E06000002'", "NULL value"),
                (f"UPDATE public.{live} SET rough_sleeping = -1 WHERE "
                 "lad24cd = 'E06000002'", "negative")):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            ok, detail = v.real_values(self.cur, ZZ, files, raw)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")
            self.assertFalse(ok, detail)
            self.assertIn(needle, detail)
        other = write_file(Path(self.tmpdir.name) / "o" / "f.ods", 2025,
                           values={("E06000002", 2025): 99})
        ok, detail = v.real_values(self.cur, ZZ,
                                   {"sha256": m.content_sha256(other)},
                                   other.parent)
        self.assertFalse(ok)
        self.assertIn("England row", detail)

    def test_the_editions_table_refuses_a_null_value(self):
        self.seed()
        self.cur.execute("SAVEPOINT t")
        with self.assertRaises(Exception):
            self.cur.execute(
                f"INSERT INTO public.{ZZ.editions_table} (lad24cd, "
                "snapshot_year, edition, rough_sleeping, "
                "rough_sleeping_prev_year, release_label, published_date, "
                "source_file, source_sha256) VALUES ('E06000001', 2030, 1, "
                "NULL, 1, 'x', now(), 'x', 'x')")
        self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_the_source_check_catches_each_planted_construct(self):
        p = Path(self.tmpdir.name) / "x.py"
        p.write_text(
            '"""E08000038 in a docstring; la_code_lookup too"""\n'
            'X = {"a": "E08000038"}\n'
            'Q = "SELECT old_code FROM la_code_lookup"\n'
            'def f(v):\n    return v or 0\n'
            'def g(v):\n    return v or 0  # not a source value\n'
            'C = "SELECT COALESCE(x, 0) FROM t"\n'
            'D = "a message about la_code_lookup recode"\n',
            encoding="utf-8")
        self.assertEqual(len(v._source_problems(p)), 2)
        self.assertEqual(len(v._source_problems(p, zeros=True)), 4)
        self.assertEqual(v._source_problems(v.LOADER, zeros=True), [])

    def test_the_before_state_note_is_read_by_regex(self):
        note = Path(self.tmpdir.name) / "n.md"
        h = "ab" * 32
        note.write_text(f"before-state la_rough_sleeping {P} rows=296 "
                        f"{v.hash_fields(h)}\nbefore-state-all "
                        f"la_rough_sleeping {P} rows=296 "
                        f"{v.hash_fields('cd' * 32)}\n", encoding="utf-8")
        self.assertEqual(v.note_hashes(note), {P: (296, h)})
        self.assertEqual(v.note_hashes(Path(self.tmpdir.name) / "none"), {})
        # the note's hash fields hold no 64-hex run (credential-scan safe)
        self.assertNotRegex(note.read_text(encoding="utf-8"),
                            r"[0-9a-f]{64}")

    def test_edition_1_hash_against_the_note(self):
        self.migrated()
        h = m.rows_content_sha(m.records(self.cur, ZZ, P, 1))
        note = Path(self.tmpdir.name) / "n.md"
        line = (f"before-state la_rough_sleeping {P} rows={N} "
                f"{v.hash_fields(h)}")
        note.write_text(line + "\n", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, N)
        self.assertTrue(ok, detail)
        for text, needle in (
                (line.replace(h[:32], "0" * 32), "differs"),
                (line.replace(f"rows={N}", f"rows={N + 1}"), "differs"),
                (f"before-state-all la_rough_sleeping {P} rows={N} "
                 f"{v.hash_fields(h)}", "records no"),
                (line + "\n" + line.replace(P, "2024"),
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

    def test_the_migration_proof_and_reread_run_from_the_file_by_sha256(self):
        path, files, raw = self.migrated()
        ok, detail = v.real_migration_proof(self.cur, ZZ, files, raw)
        self.assertTrue(ok, detail)
        self.assertIn("0 differences", detail)
        ok, detail = v.real_held_reread(self.cur, ZZ, files, raw)
        self.assertTrue(ok, detail)
        self.assertIn("published zeros kept as 0", detail)
        self.assertEqual(v.reread_after_migration(self.e, path), [])
        (raw / path.name).write_bytes(path.read_bytes() + b"\0")
        for fn in (v.real_migration_proof, v.real_held_reread):
            ok, detail = fn(self.cur, ZZ, files, raw)
            self.assertFalse(ok)
            self.assertIn("no file", detail)

    def test_a_file_that_is_not_edition_1_fails_the_proof_and_the_reread(self):
        path, files, _ = self.migrated()
        other = write_file(Path(self.tmpdir.name) / "o" / path.name, 2025,
                           values={("E06000002", 2025): 40})
        fx = {**files, "sha256": m.content_sha256(other)}
        ok, detail = v.real_migration_proof(self.cur, ZZ, fx, other.parent)
        self.assertFalse(ok)
        self.assertIn("differences", detail)
        ok, detail = v.real_held_reread(self.cur, ZZ, fx, other.parent)
        self.assertFalse(ok)
        self.assertIn("differs", detail)

    def test_the_held_file_cells_and_identity_from_a_file_on_disk(self):
        d = Path(self.tmpdir.name) / "disk"
        good = write_file(d / "good.ods", 2025)
        fx = {"sha256": m.content_sha256(good)}
        ok, detail = v.real_cells(fx, d)
        self.assertTrue(ok, detail)
        self.assertIn("published zeros 1 in 2025", detail)
        ok, detail = v.real_identity(None, fx, d)       # the surveyed rank
        self.assertTrue(ok, detail)
        bad = write_file(d / "bad.ods", 2025, values={("E06000002", 2012):
                                                      "[x]"})
        ok, detail = v.real_cells({"sha256": m.content_sha256(bad)}, d)
        self.assertFalse(ok)
        self.assertIn("Not Available", detail)
        ok, detail = v.real_cells({"sha256": "0" * 64}, d)
        self.assertFalse(ok)
        self.assertIn("no file", detail)
        old = write_file(d / "old.ods", 2025,
                         published=m.LEGACY_RANK[0].replace(day=1))
        ok, detail = v.real_identity(None, {"sha256": m.content_sha256(old)},
                                     d)
        self.assertFalse(ok)
        self.assertIn("rank", detail)

    def test_the_throwaway_tables_exist_only_inside_the_transaction(self):
        self.seed()
        self.assertEqual(core.rows_differing(self.cur, ZZ, P, 1), 0)


if __name__ == "__main__":
    unittest.main()
