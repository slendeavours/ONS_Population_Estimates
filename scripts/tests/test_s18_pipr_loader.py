"""Tests for the S18 loader in s18_pipr_editions (spec, fetch, file records,
window, the per-period checks and the commands).

No network: fetch_latest_file is tested with a stub session, and every `load`
here is given a small workbook built in the test. Database tests run on a
connection from _db.get_conn() inside a transaction that is always rolled
back (rolled_back below); nothing here commits: the commands get a stand-in
connection (_Borrowed) whose commit only counts, and a cursor proxy whose
.connection is a stand-in too, so the engine's per-period commit never
reaches the real connection. The only tables touched are the throwaway
zz_s18_live and zz_s18_editions (a copy of SPEC named zz_s18), created inside
that transaction (refused if either already exists), so they never persist.
The reference lookups (la_boundaries, la_code_lookup) and the run log are
stubbed; the real la_code_lookup recode rows are read (never written).
"""
import contextlib
import dataclasses
import io
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
import s18_pipr_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s18_pipr_pure import write_workbook  # noqa: E402

ZZ = dataclasses.replace(
    m.SPEC, name="zz_s18", live_table="zz_s18_live",
    editions_table="zz_s18_editions", fk_la_boundaries=False)
N = 12
CODES = [f"E060000{i:02d}" for i in range(1, N + 1)]
SPARE = "E06000099"           # a valid code no period normally has
P1, P2, P3, P4 = "2026-04-01", "2026-05-01", "2026-06-01", "2026-07-01"
BLOCKS = len(m.BLOCKS)


def cover(edition):
    d = m.edition_date(edition)
    return ("The data tables in this spreadsheet were originally published "
            f"at 9:30am on {d.day} {d.strftime('%B')} {d.year}. Crown "
            "copyright.")


def rent(period, i, bump=None):
    base = 1000 + 100 * int(period[5:7]) + 10 * i
    return base + (bump or {}).get((period, CODES[i] if i < N else SPARE), 0)


def write_edition(d, edition, periods, *, codes=None, bump=None, extra=(),
                  overrides=None, drop=(), name=None, text=None):
    """pipr_<edition>.xlsx in directory d. codes {period: [codes]} (default
    CODES); bump {(period, code): +x} on every block's rent; extra
    [(period, code)] rows added; overrides {(period, code): {column: value}};
    A Welsh row and a pre-window row are always there (both out of scope)."""
    codes = codes or {}
    rows = [(datetime(2021, 12, 1), CODES[0]),
            (datetime.fromisoformat(periods[-1]), "W06000015")]
    for p in periods:
        for c in codes.get(p, CODES):
            i = CODES.index(c) if c in CODES else N
            kw = {"rent": rent(p, i, bump), "index": 100.0 + i,
                  "change": 1.5}
            if overrides and (p, c) in overrides:
                kw["overrides"] = overrides[(p, c)]
            rows.append((datetime.fromisoformat(p), c, kw))
    for p, c in extra:
        rows.append((datetime.fromisoformat(p), c))
    path = Path(d) / (name or f"pipr_{edition}.xlsx")
    write_workbook(path, rows, drop=drop,
                   cover_text=text if text is not None else cover(edition))
    return path


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _FakeConn:
    def __init__(self, real):
        self.encoding = real.encoding
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Cur:
    def __init__(self, cur):
        self._cur = cur
        self.connection = _FakeConn(cur.connection)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _Borrowed:
    def __init__(self, cur):
        self.cur = _Cur(cur)
        self.commits = 0
        self.rollbacks = 0

    @contextmanager
    def cursor(self):
        yield self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


@contextmanager
def rolled_back(conn):
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_s18_live'), "
                    "to_regclass('public.zz_s18_editions')")
        if cur.fetchone() != (None, None):
            raise RuntimeError("zz_s18_live or zz_s18_editions already exists "
                               "as a real table; refusing to run")
        cur.execute("""CREATE TABLE public.zz_s18_live (
            lad24cd varchar(9) NOT NULL, period date NOT NULL,
            breakdown_type varchar(20) NOT NULL, category varchar(30) NOT NULL,
            mean_rent numeric(8,2), rent_index numeric(8,2),
            annual_pct_change numeric(6,2), provisional boolean DEFAULT false,
            source text, loaded_at timestamptz DEFAULT now(),
            PRIMARY KEY (lad24cd, period, breakdown_type, category))""")
        core.create_schema(cur, ZZ)
        yield cur
    finally:
        cur.close()
        conn.rollback()


def editions(cur, period=None):
    """[(edition, supersedes, rows)] of the throwaway editions table."""
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM "
                "public.zz_s18_editions"
                + (" WHERE period = %s" if period else "")
                + " GROUP BY 1, 2 ORDER BY 1", (period,) if period else None)
    return cur.fetchall()


def live(cur, period, bt="all", cat="all", col="mean_rent"):
    """{lad24cd: value} of one block of the throwaway live table."""
    cur.execute(f"SELECT lad24cd, {col} FROM public.zz_s18_live WHERE "
                "period = %s AND breakdown_type = %s AND category = %s",
                (period, bt, cat))
    return {a: (int(b) if col == "mean_rent" and b is not None else b)
            for a, b in cur.fetchall()}


def live_rows(cur, period):
    cur.execute("SELECT COUNT(*) FROM public.zz_s18_live WHERE period = %s",
                (period,))
    return cur.fetchone()[0]


class LoaderDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def dir(self):
        self.n += 1
        d = Path(self.tmp.name) / str(self.n)
        d.mkdir()
        return d

    def run_main(self, cur, argv, *, file=None, table=True, fetch=None):
        """main(argv) on the throwaway tables; file is passed as --file.
        Returns (rc or 'halt', text, borrowed conn, log mock)."""
        borrowed = _Borrowed(cur)
        args = list(argv)
        if file:
            args += ["--file", str(file)]
        out = io.StringIO()
        with mock.patch.object(m, "SPEC", ZZ), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "reference_codes",
                                  return_value=(set(CODES) | {SPARE,
                                                              "E08000016",
                                                              "E08000019"}, {})), \
                mock.patch.object(m, "fetch_latest_file",
                                  side_effect=fetch or AssertionError(
                                      "no download in tests")), \
                mock.patch.object(m, "log_run") as logged, \
                mock.patch.object(m, "table_exists",
                                  side_effect=lambda c, t: table
                                  and pe.table_exists(c, t)), \
                contextlib.redirect_stdout(out):
            try:
                rc = m.main(args)
            except SystemExit as e:
                return "halt", f"{out.getvalue()}\n{e.code}", borrowed, logged
        return rc, out.getvalue(), borrowed, logged

    def seed(self, cur, periods=(P1, P2, P3), edition="22july2026"):
        f = write_edition(self.dir(), edition, list(periods))
        rc, text, _, _ = self.run_main(
            cur, ["load", "--commit", "--min-period", periods[0],
                  "--expected-areas", str(N)], file=f)
        self.assertEqual(rc, 0, text)
        return f

    # -- the brief's tests --------------------------------------------------

    def test_new_period_edition_1_and_live_in_one_transaction(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.assertEqual(live_rows(cur, P3), N * BLOCKS)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4])
            rc, text, conn, _ = self.run_main(cur, ["load", "--commit"],
                                              file=f)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, P4), [(1, None, N * BLOCKS)])
            self.assertEqual(live(cur, P4), {c: rent(P4, i) for i, c
                                             in enumerate(CODES)})
            self.assertEqual(core.rows_differing(cur, ZZ, P4, 1), 0)
            self.assertIn(f"{P4}: new", text)
            cur.execute("SELECT DISTINCT release_label, source_file, "
                        "source_sha256 FROM public.zz_s18_editions "
                        "WHERE period = %s", (P4,))
            (label, src, sha), = cur.fetchall()
            self.assertEqual(label, "ONS PIPR 19august2026 edition")
            self.assertIn("pipr_19august2026.xlsx", src)
            self.assertIn(m.content_sha256(f)[:16], src)
            self.assertEqual(len(sha), 64)
            # live source names the edition that inserted the rows
            cur.execute("SELECT DISTINCT source FROM public.zz_s18_live "
                        "WHERE period = %s", (P4,))
            self.assertEqual(cur.fetchall(),
                             [("ONS PIPR 19august2026 edition",)])
            # the old provisional month became final in this edition: a
            # revision, pending until refresh-latest
            self.assertEqual(m.status(cur, ZZ)["pending_refresh"], [P3])

        # a failure inserting the live rows rolls back the edition too
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4])
            with mock.patch.object(m, "insert_live",
                                   side_effect=RuntimeError("boom")):
                rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                    file=f)
            self.assertEqual(rc, 1, text)
            self.assertEqual(editions(cur, P4), [])
            self.assertEqual(live_rows(cur, P4), 0)
            logged.assert_not_called()

    def test_revised_period_edition_2_live_untouched_until_refresh(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live(cur, P2)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              bump={(P2, CODES[1]): 500,
                                    (P2, CODES[3]): -250})
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 0, text)
            self.assertIn(f"{P2}: revised, {2 * BLOCKS} rows changed", text)
            self.assertEqual(editions(cur, P2),
                             [(1, None, N * BLOCKS), (2, 1, N * BLOCKS)])
            self.assertEqual(editions(cur, P1), [(1, None, N * BLOCKS)])
            self.assertEqual(live(cur, P2), before)
            self.assertEqual(m.status(cur, ZZ)["pending_refresh"], [P2, P3])
            self.assertIn("largest rent/index revisions", text)
            rc, text, _, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertEqual(live(cur, P2)[CODES[1]], rent(P2, 1) + 500)
            self.assertEqual(live(cur, P2)[CODES[3]], rent(P2, 3) - 250)
            self.assertTrue(m.status(cur, ZZ)["ok"])

    def test_provisional_to_final_is_a_revision_and_refresh_brings_live(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)      # P3 is the file's latest month: provisional
            cur.execute("SELECT DISTINCT provisional FROM public.zz_s18_live "
                        "WHERE period = %s", (P3,))
            self.assertEqual(cur.fetchall(), [(True,)])
            cur.execute("SELECT DISTINCT provisional FROM public.zz_s18_live "
                        "WHERE period = %s", (P2,))
            self.assertEqual(cur.fetchall(), [(False,)])
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4])
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 0, text)
            # P3's numbers are unchanged; only the provisional flag moved
            self.assertIn(f"{P3}: revised, {N * BLOCKS} rows changed", text)
            self.assertIn("provisional " + str(N * BLOCKS), text)
            self.assertEqual(editions(cur, P3),
                             [(1, None, N * BLOCKS), (2, 1, N * BLOCKS)])
            cur.execute("SELECT DISTINCT provisional FROM public.zz_s18_live "
                        "WHERE period = %s", (P3,))
            self.assertEqual(cur.fetchall(), [(True,)])      # not yet
            rc, text, _, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, 0, text)
            cur.execute("SELECT DISTINCT provisional FROM public.zz_s18_live "
                        "WHERE period = %s", (P3,))
            self.assertEqual(cur.fetchall(), [(False,)])
            cur.execute("SELECT DISTINCT provisional FROM public.zz_s18_live "
                        "WHERE period = %s", (P4,))
            self.assertEqual(cur.fetchall(), [(True,)])
            self.assertTrue(m.status(cur, ZZ)["ok"])

    def test_unchanged_period_stores_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            f = self.seed(cur)
            cur.execute("SELECT COUNT(*), MAX(loaded_at) FROM "
                        "public.zz_s18_live")
            before = cur.fetchone()
            for _ in range(2):
                rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                               file=f)
                self.assertEqual(rc, 0, text)
                self.assertIn(f"{P1}: unchanged", text)
                self.assertIn(f"{P3}: unchanged", text)
            self.assertEqual(editions(cur), [(1, None, 3 * N * BLOCKS)])
            cur.execute("SELECT COUNT(*), MAX(loaded_at) FROM "
                        "public.zz_s18_live")
            self.assertEqual(cur.fetchone(), before)

    def test_older_file_halts_unless_allowed(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f8 = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                               bump={(P2, CODES[0]): 7})
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f8)
            self.assertEqual(rc, 0, text)
            before = (editions(cur), live(cur, P4))
            # last month's file, in preview, commit and simulate
            old = write_edition(self.dir(), "22july2026", [P1, P2, P3],
                                bump={(P2, CODES[1]): 9})
            for argv in (["load"], ["load", "--commit"], ["load", "--simulate"]):
                with mock.patch.object(pe, "load_periods") as lp:
                    rc, text, _, logged = self.run_main(cur, argv, file=old)
                self.assertEqual(rc, "halt", argv)
                self.assertIn("already held", text)
                self.assertIn("--allow-older-file", text)
                lp.assert_not_called()
                logged.assert_not_called()
            self.assertEqual((editions(cur), live(cur, P4)), before)
            rc, text, _, _ = self.run_main(
                cur, ["load", "--allow-older-file"], file=old)
            self.assertEqual(rc, 0, text)
            self.assertIn("--allow-older-file given", text)
            # a rerun of the same edition and a newer file are unaffected
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f8)
            self.assertEqual(rc, 0, text)
            self.assertNotIn("--allow-older-file", text)
            f9 = write_edition(self.dir(), "16september2026",
                               [P1, P2, P3, P4, "2026-08-01"])
            rc, text, _, _ = self.run_main(cur, ["load"], file=f9)
            self.assertEqual(rc, 0, text)
            self.assertNotIn("--allow-older-file", text)

    def test_older_file_guard_uses_the_live_source_before_editions_exist(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("UPDATE public.zz_s18_live SET source = %s",
                        ("ONS PIPR 19august2026 edition",))
            self.assertEqual(m.latest_held_edition(cur, ZZ, True),
                             "19august2026")
            old = write_edition(self.dir(), "22july2026", [P1, P2, P3])
            rc, text, _, _ = self.run_main(cur, ["load"], file=old)
            self.assertEqual(rc, "halt")
            self.assertIn("19august2026", text)

    def test_older_file_problem_pure(self):
        f = m.older_file_problem
        self.assertIsNone(f("22july2026", ["2026-06-01"], ["2026-06-01"],
                            "22july2026"))
        self.assertIsNone(f("19august2026", ["2026-07-01"], ["2026-06-01"],
                            None))
        self.assertIn("22july2026", f("17june2026", ["2026-05-01"],
                                      ["2026-05-01"], "22july2026"))
        self.assertIn("already held", f("17june2026", ["2026-05-01"],
                                        ["2026-06-01"], None))

    def test_missing_breakdown_block_halts(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              drop=("Annual change terraced",))
            with mock.patch.object(pe, "load_periods") as lp:
                rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                    file=f)
            self.assertEqual(rc, "halt")
            self.assertIn("Annual change terraced", text)
            lp.assert_not_called()
            logged.assert_not_called()
            self.assertEqual(editions(cur, P4), [])

    def test_short_period_and_period_missing_an_area_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              codes={P4: CODES[:-1]},
                              bump={(P2, CODES[0]): 1})
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                file=f)
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P4}: REJECTED", text)
            self.assertIn(f"{N - 1} areas, expected {N}", text)
            self.assertEqual(editions(cur, P4), [])
            self.assertEqual(live_rows(cur, P4), 0)
            # the other periods of the same run went through
            self.assertEqual(editions(cur, P2)[-1][0], 2)
            logged.assert_not_called()
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              codes={P4: CODES[:-1] + [SPARE]})
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 1, text)
            self.assertIn(CODES[-1], text)
            self.assertEqual(editions(cur, P4), [])

    def test_stop_conditions_reject_the_period(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            null6 = {(P2, c): {"Rental price": None} for c in CODES[:6]}
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              overrides=null6)
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P2}: REJECTED, not stored: STOP CONDITION: NULL "
                          "replaces a number in 6 areas", text)
            self.assertEqual(editions(cur, P2), [(1, None, N * BLOCKS)])
            self.assertEqual(editions(cur, P4), [(1, None, N * BLOCKS)])
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            null5 = {(P2, c): {"Rental price": None} for c in CODES[:5]}
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              overrides=null5)
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 0, text)       # five is within the limit
            self.assertEqual(editions(cur, P2)[-1][0], 2)
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              bump={(P3, c): 2000 for c in CODES[:11]})
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 1, text)
            self.assertIn("revised above 50% in 11 areas", text)
            self.assertEqual(editions(cur, P3), [(1, None, N * BLOCKS)])

    def test_zero_rent_rejected_and_blank_is_null(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              overrides={(P4, CODES[2]): {"Rental price": 0}})
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 1, text)
            self.assertIn("zero or negative rent", text)
            self.assertEqual(editions(cur, P4), [])
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              overrides={(P4, CODES[2]): {
                                  "Rental price one bed": "[x]",
                                  "Annual change": None}})
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 0, text)
            self.assertIsNone(live(cur, P4, "bedroom", "1_bed")[CODES[2]])
            self.assertIsNone(live(cur, P4, "all", "all",
                                   "annual_pct_change")[CODES[2]])
            self.assertEqual(live(cur, P4, "bedroom", "2_bed")[CODES[2]],
                             rent(P4, 2) + 2)

    def test_unresolved_code_hard_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              extra=[(P4, "E06000098")])
            with mock.patch.object(load_checks, "check_codes",
                                   return_value=["UNEXPLAINED E06000098"]) as cc, \
                    mock.patch.object(pe, "load_periods") as lp:
                rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                               file=f)
            self.assertEqual(rc, "halt")
            self.assertIn("UNEXPLAINED E06000098", text)
            self.assertEqual(list(cc.call_args.args[1]), ["E06000098"])
            lp.assert_not_called()
            self.assertEqual(editions(cur, P4), [])

    def test_barnsley_sheffield_form_disagreement_halts(self):
        # S18 is declared 'new': a file carrying E08000016 stops the load
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              extra=[(P4, "E08000016")])
            with mock.patch.object(pe, "load_periods") as lp:
                rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                               file=f)
            self.assertEqual(rc, "halt")
            for part in ("declared 'new'", "E08000016", "geography.py"):
                self.assertIn(part, text)
            lp.assert_not_called()

    def test_recode_map_comes_from_geography_resolve(self):
        with rolled_back(self.conn) as cur:
            f = write_edition(self.dir(), "19august2026", [P4],
                              extra=[(P4, "E08000038")])
            with quiet(), mock.patch.object(
                    m, "reference_codes",
                    return_value=(set(CODES) | {"E08000016"}, {})), \
                    mock.patch.object(m.geography, "resolve",
                                      wraps=m.geography.resolve) as rs:
                by_period, ed = m.load_file_records(
                    f, _Borrowed(cur), min_period=m.date(2026, 4, 1))
            self.assertEqual(ed, "19august2026")
            self.assertEqual(rs.call_args.args[1], "18")
            self.assertIn("E08000038", rs.call_args.args[2])
            self.assertIn("E08000016",
                          [r["lad24cd"] for r in by_period[P4]])
            self.assertNotIn("E08000038",
                             [r["lad24cd"] for r in by_period[P4]])

    def test_file_identity_mismatch_halts_before_any_write(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cases = [
                # the Cover sheet names another edition
                write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              text=cover("22july2026")),
                # no publication date on the Cover sheet
                write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              text="Crown copyright."),
                # the edition is not one month after the latest month
                write_edition(self.dir(), "16september2026",
                              [P1, P2, P3, P4]),
                # a name that names no edition
                write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              name="prices.xlsx"),
            ]
            for f in cases:
                with mock.patch.object(pe, "apply_period",
                                       side_effect=AssertionError("wrote")), \
                        mock.patch.object(pe, "load_periods") as lp:
                    rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                                   file=f)
                self.assertEqual(rc, "halt", f)
                self.assertIn("identity", text)
                lp.assert_not_called()
            self.assertEqual(editions(cur, P4), [])

    def test_preview_default_and_simulate_rolls_back(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              bump={(P2, CODES[0]): 7})
            for flags, word in (([], "PREVIEW"), (["--simulate"], "SIMULATION")):
                rc, text, conn, logged = self.run_main(cur, ["load"] + flags,
                                                       file=f)
                self.assertEqual(rc, 0, text)
                self.assertIn(word, text)
                self.assertEqual(conn.commits, 0)
                self.assertEqual(conn.cur.connection.commits, 0)
                logged.assert_not_called()
                self.assertEqual(editions(cur, P4), [])
                self.assertEqual(editions(cur, P2), [(1, None, N * BLOCKS)])
                self.assertEqual(live_rows(cur, P4), 0)
            # with no editions table the preview compares with the LIVE table
            rc, text, _, _ = self.run_main(cur, ["load"], file=f, table=False)
            self.assertEqual(rc, 0, text)
            self.assertIn("compares each period with the LIVE table", text)
            rc, text, _, _ = self.run_main(cur, ["load", "--simulate"],
                                           file=f, table=False)
            self.assertEqual(rc, "halt")
            self.assertIn("does not exist yet", text)

    def test_nothing_held_requires_min_period(self):
        with rolled_back(self.conn) as cur:
            f = write_edition(self.dir(), "22july2026", [P1, P2, P3])
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, "halt")
            self.assertIn("--min-period", text)
            rc, text, _, _ = self.run_main(cur, ["load"])   # no download
            self.assertEqual(rc, "halt")
            self.assertNotIn("no download in tests", text)
            rc, text, _, _ = self.run_main(
                cur, ["load", "--min-period", P1], file=f)
            self.assertEqual(rc, "halt")
            self.assertIn("--expected-areas", text)

    def test_window_never_loads_before_earliest_held(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, periods=(P2, P3, P4), edition="19august2026")
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4])
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, P1), [])
            self.assertIn("earlier than the earliest held period", text)
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit", "--min-period", P1], file=f)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, P1), [(1, None, N * BLOCKS)])

    def test_main_downloads_nothing_when_file_given(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4])
            calls = []

            def fake_fetch(*a, **kw):
                calls.append(a)
                return f, "19august2026", "https://example/x.xlsx"
            rc, text, _, _ = self.run_main(cur, ["load"], fetch=fake_fetch)
            self.assertEqual(rc, 0, text)
            self.assertEqual(len(calls), 1)
            # the landing page naming another edition than the file halts
            rc, text, _, _ = self.run_main(
                cur, ["load"], fetch=lambda: (f, "16september2026", "u"))
            self.assertEqual(rc, "halt")
            self.assertIn("landing page named edition", text)

    def test_status_flags_period_missing_from_live(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.assertTrue(m.status(cur, ZZ)["ok"])
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4])
            with quiet():
                rows = m.parse_workbook(f)
            recs = m.group_by_period(m.build_records(rows, {}))
            core.insert_edition(cur, ZZ, recs[P4], P4, release_label="t",
                                published_date=datetime(2026, 10, 8).date(),
                                source_file="t", source_sha256="h",
                                supersedes=None)
            st = m.status(cur, ZZ)
            self.assertEqual(st["live_missing"], [P4])
            self.assertFalse(st["ok"])
            self.assertIn("ACTION NEEDED", m.format_status(st))
            rc, text, _, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1)
            rc, text, _, _ = self.run_main(cur, ["status"], table=False)
            self.assertEqual(rc, 1)
            self.assertIn("does not exist yet", text)
            # load repairs the stranded month: live rows, no new edition
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, P4), [(1, None, N * BLOCKS)])
            self.assertEqual(live_rows(cur, P4), N * BLOCKS)

    def test_sync_new_records_edition_1_from_live(self):
        with rolled_back(self.conn) as cur:
            f = write_edition(self.dir(), "22july2026", [P1, P2])
            # live rows as the old pipeline left them (no editions yet)
            recs = m.group_by_period(m.build_records(
                m.parse_workbook(f), {}))
            prof = m.run_profile(ZZ, "22july2026", N)
            for p in (P1, P2):
                m.insert_live(cur, prof, p, recs[p])
            rc, text, _, _ = self.run_main(cur, ["sync-new",
                                                 "--expected-areas", str(N)])
            self.assertEqual(rc, 0, text)
            self.assertIn("DRY RUN", text)
            self.assertEqual(editions(cur), [])
            rc, text, _, _ = self.run_main(
                cur, ["sync-new", "--commit", "--expected-areas", str(N)])
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur), [(1, None, 2 * N * BLOCKS)])
            self.assertTrue(m.status(cur, ZZ)["ok"])

    def test_run_log_only_on_commit(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_edition(self.dir(), "19august2026", [P1, P2, P3, P4],
                              bump={(P2, CODES[2]): 3})
            for flags in ([], ["--simulate"]):
                _, _, _, logged = self.run_main(cur, ["load"] + flags, file=f)
                logged.assert_not_called()
            rc, text, conn, logged = self.run_main(cur, ["load", "--commit"],
                                                   file=f)
            self.assertEqual(rc, 0, text)
            logged.assert_called_once()
            rows, notes = logged.call_args.args[1:3]
            # P4 new (edition + live rows), P2 revised, P3 provisional->final
            self.assertEqual(rows, 4 * N * BLOCKS)
            self.assertIn(f"new {P4}", notes)
            self.assertIn(f"revised {P2}, {P3}", notes)
            self.assertIn("pipr_19august2026.xlsx", notes)
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                file=f)
            self.assertEqual(rc, 0, text)
            self.assertEqual(logged.call_args.args[1], 0)
            self.assertIn("Nothing new", logged.call_args.args[2])
        c = mock.MagicMock()
        m.log_run(c, 7, "notes")
        (sql, params), = [x.args for x in c.execute.call_args_list]
        self.assertEqual(params[:4], ("Source 18 - ONS Private Rents",
                                      "18", "18", 7))

    def test_cell_summary_counts_areas_for_the_stop_conditions(self):
        from decimal import Decimal as D
        prof = m.run_profile(ZZ, "19august2026", 1)

        def rec(rent_, change, prov=False, cat="all"):
            return {"lad24cd": "A", "period": P1, "breakdown_type": "bedroom",
                    "category": cat, "mean_rent": rent_, "rent_index": D(100),
                    "annual_pct_change": change, "provisional": prov}
        old = {("A", "bedroom", "all"): (D(100), D(100), D("0.10"), False),
               ("A", "bedroom", "1_bed"): (D(100), D(100), D("1.00"), False)}
        new = [rec(D(160), D("1.30")), rec(None, D("1.00"), True, "1_bed")]
        with mock.patch.object(pe, "stored", return_value=(old, "live")):
            s = m.cell_summary(None, prof, {P1: new}, [P1], "live")
        self.assertEqual(s["by_col"]["provisional"], 1)
        self.assertEqual(s["null_for_number"], 1)
        self.assertEqual(s["over_50pct"], 1)
        # annual change is counted but not ranked as a relative revision
        self.assertEqual([x[3] for x in s["largest"]], ["mean_rent"])
        self.assertEqual(s["per_period"][P1], {"null_areas": 1,
                                               "big_areas": 1})
        self.assertEqual(m.stop_problems(s), {})

    def test_real_spec(self):
        self.assertEqual(m.SPEC.name, "s18")
        self.assertEqual(m.SPEC.live_table, "la_private_rents")
        self.assertEqual(m.SPEC.editions_table, "la_private_rents_editions")
        self.assertEqual(m.SPEC.key_cols,
                         ("lad24cd", "breakdown_type", "category"))
        self.assertEqual(m.SPEC.period_col, "period")
        self.assertEqual(dict(m.SPEC.value_cols), {
            "mean_rent": "numeric(8,2)", "rent_index": "numeric(8,2)",
            "annual_pct_change": "numeric(6,2)", "provisional": "boolean"})
        self.assertEqual(m.SPEC.refresh_cols, m.VALUE_COLUMNS)
        self.assertTrue(m.SPEC.fk_la_boundaries)
        self.assertEqual(m.SPEC.as_loaded_date, "latest")
        self.assertEqual((m.RUN_AGENT, m.RUN_SOURCE),
                         ("Source 18 - ONS Private Rents", "18"))
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, ZZ)     # idempotent (second time)
            cur.execute("""SELECT column_name, data_type, numeric_precision,
                                  numeric_scale FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = 'zz_s18_editions'""")
            cols = {r[0]: r[1:] for r in cur.fetchall()}
            self.assertEqual(cols["period"][0], "date")
            self.assertEqual(cols["provisional"][0], "boolean")
            self.assertEqual(cols["mean_rent"], ("numeric", 8, 2))
            self.assertEqual(cols["annual_pct_change"], ("numeric", 6, 2))
            self.assertIn("category", cols)


# ---------------------------------------------------------------------------
# Download (stub session)
# ---------------------------------------------------------------------------

LINK_PATH = ("/file?uri=/economy/inflationandpriceindices/datasets/"
             "priceindexofprivaterentsukmonthlypricestatistics/"
             "19august2026/priceindexofprivaterentsukmonthlypricestatistics13"
             ".xlsx")
LANDING_HTML = (f'<html><a href="{LINK_PATH}">Download</a>'
                '<a href="/file?uri=/economy/inflationandpriceindices/'
                'datasets/priceindexofprivaterentsukmonthlypricestatistics/'
                '22july2026/priceindexofprivaterentsukmonthlypricestatistics'
                '12.xlsx">older</a></html>')


class _Resp:
    def __init__(self, body, status=200):
        self.content = body if isinstance(body, bytes) else body.encode()
        self.text = self.content.decode("utf-8", errors="replace")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kw):
        self.calls.append((url, headers or {}))
        for prefix, body in self.pages.items():
            if url.startswith(prefix):
                return _Resp(body)
        return _Resp(b"not found", 404)


class Fetch(unittest.TestCase):
    def workbook_bytes(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        return write_edition(d, "19august2026", [P1, P2]).read_bytes()

    def fetch(self, pages, min_bytes=1000):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        s = _Session(pages)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(m, "MIN_FILE_BYTES", min_bytes):
            try:
                got = m.fetch_latest_file(session=s, dest=Path(d) / "raw")
            except SystemExit as e:
                return "halt", str(e.code), s, Path(d)
        return got, out.getvalue(), s, Path(d)

    def test_fetch_takes_the_first_link_and_names_the_file_by_edition(self):
        body = self.workbook_bytes()
        pages = {m.LANDING: LANDING_HTML, m.BASE_URL + LINK_PATH: body}
        (path, edition, link), _, s, d = self.fetch(pages)
        self.assertEqual(edition, "19august2026")
        self.assertEqual(path, d / "raw" / "pipr_19august2026.xlsx")
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(link, m.BASE_URL + LINK_PATH)
        self.assertEqual([u for u, _ in s.calls],
                         [m.LANDING, m.BASE_URL + LINK_PATH])
        self.assertTrue(all("User-Agent" in h for _, h in s.calls))

    def _fetch_into(self, d, body):
        s = _Session({m.LANDING: LANDING_HTML, m.BASE_URL + LINK_PATH: body})
        out = io.StringIO()
        with contextlib.redirect_stdout(out),                 mock.patch.object(m, "MIN_FILE_BYTES", 1000):
            got = m.fetch_latest_file(session=s, dest=Path(d) / "raw")
        return got, out.getvalue()

    def test_fetch_never_overwrites_an_existing_file_with_different_bytes(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        first = self.workbook_bytes()
        (p1, _, _), _ = self._fetch_into(d, first)
        self.assertEqual(p1.read_bytes(), first)
        d2 = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d2, True))
        other = write_edition(d2, "19august2026", [P1]).read_bytes()
        self.assertNotEqual(other, first)
        (p2, edition, _), text = self._fetch_into(d, other)
        self.assertEqual(p1.read_bytes(), first)          # untouched
        self.assertNotEqual(p2, p1)
        self.assertEqual(p2.read_bytes(), other)          # new one kept
        self.assertEqual(edition, "19august2026")
        self.assertIn("already exists with different content", text)
        self.assertIn(p2.name, text)
        self.assertEqual(sorted(x.name for x in (Path(d) / "raw").iterdir()),
                         sorted([p1.name, p2.name]))      # no temp left

    def test_fetch_same_bytes_again_keeps_the_one_file(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        body = self.workbook_bytes()
        (p1, _, _), _ = self._fetch_into(d, body)
        (p2, _, _), text = self._fetch_into(d, body)
        self.assertEqual(p1, p2)
        self.assertIn("same content", text)
        self.assertEqual(len(list((Path(d) / "raw").iterdir())), 1)

    def test_fetch_bad_download_leaves_an_existing_file_alone(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        body = self.workbook_bytes()
        (p1, _, _), _ = self._fetch_into(d, body)
        with self.assertRaises(SystemExit):
            self._fetch_into(d, b"not a workbook" * 200)
        self.assertEqual(p1.read_bytes(), body)
        self.assertEqual(len(list((Path(d) / "raw").iterdir())), 1)

    def test_fetch_halts_on_a_changed_layout_or_a_bad_download(self):
        body = self.workbook_bytes()
        cases = (
            ({m.LANDING: "<html>New layout: see Reports</html>"},
             "New layout: see Reports", 1000),
            ({m.LANDING: LANDING_HTML, m.BASE_URL + LINK_PATH: b"tiny"},
             "bytes", 1000),
            ({m.LANDING: LANDING_HTML, m.BASE_URL + LINK_PATH: body},
             "bytes", 10 ** 9),
            ({m.LANDING: LANDING_HTML,
              m.BASE_URL + LINK_PATH: b"not a workbook" * 200},
             "not a usable workbook", 1000),
            ({}, "failed", 1000),
        )
        for pages, words, mb in cases:
            got, msg, _, d = self.fetch(pages, mb)
            self.assertEqual(got, "halt", words)
            self.assertIn(words, msg)
            self.assertEqual(list((d / "raw").glob("*.xlsx"))
                             if (d / "raw").exists() else [], [], words)


if __name__ == "__main__":
    unittest.main()
