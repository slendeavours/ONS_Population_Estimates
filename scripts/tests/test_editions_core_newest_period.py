"""Tests for the newest-period-only opt-in, EditionSpec.live_newest_period_only
(S24's judgements and notices: every held file is a period, live holds the
newest period only, every older period is history in the editions table).

With the flag on, a new period newer than the live one replaces it (edition
1, delete of the older live rows and insert of the new ones, in one
savepoint); a new period older than the live one is stored as editions only
('held-not-live'); periods other than the live one are 'history' in status
(information, not a fault); refresh-latest refreshes the live period only and
halts when another period is named; two live periods halt.

With the flag off nothing changes: the flag-off scenario of
test_editions_core_single_period is re-run against its committed fixture with
every newest-period path spied on, and the single-vintage (live_single_period)
scenario is compared, statement for statement, with a fixture recorded from
the engine before this opt-in existed (tests/fixtures/
editions_core_single_on_scenario.json).

Every database test runs on a connection from _db.get_conn() inside a
transaction that is always rolled back. The only tables touched are the
throwaway zz_np_live, zz_np_editions and zz_np_editions_file_checks (and the
zz_ec_sp_* tables of the single-vintage scenarios), created inside that
transaction, so they never persist. Nothing here commits.
"""
import dataclasses
import io
import json
import sys
import unittest
from argparse import Namespace
from contextlib import contextmanager, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
import test_editions_core_single_period as sp  # noqa: E402
from _db import get_conn  # noqa: E402

FETCHED = date(2026, 10, 11)
CODES = ("E06000001", "E06000002", "E06000003")
N = len(CODES)
A, B, C = "2026-08-12", "2026-09-30", "2026-10-31"

NP = core.EditionSpec(
    name="zz_np",
    live_table="zz_np_live",
    editions_table="zz_np_editions",
    key_cols=("lad24cd",),
    period_col="period",
    value_cols=(("value", "integer"),),
    refresh_cols=("value",),
    fk_la_boundaries=False,
    live_newest_period_only=True,
)
NPK = dataclasses.replace(NP, refresh_key_changes=True)
OFF = dataclasses.replace(NP, live_newest_period_only=False)


def profile(spec, file_checks=False, expected_areas=N):
    return pe.Profile(
        spec=spec, value_cols=("value",), run_agent="Source ZZ - test",
        run_source="99", heading="ZZ newest-period test",
        default_source_file="zz source file", expected_areas=expected_areas,
        release_label=lambda d: f"zz fetch {d.isoformat()}",
        savepoint="zz_np_period", file_checks=file_checks)


P = profile(NP)
PK = profile(NPK, expected_areas=None)


def recs(period, base=10, codes=CODES):
    return [{"lad24cd": c, "period": period, "value": base * (i + 1)}
            for i, c in enumerate(codes)]


@contextmanager
def rolled_back(conn):
    """A cursor in a transaction that is rolled back whatever happens, with
    the throwaway live and editions tables made inside it."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_np_live'), "
                    "to_regclass('public.zz_np_editions'), "
                    "to_regclass('public.zz_np_editions_file_checks')")
        if cur.fetchone() != (None, None, None):
            raise RuntimeError("a zz_np table already exists as a real "
                               "table; refusing to run")
        cur.execute("""CREATE TABLE public.zz_np_live (
            lad24cd text NOT NULL, period text NOT NULL, value integer,
            loaded_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (lad24cd, period))""")
        core.create_schema(cur, NP)
        yield cur
    finally:
        cur.close()
        conn.rollback()


def live(cur):
    cur.execute("SELECT period, lad24cd, value FROM public.zz_np_live "
                "ORDER BY 1, 2")
    return cur.fetchall()


def editions(cur):
    cur.execute("SELECT period, edition, supersedes, COUNT(*) "
                "FROM public.zz_np_editions GROUP BY 1, 2, 3 ORDER BY 1, 2")
    return cur.fetchall()


def as_live(rows):
    return [(r["period"], r["lad24cd"], r["value"]) for r in rows]


def apply(cur, period, rows, prof=P, **kw):
    return pe.apply_period(cur, prof, period, rows, fetched_on=FETCHED, **kw)


def borrowed(cur):
    return dict(connect=lambda w: sp._Borrowed(cur),
                table_exists=lambda c, t: True)


class SpecValidation(unittest.TestCase):
    def test_default_off(self):
        fields = {f.name: f for f in dataclasses.fields(core.EditionSpec)}
        self.assertIs(fields["live_newest_period_only"].default, False)
        self.assertFalse(OFF.live_newest_period_only)
        self.assertFalse(sp.SP.live_newest_period_only)

    def test_both_opt_ins_together_raise_naming_both(self):
        with self.assertRaises(ValueError) as cm:
            dataclasses.replace(NP, live_single_period=True)
        self.assertIn("live_newest_period_only", str(cm.exception))
        self.assertIn("live_single_period", str(cm.exception))

    def test_allowed_with_refresh_key_changes(self):
        self.assertTrue(NPK.refresh_key_changes)
        self.assertTrue(NPK.live_newest_period_only)


class NewestPeriodDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_first_period_into_empty_live_inserts_as_usual(self):
        with rolled_back(self.conn) as cur:
            self.assertEqual(apply(cur, A, recs(A)), "new")
            self.assertEqual(live(cur), as_live(recs(A)))
            self.assertEqual(editions(cur), [(A, 1, None, N)])
            st = core.status(cur, NP)
            self.assertTrue(st["ok"])
            self.assertEqual(st["history"], [])

    def test_newer_period_replaces_live(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            self.assertEqual(pe.compare_period(cur, P, B, recs(B, 7))["kind"],
                             "new")
            self.assertEqual(apply(cur, B, recs(B, 7)), "new")
            self.assertEqual(live(cur), as_live(recs(B, 7)))
            self.assertEqual(editions(cur), [(A, 1, None, N),
                                             (B, 1, None, N)])
            st = core.status(cur, NP)
            self.assertTrue(st["ok"])
            self.assertEqual((st["periods"], st["history"]), (1, [A]))
            self.assertEqual(load_checks.check_latest_equals_live(cur, NP),
                             [])

    def test_failing_insert_leaves_the_old_live_rows(self):
        def bad_insert(cur, profile, period, records):
            pe.insert_live(cur, profile, period,
                           [dict(r, value=r["value"] + 1) for r in records])

        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            with self.assertRaises(SystemExit) as cm:
                apply(cur, B, recs(B, 7), insert=bad_insert)
            self.assertIn("differ from edition", str(cm.exception.code))
            # the delete of A and the insert of B went together
            self.assertEqual(live(cur), as_live(recs(A)))
            # in a load the period's savepoint also drops B's edition
            out = io.StringIO()
            with redirect_stdout(out):
                rc = pe.load_periods(
                    cur, P, [C], lambda p: recs(p, 5), FETCHED, False,
                    simulate=True,
                    apply=lambda c, pr, p, r, **kw: pe.apply_period(
                        c, pr, p, r, insert=bad_insert, **kw))
            self.assertEqual(rc, 1)
            self.assertIn("FAILED", out.getvalue())
            self.assertEqual(live(cur), as_live(recs(A)))
            self.assertNotIn(C, [e[0] for e in editions(cur)])

    def test_older_period_is_history_and_live_untouched(self):
        with rolled_back(self.conn) as cur:
            apply(cur, B, recs(B, 7))
            self.assertEqual(pe.compare_period(cur, P, A, recs(A))["kind"],
                             pe.HELD_NOT_LIVE)
            self.assertEqual(apply(cur, A, recs(A)), pe.HELD_NOT_LIVE)
            self.assertEqual(live(cur), as_live(recs(B, 7)))
            self.assertEqual(editions(cur), [(A, 1, None, N),
                                             (B, 1, None, N)])
            # a re-read is unchanged; a revision is the next edition, not live
            self.assertEqual(apply(cur, A, recs(A)), "unchanged")
            self.assertEqual(apply(cur, A, recs(A, 8)), "revised")
            self.assertEqual(live(cur), as_live(recs(B, 7)))
            self.assertEqual(core.refresh_latest(cur, NP)["rows"], 0)
            self.assertEqual(live(cur), as_live(recs(B, 7)))

    def test_status_lists_history_not_new_not_fault(self):
        with rolled_back(self.conn) as cur:
            apply(cur, B, recs(B, 7))
            apply(cur, A, recs(A))
            lm = core.latest_map(cur, NP)
            self.assertEqual(tuple(lm), ({B: 1}, [], {}))
            self.assertEqual(lm.history, [A])
            self.assertEqual(lm.held_not_live, [])
            st = core.status(cur, NP)
            self.assertEqual(st["history"], [A])
            self.assertNotIn("held_not_live", st)
            self.assertEqual(st["new_periods"], [])
            self.assertTrue(st["ok"])
            pst = pe.status(cur, P)
            self.assertEqual(pst["history"], [A])
            self.assertEqual(pst["live_missing"], [])
            self.assertTrue(pst["ok"])
            text = pe.format_status(P, pst)
            self.assertIn(f"history: {A}", text)
            self.assertNotIn("missing from live", text)
            self.assertEqual(core.sync_new(cur, NP), [])

    def test_refresh_of_the_live_period_works(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            apply(cur, B, recs(B, 7))
            self.assertEqual(apply(cur, B, recs(B, 9)), "revised")
            self.assertEqual(core.status(cur, NP)["pending_refresh"], [B])
            res = core.refresh_latest(cur, NP)
            self.assertEqual(res["updated"], {B: N})
            self.assertEqual(live(cur), as_live(recs(B, 9)))
            self.assertEqual(load_checks.check_latest_equals_live(cur, NP),
                             [])
            st = core.status(cur, NP)
            self.assertTrue(st["ok"])
            self.assertEqual(st["history"], [A])

    def test_refresh_naming_a_history_period_halts(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            apply(cur, B, recs(B, 7))
            for kw in ({"accept_drift": (A,)}, {"accept_key_changes": (A,)}):
                with self.assertRaises(SystemExit) as cm:
                    core.refresh_latest(cur, NPK, **kw)
                msg = str(cm.exception.code)
                self.assertIn(A, msg)
                self.assertIn("not the live period", msg)
                self.assertIn(B, msg)
            with self.assertRaises(SystemExit) as cm:
                with redirect_stdout(io.StringIO()):
                    pe.run_refresh_latest(
                        P, Namespace(commit=False, simulate=True,
                                     accept_drift=[A]), **borrowed(cur))
            self.assertIn("not the live period", str(cm.exception.code))
            self.assertEqual(live(cur), as_live(recs(B, 7)))

    def test_key_changes_of_the_live_period_follow_the_opt_in(self):
        four = CODES + ("E06000004",)
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A), prof=PK)
            apply(cur, B, recs(B, 7), prof=PK)
            self.assertEqual(apply(cur, B, recs(B, 7, four), prof=PK),
                             "revised")
            with self.assertRaises(SystemExit) as cm:
                core.refresh_latest(cur, NPK)
            self.assertIn("--accept-key-changes", str(cm.exception.code))
            with self.assertRaises(SystemExit):
                core.refresh_latest(cur, NP)    # not opted in: halts as usual
            res = core.refresh_latest(cur, NPK, accept_key_changes=(B,))
            self.assertEqual(res["inserted"], {B: 1})
            self.assertEqual(live(cur), as_live(recs(B, 7, four)))
            self.assertEqual(core.status(cur, NPK)["history"], [A])

    def test_two_live_periods_halt(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            pe.insert_live(cur, P, B, recs(B, 7))
            for f in (core.latest_map, core.status):
                with self.assertRaises(SystemExit) as cm:
                    f(cur, NP)
                self.assertIn("2 periods", str(cm.exception.code))
            with self.assertRaises(SystemExit):
                core.refresh_latest(cur, NP)
            with self.assertRaises(SystemExit):
                apply(cur, C, recs(C, 5))
            self.assertEqual(live(cur), as_live(recs(A)) + as_live(recs(B, 7)))

    def test_newer_period_halts_when_live_rows_are_unrecorded(self):
        with rolled_back(self.conn) as cur:
            # live holds a period with no editions: deleting it would lose it
            pe.insert_live(cur, P, A, recs(A))
            with self.assertRaises(SystemExit) as cm:
                apply(cur, B, recs(B, 7))
            self.assertIn("no editions", str(cm.exception.code))
            self.assertEqual(live(cur), as_live(recs(A)))
        with rolled_back(self.conn) as cur:
            # live drifted from every stored edition: same
            apply(cur, A, recs(A))
            cur.execute("UPDATE public.zz_np_live SET value = 99 "
                        "WHERE lad24cd = %s", (CODES[0],))
            with self.assertRaises(SystemExit) as cm:
                apply(cur, B, recs(B, 7))
            self.assertIn("drift", str(cm.exception.code))
            self.assertEqual(live(cur)[0], (A, CODES[0], 99))
            self.assertEqual(len(live(cur)), N)

    def test_editions_only_period_with_other_live_period(self):
        with rolled_back(self.conn) as cur:
            apply(cur, B, recs(B, 7))
            for p in (A, C):
                core.insert_edition(cur, NP, recs(p, 3), p, release_label="t",
                                    published_date=FETCHED, source_file="f",
                                    source_sha256=core.rows_sha256(
                                        NP, recs(p, 3)),
                                    supersedes=None)
            # older: history, unchanged on a re-read
            self.assertEqual(apply(cur, A, recs(A, 3)), "unchanged")
            # newer with no live rows: never a second live period
            self.assertEqual(pe.compare_period(cur, P, C, recs(C, 3))["kind"],
                             pe.LIVE_MISSING)
            with self.assertRaises(SystemExit) as cm:
                apply(cur, C, recs(C, 3))
            self.assertIn("never takes a second period", str(cm.exception.code))
            self.assertEqual(live(cur), as_live(recs(B, 7)))

    def test_editions_only_with_empty_live_is_live_missing(self):
        with rolled_back(self.conn) as cur:
            core.insert_edition(cur, NP, recs(A), A, release_label="t",
                                published_date=FETCHED, source_file="f",
                                source_sha256=core.rows_sha256(NP, recs(A)),
                                supersedes=None)
            pst = pe.status(cur, P)
            self.assertEqual(pst["live_missing"], [A])
            self.assertEqual(pst["history"], [])
            self.assertFalse(pst["ok"])
            self.assertEqual(apply(cur, A, recs(A)), pe.LIVE_MISSING)
            self.assertEqual(live(cur), as_live(recs(A)))

    def test_check_latest_equals_live_compares_the_live_period_only(self):
        with rolled_back(self.conn) as cur:
            apply(cur, B, recs(B, 7))
            apply(cur, A, recs(A))
            self.assertEqual(
                load_checks.check_latest_equals_live(cur, NP, [A, B]), [])
            bad = load_checks.check_latest_equals_live(cur, OFF, [A, B])
            self.assertEqual(len(bad), 1)
            self.assertIn(A, bad[0])

    def test_load_periods_reports_new_and_history(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            out, stats = io.StringIO(), {}
            with redirect_stdout(out):
                rc = pe.load_periods(cur, P, [B], lambda p: recs(p, 7),
                                     FETCHED, False, simulate=True,
                                     stats=stats)
            self.assertEqual(rc, 0)
            self.assertIn(f"{B}: new", out.getvalue())
            self.assertEqual(stats["live_rows"], N)
            apply(cur, B, recs(B, 7))
            out, stats = io.StringIO(), {}
            with redirect_stdout(out):
                rc = pe.load_periods(cur, P, ["2026-07-01"],
                                     lambda p: recs(p, 2), FETCHED, False,
                                     simulate=True, stats=stats)
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("2026-07-01: held-not-live", text)
            self.assertIn("live holds a newer period", text)
            self.assertIn("held-not-live 1 month(s)", text)
            self.assertEqual((stats["stored_rows"], stats["live_rows"]),
                             (N, 0))
            self.assertEqual(live(cur), as_live(recs(B, 7)))

    def test_file_check_ledger_accepts_held_outcome(self):
        with rolled_back(self.conn) as cur:
            fc = profile(NP, file_checks=True)
            pe.create_file_checks(cur, fc)
            pe.record_file_check(cur, fc, A, "f.xlsx", "ab" * 32,
                                 pe.HELD_NOT_LIVE, 1)
            cur.execute("SELECT outcome FROM "
                        "public.zz_np_editions_file_checks")
            self.assertEqual(cur.fetchall(), [(pe.HELD_NOT_LIVE,)])


# ---------------------------------------------------------------------------
# Flag off: the same SQL and results as the engine before this opt-in
# ---------------------------------------------------------------------------

SINGLE_ON_GOLDEN = TESTS / "fixtures" / "editions_core_single_on_scenario.json"


def single_on_scenario(cur):
    """One live_single_period run through every path this opt-in touches;
    (results, stdout)."""
    out = io.StringIO()
    res = []
    S, Ps = sp.SP, sp.P
    with redirect_stdout(out):
        res.append(pe.apply_period(cur, Ps, sp.V1, sp.recs(sp.V1),
                                   fetched_on=sp.FETCHED))
        res.append(pe.apply_period(cur, Ps, sp.V2, sp.recs(sp.V2, 7),
                                   fetched_on=sp.FETCHED))
        res.append(pe.compare_period(cur, Ps, sp.V2, sp.recs(sp.V2, 7)))
        res.append(pe.compare_period(cur, Ps, "2051", sp.recs("2051", 3)))
        res.append(pe.apply_period(cur, Ps, sp.V1, sp.recs(sp.V1, 11),
                                   fetched_on=sp.FETCHED))
        lm = core.latest_map(cur, S)
        res.append(tuple(lm))
        res.append(lm.held_not_live)
        res.append(core.status(cur, S))
        res.append(pe.status(cur, Ps))
        res.append(pe.format_status(Ps, res[-1]))
        res.append(core.refresh_latest(cur, S))
        res.append(load_checks.check_latest_equals_live(cur, S))
        res.append(load_checks.check_latest_equals_live(cur, S,
                                                        [sp.V1, sp.V2]))
        res.append(core.sync_new(cur, S))
        res.append(pe.load_periods(cur, Ps, ["2051", sp.V1],
                                   lambda p: sp.recs(p, 3), sp.FETCHED,
                                   False, simulate=True, stats={}))
        for kw in ({"accept_drift": (sp.V2,)},
                   {"accept_key_changes": (sp.V2,)}):
            try:
                core.refresh_latest(cur, S, **kw)
            except SystemExit as e:
                res.append(str(e.code))
        pe.create_file_checks(cur, sp.profile(S, file_checks=True))
        res.append(sp.live(cur))
        res.append(sp.editions(cur))
    return res, out.getvalue()


def run_single_on(conn, with_day=False):
    with sp.rolled_back(conn) as cur:
        day = sp.run_day(cur)
        rec = sp._Recorder(cur)
        res, out = single_on_scenario(rec)
        return (res, out, rec.sql, day) if with_day else (res, out, rec.sql)


@contextmanager
def newest_paths_refused():
    """Spies: no newest-period path may do any work. live_other_periods
    must answer [] (it does so without a query for any other spec), and the
    replacement of live never runs."""
    answers = []
    real = pe.live_other_periods

    def other(cur, profile, period):
        got = real(cur, profile, period)
        answers.append(got)
        return got

    def refuse(*a, **k):
        raise AssertionError("_replace_older_live ran for a spec without "
                             "live_newest_period_only")

    with mock.patch.object(pe, "live_other_periods", side_effect=other), \
            mock.patch.object(pe, "_replace_older_live", side_effect=refuse):
        yield answers


class FlagOffUnchanged(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def assert_matches(self, golden_path, run):
        """run: (results, stdout, sql, run day); compared with the golden
        file, the run day normalised on both sides (sp.golden_matches)."""
        res, out, sql, day = run
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        now = sp.recorded(res, out, sql)
        sp.golden_matches(self, golden, now, day)
        return now

    def test_flag_off_scenario_unchanged_and_no_newest_path_runs(self):
        with newest_paths_refused() as answers:
            run = sp.run_scenario(self.conn, with_day=True)
        self.assertTrue(answers)
        self.assertFalse(any(answers))
        now = self.assert_matches(sp.GOLDEN, run)
        for r in now["results"]:
            self.assertNotIn("'history'", r)

    def test_single_vintage_scenario_unchanged_and_no_newest_path_runs(self):
        with newest_paths_refused() as answers:
            run = run_single_on(self.conn, with_day=True)
        self.assertTrue(answers)
        self.assertFalse(any(answers))
        now = self.assert_matches(SINGLE_ON_GOLDEN, run)
        for r in now["results"]:
            self.assertNotIn("'history'", r)


def write_golden():
    """Record the single-vintage fixture from the current modules.
    Deliberate only: read the diff before committing it."""
    about = ("The live_single_period scenario of "
             "test_editions_core_newest_period (single_on_scenario()), run "
             "through editions_core, load_checks and period_editions as at "
             "commit afa5707 with only the live_newest_period_only field and "
             "its __post_init__ check added (no behaviour of the opt-in yet) "
             "on 2026-10-11: every result as repr, the printed output and every "
             "SQL statement, with hex runs of 40 or more characters cut into "
             "32-character pieces joined by '~'. A live_single_period spec "
             "must reproduce it exactly. Regenerate only deliberately, after "
             "reading the diff: python tests/test_editions_core_newest_period"
             ".py --write-golden")
    if SINGLE_ON_GOLDEN.exists():
        about = json.loads(SINGLE_ON_GOLDEN.read_text(
            encoding="utf-8"))["about"]
    conn = get_conn()
    try:
        res, out, sql, day = run_single_on(conn, with_day=True)
        doc = {"about": about, "recorded_on": day.isoformat()}
        doc.update(sp.recorded(res, out, sql))
    finally:
        conn.rollback()
        conn.close()
    with open(SINGLE_ON_GOLDEN, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {SINGLE_ON_GOLDEN}")


if __name__ == "__main__":
    if "--write-golden" in sys.argv:
        write_golden()
    else:
        unittest.main()
