"""Tests for editions_core (write side: schema, insert, chain tip; read side
and refresh: status, sync-new, refresh-latest, the hash guard).

Every database test runs on a connection from _db.get_conn() inside a
transaction that is always rolled back (rolled_back below). Nothing here
commits. The only tables touched are the throwaway zz_core_live and
zz_core_editions, created inside that transaction, so they never persist.
Statements expected to fail (UPDATE, DELETE, TRUNCATE) run inside a savepoint
so the transaction stays usable.
"""
import sys
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg2  # noqa: E402
import psycopg2.errors  # noqa: E402

import editions_core as core  # noqa: E402
from _db import get_conn  # noqa: E402

SPEC = core.EditionSpec(
    name="zz_core",
    live_table="zz_core_live",
    editions_table="zz_core_editions",
    key_cols=("lad24cd",),
    period_col="period",
    value_cols=(("value", "integer"),),
    refresh_cols=("value",),
    fk_la_boundaries=False,
)
ROWS = [{"lad24cd": "E06000001", "value": 10},
        {"lad24cd": "E06000002", "value": None}]


@contextmanager
def rolled_back(conn):
    """A cursor in a transaction that is rolled back whatever happens."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_core_live'), "
                    "to_regclass('public.zz_core_editions')")
        if cur.fetchone() != (None, None):
            raise RuntimeError("zz_core_live or zz_core_editions already "
                               "exists as a real table; refusing to run")
        cur.execute("CREATE TABLE public.zz_core_live (lad24cd varchar(9), "
                    "period varchar(6), value integer, "
                    "loaded_at timestamptz NOT NULL DEFAULT now())")
        yield cur
    finally:
        cur.close()
        conn.rollback()


@contextmanager
def savepoint(cur):
    cur.execute("SAVEPOINT sp")
    try:
        yield
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT sp")


def ins(cur, sha, supersedes, period="2025Q1", rows=ROWS):
    return core.insert_edition(
        cur, SPEC, rows, period, release_label="test",
        published_date=date(2026, 1, 1), source_file="f.ods",
        source_sha256=sha, supersedes=supersedes)


def live(cur, rows, period="2025Q1"):
    """Rows straight into the throwaway live table (as a loader would)."""
    for r in rows:
        cur.execute("INSERT INTO public.zz_core_live (lad24cd, period, value) "
                    "VALUES (%s, %s, %s)", (r["lad24cd"], period, r["value"]))


def live_values(cur):
    cur.execute("SELECT period, lad24cd, value FROM public.zz_core_live "
                "ORDER BY 1, 2")
    return cur.fetchall()


class EditionsCoreDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_create_schema_idempotent(self):
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, SPEC)
            core.create_schema(cur, SPEC)
            cur.execute("""SELECT column_name FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = 'zz_core_editions'""")
            cols = {r[0] for r in cur.fetchall()}
            self.assertEqual(cols, {
                "lad24cd", "period", "edition", "value", "release_label",
                "published_date", "source_file", "source_sha256",
                "supersedes", "loaded_at"})
            cur.execute("""SELECT a.attname FROM pg_index i
                           JOIN pg_attribute a ON a.attrelid = i.indrelid
                            AND a.attnum = ANY(i.indkey)
                           WHERE i.indrelid = 'public.zz_core_editions'::regclass
                             AND i.indisprimary""")
            self.assertEqual({r[0] for r in cur.fetchall()},
                             {"lad24cd", "period", "edition"})
            cur.execute("""SELECT tgname FROM pg_trigger
                           WHERE tgrelid = 'public.zz_core_editions'::regclass
                             AND NOT tgisinternal ORDER BY 1""")
            self.assertEqual([r[0] for r in cur.fetchall()],
                             ["zz_core_editions_immutable",
                              "zz_core_editions_no_truncate"])

    def test_update_delete_truncate_blocked(self):
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, SPEC)
            ins(cur, "a", None)
            for sql in ("UPDATE public.zz_core_editions SET value = 1",
                        "DELETE FROM public.zz_core_editions",
                        "TRUNCATE public.zz_core_editions"):
                with self.subTest(sql=sql), savepoint(cur):
                    with self.assertRaises(psycopg2.errors.RaiseException) as cm:
                        cur.execute(sql)
                    self.assertIn("append-only", str(cm.exception))
            cur.execute("SELECT COUNT(*) FROM public.zz_core_editions")
            self.assertEqual(cur.fetchone()[0], 2)

    def test_insert_first_edition_is_1(self):
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, SPEC)
            self.assertEqual(ins(cur, "a", None), 1)
            cur.execute("""SELECT lad24cd, period, edition, value, supersedes,
                                  source_sha256, loaded_at IS NOT NULL
                           FROM public.zz_core_editions ORDER BY 1""")
            self.assertEqual(cur.fetchall(), [
                ("E06000001", "2025Q1", 1, 10, None, "a", True),
                ("E06000002", "2025Q1", 1, None, None, "a", True)])
            # same file again: nothing inserted, same edition returned
            self.assertEqual(ins(cur, "a", None), 1)
            cur.execute("SELECT COUNT(*) FROM public.zz_core_editions")
            self.assertEqual(cur.fetchone()[0], 2)

    def test_second_edition_must_supersede_tip(self):
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, SPEC)
            ins(cur, "a", None)
            with self.assertRaises(SystemExit) as cm:
                ins(cur, "b", None)
            self.assertIn("chain tip", str(cm.exception))
            self.assertEqual(ins(cur, "b", 1), 2)
            with self.assertRaises(SystemExit):
                ins(cur, "c", 1)  # 1 is no longer the tip
            with self.assertRaises(SystemExit):
                ins(cur, "x", 1, period="2025Q2")  # no editions yet
            with self.assertRaises(SystemExit):
                ins(cur, "y", None, rows=[])
            with self.assertRaises(SystemExit):
                ins(cur, "z", None, period="2025Q3",
                    rows=[{"lad24cd": "E06000001", "period": "2025Q4"}])

    def test_latest_edition_follows_chain(self):
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, SPEC)
            ins(cur, "a", None)
            ins(cur, "b", 1)
            ins(cur, "c", 2)
            self.assertEqual(core.latest_edition(cur, SPEC, "2025Q1"), 3)
            with self.assertRaises(SystemExit) as cm:
                core.latest_edition(cur, SPEC, "2099Q1")
            self.assertIn("no editions", str(cm.exception))

    def test_fork_detected(self):
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, SPEC)
            ins(cur, "a", None)
            # a fork cannot be made through insert_edition, so write it raw
            cur.execute("""INSERT INTO public.zz_core_editions
                           (lad24cd, period, edition, supersedes, source_sha256)
                           VALUES ('E06000001', '2025Q1', 2, 1, 'b'),
                                  ('E06000001', '2025Q1', 3, 1, 'c')""")
            with self.assertRaises(SystemExit) as cm:
                core.latest_edition(cur, SPEC, "2025Q1")
            self.assertIn("fork", str(cm.exception))

    # ------------------------------------------------ read side and refresh

    def two_periods_synced(self, cur):
        """Live 2025Q1 and 2025Q2 (ROWS each), both given edition 1."""
        core.create_schema(cur, SPEC)
        live(cur, ROWS, "2025Q1")
        live(cur, ROWS, "2025Q2")
        self.assertEqual(core.sync_new(cur, SPEC, expected_authorities_n=2),
                         ["2025Q1", "2025Q2"])

    def test_status_reports_new_live_period(self):
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, SPEC)
            live(cur, ROWS)
            st = core.status(cur, SPEC)
            self.assertEqual(st["new_periods"], ["2025Q1"])
            self.assertFalse(st["ok"])
            for k in ("pending_refresh", "drift", "bad_counts", "forked"):
                self.assertFalse(st[k], k)
            ins(cur, "a", None)  # an edition equal to the live rows
            st = core.status(cur, SPEC)
            self.assertEqual(st["new_periods"], [])
            self.assertTrue(st["ok"], st)

    def test_sync_new_records_edition_1_idempotently(self):
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, SPEC)
            live(cur, ROWS, "2025Q1")
            live(cur, ROWS, "2025Q2")
            # nothing has editions, so the authority count must be given
            with self.assertRaises(SystemExit) as cm:
                core.sync_new(cur, SPEC)
            self.assertIn("expected-authorities", str(cm.exception))
            with self.assertRaises(SystemExit):  # wrong count
                core.sync_new(cur, SPEC, expected_authorities_n=3)
            self.assertEqual(core.sync_new(cur, SPEC, expected_authorities_n=2),
                             ["2025Q1", "2025Q2"])
            cur.execute("""SELECT period, lad24cd, edition, value, supersedes,
                                  release_label
                           FROM public.zz_core_editions ORDER BY 1, 2""")
            label = core.AS_LOADED_LABEL
            self.assertEqual(cur.fetchall(), [
                ("2025Q1", "E06000001", 1, 10, None, label),
                ("2025Q1", "E06000002", 1, None, None, label),  # NULL kept
                ("2025Q2", "E06000001", 1, 10, None, label),
                ("2025Q2", "E06000002", 1, None, None, label)])
            self.assertEqual(core.sync_new(cur, SPEC), [])
            self.assertTrue(core.status(cur, SPEC)["ok"])
            # a later period is checked against the derived count (2)
            live(cur, ROWS[:1], "2025Q3")
            with self.assertRaises(SystemExit) as cm:
                core.sync_new(cur, SPEC)
            self.assertIn("2025Q3", str(cm.exception))
            cur.execute("SELECT COUNT(*) FROM public.zz_core_editions "
                        "WHERE period = '2025Q3'")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_refresh_copies_latest_into_live_and_nothing_else(self):
        with rolled_back(self.conn) as cur:
            self.two_periods_synced(cur)
            cur.execute("SELECT period, lad24cd, loaded_at "
                        "FROM public.zz_core_live ORDER BY 1, 2")
            loaded_before = cur.fetchall()
            q2_before = core.period_hashes(cur, SPEC, "live")["2025Q2"]
            # edition 2 of 2025Q1: 10 -> 11, and NULL -> 0 (a real change)
            ins(cur, "b", 1, rows=[{"lad24cd": "E06000001", "value": 11},
                                   {"lad24cd": "E06000002", "value": 0}])
            st = core.status(cur, SPEC)
            self.assertEqual(st["pending_refresh"], ["2025Q1"])
            self.assertFalse(st["ok"])
            ed_before = core.period_hashes(cur, SPEC, "editions")
            res = core.refresh_latest(cur, SPEC)
            self.assertEqual(res["updated"], {"2025Q1": 2})
            self.assertEqual(res["rows"], 2)
            self.assertEqual(res["drift_accepted"], [])
            self.assertEqual(live_values(cur), [
                ("2025Q1", "E06000001", 11), ("2025Q1", "E06000002", 0),
                ("2025Q2", "E06000001", 10), ("2025Q2", "E06000002", None)])
            self.assertEqual(core.period_hashes(cur, SPEC, "live")["2025Q2"],
                             q2_before)
            self.assertEqual(core.period_hashes(cur, SPEC, "editions"),
                             ed_before)
            cur.execute("SELECT period, lad24cd, loaded_at "
                        "FROM public.zz_core_live ORDER BY 1, 2")
            self.assertEqual(cur.fetchall(), loaded_before)
            self.assertTrue(core.status(cur, SPEC)["ok"])
            # nothing left to do: a second refresh writes nothing
            self.assertEqual(core.refresh_latest(cur, SPEC)["rows"], 0)

    def test_refresh_halts_on_drift(self):
        with rolled_back(self.conn) as cur:
            self.two_periods_synced(cur)
            # NULL -> 0 directly in live: not equal to any edition
            cur.execute("UPDATE public.zz_core_live SET value = 0 "
                        "WHERE period = '2025Q1' AND lad24cd = 'E06000002'")
            st = core.status(cur, SPEC)
            self.assertEqual(st["drift"], ["2025Q1"])
            self.assertEqual(st["pending_refresh"], [])
            with self.assertRaises(SystemExit) as cm:
                core.refresh_latest(cur, SPEC)
            self.assertIn("2025Q1", str(cm.exception))
            self.assertIn("matches no stored edition", str(cm.exception))
            with self.assertRaises(SystemExit) as cm:  # not drifted
                core.refresh_latest(cur, SPEC, accept_drift=("2025Q2",))
            self.assertIn("accept-drift", str(cm.exception))
            res = core.refresh_latest(cur, SPEC, accept_drift=("2025Q1",))
            self.assertEqual(res["drift_accepted"], ["2025Q1"])
            self.assertEqual(res["updated"], {"2025Q1": 1})
            self.assertEqual(live_values(cur)[1], ("2025Q1", "E06000002", None))
            self.assertTrue(core.status(cur, SPEC)["ok"])

    def test_refresh_guard_catches_other_period_change(self):
        with rolled_back(self.conn) as cur:
            self.two_periods_synced(cur)
            ins(cur, "b", 1, rows=[{"lad24cd": "E06000001", "value": 11},
                                   {"lad24cd": "E06000002", "value": None}])
            before = live_values(cur)
            seen = []

            def hook(c, plan):
                seen.append(sorted(plan))
                c.execute("UPDATE public.zz_core_live SET value = 7 "
                          "WHERE period = '2025Q2' AND lad24cd = 'E06000001'")

            with self.assertRaises(SystemExit) as cm:
                core.refresh_latest(cur, SPEC, _after_update_hook=hook)
            self.assertEqual(seen, [["2025Q1"]])
            self.assertIn("guard: 2025Q2 changed", str(cm.exception))
            # the refresh undid its own writes; the transaction is usable
            self.assertEqual(live_values(cur), before)
            self.assertEqual(core.status(cur, SPEC)["pending_refresh"],
                             ["2025Q1"])

    def test_refresh_refuses_one_sided_period(self):
        """A key in only one of live and the edition cannot be repaired by
        an UPDATE: halt before writing (RO4's rule), even with the period's
        drift accepted."""
        with rolled_back(self.conn) as cur:
            self.two_periods_synced(cur)
            cur.execute("DELETE FROM public.zz_core_live "
                        "WHERE period = '2025Q1' AND lad24cd = 'E06000002'")
            cur.execute("UPDATE public.zz_core_live SET value = 5 "
                        "WHERE period = '2025Q1'")
            before = live_values(cur)
            with self.assertRaises(SystemExit) as cm:
                core.refresh_latest(cur, SPEC, accept_drift=("2025Q1",))
            self.assertIn("present in only one", str(cm.exception))
            self.assertEqual(live_values(cur), before)

    def test_status_flags_fork(self):
        with rolled_back(self.conn) as cur:
            core.create_schema(cur, SPEC)
            live(cur, ROWS)
            ins(cur, "a", None)
            cur.execute("""INSERT INTO public.zz_core_editions
                           (lad24cd, period, edition, supersedes, source_sha256)
                           VALUES ('E06000001', '2025Q1', 2, 1, 'b'),
                                  ('E06000001', '2025Q1', 3, 1, 'c')""")
            st = core.status(cur, SPEC)
            self.assertEqual(list(st["forked"]), ["2025Q1"])
            self.assertIn("fork", st["forked"]["2025Q1"])
            self.assertFalse(st["ok"])
            for f in (lambda: core.refresh_latest(cur, SPEC),
                      lambda: core.sync_new(cur, SPEC)):
                with self.assertRaises(SystemExit) as cm:
                    f()
                self.assertIn("fork", str(cm.exception))

    @unittest.skip("needs the S1, S1b and RO4 SPEC objects; Tasks 5 to 7 "
                   "declare them and unskip this test")
    def test_create_schema_matches_existing_tables(self):
        """For each real spec: create_schema on a throwaway copy of the spec
        (editions_table renamed zz_core_<name>_editions, inside rolled_back)
        and compare column names and types from information_schema.columns
        with the real editions table's. The real table is only read."""


class ChainValidation(unittest.TestCase):
    """The chain rules, without a database (pure function)."""

    def tip(self, pairs):
        return core._chain_tip(pairs, "P")

    def test_linear(self):
        self.assertEqual(self.tip([(1, None), (2, 1), (3, 2)]), 3)

    def test_broken_chains_refused(self):
        for pairs, word in (([(1, None), (2, None)], "root"),
                            ([(1, None), (2, 1), (3, 1)], "fork"),
                            ([(1, None), (2, 2)], "itself"),
                            ([(1, None), (2, 5)], "missing"),
                            ([(1, None), (2, 3), (3, 2)], "cycle"),
                            ([(1, None), (1, 2)], "inconsistent")):
            with self.subTest(pairs=pairs):
                with self.assertRaises(ValueError) as cm:
                    self.tip(pairs)
                self.assertIn(word, str(cm.exception))

    def test_no_editions(self):
        with self.assertRaises(LookupError):
            self.tip([])


class GuardRule(unittest.TestCase):
    """guard_problems without a database (pure function)."""

    def test_guard(self):
        full = {"A": "1", "B": "2"}
        kept = {"A": "k1", "B": "k2"}
        self.assertEqual(core.guard_problems(full, {"A": "x", "B": "2"}, kept,
                                             kept, {"A"}), [])
        self.assertIn("B changed", core.guard_problems(
            full, {"A": "1", "B": "y"}, kept, kept, {"A"})[0])
        self.assertIn("outside the refresh columns", core.guard_problems(
            full, full, kept, {"A": "z", "B": "k2"}, {"A"})[0])
        self.assertIn("C changed", core.guard_problems(
            full, dict(full, C="3"), kept, kept, set())[0])


class SpecValidation(unittest.TestCase):
    def test_bad_identifier_refused(self):
        with self.assertRaises(ValueError):
            core.EditionSpec(name="x", live_table="x; drop", editions_table="y",
                             key_cols=("lad24cd",), period_col="period",
                             value_cols=(), refresh_cols=())

    def test_inconsistent_columns_refused(self):
        base = dict(name="x", live_table="x", editions_table="y",
                    key_cols=("lad24cd",), period_col="period",
                    value_cols=(("v", "integer"),), refresh_cols=("v",))
        for bad in (dict(key_types=(("v", "integer"),)),  # not a key column
                    dict(refresh_cols=("lad24cd",)),       # a key column
                    dict(refresh_cols=("nowhere",)),       # not in editions
                    dict(refresh_from=(("w", "loaded_at"),)),  # w not refreshed
                    dict(refresh_cols=("v", "w"),
                         refresh_from=(("w", "nowhere"),))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.EditionSpec(**{**base, **bad})
        s = core.EditionSpec(**{**base, "refresh_cols": ("v", "source_file",
                                                         "extracted_at"),
                                "refresh_from": (("extracted_at", "loaded_at"),)})
        self.assertEqual(s.compare_cols, ("v",))
        self.assertEqual(s.update_cols, ("v", "source_file"))

    def test_rows_sha256_null_is_not_zero(self):
        a = core.rows_sha256(SPEC, [{"lad24cd": "E1", "value": None}])
        b = core.rows_sha256(SPEC, [{"lad24cd": "E1", "value": 0}])
        self.assertNotEqual(a, b)
        self.assertEqual(a, core.rows_sha256(SPEC, [{"lad24cd": "E1"}]))


if __name__ == "__main__":
    unittest.main()
