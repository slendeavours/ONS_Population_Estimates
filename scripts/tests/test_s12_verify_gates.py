"""Tests that the real-table gates of s12_financial_stress_editions_verify fail
on a short, empty, drifted or unmigrated state. The gate bodies (real_*) take
specs, so they are called here against the throwaway zz_s12_* tables (created
inside a transaction that is always rolled back); no real table is touched,
and the S12 editions tables are never created. Every test ends in a rollback
(also on an exception), and tearDownClass reads the database on a separate
read-only connection and fails if a zz_s12 table was left committed."""
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

import s12_financial_stress_editions as m  # noqa: E402
import s12_financial_stress_editions_verify as v  # noqa: E402
import test_s12_loader as tl  # noqa: E402
from _db import get_conn  # noqa: E402

E, S = v.ZZ_EFS, v.ZZ_S114


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
            left = [t for t in v.leftovers() if t.startswith("zz_s12")]
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
        self.addCleanup(self.e.close)

    def rollback(self):
        self.raw.close()
        self.conn.rollback()

    def seed(self):
        v.seed_both(self.e)

    def test_a_cursor_cannot_commit_or_roll_back_the_transaction(self):
        with self.assertRaises(RuntimeError):
            self.cur.connection.commit()
        with self.assertRaises(RuntimeError):
            self.cur.connection.rollback()

    def test_a_seeded_complete_state_passes_the_real_gates(self):
        self.seed()
        for fn in (lambda: v.real_edition1_and_latest(self.cur, E, S),
                   lambda: v.real_grammar(self.cur, E, []),
                   lambda: v.real_null_rule(self.cur, E),
                   lambda: v.real_s114(self.cur, E, S)):
            ok, detail = fn()
            self.assertTrue(ok, detail)
        self.assertEqual(v.shape_problems(self.cur, E), [])
        self.assertEqual(v.shape_problems(self.cur, S), [])

    def test_the_s114_register_and_counts_check(self):
        self.seed()
        reg = self.e.register
        with mock.patch.object(m, "S114_ROOTS", (Path(self.tmpdir.name),)):
            def run(**kw):
                args = dict(register=reg, notices=3, authorities=2,
                            refiles={})
                args.update(kw)
                return v.real_s114_register(self.cur, S, **args)
            ok, detail = run()
            self.assertTrue(ok, detail)
            ok, detail = run(notices=14)
            self.assertFalse(ok)
            self.assertIn("not 14", detail)
            ok, detail = run(authorities=10)
            self.assertFalse(ok)
            ok, detail = run(refiles={("E09000008", "2022-01-01"): {
                "from": "2021-22", "to": "2022-23"}})
            self.assertFalse(ok)
            self.assertIn("not 2022-23", detail)
            self.cur.execute(f"UPDATE public.{S.live_table} SET "
                             "financial_year = '2020-21' WHERE notice_date = "
                             "'2022-01-01'")
            ok, detail = run()
            self.assertFalse(ok)
            self.assertIn("differs from the register", detail)
            self.assertIn("April-March", detail)

    def test_an_empty_state_is_not_a_pass(self):
        ok, detail = v.real_edition1_and_latest(self.cur, E, S)
        self.assertFalse(ok)
        self.assertIn("no live periods", detail)

    def test_an_unmigrated_state_fails_with_a_clear_message(self):
        ge = dataclasses.replace(E, editions_table="zz_s12_not_there")
        gs = dataclasses.replace(S, editions_table="zz_s12_not_there2")
        for fn in (lambda: v.real_edition1_and_latest(self.cur, ge, gs),
                   lambda: v.real_grammar(self.cur, ge, []),
                   lambda: v.real_null_rule(self.cur, ge),
                   lambda: v.real_s114(self.cur, ge, gs),
                   lambda: v.real_ranks(self.cur, ge, gs),
                   lambda: v.real_map_sets(self.cur, ge, gs, __file__),
                   lambda: v.real_edition1_hash(self.cur, ge, gs)):
            with self.assertRaises(ValueError) as e:
                fn()
            self.assertIn("does not exist", str(e.exception))
        self.assertTrue(v.shape_problems(self.cur, ge))

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

    def test_drifted_live_cells_and_null_rule_violations_fail(self):
        self.seed()
        self.cur.execute("SAVEPOINT t")
        self.cur.execute(f"UPDATE public.{E.live_table} SET amount_m = "
                         "amount_m + 1 WHERE lad24cd = 'E09000008'")
        ok, detail = v.real_edition1_and_latest(self.cur, E, S)
        self.assertFalse(ok, detail)
        self.assertIn("live-only", detail)
        self.cur.execute("ROLLBACK TO SAVEPOINT t")
        self.cur.execute(f"UPDATE public.{E.live_table} SET amount_m = NULL "
                         "WHERE lad24cd = 'E09000008'")
        ok, detail = v.real_null_rule(self.cur, E)
        self.assertFalse(ok, detail)
        self.assertIn("disagrees", detail)

    def test_a_stored_cell_text_that_no_longer_matches_fails(self):
        self.seed()
        self.cur.execute("SAVEPOINT t")
        self.cur.execute(f"ALTER TABLE public.{E.editions_table} DISABLE "
                         "TRIGGER USER")
        self.cur.execute(f"UPDATE public.{E.editions_table} SET cell_text = "
                         "'£1.0m (pending)' WHERE lad24cd = 'E09000008'")
        ok, detail = v.real_grammar(self.cur, E, [])
        self.assertFalse(ok, detail)
        self.assertIn("E09000008", detail)
        self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_a_predecessor_made_direct_fails(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{S.live_table} SET attribution = "
                         "'direct', successor_codes = NULL, "
                         "attribution_note = NULL WHERE lad24cd = 'E10000021'")
        ok, detail = v.real_s114(self.cur, E, S)
        self.assertFalse(ok, detail)

    def test_the_fuzzy_check_catches_a_fuzzy_matcher(self):
        self.assertEqual(v.fuzzy_problems(), [])
        fake = ("import difflib\n\ndef match_name(name, boundaries, aliases, "
                "exclusions):\n    return [n for n in boundaries if "
                "name.lower() in n.lower()]\n")
        self.assertGreaterEqual(len(v.fuzzy_problems(fake)), 2)

    def test_the_grammar_probes_hold(self):
        self.assertEqual(v.grammar_problems(), [])
        with mock.patch.object(m, "parse_cell", side_effect=lambda t, w="": (
                m.Cell((t,), (m.Statement(None, m.Decimal("5.0"), None,
                                          "figure", t),), False))):
            self.assertTrue(v.grammar_problems())

    def test_the_names_file_checks_hold_and_catch_a_haringey_alias(self):
        self.assertEqual(v.names_problems(self.cur), [])
        bad = Path(self.tmpdir.name) / "names.json"
        text = m.NAMES_FILE.read_text(encoding="utf-8").replace(
            '"aliases": {', '"aliases": {\n    "Haringey": {"lad24cd": '
            '"E09000013", "seen": [{"year": "2025-26", "where": "table"}]},',
            1)
        bad.write_text(text, encoding="utf-8")
        self.assertTrue(v.names_problems(self.cur, bad))

    def test_note_lines_are_scan_safe_and_verified(self):
        self.seed()
        lines = v.note_lines_for(self.cur, E, S)
        self.assertTrue(lines)
        for line in lines:
            self.assertIsNone(re.search(r"[0-9a-f]{64}", line))
            self.assertRegex(line, r"sha256-first32=[0-9a-f]{32} "
                                   r"sha256-last32=[0-9a-f]{32}$")
        self.assertTrue(any(x.startswith("w1-read la_efs_support")
                            for x in lines))

    def test_the_w1_read_efs_set_drops_only_bcp_and_north_northamptonshire(self):
        # Scott, 2026-10-10: Bexley is kept; only E06000058 and E06000061
        # leave the EFS set (the real note line is then rows=49 of 51)
        self.assertEqual(v.RULE_CODES, frozenset({"E06000058", "E06000061"}))
        self.assertNotIn("E09000004", v.RULE_CODES)
        self.seed()
        live = v.code_set(self.cur, E.live_table)
        lines = {x.split(" lad24cd-set")[0]: x
                 for x in v.note_lines_for(self.cur, E, S)
                 if " lad24cd-set " in x}
        self.assertEqual(lines["w1-read la_efs_support"],
                         v.set_line("w1-read", v.EFS_T,
                                    live - {"E06000058", "E06000061"}))

    def test_the_map_set_gate_needs_the_note(self):
        self.seed()
        ok, detail = v.real_map_sets(self.cur, E, S,
                                     Path(self.tmpdir.name) / "none.md")
        self.assertFalse(ok)
        self.assertIn("not written", detail)
        note = Path(self.tmpdir.name) / "n.md"
        note.write_text("\n".join(v.note_lines_for(self.cur, E, S)) + "\n",
                        encoding="utf-8")
        ok, detail = v.real_map_sets(self.cur, E, S, note)
        self.assertTrue(ok, detail)

    def test_the_real_page_checks_read_the_held_pages(self):
        gone = dataclasses.replace(E, editions_table="zz_s12_not_there")
        ok, detail = v.real_page_rows(self.cur, gone)
        self.assertTrue(ok, detail)
        ok, detail = v.real_page_rows(self.cur, E)
        self.assertFalse(ok, detail)
        self.assertIn("in no edition", detail)
        self.assertEqual(v.seen_problems(v.held_pages()[0]), [])


if __name__ == "__main__":
    unittest.main()
