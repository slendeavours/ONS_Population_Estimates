"""Tests for geography (the per-dataset Barnsley and Sheffield rule).

The pure tests need nothing. The database tests use a connection from
_db.get_conn() and are skipped when no database is reachable; they read
la_code_lookup, la_boundaries and source_registry, and the load_recodes test
works on two TEMP tables created inside a transaction that is always rolled
back, so nothing persists.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geography as g  # noqa: E402

FB = g.RECODES_FALLBACK
OLD = ["E08000016", "E08000019"]
NEW = ["E08000038", "E08000039"]


def _conn_or_skip():
    try:
        from _db import get_conn
        return get_conn()
    except BaseException as e:  # noqa: BLE001 (no .env exits; no server raises)
        raise unittest.SkipTest(f"no database: {e}")


def _declare(**forms):
    """A DATASET_FORM patch: {code: form} with a placeholder evidence."""
    return {k: (v, "test") for k, v in forms.items()}


class Canonical(unittest.TestCase):
    def test_fallback_is_the_two_recodes(self):
        self.assertEqual(FB, {"E08000038": "E08000016",
                              "E08000039": "E08000019"})

    def test_new_codes_map_to_canonical(self):
        rec = dict(FB, E08000016="E08000016", E08000019="E08000019")
        self.assertEqual(g.canonical("E08000038", rec), "E08000016")
        self.assertEqual(g.canonical("E08000039", rec), "E08000019")

    def test_canonical_codes_map_to_themselves(self):
        rec = dict(FB, E08000016="E08000016", E08000019="E08000019")
        self.assertEqual(g.canonical("E08000016", rec), "E08000016")
        self.assertEqual(g.canonical("E08000019", rec), "E08000019")

    def test_other_codes_pass_through(self):
        for c in ("E06000001", "E09000033", "W06000015", ""):
            self.assertEqual(g.canonical(c, FB), c)

    def test_flipped_lookup_flips_the_canonical_form(self):
        # la_boundaries moved to a vintage with the new codes: the lookup is
        # reversed and nothing else changes.
        rec = {"E08000016": "E08000038", "E08000038": "E08000038"}
        self.assertEqual(g.canonical("E08000016", rec), "E08000038")
        self.assertEqual(g.canonical("E08000038", rec), "E08000038")


class CheckForms(unittest.TestCase):
    def setUp(self):
        self._saved = g.DATASET_FORM
        g.DATASET_FORM = _declare(o="old", n="new", m="mixed", z="none",
                                  u="unverified")
        self.addCleanup(setattr, g, "DATASET_FORM", self._saved)

    def test_old_passes_old_codes_and_fails_new(self):
        self.assertEqual(g.check_forms("o", OLD + ["E06000001"]), [])
        out = g.check_forms("o", ["E06000001", "E08000038"])
        self.assertEqual(len(out), 1)
        for part in ("'o'", "old", "E08000038"):
            self.assertIn(part, out[0])

    def test_new_passes_new_codes_and_fails_old(self):
        self.assertEqual(g.check_forms("n", NEW), [])
        out = g.check_forms("n", ["E08000019", "E08000039"])
        self.assertEqual(len(out), 1)
        for part in ("'n'", "new", "E08000019"):
            self.assertIn(part, out[0])
        self.assertNotIn("E08000039", out[0].split("carries")[1])

    def test_old_or_new_checked_inside_by_period_too(self):
        out = g.check_forms("o", [], by_period={"2025Q1": ["E08000039"]})
        self.assertTrue(out)
        self.assertIn("E08000039", out[0])

    def test_none_fails_either_form(self):
        self.assertEqual(g.check_forms("z", ["E06000001"]), [])
        for codes in (["E08000016"], ["E08000038"], OLD + NEW):
            out = g.check_forms("z", codes)
            self.assertEqual(len(out), 1, codes)
            self.assertIn("none", out[0])
            for c in codes:
                self.assertIn(c, out[0])

    def test_mixed_accepts_either_form_across_periods(self):
        self.assertEqual(g.check_forms("m", OLD + NEW), [])
        self.assertEqual(g.check_forms(
            "m", [], by_period={"2024Q4": OLD, "2025Q1": NEW}), [])
        # Barnsley new while Sheffield still old in one period: no clash
        self.assertEqual(g.check_forms(
            "m", [], by_period={"2025Q1": ["E08000038", "E08000019"]}), [])

    def test_mixed_fails_a_period_with_both_forms_of_one_area(self):
        out = g.check_forms("m", [], by_period={
            "2024Q4": OLD, "2025Q1": ["E08000016", "E08000038", "E08000019"]})
        self.assertEqual(len(out), 1)
        for part in ("2025Q1", "E08000016", "E08000038", "mixed"):
            self.assertIn(part, out[0])
        out = g.check_forms("m", [], by_period={"2025Q1": OLD + NEW})
        self.assertEqual(len(out), 2)

    def test_unverified_never_a_problem(self):
        self.assertEqual(g.check_forms("u", OLD + NEW,
                                       by_period={"p": OLD + NEW}), [])

    def test_unknown_source_raises_keyerror(self):
        with self.assertRaises(KeyError) as cm:
            g.check_forms("99", OLD)
        self.assertIn("99", str(cm.exception))

    def test_confirm_note_only_for_unverified(self):
        self.assertIsNone(g.confirm_note("o"))
        note = g.confirm_note("u")
        self.assertIn("unverified", note)
        self.assertNotIn("\n", note)


class Declarations(unittest.TestCase):
    FORMS = {"old", "new", "mixed", "none", "unverified"}

    def test_every_entry_has_a_known_form_and_evidence(self):
        for code, (form, evidence) in g.DATASET_FORM.items():
            self.assertIn(form, self.FORMS, code)
            self.assertTrue(evidence.strip(), code)

    def test_s15_declared_new(self):
        self.assertEqual(g.DATASET_FORM["15"][0], "new")

    def test_declaration_report_lists_every_source(self):
        rep = g.declaration_report()
        lines = rep.splitlines()
        self.assertTrue(lines[0].startswith("| Source | Form | Evidence |"))
        body = lines[2:]
        self.assertEqual(len(body), len(g.DATASET_FORM))
        for code, (form, _) in g.DATASET_FORM.items():
            self.assertTrue(any(l.startswith(f"| {code} | {form} |")
                                for l in body), code)

    def test_report_orders_sources_naturally(self):
        codes = [l.split("|")[1].strip()
                 for l in g.declaration_report().splitlines()[2:]]
        self.assertEqual(codes, sorted(codes, key=g.source_sort_key))
        self.assertLess(codes.index("2"), codes.index("10"))
        self.assertLess(codes.index("1"), codes.index("1b"))


class _Cur:
    """A stand-in cursor for resolve: answers the two load_recodes queries."""

    def __init__(self, lookup, boundaries):
        self.rows, self.lookup, self.boundaries = None, lookup, boundaries

    def execute(self, sql, params=None):
        self.rows = (self.lookup if "change_type" in sql else
                     [(c,) for c in self.boundaries])

    def fetchall(self):
        return list(self.rows)


class Resolve(unittest.TestCase):
    def setUp(self):
        self._saved = g.DATASET_FORM
        g.DATASET_FORM = _declare(o="old", n="new", u="unverified")
        self.addCleanup(setattr, g, "DATASET_FORM", self._saved)
        self.cur = _Cur([("E08000038", "E08000016"),
                         ("E08000039", "E08000019")],
                        ["E08000016", "E08000019", "E06000001"])

    def test_map_covers_only_the_recoded_areas(self):
        mp, problems = g.resolve(self.cur, "n", ["E08000038", "E08000039",
                                                 "E06000001"])
        self.assertEqual(mp, {"E08000038": "E08000016",
                              "E08000039": "E08000019"})
        self.assertEqual(problems, [])

    def test_disagreement_is_returned_as_a_problem(self):
        mp, problems = g.resolve(self.cur, "o", ["E08000038", "E08000016"])
        self.assertEqual(mp, {"E08000038": "E08000016",
                              "E08000016": "E08000016"})
        self.assertEqual(len(problems), 1)
        self.assertIn("E08000038", problems[0])

    def test_unknown_source_raises_before_any_query(self):
        class Boom:
            def execute(self, *a, **k):
                raise AssertionError("queried")
        with self.assertRaises(KeyError):
            g.resolve(Boom(), "99", OLD)

    def test_by_period_reaches_check_forms(self):
        self.cur.lookup = [("E08000038", "E08000016")]
        _, problems = g.resolve(self.cur, "o", [],
                                by_period={"p": ["E08000038"]})
        self.assertTrue(problems)


class LoadRecodesPure(unittest.TestCase):
    def test_target_outside_boundaries_raises(self):
        cur = _Cur([("E08000038", "E08000016")], ["E06000001"])
        with self.assertRaises(ValueError) as cm:
            g.load_recodes(cur)
        self.assertIn("E08000016", str(cm.exception))

    def test_no_recode_rows_raises(self):
        with self.assertRaises(ValueError):
            g.load_recodes(_Cur([], ["E08000016"]))

    def test_two_targets_for_one_code_raises(self):
        cur = _Cur([("E08000038", "E08000016"), ("E08000038", "E08000019")],
                   ["E08000016", "E08000019"])
        with self.assertRaises(ValueError):
            g.load_recodes(cur)

    def test_bad_table_name_refused(self):
        with self.assertRaises(ValueError):
            g.load_recodes(_Cur([], []), lookup="x; drop table y")


class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = _conn_or_skip()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_load_recodes_on_a_throwaway_lookup(self):
        cur = self.conn.cursor()
        try:
            cur.execute("CREATE TEMP TABLE zz_geo_lookup "
                        "(LIKE public.la_code_lookup)")
            cur.execute("CREATE TEMP TABLE zz_geo_boundaries "
                        "(lad24cd varchar(9))")
            cur.execute("INSERT INTO zz_geo_boundaries VALUES "
                        "('E08000016'), ('E08000019'), ('E06000063')")
            cur.execute("""INSERT INTO zz_geo_lookup
                (old_code, new_code, la_name, change_type) VALUES
                ('E08000038', 'E08000016', 'Barnsley', 'recode'),
                ('E08000039', 'E08000019', 'Sheffield', 'recode'),
                ('E08000016', 'E08000016', 'Barnsley', 'current'),
                ('E07000026', 'E06000063', 'Allerdale', 'new_unitary')""")
            out = g.load_recodes(cur, lookup="zz_geo_lookup",
                                 boundaries="zz_geo_boundaries")
            self.assertEqual(out, {"E08000038": "E08000016",
                                   "E08000039": "E08000019",
                                   "E08000016": "E08000016",
                                   "E08000019": "E08000019"})
        finally:
            cur.close()
            self.conn.rollback()

    def test_load_recodes_on_the_real_lookup(self):
        cur = self.conn.cursor()
        try:
            out = g.load_recodes(cur)
        finally:
            cur.close()
            self.conn.rollback()
        for new, old in FB.items():
            self.assertEqual(out[new], old)
            self.assertEqual(out[old], old)

    def test_every_registered_source_is_declared(self):
        cur = self.conn.cursor()
        try:
            cur.execute("SELECT source_code FROM public.source_registry")
            codes = {r[0] for r in cur.fetchall()}
        finally:
            cur.close()
            self.conn.rollback()
        self.assertTrue(codes)
        self.assertEqual(sorted(codes - set(g.DATASET_FORM)), [])
        self.assertEqual(sorted(set(g.DATASET_FORM) - codes), [])


if __name__ == "__main__":
    unittest.main()
