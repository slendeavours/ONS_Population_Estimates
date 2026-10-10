"""Tests that the real-table gates of s13_lahs_editions_verify fail on a
short, empty, drifted or unmigrated state. The gate bodies (real_*) take a
spec, so they are called here against the throwaway zz_s13_* tables (created
inside a transaction that is always rolled back); no real table is touched,
and the S13 editions table is never created. Every test ends in a rollback
(also on an exception), and tearDownClass reads the database on a separate
read-only connection and fails if a zz_s13 table was left committed."""
import contextlib
import dataclasses
import io
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import s13_lahs_editions as m  # noqa: E402
import s13_lahs_editions_verify as v  # noqa: E402
import test_s13_lahs_loader as tl  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s13_lahs_pure import PREDECESSORS  # noqa: E402

ZZ = v.ZZ
N = tl.N
Y3 = ("2017-18", "2018-19", "2024-25")


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
            left = [t for t in v.leftovers() if t.startswith("zz_s13")]
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

    def seed(self, **kw):
        """A loaded 2018, 2024 and 2025; returns (csv, ods, files)."""
        csv_path, ods_path = self.e.files(**kw)
        self.e.ok(self.cur, ["load", "--commit"])
        return csv_path, ods_path, {"sha256": m.content_sha256(csv_path)}

    def migrated(self):
        feb, feb_ods = self.e.files(latest=v.FEB, register=False)
        self.e.n8n_live(self.cur, feb)
        pre = {p: m.records(self.cur, ZZ, p) for p in v.PERIODS}
        rc, text, _ = v.migrate_cmd(self.e, feb, "--commit")
        self.assertEqual(rc, 0, text)
        return feb, feb_ods, pre

    def clean_loader(self):
        p = Path(self.tmpdir.name) / "clean.py"
        p.write_text('"""A clean module."""\nX = 1\n', encoding="utf-8")
        return p

    def test_a_cursor_cannot_commit_or_roll_back_the_transaction(self):
        with self.assertRaises(RuntimeError):
            self.cur.connection.commit()
        with self.assertRaises(RuntimeError):
            self.cur.connection.rollback()

    def test_a_seeded_complete_state_passes_every_real_gate(self):
        csv_path, _, files = self.seed(years=Y3)
        for label, fn in (
                ("2", lambda: v.real_edition1_and_latest(self.cur, ZZ)),
                ("3", lambda: v.real_codes(
                    self.cur, ZZ, loader=self.clean_loader(),
                    csv_sha=files["sha256"], raw_dir=csv_path.parent,
                    dorset=False)),
                ("4", lambda: v.real_row_counts(self.cur, ZZ, n=N)),
                ("5", lambda: v.real_flags(self.cur, ZZ)),
                ("6", lambda: v.real_successors(self.cur, ZZ))):
            ok, detail = fn()
            self.assertTrue(ok, f"{label}: {detail}")
        self.assertEqual(v.shape_problems(self.cur, ZZ), [])

    def test_an_empty_state_is_not_a_pass(self):
        for fn in (v.real_edition1_and_latest, v.real_row_counts,
                   v.real_flags, v.real_successors):
            ok, detail = fn(self.cur, ZZ)
            self.assertFalse(ok, detail)
            self.assertIn("no live periods", detail)
        ok, detail = v.real_codes(self.cur, ZZ, loader=self.clean_loader(),
                                  dorset=False, csv_sha="0" * 64)
        self.assertFalse(ok)
        self.assertIn("no live periods", detail)

    def test_an_unmigrated_state_fails_with_a_clear_message(self):
        gone = dataclasses.replace(ZZ, editions_table="zz_s13_not_there")
        for fn in (v.real_edition1_and_latest, v.real_codes,
                   v.real_row_counts, v.real_flags, v.real_successors,
                   v.real_migration_proof, v.real_reread,
                   v.real_edition1_hash, v.real_w1_read, v.real_correction):
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
        for sql in (
                f"UPDATE public.{live} SET households_on_register = "
                "households_on_register + 1 WHERE lad24cd = 'E06000002'",
                f"UPDATE public.{live} SET reasonable_preference = NULL "
                "WHERE lad24cd = 'E06000002'",
                f"UPDATE public.{live} SET jointly_managed_register = NOT "
                "jointly_managed_register WHERE lad24cd = 'E06000002' AND "
                "reporting_year = 2024"):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")
            self.assertFalse(ok, sql)
            self.assertIn("edition-only", detail)

    def test_a_missing_live_row_and_a_missing_edition_1_fail(self):
        self.plant(f"DELETE FROM public.{ZZ.live_table} WHERE lad24cd = "
                   "'E06000002'", v.real_edition1_and_latest, "edition-only")

    def test_a_period_without_edition_1_fails(self):
        self.plant(f"UPDATE public.{ZZ.live_table} SET reporting_year = "
                   "2030 WHERE reporting_year = 2018",
                   v.real_edition1_and_latest, "no edition 1")

    def test_new_barnsley_code_a_stray_code_form_and_file_fail(self):
        csv_path, _, files = self.seed()
        live = ZZ.live_table
        kw = dict(csv_sha=files["sha256"], raw_dir=csv_path.parent,
                  loader=self.clean_loader(), dorset=False)
        for sql, needle in (
                (f"UPDATE public.{live} SET lad24cd = 'E08000038' WHERE "
                 "lad24cd = 'E08000016'", "Barnsley/Sheffield"),
                (f"UPDATE public.{live} SET lad24cd = 'E07999999' WHERE "
                 "lad24cd = 'E06000001'", "not in la_boundaries")):
            self.cur.execute("SAVEPOINT t")
            self.cur.execute(sql)
            ok, detail = v.real_codes(self.cur, ZZ, **kw)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")
            self.assertFalse(ok, detail)
            self.assertIn(needle, detail)
        ok, detail = v.real_codes(self.cur, ZZ, **dict(kw, form="new"))
        self.assertFalse(ok)
        self.assertIn("declared", detail)
        ok, detail = v.real_codes(self.cur, ZZ, **dict(kw,
                                                       csv_sha="0" * 64))
        self.assertFalse(ok)
        self.assertIn("no .csv", detail)
        # the real la_code_lookup is read: Dorset is checked unless asked not
        ok, detail = v.real_codes(self.cur, ZZ, **dict(kw, dorset=True))
        have = v.dorset_problems(self.cur)
        self.assertEqual(ok, not have, detail)

    def test_a_loader_with_a_private_table_fails_the_code_gate(self):
        csv_path, _, files = self.seed()
        bad = Path(self.tmpdir.name) / "bad.py"
        bad.write_text('LOOKUP_GAPS = {"E07000049": ("E06000059", "x")}\n',
                       encoding="utf-8")
        ok, detail = v.real_codes(self.cur, ZZ, csv_sha=files["sha256"],
                                  raw_dir=csv_path.parent, loader=bad,
                                  dorset=False)
        self.assertFalse(ok)
        self.assertIn("LOOKUP_GAPS", detail)

    def test_dorset_resolves_only_through_la_code_lookup(self):
        pub = {c: "E06000059" for c in v.DORSET}
        rows = v._Cur.LOOKUP + [(c, "E06000059", "new_unitary")
                                for c in v.DORSET]
        with mock.patch.object(m, "LOOKUP_GAPS", {}, create=True):
            rmap, problems = m.resolve_codes(v._Cur(lookup=rows),
                                             set(v.DORSET), "2018", pub)
            self.assertEqual(problems, [])
            self.assertEqual(set(rmap.values()), {"E06000059"})
            _, problems = m.resolve_codes(v._Cur(), set(v.DORSET), "2018",
                                          pub)
            self.assertEqual(len(problems), 5)
            self.assertTrue(all("UNEXPLAINED" in p for p in problems))

    def test_flags_nulls_zeros_and_reasons_are_checked(self):
        over = {("E06000002", "2017-18", "cc1a"): "[x]",
                ("E06000001", "2023-24", "cc5a"): "[z]"}
        self.seed(overrides=over)
        ok, detail = v.real_flags(self.cur, ZZ)
        self.assertTrue(ok, detail)
        self.assertIn("households_on_register: [x] 2 [z] 1", detail)
        self.assertIn("reasonable_preference: [x] 0 [z] 1", detail)
        e = ZZ.editions_table
        base = f"UPDATE public.{e} SET "
        for sql, needle in (
                (base + "households_on_register = 0 WHERE lad24cd = "
                 "'E06000002' AND reporting_year = 2018",
                 "a 0 where the flag says NULL"),
                (base + "value_flag = NULL WHERE lad24cd = 'E06000002' AND "
                 "reporting_year = 2018", "NULL without a reason"),
                (base + "value_flag = 'households_on_register=blank' WHERE "
                 "lad24cd = 'E06000002' AND reporting_year = 2018",
                 "not one the loader writes"),
                (base + "value_flag = 'households_on_register=part_missing' "
                 "WHERE lad24cd = 'E06000002' AND reporting_year = 2018",
                 "one source code"),
                (base + "value_flag = 'oops' WHERE lad24cd = 'E06000002' "
                 "AND reporting_year = 2018", "malformed")):
            with v.planted(self.cur, sql, editions=True):
                ok, detail = v.real_flags(self.cur, ZZ)
            self.assertFalse(ok, sql)
            self.assertIn(needle, detail)
        # the planted fault was rolled back, triggers and all
        ok, detail = v.real_flags(self.cur, ZZ)
        self.assertTrue(ok, detail)
        self.assertEqual(v.shape_problems(self.cur, ZZ), [])

    def test_successor_totals_are_null_exactly_when_a_part_is_missing(self):
        self.seed(years=Y3, overrides={
            ("E07000028", "2018-19", "cc1a"): "[x]"})
        ok, detail = v.real_successors(self.cur, ZZ)
        self.assertTrue(ok, detail)
        self.assertIn("2 NULL total", detail)
        e = ZZ.editions_table
        for sql, needle in (
                (f"UPDATE public.{e} SET households_on_register = 5 WHERE "
                 "lad24cd = 'E06000063' AND reporting_year = 2019",
                 "households_on_register is 5"),
                (f"UPDATE public.{e} SET value_flag = NULL WHERE lad24cd = "
                 "'E06000063' AND reporting_year = 2019",
                 "households_on_register is None")):
            with v.planted(self.cur, sql, editions=True):
                ok, detail = v.real_successors(self.cur, ZZ)
            self.assertFalse(ok, sql)
            self.assertIn(needle, detail)

    def test_a_stray_authority_count_fails(self):
        self.seed()
        ok, _ = v.real_row_counts(self.cur, ZZ, n=N)
        self.assertTrue(ok)
        ok, detail = v.real_row_counts(self.cur, ZZ, n=N + 1)
        self.assertFalse(ok)
        self.assertIn("authorities in", detail)
        self.cur.execute(f"DELETE FROM public.{ZZ.live_table} WHERE "
                         "lad24cd = 'E08000016' AND reporting_year = 2018")
        self.assertFalse(v.real_row_counts(self.cur, ZZ, n=N)[0])

    def test_the_editions_table_is_append_only(self):
        self.seed()
        for sql in (f"UPDATE public.{ZZ.editions_table} SET edition = 5",
                    f"DELETE FROM public.{ZZ.editions_table}",
                    f"TRUNCATE public.{ZZ.editions_table}"):
            self.cur.execute("SAVEPOINT t")
            with self.assertRaises(Exception) as ex:
                self.cur.execute(sql)
            self.assertIn("append-only", str(ex.exception))
            self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_the_source_check_catches_each_planted_construct(self):
        p = Path(self.tmpdir.name) / "x.py"
        p.write_text(
            '"""E08000038 and E07000049 in a docstring"""\n'
            'TELFORD_NO_REGISTER = {"code": "E06000020", "first_year": 2022,'
            ' "column": "c"}\n'
            'LOOKUP_GAPS = {"E07000049": ("E06000059", "x")}\n'
            'MY_MAP = {"E07000050": "E06000059"}\n'
            'OTHER_RULE = {"code": "E06000001", "first_year": 1, '
            '"column": "c"}\n'
            'MSG = "E08000016 in a message"\n'
            'def f(r):\n    return r == "E06000001" and r["h"] == 0\n'
            'def g(r):\n    return r["h"] == 0\n'
            'def h(v):\n    return v or 0\n'
            'def i(v):\n    return v or 0  # not a source value\n'
            'def j(d):\n    return d.get("x", 0)\n'
            'def k(v):\n    return v if v else 0\n'
            'Q = "SELECT COALESCE(x, 0) FROM t"\n',
            encoding="utf-8")
        plain = v._lines_of(v._source_problems(p))
        self.assertEqual(plain, {3, 4, 5, 6, 8})
        zeros = v._lines_of(v._source_problems(p, zeros=True))
        self.assertEqual(zeros, {3, 4, 5, 6, 8, 12, 16, 18, 19})

    def test_the_real_loader_has_only_the_pending_dorset_constant(self):
        problems = v._source_problems(v.LOADER, zeros=True)
        self.assertTrue(v.pending_dorset_only(problems), problems)
        self.assertEqual(v.zero_rule_state(), [])

    def test_a_third_zero_rule_or_a_changed_rule_is_caught(self):
        extra = dict(m.TELFORD_NO_REGISTER, name="X")
        with mock.patch.object(m, "ZERO_RULES", m.ZERO_RULES + (extra,)):
            self.assertTrue(v.zero_rule_state())
        wrong = dict(m.ALLERDALE_ZERO, column="reasonable_preference")
        with mock.patch.object(m, "ZERO_RULES",
                               (m.TELFORD_NO_REGISTER, wrong)):
            self.assertTrue(v.zero_rule_state())
        # a rule not extended to cc5a is caught
        narrow = dict(m.TELFORD_NO_REGISTER, extra_columns={})
        with mock.patch.object(m, "ZERO_RULES",
                               (narrow, m.ALLERDALE_ZERO)):
            self.assertTrue(any("cc5a" in b for b in v.zero_rule_state()))

    def test_the_note_lines_are_read_by_regex_and_are_scan_safe(self):
        note = Path(self.tmpdir.name) / "n.md"
        h = "ab" * 32
        note.write_text(
            f"before-state la_housing_register 2025 rows=296 "
            f"{v.hash_fields(h)}\nbefore-state-all la_housing_register 2025 "
            f"rows=296 {v.hash_fields('cd' * 32)}\nw1-read "
            f"la_housing_register 2025 rows=296 {v.hash_fields('ef' * 32)}\n"
            f"w1-read-all la_housing_register 2025 rows=296 "
            f"{v.hash_fields('01' * 32)}\n", encoding="utf-8")
        self.assertEqual(v.note_hashes(note), {"2025": (296, h)})
        self.assertEqual(v.w1_note_hashes(note), {"2025": (296, "ef" * 32)})
        self.assertEqual(v.note_hashes(Path(self.tmpdir.name) / "none"), {})
        self.assertNotRegex(note.read_text(encoding="utf-8"),
                            r"[0-9a-f]{64}")

    def test_the_hashes_see_each_column_they_are_defined_on(self):
        rows = [{"lad24cd": "E06000001", "households_on_register": 5,
                 "jointly_managed_register": None,
                 "reasonable_preference": 3, "live_source": "s"}]
        base = v.live_columns_sha(rows)
        for col, val in (("households_on_register", 6),
                         ("jointly_managed_register", False),
                         ("reasonable_preference", 4),
                         ("live_source", "t"), ("lad24cd", "E06000002")):
            self.assertNotEqual(v.live_columns_sha([dict(rows[0],
                                                         **{col: val})]),
                                base, col)
        self.assertNotEqual(v.live_columns_sha(rows),
                            v.w1_read_sha(rows))
        w1 = v.w1_read_sha(rows)
        self.assertEqual(v.w1_read_sha([dict(rows[0], reasonable_preference=9,
                                             live_source="z")]), w1)
        self.assertNotEqual(v.w1_read_sha([dict(rows[0],
                                                households_on_register=6)]),
                            w1)

    def test_edition_1_hash_against_the_note_and_live_before_migration(self):
        feb, _, pre = self.migrated()
        note = Path(self.tmpdir.name) / "n.md"
        lines = [f"before-state la_housing_register {p} rows={len(r)} "
                 f"{v.hash_fields(v.live_columns_sha(r))}"
                 for p, r in sorted(pre.items())]
        kw = dict(legacy_rows=3 * N, years=3)
        note.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, **kw)
        self.assertTrue(ok, detail)
        h = v.live_columns_sha(pre["2025"])
        for text, needle in (
                ("\n".join(lines).replace(h[:32], "0" * 32), "differs"),
                ("\n".join(lines).replace(f"rows={N}", f"rows={N + 1}"),
                 "differs"),
                ("\n".join(lines[:2]) + "\nbefore-state-all "
                 + lines[2][len("before-state "):], "records no"),
                ("\n".join(lines + [lines[0].replace("2018", "2019")]),
                 "no edition 1 as loaded"),
                ("", "records no")):
            note.write_text(text, encoding="utf-8")
            ok, detail = v.real_edition1_hash(self.cur, ZZ, note, **kw)
            self.assertFalse(ok, text)
            self.assertIn(needle, detail)
        note.write_text("\n".join(lines), encoding="utf-8")
        for kw2, needle in ((dict(legacy_rows=3 * N + 5, years=3), "surveyed"),
                            (dict(legacy_rows=3 * N, years=4), "surveyed")):
            ok, detail = v.real_edition1_hash(self.cur, ZZ, note, **kw2)
            self.assertFalse(ok)
            self.assertIn(needle, detail)
        ok, detail = v.real_edition1_hash(self.cur, ZZ,
                                          Path(self.tmpdir.name) / "x", **kw)
        self.assertFalse(ok)
        self.assertIn("not written", detail)

    def test_edition_1_hash_fails_before_any_migration(self):
        self.seed()      # a load, not a migration: no edition 1 'as loaded'
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, 3 * N, 3)
        self.assertFalse(ok)
        self.assertIn("migrate-legacy not run", detail)

    def test_note_lines_for_the_live_table_round_trip(self):
        self.migrated()
        lines = v.note_lines_for(self.cur, ZZ)
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[-1].startswith("w1-read la_housing_register "
                                             "2025 rows="))
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("\n".join(lines), encoding="utf-8")
        self.assertEqual(sorted(v.note_hashes(note)),
                         ["2018", "2024", "2025"])
        ok, detail = v.real_w1_read(self.cur, ZZ, note, rows=N)
        self.assertTrue(ok, detail)

    def test_the_w1_read_follows_households_only(self):
        self.seed(latest=v.FEB)
        note = Path(self.tmpdir.name) / "n.md"
        line = (f"w1-read la_housing_register 2025 rows={N} " + v.hash_fields(
            v.w1_read_sha(m.records(self.cur, ZZ, "2025"))))
        note.write_text(line + "\n", encoding="utf-8")
        ok, detail = v.real_w1_read(self.cur, ZZ, note, rows=N)
        self.assertTrue(ok, detail)
        self.e.files(overrides={("E06000002", "2024-25", "cc5a"): "9"},
                     ods={"overrides": {("E06000002", "cc5a"): 9}})
        self.e.ok(self.cur, ["load", "--commit"])
        self.e.ok(self.cur, ["refresh-latest", "--commit"])
        ok, detail = v.real_w1_read(self.cur, ZZ, note, rows=N)
        self.assertTrue(ok, detail)
        first = tl.live_val(self.cur, "E06000002")
        self.e.files(latest="2 July 2026",
                     overrides={("E06000002", "2024-25", "cc1a"):
                                str(first + 1)},
                     ods={"overrides": {("E06000002", "cc1a"): first + 1}})
        self.e.ok(self.cur, ["load", "--commit", "--acknowledge", "2025"])
        self.e.ok(self.cur, ["refresh-latest", "--commit"])
        ok, detail = v.real_w1_read(self.cur, ZZ, note, rows=N)
        self.assertFalse(ok)
        self.assertIn("differs", detail)
        for text, needle in ((line.replace("w1-read", "w1-read-all"),
                              "records no"),
                             (line.replace(f"rows={N}", f"rows={N + 1}"),
                              "records no")):
            note.write_text(text + "\n", encoding="utf-8")
            ok, detail = v.real_w1_read(self.cur, ZZ, note, rows=N)
            self.assertFalse(ok)
        ok, detail = v.real_w1_read(self.cur, ZZ, Path(self.tmpdir.name)
                                    / "x", rows=N)
        self.assertFalse(ok)
        self.assertIn("not written", detail)

    def test_the_migration_proof_runs_from_the_file_and_catches_changes(self):
        feb, _, _ = self.migrated()
        files = self.e.legacy(feb)
        ok, detail = v.real_migration_proof(self.cur, ZZ, files)
        self.assertTrue(ok, detail)
        self.assertIn("0 differences", detail)
        ok, detail = v.real_migration_proof(
            self.cur, ZZ, {**files, "sha256": "0" * 64})
        self.assertFalse(ok)
        self.assertIn("no file", detail)
        ok, detail = v.real_migration_proof(self.cur, ZZ, files,
                                            expect={"not_first": 9,
                                                    "from_june": 0,
                                                    "telford": 0})
        self.assertFalse(ok)
        self.assertIn("surveyed", detail)
        for sql in (f"UPDATE public.{ZZ.editions_table} SET "
                    "households_on_register = households_on_register + 1 "
                    "WHERE lad24cd = 'E06000002' AND reporting_year = 2024",
                    f"UPDATE public.{ZZ.editions_table} SET "
                    "predecessor_codes = 'E06000999' WHERE lad24cd = "
                    "'E06000002' AND reporting_year = 2024",
                    f"UPDATE public.{ZZ.editions_table} SET value_flag = "
                    "NULL WHERE lad24cd = 'E06000002' AND reporting_year = "
                    "2024"):
            with v.planted(self.cur, sql, editions=True):
                ok, detail = v.real_migration_proof(self.cur, ZZ, files)
            self.assertFalse(ok, sql)
            self.assertIn("differences", detail)
        other, _ = self.e.files(register=False, latest=v.FEB, overrides={
            ("E06000002", "2023-24", "cc1a"): "7"})
        ok, detail = v.real_migration_proof(
            self.cur, ZZ, {**files, "where": str(other),
                           "sha256": m.content_sha256(other)})
        self.assertFalse(ok)
        self.assertIn("differences", detail)

    def test_the_reread_of_a_loaded_file_is_equal_and_a_change_is_not(self):
        csv_path, ods_path, files = self.seed()
        ok, detail = v.real_reread(self.cur, ZZ, files, csv_path.parent)
        self.assertTrue(ok, detail)
        self.assertIn("3 year(s)", detail)
        ok, detail = v.real_reread(self.cur, ZZ, {"sha256": "0" * 64},
                                   csv_path.parent)
        self.assertFalse(ok)
        self.assertIn("no .csv", detail)
        other, other_ods = self.e.files(
            register=False, overrides={("E06000002", "2023-24", "cc5a"):
                                       "3"})
        ok, detail = v.real_reread(self.cur, ZZ,
                                   {"sha256": m.content_sha256(other)},
                                   other.parent)
        self.assertFalse(ok)
        self.assertIn("no edition was stored", detail)

    def test_the_cross_check_and_identity_run_from_files_on_disk(self):
        csv_path, ods_path = self.e.files(register=False)
        sha = {"sha256": m.content_sha256(csv_path)}
        ok, detail = v.real_cross_check(sha, csv_path.parent)
        self.assertTrue(ok, detail)
        self.assertIn("all 5 authorities", detail)
        ok, detail = v.real_identity(None, sha, csv_path.parent)
        self.assertTrue(ok, detail)
        self.assertIn("2026-06-25", detail)
        ok, detail = v.real_identity(None, sha, csv_path.parent,
                                     rank=date(2026, 2, 12))
        self.assertFalse(ok)
        self.assertIn("Latest update", detail)
        bad_csv, _ = self.e.files(register=False, ods={
            "overrides": {("E06000002", "cc1a"): 5}})
        ok, detail = v.real_cross_check({"sha256": m.content_sha256(bad_csv)},
                                        bad_csv.parent)
        self.assertFalse(ok)
        self.assertIn("cc1a", detail)
        ok, detail = v.real_cross_check({"sha256": "0" * 64}, bad_csv.parent)
        self.assertFalse(ok)
        self.assertIn("no .csv", detail)

    def test_the_named_correction_is_well_formed_and_recorded(self):
        self.assertEqual(v.correction_problems_static(), [])
        with mock.patch.dict(m.ACKNOWLEDGED_CORRECTIONS, {
                v.CORRECTION: dict(m.ACKNOWLEDGED_CORRECTIONS[v.CORRECTION],
                                   periods={"2025": {"cc5a_filled": 1,
                                                     "predecessor_sum": 1}})}):
            bad = v.correction_problems_static()
        self.assertTrue(any("2025" in b for b in bad), bad)
        # the cc5a correction: pinned to the June file, seven years, one
        # cell each; any other shape is caught
        for change in ({"file_sha256": "0" * 64},
                       {"file_sha256": None},
                       {"periods": {"2025": {"cc5a_zero_rule": 1}}},
                       {"periods": dict(m.ACKNOWLEDGED_CORRECTIONS[
                           v.CC5A_CORRECTION]["periods"],
                           **{"2024": {"cc5a_zero_rule": 2}})}):
            with mock.patch.dict(m.ACKNOWLEDGED_CORRECTIONS, {
                    v.CC5A_CORRECTION: dict(m.ACKNOWLEDGED_CORRECTIONS[
                        v.CC5A_CORRECTION], **change)}):
                self.assertTrue(v.correction_problems_static(), change)
        self.seed()
        ok, detail = v.real_correction(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("not yet loaded", detail)

    def test_the_cc5a_zero_cells_gate_needs_the_corrected_state(self):
        self.seed()
        ok, detail = v.real_zero_cells(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("expected NULL", detail)
        gone = dataclasses.replace(ZZ, editions_table="zz_s13_not_there")
        with self.assertRaises(ValueError):
            v.real_zero_cells(self.cur, gone)

    def test_the_flag_reader_parses_and_rejects(self):
        self.assertEqual(v.parse_flag(None), [])
        self.assertEqual(v.parse_flag("a=b; c=d"), [("a", "b"), ("c", "d")])
        for bad in ("a", "a="):
            with self.assertRaises(ValueError):
                v.parse_flag(bad)

    def test_the_throwaway_tables_exist_only_inside_the_transaction(self):
        self.seed()
        self.assertEqual(core.rows_differing(self.cur, ZZ, "2025", 1), 0)
        self.assertTrue(re.fullmatch(r"zz_s13_[a-z_]+", ZZ.editions_table))


if __name__ == "__main__":
    unittest.main()
