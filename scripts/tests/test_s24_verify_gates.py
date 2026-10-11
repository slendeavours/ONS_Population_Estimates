"""Tests that the real-table gates of s24_rsh_register_editions_verify fail on
an empty, drifted, planted or unmigrated state. The gate bodies (real_*) take
specs, so they are called here against the throwaway zz_s24_* tables (created
inside a transaction that is always rolled back); no real table is written
(test_the_gates_leave_the_real_tables_as_they_were compares their row
counts). Every test ends in a rollback (also on an exception), and
tearDownClass reads the database on a separate read-only connection and fails
if a zz_s24 table, function or trigger was left committed."""
import contextlib
import dataclasses
import io
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import s24_rsh_register_editions as m  # noqa: E402
import s24_rsh_register_editions_verify as v  # noqa: E402
import test_s24_rsh_loader as tl  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s24_rsh_pure import JROWS, jrow, write_register  # noqa: E402

ZZ = v.ZZ
J_SEP, J_AUG, SEP, AUG = v.J_SEP, v.J_AUG, v.SEP, v.AUG


class VerifyGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()
        # a second, read-only view of committed state: what other sessions
        # see, taken before any scenario runs
        cls.conn2 = get_conn()
        cls.conn2.set_session(readonly=True)
        cls.raw_second = cls.conn2.cursor()
        cls.real_before = cls.real_state(cls.raw_second)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.rollback()
            cls.conn.close()
            cls.raw_second.close()
            cls.conn2.rollback()
            cls.conn2.close()
        finally:
            left = [t for t in v.leftovers() if "zz_s24" in t]
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

    def completed(self):
        self.e.completed()

    def real_gates(self):
        """(label, fn(cur, specs)) of the table-reading gates, with the
        fixture's files and no expected real split."""
        e = self.e
        return (
            ("2", v.real_edition1_and_latest),
            ("3", v.real_geography),
            ("4", v.real_provenance),
            ("5", lambda c, s: v.real_unassessed(c, s, [e.root], {})),
            ("8", v.real_ranks_from_source),
            ("13", lambda c, s: v.real_held_reread(
                c, s, getattr(e, "held", {}), [e.root])),
            ("14", v.real_history),
            ("15", v.real_refreshed),
        )

    # -- the harness itself ------------------------------------------------
    def test_a_cursor_cannot_commit_or_roll_back_the_transaction(self):
        with self.assertRaises(RuntimeError):
            self.cur.connection.commit()
        with self.assertRaises(RuntimeError):
            self.cur.connection.rollback()

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
        """The real tables (live, editions, ledger) hold the same rows after
        the throwaway scenarios as before them; only the zz_s24 copies were
        touched. The real editions tables exist since the first load of
        2026-10-11."""
        self.assertTrue(v.exists(self.cur))
        self.assertTrue(pe_exists(self.cur))
        for name, s in ZZ.items():
            self.assertNotEqual(s.editions_table, v.REAL[name].editions_table)
            self.assertTrue(s.editions_table.startswith("zz_s24_"))
            self.assertTrue(s.live_table.startswith("zz_s24_"))
        self.completed()
        self.assertEqual(self.real_state(self.raw), type(self).real_before)
        self.assertEqual(self.real_state(self.raw_second),
                         type(self).real_before)

    @staticmethod
    def real_state(cur):
        """Row counts of the nine real relations, None where one does not
        exist. A cursor on a separate connection sees only committed
        state."""
        state = {}
        for s in v.REAL.values():
            for name in (s.live_table, s.editions_table, v.ledger_name(s)):
                cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
                if cur.fetchone()[0] is None:
                    state[name] = None
                    continue
                cur.execute(f"SELECT count(*) FROM public.{name}")
                state[name] = cur.fetchone()[0]
        return state

    def test_an_unmigrated_state_fails_with_a_clear_message(self):
        gone = {n: dataclasses.replace(s, editions_table="zz_s24_not_there")
                for n, s in ZZ.items()}
        for label, fn in self.real_gates():
            with self.subTest(gate=label):
                with self.assertRaises(ValueError) as e:
                    fn(self.cur, gone)
                self.assertIn("does not exist", str(e.exception))
        self.assertTrue(v.shape_problems(self.cur, gone["register"]))

    # -- a complete state passes, an empty one does not ---------------------
    def test_a_completed_state_passes_every_real_gate(self):
        self.completed()
        for label, fn in self.real_gates():
            ok, detail = fn(self.cur, ZZ)
            self.assertTrue(ok, f"gate {label}: {detail}")
        for s in ZZ.values():
            self.assertEqual(v.shape_problems(self.cur, s), [])
        run = dict(m.LEGACY_RUN, rows=self.e.run_rows, register=v.JUL,
                   judgements=J_AUG)
        ok, detail = v.real_migration_proof(
            self.cur, ZZ, self.e.held, [self.e.root], run=run,
            run_rows=lambda c: self.e.run_rows)
        self.assertTrue(ok, detail)

    def test_an_empty_state_is_not_a_pass(self):
        for label, fn in self.real_gates():
            if label == "3":
                continue        # a geography check needs no rows
            with self.subTest(gate=label):
                ok, detail = fn(self.cur, ZZ)
                self.assertFalse(ok, detail)

    # -- planted failures ---------------------------------------------------
    def plant(self, sql, fn, needle):
        """The completed world once per test; the planted change is rolled
        back to a savepoint after each check."""
        if not getattr(self, "_world", False):
            self.completed()
            self._world = True
        self.cur.execute("SAVEPOINT plant")
        self.cur.execute(sql)
        ok, detail = fn(self.cur, ZZ)
        self.cur.execute("ROLLBACK TO SAVEPOINT plant")
        self.assertFalse(ok, detail)
        self.assertIn(needle, detail)

    def test_drifted_live_cells_and_periods_fail(self):
        reg, jud = ZZ["register"], ZZ["judgements"]
        self.plant(f"UPDATE public.{reg.live_table} SET designation = 'x' "
                   "WHERE registration_number = '4001'",
                   v.real_edition1_and_latest, "live-only")
        self.plant(f"UPDATE public.{reg.live_table} SET snapshot_date = "
                   "'2030-01-01' WHERE snapshot_date = '2026-07-24'",
                   v.real_edition1_and_latest, "no edition 1")
        self.plant(f"UPDATE public.{jud.live_table} SET edition_date = "
                   f"'{J_AUG}'", v.real_edition1_and_latest,
                   "not the newest edition")

    def test_provenance_that_is_empty_non_uniform_or_not_the_tips_fails(self):
        live = ZZ["judgements"].live_table
        self.plant(f"UPDATE public.{live} SET source_url = ''",
                   v.real_provenance, "missing a provenance")
        self.plant(f"UPDATE public.{live} SET source_file = 'other' WHERE "
                   "registration_number = 'LA01'", v.real_provenance,
                   "not uniform")
        self.plant(f"UPDATE public.{live} SET release_page_url = 'x'",
                   v.real_provenance, "differs from the tip's")

    def test_geography_columns_fail(self):
        self.completed()
        self.cur.execute(f"ALTER TABLE public.{ZZ['register'].live_table} "
                         "ADD COLUMN la_code text")
        ok, detail = v.real_geography(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("la_code", detail)
        self.assertTrue(v.geography_problems(self.cur, [], ["rsh_x"])[0]
                        .startswith("staging_la_signals.rsh_x"))

    def test_a_grade_or_reason_defect_fails_the_unassessed_gate(self):
        self.completed()
        jud = ZZ["judgements"]
        both = {J_SEP: (2, 1, 8), J_AUG: (2, 1, 8)}
        ok, detail = v.real_unassessed(self.cur, ZZ, [self.e.root], both)
        self.assertTrue(ok, detail)
        self.assertIn("4 edition(s) counted", detail)
        for over, needle in (
                (dict(consumer_grade="'-'"), "outside the allowed set"),
                (dict(consumer_grade="''"), "outside the allowed set"),
                (dict(consumer_grade="NULL", null_reasons="'{}'::jsonb"),
                 "without a null_reasons entry")):
            with self.subTest(over=over):
                self.cur.execute("SAVEPOINT t")
                v.clone_edition(self.cur, jud, "registration_number = "
                                "'LA01' AND edition = 2", edition="9", **over)
                ok, detail = v.real_unassessed(self.cur, ZZ, [self.e.root],
                                               {})
                self.cur.execute("ROLLBACK TO SAVEPOINT t")
                self.assertFalse(ok, detail)
                self.assertIn(needle, detail)
        ok, detail = v.real_unassessed(self.cur, ZZ, [self.e.root],
                                       {J_SEP: (206, 66, 332)})
        self.assertFalse(ok)
        self.assertIn("expected (206, 66, 332)", detail)
        ok, detail = v.real_unassessed(self.cur, ZZ, [], both)
        self.assertFalse(ok)
        self.assertIn("no judgements edition's file", detail)

    def test_edition_1_alone_does_not_pass_the_real_reasons_split(self):
        self.e.migrated()
        ok, detail = v.real_unassessed(self.cur, ZZ, [self.e.root], {})
        self.assertTrue(ok, detail)
        ok, detail = v.real_unassessed(self.cur, ZZ, [self.e.root],
                                       {J_SEP: (2, 1, 8)})
        self.assertFalse(ok)
        self.assertIn("only edition 1's", detail)

    def test_the_held_split_constants_are_the_files_dashes(self):
        # the real held files: 604 = 206 + 66 + 332 and 600 = 200 + 74 + 326
        bad, parts = v.held_split_problems()
        self.assertEqual(bad, [])
        self.assertEqual(len(parts), 2)
        self.assertEqual(v.EXPECTED_SPLIT["2026-09-30"], (206, 66, 332))
        self.assertEqual(sum(v.EXPECTED_SPLIT["2026-08-12"]), 600)
        bad, _ = v.held_split_problems(split={"2026-09-30": (206, 66, 331)})
        self.assertTrue(bad)

    def test_a_pending_refresh_and_a_left_over_live_period_fail(self):
        self.completed()
        rows = list(tl.REG_ROWS[:3])
        rows[0] = rows[0][:3] + ("Profit",) + rows[0][4:]
        self.e.register("17 September 2026", rows=rows,
                        modified=datetime(2026, 9, 19, 9))
        self.e.ok(self.cur, ["load", "--only", "register", "--acknowledge",
                             SEP, "--commit"])
        ok, detail = v.real_refreshed(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("pending", detail)
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertFalse(ok)

    def test_the_migrated_not_refreshed_state_fails_history_and_refresh(self):
        self.e.migrated()
        ok, detail = v.real_refreshed(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("left-over live rows", detail)
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("holds 2 periods", detail)

    def test_the_reread_check_needs_edition_2_and_catches_a_changed_tip(self):
        self.e.migrated()
        ok, detail = v.real_held_reread(self.cur, ZZ, self.e.held,
                                        [self.e.root])
        self.assertFalse(ok)
        self.assertIn("edition 2, with the real reasons, is not stored",
                      detail)
        ok, detail = v.real_held_reread(self.cur, ZZ, self.e.held, [])
        self.assertFalse(ok)
        self.assertIn("no file", detail)

    # -- the hidden sheet ---------------------------------------------------
    def test_the_source_check_catches_each_planted_construct(self):
        cases = {
            "wb.worksheets": "import openpyxl\ndef _read_sheet(p, title):\n"
                             "    wb = openpyxl.load_workbook(p)\n"
                             "    for ws in wb.worksheets:\n        pass\n"
                             "    return wb[title]\n",
            "a sheetnames loop": "import openpyxl\ndef _read_sheet(p, "
                                 "title):\n    wb = openpyxl.load_workbook("
                                 "p)\n    return [n for n in wb.sheetnames"
                                 "]\n",
            "an iterated workbook": "import openpyxl\ndef _read_sheet(p, "
                                    "title):\n    wb = openpyxl."
                                    "load_workbook(p)\n    for ws in wb:\n"
                                    "        pass\n    return wb[title]\n",
            "wb.active": "import openpyxl\ndef _read_sheet(p, title):\n"
                         "    wb = openpyxl.load_workbook(p)\n"
                         "    return wb.active\n",
            "another zip member": "import zipfile\ndef f(p):\n    with "
                                  "zipfile.ZipFile(p) as z:\n        return"
                                  " z.read('xl/worksheets/sheet2.xml')\n"}
        for label, code in cases.items():
            with self.subTest(label=label):
                self.assertTrue(v.sheet_access_problems(code), label)
        self.assertEqual(v.sheet_access_problems(), [])

    def test_the_proxy_workbook_sees_only_the_wanted_sheets(self):
        reg = write_register(Path(self.tmpdir.name) / "r.xlsx")
        self.assertEqual(v.non_visible_sheets(reg), 1)
        self.assertEqual(v.hidden_sheet_problems(reg, SEP), [])
        # a loader that touched wb.worksheets is caught by the proxy
        import openpyxl
        with v.watched_workbooks() as log:
            wb = openpyxl.load_workbook(reg)
            with self.assertRaises(AssertionError):
                wb.worksheets
        self.assertEqual(log["violations"], ["worksheets"])

    # -- the decision note --------------------------------------------------
    def test_note_lines_are_scan_safe_and_regex_read(self):
        self.e.seed(self.cur)
        lines = v.note_lines_for(self.cur, ZZ)
        self.assertEqual(len(lines), 12)
        for ln in lines:
            self.assertIsNone(re.search(r"[0-9a-fA-F]{40,}", ln), ln)
            self.assertRegex(ln, r"^before-state(-all)? zz_s24_(reg|jud|not)_"
                                 r"live \d{4}-\d{2}-\d{2} rows=\d+ "
                                 r"sha256-first32=[0-9a-f]{32} "
                                 r"sha256-last32=[0-9a-f]{32}$")
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("\n".join(lines) + "\n", encoding="utf-8")
        got = v.note_hashes(note, "before-state")
        self.assertEqual(len(got), 6)
        self.assertEqual(len(v.note_hashes(note, "before-state-all")), 6)
        self.assertEqual(v.note_hashes(Path(self.tmpdir.name) / "none",
                                       "before-state"), {})

    def test_edition_1_hash_against_the_note(self):
        self.e.seed(self.cur)
        lines = v.note_lines_for(self.cur, ZZ)
        survey = {n: sorted(self.e.legacy[n]["periods"]) for n in v.NAMES}
        self.e.migrate(self.cur)
        note = Path(self.tmpdir.name) / "n.md"
        full = "\n".join(lines) + "\n"
        note.write_text(full, encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, survey)
        self.assertTrue(ok, detail)
        first = next(x for x in lines if x.startswith("before-state "))
        sha = first.split("sha256-first32=")[1][:32]
        jaug = next(x for x in lines if x.startswith("before-state ")
                    and f"_jud_live {J_AUG}" in x)
        for text, needle in (
                (full.replace(sha, "0" * 32), "equal neither"),
                (full.replace(jaug + "\n", ""), "records no"),
                ("", "records no"),
                (full + "before-state zz_s24_reg_live 2030-01-01 rows=1 "
                 + v.hash_fields("a" * 64) + "\n", "no edition 1")):
            with self.subTest(needle=needle):
                note.write_text(text, encoding="utf-8")
                ok, detail = v.real_edition1_hash(self.cur, ZZ, note, survey)
                self.assertFalse(ok, text[:40])
                self.assertIn(needle, detail)
        ok, detail = v.real_edition1_hash(self.cur, ZZ,
                                          Path(self.tmpdir.name) / "x",
                                          survey)
        self.assertFalse(ok)
        self.assertIn("not written", detail)

    def test_edition_1_hash_fails_before_any_migration(self):
        self.e.load_world()      # a load, not a migration: no 'as loaded'
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note,
                                          {n: [] for n in v.NAMES})
        self.assertFalse(ok)
        self.assertIn("migrate-legacy not run", detail)

    def test_print_note_lines_reads_the_real_tables_only(self):
        """One before-state and one before-state-all line per period the live
        tables hold now (read-only; after the 2026-10-11 migration: three
        register snapshots, the 30 September judgements and notices)."""
        periods = 0
        for s in v.REAL.values():
            self.raw_second.execute(
                f"SELECT count(DISTINCT {s.period_col}) "
                f"FROM public.{s.live_table}")
            periods += self.raw_second.fetchone()[0]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            v.print_note_lines()
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2 * periods)
        self.assertEqual(len([x for x in lines
                              if x.startswith("before-state ")]), periods)
        self.assertEqual(len([x for x in lines
                              if x.startswith("before-state-all ")]), periods)
        for ln in lines:
            self.assertIsNone(re.search(r"[0-9a-fA-F]{40,}", ln), ln)
        self.assertIn("rsh_registered_providers 2026-09-17 rows=1577",
                      out.getvalue())
        self.assertIn("rsh_regulatory_judgements 2026-09-30 rows=311",
                      out.getvalue())

    def test_the_migration_proof_reruns_from_a_directory_and_catches_changes(self):
        self.e.migrated()
        run = dict(m.LEGACY_RUN, rows=self.e.run_rows, register=v.JUL,
                   judgements=J_AUG)
        kw = dict(run=run, run_rows=lambda c: self.e.run_rows)
        ok, detail = v.real_migration_proof(self.cur, ZZ, self.e.held,
                                            [self.e.root], **kw)
        self.assertTrue(ok, detail)
        self.assertIn("0 differences", detail)
        other = write_register(Path(self.tmpdir.name) / "o" / "o.xlsx",
                               rows=tl.REG_ROWS[:2])
        held = {**self.e.held, ("register", SEP): {
            **self.e.held[("register", SEP)],
            "sha256": m.content_sha256(other)}}
        ok, detail = v.real_migration_proof(self.cur, ZZ, held,
                                            [other.parent, self.e.root], **kw)
        self.assertFalse(ok)
        self.assertIn("differences", detail)
        ok, detail = v.real_migration_proof(
            self.cur, ZZ, self.e.held, [self.e.root],
            run=dict(run, rows=run["rows"] + 1), run_rows=kw["run_rows"])
        self.assertFalse(ok)
        self.assertIn("run-log", detail)

    def test_the_throwaway_tables_exist_only_inside_the_transaction(self):
        self.completed()
        self.assertEqual(core.rows_differing(self.cur, ZZ["register"], SEP,
                                             1), 0)


def pe_exists(cur):
    import period_editions as pe
    return all(pe.table_exists(cur, t) for s in v.REAL.values()
               for t in (s.editions_table, v.ledger_name(s)))


if __name__ == "__main__":
    unittest.main()
