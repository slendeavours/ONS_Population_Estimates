"""Tests for the S15 loader in s15_hpi_editions (spec, fetch, file records,
window, the per-period checks and the commands).

No network: fetch_latest_files is tested with a stub session (pages trimmed
from the real gov.uk markup and tiny CSV bodies), and every `load` here is
given local files. Database tests run on a connection from _db.get_conn()
inside a transaction that is always rolled back (rolled_back below); nothing
here commits: the commands get a stand-in connection (_Borrowed) whose
commit only counts, and a cursor proxy whose .connection is a stand-in too,
so the engine's per-period commit never reaches the real connection. The
only tables touched are the throwaway zz_s15_live and zz_s15_editions (a
copy of SPEC named zz_s15), created inside that transaction (refused if
either already exists), so they never persist. The reference lookups
(la_boundaries, la_code_lookup) and the run log are stubbed.
"""
import contextlib
import dataclasses
import io
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
import s15_hpi_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402

ZZ = dataclasses.replace(
    m.SPEC, name="zz_s15", live_table="zz_s15_live",
    editions_table="zz_s15_editions", fk_la_boundaries=False)
N = 5
CODES = [f"E0600000{i}" for i in range(1, N + 1)]
SPARE = "E06000009"           # a valid code no period normally has
P1, P2, P3 = "2026-05-01", "2026-06-01", "2026-07-01"

AVG_HEAD = ("Date,Region_Name,Area_Code,Average_Price,Monthly_Change,"
            "Annual_Change,Average_Price_SA")
PT_HEAD = ("Date,Region_Name,Area_Code,Detached_Average_Price,Detached_Index,"
           "Detached_Monthly_Change,Detached_Annual_Change,"
           "Semi_Detached_Average_Price,Semi_Detached_Index,"
           "Semi_Detached_Monthly_Change,Semi_Detached_Annual_Change,"
           "Terraced_Average_Price,Terraced_Index,Terraced_Monthly_Change,"
           "Terraced_Annual_Change,Flat_Average_Price,Flat_Index,"
           "Flat_Monthly_Change,Flat_Annual_Change")


def price(period, i, bump=None):
    """A deterministic price for area i in period; bump {(period, code): +x}."""
    base = 200000 + 1000 * i + 10 * int(period[5:7])
    return base + (bump or {}).get((period, CODES[i] if i < N else SPARE), 0)


def write_files(d, edition, periods, *, codes=None, bump=None, pt_periods=None,
                extra=(), avg_name=None, pt_name=None):
    """The two CSVs in directory d; codes {period: [codes]} (default CODES),
    pt_periods the periods the property-type file holds (default all),
    extra (Area_Code, Date) rows added to the average-prices file. A Welsh
    row and a pre-window row are always there (both out of scope)."""
    codes = codes or {}
    pt_periods = periods if pt_periods is None else pt_periods
    avg = [AVG_HEAD, "2021-12-01,Hartlepool,E06000001,1,,1.0,1",
           f"{periods[-1]},Cardiff,W06000015,5,,1.0,5"]
    pt = [PT_HEAD]
    for p in periods:
        for c in codes.get(p, CODES):
            i = CODES.index(c) if c in CODES else N
            v = price(p, i, bump)
            avg.append(f"{p},Area {c},{c},{v},0.1,1.5,{v + 5}")
            if p in pt_periods:
                pt.append(f"{p},Area {c},{c},{v + 100},1,0,0,{v + 50},1,0,0,"
                          f"{v - 50},1,0,0,{v - 100},1,0,0")
    for c, p in extra:
        avg.append(f"{p},Area {c},{c},123,0,1.0,124")
    a = Path(d) / (avg_name or f"Average-prices-{edition}.csv")
    b = Path(d) / (pt_name or f"Average-prices-Property-Type-{edition}.csv")
    a.write_text("\n".join(avg) + "\n", encoding="utf-8")
    b.write_text("\n".join(pt) + "\n", encoding="utf-8")
    return a, b


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def file_records(f, cur):
    """records_by_period of the two files f (stub reference codes)."""
    with quiet(), mock.patch.object(m, "reference_codes",
                                    return_value=(set(CODES) | {SPARE}, {})):
        return m.load_file_records(f[0], f[1], _Borrowed(cur))[0]


class _FakeConn:
    """cur.connection for the engine: commit and rollback only count (the
    real transaction is rolled back by rolled_back)."""

    def __init__(self, real):
        self.encoding = real.encoding
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Cur:
    """The real cursor with a stand-in .connection."""

    def __init__(self, cur):
        self._cur = cur
        self.connection = _FakeConn(cur.connection)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _Borrowed:
    """A connection stand-in for the commands: hands out the proxy cursor,
    and never commits, rolls back or closes the real connection."""

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
    """A cursor in a transaction that is rolled back whatever happens."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_s15_live'), "
                    "to_regclass('public.zz_s15_editions')")
        if cur.fetchone() != (None, None):
            raise RuntimeError("zz_s15_live or zz_s15_editions already exists "
                               "as a real table; refusing to run")
        cur.execute("""CREATE TABLE public.zz_s15_live (
            lad24cd varchar(9) NOT NULL, period date NOT NULL,
            avg_price_all numeric(12,2), avg_price_all_sa numeric(12,2),
            annual_change_pct numeric(6,2), avg_price_detached numeric(12,2),
            avg_price_semi numeric(12,2), avg_price_terraced numeric(12,2),
            avg_price_flat numeric(12,2),
            loaded_at timestamptz DEFAULT now(),
            PRIMARY KEY (lad24cd, period))""")
        core.create_schema(cur, ZZ)
        yield cur
    finally:
        cur.close()
        conn.rollback()


def editions(cur, period=None):
    """[(edition, supersedes, rows)] of the throwaway editions table."""
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM public.zz_s15_editions"
                + (" WHERE period = %s" if period else "")
                + " GROUP BY 1, 2 ORDER BY 1", (period,) if period else None)
    return cur.fetchall()


def live(cur, period):
    """{lad24cd: avg_price_all} of the throwaway live table."""
    cur.execute("SELECT lad24cd, avg_price_all FROM public.zz_s15_live "
                "WHERE period = %s", (period,))
    return {a: int(b) if b is not None else None for a, b in cur.fetchall()}


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

    def run_main(self, cur, argv, *, files=None, table=True, fetch=None):
        """main(argv) on the throwaway tables; files (avg, pt) are passed as
        --avg-prices/--property-type. Returns (rc or 'halt', text, conn,
        log mock)."""
        borrowed = _Borrowed(cur)
        args = list(argv)
        if files:
            args += ["--avg-prices", str(files[0]),
                     "--property-type", str(files[1])]
        out = io.StringIO()
        with mock.patch.object(m, "SPEC", ZZ), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "reference_codes",
                                  return_value=(set(CODES) | {SPARE}, {})), \
                mock.patch.object(m, "fetch_latest_files",
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

    def seed(self, cur, periods=(P1, P2), edition="2026-06"):
        """First run: nothing held, --min-period and --expected-areas."""
        f = write_files(self.dir(), edition, list(periods))
        rc, text, _, _ = self.run_main(
            cur, ["load", "--commit", "--min-period", periods[0],
                  "--expected-areas", str(N)], files=f)
        self.assertEqual(rc, 0, text)
        return f

    # -- the brief's tests --------------------------------------------------

    def test_new_period_edition_1_and_live_in_one_transaction(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_files(self.dir(), "2026-07", [P1, P2, P3])
            rc, text, conn, _ = self.run_main(cur, ["load", "--commit"], files=f)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, P3), [(1, None, N)])
            self.assertEqual(live(cur, P3), {c: price(P3, i)
                                              for i, c in enumerate(CODES)})
            self.assertEqual(core.rows_differing(cur, ZZ, P3, 1), 0)
            self.assertEqual(conn.cur.connection.commits, 3)  # one per period
            self.assertIn(f"{P3}: new", text)
            cur.execute("SELECT DISTINCT release_label, source_file, "
                        "source_sha256 FROM public.zz_s15_editions "
                        "WHERE period = %s", (P3,))
            (label, src, sha), = cur.fetchall()
            self.assertEqual(label, "UK HPI July 2026 edition")
            self.assertIn("Average-prices-2026-07.csv", src)
            self.assertIn("Average-prices-Property-Type-2026-07.csv", src)
            self.assertIn(m.file_sha256(f[0])[:16], src)
            self.assertIn(m.file_sha256(f[1])[:16], src)
            recs = file_records(f, cur)
            self.assertEqual(sha, m.content_sha256(recs[P3]))
            self.assertTrue(m.status(cur, ZZ)["ok"])

        # a failure inserting the live rows rolls back the edition too
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_files(self.dir(), "2026-07", [P1, P2, P3])
            with mock.patch.object(pe, "insert_live",
                                   side_effect=RuntimeError("boom")):
                rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                    files=f)
            self.assertEqual(rc, 1, text)
            self.assertEqual(editions(cur, P3), [])
            self.assertEqual(live(cur, P3), {})
            logged.assert_not_called()

    def test_revised_period_edition_2_live_untouched_until_refresh(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live(cur, P2)
            f = write_files(self.dir(), "2026-07", [P1, P2, P3],
                            bump={(P2, CODES[1]): 500, (P2, CODES[3]): -250})
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], files=f)
            self.assertEqual(rc, 0, text)
            self.assertIn(f"{P2}: revised, 2 areas changed", text)
            self.assertEqual(editions(cur, P2), [(1, None, N), (2, 1, N)])
            self.assertEqual(editions(cur, P1), [(1, None, N)])
            self.assertEqual(live(cur, P2), before)
            self.assertEqual(m.status(cur, ZZ)["pending_refresh"], [P2])
            # the preview's cell summary names the revision
            self.assertIn("largest price revisions", text)
            rc, text, _, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertEqual(live(cur, P2)[CODES[1]], price(P2, 1) + 500)
            self.assertEqual(live(cur, P2)[CODES[3]], price(P2, 3) - 250)
            self.assertTrue(m.status(cur, ZZ)["ok"])

    def test_unchanged_period_stores_nothing(self):
        with rolled_back(self.conn) as cur:
            f = self.seed(cur)
            cur.execute("SELECT COUNT(*), MAX(loaded_at) FROM public.zz_s15_live")
            before = cur.fetchone()
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], files=f)
            self.assertEqual(rc, 0, text)
            self.assertIn(f"{P1}: unchanged", text)
            self.assertIn(f"{P2}: unchanged", text)
            self.assertEqual(editions(cur), [(1, None, 2 * N)])
            cur.execute("SELECT COUNT(*), MAX(loaded_at) FROM public.zz_s15_live")
            self.assertEqual(cur.fetchone(), before)

    def test_short_period_rejected_not_stored(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_files(self.dir(), "2026-07", [P1, P2, P3],
                            codes={P3: CODES[:-1]})
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                files=f)
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P3}: REJECTED", text)
            self.assertIn(f"{N - 1} areas, expected {N}", text)
            self.assertEqual(editions(cur, P3), [])
            self.assertEqual(live(cur, P3), {})
            logged.assert_not_called()
            with self.assertRaises(ValueError):
                m.check_period(file_records(f, cur)[P3], P3, N, set(CODES))

    def test_period_missing_an_area_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            # P3 has N areas but SPARE in place of a held area; P2 is revised
            f = write_files(self.dir(), "2026-07", [P1, P2, P3],
                            codes={P3: CODES[:-1] + [SPARE]},
                            bump={(P2, CODES[0]): 1})
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                files=f)
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P3}: REJECTED", text)
            self.assertIn(CODES[-1], text)
            self.assertEqual(editions(cur, P3), [])
            self.assertEqual(live(cur, P3), {})
            # the other periods of the same run went through
            self.assertEqual(editions(cur, P2), [(1, None, N), (2, 1, N)])
            logged.assert_not_called()

    def test_unresolved_code_hard_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_files(self.dir(), "2026-07", [P1, P2, P3],
                            extra=[("E06000099", P3)])
            with mock.patch.object(load_checks, "check_codes",
                                   return_value=["UNEXPLAINED E06000099"]) as cc, \
                    mock.patch.object(pe, "load_periods") as lp:
                rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                               files=f)
            self.assertEqual(rc, "halt")
            self.assertIn("UNEXPLAINED E06000099", text)
            self.assertIn("la_code_lookup", text)
            self.assertEqual(list(cc.call_args.args[1]), ["E06000099"])
            lp.assert_not_called()
            self.assertEqual(editions(cur, P3), [])

    def test_file_identity_mismatch_halts_before_any_write(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            d = self.dir()
            cases = [
                # the two file names disagree
                write_files(d, "2026-07", [P1, P2, P3],
                            pt_name="Average-prices-Property-Type-2026-06.csv"),
                # the edition is not the latest period in the file
                write_files(self.dir(), "2026-08", [P1, P2, P3]),
            ]
            for f in cases:
                with mock.patch.object(pe, "apply_period",
                                       side_effect=AssertionError("wrote")), \
                        mock.patch.object(pe, "load_periods") as lp:
                    rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                                   files=f)
                self.assertEqual(rc, "halt", f)
                self.assertIn("identity", text)
                lp.assert_not_called()
            self.assertEqual(editions(cur, P3), [])
            # an unrecognisable file name halts too
            bad = write_files(self.dir(), "2026-07", [P3],
                              avg_name="prices.csv")
            rc, text, _, _ = self.run_main(cur, ["load"], files=bad)
            self.assertEqual(rc, "halt")

    def test_window_never_loads_before_earliest_held(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, periods=(P2, P3), edition="2026-07")
            f = write_files(self.dir(), "2026-07", [P1, P2, P3])
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], files=f)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, P1), [])
            self.assertEqual(live(cur, P1), {})
            self.assertIn("earlier than the earliest held period", text)
            self.assertIn(P1, text)
            # --min-period widens the window explicitly
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit", "--min-period", P1], files=f)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, P1), [(1, None, N)])

    def test_nothing_held_requires_min_period(self):
        with rolled_back(self.conn) as cur:
            f = write_files(self.dir(), "2026-06", [P1, P2])
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], files=f)
            self.assertEqual(rc, "halt")
            self.assertIn("--min-period", text)
            # no download is attempted before that halt
            rc, text, _, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt")
            self.assertIn("--min-period", text)
            self.assertNotIn("no download in tests", text)
            # with nothing held the area count cannot be derived either
            rc, text, _, _ = self.run_main(
                cur, ["load", "--min-period", P1], files=f)
            self.assertEqual(rc, "halt")
            self.assertIn("--expected-areas", text)
            self.seed(cur)
            self.assertEqual(editions(cur), [(1, None, 2 * N)])

    def test_preview_default_and_simulate_rolls_back(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_files(self.dir(), "2026-07", [P1, P2, P3],
                            bump={(P2, CODES[0]): 7})
            texts = {}
            for flags, word in (([], "PREVIEW"), (["--simulate"], "SIMULATION")):
                rc, text, conn, logged = self.run_main(cur, ["load"] + flags,
                                                       files=f)
                texts[word] = text
                self.assertEqual(rc, 0, text)
                self.assertIn(word, text)
                self.assertEqual(conn.commits, 0)
                self.assertEqual(conn.cur.connection.commits, 0)
                logged.assert_not_called()
                self.assertEqual(editions(cur, P3), [])
                self.assertEqual(editions(cur, P2), [(1, None, N)])
                self.assertEqual(live(cur, P3), {})
            self.assertIn("would store edition 1 and insert 5 live rows",
                          texts["PREVIEW"])
            self.assertIn("stored edition 1 and inserted 5 live rows",
                          texts["SIMULATION"])
            # with no editions table the preview compares with the LIVE table
            rc, text, _, _ = self.run_main(cur, ["load"], files=f, table=False)
            self.assertEqual(rc, 0, text)
            self.assertIn("compares each period with the LIVE table", text)
            self.assertIn("compared with live", text)
            # writing without the editions table halts
            rc, text, _, _ = self.run_main(cur, ["load", "--simulate"],
                                           files=f, table=False)
            self.assertEqual(rc, "halt")
            self.assertIn("does not exist yet", text)

    def test_main_downloads_nothing_when_files_given(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)    # fetch_latest_files raises if called
            f = write_files(self.dir(), "2026-07", [P1, P2, P3])
            calls = []

            def fake_fetch(*a, **kw):
                calls.append(a)
                return f[0], f[1], "2026-07"
            rc, text, _, _ = self.run_main(cur, ["load"], fetch=fake_fetch)
            self.assertEqual(rc, 0, text)
            self.assertEqual(len(calls), 1)    # no files: the loader fetches
        for argv in (["load", "--avg-prices", "a.csv"],
                     ["load", "--property-type", "b.csv"]):
            with self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                m.main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_status_flags_period_missing_from_live(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.assertTrue(m.status(cur, ZZ)["ok"])
            recs = file_records(
                write_files(self.dir(), "2026-07", [P1, P2, P3]), cur)
            core.insert_edition(cur, ZZ, recs[P3], P3, release_label="t",
                                published_date=date(2026, 10, 8),
                                source_file="t", source_sha256="h",
                                supersedes=None)
            st = m.status(cur, ZZ)
            self.assertEqual(st["live_missing"], [P3])
            self.assertFalse(st["ok"])
            text = m.format_status(st)
            self.assertIn(f"editions month missing from live: {P3}", text)
            self.assertIn("ACTION NEEDED", text)
            self.assertNotIn("datetime.date(", text)
            rc, text, _, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1)
            rc, text, _, _ = self.run_main(cur, ["status"], table=False)
            self.assertEqual(rc, 1)
            self.assertIn("does not exist yet", text)

    def test_stranded_period_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_files(self.dir(), "2026-07", [P1, P2, P3])
            recs = file_records(f, cur)
            core.insert_edition(cur, ZZ, recs[P3], P3, release_label="t",
                                published_date=date(2026, 10, 8),
                                source_file="t",
                                source_sha256=m.content_sha256(recs[P3]),
                                supersedes=None)
            rc, text, _, _ = self.run_main(cur, ["load"], files=f)
            self.assertIn("would insert 5 live rows (no new edition)", text)
            self.assertEqual(live(cur, P3), {})
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                files=f)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, P3), [(1, None, N)])
            self.assertEqual(len(live(cur, P3)), N)
            self.assertTrue(m.status(cur, ZZ)["ok"])
            self.assertEqual(logged.call_args.args[1], N)   # live rows only

    def test_run_log_only_on_commit(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_files(self.dir(), "2026-07", [P1, P2, P3],
                            bump={(P2, CODES[2]): 3})
            for flags in ([], ["--simulate"]):
                _, _, _, logged = self.run_main(cur, ["load"] + flags, files=f)
                logged.assert_not_called()
            rc, text, conn, logged = self.run_main(cur, ["load", "--commit"],
                                                   files=f)
            self.assertEqual(rc, 0, text)
            logged.assert_called_once()
            rows, notes = logged.call_args.args[1:3]
            # P3 new (5 edition + 5 live rows), P2 revised (5 edition rows)
            self.assertEqual(rows, 3 * N)
            self.assertIn("new 2026-07-01", notes)
            self.assertIn("revised 2026-06-01", notes)
            self.assertIn("Average-prices-2026-07.csv", notes)
            self.assertIn("pipeline_run_log row written", text)
            self.assertGreaterEqual(conn.commits, 1)
            # a run that rechecks and finds nothing new is logged too
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                files=f)
            self.assertEqual(rc, 0, text)
            self.assertEqual(logged.call_args.args[1], 0)
            self.assertIn("Nothing new", logged.call_args.args[2])
        # the real log_run writes the S15 agent and source
        c = mock.MagicMock()
        m.log_run(c, 7, "notes")
        (sql, params), = [x.args for x in c.execute.call_args_list]
        self.assertEqual(params[:4], ("Source 15 - Land Registry UK HPI",
                                      "15", "15", 7))

    # -- further cases --------------------------------------------------------

    def test_period_only_in_average_prices_file_stored_with_property_type_null(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = write_files(self.dir(), "2026-07", [P1, P2, P3],
                            pt_periods=[P1, P2])
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], files=f)
            self.assertEqual(rc, 0, text)
            self.assertIn("average-prices file only", text)
            self.assertIn(P3, text)
            cur.execute("SELECT COUNT(*) FROM public.zz_s15_live WHERE "
                        "period = %s AND avg_price_all IS NOT NULL AND "
                        "avg_price_detached IS NULL AND avg_price_semi IS NULL "
                        "AND avg_price_terraced IS NULL AND avg_price_flat "
                        "IS NULL", (P3,))
            self.assertEqual(cur.fetchone()[0], N)

    def test_cell_summary_counts_cells_and_ranks_price_revisions(self):
        from decimal import Decimal as D
        cols = m.VALUE_COLUMNS
        rec = {"lad24cd": CODES[0], "period": P1,
               **{c: D("100000") for c in cols}}
        rec["annual_change_pct"] = D("1.30")
        rec["avg_price_flat"] = None
        old = {CODES[0]: tuple(D("100000") if c not in ("annual_change_pct",
                                                        "avg_price_all")
                               else (D("0.10") if c == "annual_change_pct"
                                     else D("90000")) for c in cols)}
        prof = m.run_profile(ZZ, "2026-07", 1)
        with mock.patch.object(pe, "stored", return_value=(old, "live")):
            s = m.cell_summary(None, prof, {P1: [rec]}, [P1], "live")
        self.assertEqual(s["cells"], 3)
        self.assertEqual(s["null_for_number"], 1)
        self.assertEqual(s["by_col"]["annual_change_pct"], 1)
        # the annual change (a 1200% move on a small base) is not ranked
        self.assertEqual([x[3] for x in s["largest"]], ["avg_price_all"])
        self.assertIn("avg_price_all: 90000 -> 100000",
                      m.format_cell_summary(s, "live"))

    def test_ddl_and_real_spec(self):
        self.assertEqual(m.SPEC.name, "s15")
        self.assertEqual(m.SPEC.live_table, "la_house_prices")
        self.assertEqual(m.SPEC.editions_table, "la_house_prices_editions")
        self.assertEqual(m.SPEC.key_cols, ("lad24cd",))
        self.assertEqual(m.SPEC.period_col, "period")
        self.assertEqual(dict(m.SPEC.key_types), {"period": "date NOT NULL"})
        self.assertEqual(dict(m.SPEC.value_cols), {
            "avg_price_all": "numeric(12,2)", "avg_price_all_sa": "numeric(12,2)",
            "annual_change_pct": "numeric(6,2)",
            "avg_price_detached": "numeric(12,2)",
            "avg_price_semi": "numeric(12,2)",
            "avg_price_terraced": "numeric(12,2)",
            "avg_price_flat": "numeric(12,2)"})
        self.assertEqual(m.SPEC.refresh_cols, m.VALUE_COLUMNS)
        self.assertTrue(m.SPEC.fk_la_boundaries)
        self.assertIsNone(m.PROFILE.expected_areas)
        self.assertEqual(m.release_label("2026-07"), "UK HPI July 2026 edition")
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, ZZ)     # idempotent (second time)
            cur.execute("""SELECT column_name, data_type, numeric_precision,
                                  numeric_scale FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = 'zz_s15_editions'""")
            cols = {r[0]: r[1:] for r in cur.fetchall()}
            self.assertEqual(cols["period"][0], "date")
            self.assertEqual(cols["annual_change_pct"], ("numeric", 6, 2))
            self.assertEqual(cols["avg_price_flat"], ("numeric", 12, 2))


# ---------------------------------------------------------------------------
# Pages and download (stub session; markup trimmed from the real gov.uk pages
# seen on 2026-10-08)
# ---------------------------------------------------------------------------

REAL_COLLECTIONS = """<html><head>
<script type="application/ld+json">{"sameAs": "https://www.gov.uk/government/statistical-data-sets/uk-house-price-index-data-downloads-july-2026"}</script>
</head><body>
<div class="gem-c-document-list__item-title">          <a class="  govuk-link gem-print-force-link-styles " href="/government/statistical-data-sets/uk-house-price-index-data-downloads-july-2026">UK House Price Index: data downloads July 2026</a>
</div></body></html>"""

_LR = "https://publicdata.landregistry.gov.uk/market-trend-data/house-price-index-data"
REAL_DOWNLOADS = f"""<html><body>
<script>{{"body": "\\u003ca rel=\\"external\\" href=\\"{_LR}/Average-prices-2026-07.csv?utm_medium=GOV.UK\\u0026amp;utm_source=datadownload\\"\\u003eAverage price\\u003c/a\\u003e"}}</script>
    <p><a rel="external" href="{_LR}/Average-prices-2026-07.csv?utm_medium=GOV.UK&amp;utm_source=datadownload&amp;utm_campaign=average_price&amp;utm_term=9.30_16_09_26">Average price</a> (CSV, 7.4KB)</p>
    <p><a rel="external" href="{_LR}/Average-prices-Property-Type-2026-07.csv?utm_medium=GOV.UK&amp;utm_source=datadownload&amp;utm_campaign=average_price_property_price&amp;utm_term=9.30_16_09_26">Average price by property type</a> (CSV, 16KB)</p>
    <p><a rel="external" href="{_LR}/Average-price-seasonally-adjusted-2026-07.csv?utm_medium=GOV.UK">Average price seasonally adjusted</a></p>
</body></html>"""

DL_URL = ("https://www.gov.uk/government/statistical-data-sets/"
          "uk-house-price-index-data-downloads-july-2026")


def csv_body(head, n=60):
    rows = [head] + [f"2026-07-01,Area,E0600{i:04d},{200000 + i},0,1.0,1"
                     for i in range(n)]
    return ("\n".join(rows) + "\n").encode("utf-8")


class _Resp:
    def __init__(self, body, status=200):
        self.content = body if isinstance(body, bytes) else body.encode("utf-8")
        self.text = self.content.decode("utf-8")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers or {}))
        for prefix, body in self.pages.items():
            if url.startswith(prefix):
                return _Resp(body)
        return _Resp(b"not found", 404)


class Fetch(unittest.TestCase):
    def pages(self, **over):
        p = {m.COLLECTIONS_URL: REAL_COLLECTIONS, DL_URL: REAL_DOWNLOADS,
             f"{_LR}/Average-prices-2026-07.csv": csv_body(AVG_HEAD),
             f"{_LR}/Average-prices-Property-Type-2026-07.csv": csv_body(PT_HEAD)}
        p.update(over)
        return p

    def fetch(self, pages):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        s = _Session(pages)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                got = m.fetch_latest_files(session=s, dest=Path(d) / "raw")
            except SystemExit as e:
                return "halt", str(e.code), s, Path(d)
        return got, out.getvalue(), s, Path(d)

    def test_parsers_on_real_markup(self):
        self.assertEqual(m.find_download_page(REAL_COLLECTIONS), DL_URL)
        a, p, ed = m.find_csv_links(REAL_DOWNLOADS)
        self.assertEqual(ed, "2026-07")
        self.assertEqual(a, f"{_LR}/Average-prices-2026-07.csv?utm_medium="
                            "GOV.UK&utm_source=datadownload&utm_campaign="
                            "average_price&utm_term=9.30_16_09_26")
        self.assertNotIn("&amp;", p)
        self.assertIn("/Average-prices-Property-Type-2026-07.csv?", p)

    def test_fetch_latest_files_with_stubbed_session(self):
        got, text, s, d = self.fetch(self.pages())
        avg, pt, ed = got
        self.assertEqual(ed, "2026-07")
        self.assertEqual((avg.name, pt.name),
                         ("Average-prices-2026-07.csv",
                          "Average-prices-Property-Type-2026-07.csv"))
        self.assertEqual(avg.read_bytes(), csv_body(AVG_HEAD))
        self.assertEqual(pt.read_bytes(), csv_body(PT_HEAD))
        self.assertEqual(avg.parent, d / "raw")
        self.assertEqual([u for u, _ in s.calls][:2], [m.COLLECTIONS_URL, DL_URL])
        self.assertTrue(all("User-Agent" in h for _, h in s.calls))
        self.assertNotIn("&amp;", s.calls[2][0])
        # a changed layout halts with an excerpt of what was seen
        for pages, words in (
                (self.pages(**{m.COLLECTIONS_URL:
                               "<html><body>New layout: see Reports</body></html>"}),
                 "New layout: see Reports"),
                (self.pages(**{DL_URL: REAL_DOWNLOADS.replace(
                    "Average-prices-Property-Type-2026-07.csv", "PT.csv")}),
                 "Average price"),
                (self.pages(**{f"{_LR}/Average-prices-2026-07.csv":
                               b"Date,Area_Code\n"}), "bytes"),
                (self.pages(**{f"{_LR}/Average-prices-Property-Type-2026-07.csv":
                               csv_body("Period,Code,Price")}), "Period,Code"),
                (self.pages(**{DL_URL: REAL_DOWNLOADS.replace("2026-07", "2026-06")}),
                 "july-2026")):
            got, msg, _, d = self.fetch(pages)
            self.assertEqual(got, "halt", words)
            self.assertIn(words, msg)
            self.assertFalse(list((d / "raw").glob("*.csv"))
                             if (d / "raw").exists() else [], words)


if __name__ == "__main__":
    unittest.main()
