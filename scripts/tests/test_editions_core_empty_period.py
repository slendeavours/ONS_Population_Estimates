"""Tests for the empty-period opt-in, EditionSpec.empty_periods_allowed (S24's
enforcement notices: a judgements issue whose notices sheet holds no notice,
Scott's Option C of 2026-10-12, decisions README item 8).

An opted-in spec (it must also be live_newest_period_only and
refresh_key_changes) may record a period as EMPTY: one row in its file-check
ledger with outcome 'empty', no edition (NULL), the file's source and sha256
and the name of the acknowledgement that allowed it, and no editions rows.
The newest period wins: an empty period newer than every editions period is
the latest state, so status says live is pending until refresh-latest
--accept-key-changes <that period> deletes the live rows of the older period
(and inserts none); the older period's editions stay as history. A later
period with rows replaces the empty state normally. Every other spec is
unchanged; the committed golden fixtures of test_editions_core_single_period
and test_editions_core_newest_period prove that statement for statement.

Every database test runs on a connection from _db.get_conn() inside a
transaction that is always rolled back. The only tables touched are the
throwaway zz_ep_live, zz_ep_editions and zz_ep_editions_file_checks, created
inside that transaction, so they never persist. Nothing here commits.
"""
import dataclasses
import io
import sys
import unittest
from argparse import Namespace
from contextlib import contextmanager, redirect_stdout
from datetime import date
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
from _db import get_conn  # noqa: E402

FETCHED = date(2026, 10, 12)
CODES = ("E06000001", "E06000002", "E06000003")
N = len(CODES)
A, B, C = "2026-08-12", "2026-09-30", "2026-10-31"
SHA = "ab" * 32
ACK = "--acknowledge-no-notices 2026-09-30"

EP = core.EditionSpec(
    name="zz_ep",
    live_table="zz_ep_live",
    editions_table="zz_ep_editions",
    key_cols=("lad24cd",),
    period_col="period",
    value_cols=(("value", "integer"),),
    refresh_cols=("value",),
    fk_la_boundaries=False,
    live_newest_period_only=True,
    refresh_key_changes=True,
    empty_periods_allowed=True,
)
NP = dataclasses.replace(EP, empty_periods_allowed=False)


def profile(spec):
    return pe.Profile(
        spec=spec, value_cols=("value",), run_agent="Source ZZ - test",
        run_source="99", heading="ZZ empty-period test",
        default_source_file="zz source file", expected_areas=None,
        release_label=lambda d: f"zz fetch {d.isoformat()}",
        savepoint="zz_ep_period", file_checks=True)


P = profile(EP)
P_NP = profile(NP)


def recs(period, base=10, codes=CODES):
    return [{"lad24cd": c, "period": period, "value": base * (i + 1)}
            for i, c in enumerate(codes)]


@contextmanager
def rolled_back(conn, *, ledger=True):
    """A cursor in a transaction that is rolled back whatever happens, with
    the throwaway live, editions and ledger tables made inside it."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_ep_live'), "
                    "to_regclass('public.zz_ep_editions'), "
                    "to_regclass('public.zz_ep_editions_file_checks')")
        if cur.fetchone() != (None, None, None):
            raise RuntimeError("a zz_ep table already exists as a real "
                               "table; refusing to run")
        cur.execute("""CREATE TABLE public.zz_ep_live (
            lad24cd text NOT NULL, period text NOT NULL, value integer,
            loaded_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (lad24cd, period))""")
        core.create_schema(cur, EP)
        if ledger:
            pe.create_file_checks(cur, P)
        yield cur
    finally:
        cur.close()
        conn.rollback()


class _Borrowed:
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


def borrowed(cur):
    return dict(connect=lambda w: _Borrowed(cur),
                table_exists=lambda c, t: True)


def live(cur):
    cur.execute("SELECT period, lad24cd, value FROM public.zz_ep_live "
                "ORDER BY 1, 2")
    return cur.fetchall()


def editions(cur):
    cur.execute("SELECT period, edition, supersedes, COUNT(*) "
                "FROM public.zz_ep_editions GROUP BY 1, 2, 3 ORDER BY 1, 2")
    return cur.fetchall()


def ledger(cur):
    cur.execute("SELECT period, outcome, edition, acknowledgement, "
                "file_sha256 FROM public.zz_ep_editions_file_checks "
                "ORDER BY id")
    return cur.fetchall()


def as_live(rows):
    return [(r["period"], r["lad24cd"], r["value"]) for r in rows]


def apply(cur, period, rows, prof=P):
    return pe.apply_period(cur, prof, period, rows, fetched_on=FETCHED)


def empty(cur, period, prof=P, ack=ACK):
    return pe.record_empty_period(cur, prof, period, f"f-{period}.xlsx",
                                  SHA, ack)


class SpecValidation(unittest.TestCase):
    def test_default_off_and_existing_specs_untouched(self):
        fields = {f.name: f for f in dataclasses.fields(core.EditionSpec)}
        self.assertIs(fields["empty_periods_allowed"].default, False)
        self.assertFalse(NP.empty_periods_allowed)

    def test_needs_newest_period_only_and_key_changes(self):
        for over in ({"live_newest_period_only": False},
                     {"refresh_key_changes": False}):
            with self.subTest(**over):
                with self.assertRaises(ValueError) as cm:
                    dataclasses.replace(EP, **over)
                self.assertIn("empty_periods_allowed", str(cm.exception))

    def test_ledger_name(self):
        self.assertEqual(core.file_checks_table(EP),
                         "zz_ep_editions_file_checks")
        self.assertEqual(core.file_checks_table(EP), pe.file_checks_table(P))


class Ledger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def insert(self, cur, outcome, edition, ack):
        cur.execute("SAVEPOINT t")
        try:
            pe.record_file_check(cur, P, B, "f.xlsx", SHA, outcome, edition,
                                 acknowledgement=ack)
            cur.execute("RELEASE SAVEPOINT t")
            return None
        except Exception as e:  # noqa: BLE001 (the check violation)
            cur.execute("ROLLBACK TO SAVEPOINT t")
            return type(e).__name__

    def test_empty_needs_an_acknowledgement_and_no_edition(self):
        with rolled_back(self.conn) as cur:
            self.assertTrue(pe.file_checks_accept_empty(cur, P))
            self.assertIsNone(self.insert(cur, core.EMPTY, None, ACK))
            self.assertIsNotNone(self.insert(cur, core.EMPTY, None, None))
            self.assertIsNotNone(self.insert(cur, core.EMPTY, 1, ACK))
            self.assertIsNone(self.insert(cur, "new", 1, None))
            self.assertEqual([r[1] for r in ledger(cur)], ["empty", "new"])

    def test_an_old_ledger_is_upgraded_in_place_and_idempotently(self):
        with rolled_back(self.conn, ledger=False) as cur:
            pe.create_file_checks(cur, P_NP)          # the ledger as it was
            pe.record_file_check(cur, P_NP, A, "f.xlsx", SHA, "new", 1)
            self.assertFalse(pe.file_checks_accept_empty(cur, P))
            pe.create_file_checks(cur, P)
            pe.create_file_checks(cur, P)            # a second ddl: no-op
            self.assertTrue(pe.file_checks_accept_empty(cur, P))
            self.assertEqual(ledger(cur), [(A, "new", 1, None, SHA)])
            self.assertIsNone(self.insert(cur, core.EMPTY, None, ACK))
            # the upgraded ledger is still append-only
            cur.execute("SAVEPOINT u")
            with self.assertRaises(Exception) as cm:
                cur.execute("UPDATE public.zz_ep_editions_file_checks SET "
                            "outcome = outcome")
            cur.execute("ROLLBACK TO SAVEPOINT u")
            self.assertIn("append-only", str(cm.exception))

    def test_a_ledger_without_the_opt_in_refuses_empty(self):
        with rolled_back(self.conn, ledger=False) as cur:
            pe.create_file_checks(cur, P_NP)
            cur.execute("SAVEPOINT t")
            with self.assertRaises(Exception):
                cur.execute("INSERT INTO public.zz_ep_editions_file_checks "
                            "(period, source_file, file_sha256, outcome) "
                            "VALUES ('x', 'f', 's', 'empty')")
            cur.execute("ROLLBACK TO SAVEPOINT t")
            with self.assertRaises(ValueError):
                empty(cur, B, prof=P_NP)


class EmptyPeriodDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_record_empty_writes_one_ledger_row_and_nothing_else(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            ed = core.period_hashes(cur, EP, "editions")
            self.assertEqual(empty(cur, B), core.EMPTY)
            self.assertEqual(ledger(cur), [(B, "empty", None, ACK, SHA)])
            self.assertEqual(core.period_hashes(cur, EP, "editions"), ed)
            self.assertEqual(live(cur), as_live(recs(A)))   # until refresh
            self.assertEqual(core.empty_periods(cur, EP), [B])

    def test_the_newest_empty_period_is_pending_until_refresh(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            empty(cur, B)
            lm = core.latest_map(cur, EP)
            self.assertEqual(tuple(lm), ({A: 1}, [], {}))
            self.assertEqual((lm.empty, lm.empty_pending), ([B], B))
            st = core.status(cur, EP)
            self.assertFalse(st["ok"])
            self.assertEqual((st["empty"], st["empty_latest"],
                              st["empty_pending"]), ([B], B, [B]))
            pst = pe.status(cur, P)
            self.assertFalse(pst["ok"])
            self.assertEqual(pst["empty_pending"], [B])
            text = pe.format_status(P, pst)
            self.assertIn(f"--accept-key-changes {B}", text)
            self.assertIn("ACTION NEEDED", text)

    def test_refresh_needs_the_empty_period_named_then_empties_live(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            empty(cur, B)
            ed = core.period_hashes(cur, EP, "editions")
            with self.assertRaises(SystemExit) as cm:
                core.refresh_latest(cur, EP)
            self.assertIn(f"--accept-key-changes {B}", str(cm.exception.code))
            self.assertEqual(live(cur), as_live(recs(A)))
            with self.assertRaises(SystemExit):          # the live period
                core.refresh_latest(cur, EP, accept_key_changes=(A,))
            self.assertEqual(live(cur), as_live(recs(A)))
            res = core.refresh_latest(cur, EP, accept_key_changes=(B,))
            self.assertEqual((res["emptied"], res["empty"]), ({A: N}, B))
            self.assertEqual(res["rows"], 0)
            self.assertEqual(live(cur), [])
            self.assertEqual(core.period_hashes(cur, EP, "editions"), ed)
            self.assertEqual(editions(cur), [(A, 1, None, N)])
            st = core.status(cur, EP)
            self.assertTrue(st["ok"], st)
            self.assertEqual((st["history"], st["empty_latest"],
                              st["empty_pending"]), ([A], B, []))
            pst = pe.status(cur, P)
            self.assertTrue(pst["ok"], pst)
            self.assertEqual((pst["history"], pst["live_missing"],
                              pst["empty"]), ([A], [], [B]))
            self.assertIn(f"empty: {B}", pe.format_status(P, pst))
            self.assertEqual(load_checks.check_latest_equals_live(cur, EP),
                             [])
            # nothing left to empty: naming it again halts, plain is a no-op
            with self.assertRaises(SystemExit) as cm:
                core.refresh_latest(cur, EP, accept_key_changes=(B,))
            self.assertIn("nothing to empty", str(cm.exception.code))
            self.assertEqual(core.refresh_latest(cur, EP)["rows"], 0)

    def test_a_halt_after_the_live_delete_leaves_live_untouched(self):
        from unittest import mock
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            empty(cur, B)
            with mock.patch.object(core, "_plan",
                                   side_effect=SystemExit("HALT: later")):
                with self.assertRaises(SystemExit):
                    core.refresh_latest(cur, EP, accept_key_changes=(B,))
            self.assertEqual(live(cur), as_live(recs(A)))
            cur.execute("SELECT 1")           # the transaction is still usable

    def test_a_drifted_live_period_is_not_emptied(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            empty(cur, B)
            cur.execute("UPDATE public.zz_ep_live SET value = 99 WHERE "
                        "lad24cd = %s", (CODES[0],))
            with self.assertRaises(SystemExit) as cm:
                core.refresh_latest(cur, EP, accept_key_changes=(B,))
            self.assertIn("drift", str(cm.exception.code))
            self.assertEqual(len(live(cur)), N)

    def test_a_later_period_with_rows_restores_live(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            empty(cur, B)
            core.refresh_latest(cur, EP, accept_key_changes=(B,))
            self.assertEqual(pe.compare_period(cur, P, C, recs(C, 5))["kind"],
                             "new")
            self.assertEqual(apply(cur, C, recs(C, 5)), "new")
            self.assertEqual(live(cur), as_live(recs(C, 5)))
            st = pe.status(cur, P)
            self.assertTrue(st["ok"], st)
            self.assertEqual((st["history"], st["empty"],
                              st["empty_pending"]), ([A], [B], []))
            # and straight from a pending empty state (not yet refreshed)
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            empty(cur, B)
            self.assertEqual(apply(cur, C, recs(C, 5)), "new")
            self.assertEqual(live(cur), as_live(recs(C, 5)))
            self.assertTrue(pe.status(cur, P)["ok"])

    def test_an_older_period_than_the_empty_state_is_history(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            empty(cur, C)
            core.refresh_latest(cur, EP, accept_key_changes=(C,))
            self.assertEqual(pe.compare_period(cur, P, B, recs(B))["kind"],
                             pe.HELD_NOT_LIVE)
            self.assertEqual(apply(cur, B, recs(B)), pe.HELD_NOT_LIVE)
            self.assertEqual(live(cur), [])
            self.assertEqual(apply(cur, B, recs(B)), "unchanged")
            self.assertEqual(live(cur), [])
            st = pe.status(cur, P)
            self.assertTrue(st["ok"], st)
            self.assertEqual(st["history"], [A, B])

    def test_rows_recorded_later_for_an_empty_period_win(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            empty(cur, B)
            self.assertEqual(apply(cur, B, recs(B, 7)), "new")
            self.assertEqual(core.empty_periods(cur, EP), [])
            self.assertEqual(live(cur), as_live(recs(B, 7)))
            self.assertTrue(pe.status(cur, P)["ok"])

    def test_a_period_with_editions_is_never_recorded_empty(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            with self.assertRaises(SystemExit) as cm:
                empty(cur, A)
            self.assertIn("edition", str(cm.exception.code))
            self.assertEqual(ledger(cur), [])

    def test_an_acknowledgement_is_required(self):
        with rolled_back(self.conn) as cur:
            for ack in (None, "", "   "):
                with self.assertRaises(ValueError):
                    empty(cur, B, ack=ack)
            self.assertEqual(ledger(cur), [])

    def test_the_refresh_command_preview_and_commit(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            empty(cur, B)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = pe.run_refresh_latest(
                    P, Namespace(commit=False, simulate=False,
                                 accept_drift=None,
                                 accept_key_changes=None), **borrowed(cur))
            self.assertEqual(rc, 0)
            self.assertIn(f"{B} is recorded empty", out.getvalue())
            self.assertIn(f"--accept-key-changes {B}", out.getvalue())
            self.assertIn("DRY RUN", out.getvalue())
            self.assertEqual(live(cur), as_live(recs(A)))
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                pe.run_refresh_latest(
                    P, Namespace(commit=False, simulate=True,
                                 accept_drift=None, accept_key_changes=None),
                    **borrowed(cur))
            self.assertEqual(live(cur), as_live(recs(A)))

    def test_an_empty_live_table_without_an_empty_period_is_still_missing(self):
        with rolled_back(self.conn) as cur:
            apply(cur, A, recs(A))
            cur.execute("DELETE FROM public.zz_ep_live")
            st = pe.status(cur, P)
            self.assertFalse(st["ok"])
            self.assertEqual(st["live_missing"], [A])


class NotOptedIn(unittest.TestCase):
    """A newest-period spec without the opt-in: no empty path runs, the
    status and refresh results carry no new key."""

    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_results_unchanged(self):
        with rolled_back(self.conn) as cur:
            pe.apply_period(cur, P_NP, A, recs(A), fetched_on=FETCHED)
            lm = core.latest_map(cur, NP)
            self.assertEqual((lm.empty, lm.empty_pending), ([], None))
            st = core.status(cur, NP)
            for k in ("empty", "empty_latest", "empty_pending"):
                self.assertNotIn(k, st)
                self.assertNotIn(k, pe.status(cur, P_NP))
            res = core.refresh_latest(cur, NP)
            self.assertNotIn("emptied", res)
            self.assertEqual(core.empty_periods(cur, NP), [])
            self.assertIsNone(core.empty_state(cur, NP))


if __name__ == "__main__":
    unittest.main()
