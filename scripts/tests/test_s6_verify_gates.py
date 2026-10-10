"""Tests that the real-table gates of s6_asylum_editions_verify fail on a
short, empty, drifted or unmigrated state. The gate bodies (real_*) take the
four specs as a dict, so they are called here against the throwaway zz_s6_*
tables (created inside a transaction that is always rolled back); no real
table is written, and the S6 editions tables are never created. Every test
ends in a rollback (also on an exception), and tearDownClass reads the
database on a separate read-only connection and fails if a zz_s6 table was
left committed."""
import contextlib
import dataclasses
import io
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import s6_asylum_editions as m  # noqa: E402
import s6_asylum_editions_verify as v  # noqa: E402
import test_s6_asylum_loader as tl  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s6_asylum_pure import (Q3, Q4, d11_rows, write_d09,  # noqa: E402
                                 write_d11, write_reg02)

ZZ = v.ZZ
P_MAR = v.P_MAR


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
            left = [t for t in v.leftovers() if t.startswith("zz_s6")]
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
        self.seeded = False

    def rollback(self):
        self.raw.close()
        self.conn.rollback()

    def seed(self):
        if not self.seeded:
            self.e.seed_all()
            self.seeded = True

    def gates(self):
        return (v.real_edition1_and_latest, v.real_provenance, v.real_codes,
                v.real_rule1)

    def plant(self, sql, fn, needle):
        """Seed, run one planted statement in a savepoint, and assert the
        gate fails naming `needle`."""
        self.seed()
        self.cur.execute("SAVEPOINT plant")
        try:
            self.cur.execute(sql)
            ok, detail = fn(self.cur, ZZ)
            self.assertFalse(ok, detail)
            self.assertIn(needle, detail)
        finally:
            self.cur.execute("ROLLBACK TO SAVEPOINT plant")
            self.cur.execute("RELEASE SAVEPOINT plant")

    def migrated(self):
        paths, legacy, files = self.e.seed_legacy(self.cur)
        rc, text, _ = v.mig(self.e, paths, legacy, files)
        self.assertEqual(rc, 0, text)
        return paths, legacy, files

    def raw_copy(self, paths):
        raw = Path(self.tmpdir.name) / "raw"
        raw.mkdir(exist_ok=True)
        for p in paths:
            (raw / p.name).write_bytes(p.read_bytes())
        return raw

    # ------------------------------------------------------------ basics

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
        for t in m.TAGS:
            self.assertEqual(v.shape_problems(self.cur, ZZ[t]), [], t)

    def test_an_empty_state_is_not_a_pass(self):
        for fn in self.gates():
            ok, detail = fn(self.cur, ZZ)
            self.assertFalse(ok, detail)
            self.assertTrue("no live periods" in detail
                            or "an empty table is not a pass" in detail,
                            detail)

    def test_an_unmigrated_state_fails_with_a_clear_message(self):
        gone = dict(ZZ, support=dataclasses.replace(
            ZZ["support"], editions_table="zz_s6_not_there"))
        for fn in self.gates():
            with self.assertRaises(ValueError) as e:
                fn(self.cur, gone)
            self.assertIn("does not exist", str(e.exception))
        self.assertTrue(v.shape_problems(self.cur, gone["support"]))
        self.assertFalse(v.exists(self.cur, gone))
        self.assertTrue(v.exists(self.cur, ZZ))

    def test_the_real_editions_tables_are_never_created(self):
        names = [s.editions_table for s in v.REAL.values()] + [
            v.ledger_name(s) for s in v.REAL.values()]
        self.cur.execute("SELECT " + ", ".join(f"to_regclass('public.{n}')"
                                               for n in names))
        self.assertEqual(set(self.cur.fetchone()), {None})
        self.assertFalse(v.exists(self.cur))

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

    # ------------------------------------------------- gates 2 - 5 (real)

    def test_drifted_live_cells_fail(self):
        for sql in (
                "UPDATE public.zz_s6_support SET people = people + 1 WHERE "
                "lad24cd = 'E06000001' AND support_type = 'Section 95'",
                "UPDATE public.zz_s6_support SET published_la_name = 'x' "
                "WHERE lad24cd = 'E06000001'",
                "UPDATE public.zz_s6_unallocated SET people = people + 1",
                "UPDATE public.zz_s6_non_england SET country = 'Wales' WHERE "
                "lad_code = 'S12000033'",
                "UPDATE public.zz_s6_groups SET percentage_of_population = "
                "percentage_of_population + 0.0001 WHERE pathway = "
                "'all_pathways' AND lad24cd = 'E06000001'"):
            with self.subTest(sql=sql[:55]):
                self.plant(sql, v.real_edition1_and_latest, "edition-only")

    def test_a_missing_live_row_fails(self):
        self.plant("DELETE FROM public.zz_s6_support WHERE lad24cd = "
                   "'E06000001' AND support_type = 'Section 95'",
                   v.real_edition1_and_latest, "live-only")

    def test_a_period_without_edition_1_fails(self):
        self.plant("UPDATE public.zz_s6_support SET period_ending = "
                   "'2030-03-31' WHERE period_ending = '2026-03-31'",
                   v.real_edition1_and_latest, "no edition 1")

    def test_non_uniform_and_drifted_source_edition_fail(self):
        self.plant("UPDATE public.zz_s6_support SET source_edition = 'other' "
                   "WHERE lad24cd = 'E06000001' AND support_type = "
                   "'Section 95' AND period_ending = '2026-03-31'",
                   v.real_provenance, "not uniform")
        self.plant("UPDATE public.zz_s6_groups SET source_edition = 'x'",
                   v.real_provenance, "differs from the tip")

    def test_new_barnsley_code_and_foreign_prefix_fail(self):
        self.plant("UPDATE public.zz_s6_support SET lad24cd = 'E08000038' "
                   "WHERE lad24cd = 'E08000016'", v.real_codes,
                   "Barnsley/Sheffield")
        self.plant("UPDATE public.zz_s6_non_england SET lad_code = "
                   "'E06000001' WHERE lad_code = 'S12000033'", v.real_codes,
                   "outside S12/W06/N09")
        self.plant("UPDATE public.zz_s6_groups SET lad24cd = 'E06999999' "
                   "WHERE lad24cd = 'E06000001'", v.real_codes,
                   "not in la_boundaries")

    def test_the_tables_refuse_a_null_people_and_the_gate_catches_zeros(self):
        self.seed()
        for t in m.D11_TAGS:
            self.cur.execute("SAVEPOINT t")
            with self.assertRaises(Exception):
                self.cur.execute(f"UPDATE public.zz_s6_{t} SET people = NULL")
            self.cur.execute("ROLLBACK TO SAVEPOINT t")
            self.cur.execute("RELEASE SAVEPOINT t")
            self.plant(f"UPDATE public.zz_s6_{t} SET people = 0",
                       v.real_rule1, "zero people")

    def test_groups_null_suppressed_marker_must_agree(self):
        for sql, needle in (
                ("UPDATE public.zz_s6_groups SET suppressed = false WHERE "
                 "suppressed", "NULL people not suppressed"),
                ("UPDATE public.zz_s6_groups SET people = 3 WHERE suppressed",
                 "suppressed with people"),
                ("UPDATE public.zz_s6_groups SET source_marker = NULL WHERE "
                 "suppressed", "suppressed without"),
                ("UPDATE public.zz_s6_groups SET people = 0 WHERE "
                 "suppressed", "suppressed zero")):
            with self.subTest(needle=needle):
                self.plant(sql, v.real_rule1, needle)

    def test_the_seeded_groups_hold_star_nulls_and_published_zeros(self):
        self.seed()
        ok, detail = v.real_rule1(self.cur, ZZ)
        self.assertTrue(ok, detail)
        self.assertIn("'*' NULL", detail)
        self.cur.execute(f"SELECT COUNT(*) FROM public.{v.ED['groups']} "
                         "WHERE edition = 1 AND people IS NULL AND "
                         "source_marker = '*'")
        self.assertGreater(self.cur.fetchone()[0], 0)
        self.cur.execute(f"SELECT COUNT(*) FROM public.{v.ED['groups']} "
                         "WHERE edition = 1 AND people = 0")
        self.assertGreater(self.cur.fetchone()[0], 0)

    def test_the_source_check_catches_each_planted_construct(self):
        p = Path(self.tmpdir.name) / "x.py"
        p.write_text(
            '"""E08000038 in a docstring; la_code_lookup too"""\n'
            'import geography\n'
            'X = {"a": "E08000038"}\n'
            'Q = "SELECT old_code FROM la_code_lookup"\n'
            'OK = ("SELECT old_code, new_code FROM la_code_lookup WHERE "\n'
            '      "change_type = \'new_unitary\'")\n'
            'def f(v):\n    return v or 0\n'
            'def g(v):\n    return v or 0  # not a source value\n'
            'C = "SELECT COALESCE(x, 0) FROM t"\n'
            'D = "a message about la_code_lookup recode"\n',
            encoding="utf-8")
        bad = v._source_problems(p)
        self.assertEqual(len(bad), 5, bad)
        self.assertEqual(v._source_problems(v.LOADER), [])
        self.assertEqual(v._write_sql_problems(v.LOADER), [])

    # ------------------------------------------------- gates 13, 15, 16

    def test_the_before_state_note_is_read_by_regex(self):
        note = Path(self.tmpdir.name) / "n.md"
        h = "ab" * 32
        note.write_text(
            f"before-state la_asylum_support 2026-06-30 rows=10 "
            f"{v.hash_fields(h)}\nbefore-state-all la_asylum_support "
            f"2026-06-30 rows=10 {v.hash_fields('cd' * 32)}\n"
            f"before-state la_immigration_groups 2026-03-31 rows=3 "
            f"{v.hash_fields('ef' * 32)}\n", encoding="utf-8")
        got = v.note_hashes(note)
        self.assertEqual(got[("la_asylum_support", "2026-06-30")], (10, h))
        self.assertEqual(len(got), 2)
        self.assertEqual(v.note_hashes(Path(self.tmpdir.name) / "none"), {})
        self.assertNotRegex(note.read_text(), r"[0-9a-f]{40,}")

    def survey(self):
        return {ZZ[k].live_table: (tl.count(self.cur, v.ED[k]), None, None)
                for k in m.TAGS}

    def test_edition_1_hash_against_the_note(self):
        self.migrated()
        lines = {}
        for k in m.TAGS:
            spec = ZZ[k]
            self.cur.execute(f"SELECT DISTINCT period_ending FROM public."
                             f"{spec.editions_table} ORDER BY 1")
            for (p,) in self.cur.fetchall():
                recs = m.records(self.cur, spec, str(p), 1)
                lines[(spec.live_table, str(p))] = (
                    f"before-state {spec.live_table} {p} rows={len(recs)} "
                    f"{v.hash_fields(m.rows_content_sha(spec, recs))}")
        note = Path(self.tmpdir.name) / "n.md"
        text = "\n".join(lines.values()) + "\n"
        note.write_text(text, encoding="utf-8")
        survey = self.survey()
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, survey)
        self.assertTrue(ok, detail)
        k = next(iter(lines))
        for bad, needle in (
                (text.replace(lines[k], lines[k][:-5] + "0" * 5), "differs"),
                (text.replace(lines[k], ""), "records no"),
                (text + lines[k].replace(k[1], "2030-03-31") + "\n",
                 "has no edition 1"),
                (text.replace("before-state ", "before-state-all ", 1),
                 "records no"), ("", "records no")):
            note.write_text(bad, encoding="utf-8")
            ok, detail = v.real_edition1_hash(self.cur, ZZ, note, survey)
            self.assertFalse(ok, bad[:60])
            self.assertIn(needle, detail)
        note.write_text(text, encoding="utf-8")
        wrong = {t: (n + 3, h, x) for t, (n, h, x) in survey.items()}
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, wrong)
        self.assertFalse(ok)
        self.assertIn("surveyed", detail)
        ok, detail = v.real_edition1_hash(self.cur, ZZ,
                                          Path(self.tmpdir.name) / "x",
                                          survey)
        self.assertFalse(ok)
        self.assertIn("not written", detail)

    def test_edition_1_hash_fails_before_any_migration(self):
        self.seed()      # a load, not a migration: no edition 1 'as loaded'
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, self.survey())
        self.assertFalse(ok)
        self.assertIn("migrate-legacy not run", detail)

    def test_the_proof_and_the_reread_run_from_files_found_by_sha256(self):
        paths, legacy, files = self.migrated()
        raw = self.raw_copy(paths)
        ok, detail = v.real_migration_proof(self.cur, ZZ, files, raw, 4)
        self.assertTrue(ok, detail)
        self.assertIn("0 differences", detail)
        ok, detail = v.real_held_reread(self.cur, ZZ, files, raw, 4)
        self.assertTrue(ok, detail)
        self.assertIn("'*' NULL cells", detail)
        self.assertIn("published zeros", detail)
        ok, detail = v.real_identity(self.cur, files, raw, ZZ)
        self.assertTrue(ok, detail)
        ok, detail = v.real_reconciliation(self.cur, files, raw, 4)
        self.assertTrue(ok, detail)
        # a changed file on disk is not found by sha256
        (raw / paths[2].name).write_bytes(paths[2].read_bytes() + b"\0")
        for fn in (v.real_migration_proof, v.real_held_reread):
            ok, detail = fn(self.cur, ZZ, files, raw, 4)
            self.assertFalse(ok)
            self.assertIn("no file", detail)
        ok, detail = v.real_reconciliation(self.cur, files, raw, 4)
        self.assertFalse(ok)

    def test_a_file_that_is_not_edition_1_fails_the_proof_and_the_reread(self):
        paths, legacy, files = self.migrated()
        other = write_reg02(Path(self.tmpdir.name) / "o" / paths[3].name,
                            "2026-06-30", values={"E06000001": {0: 11}})
        fx = {**files, "reg02_jun": {**files["reg02_jun"],
                                      "sha256": m.content_sha256(other)}}
        raw = self.raw_copy(paths[:3])
        (raw / other.name).write_bytes(other.read_bytes())
        for fn in (v.real_migration_proof, v.real_held_reread):
            ok, detail = fn(self.cur, ZZ, fx, raw, 4)
            self.assertFalse(ok, detail)
            self.assertIn("differ", detail)

    def test_a_missing_ledger_pair_fails_the_reread(self):
        paths, legacy, files = self.migrated()
        raw = self.raw_copy(paths)
        ok, detail = v.real_held_reread(self.cur, ZZ, files, raw, 4)
        self.assertTrue(ok, detail)
        bad = {**files, "d11": {**files["d11"], "url": "https://x.example/y"}}
        ok, detail = v.real_held_reread(self.cur, ZZ, bad, raw, 4)
        self.assertFalse(ok)
        self.assertIn("(URL, sha256) pair", detail)

    # ---------------------------------------------------- gates 6, 7, 22

    def test_reconciliation_fails_on_a_planted_cell_from_a_file_on_disk(self):
        d = Path(self.tmpdir.name) / "disk"
        rows = d11_rows(Q4)
        p11 = write_d11(d / "d11.xlsx", Q4, rows=rows)
        p09 = write_d09(d / "d09.xlsx", Q4, rows=rows)
        rm = write_reg02(d / "rm.ods", "2026-03-31", d11=rows)
        rj = write_reg02(d / "rj.ods", "2026-06-30", d11=rows)
        files = {k: {"sha256": m.content_sha256(p)} for k, p in (
            ("d11", p11), ("d09", p09), ("reg02_mar", rm),
            ("reg02_jun", rj))}
        ok, detail = v.real_reconciliation(self.cur, files, d, 4)
        self.assertTrue(ok, detail)
        ok, detail = v.real_reconciliation(self.cur, files, d)   # 296
        self.assertFalse(ok)
        self.assertIn("expected 296", detail)
        bad = write_reg02(d / "bad.ods", "2026-06-30", d11=rows,
                          values={"E06000001": {5: 999, 7: 999}})
        ok, detail = v.real_reconciliation(
            self.cur, {**files, "reg02_jun": {"sha256":
                                              m.content_sha256(bad)}}, d, 4)
        self.assertFalse(ok)
        self.assertIn("supported_asylum", detail)
        bad = write_d09(d / "bad9.xlsx", Q4, rows=rows,
                        values={("31 Dec 2025", "North East"): 1})
        ok, detail = v.real_reconciliation(
            self.cur, {**files, "d09": {"sha256": m.content_sha256(bad)}},
            d, 4)
        self.assertFalse(ok)
        self.assertIn("England", detail)
        ok, detail = v.real_reconciliation(
            self.cur, {**files, "d09": {"sha256": "0" * 64}}, d, 4)
        self.assertFalse(ok)
        self.assertIn("no file", detail)

    def test_identity_fails_on_another_rank_or_kind(self):
        d = Path(self.tmpdir.name) / "disk"
        p11 = write_d11(d / "d11.xlsx", Q4)
        rank = (date(2026, 6, 30), date(2026, 8, 21))
        files = {"d11": {"sha256": m.content_sha256(p11), "rank": rank}}
        paths, bad = v.read_held(None, files, d, which=("d11",))
        self.assertEqual(bad, [])
        self.assertEqual(tuple(m.read_any(paths["d11"])["cover"]["rank"]),
                         rank)
        # real_identity reads all four held files: with only one present it
        # names the missing ones
        ok, detail = v.real_identity(None, {**m.LEGACY_FILES, **files}, d)
        self.assertFalse(ok)
        self.assertIn("no file", detail)

    def test_row_order_check_on_a_fixture_file(self):
        d = Path(self.tmpdir.name) / "disk"
        rows = d11_rows(Q3, add=v.MERGE_ADD)
        p11 = write_d11(d / "d11.xlsx", Q3, rows=rows)
        files = {"d11": {"sha256": m.content_sha256(p11)}}
        ok, detail = v.real_row_order(self.cur, files, d)
        self.assertTrue(ok, detail)
        self.assertIn("same records", detail)
        ok, detail = v.real_row_order(self.cur, {"d11": {"sha256": "0" * 64}},
                                      d)
        self.assertFalse(ok)
        self.assertIn("no file", detail)

    def test_row_order_check_fails_when_the_name_follows_row_order(self):
        d = Path(self.tmpdir.name) / "disk"
        rows = d11_rows(Q3, add=v.MERGE_ADD)
        p11 = write_d11(d / "d11.xlsx", Q3, rows=rows)
        files = {"d11": {"sha256": m.content_sha256(p11)}}
        order = {"n": 0}

        def by_order(code, name, marker=None):
            order["n"] += 1
            return (order["n"],)       # the last row seen wins
        with mock.patch.object(m, "name_pick", side_effect=by_order):
            ok, detail = v.real_row_order(self.cur, files, d, 6)
        self.assertFalse(ok, detail)
        self.assertIn("other records", detail)

    # ------------------------------------------------------------ gate 20

    def test_series_breaks_and_the_view_are_read_not_written(self):
        ok, detail = v.real_series_and_view(self.cur)
        self.assertTrue(ok, detail)
        self.cur.execute("CREATE TEMP TABLE s6_breaks_copy AS SELECT * FROM "
                         "public.asylum_series_breaks")
        self.cur.execute("UPDATE s6_breaks_copy SET description = "
                         "description || ' x' WHERE break_id = (SELECT "
                         "MIN(break_id) FROM s6_breaks_copy)")
        ok, detail = v.real_series_and_view(self.cur,
                                            "pg_temp.s6_breaks_copy")
        self.assertFalse(ok)
        self.assertIn("differ", detail)

    # ------------------------------------------------------------ cleanup

    def test_the_throwaway_tables_exist_only_inside_the_transaction(self):
        self.seed()
        for k in m.TAGS:
            self.cur.execute(f"SELECT DISTINCT period_ending FROM public."
                             f"{ZZ[k].editions_table}")
            for (p,) in self.cur.fetchall():
                self.assertEqual(core.rows_differing(self.cur, ZZ[k],
                                                     str(p), 1), 0)
        self.assertEqual([t for t in v.leftovers()
                          if t.startswith("zz_s6")], [])


if __name__ == "__main__":
    unittest.main()
