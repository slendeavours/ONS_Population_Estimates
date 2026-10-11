"""Tests for the single-vintage opt-in, EditionSpec.live_single_period (the
reference trio: S5, S3b, S7).

A single-vintage live table holds one period (vintage) only. With the flag
on, a different vintage is stored as editions only ('held-not-live') and
never reaches live through load, sync-new or refresh-latest; a reissue of
the live vintage is the next edition and refreshes as usual. With the flag
off every generated statement and every result is the same as the code at
the branch base (8b5a286), which the last test class proves by running one
scenario through both and comparing the SQL text statement for statement.

Every database test runs on a connection from _db.get_conn() inside a
transaction that is always rolled back. The only tables touched are the
throwaway zz_ec_sp_live, zz_ec_sp_editions and zz_ec_sp_editions_file_checks,
created inside that transaction, so they never persist. Nothing here
commits.
"""
import dataclasses
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager, redirect_stdout
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
from _db import get_conn  # noqa: E402

BASE_COMMIT = "8b5a286"
FETCHED = date(2026, 10, 10)
CODES = ("E06000001", "E06000002", "E06000003")
N = len(CODES)
V1, V2 = "2021", "2031"

SP = core.EditionSpec(
    name="zz_ec_sp",
    live_table="zz_ec_sp_live",
    editions_table="zz_ec_sp_editions",
    key_cols=("lad24cd",),
    period_col="vintage",
    value_cols=(("value", "integer"),),
    refresh_cols=("value",),
    fk_la_boundaries=False,
    live_single_period=True,
)
OFF = dataclasses.replace(SP, live_single_period=False)


def profile(spec, file_checks=False):
    return pe.Profile(
        spec=spec, value_cols=("value",), run_agent="Source ZZ - test",
        run_source="99", heading="ZZ single-vintage test",
        default_source_file="zz source file", expected_areas=N,
        release_label=lambda d: f"zz fetch {d.isoformat()}",
        savepoint="zz_ec_sp_period", file_checks=file_checks)


P = profile(SP)
P_OFF = profile(OFF)


def recs(period, base=10):
    return [{"lad24cd": c, "vintage": period, "value": base * (i + 1)}
            for i, c in enumerate(CODES)]


@contextmanager
def rolled_back(conn):
    """A cursor in a transaction that is rolled back whatever happens, with
    the throwaway live and editions tables made inside it."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_ec_sp_live'), "
                    "to_regclass('public.zz_ec_sp_editions'), "
                    "to_regclass('public.zz_ec_sp_editions_file_checks')")
        if cur.fetchone() != (None, None, None):
            raise RuntimeError("a zz_ec_sp table already exists as a real "
                               "table; refusing to run")
        cur.execute("""CREATE TABLE public.zz_ec_sp_live (
            lad24cd text NOT NULL, vintage text NOT NULL, value integer,
            loaded_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (lad24cd))""")
        core.create_schema(cur, SP)
        yield cur
    finally:
        cur.close()
        conn.rollback()


class _Borrowed:
    """A connection stand-in for the run_* commands: hands out the test's
    cursor, never commits, rolls back or closes the real connection."""

    def __init__(self, cur):
        self.cur = cur

    @contextmanager
    def cursor(self):
        yield self.cur

    def commit(self):
        raise AssertionError("a test must never commit")

    def rollback(self):
        pass

    def close(self):
        pass


def live(cur):
    cur.execute("SELECT vintage, lad24cd, value FROM public.zz_ec_sp_live "
                "ORDER BY 1, 2")
    return cur.fetchall()


def editions(cur):
    cur.execute("SELECT vintage, edition, supersedes, COUNT(*) "
                "FROM public.zz_ec_sp_editions GROUP BY 1, 2, 3 ORDER BY 1, 2")
    return cur.fetchall()


def as_live(rows):
    return [(r["vintage"], r["lad24cd"], r["value"]) for r in rows]


def two_vintages(cur):
    """V1 live (edition 1), V2 held (edition 1, not live)."""
    assert pe.apply_period(cur, P, V1, recs(V1), fetched_on=FETCHED) == "new"
    return pe.apply_period(cur, P, V2, recs(V2, 7), fetched_on=FETCHED)


class SpecValidation(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(OFF.live_single_period)
        fields = {f.name: f for f in dataclasses.fields(core.EditionSpec)}
        self.assertIs(fields["live_single_period"].default, False)

    def test_refused_with_refresh_key_changes(self):
        with self.assertRaises(ValueError) as cm:
            dataclasses.replace(SP, refresh_key_changes=True)
        self.assertIn("live_single_period", str(cm.exception))
        self.assertIn("refresh_key_changes", str(cm.exception))


class SingleVintageDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_first_vintage_into_empty_live_inserts_live(self):
        with rolled_back(self.conn) as cur:
            self.assertEqual(
                pe.apply_period(cur, P, V1, recs(V1), fetched_on=FETCHED),
                "new")
            self.assertEqual(live(cur), as_live(recs(V1)))
            self.assertEqual(editions(cur), [(V1, 1, None, N)])
            self.assertTrue(core.status(cur, SP)["ok"])

    def test_second_vintage_stored_as_edition_1_only(self):
        with rolled_back(self.conn) as cur:
            self.assertEqual(two_vintages(cur), pe.HELD_NOT_LIVE)
            self.assertEqual(pe.HELD_NOT_LIVE, "held-not-live")
            self.assertEqual(live(cur), as_live(recs(V1)))
            self.assertEqual(editions(cur), [(V1, 1, None, N),
                                             (V2, 1, None, N)])

    def test_status_lists_held_not_new_not_fault(self):
        with rolled_back(self.conn) as cur:
            two_vintages(cur)
            lm = core.latest_map(cur, SP)
            tips, new, errors = lm
            self.assertEqual((tips, new, errors), ({V1: 1}, [], {}))
            self.assertEqual(lm.held_not_live, [V2])
            st = core.status(cur, SP)
            self.assertEqual(st["held_not_live"], [V2])
            self.assertEqual(st["new_periods"], [])
            self.assertEqual(st["periods"], 1)
            self.assertTrue(st["ok"])
            pst = pe.status(cur, P)
            self.assertEqual(pst["held_not_live"], [V2])
            self.assertEqual(pst["live_missing"], [])
            self.assertEqual(pst["new_periods"], [])
            self.assertTrue(pst["ok"])
            text = pe.format_status(P, pst)
            self.assertIn(f"held, not live: {V2}", text)
            self.assertNotIn("missing from live", text)

    def test_reread_of_held_vintage_is_unchanged_and_stays_out(self):
        with rolled_back(self.conn) as cur:
            two_vintages(cur)
            self.assertEqual(
                pe.compare_period(cur, P, V2, recs(V2, 7))["kind"],
                "unchanged")
            self.assertEqual(
                pe.apply_period(cur, P, V2, recs(V2, 7), fetched_on=FETCHED),
                "unchanged")
            self.assertEqual(live(cur), as_live(recs(V1)))

    def test_revision_of_held_vintage_is_next_edition_not_live(self):
        with rolled_back(self.conn) as cur:
            two_vintages(cur)
            self.assertEqual(
                pe.apply_period(cur, P, V2, recs(V2, 8), fetched_on=FETCHED),
                "revised")
            self.assertEqual(editions(cur), [(V1, 1, None, N),
                                             (V2, 1, None, N),
                                             (V2, 2, 1, N)])
            self.assertEqual(live(cur), as_live(recs(V1)))
            self.assertEqual(core.refresh_latest(cur, SP)["rows"], 0)
            self.assertEqual(live(cur), as_live(recs(V1)))

    def test_sync_new_and_refresh_latest_leave_live_alone(self):
        with rolled_back(self.conn) as cur:
            two_vintages(cur)
            before = core.period_hashes(cur, SP, "live")
            ed_before = core.period_hashes(cur, SP, "editions")
            self.assertEqual(core.sync_new(cur, SP), [])
            res = core.refresh_latest(cur, SP)
            self.assertEqual((res["updated"], res["rows"]), ({}, 0))
            self.assertEqual(core.period_hashes(cur, SP, "live"), before)
            self.assertEqual(core.period_hashes(cur, SP, "editions"),
                             ed_before)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = pe.run_sync_new(
                    P, Namespace(commit=False, simulate=False,
                                 expected_authorities=None),
                    connect=lambda w: _Borrowed(cur),
                    table_exists=lambda c, t: True)
                self.assertEqual(rc, 0)
                rc = pe.run_refresh_latest(
                    P, Namespace(commit=False, simulate=False,
                                 accept_drift=None),
                    connect=lambda w: _Borrowed(cur),
                    table_exists=lambda c, t: True)
                self.assertEqual(rc, 0)
            self.assertIn("months with no editions: none", out.getvalue())
            self.assertIn("rows refresh-latest would write: none",
                          out.getvalue())
            self.assertEqual(core.period_hashes(cur, SP, "live"), before)

    def test_refresh_accept_of_non_live_period_halts_naming_it(self):
        with rolled_back(self.conn) as cur:
            two_vintages(cur)
            with self.assertRaises(SystemExit) as cm:
                core.refresh_latest(cur, SP, accept_drift=(V2,))
            msg = str(cm.exception.code)
            self.assertIn(V2, msg)
            self.assertIn("not the live period", msg)
            self.assertIn(V1, msg)
            with self.assertRaises(SystemExit) as cm:
                with redirect_stdout(io.StringIO()):
                    pe.run_refresh_latest(
                        P, Namespace(commit=False, simulate=True,
                                     accept_drift=[V2]),
                        connect=lambda w: _Borrowed(cur),
                        table_exists=lambda c, t: True)
            msg = str(cm.exception.code)
            self.assertIn(V2, msg)
            self.assertIn("not the live period", msg)
            self.assertEqual(live(cur), as_live(recs(V1)))

    def test_reissue_of_live_vintage_is_edition_2_and_refreshes(self):
        with rolled_back(self.conn) as cur:
            two_vintages(cur)
            self.assertEqual(
                pe.apply_period(cur, P, V1, recs(V1, 11), fetched_on=FETCHED),
                "revised")
            self.assertEqual(editions(cur), [(V1, 1, None, N),
                                             (V1, 2, 1, N),
                                             (V2, 1, None, N)])
            self.assertEqual(live(cur), as_live(recs(V1)))
            st = core.status(cur, SP)
            self.assertEqual(st["pending_refresh"], [V1])
            self.assertEqual(st["held_not_live"], [V2])
            res = core.refresh_latest(cur, SP)
            self.assertEqual(res["updated"], {V1: N})
            self.assertEqual(live(cur), as_live(recs(V1, 11)))
            self.assertEqual(
                load_checks.check_latest_equals_live(cur, SP), [])
            self.assertTrue(core.status(cur, SP)["ok"])

    def test_check_latest_equals_live_compares_live_period_only(self):
        with rolled_back(self.conn) as cur:
            two_vintages(cur)
            self.assertEqual(
                load_checks.check_latest_equals_live(cur, SP, [V1, V2]), [])
            # the same tables under the flag-off spec: V2 is compared (and
            # differs, live holds none of it), as before
            bad = load_checks.check_latest_equals_live(cur, OFF, [V1, V2])
            self.assertEqual(len(bad), 1)
            self.assertIn(V2, bad[0])

    def test_editions_only_with_empty_live_is_live_missing(self):
        with rolled_back(self.conn) as cur:
            core.insert_edition(cur, SP, recs(V1), V1, release_label="t",
                                published_date=FETCHED, source_file="f",
                                source_sha256=core.rows_sha256(SP, recs(V1)),
                                supersedes=None)
            pst = pe.status(cur, P)
            self.assertEqual(pst["live_missing"], [V1])
            self.assertEqual(pst["held_not_live"], [])
            self.assertFalse(pst["ok"])
            self.assertEqual(
                pe.apply_period(cur, P, V1, recs(V1), fetched_on=FETCHED),
                pe.LIVE_MISSING)
            self.assertEqual(live(cur), as_live(recs(V1)))

    def test_two_live_periods_halt(self):
        with rolled_back(self.conn) as cur:
            pe.apply_period(cur, P, V1, recs(V1), fetched_on=FETCHED)
            cur.execute("ALTER TABLE public.zz_ec_sp_live "
                        "DROP CONSTRAINT zz_ec_sp_live_pkey")
            pe.insert_live(cur, P, V2, recs(V2, 7))
            with self.assertRaises(SystemExit) as cm:
                core.status(cur, SP)
            self.assertIn("2 periods", str(cm.exception.code))

    def test_load_periods_reports_held(self):
        with rolled_back(self.conn) as cur:
            pe.apply_period(cur, P, V1, recs(V1), fetched_on=FETCHED)
            out, stats = io.StringIO(), {}
            with redirect_stdout(out):
                rc = pe.load_periods(cur, P, [V2], lambda p: recs(p, 7),
                                     FETCHED, False, simulate=True,
                                     stats=stats)
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn(f"{V2}: held-not-live", text)
            self.assertIn("stored edition 1 only", text)
            self.assertIn("held-not-live 1 month(s)", text)
            self.assertEqual(stats["kinds"], {V2: "held-not-live"})
            self.assertEqual((stats["stored_rows"], stats["live_rows"]),
                             (N, 0))
            out = io.StringIO()
            with redirect_stdout(out):
                pe.load_periods(cur, P, [V2], lambda p: recs(p, 7),
                                FETCHED, False)
            self.assertIn("would store edition 1 only", out.getvalue())
            self.assertEqual(live(cur), as_live(recs(V1)))

    def test_file_check_ledger_accepts_held_outcome(self):
        with rolled_back(self.conn) as cur:
            fc = profile(SP, file_checks=True)
            pe.create_file_checks(cur, fc)
            pe.record_file_check(cur, fc, V2, "f.xlsx", "ab" * 32,
                                 pe.HELD_NOT_LIVE, 1)
            cur.execute("SELECT outcome FROM "
                        "public.zz_ec_sp_editions_file_checks")
            self.assertEqual(cur.fetchall(), [(pe.HELD_NOT_LIVE,)])


# ---------------------------------------------------------------------------
# Flag off: the same SQL and results as the code at the branch base
# ---------------------------------------------------------------------------

def _git_show(path):
    return subprocess.run(
        ["git", "-C", str(SCRIPTS.parent), "show", f"{BASE_COMMIT}:{path}"],
        check=True, capture_output=True, text=True,
        encoding="utf-8").stdout


def _load(name, source, tmp):
    f = Path(tmp) / f"{name}.py"
    f.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, f)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses looks its module up by name
    spec.loader.exec_module(mod)
    return mod


def base_modules():
    """(editions_core, load_checks, period_editions) as at BASE_COMMIT,
    wired to each other."""
    with tempfile.TemporaryDirectory() as tmp:
        bc = _load("zz_base_editions_core",
                   _git_show("scripts/editions_core.py"), tmp)
        bl = _load("zz_base_load_checks",
                   _git_show("scripts/load_checks.py"), tmp)
        bp = _load("zz_base_period_editions",
                   _git_show("scripts/period_editions.py"), tmp)
    bl.chain_tip = bc.chain_tip
    bp.core, bp.load_checks, bp.halt = bc, bl, bc.halt
    return bc, bl, bp


class _Recorder:
    """A cursor proxy that records every statement as text."""

    def __init__(self, cur):
        self._cur = cur
        self.sql = []

    def execute(self, sql, params=None):
        text = self._cur.mogrify(sql, params) if params is not None else sql
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        self.sql.append(text)
        return self._cur.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cur, name)


def scenario(c, lc, m, cur):
    """One flag-off run through every touched path; (results, stdout)."""
    out = io.StringIO()
    res = []
    with redirect_stdout(out):
        c.create_schema(cur, OFF)
        res.append(m.apply_period(cur, P_OFF, V1, recs(V1),
                                  fetched_on=FETCHED))
        res.append(m.apply_period(cur, P_OFF, V2, recs(V2, 7),
                                  fetched_on=FETCHED))
        res.append(m.compare_period(cur, P_OFF, V2, recs(V2, 7)))
        res.append(m.apply_period(cur, P_OFF, V1, recs(V1, 11),
                                  fetched_on=FETCHED))
        res.append(tuple(c.latest_map(cur, OFF)))
        res.append(c.status(cur, OFF))
        res.append(m.status(cur, P_OFF))
        res.append(m.format_status(P_OFF, res[-1]))
        res.append(c.refresh_latest(cur, OFF))
        res.append(lc.check_latest_equals_live(cur, OFF))
        res.append(lc.check_latest_equals_live(cur, OFF, [V1, V2]))
        cur.execute("INSERT INTO public.zz_ec_sp_live (lad24cd, vintage, "
                    "value) SELECT lad24cd, '2041', value "
                    "FROM public.zz_ec_sp_live WHERE vintage = %s", (V1,))
        res.append(c.sync_new(cur, OFF))
        res.append(m.load_periods(cur, P_OFF, ["2051", V1],
                                  lambda p: recs(p, 3), FETCHED, False,
                                  simulate=True, stats={}))
        try:
            c.refresh_latest(cur, OFF, accept_drift=(V2,))
        except SystemExit as e:
            res.append(str(e.code))
        res.append(c.status(cur, OFF))
        m.create_file_checks(cur, profile(OFF, file_checks=True))
        res.append(live(cur))
        res.append(editions(cur))
    return res, out.getvalue()


class FlagOffUnchanged(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()
        cls.base = base_modules()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def _run(self, mods):
        with rolled_back(self.conn) as cur:
            # rolled_back made the editions table with SP; drop it so the
            # scenario's own create_schema makes it, recorded
            cur.execute("DROP TABLE public.zz_ec_sp_editions")
            # a flag-off live table holds many periods
            cur.execute("ALTER TABLE public.zz_ec_sp_live "
                        "DROP CONSTRAINT zz_ec_sp_live_pkey")
            rec = _Recorder(cur)
            res, out = scenario(*mods, rec)
            return res, out, rec.sql

    def test_same_sql_and_results_as_base(self):
        base_res, base_out, base_sql = self._run(self.base)
        new_res, new_out, new_sql = self._run((core, load_checks, pe))
        self.assertGreater(len(base_sql), 50)
        self.assertEqual(new_res, base_res)
        self.assertEqual(new_out, base_out)
        self.assertEqual(len(new_sql), len(base_sql))
        for i, (a, b) in enumerate(zip(base_sql, new_sql)):
            self.assertEqual(b, a, f"statement {i} differs")


if __name__ == "__main__":
    unittest.main()
