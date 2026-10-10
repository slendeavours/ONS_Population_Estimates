"""Tests that the real-table gates of s17_marac_editions_verify fail on a
short, empty, drifted or unmigrated state. The gate bodies (real_*) take a
spec, so they are called here against the throwaway zz_s17_* tables (created
inside a transaction that is always rolled back); no real table is touched
(test_the_gates_leave_the_real_tables_as_they_were compares their row counts). Every test ends in a rollback
(also on an exception), and tearDownClass reads the database on a separate
read-only connection and fails if a zz_s17 table was left committed."""
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

import s17_marac_editions as m  # noqa: E402
import s17_marac_editions_verify as v  # noqa: E402
import test_s17_marac_loader as tl  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s17_marac_pure import PFAS  # noqa: E402

ZZ = v.ZZ
N = tl.N
Y = tl.Y


class VerifyGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()
        # a second, read-only view of committed state: what other sessions
        # see, taken before any scenario runs
        cls.conn2 = get_conn()
        cls.conn2.set_session(readonly=True)
        cls.raw_second = cls.conn2.cursor()
        cls.real_before = cls.real_state(None, cls.raw_second)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.rollback()
            cls.conn.close()
            cls.raw_second.close()
            cls.conn2.rollback()
            cls.conn2.close()
        finally:
            left = [t for t in v.leftovers() if t.startswith("zz_s17")]
            if left:
                raise AssertionError(f"committed throwaway tables: {left}")

    def setUp(self):
        self.raw = self.conn.cursor()
        self.cur = v.SafeCur(self.raw)
        self.addCleanup(self.rollback)
        v.setup_throwaway(self.cur)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.e = v.Env(self.cur, self.tmpdir.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(tl.specs(self.e.root, self.e.names))
        self.addCleanup(self.stack.close)

    def rollback(self):
        self.raw.close()
        self.conn.rollback()

    def seed(self, **kw):
        """A loaded 2025-26; returns (path, files)."""
        path = self.e.file(**kw)
        self.e.ok(self.cur, ["load", "--commit"])
        return path, {Y: {"path": path, "sha256": m.content_sha256(path)}}

    def migrated(self, **kw):
        path, files, label = self.e.seed_legacy(self.cur, **kw)
        pre = m.held_records(self.cur, ZZ, label)
        rc, text, _ = v.migrate_cmd(self.e, files, "--commit")
        self.assertEqual(rc, 0, text)
        return path, files, label, pre

    def test_a_cursor_cannot_commit_or_roll_back_the_transaction(self):
        with self.assertRaises(RuntimeError):
            self.cur.connection.commit()
        with self.assertRaises(RuntimeError):
            self.cur.connection.rollback()

    def test_a_seeded_complete_state_passes_the_real_gates(self):
        path, files = self.seed(values={
            "Norfolk": {"housing_referrals": "No Data"}})
        for label, fn in (
                ("2", lambda: v.real_edition1_and_latest(self.cur, ZZ)),
                ("3", lambda: v.real_forces(self.cur, ZZ, names=PFAS,
                                            added=())),
                ("4", lambda: v.real_blanks(self.cur, ZZ, files=files,
                                            names=PFAS, rules=False)),
                ("7", lambda: v.real_ranks(self.cur, ZZ)),
                ("12", lambda: v.real_reread(self.cur, ZZ, files=files,
                                             names=PFAS))):
            ok, detail = fn()
            self.assertTrue(ok, f"{label}: {detail}")
        self.assertEqual(v.shape_problems(self.cur, ZZ), [])

    def test_an_empty_state_is_not_a_pass(self):
        for fn in (v.real_edition1_and_latest, v.real_forces,
                   v.real_blanks):
            ok, detail = fn(self.cur, ZZ)
            self.assertFalse(ok, detail)
            self.assertIn("no live periods", detail)

    def test_an_unmigrated_state_fails_with_a_clear_message(self):
        gone = dataclasses.replace(ZZ, editions_table="zz_s17_not_there")
        for fn in (v.real_edition1_and_latest, v.real_forces, v.real_blanks,
                   v.real_ranks, v.real_reread, v.real_edition1_hash,
                   v.real_map, v.real_migration_proof):
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

    def test_the_gates_leave_the_real_tables_as_they_were(self):
        """The real tables (live, editions, ledger) hold the same rows after
        the throwaway scenarios as before them, whether or not the real
        editions table exists yet; only the zz_s17 copies were touched."""
        self.assertTrue(v.exists(self.cur, ZZ))
        self.assertNotEqual(ZZ.editions_table, v.REAL.editions_table)
        self.assertTrue(ZZ.editions_table.startswith("zz_s17_"))
        self.assertTrue(ZZ.live_table.startswith("zz_s17_"))
        self.seed()
        self.assertEqual(self.real_state(self.cur), type(self).real_before)
        self.assertEqual(self.real_state(self.cur, self.raw_second),
                         type(self).real_before)

    @staticmethod
    def real_state(cur, other=None):
        """Row counts of the three real relations, None where one does not
        exist. `other` is a second cursor on a separate connection, which
        sees only committed state."""
        cur = other if other is not None else cur
        state = {}
        for name in (v.REAL.live_table, v.REAL.editions_table,
                     v.REAL.editions_table + "_file_checks"):
            cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
            if cur.fetchone()[0] is None:
                state[name] = None
                continue
            cur.execute(f"SELECT count(*) FROM public.{name}")
            state[name] = cur.fetchone()[0]
        return state

    def test_drifted_live_cells_fail(self):
        self.seed()
        live = ZZ.live_table
        for sql in (
                f"UPDATE public.{live} SET cases_discussed = "
                "cases_discussed + 1 WHERE pfa_name_safelives = 'Cumbria'",
                f"UPDATE public.{live} SET repeat_cases_pct = NULL WHERE "
                "pfa_name_safelives = 'Cumbria'",
                f"UPDATE public.{live} SET marac_count = marac_count + 1 "
                "WHERE pfa_name_safelives = 'Cumbria'",
                f"DELETE FROM public.{live} WHERE pfa_name_safelives = "
                "'Cumbria'"):
            with v.planted(self.cur, sql):
                ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
            self.assertFalse(ok, sql)
            self.assertIn("edition-only", detail)

    def test_a_period_without_edition_1_fails(self):
        self.seed()
        with v.planted(self.cur, f"UPDATE public.{ZZ.live_table} SET "
                       "financial_year = '2030-31'"):
            ok, detail = v.real_edition1_and_latest(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("no edition 1", detail)

    def test_forces_are_checked_against_the_mapping(self):
        self.seed()
        ok, detail = v.real_forces(self.cur, ZZ, names=PFAS, added=())
        self.assertTrue(ok, detail)
        ok, detail = v.real_forces(self.cur, ZZ, names=PFAS | {"Surrey"},
                                   added=())
        self.assertFalse(ok)
        self.assertIn("missing", detail)
        ok, detail = v.real_forces(self.cur, ZZ, names=PFAS - {"Suffolk"},
                                   added=())
        self.assertFalse(ok)
        self.assertIn("extra", detail)
        with v.planted(self.cur, f"DELETE FROM public.{ZZ.live_table} WHERE "
                       "pfa_name_safelives = 'Suffolk'"):
            ok, detail = v.real_forces(self.cur, ZZ, names=PFAS, added=())
        self.assertFalse(ok)
        self.assertIn("live", detail)

    def test_the_mapping_is_checked_against_the_ons_file(self):
        self.assertEqual(v.mapping_problems(self.cur), [])
        feats = json.loads(v.ONS_JSON.read_text(encoding="utf-8"))
        feats["features"] = [f for f in feats["features"]
                             if f["attributes"]["LAD24CD"] != "E06000001"]
        p = Path(self.tmpdir.name) / "ons.json"
        p.write_text(json.dumps(feats), encoding="utf-8")
        self.assertTrue(any("E06000001" in x for x in
                            v.mapping_problems(self.cur, p)))
        self.assertTrue(v.mapping_problems(self.cur, forces=38))
        self.assertTrue(v.mapping_problems(
            self.cur, Path(self.tmpdir.name) / "none.json"))
        self.assertTrue(v.mapping_problems(self.cur, spelling={}))

    def test_nulls_zeros_and_reasons_are_checked(self):
        path, files = self.seed(values={
            "Norfolk": {"housing_referrals": "No Data"},
            "Suffolk": {"cases_per_10k_adult_females":
                        "No population data"}})
        kw = dict(files=files, names=PFAS, rules=False)
        ok, detail = v.real_blanks(self.cur, ZZ, **kw)
        self.assertTrue(ok, detail)
        self.assertIn("not_submitted 1", detail)
        self.assertIn("no_population 1", detail)
        e = ZZ.editions_table
        base = f"UPDATE public.{e} SET "
        for sql, needle in (
                (base + "marac_count = NULL WHERE pfa_name_safelives = "
                 "'Cumbria'", "no value_flag"),
                (base + "housing_referrals = NULL, value_flag = "
                 "'not_submitted' WHERE pfa_name_safelives = "
                 "'City of London'", "published 0"),
                (base + "housing_referrals = 3, marac_count = NULL WHERE "
                 "pfa_name_safelives = 'Norfolk'",
                 "the edition holds a value")):
            with v.planted(self.cur, sql, editions=True):
                ok, detail = v.real_blanks(self.cur, ZZ, **kw)
            self.assertFalse(ok, sql)
            self.assertIn(needle, detail)
        ok, detail = v.real_blanks(self.cur, ZZ, **kw)
        self.assertTrue(ok, detail)
        self.assertEqual(v.shape_problems(self.cur, ZZ), [])

    def test_the_value_flag_check_is_exercised(self):
        self.assertEqual(v.flag_check_problems(self.cur, ZZ), [])
        # a table without the CHECK would accept a flag on a full row
        with v.planted(self.cur, f"ALTER TABLE public.{ZZ.editions_table} "
                       "DROP CONSTRAINT marac_cases_editions_value_flag_chk"):
            self.assertTrue(v.flag_check_problems(self.cur, ZZ))
            self.assertTrue(any("CHECK" in x for x in
                                v.shape_problems(self.cur, ZZ)))
        self.assertEqual(v.flag_check_problems(self.cur, ZZ), [])

    def test_the_editions_table_is_append_only(self):
        self.seed()
        for sql in (f"UPDATE public.{ZZ.editions_table} SET edition = 5",
                    f"DELETE FROM public.{ZZ.editions_table}",
                    f"TRUNCATE public.{ZZ.editions_table}",
                    f"UPDATE public.{v.LEDGER} SET outcome = 'new'"):
            self.cur.execute("SAVEPOINT t")
            with self.assertRaises(Exception) as ex:
                self.cur.execute(sql)
            self.assertIn("append-only", str(ex.exception))
            self.cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_the_source_check_catches_each_planted_construct(self):
        p = Path(self.tmpdir.name) / "x.py"
        p.write_text(
            '"""Lancashire and Norfolk in a docstring"""\n'
            'NORFOLK_2023_24_NOT_SUBMITTED = {"pfa": "Norfolk", '
            '"columns": ("c",)}\n'
            'LANCASHIRE_RULE = {"pfa": "Lancashire", "columns": ("c",)}\n'
            'ACKNOWLEDGED_FLIPS = {"Lancashire/housing": 1}\n'
            'MSG = "Lancashire in a message"\n'
            'def f(r):\n    return r["k"] == "Kent" and r["h"] == 0\n'
            'def g(r):\n    return r["h"] == 0\n'
            'def h(v):\n    return v or 0\n'
            'def i(v):\n    return v or 0  # not a source value\n'
            'def j(d):\n    return d.get("x", 0)\n'
            'def k(v):\n    return v if v else 0\n'
            'Q = "SELECT COALESCE(x, 0) FROM t"\n'
            'def n(r):\n    return r["k"] == "Kent/housing" and r != 0\n',
            encoding="utf-8")
        pfas = {"Kent", "Norfolk", "Lancashire"}
        plain = v._lines_of(v.source_problems(p, pfas))
        self.assertEqual(plain, {3, 5, 7, 20})
        zeros = v._lines_of(v.source_problems(p, pfas, zeros=True))
        self.assertEqual(zeros, {3, 5, 7, 11, 15, 17, 18, 20})

    def test_the_real_loader_is_clean_and_its_rules_are_the_decided_ones(self):
        pfas = set(m.PFA_NAMES_2026_10) | {"Norfolk", "West Midlands"}
        self.assertEqual(v.source_problems(v.LOADER, pfas, zeros=True), [])
        self.assertEqual(v.zero_rule_state(), [])

    def test_a_third_zero_rule_or_a_lancashire_rule_is_caught(self):
        extra = dict(m.NORFOLK_2023_24_NOT_SUBMITTED, name="X")
        with mock.patch.object(m, "ZERO_RULES", m.ZERO_RULES + (extra,)):
            self.assertTrue(v.zero_rule_state())
        lanc = dict(m.NORFOLK_2023_24_NOT_SUBMITTED, pfa="Lancashire",
                    name="LANCASHIRE_HOUSING_NOT_COUNTED")
        with mock.patch.object(m, "ZERO_RULES",
                               (m.ZERO_RULES[0], m.ZERO_RULES[1], lanc)):
            self.assertTrue(any("Lancashire" in x or "lancashire" in x
                                for x in v.zero_rule_state()))
        wrong = dict(m.ZERO_RULES[1], years=("2024-25",))
        with mock.patch.object(m, "ZERO_RULES", (m.ZERO_RULES[0], wrong)):
            self.assertTrue(v.zero_rule_state())

    def test_a_changed_named_entry_is_caught(self):
        ent = json.loads(json.dumps(
            {k: dict(a, periods={p: {c: list(x) for c, x in d.items()}
                                 for p, d in a["periods"].items()})
             for k, a in m.ACKNOWLEDGED_FLIPS.items()}))
        for k in ent:
            for p, d in ent[k]["periods"].items():
                for c in d:
                    d[c] = tuple(d[c])
        self.assertEqual(v.zero_rule_state(), [])
        # a value going to NULL
        bad = json.loads(json.dumps(ent))
        for p, d in bad[v.FIRST_LOAD_ENTRY]["periods"].items():
            for c in d:
                bad[v.FIRST_LOAD_ENTRY]["periods"][p][c] = ("5.00", None)
            break
        for p, d in bad[v.FIRST_LOAD_ENTRY]["periods"].items():
            for c in d:
                d[c] = tuple(d[c])
        with mock.patch.object(m, "ACKNOWLEDGED_FLIPS", bad):
            self.assertTrue(v.zero_rule_state())
        # a second entry
        two = dict(ent, other=ent[v.FIRST_LOAD_ENTRY])
        with mock.patch.object(m, "ACKNOWLEDGED_FLIPS", two):
            self.assertTrue(v.zero_rule_state())

    def test_the_limits_gate_is_clean_on_the_real_loader(self):
        self.assertEqual(v.limit_problems(), [])
        with mock.patch.object(m, "NEW_MAX_PFAS", 5):
            self.assertTrue(v.limit_problems())

    def test_the_note_lines_are_read_by_regex_and_are_scan_safe(self):
        note = Path(self.tmpdir.name) / "n.md"
        h = "ab" * 32
        note.write_text(
            f"before-state marac_cases 2025-26 rows=38 {v.hash_fields(h)}\n"
            f"before-state-all marac_cases 2025-26 rows=38 "
            f"{v.hash_fields('cd' * 32)}\nw1-read marac_cases 2025-26 "
            f"rows=38 {v.hash_fields('ef' * 32)}\nw1-read-all marac_cases "
            f"2025-26 rows=38 {v.hash_fields('01' * 32)}\n",
            encoding="utf-8")
        self.assertEqual(v.note_hashes(note), {"2025-26": (38, h)})
        self.assertEqual(v.w1_note_hashes(note), {"2025-26": (38, "ef" * 32)})
        self.assertEqual(v.note_hashes(Path(self.tmpdir.name) / "none"), {})
        self.assertNotRegex(note.read_text(encoding="utf-8"),
                            r"[0-9a-f]{64}")

    def test_the_hashes_see_each_column_they_are_defined_on(self):
        rows = [{"pfa_name_safelives": "Kent", **{c: i + 1 for i, c in
                                                  enumerate(m.VALUES)}}]
        base = v.live_columns_sha(rows)
        for col in m.VALUES + ("pfa_name_safelives",):
            self.assertNotEqual(v.live_columns_sha(
                [dict(rows[0], **{col: 99 if col != "pfa_name_safelives"
                                  else "Surrey"})]), base, col)
        self.assertEqual(v.live_columns_sha([dict(
            rows[0], live_source="x", value_flag="not_submitted")]), base)
        w1 = v.w1_read_sha(rows)
        self.assertNotEqual(base, w1)
        self.assertEqual(v.w1_read_sha([dict(rows[0], marac_count=9)]), w1)
        for col in m.MAP_COLUMNS:
            self.assertNotEqual(v.w1_read_sha([dict(rows[0], **{col: 99})]),
                                w1, col)

    def test_edition_1_hash_against_the_note_and_live_before_migration(self):
        path, files, label, pre = self.migrated()
        note = Path(self.tmpdir.name) / "n.md"
        h = v.live_columns_sha(pre)
        line = (f"before-state marac_cases {label} rows={len(pre)} "
                f"{v.hash_fields(h)}")
        kw = dict(legacy_rows=len(pre), years=1)
        note.write_text(line + "\n", encoding="utf-8")
        ok, detail = v.real_edition1_hash(self.cur, ZZ, note, **kw)
        self.assertTrue(ok, detail)
        for text, needle in (
                (line.replace(h[:32], "0" * 32), "differs"),
                (line.replace(f"rows={len(pre)}", f"rows={len(pre) + 1}"),
                 "differs"),
                (line.replace("before-state ", "before-state-all "),
                 "records no"),
                (line + "\n" + line.replace(label, "2019-20"),
                 "no edition 1 as loaded"),
                ("", "records no")):
            note.write_text(text, encoding="utf-8")
            ok, detail = v.real_edition1_hash(self.cur, ZZ, note, **kw)
            self.assertFalse(ok, text)
            self.assertIn(needle, detail)
        note.write_text(line, encoding="utf-8")
        for kw2 in (dict(legacy_rows=len(pre) + 5, years=1),
                    dict(legacy_rows=len(pre), years=2)):
            ok, detail = v.real_edition1_hash(self.cur, ZZ, note, **kw2)
            self.assertFalse(ok)
            self.assertIn("surveyed", detail)
        ok, detail = v.real_edition1_hash(self.cur, ZZ,
                                          Path(self.tmpdir.name) / "x", **kw)
        self.assertFalse(ok)
        self.assertIn("not written", detail)

    def test_the_map_gate_holds_the_held_forces_and_west_midlands(self):
        wm = {"cases_discussed": 7810, "cases_per_10k_adult_females":
              65.903502}
        path, files, label = self.e.seed_legacy(self.cur,
                                                values={"West Midlands": wm})
        held = [r for r in m.held_records(self.cur, ZZ, label)]
        note = Path(self.tmpdir.name) / "n.md"
        n = len(held)
        line = (f"w1-read marac_cases {label} rows={n} "
                f"{v.hash_fields(v.w1_read_sha(held))}")
        note.write_text(line + "\n", encoding="utf-8")
        kw = dict(note=note, year=label, held=n, mapping=False,
                  councils={"West Midlands": 0})
        rc, text, _ = v.migrate_cmd(self.e, files, "--commit")
        self.assertEqual(rc, 0, text)
        ok, detail = v.real_map(self.cur, ZZ, **kw)
        self.assertFalse(ok)
        self.assertIn("West Midlands has no", detail)
        v.go_live(self.e, path, label)
        ok, detail = v.real_map(self.cur, ZZ, **kw)
        self.assertTrue(ok, detail)
        self.assertIn("7810.00 cases", detail)
        live = ZZ.live_table
        for sql, needle in (
                (f"UPDATE public.{live} SET cases_discussed = "
                 "cases_discussed + 1 WHERE pfa_name_safelives = 'Cumbria'",
                 "differs from the note"),
                (f"UPDATE public.{live} SET cases_per_10k_adult_females = "
                 "NULL WHERE pfa_name_safelives = 'Cumbria'",
                 "differs from the note"),
                (f"UPDATE public.{live} SET cases_discussed = 7811 WHERE "
                 "pfa_name_safelives = 'West Midlands'", "expected"),
                (f"UPDATE public.{live} SET financial_year = '2030-31'",
                 "pinned")):
            with v.planted(self.cur, sql):
                ok, detail = v.real_map(self.cur, ZZ, **kw)
            self.assertFalse(ok, sql)
            self.assertIn(needle, detail)
        ok, detail = v.real_map(self.cur, ZZ, **dict(kw, held=n + 1))
        self.assertFalse(ok)
        self.assertIn("held forces, expected", detail)
        ok, detail = v.real_map(self.cur, ZZ, **dict(
            kw, note=Path(self.tmpdir.name) / "x"))
        self.assertFalse(ok)
        self.assertIn("not written", detail)

    def test_the_migration_proof_catches_a_changed_edition_1(self):
        path, files, label, pre = self.migrated()
        ent = v.entry_for(label)
        ok, detail = v.real_migration_proof(self.cur, ZZ, files, PFAS,
                                            entry=ent)
        self.assertTrue(ok, detail)
        self.assertIn("2 NULL-to-0 cells", detail)
        ed = ZZ.editions_table
        for sql in (f"UPDATE public.{ed} SET cases_discussed = "
                    "cases_discussed + 1 WHERE pfa_name_safelives = "
                    "'Cumbria'",
                    f"UPDATE public.{ed} SET marac_count = NULL WHERE "
                    "pfa_name_safelives = 'Cumbria'",
                    f"DELETE FROM public.{ed} WHERE pfa_name_safelives = "
                    "'Suffolk'"):
            with v.planted(self.cur, sql, editions=True):
                ok, detail = v.real_migration_proof(self.cur, ZZ, files,
                                                    PFAS, entry=ent)
            self.assertFalse(ok, sql)
            self.assertIn("differences", detail)
        ok, detail = v.real_migration_proof(
            self.cur, ZZ, files, PFAS, entry=v.entry_for(label, {
                "City of London/housing_referrals": (None, "0.00")}))
        self.assertFalse(ok)
        self.assertIn("named entry", detail)
        ok, detail = v.real_migration_proof(
            self.cur, ZZ, files, PFAS, entry=ent,
            expect={"rows": 1, "restored": 0, "keys_added": 0,
                    "rule_held": 0})
        self.assertFalse(ok)
        self.assertIn("surveyed", detail)

    def test_a_reread_that_differs_from_the_stored_edition_fails(self):
        path, files = self.seed()
        ok, detail = v.real_reread(self.cur, ZZ, files=files, names=PFAS)
        self.assertTrue(ok, detail)
        other = self.e.file(values={"Cumbria": {"cases_discussed": 7}})
        bad = {Y: {"path": other, "sha256": m.content_sha256(other)}}
        ok, detail = v.real_reread(self.cur, ZZ, files=bad, names=PFAS)
        self.assertFalse(ok)
        self.assertIn("not yet loaded", detail)
        wrong = {Y: {"path": path, "sha256": "0" * 64}}
        ok, detail = v.real_reread(self.cur, ZZ, files=wrong, names=PFAS)
        self.assertFalse(ok)
        self.assertIn("sha256", detail)

    def test_a_tip_rank_naming_another_year_fails(self):
        self.seed()
        ok, detail = v.real_ranks(self.cur, ZZ)
        self.assertTrue(ok, detail)
        with v.planted(self.cur, f"UPDATE public.{ZZ.editions_table} SET "
                       "source_file = replace(source_file, 'SafeLives "
                       "2025-26', 'SafeLives 2024-25')", editions=True):
            ok, detail = v.real_ranks(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("rank says", detail)

    def test_reconciliation_and_identity_of_a_held_file_are_checked(self):
        path, files = self.seed()
        ok, detail = v.real_reconcile(files)
        self.assertTrue(ok, detail)
        bad = self.e.file(england_delta=1)
        ok, detail = v.real_reconcile({Y: {"path": bad, "sha256":
                                           m.content_sha256(bad)}})
        self.assertFalse(ok)
        self.assertIn("sum to", detail)
        ok, detail = v.real_reconcile({Y: {"path": path, "sha256": "0" * 64}})
        self.assertFalse(ok)
        ok, detail = v.real_identity(files)
        self.assertTrue(ok, detail)
        skewed = self.e.file(notes_year=2025)
        ok, detail = v.real_identity({Y: {"path": skewed, "sha256":
                                          m.content_sha256(skewed)}})
        self.assertFalse(ok)

    def test_this_module_and_its_test_hold_no_64_hex_run(self):
        for p in (Path(v.__file__), Path(__file__)):
            self.assertIsNone(re.search(r"[0-9a-f]{64}",
                                        p.read_text(encoding="utf-8")), p)


if __name__ == "__main__":
    unittest.main()
