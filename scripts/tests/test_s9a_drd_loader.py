"""Tests for the S9a loader in s9a_drd_editions (spec, fetch, the commands).

No network: the publication page and the downloads are stubbed, and every
workbook is built in the test (write_workbook from test_s9a_drd_pure).
Database tests run on a connection from _db.get_conn() inside a transaction
that is always rolled back (rolled_back below); nothing here commits: the
commands get a stand-in connection (_Borrowed) whose commit only counts, and a
cursor proxy whose .connection is a stand-in too, so the engine's per-month
commit never reaches the real connection. The only tables touched are the
throwaway zz_s9a_live, zz_s9a_editions and zz_s9a_editions_file_checks (a copy
of SPEC named zz_s9a), created inside that transaction (refused if either
already exists), so they never persist. utla_lad_mapping and the run log are
stubbed; the real la_code_lookup recode rows are read (never written).
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s9a_drd_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s9a_drd_pure import (BASE, FILE, data_row, html_of,  # noqa: E402
                               url, write_workbook)

ZZ = dataclasses.replace(
    m.SPEC, name="zz_s9a", live_table="zz_s9a_live",
    editions_table="zz_s9a_editions")
N = 12
CODES = [f"E060000{i:02d}" for i in range(1, N + 1)]
P1, P2, P3, P4 = "2026-04-01", "2026-05-01", "2026-06-01", "2026-07-01"
NAME = {P1: ("April-2026", 6), P2: ("May-2026", 7), P3: ("June-2026", 8),
        P4: ("July-2026", 9)}
LEDGER = "zz_s9a_editions_file_checks"


def orig(p):
    return url(2026, NAME[p][1], NAME[p][0])


def rev(p):
    return url(2026, NAME[p][1] + 2, NAME[p][0] + "-Revised")


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
        cur.execute("SELECT to_regclass('public.zz_s9a_live'), "
                    "to_regclass('public.zz_s9a_editions'), "
                    f"to_regclass('public.{LEDGER}')")
        if cur.fetchone() != (None, None, None):
            raise RuntimeError("a zz_s9a table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s9a_live (LIKE "
                    "public.nhs_drd_discharge_delays INCLUDING ALL)")
        core.create_schema(cur, ZZ)
        pe.create_file_checks(cur, m._profile(ZZ))
        yield cur
    finally:
        cur.close()
        conn.rollback()


def count(cur, table, where="", args=None):
    cur.execute(f"SELECT COUNT(*) FROM public.{table} {where}", args)
    return cur.fetchone()[0]


def editions(cur, period=None):
    """[(edition, supersedes, rows)] of the throwaway editions table."""
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM "
                "public.zz_s9a_editions"
                + (" WHERE reporting_period = %s" if period else "")
                + " GROUP BY 1, 2 ORDER BY 1", (period,) if period else None)
    return cur.fetchall()


def ledger(cur, period=None):
    """[(source_file tail, outcome, edition)] in order."""
    cur.execute(f"SELECT source_file, outcome, edition FROM public.{LEDGER}"
                + (" WHERE reporting_period = %s" if period else "")
                + " ORDER BY id", (period,) if period else None)
    return [(s.rsplit("/", 1)[-1], o, e) for s, o, e in cur.fetchall()]


def live(cur, period, col="total_bed_days_lost"):
    cur.execute(f"SELECT utla_code, {col} FROM public.zz_s9a_live WHERE "
                "reporting_period = %s", (period,))
    return dict(cur.fetchall())


def live_source(cur, period):
    cur.execute("SELECT DISTINCT source FROM public.zz_s9a_live WHERE "
                "reporting_period = %s", (period,))
    return cur.fetchall()


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
        self.files = {}
        self.fetched = []
        self.n = 0

    # -- fixtures ---------------------------------------------------------

    def make(self, link, period, *, bump=None, codes=None, overrides=None,
             revised=None):
        """Build the workbook for `link`; register it for the download stub."""
        rows = []
        for i, c in enumerate(codes or CODES):
            v = {"total_bed_days_lost": 300 + 10 * i + int(period[5:7])
                 + (bump or {}).get(c, 0)}
            v.update((overrides or {}).get(c, {}))
            rows.append(data_row(c, f"Area {c}", v))
        self.n += 1
        d = Path(self.tmp.name) / str(self.n)
        d.mkdir()
        path = write_workbook(
            d / m._name(link), date.fromisoformat(period), rows,
            revised=revised if revised is not None
            else ("9th July 2026" if "Revised" in link else None))
        self.files[link] = path
        return path

    def run_main(self, cur, argv, *, page=(), table=True, file=None):
        """main(argv) on the throwaway tables with the page stubbed to the
        given links. Returns (rc or 'halt', text, borrowed, log mock)."""
        borrowed = _Borrowed(cur)
        args = list(argv)
        if file:
            args += ["--file", str(file)]

        def fetch_month(link, dest=None, session=None):
            self.fetched.append(link)
            return self.files[link]

        out = io.StringIO()
        with mock.patch.object(m, "SPEC", ZZ), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "valid_codes",
                                  return_value=set(CODES) | {"E10000003"}), \
                mock.patch.object(m, "fetch_page",
                                  return_value=html_of(*page)), \
                mock.patch.object(m, "fetch_month", side_effect=fetch_month), \
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

    def seed(self, cur, periods=(P1, P2, P3), link=orig):
        page = []
        for p in periods:
            self.make(link(p), p)
            page.append(link(p))
        rc, text, _, _ = self.run_main(
            cur, ["load", "--commit", "--min-period", periods[0],
                  "--expected-areas", str(N)], page=page)
        self.assertEqual(rc, 0, text)
        self.fetched.clear()
        return page

    # -- the brief's tests --------------------------------------------------

    def test_new_month_edition_1_and_live_in_one_transaction(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.assertEqual(count(cur, "zz_s9a_live",
                                   "WHERE reporting_period = %s", (P3,)), N)
            self.assertEqual(ledger(cur, P3), [(m._name(orig(P3)), "new", 1)])
            self.make(orig(P4), P4)
            rc, text, _, logged = self.run_main(
                cur, ["load", "--commit"], page=page + [orig(P4)])
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [orig(P4)])
            self.assertEqual(editions(cur, P4), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s9a_live",
                                   "WHERE reporting_period = %s", (P4,)), N)
            self.assertEqual(core.rows_differing(cur, ZZ, P4, 1), 0)
            self.assertIn(f"{P4}: new", text)
            cur.execute("SELECT DISTINCT release_label, source_file FROM "
                        "public.zz_s9a_editions WHERE reporting_period = %s",
                        (P4,))
            (label, src), = cur.fetchall()
            self.assertEqual(src, orig(P4))
            self.assertIn("original file", label)
            self.assertIn(m.content_sha256(self.files[orig(P4)])[:16], label)
            self.assertEqual(live_source(cur, P4), [(orig(P4),)])
            self.assertEqual(ledger(cur, P4), [(m._name(orig(P4)), "new", 1)])
            logged.assert_called_once()

        # a failure inserting the live rows rolls back the edition and the
        # ledger row too
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.make(orig(P4), P4)
            with mock.patch.object(m, "insert_live",
                                   side_effect=RuntimeError("boom")):
                rc, text, _, logged = self.run_main(
                    cur, ["load", "--commit"], page=page + [orig(P4)])
            self.assertEqual(rc, 1, text)
            self.assertEqual(editions(cur, P4), [])
            self.assertEqual(ledger(cur, P4), [])
            self.assertEqual(count(cur, "zz_s9a_live",
                                   "WHERE reporting_period = %s", (P4,)), 0)
            logged.assert_not_called()

    def test_unchanged_reissue_stores_nothing_and_is_not_fetched_again(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            # May is republished as -Revised with identical figures
            self.make(rev(P2), P2)
            page2 = [orig(P1), rev(P2), orig(P3)]
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                page=page2)
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [rev(P2)])      # only the new link
            self.assertIn(f"{P2}: unchanged", text)
            self.assertEqual(editions(cur, P2), [(1, None, N)])
            self.assertEqual(ledger(cur, P2),
                             [(m._name(orig(P2)), "new", 1),
                              (m._name(rev(P2)), "unchanged", 1)])
            logged.assert_called_once()
            # the next run: the link is in the ledger, so it is not fetched
            self.fetched.clear()
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page2)
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [])
            self.assertIn("nothing to fetch", text)
            # --recheck-all reads everything again
            rc, text, _, _ = self.run_main(cur, ["load", "--commit",
                                                 "--recheck-all"], page=page2)
            self.assertEqual(rc, 0, text)
            self.assertEqual(sorted(self.fetched),
                             sorted([orig(P1), rev(P2), orig(P3)]))
            self.assertEqual(editions(cur), [(1, None, 3 * N)])

    def test_revised_month_edition_2_live_untouched_until_refresh(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live(cur, P2)
            self.make(rev(P2), P2, bump={CODES[1]: 500, CODES[3]: -250})
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit"],
                page=[orig(P1), rev(P2), orig(P3)])
            self.assertEqual(rc, 0, text)
            self.assertIn(f"{P2}: revised, 2 areas changed", text)
            self.assertIn("largest bed-days revisions", text)
            self.assertEqual(editions(cur, P2), [(1, None, N), (2, 1, N)])
            self.assertEqual(live(cur, P2), before)
            self.assertEqual(live_source(cur, P2), [(orig(P2),)])
            self.assertEqual(ledger(cur, P2)[-1],
                             (m._name(rev(P2)), "revised", 2))
            cur.execute("SELECT release_label, source_file FROM "
                        "public.zz_s9a_editions WHERE reporting_period = %s "
                        "AND edition = 2 LIMIT 1", (P2,))
            label, src = cur.fetchone()
            self.assertEqual(src, rev(P2))
            self.assertIn("Revised file", label)
            self.assertIn("revised 2026-07-09", label)
            self.assertEqual(m.status(cur, ZZ)["pending_refresh"], [P2])
            rc, text, _, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertEqual(live(cur, P2)[CODES[1]], before[CODES[1]] + 500)
            self.assertEqual(live(cur, P2)[CODES[3]], before[CODES[3]] - 250)
            # the engine writes the rows that changed, source included (an
            # unchanged row of a revised month keeps its older source: see
            # the task report)
            cur.execute("SELECT utla_code, source FROM public.zz_s9a_live "
                        "WHERE reporting_period = %s", (P2,))
            src = dict(cur.fetchall())
            self.assertEqual(src[CODES[1]], rev(P2))
            self.assertEqual(src[CODES[3]], rev(P2))
            self.assertTrue(m.status(cur, ZZ)["ok"])
            # rerun: idempotent, nothing to fetch, no new edition
            self.fetched.clear()
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit"], page=[orig(P1), rev(P2), orig(P3)])
            self.assertEqual((rc, self.fetched), (0, []), text)
            self.assertEqual(editions(cur, P2), [(1, None, N), (2, 1, N)])

    def test_precision_only_revision_is_stored_and_never_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            # every area's five ratio columns move by 1e-9: 60 cells
            ov = {c: {"pct_same_day_discharge": 0.9 + 1e-9,
                      "pct_delayed_1plus_days": 0.1 + 1e-9,
                      "pct_acceptable_trust_coverage": 0.5 + 1e-9,
                      "avg_days_drd_to_discharge_inc_zero": 1.25 + 1e-9,
                      "avg_days_drd_to_discharge_exc_zero": 2.5 + 1e-9}
                  for c in CODES}
            self.make(rev(P3), P3, overrides=ov)
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit"],
                page=[orig(P1), orig(P2), rev(P3)])
            self.assertEqual(rc, 0, text)
            self.assertIn(f"{P3}: revised", text)
            self.assertIn("precision-only (below 1e-8, max", text)
            self.assertIn(f"{N * 5}", text)
            self.assertIn("changed in 1 month(s)", text)
            self.assertIn("cells (compared with the latest editions): 0 "
                          "changed", text)
            self.assertEqual(editions(cur, P3), [(1, None, N), (2, 1, N)])
            cur.execute("SELECT release_label FROM public.zz_s9a_editions "
                        "WHERE reporting_period = %s AND edition = 2 LIMIT 1",
                        (P3,))
            self.assertIn("precision-only", cur.fetchone()[0])

    def test_older_link_halts_and_allow_older_file_overrides(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, link=lambda p: rev(p) if p == P2 else orig(p))
            self.make(orig(P2), P2)               # the original is offered
            page = [orig(P1), orig(P2), orig(P3)]
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page)
            self.assertEqual(rc, "halt", text)
            self.assertIn("latest file", text)
            self.assertIn("--allow-older-file", text)
            self.assertEqual(editions(cur, P2), [(1, None, N)])
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit", "--allow-older-file"], page=page)
            self.assertEqual(rc, 0, text)
            self.assertIn("NOTE: --allow-older-file given", text)
            self.assertEqual(ledger(cur, P2)[-1][1], "unchanged")

    def test_page_latest_earlier_than_held_halts(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=[orig(P1), orig(P2)])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2026-06 is already held", text)
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit", "--allow-older-file"],
                page=[orig(P1), orig(P2)])
            self.assertEqual(rc, 0, text)

    def test_zero_to_null_needs_acknowledge(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)           # discharged_14_20_days is 0 everywhere
            self.make(rev(P3), P3, overrides={
                CODES[2]: {"discharged_14_20_days": "-"}})
            page = [orig(P1), orig(P2), rev(P3)]
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                page=page)
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P3}: REJECTED, not stored", text)
            self.assertIn("0 to NULL", text)
            self.assertEqual(editions(cur, P3), [(1, None, N)])
            self.assertEqual(ledger(cur, P3), [(m._name(orig(P3)), "new", 1)])
            logged.assert_not_called()
            # an acknowledgement for a month not in the run halts
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit", "--acknowledge", P1], page=page)
            self.assertEqual(rc, "halt", text)
            rc, text, _, logged = self.run_main(
                cur, ["load", "--commit", "--acknowledge", P3], page=page)
            self.assertEqual(rc, 0, text)
            self.assertIn("ACKNOWLEDGED", text)
            self.assertEqual(editions(cur, P3), [(1, None, N), (2, 1, N)])
            cur.execute("SELECT release_label FROM public.zz_s9a_editions "
                        "WHERE reporting_period = %s AND edition = 2 LIMIT 1",
                        (P3,))
            self.assertIn("ACKNOWLEDGED", cur.fetchone()[0])
            self.assertIn("ACKNOWLEDGED", logged.call_args[0][2])

    def test_null_replacing_numbers_in_more_than_five_areas_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            ov = {c: {"total_bed_days_lost": "-"} for c in CODES[:6]}
            self.make(rev(P3), P3, overrides=ov)
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit"],
                page=[orig(P1), orig(P2), rev(P3)])
            self.assertEqual(rc, 1, text)
            self.assertIn("NULL replaces a number in 6 areas", text)
            self.assertEqual(editions(cur, P3), [(1, None, N)])
        with rolled_back(self.conn) as cur:        # five areas is allowed
            self.seed(cur)
            ov = {c: {"total_bed_days_lost": "-"} for c in CODES[:5]}
            self.make(rev(P3), P3, overrides=ov)
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit"],
                page=[orig(P1), orig(P2), rev(P3)])
            self.assertEqual(rc, 0, text)

    def test_bed_days_revised_above_50pct_in_more_than_ten_areas_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.make(rev(P3), P3, bump={c: 1000 for c in CODES[:11]})
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit"],
                page=[orig(P1), orig(P2), rev(P3)])
            self.assertEqual(rc, 1, text)
            self.assertIn("revised above 50% in 11 areas", text)

    def test_short_month_is_rejected_not_stored(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.make(orig(P4), P4, codes=CODES[:-1])      # 11 of 12 areas
            rc, text, _, logged = self.run_main(
                cur, ["load", "--commit"], page=page + [orig(P4)])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P4}: REJECTED, not stored", text)
            self.assertIn("11 areas, expected 12", text)
            self.assertEqual(editions(cur, P4), [])
            self.assertEqual(ledger(cur, P4), [])
            self.assertEqual(count(cur, "zz_s9a_live",
                                   "WHERE reporting_period = %s", (P4,)), 0)
            logged.assert_not_called()

    def test_negative_count_and_percentage_out_of_range_rejected(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.make(orig(P4), P4, overrides={
                CODES[0]: {"total_bed_days_lost": -1}})
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page + [orig(P4)])
            self.assertEqual(rc, 1, text)
            self.assertIn("negative", text)
            self.make(orig(P4), P4, overrides={
                CODES[0]: {"pct_same_day_discharge": 1.5}})
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page + [orig(P4)])
            self.assertEqual(rc, 1, text)
            self.assertIn("outside 0..1", text)

    def test_preview_writes_nothing(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.make(orig(P4), P4)
            self.make(rev(P2), P2, bump={CODES[0]: 5})
            snap = (editions(cur), ledger(cur), live(cur, P2),
                    count(cur, "zz_s9a_live"))
            for flags in ([], ["--recheck-all"]):
                rc, text, borrowed, logged = self.run_main(
                    cur, ["load"] + flags,
                    page=[orig(P1), rev(P2), orig(P3), orig(P4)])
                self.assertEqual(rc, 0, text)
                self.assertIn("PREVIEW: nothing written", text)
                self.assertIn(f"{P4}: new", text)
                self.assertIn(f"{P2}: revised", text)
                logged.assert_not_called()
                self.assertEqual(
                    snap, (editions(cur), ledger(cur), live(cur, P2),
                           count(cur, "zz_s9a_live")))
            # --simulate runs the commit path and rolls it back
            rc, text, borrowed, logged = self.run_main(
                cur, ["load", "--simulate"],
                page=[orig(P1), rev(P2), orig(P3), orig(P4)])
            self.assertEqual(rc, 0, text)
            self.assertIn("SIMULATION", text)
            logged.assert_not_called()
            self.assertEqual(
                snap, (editions(cur), ledger(cur), live(cur, P2),
                       count(cur, "zz_s9a_live")))

    def test_stranded_month_is_repaired(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            cur.execute("DELETE FROM public.zz_s9a_live WHERE "
                        "reporting_period = %s", (P2,))
            self.assertEqual(m.status(cur, ZZ)["live_missing"], [P2])
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page)
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [orig(P2)])
            self.assertIn("no new edition", text)
            self.assertEqual(editions(cur, P2), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s9a_live",
                                   "WHERE reporting_period = %s", (P2,)), N)
            self.assertEqual(ledger(cur, P2)[-1][1], "live-missing")
            self.assertTrue(m.status(cur, ZZ)["ok"])

    # -- other behaviour ----------------------------------------------------

    def test_without_editions_table_preview_compares_with_live(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.make(rev(P3), P3, bump={CODES[0]: 7})
            before = (editions(cur), ledger(cur))
            rc, text, _, _ = self.run_main(
                cur, ["load", "--recheck-all"], table=False,
                page=[orig(P1), orig(P2), rev(P3)])
            self.assertEqual(rc, 0, text)
            self.assertIn("does not exist yet", text)
            self.assertIn("LIVE table", text)
            self.assertIn(f"{P3}: revised, 1 areas changed (compared with "
                          "live)", text)
            self.assertIn(f"{P1}: unchanged", text)
            self.assertEqual(before, (editions(cur), ledger(cur)))
            # writing commands need the table
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit"], table=False,
                page=[orig(P1), orig(P2), rev(P3)])
            self.assertEqual(rc, "halt", text)
            self.assertIn("does not exist yet", text)

    def test_status_without_editions_table_is_clean_exit_1(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            rc, text, _, _ = self.run_main(cur, ["status"], table=False)
            self.assertEqual(rc, 1, text)
            self.assertIn("does not exist yet", text)
            rc, text, _, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 0, text)

    def test_ddl_creates_the_ledger(self):
        with rolled_back(self.conn) as cur:
            cur.execute(f"DROP TABLE public.{LEDGER}")
            rc, text, _, _ = self.run_main(cur, ["ddl", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertIn(LEDGER, text)
            self.assertIsNotNone(pe.table_exists(cur, LEDGER) or None)

    def test_sync_new_records_held_months_as_edition_1(self):
        with rolled_back(self.conn) as cur:
            self.make(orig(P1), P1)
            cur.execute("INSERT INTO public.zz_s9a_live (reporting_period, "
                        "utla_code, utla_name, total_bed_days_lost, source) "
                        "SELECT %s, c, c, 5, %s FROM unnest(%s::text[]) c",
                        (P1, orig(P1), CODES))
            with mock.patch.object(m, "log_run"):
                rc, text, _, _ = self.run_main(
                    cur, ["sync-new", "--commit", "--expected-areas", str(N)])
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, P1), [(1, None, N)])
            cur.execute("SELECT DISTINCT source_file FROM "
                        "public.zz_s9a_editions")
            self.assertEqual(cur.fetchall(), [(orig(P1),)])

    def test_file_option_reads_identity_from_the_file(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = self.make(rev(P2), P2, bump={CODES[4]: 9})
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 0, text)
            self.assertIn("file given: nothing downloaded", text)
            self.assertEqual(self.fetched, [])
            self.assertEqual(editions(cur, P2), [(1, None, N), (2, 1, N)])
            # a file whose cover says another month halts
            wrong = write_workbook(
                Path(self.tmp.name) / m._name(orig(P4)), date(2026, 8, 1),
                [data_row(c, c) for c in CODES])
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           file=wrong)
            self.assertEqual(rc, "halt", text)
            self.assertIn("identity", text)

    def test_unknown_utla_code_halts(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.make(orig(P4), P4, codes=CODES[:-1] + ["E06000077"])
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page + [orig(P4)])
            self.assertEqual(rc, "halt", text)
            self.assertIn("UNEXPLAINED E06000077", text)

    def test_csv_reissue_note_is_printed(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            csv = (f"{BASE}2026/06/Discharge-Ready-Date-monthly-data-csv-"
                   "April-2026v2.csv")
            rc, text, _, _ = self.run_main(cur, ["load"], page=page + [csv])
            self.assertEqual(rc, 0, text)
            self.assertIn("April-2026v2.csv", text)

    def test_tie_between_two_links_halts_naming_both(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            a = url(2026, 9, "July-2026-Revised")
            b = url(2026, 9, "July-2026-revised")
            rc, text, _, _ = self.run_main(cur, ["load"], page=page + [a, b])
            self.assertEqual(rc, "halt", text)
            self.assertIn(a, text)
            self.assertIn(b, text)


class _Resp:
    def __init__(self, content=b"", text="", status=200):
        self.content, self.text, self.status = content, text, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, link, **kw):
        self.calls.append((link, kw))
        return self.responses.pop(0)


class Fetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "raw"
        self.link = orig(P1)

    def body(self, bump=0):
        p = write_workbook(
            Path(self.tmp.name) / f"src{bump}.xlsx", date(2026, 4, 1),
            [data_row(c, c, {"total_bed_days_lost": 300 + bump})
             for c in CODES])
        return p.read_bytes()

    def fetch(self, body, **kw):
        s = _Session(_Resp(content=body))
        with quiet(), mock.patch.object(m, "MIN_FILE_BYTES", 1000):
            return m.fetch_month(self.link, self.dest, s, **kw), s

    def test_download_kept_under_link_name_with_user_agent(self):
        path, s = self.fetch(self.body())
        self.assertEqual(path, self.dest / m._name(self.link))
        self.assertTrue(path.exists())
        self.assertIn("User-Agent", s.calls[0][1]["headers"])

    def test_same_name_same_content_is_kept(self):
        b = self.body()
        first, _ = self.fetch(b)
        mtime = first.stat().st_mtime_ns
        second, _ = self.fetch(b)
        self.assertEqual(second, first)
        self.assertEqual(second.stat().st_mtime_ns, mtime)

    def test_same_name_different_content_is_never_replaced(self):
        first, _ = self.fetch(self.body())
        before = m.content_sha256(first)
        second, _ = self.fetch(self.body(bump=1))
        self.assertNotEqual(second, first)
        self.assertEqual(m.content_sha256(first), before)
        self.assertRegex(second.name, r"-[0-9a-f]{8}\.xlsx$")
        self.assertEqual(m.read_cover(second)["period"], date(2026, 4, 1))

    def test_small_file_and_non_workbook_halt_and_keep_nothing(self):
        s = _Session(_Resp(content=b"<html>error</html>"))
        with quiet(), self.assertRaises(SystemExit):
            m.fetch_month(self.link, self.dest, s)
        s = _Session(_Resp(content=b"x" * 2000))
        with quiet(), mock.patch.object(m, "MIN_FILE_BYTES", 1000), \
                self.assertRaises(SystemExit):
            m.fetch_month(self.link, self.dest, s)
        self.assertEqual(list(self.dest.glob("*")) if self.dest.exists()
                         else [], [])

    def test_http_failure_halts(self):
        s = _Session(_Resp(status=500))
        with quiet(), self.assertRaises(SystemExit):
            m.fetch_month(self.link, self.dest, s)


class SpecTests(unittest.TestCase):
    def test_spec_matches_live_columns(self):
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT column_name, data_type FROM "
                        "information_schema.columns WHERE table_name = "
                        "'nhs_drd_discharge_delays'")
            live = dict(cur.fetchall())
        finally:
            conn.rollback()
            conn.close()
        for col, typ in m.SPEC.value_cols:
            want = "integer" if typ == "integer" else "numeric"
            self.assertEqual(live[col], want, col)
        self.assertEqual(live["utla_name"], "character varying")
        self.assertEqual(set(m.VALUE_COLUMNS) | {"reporting_period",
                                                 "utla_code", "utla_name",
                                                 "source", "loaded_at"},
                         set(live))
        self.assertEqual(m.SPEC.as_loaded_source_col, "source")
        self.assertEqual(m.SPEC.refresh_from, (("source", "source_file"),))
        self.assertFalse(m.SPEC.fk_la_boundaries)
        self.assertTrue(m.PROFILE.file_checks)
        self.assertEqual(m.PROFILE.savepoint, "s9a_period")


if __name__ == "__main__":
    unittest.main()
