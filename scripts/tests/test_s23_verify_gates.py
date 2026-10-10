"""Tests that the real-table gates of s23_rsh_stock_editions_verify fail on a
short, empty, drifted or unmigrated state. The gate bodies (real_*) take a
spec, so they are called here against the throwaway zz_s23_* tables (created
inside a transaction that is always rolled back); no real table is touched,
and the S23 editions table is never created. Every test ends in a rollback
(also on an exception), and tearDownClass reads the database on a separate
read-only connection and fails if a zz_s23 table was left committed."""
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
import s23_rsh_stock_editions as m  # noqa: E402
import s23_rsh_stock_editions_verify as v  # noqa: E402
import test_s23_rsh_stock_loader as tl  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s23_rsh_stock_pure import CODES, write_tool  # noqa: E402

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
            left = [t for t in v.leftovers() if t.startswith("zz_s23")]
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
        self.e.seed(self.cur)

    def gates(self):
        return (v.real_edition1_and_latest, v.real_provenance, v.real_codes,
                lambda c, s: v.real_row_counts(c, s, n=len(CODES)),
                v.real_stock_values)

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

    def test_an_empty_state_is_not_a_pass(self):
        for fn in self.gates():
            ok, detail = fn(self.cur, ZZ)
            self.assertFalse(ok, detail)
            self.assertIn("no live periods", detail)

    def test_an_unmigrated_state_fails_with_a_clear_message(self):
        gone = dataclasses.replace(ZZ, editions_table="zz_s23_not_there")
        for fn in self.gates():
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
        live = ZZ.live_table
        self.plant(f"UPDATE public.{live} SET general_needs_bedspaces = "
                   "general_needs_bedspaces + 1, total_social_stock = "
                   "total_social_stock + 1 WHERE rp_code = 'L0055' AND "
                   "lad24cd = 'E06000001'", v.real_edition1_and_latest,
                   "edition-only")

    def test_a_missing_live_row_and_a_missing_edition_1_fail(self):
        self.plant(f"DELETE FROM public.{ZZ.live_table} WHERE rp_code = "
                   "'4865' AND lad24cd = 'E06000001'",
                   v.real_edition1_and_latest, "live-only")

    def test_a_period_without_edition_1_fails(self):
        self.plant(f"UPDATE public.{ZZ.live_table} SET stock_date = "
                   "'2030-03-31'", v.real_edition1_and_latest,
                   "no edition 1")

    def test_non_uniform_and_drifted_provenance_fail(self):
        live = ZZ.live_table
        self.plant(f"UPDATE public.{live} SET source_file = 'other' WHERE "
                   "rp_code = '4865' AND lad24cd = 'E06000001'",
                   v.real_provenance, "not uniform")

    def test_provenance_that_is_uniform_but_not_the_tips_fails(self):
        self.plant(f"UPDATE public.{ZZ.live_table} SET release_page_url = "
                   "'x'", v.real_provenance, "differs from the tip")

    def test_new_barnsley_code_and_unresolvable_publisher_code_fail(self):
        live = ZZ.live_table
        self.plant(f"UPDATE public.{live} SET lad24cd = 'E08000038' WHERE "
                   "lad24cd = 'E08000016'", v.real_codes,
                   "Barnsley/Sheffield")

    def test_a_publisher_code_that_does_not_resolve_fails(self):
        self.plant(f"UPDATE public.{ZZ.live_table} SET publisher_la_code = "
                   "'E06000099' WHERE lad24cd = 'E06000001'", v.real_codes,
                   "does not resolve")

    def test_a_stray_authority_count_fails(self):
        self.seed()
        ok, _ = v.real_row_counts(self.cur, ZZ, n=len(CODES))
        self.assertTrue(ok)
        ok, detail = v.real_row_counts(self.cur, ZZ, n=len(CODES) + 1)
        self.assertFalse(ok)
        self.assertIn("authorities, expected", detail)
        self.cur.execute(f"DELETE FROM public.{ZZ.live_table} WHERE "
                         "lad24cd = 'E08000019'")
        self.assertFalse(v.real_row_counts(self.cur, ZZ, n=len(CODES))[0])

    def test_the_table_refuses_a_null_stock_cell_and_a_broken_sum(self):
        self.seed()
        for sql in (f"UPDATE public.{ZZ.live_table} SET "
                    "low_cost_home_ownership = NULL",
                    f"UPDATE public.{ZZ.live_table} SET "
                    "general_needs_self_contained = NULL",
                    f"UPDATE public.{ZZ.live_table} SET null_reasons = 'x' "
                    "WHERE low_cost_home_ownership IS NOT NULL",
                    f"UPDATE public.{ZZ.live_table} SET total_social_stock "
                    "= total_social_stock + 1"):
            self.cur.execute("SAVEPOINT t")
            with self.assertRaises(Exception):
                self.cur.execute(sql)
            self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_lcho_nulls_and_zeros_follow_the_not_counted_rule(self):
        self.seed()
        ok, detail = v.real_stock_values(self.cur, ZZ)
        self.assertTrue(ok, detail)
        self.assertIn(f"{2 * tl.SMALL} LCHO NULL(s)", detail)   # live + tip
        live = ZZ.live_table
        drop = [f"ALTER TABLE public.{live} DROP CONSTRAINT "
                f"{m.LIVE_PREFIX}_{s}" for s in ("components_sum_chk",
                                                 "lcho_null_reason_chk")]
        for sql, needle in (
                (f"UPDATE public.{live} SET low_cost_home_ownership = 0, "
                 "null_reasons = NULL WHERE rp_code = 'H0375'",
                 "Small PRP (Short Form) LCHO zero"),
                (f"UPDATE public.{live} SET null_reasons = 'other' WHERE "
                 "rp_code = 'H0375'", "without the not-counted reason"),
                (f"UPDATE public.{live} SET low_cost_home_ownership = NULL, "
                 f"null_reasons = '{m.NOT_COUNTED_REASON}', "
                 "total_social_stock = general_needs_self_contained + "
                 "general_needs_bedspaces + supported_housing_and_older_people"
                 " WHERE rp_code = 'L0055'", "not on a Small PRP row"),
                (f"UPDATE public.{live} SET general_needs_bedspaces = NULL "
                 "WHERE rp_code = 'L0055'", "always-counted")):
            with self.subTest(needle=needle):
                self.cur.execute("SAVEPOINT t")
                for d in drop:
                    self.cur.execute(d)
                if needle == "always-counted":
                    self.cur.execute(f"ALTER TABLE public.{live} ALTER "
                                     "COLUMN general_needs_bedspaces DROP "
                                     "NOT NULL")
                self.cur.execute(sql)
                ok, detail = v.real_stock_values(self.cur, ZZ)
                self.cur.execute("ROLLBACK TO SAVEPOINT t")
                self.assertFalse(ok, detail)
                self.assertIn(needle, detail)

    def test_edition_1_as_loaded_keeps_the_published_zeros(self):
        path, files = self.migrated()
        with tl.ack_cells():
            rc, text, _ = self.e.cmd(["load", "--file", path, "--no-page",
                                      "--acknowledge-flips", tl.ACK,
                                      "--commit"])
            self.assertEqual(rc, 0, text)
        rc, text, _ = self.e.cmd(["refresh-latest", "--commit"])
        self.assertEqual(rc, 0, text)
        ok, detail = v.real_stock_values(self.cur, ZZ)
        self.assertTrue(ok, detail)
        self.assertIn("1 edition 1 'as loaded' period(s) keep", detail)
        self.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.editions_table} "
                         "WHERE edition = 1 AND rp_code = 'H0375' AND "
                         "low_cost_home_ownership = 0")
        self.assertEqual(self.cur.fetchone()[0], tl.SMALL)
        ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertTrue(ok, detail)

    def test_a_zero_total_that_is_not_a_larp_fails(self):
        self.seed()
        self.cur.execute(f"UPDATE public.{ZZ.live_table} SET provider_type "
                         "= 'PRP' WHERE total_social_stock = 0")
        ok, detail = v.real_stock_values(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("not LARPs", detail)

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
        bad = v._source_problems(p)
        self.assertEqual(len(bad), 4, bad)
        self.assertEqual(v._source_problems(v.LOADER), [])

    def test_the_before_state_note_is_read_by_regex(self):
        note = Path(self.tmpdir.name) / "n.md"
        h = "ab" * 32
        note.write_text(f"before-state rsh_rp_stock_by_la {P} rows=10171 "
                        f"{v.hash_fields(h)}\nbefore-state-all rsh_rp_stock_by_la "
                        f"{P} rows=10171 {v.hash_fields('cd' * 32)}\n",
                        encoding="utf-8")
        self.assertEqual(v.note_hashes(note), {P: (10171, h)})
        self.assertEqual(v.note_hashes(Path(self.tmpdir.name) / "none"), {})

    def migrated(self):
        path, files = self.e.seed_legacy(self.cur)
        with mock.patch.object(m, "LEGACY_LIVE", self.e.legacy(self.cur)), \
                mock.patch.object(m, "LEGACY_FILE", files):
            rc, text, _ = self.e.cmd(["migrate-legacy", path, "--commit"])
        self.assertEqual(rc, 0, text)
        return path, files

    def test_edition_1_hash_against_the_note(self):
        self.migrated()
        h = m.rows_content_sha(m.records(self.cur, ZZ, P, 1))
        note = Path(self.tmpdir.name) / "n.md"
        line = (f"before-state rsh_rp_stock_by_la {P} rows={N} "
                f"{v.hash_fields(h)}")
        note.write_text(line + "\n", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, N)
        self.assertTrue(ok, detail)
        for text, needle in (
                (line.replace(h[:32], "0" * 32), "differs"),
                (line.replace(f"rows={N}", f"rows={N + 1}"), "differs"),
                (f"before-state-all rsh_rp_stock_by_la {P} rows={N} "
                 f"{v.hash_fields(h)}", "records no"),
                (line + "\n" + line.replace(P, "2024-03-31"),
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

    def test_the_migration_proof_reruns_from_the_file_found_by_sha256(self):
        path, files = self.migrated()
        raw = Path(self.tmpdir.name) / "raw"
        raw.mkdir()
        (raw / path.name).write_bytes(path.read_bytes())
        kw = dict(areas=len(CODES), regions=2)
        ok, detail = v.real_migration_proof(self.cur, ZZ, files, raw, **kw)
        self.assertTrue(ok, detail)
        self.assertIn("0 differences", detail)
        # edition 1 as loaded is the tip: a load now reads the rule's NULLs
        with tl.ack_cells():
            ok, detail = v.real_held_reread(self.cur, ZZ, files, raw)
        self.assertFalse(ok, detail)
        self.assertIn("with the not-counted rule", detail)
        # the named acknowledgement stores edition 2; then the re-read
        # equals the tip, and the proof of edition 1 still holds
        with tl.ack_cells():
            rc, text, _ = self.e.cmd(["load", "--file", path, "--no-page",
                                      "--acknowledge-flips", tl.ACK,
                                      "--commit"])
            self.assertEqual(rc, 0, text)
            ok, detail = v.real_held_reread(self.cur, ZZ, files, raw)
        self.assertTrue(ok, detail)
        self.assertIn(f"{tl.SMALL} LCHO cell(s) 0 -> NULL", detail)
        ok, detail = v.real_migration_proof(self.cur, ZZ, files, raw, **kw)
        self.assertTrue(ok, detail)
        # without the acknowledgement recorded, the changes are not covered
        with mock.patch.dict(m.ACKNOWLEDGED_FLIPS, clear=True):
            ok, detail = v.real_held_reread(self.cur, ZZ, files, raw)
        self.assertFalse(ok, detail)
        self.assertIn("no named acknowledgement covers", detail)
        (raw / path.name).write_bytes(path.read_bytes() + b"\0")
        for fn in (v.real_migration_proof, v.real_held_reread):
            ok, detail = fn(self.cur, ZZ, files, raw)
            self.assertFalse(ok)
            self.assertIn("no file", detail)

    def test_a_file_that_is_not_edition_1_fails_the_proof(self):
        path, files = self.migrated()
        other = write_tool(Path(self.tmpdir.name) / "o" / path.name,
                           values={("L0055", "E06000002"):
                                   {"LA_GN_SC_Own": 140}})
        fx = {**files, "sha256": m.content_sha256(other)}
        ok, detail = v.real_migration_proof(self.cur, ZZ, fx, other.parent,
                                            areas=len(CODES), regions=2)
        self.assertFalse(ok)
        self.assertIn("differences", detail)
        ok, detail = v.real_held_reread(self.cur, ZZ, fx, other.parent)
        self.assertFalse(ok)
        self.assertIn("differs", detail)

    def test_reconciliation_and_identity_from_a_file_on_disk(self):
        d = Path(self.tmpdir.name) / "disk"
        good = write_tool(d / "good.xlsx")
        fx = {"sha256": m.content_sha256(good)}
        ok, detail = v.real_reconciliation(fx, d, areas=len(CODES),
                                           regions=2)
        self.assertTrue(ok, detail)
        ok, detail = v.real_reconciliation(fx, d)       # 296 expected
        self.assertFalse(ok)
        bad = write_tool(d / "bad.xlsx", subtotal={"E06000001":
                                                   {"LA_SHHOP": 1}})
        ok, detail = v.real_reconciliation({"sha256": m.content_sha256(bad)},
                                           d, areas=len(CODES), regions=2)
        self.assertFalse(ok)
        self.assertIn("reconciliation failed", detail)
        ok, detail = v.real_reconciliation({"sha256": "0" * 64}, d)
        self.assertFalse(ok)
        self.assertIn("no file", detail)
        # identity: this fixture is version 1.1 of November 2025, the rank
        # the loader surveyed for the held file
        ok, detail = v.real_identity(None, fx, d)
        self.assertTrue(ok, detail)
        old = write_tool(d / "old.xlsx", version="1.0",
                         month="October 2025")
        ok, detail = v.real_identity(None, {"sha256": m.content_sha256(old)},
                                     d)
        self.assertFalse(ok)
        self.assertIn("rank", detail)

    def test_the_throwaway_tables_exist_only_inside_the_transaction(self):
        self.seed()
        self.assertEqual(core.rows_differing(self.cur, ZZ, P, 1), 0)


if __name__ == "__main__":
    unittest.main()
