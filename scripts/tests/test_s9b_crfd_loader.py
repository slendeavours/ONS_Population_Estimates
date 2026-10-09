"""Tests for the S9b loader in s9b_crfd_editions (spec, fetch, the commands).

No network: the publication pages and the downloads are stubbed, and every
data file is built in the test (write_data_file from test_s9b_crfd_pure).
Database tests run on a connection from _db.get_conn() inside a transaction
that is always rolled back (rolled_back below); nothing here commits: the
commands get a stand-in connection (_Borrowed) whose commit only counts, and a
cursor proxy whose .connection is a stand-in too, so the engine's per-month
commit never reaches the real connection. The only tables touched are the
throwaway zz_s9b_live, zz_s9b_editions and zz_s9b_editions_file_checks (a copy
of SPEC named zz_s9b), created inside that transaction (refused if any
already exists), so they never persist. la_boundaries and the run log are
stubbed; the real la_code_lookup recode rows are read (never written).
"""
import contextlib
import dataclasses
import io
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s9b_crfd_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s9b_crfd_pure import (final_name, html_of, perf_name,  # noqa: E402
                                url, write_data_file)

ZZ = dataclasses.replace(
    m.SPEC, name="zz_s9b", live_table="zz_s9b_live",
    editions_table="zz_s9b_editions", fk_la_boundaries=False)
# 160 areas: the Performance-to-Final test changes 150 of them
CODES = [f"E06000{i:03d}" for i in range(1, 159)] + ["E08000016",
                                                     "E08000019"]
N = len(CODES)
P1, P2, P3, P4 = "2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"
THIS_MONTH = "2026-05-01"
LEDGER = "zz_s9b_editions_file_checks"
_TAGS = {}


def link(name):
    """A stable files.digital.nhs.uk URL per file name (its own hash path)."""
    tag = _TAGS.setdefault(name, f"{len(_TAGS) % 100:02d}/{len(_TAGS):06X}")
    return url(name, tag)


def perf(p, v=1):
    return link(perf_name(p, v))


def final(p, v=1):
    return link(final_name(p, v))


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
        cur.execute("SELECT to_regclass('public.zz_s9b_live'), "
                    "to_regclass('public.zz_s9b_editions'), "
                    f"to_regclass('public.{LEDGER}')")
        if cur.fetchone() != (None, None, None):
            raise RuntimeError("a zz_s9b table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s9b_live (LIKE "
                    "public.nhs_mh_crfd INCLUDING ALL)")
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
                "public.zz_s9b_editions"
                + (" WHERE reporting_period = %s" if period else "")
                + " GROUP BY 1, 2 ORDER BY 1", (period,) if period else None)
    return cur.fetchall()


def ledger(cur, period=None):
    """[(source_file name, outcome, edition)] in order."""
    cur.execute(f"SELECT source_file, outcome, edition FROM public.{LEDGER}"
                + (" WHERE reporting_period = %s" if period else "")
                + " ORDER BY id", (period,) if period else None)
    return [(m._name(s), o, e) for s, o, e in cur.fetchall()]


def live(cur, period):
    cur.execute("SELECT lad24cd, measure_value FROM public.zz_s9b_live "
                "WHERE reporting_period = %s", (period,))
    return dict(cur.fetchall())


def live_source(cur, period):
    cur.execute("SELECT DISTINCT source FROM public.zz_s9b_live WHERE "
                "reporting_period = %s", (period,))
    return cur.fetchall()


def base_value(i, period):
    return 100 + 5 * i + int(period[5:7])


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

    def make(self, lnk, period, *, values=None, codes=None, status=None,
             transform=None):
        """Build the data file for `lnk`; register it for the download stub.
        values: {code: value} overrides; transform(i, code, v) -> value."""
        vals = []
        for i, c in enumerate(codes or CODES):
            v = base_value(i, period)
            if transform:
                v = transform(i, c, v)
            v = (values or {}).get(c, v)
            vals.append((c, "*" if v is None else v))
        kind = m.classify_link(lnk)[0]
        self.n += 1
        d = Path(self.tmp.name) / str(self.n)
        d.mkdir()
        path = write_data_file(d / m._name(lnk), period, vals,
                               status=status or kind)
        self.files[lnk] = path
        return path

    def run_main(self, cur, argv, *, page=(), table=True, file=None):
        """main(argv) on the throwaway tables with the publication pages
        stubbed to the given links. Returns (rc or 'halt', text, borrowed,
        log mock)."""
        borrowed = _Borrowed(cur)
        args = list(argv)
        if file:
            args += ["--file", str(file)]
        pages = {}
        for lnk in page:
            pages.setdefault(m.period_from_link(lnk), []).append(lnk)

        def fetch_page(period, session=None):
            if period not in pages:
                return None
            return html_of(*pages[period])

        def fetch_month(lnk, dest=None, session=None):
            self.fetched.append(lnk)
            return self.files[lnk]

        out = io.StringIO()
        with mock.patch.object(m, "SPEC", ZZ), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "valid_codes", return_value=set(CODES)), \
                mock.patch.object(m, "fetch_page", side_effect=fetch_page), \
                mock.patch.object(m, "fetch_month", side_effect=fetch_month), \
                mock.patch.object(m, "_this_month", return_value=THIS_MONTH), \
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

    def seed(self, cur, periods=(P1, P2, P3), lnk=perf):
        page = []
        for p in periods:
            self.make(lnk(p), p)
            page.append(lnk(p))
        rc, text, _, _ = self.run_main(
            cur, ["load", "--commit", "--min-period", periods[0],
                  "--expected-areas", str(N)], page=page)
        self.assertEqual(rc, 0, text)
        self.fetched.clear()
        return page

    @staticmethod
    def final_like(i, c, v):
        """150 of 160 areas change: 20 by +60%, 130 by +5% (national total
        moves about +10%)."""
        if i < 20:
            return int(v * 1.6)
        if i < 150:
            return v + 5
        return v

    # -- the brief's tests --------------------------------------------------

    def test_performance_to_final_is_edition_2_and_not_stopped(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live(cur, P2)
            self.make(final(P2), P2, transform=self.final_like)
            page = [perf(P1), perf(P2), final(P2), perf(P3)]
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                page=page)
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [final(P2)])
            self.assertIn(f"{P2}: Performance to Final: 150 areas changed",
                          text)
            self.assertIn(f"{P2}: revised, 150 rows changed", text)
            self.assertNotIn("REJECTED", text)
            self.assertEqual(editions(cur, P2), [(1, None, N), (2, 1, N)])
            self.assertEqual(live(cur, P2), before)     # until refresh
            self.assertEqual(ledger(cur, P2)[-1],
                             (final_name(P2), "revised", 2))
            cur.execute("SELECT release_label, source_file FROM "
                        "public.zz_s9b_editions WHERE reporting_period = %s "
                        "AND edition = 2 LIMIT 1", (P2,))
            label, src = cur.fetchone()
            self.assertEqual(src, final(P2))
            self.assertIn("Final data file", label)
            self.assertIn("year-end Final", label)
            self.assertIn("Performance to Final", logged.call_args[0][2])
            rc, text, _, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertEqual(live(cur, P2)[CODES[0]],
                             int(base_value(0, P2) * 1.6))
            # values are written only where they changed, but the source is
            # the Final file's on every row of the refreshed month
            cur.execute("SELECT source, COUNT(*) FROM public.zz_s9b_live "
                        "WHERE reporting_period = %s GROUP BY 1 ORDER BY 2",
                        (P2,))
            self.assertEqual(cur.fetchall(), [(final(P2), 160)])
            self.assertTrue(m.status(cur, ZZ)["ok"])
            # rerun: nothing fetched, no new edition
            self.fetched.clear()
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page)
            self.assertEqual((rc, self.fetched), (0, []), text)
            self.assertEqual(editions(cur, P2), [(1, None, N), (2, 1, N)])

    def test_same_changes_on_a_reissue_stop_until_acknowledged(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.make(perf(P2, 2), P2, transform=self.final_like)
            page = [perf(P1), perf(P2, 2), perf(P3)]
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                page=page)
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P2}: REJECTED, not stored", text)
            self.assertIn("revised above 50% in 20 areas", text)
            self.assertEqual(editions(cur, P2), [(1, None, N)])
            self.assertEqual(ledger(cur, P2), [(perf_name(P2), "new", 1)])
            logged.assert_not_called()
            rc, text, _, logged = self.run_main(
                cur, ["load", "--commit", "--acknowledge", P2], page=page)
            self.assertEqual(rc, 0, text)
            self.assertIn("ACKNOWLEDGED", text)
            self.assertEqual(editions(cur, P2), [(1, None, N), (2, 1, N)])
            cur.execute("SELECT release_label FROM public.zz_s9b_editions "
                        "WHERE reporting_period = %s AND edition = 2 LIMIT 1",
                        (P2,))
            self.assertIn("ACKNOWLEDGED", cur.fetchone()[0])
            self.assertIn("ACKNOWLEDGED", logged.call_args[0][2])

    def test_reissue_null_replacing_numbers_in_six_areas_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.make(perf(P3, 2), P3, values={c: None for c in CODES[:6]})
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit"],
                page=[perf(P1), perf(P2), perf(P3, 2)])
            self.assertEqual(rc, 1, text)
            self.assertIn("NULL replaces a number in 6 areas", text)
        with rolled_back(self.conn) as cur:        # five areas is allowed
            self.seed(cur)
            self.make(perf(P3, 2), P3, values={c: None for c in CODES[:5]})
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit"],
                page=[perf(P1), perf(P2), perf(P3, 2)])
            self.assertEqual(rc, 0, text)

    def test_final_moving_national_total_over_50pct_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.make(final(P2), P2, transform=lambda i, c, v: v * 2)
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit"],
                page=[perf(P1), perf(P2), final(P2), perf(P3)])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P2}: REJECTED, not stored", text)
            self.assertIn("national total", text)
            self.assertEqual(editions(cur, P2), [(1, None, N)])

    def test_performance_link_after_a_final_halts(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, lnk=lambda p: final(p) if p == P2 else perf(p))
            self.make(perf(P2, 3), P2)
            page = [perf(P1), perf(P2, 3), perf(P3)]
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page)
            self.assertEqual(rc, "halt", text)
            self.assertIn("latest file", text)
            self.assertIn("--allow-older-file", text)
            self.assertEqual(self.fetched, [])
            self.assertEqual(editions(cur, P2), [(1, None, N)])
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit", "--allow-older-file"], page=page)
            self.assertEqual(rc, 0, text)
            self.assertIn("NOTE: --allow-older-file given", text)

    def test_unchanged_v2_recorded_once_and_not_fetched_again(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.make(perf(P2, 2), P2)                 # same figures
            page = [perf(P1), perf(P2, 2), perf(P3)]
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"],
                                                page=page)
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [perf(P2, 2)])
            self.assertIn(f"{P2}: unchanged", text)
            self.assertEqual(editions(cur, P2), [(1, None, N)])
            self.assertEqual(ledger(cur, P2),
                             [(perf_name(P2), "new", 1),
                              (perf_name(P2, 2), "unchanged", 1)])
            logged.assert_called_once()
            self.fetched.clear()
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page)
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [])
            self.assertIn("nothing to fetch", text)
            self.assertEqual(len(ledger(cur, P2)), 2)
            # --recheck-all reads every held month again
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit", "--recheck-all"], page=page)
            self.assertEqual(rc, 0, text)
            self.assertEqual(sorted(self.fetched), sorted(page))
            self.assertEqual(editions(cur), [(1, None, 3 * N)])

    def test_new_month_edition_1_and_live_in_one_transaction(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.make(perf(P4), P4)
            rc, text, _, logged = self.run_main(
                cur, ["load", "--commit"], page=page + [perf(P4)])
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [perf(P4)])
            self.assertEqual(editions(cur, P4), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s9b_live",
                                   "WHERE reporting_period = %s", (P4,)), N)
            self.assertEqual(core.rows_differing(cur, ZZ, P4, 1), 0)
            self.assertEqual(live_source(cur, P4), [(perf(P4),)])
            cur.execute("SELECT DISTINCT la_name IS NOT NULL, measure_name, "
                        "measure_id FROM public.zz_s9b_live WHERE "
                        "reporting_period = %s", (P4,))
            self.assertEqual(cur.fetchall()[0][2], "MHS26")
            self.assertEqual(ledger(cur, P4), [(perf_name(P4), "new", 1)])
            logged.assert_called_once()
        # a failure inserting the live rows rolls back the edition and the
        # ledger row too
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.make(perf(P4), P4)
            with mock.patch.object(m, "insert_live",
                                   side_effect=RuntimeError("boom")):
                rc, text, _, logged = self.run_main(
                    cur, ["load", "--commit"], page=page + [perf(P4)])
            self.assertEqual(rc, 1, text)
            self.assertEqual(editions(cur, P4), [])
            self.assertEqual(ledger(cur, P4), [])
            self.assertEqual(count(cur, "zz_s9b_live",
                                   "WHERE reporting_period = %s", (P4,)), 0)
            logged.assert_not_called()

    def test_preview_writes_nothing(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.make(perf(P4), P4)
            self.make(final(P2), P2, transform=self.final_like)
            page = [perf(P1), final(P2), perf(P3), perf(P4)]
            snap = (editions(cur), ledger(cur), live(cur, P2),
                    count(cur, "zz_s9b_live"))
            for flags in ([], ["--recheck-all"]):
                rc, text, _, logged = self.run_main(cur, ["load"] + flags,
                                                    page=page)
                self.assertEqual(rc, 0, text)
                self.assertIn("PREVIEW: nothing written", text)
                self.assertIn(f"{P4}: new", text)
                self.assertIn(f"{P2}: revised", text)
                logged.assert_not_called()
                self.assertEqual(snap, (editions(cur), ledger(cur),
                                        live(cur, P2),
                                        count(cur, "zz_s9b_live")))
            rc, text, _, logged = self.run_main(cur, ["load", "--simulate"],
                                                page=page)
            self.assertEqual(rc, 0, text)
            self.assertIn("SIMULATION", text)
            logged.assert_not_called()
            self.assertEqual(snap, (editions(cur), ledger(cur),
                                    live(cur, P2), count(cur, "zz_s9b_live")))

    def test_barnsley_sheffield_new_codes_resolve(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            codes = CODES[:-2] + ["E08000038", "E08000039"]
            self.make(perf(P4), P4, codes=codes)
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page + [perf(P4)])
            self.assertEqual(rc, 0, text)
            got = live(cur, P4)
            self.assertIn("E08000016", got)
            self.assertIn("E08000019", got)
            self.assertNotIn("E08000038", got)
            self.assertIn("2026-04 38/39", text)

    def test_both_forms_of_one_area_in_one_file_halt(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.make(perf(P4), P4, codes=CODES + ["E08000038"])
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page + [perf(P4)])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Barnsley", text)
            self.assertIn("both", text)
            self.assertEqual(editions(cur, P4), [])

    # -- other behaviour ----------------------------------------------------

    def test_short_month_is_rejected_not_stored(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.make(perf(P4), P4, codes=CODES[:-1])
            rc, text, _, logged = self.run_main(
                cur, ["load", "--commit"], page=page + [perf(P4)])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{N - 1} areas, expected {N}", text)
            self.assertEqual(editions(cur, P4), [])
            logged.assert_not_called()

    def test_negative_value_rejected(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.make(perf(P4), P4, values={CODES[0]: -5})
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page + [perf(P4)])
            self.assertEqual(rc, 1, text)
            self.assertIn("negative", text)

    def test_identity_mismatch_halts(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            # a Final link whose file says Performance
            self.make(final(P3), P3, status="Performance")
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page + [final(P3)])
            self.assertEqual(rc, "halt", text)
            self.assertIn("identity", text)
            self.assertIn("STATUS", text)
            self.assertEqual(editions(cur, P3), [(1, None, N)])

    def test_unknown_code_halts(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            self.make(perf(P4), P4, codes=CODES[:-1] + ["E06000999"])
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page + [perf(P4)])
            self.assertEqual(rc, "halt", text)
            self.assertIn("UNEXPLAINED E06000999", text)

    def test_zero_to_null_needs_acknowledge(self):
        with rolled_back(self.conn) as cur:
            self.make(perf(P1), P1, values={CODES[0]: 0})
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit", "--min-period", P1,
                      "--expected-areas", str(N)], page=[perf(P1)])
            self.assertEqual(rc, 0, text)
            self.make(final(P1), P1, values={CODES[0]: None})
            page = [perf(P1), final(P1)]
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page)
            self.assertEqual(rc, 1, text)
            self.assertIn("0 to NULL", text)
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit", "--acknowledge", P1], page=page)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, P1), [(1, None, N), (2, 1, N)])

    def test_page_latest_earlier_than_held_halts(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=[perf(P1), perf(P2)])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2026-03 is already held", text)

    def test_stranded_month_is_repaired(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            cur.execute("DELETE FROM public.zz_s9b_live WHERE "
                        "reporting_period = %s", (P2,))
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           page=page)
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [perf(P2)])
            self.assertEqual(count(cur, "zz_s9b_live",
                                   "WHERE reporting_period = %s", (P2,)), N)
            self.assertEqual(ledger(cur, P2)[-1][1], "live-missing")
            self.assertTrue(m.status(cur, ZZ)["ok"])

    def test_without_editions_table_preview_compares_with_live(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.make(final(P3), P3, transform=self.final_like)
            before = (editions(cur), ledger(cur))
            rc, text, _, _ = self.run_main(
                cur, ["load"], table=False,
                page=[perf(P1), perf(P2), final(P3)])
            self.assertEqual(rc, 0, text)
            self.assertIn("does not exist yet", text)
            self.assertIn("LIVE table", text)
            self.assertIn(f"{P3}: Performance to Final: 150 areas changed",
                          text)
            self.assertEqual(self.fetched, [final(P3)])
            self.assertEqual(before, (editions(cur), ledger(cur)))
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit"], table=False,
                page=[perf(P1), perf(P2), final(P3)])
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
            self.assertTrue(pe.table_exists(cur, LEDGER))

    def test_sync_new_records_held_months_as_edition_1(self):
        with rolled_back(self.conn) as cur:
            cur.execute("INSERT INTO public.zz_s9b_live (reporting_period, "
                        "lad24cd, la_name, measure_id, measure_name, "
                        "measure_value, source) SELECT %s, c, c, 'MHS26', "
                        "'m', 5, %s FROM unnest(%s::text[]) c",
                        (P1, perf(P1), CODES))
            rc, text, _, _ = self.run_main(
                cur, ["sync-new", "--commit", "--expected-areas", str(N)])
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, P1), [(1, None, N)])
            cur.execute("SELECT DISTINCT source_file FROM "
                        "public.zz_s9b_editions")
            self.assertEqual(cur.fetchall(), [(perf(P1),)])

    def test_file_option_reads_identity_from_the_file(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            f = self.make(final(P2), P2, transform=self.final_like)
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, 0, text)
            self.assertIn("file given: nothing downloaded", text)
            self.assertIn("2026-02-01 Final (read from the file)", text)
            self.assertEqual(self.fetched, [])
            self.assertEqual(editions(cur, P2), [(1, None, N), (2, 1, N)])
            # a file whose name names another month halts
            wrong = write_data_file(
                Path(self.tmp.name) / perf_name(P3), P2,
                [(c, 5) for c in CODES], status="Performance")
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           file=wrong)
            self.assertEqual(rc, "halt", text)
            self.assertIn("identity", text)

    def test_tie_between_two_links_halts_naming_both(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            a = url(perf_name(P4), "AA/AAAAAA")
            b = url(perf_name(P4), "BB/BBBBBB")
            rc, text, _, _ = self.run_main(cur, ["load"], page=page + [a, b])
            self.assertEqual(rc, "halt", text)
            self.assertIn(a, text)
            self.assertIn(b, text)

    def test_link_naming_another_month_is_ignored(self):
        with rolled_back(self.conn) as cur:
            page = self.seed(cur)
            pages = {P1: [perf(P1)], P2: [perf(P2)], P3: [perf(P3),
                                                          perf(P4)]}
            with mock.patch.object(m, "fetch_page",
                                   side_effect=lambda p, session=None:
                                   html_of(*pages[p]) if p in pages
                                   else None), quiet():
                links, notes = m.discover([P1, P2, P3, P4])
            self.assertEqual(links, {P1: [perf(P1)], P2: [perf(P2)],
                                     P3: [perf(P3)]})
            self.assertTrue(any("names 2026-04-01; ignored" in n
                                for n in notes))
            self.assertTrue(any(n.startswith(f"{P4}: no publication page")
                                for n in notes))
            del page


class _Resp:
    def __init__(self, content=b"", text="", status=200):
        self.content, self.text, self.status_code = content, text, status
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, lnk, **kw):
        self.calls.append((lnk, kw))
        return self.responses.pop(0)


class Fetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "raw"
        self.link = perf(P1)

    def body(self, bump=0, as_zip=True):
        p = write_data_file(
            Path(self.tmp.name) / (f"src{bump}" + (".zip" if as_zip
                                                    else ".csv")),
            P1, [(c, 100 + bump) for c in CODES], as_zip=as_zip)
        return p.read_bytes()

    def fetch(self, body, lnk=None):
        s = _Session(_Resp(content=body))
        with quiet(), mock.patch.object(m, "MIN_FILE_BYTES", 1000), \
                mock.patch.object(m, "CHUNK", 4096):
            return m.fetch_month(lnk or self.link, self.dest, s), s

    def test_chunked_download_kept_under_link_name(self):
        path, s = self.fetch(self.body())
        self.assertEqual(path, self.dest / perf_name(P1))
        self.assertEqual(m.parse_file(path)[1]["periods"], [P1])
        self.assertIn("User-Agent", s.calls[0][1]["headers"])
        self.assertTrue(s.calls[0][1]["stream"])
        self.assertEqual([p.name for p in self.dest.iterdir()],
                         [perf_name(P1)])

    def test_plain_csv_download(self):
        lnk = link("MHSDS Data_SepPrf_2023.csv")
        path, _ = self.fetch(self.body(as_zip=False), lnk)
        self.assertEqual(path.suffix, ".csv")

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
        self.assertRegex(second.name, r"-[0-9a-f]{8}\.zip$")

    def test_small_or_unusable_file_halts_and_keeps_nothing(self):
        s = _Session(_Resp(content=b"<html>error</html>"))
        with quiet(), self.assertRaises(SystemExit):
            m.fetch_month(self.link, self.dest, s)
        s = _Session(_Resp(content=b"x" * 5000))
        with quiet(), mock.patch.object(m, "MIN_FILE_BYTES", 1000), \
                self.assertRaises(SystemExit):
            m.fetch_month(self.link, self.dest, s)
        self.assertEqual(list(self.dest.glob("*")), [])

    def test_http_failure_halts(self):
        s = _Session(_Resp(status=500))
        with quiet(), self.assertRaises(SystemExit):
            m.fetch_month(self.link, self.dest, s)
        self.assertEqual(list(self.dest.glob("*")), [])

    def test_fetch_page_tries_both_slugs(self):
        s = _Session(_Resp(status=404), _Resp(text="<html>ok</html>"))
        self.assertEqual(m.fetch_page("2023-04-01", s), "<html>ok</html>")
        self.assertTrue(s.calls[1][0].endswith(
            "performance-april-provisional-may-2023"))
        s = _Session(_Resp(status=404), _Resp(status=404))
        self.assertIsNone(m.fetch_page("2027-01-01", s))
        s = _Session(_Resp(status=500))
        with quiet(), self.assertRaises(SystemExit):
            m.fetch_page("2026-04-01", s)


class SpecTests(unittest.TestCase):
    def test_spec_matches_live_columns(self):
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT column_name, data_type FROM "
                        "information_schema.columns WHERE table_name = "
                        "'nhs_mh_crfd'")
            live_cols = dict(cur.fetchall())
        finally:
            conn.rollback()
            conn.close()
        self.assertEqual(live_cols["measure_value"], "integer")
        self.assertEqual(live_cols["la_name"], "character varying")
        self.assertEqual(live_cols["measure_name"], "text")
        self.assertEqual(set(live_cols),
                         {"reporting_period", "lad24cd", "la_name",
                          "measure_id", "measure_name", "measure_value",
                          "source", "loaded_at"})
        self.assertEqual(m.SPEC.key_cols, ("lad24cd", "measure_id"))
        self.assertIn(("measure_id", "varchar NOT NULL"), m.SPEC.key_types)
        self.assertEqual(m.SPEC.refresh_cols, ("measure_value", "la_name",
                                               "measure_name", "source"))
        self.assertEqual(m.SPEC.as_loaded_source_col, "source")
        self.assertEqual(m.SPEC.refresh_from, (("source", "source_file"),))
        self.assertTrue(m.SPEC.refresh_source_whole_period)
        self.assertTrue(m.PROFILE.file_checks)
        self.assertEqual(m.PROFILE.savepoint, "s9b_period")
        self.assertEqual(m.PROFILE.run_agent, "Source 9b - MHSDS MHS26 CRFD")


if __name__ == "__main__":
    unittest.main()
