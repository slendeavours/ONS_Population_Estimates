"""Tests for the period-editions engine (scripts/period_editions.py).

Database tests run on a connection from _db.get_conn() inside a transaction
that is always rolled back (rolled_back below); nothing here commits. The
only tables touched are the throwaway zz_pe_live and zz_pe_editions, created
inside that transaction (refused if either already exists), so they never
persist. Transaction control (commit per period, stop on failure) and the
commands are tested on a mock connection.
"""
import contextlib
import dataclasses
import io
import sys
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
from _db import get_conn  # noqa: E402

N = 5
CODES = [f"E0900000{i}" for i in range(N)]
FETCHED = date(2026, 10, 8)


def make_spec(values=("v1", "v2"), period_type=None):
    return core.EditionSpec(
        name="zz_pe", live_table="zz_pe_live", editions_table="zz_pe_editions",
        key_cols=("lad24cd",), period_col="period",
        value_cols=tuple((v, "integer") for v in values),
        refresh_cols=tuple(values), fk_la_boundaries=False,
        key_types=((("period", period_type),) if period_type else ()))


def make_profile(values=("v1", "v2"), period_type=None):
    return pe.Profile(
        spec=make_spec(values, period_type), value_cols=tuple(values),
        run_agent="Source ZZ - test", run_source="99", heading="ZZ test",
        default_source_file="zz source file", expected_areas=N,
        release_label=lambda d: f"zz fetch {d.isoformat()}",
        savepoint="zz_pe_period")


P = make_profile()


def recs(period, values=("v1", "v2"), n=N, **fns):
    """n records of `period`; value column c is fns[c](i), default 10*i."""
    return [dict({"lad24cd": CODES[i], "period": period},
                 **{c: fns.get(c, lambda i: 10 * i)(i) for c in values})
            for i in range(n)]


def fetcher(by_period):
    return lambda period: by_period[period]


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


@contextmanager
def rolled_back(conn, values=("v1", "v2"), period_type="text"):
    """A cursor in a transaction that is rolled back whatever happens, with
    the throwaway zz_pe_live and zz_pe_editions made inside it."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_pe_live'), "
                    "to_regclass('public.zz_pe_editions')")
        if cur.fetchone() != (None, None):
            raise RuntimeError("zz_pe_live or zz_pe_editions already exists "
                               "as a real table; refusing to run")
        cols = ", ".join(f"{v} integer" for v in values)
        cur.execute(f"""CREATE TABLE public.zz_pe_live (
            lad24cd text NOT NULL, period {period_type} NOT NULL, {cols},
            loaded_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (lad24cd, period))""")
        core.create_schema(cur, make_spec(
            values, None if period_type == "text" else f"{period_type} NOT NULL"))
        yield cur
    finally:
        cur.close()
        conn.rollback()


def editions(cur, period=None):
    """[(edition, supersedes, rows)] of the throwaway editions table."""
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM public.zz_pe_editions"
                + (" WHERE period = %s" if period else "")
                + " GROUP BY 1, 2 ORDER BY 1", (period,) if period else None)
    return cur.fetchall()


def live(cur, period, values=("v1", "v2")):
    cur.execute(f"SELECT lad24cd, {', '.join(values)} FROM public.zz_pe_live "
                "WHERE period = %s", (period,))
    return {r[0]: tuple(r[1:]) for r in cur.fetchall()}


def as_live(records, values=("v1", "v2")):
    return {r["lad24cd"]: tuple(r[c] for c in values) for r in records}


class EngineDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_new_period_becomes_edition_1_and_live_rows(self):
        with rolled_back(self.conn) as cur:
            r = recs("202604", v1=lambda i: None if i == 0 else (
                0 if i == 1 else 10 * i))
            self.assertEqual(pe.classify_period(cur, P, "202604", r), "new")
            self.assertEqual(pe.apply_period(cur, P, "202604", r,
                                             fetched_on=FETCHED), "new")
            self.assertEqual(editions(cur), [(1, None, N)])
            cur.execute("SELECT DISTINCT release_label, published_date, "
                        "source_file FROM public.zz_pe_editions")
            self.assertEqual(cur.fetchall(), [("zz fetch 2026-10-08", FETCHED,
                                               "zz source file")])
            self.assertEqual(live(cur, "202604"), as_live(r))
            self.assertEqual(pe.check_live_equals_edition(cur, P, "202604", 1),
                             [])
            self.assertEqual(pe.live_row_count(cur, P, "202604"), N)
            st = pe.status(cur, P)
            self.assertEqual(st["live_missing"], [])
            self.assertTrue(st["ok"], st)

    def test_unchanged_stores_nothing(self):
        with rolled_back(self.conn) as cur:
            pe.apply_period(cur, P, "202604", recs("202604"), fetched_on=FETCHED)
            cur.execute("SELECT COUNT(*), MAX(loaded_at) FROM public.zz_pe_live")
            before = cur.fetchone()
            ins = mock.MagicMock()
            self.assertEqual(pe.apply_period(cur, P, "202604", recs("202604"),
                                             fetched_on=date(2026, 11, 8),
                                             insert=ins), "unchanged")
            ins.assert_not_called()
            self.assertEqual(editions(cur), [(1, None, N)])
            cur.execute("SELECT COUNT(*), MAX(loaded_at) FROM public.zz_pe_live")
            self.assertEqual(cur.fetchone(), before)

    def test_revised_becomes_edition_2_live_untouched(self):
        with rolled_back(self.conn) as cur:
            r = recs("202604")
            pe.apply_period(cur, P, "202604", r, fetched_on=FETCHED)
            rev = recs("202604", v2=lambda i: 10 * i + (1 if i in (1, 2) else 0))
            cmp = pe.compare_period(cur, P, "202604", rev)
            self.assertEqual((cmp["kind"], cmp["changed"]), ("revised", 2))
            self.assertEqual(cmp["against"], "edition 1")
            self.assertLessEqual(len(cmp["examples"]), 3)
            self.assertIn("(v1, v2)", cmp["examples"][0])
            ins = mock.MagicMock()
            self.assertEqual(pe.apply_period(cur, P, "202604", rev,
                                             fetched_on=FETCHED, insert=ins),
                             "revised")
            ins.assert_not_called()
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertEqual(live(cur, "202604"), as_live(r))
            self.assertEqual(pe.status(cur, P)["pending_refresh"], ["202604"])

    def test_revert_stored_as_new_edition(self):
        with rolled_back(self.conn) as cur:
            a, b = recs("202604"), recs("202604", v1=lambda i: 10 * i + 1)
            kinds = [pe.apply_period(cur, P, "202604", x, fetched_on=FETCHED)
                     for x in (a, b, a, a)]
            self.assertEqual(kinds, ["new", "revised", "revised", "unchanged"])
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N), (3, 2, N)])

    def test_null_versus_zero_is_a_change(self):
        with rolled_back(self.conn) as cur:
            zero = recs("202604", v2=lambda i: 0 if i == 0 else i)
            nulls = recs("202604", v2=lambda i: None if i == 0 else i)
            pe.apply_period(cur, P, "202604", zero, fetched_on=FETCHED)
            self.assertEqual(pe.classify_period(cur, P, "202604", nulls),
                             "revised")
            pe.apply_period(cur, P, "202604", nulls, fetched_on=FETCHED)
            cur.execute("SELECT edition, v2 FROM public.zz_pe_editions "
                        "WHERE lad24cd = %s ORDER BY 1", (CODES[0],))
            self.assertEqual(cur.fetchall(), [(1, 0), (2, None)])

    def test_failure_inserting_live_rolls_back_both(self):
        seen = {}

        def boom(cur, profile, period, records):
            cur.execute("SELECT COUNT(*) FROM public.zz_pe_editions")
            seen["editions_before_failure"] = cur.fetchone()[0]
            pe.insert_live(cur, profile, period, records[:2])
            raise RuntimeError("live insert failed")

        def apply(cur, profile, period, records, **kw):
            return pe.apply_period(cur, profile, period, records, insert=boom,
                                   **kw)
        with rolled_back(self.conn) as cur:
            with quiet():
                rc = pe.load_periods(cur, P, ["202604"],
                                     fetcher({"202604": recs("202604")}),
                                     FETCHED, False, simulate=True, apply=apply)
            self.assertEqual(rc, 1)
            self.assertEqual(seen["editions_before_failure"], N)
            self.assertEqual(editions(cur), [])
            self.assertEqual(live(cur, "202604"), {})

        # live rows that differ from the edition halt the period
        def wrong(cur, profile, period, records):
            pe.insert_live(cur, profile, period,
                           [dict(records[0], v1=-5)] + records[1:])
        with rolled_back(self.conn) as cur:
            with self.assertRaises(SystemExit) as ctx:
                pe.apply_period(cur, P, "202604", recs("202604"),
                                fetched_on=FETCHED, insert=wrong)
            self.assertIn("live rows differ from edition 1",
                          str(ctx.exception.code))

    def test_stranded_period_repaired_without_new_edition(self):
        with rolled_back(self.conn) as cur:
            r = recs("202605")
            core.insert_edition(cur, P.spec, r, "202605", release_label="t",
                                published_date=FETCHED, source_file="t",
                                source_sha256="h", supersedes=None)
            self.assertEqual(pe.live_missing_periods(cur, P), ["202605"])
            self.assertEqual(pe.classify_period(cur, P, "202605", r),
                             pe.LIVE_MISSING)
            stats = {}
            with quiet():
                pe.load_periods(cur, P, ["202605"], fetcher({"202605": r}),
                                FETCHED, False, simulate=True, stats=stats)
            self.assertEqual((stats["live_rows"], stats["stored_rows"]), (N, 0))
            self.assertEqual(pe.apply_period(cur, P, "202605", r,
                                             fetched_on=FETCHED),
                             pe.LIVE_MISSING)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(live(cur, "202605"), as_live(r))
            self.assertTrue(pe.status(cur, P)["ok"])

    def test_preview_writes_nothing(self):
        with rolled_back(self.conn) as cur:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = pe.load_periods(cur, P, ["202604"],
                                     fetcher({"202604": recs("202604")}),
                                     FETCHED, False)
            self.assertEqual(rc, 0)
            self.assertEqual(editions(cur), [])
            self.assertEqual(live(cur, "202604"), {})
            self.assertIn(f"would store edition 1 and insert {N} live rows",
                          out.getvalue())
            # a savepoint was rolled back, not the transaction
            cur.execute("SELECT to_regclass('public.zz_pe_editions')")
            self.assertIsNotNone(cur.fetchone()[0])

    def test_status_flags_period_missing_from_live(self):
        with rolled_back(self.conn) as cur:
            pe.apply_period(cur, P, "202604", recs("202604"), fetched_on=FETCHED)
            core.insert_edition(cur, P.spec, recs("202605"), "202605",
                                release_label="t", published_date=FETCHED,
                                source_file="t", source_sha256="h",
                                supersedes=None)
            st = pe.status(cur, P)
            self.assertEqual(st["live_missing"], ["202605"])
            self.assertFalse(st["ok"])
            text = pe.format_status(P, st)
            self.assertIn("ZZ test: 1 periods in the live table", text)
            self.assertIn("editions month missing from live: 202605", text)
            self.assertTrue(text.endswith("  status: ACTION NEEDED"))
            self.assertEqual(pe.held_periods(cur, P, True), ["202604", "202605"])
            self.assertEqual(pe.held_periods(cur, P, False), ["202604"])

    def test_a_third_value_column_is_supported(self):
        vals = ("v1", "v2", "v3")
        p3 = make_profile(vals)
        with rolled_back(self.conn, values=vals) as cur:
            r = recs("202604", vals, v3=lambda i: 1000 + i)
            self.assertEqual(pe.apply_period(cur, p3, "202604", r,
                                             fetched_on=FETCHED), "new")
            self.assertEqual(live(cur, "202604", vals), as_live(r, vals))
            rev = recs("202604", vals, v3=lambda i: 1000 + i + (i == 4))
            cmp = pe.compare_period(cur, p3, "202604", rev)
            self.assertEqual((cmp["kind"], cmp["changed"]), ("revised", 1))
            self.assertIn("(v1, v2, v3)", cmp["examples"][0])
            self.assertEqual(pe.apply_period(cur, p3, "202604", rev,
                                             fetched_on=FETCHED), "revised")
            cur.execute("SELECT v3 FROM public.zz_pe_editions WHERE edition = 2 "
                        "AND lad24cd = %s", (CODES[4],))
            self.assertEqual(cur.fetchone()[0], 1005)
            self.assertEqual(pe.status(cur, p3)["pending_refresh"], ["202604"])

    def test_date_typed_period_column(self):
        pd = make_profile(period_type="date NOT NULL")
        with rolled_back(self.conn, period_type="date") as cur:
            r = recs("2026-04-01")
            self.assertEqual(pe.apply_period(cur, pd, "2026-04-01", r,
                                             fetched_on=FETCHED), "new")
            self.assertEqual(pe.held_periods(cur, pd, True), ["2026-04-01"])
            self.assertEqual(pe.compare_period(cur, pd, "2026-04-01",
                                               r)["kind"], "unchanged")
            core.insert_edition(cur, pd.spec, recs("2026-05-01"), "2026-05-01",
                                release_label="t", published_date=FETCHED,
                                source_file="t", source_sha256="h",
                                supersedes=None)
            self.assertEqual(pe.live_missing_periods(cur, pd), ["2026-05-01"])
            st = pe.status(cur, pd)
            self.assertEqual(st["live_missing"], ["2026-05-01"])
            self.assertIn("2026-05-01", pe.format_status(pd, st))
            self.assertEqual(pe.apply_period(cur, pd, "2026-05-01",
                                             recs("2026-05-01"),
                                             fetched_on=FETCHED),
                             pe.LIVE_MISSING)
            self.assertTrue(pe.status(cur, pd)["ok"])
            old, label = pe.stored(cur, pd, "2026-04-01", "editions")
            self.assertEqual((old, label), (as_live(r), "edition 1"))


class _Borrowed:
    """A connection stand-in for the run_* commands that hands out the test's
    own cursor (inside rolled_back) and never commits, rolls back or closes
    the real connection: the throwaway tables and everything written to them
    go with rolled_back's rollback."""

    def __init__(self, cur):
        self.cur = cur
        self.rollbacks = 0

    @contextmanager
    def cursor(self):
        yield self.cur

    def commit(self):
        raise AssertionError("a test must never commit")

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class EngineDateCommands(unittest.TestCase):
    """refresh-latest and sync-new on a DATE period column: CLI periods are
    ISO strings, messages name ISO strings, never datetime.date(...)."""

    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()
        cls.pd = make_profile(period_type="date NOT NULL")

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def _refresh(self, cur, accept=None):
        args = mock.Mock(commit=False, simulate=True, accept_drift=accept)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                rc = pe.run_refresh_latest(self.pd, args,
                                           connect=lambda w: _Borrowed(cur),
                                           table_exists=lambda c, t: True)
            except SystemExit as e:
                return "halt", str(e.code), out.getvalue()
        return rc, None, out.getvalue()

    def _drifted(self, cur):
        for p in ("2026-04-01", "2026-05-01"):
            pe.apply_period(cur, self.pd, p, recs(p), fetched_on=FETCHED)
        cur.execute("UPDATE public.zz_pe_live SET v1 = 999 "
                    "WHERE period = '2026-04-01' AND lad24cd = %s", (CODES[0],))

    def test_refresh_latest_accept_drift_with_iso_date(self):
        with rolled_back(self.conn, period_type="date") as cur:
            self._drifted(cur)
            cur.execute("SAVEPOINT t")
            rc, msg, out = self._refresh(cur)
            cur.execute("ROLLBACK TO SAVEPOINT t")
            self.assertEqual(rc, "halt")
            self.assertIn("matches no stored edition for ['2026-04-01']", msg)
            self.assertNotIn("datetime.date(", msg + out)
            rc, msg, out = self._refresh(cur, ["2026-04-01"])
            self.assertEqual(rc, 0, msg)
            self.assertIn("rows refresh-latest would write: 2026-04-01=1", out)
            self.assertIn("1 live rows refreshed in ['2026-04-01']", out)
            self.assertNotIn("datetime.date(", out)
            self.assertEqual(live(cur, "2026-04-01"),
                             as_live(recs("2026-04-01")))

    def test_accept_of_a_period_not_drifted_halts_naming_iso(self):
        with rolled_back(self.conn, period_type="date") as cur:
            self._drifted(cur)
            for accept, named in ((["2026-05-01"], "['2026-05-01']"),
                                  (["2026-04-01", "2026-09-01"],
                                   "['2026-09-01']")):
                cur.execute("SAVEPOINT t")
                rc, msg, out = self._refresh(cur, accept)
                cur.execute("ROLLBACK TO SAVEPOINT t")
                self.assertEqual(rc, "halt", accept)
                self.assertIn(f"--accept-drift {named}: not drifted periods",
                              msg)
                self.assertNotIn("datetime.date(", msg + out)

    def test_sync_new_records_edition_1_for_date_periods(self):
        with rolled_back(self.conn, period_type="date") as cur:
            for p in ("2026-04-01", "2026-05-01"):
                pe.insert_live(cur, self.pd, p, recs(p))
            args = mock.Mock(commit=False, simulate=True,
                             expected_authorities=N)
            out = io.StringIO()
            # log_run would write pipeline_run_log, a real table: stubbed
            with mock.patch.object(pe, "log_run") as logged, \
                    contextlib.redirect_stdout(out):
                rc = pe.run_sync_new(self.pd, args,
                                     connect=lambda w: _Borrowed(cur),
                                     table_exists=lambda c, t: True)
            self.assertEqual(rc, 0)
            self.assertEqual(editions(cur), [(1, None, 2 * N)])
            text = out.getvalue()
            self.assertIn("months with no editions: 2026-04-01, 2026-05-01",
                          text)
            self.assertIn(f"  2026-04-01: edition 1 recorded, {N} rows", text)
            self.assertIn("2 month(s) recorded as edition 1", text)
            (c, prof, n, notes), _ = logged.call_args
            self.assertEqual(n, 2 * N)   # the = ANY(%s) count over dates
            self.assertIn("for 2026-04-01, 2026-05-01", notes)
            self.assertNotIn("datetime.date(", text + notes)
            self.assertTrue(pe.status(cur, self.pd)["ok"])


class EngineControl(unittest.TestCase):
    """Transaction control and commands on a mock connection."""

    def test_load_periods_per_period_commit_and_failure_exit(self):
        cur = mock.MagicMock()
        compare = mock.MagicMock(return_value={
            "kind": "new", "changed": N, "examples": [], "against": "no edition"})
        apply = mock.MagicMock(side_effect=["new", RuntimeError("boom")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pe.load_periods(cur, P, ["202604", "202605", "202606"],
                                 fetcher({p: recs(p) for p in
                                          ("202604", "202605", "202606")}),
                                 FETCHED, True, compare=compare, apply=apply)
        self.assertEqual(rc, 1)
        cur.connection.commit.assert_called_once()   # 202604 only
        cur.connection.rollback.assert_called()
        sql = [c.args[0] for c in cur.execute.call_args_list]
        self.assertIn("SAVEPOINT zz_pe_period", sql)
        self.assertIn("RELEASE SAVEPOINT zz_pe_period", sql)
        self.assertIn("ROLLBACK TO SAVEPOINT zz_pe_period", sql)
        self.assertIn("202605: FAILED, nothing stored for this month",
                      out.getvalue())
        self.assertIn("not attempted: 202606", out.getvalue())
        self.assertEqual(apply.call_args.kwargs["source_file"],
                         "zz source file")
        # a records check that fails stops before compare or apply
        compare.reset_mock()
        apply = mock.MagicMock()
        bad = dataclasses.replace(P, check_records=mock.MagicMock(
            side_effect=ValueError("short")))
        cur = mock.MagicMock()
        with quiet():
            rc = pe.load_periods(cur, bad, ["202604"],
                                 fetcher({"202604": recs("202604")}),
                                 FETCHED, True, compare=compare, apply=apply)
        self.assertEqual(rc, 1)
        compare.assert_not_called()
        apply.assert_not_called()
        cur.connection.commit.assert_not_called()
        with self.assertRaises(ValueError):
            pe.load_periods(cur, P, [], fetcher({}), FETCHED, True,
                            simulate=True)

    def _conn(self):
        conn = mock.MagicMock()
        return conn, conn.cursor.return_value.__enter__.return_value

    def _inserts(self, cur):
        return [c for c in cur.execute.call_args_list
                if "INSERT INTO pipeline_run_log" in str(c.args[0])]

    def test_run_log_only_on_commit(self):
        for flags, committed, logged in (
                ({"commit": True, "simulate": False}, True, True),
                ({"commit": False, "simulate": True}, False, True),
                ({"commit": False, "simulate": False}, False, False)):
            conn, cur = self._conn()
            cur.fetchone.return_value = (N,)
            args = mock.Mock(expected_authorities=N, **flags)
            with mock.patch.object(core, "latest_map",
                                   return_value=(None, ["202607"], [])), \
                    mock.patch.object(core, "sync_new",
                                      return_value=["202607"]), \
                    mock.patch.object(load_checks, "check_latest_equals_live",
                                      return_value=[]), \
                    quiet():
                rc = pe.run_sync_new(
                    P, args, connect=lambda w: conn,
                    table_exists=lambda c, t: True,
                    status=lambda c: {"new_periods": [], "chain_errors": {}})
            self.assertEqual(rc, 0)
            inserts = self._inserts(cur)
            self.assertEqual(len(inserts), 1 if logged else 0, flags)
            if logged:
                self.assertEqual(inserts[0].args[1][:4],
                                 ("Source ZZ - test", "99", "99", N))
            self.assertEqual(conn.commit.called, committed, flags)
        cur = mock.MagicMock()
        pe.log_run(cur, P, 7, "notes")
        (sql, params), = [c.args for c in cur.execute.call_args_list]
        self.assertIn("'success'", sql)
        self.assertEqual(params, ("Source ZZ - test", "99", "99", 7, None,
                                  "notes"))

    def test_mode_parser_commit_and_simulate_exclusive(self):
        import argparse
        ap = argparse.ArgumentParser()
        pe.mode_parser(ap)
        self.assertTrue(ap.parse_args(["--commit"]).commit)
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            ap.parse_args(["--commit", "--simulate"])


if __name__ == "__main__":
    unittest.main()
