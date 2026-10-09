"""Tests for editions_core: several whole-period columns (whole_period_cols)
and editions-only columns skipped by key-change inserts (S23 Task 1).

Every database test runs on a connection from _db.get_conn() inside a
transaction that is always rolled back. The only tables touched are the
throwaway zz_ec_wp_live and zz_ec_wp_editions, created inside that
transaction, so they never persist. Nothing here commits.

The test spec mirrors S23's shape: the editions table carries two
editions-only provenance columns (file_a, file_b) that the live table does
not have; refresh_from maps them to the live columns label_a, label_b, and
both are whole-period columns.
"""
import dataclasses
import sys
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import editions_core as core  # noqa: E402
from _db import get_conn  # noqa: E402

WP = core.EditionSpec(
    name="zz_ec_wp",
    live_table="zz_ec_wp_live",
    editions_table="zz_ec_wp_editions",
    key_cols=("lad24cd",),
    period_col="period",
    value_cols=(("value", "integer"),),
    extra_cols=(("file_a", "text"), ("file_b", "text")),
    refresh_cols=("value", "label_a", "label_b"),
    refresh_from=(("label_a", "file_a"), ("label_b", "file_b")),
    whole_period_cols=("label_a", "label_b"),
    fk_la_boundaries=False,
)
WP_KC = dataclasses.replace(WP, refresh_key_changes=True)


@contextmanager
def rolled_back(conn):
    """A cursor in a transaction that is rolled back whatever happens."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_ec_wp_live'), "
                    "to_regclass('public.zz_ec_wp_editions')")
        if cur.fetchone() != (None, None):
            raise RuntimeError("zz_ec_wp_live or zz_ec_wp_editions already "
                               "exists as a real table; refusing to run")
        cur.execute("CREATE TABLE public.zz_ec_wp_live (lad24cd varchar(9), "
                    "period varchar(10), value integer, label_a text, "
                    "label_b text, "
                    "loaded_at timestamptz NOT NULL DEFAULT now())")
        yield cur
    finally:
        cur.close()
        conn.rollback()


def edition(cur, spec, period, rows, sha, supersedes):
    return core.insert_edition(
        cur, spec, rows, period, release_label="test",
        published_date=date(2026, 1, 1), source_file="f.xlsx",
        source_sha256=sha, supersedes=supersedes)


def r(lad, value, a, b):
    return {"lad24cd": lad, "value": value, "file_a": a, "file_b": b}


def setup_two_periods(cur, spec):
    """Live and edition 1 for 2025Q1 and 2025Q2, labels a1/b1 and q2a/q2b."""
    core.create_schema(cur, spec)
    for period, a, b in (("2025Q1", "a1", "b1"), ("2025Q2", "q2a", "q2b")):
        rows = [r("E06000001", 10, a, b), r("E06000002", 20, a, b)]
        for x in rows:
            cur.execute("INSERT INTO public.zz_ec_wp_live (lad24cd, period, "
                        "value, label_a, label_b) VALUES (%s, %s, %s, %s, %s)",
                        (x["lad24cd"], period, x["value"], a, b))
        edition(cur, spec, period, rows, f"{period}-1", None)


def live_rows(cur):
    cur.execute("SELECT period, lad24cd, value, label_a, label_b "
                "FROM public.zz_ec_wp_live ORDER BY 1, 2")
    return cur.fetchall()


class WholePeriodDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_two_whole_period_columns_set_on_every_row(self):
        with rolled_back(self.conn) as cur:
            setup_two_periods(cur, WP)
            q2 = core.period_hashes(cur, WP, "live")["2025Q2"]
            # edition 2 of 2025Q1: one value revised, new provenance on both
            edition(cur, WP, "2025Q1", [r("E06000001", 11, "a2", "b2"),
                                        r("E06000002", 20, "a2", "b2")],
                    "2025Q1-2", 1)
            res = core.refresh_latest(cur, WP)
            self.assertEqual(res["updated"], {"2025Q1": 1})
            self.assertEqual(res["rows"], 1)
            self.assertEqual(live_rows(cur), [
                ("2025Q1", "E06000001", 11, "a2", "b2"),
                ("2025Q1", "E06000002", 20, "a2", "b2"),
                ("2025Q2", "E06000001", 10, "q2a", "q2b"),
                ("2025Q2", "E06000002", 20, "q2a", "q2b")])
            self.assertEqual(core.period_hashes(cur, WP, "live")["2025Q2"], q2)
            self.assertTrue(core.status(cur, WP)["ok"])
            self.assertEqual(core.refresh_latest(cur, WP)["rows"], 0)

    def test_after_check_fails_on_planted_row_not_equal_to_tip(self):
        with rolled_back(self.conn) as cur:
            setup_two_periods(cur, WP)
            edition(cur, WP, "2025Q1", [r("E06000001", 11, "a2", "b2"),
                                        r("E06000002", 20, "a2", "b2")],
                    "2025Q1-2", 1)
            before = live_rows(cur)

            def plant(c, plan):
                c.execute("UPDATE public.zz_ec_wp_live SET label_a = 'x' "
                          "WHERE period = '2025Q1' AND lad24cd = 'E06000002'")
            with self.assertRaises(SystemExit) as cm:
                core.refresh_latest(cur, WP, _after_update_hook=plant)
            msg = str(cm.exception)
            self.assertIn("rolled back", msg)
            self.assertIn("label_a", msg)
            self.assertNotIn("label_b", msg)
            self.assertEqual(live_rows(cur), before)

    def test_after_check_fails_when_tip_is_not_uniform(self):
        with rolled_back(self.conn) as cur:
            setup_two_periods(cur, WP)
            edition(cur, WP, "2025Q1", [r("E06000001", 11, "a2", "b2"),
                                        r("E06000002", 20, "a2x", "b2")],
                    "2025Q1-2", 1)
            before = live_rows(cur)
            with self.assertRaises(SystemExit) as cm:
                core.refresh_latest(cur, WP)
            msg = str(cm.exception)
            self.assertIn("label_a", msg)
            self.assertIn("not uniform", msg)
            self.assertNotIn("label_b", msg)
            self.assertEqual(live_rows(cur), before)

    def test_key_change_insert_skips_editions_only_columns(self):
        with rolled_back(self.conn) as cur:
            setup_two_periods(cur, WP_KC)
            # edition 2: drops E06000002, adds E06000003; E06000001 unchanged
            edition(cur, WP_KC, "2025Q1", [r("E06000001", 10, "a2", "b2"),
                                           r("E06000003", 30, "a2", "b2")],
                    "2025Q1-2", 1)
            res = core.refresh_latest(cur, WP_KC,
                                      accept_key_changes=("2025Q1",))
            self.assertEqual((res["inserted"], res["deleted"]),
                             ({"2025Q1": 1}, {"2025Q1": 1}))
            self.assertEqual(live_rows(cur)[:2], [
                ("2025Q1", "E06000001", 10, "a2", "b2"),
                ("2025Q1", "E06000003", 30, "a2", "b2")])
            self.assertTrue(core.status(cur, WP_KC)["ok"])


def old_insert_cols(spec):
    """_insert_added's column lists before S23 (the reference)."""
    mapped = dict(spec.refresh_from)
    data = tuple(c for c in spec.data_cols if c not in mapped)
    meta = tuple(c for c in spec.update_cols if c not in spec.data_cols)
    live_cols = data + meta + tuple(lc for lc, _ in spec.refresh_from)
    src = (tuple(f"e.{c}" for c in data + meta)
           + tuple(f"e.{ec}" for _, ec in spec.refresh_from))
    return live_cols, src


class InsertColumns(unittest.TestCase):
    def test_editions_only_cols(self):
        self.assertEqual(WP.editions_only_cols, ("file_a", "file_b"))
        live_cols, src = core._insert_cols(WP)
        self.assertNotIn("file_a", live_cols)
        self.assertNotIn("file_b", live_cols)
        self.assertEqual(live_cols, ("lad24cd", "period", "value",
                                     "label_a", "label_b"))
        self.assertEqual(src, ("e.lad24cd", "e.period", "e.value",
                               "e.file_a", "e.file_b"))

    def test_insert_cols_unchanged_without_editions_only_columns(self):
        SPEC = core.EditionSpec(   # test_editions_core's SPEC
            name="zz_core", live_table="zz_core_live",
            editions_table="zz_core_editions", key_cols=("lad24cd",),
            period_col="period", value_cols=(("value", "integer"),),
            refresh_cols=("value",), fk_la_boundaries=False)
        specs = {
            "core": dataclasses.replace(SPEC, refresh_key_changes=True),
            "core_source": dataclasses.replace(
                SPEC, refresh_cols=("value", "source"),
                refresh_from=(("source", "source_file"),),
                as_loaded_source_col="source",
                refresh_source_whole_period=True, refresh_key_changes=True),
        }
        specs.update(real_specs(opted_in=False))
        self.assertIn("s4_care_leaver_editions.SPEC", specs)
        for name, spec in specs.items():
            with self.subTest(spec=name):
                self.assertEqual(spec.editions_only_cols, ())
                self.assertEqual(core._insert_cols(spec),
                                 old_insert_cols(spec))

    def test_real_specs_whole_period_set_unchanged(self):
        """Every real spec: no whole_period_cols; the whole-period set is
        the source column exactly when refresh_source_whole_period."""
        specs = real_specs(opted_in=False)
        self.assertGreaterEqual(len(specs), 12)
        for name, spec in specs.items():
            with self.subTest(spec=name):
                self.assertEqual(spec.whole_period_cols, ())
                want = ((spec.as_loaded_source_col,
                         dict(spec.refresh_from)[spec.as_loaded_source_col]),
                        ) if spec.refresh_source_whole_period else ()
                self.assertEqual(spec.whole_period_pairs, want)


# real specs that opt in to whole_period_cols and editions-only columns by
# design (the engine options were added for them); the "unchanged" tests
# cover every other real spec, and WholePeriodOptIn checks these
OPTED_IN = {"s23_rsh_stock_editions.SPEC"}


def real_specs(opted_in=True) -> dict:
    """{'module.NAME': spec} of every EditionSpec defined at module level by
    the editions loaders (only imported, nothing run); without the OPTED_IN
    specs when opted_in is false."""
    import importlib
    out = {}
    for path in sorted(Path(__file__).resolve().parents[1].glob(
            "*_editions.py")):
        mod = importlib.import_module(path.stem)
        for attr, val in vars(mod).items():
            if isinstance(val, core.EditionSpec):
                out[f"{path.stem}.{attr}"] = val
    if not opted_in:
        out = {k: v for k, v in out.items() if k not in OPTED_IN}
    return out


class WholePeriodOptIn(unittest.TestCase):
    def test_s23_opts_in_to_both(self):
        spec = real_specs()["s23_rsh_stock_editions.SPEC"]
        prov = ("edition", "publication_date", "source_url", "source_file",
                "release_page_url")
        self.assertEqual(spec.whole_period_cols, prov)
        self.assertEqual(spec.editions_only_cols,
                         ("file_edition", "file_publication_date",
                          "file_source_url", "file_name",
                          "file_release_page_url"))
        live_cols, _ = core._insert_cols(spec)
        self.assertFalse(set(spec.editions_only_cols) & set(live_cols))
        self.assertTrue(set(prov) <= set(live_cols))


class WholePeriodValidation(unittest.TestCase):
    def test_whole_period_pairs(self):
        self.assertEqual(WP.whole_period_pairs,
                         (("label_a", "file_a"), ("label_b", "file_b")))

    def test_unmapped_column_refused(self):
        with self.assertRaises(ValueError) as cm:
            dataclasses.replace(WP, whole_period_cols=("value",))
        self.assertIn("refresh_from", str(cm.exception))

    def test_not_a_refresh_column_refused(self):
        with self.assertRaises(ValueError) as cm:
            dataclasses.replace(WP, whole_period_cols=("label_c",))
        self.assertIn("not a refresh column", str(cm.exception))

    def test_duplicate_refused(self):
        with self.assertRaises(ValueError) as cm:
            dataclasses.replace(WP, whole_period_cols=("label_a", "label_a"))
        self.assertIn("twice", str(cm.exception))

    def test_not_an_identifier_refused(self):
        with self.assertRaises(ValueError):
            dataclasses.replace(WP, whole_period_cols=("label a",))

    def test_source_flag_alone_means_the_as_loaded_source_col(self):
        spec = dataclasses.replace(WP, whole_period_cols=(),
                                   as_loaded_source_col="label_a",
                                   refresh_source_whole_period=True)
        self.assertEqual(spec.whole_period_pairs, (("label_a", "file_a"),))

    def test_both_union_de_duplicated(self):
        spec = dataclasses.replace(WP, as_loaded_source_col="label_b",
                                   refresh_source_whole_period=True)
        self.assertEqual(spec.whole_period_pairs,
                         (("label_b", "file_b"), ("label_a", "file_a")))
        spec = dataclasses.replace(WP, whole_period_cols=("label_a",),
                                   as_loaded_source_col="label_b",
                                   refresh_source_whole_period=True)
        self.assertEqual(spec.whole_period_pairs,
                         (("label_b", "file_b"), ("label_a", "file_a")))

    def test_default_is_none(self):
        spec = dataclasses.replace(WP, whole_period_cols=())
        self.assertEqual(spec.whole_period_pairs, ())


if __name__ == "__main__":
    unittest.main()
