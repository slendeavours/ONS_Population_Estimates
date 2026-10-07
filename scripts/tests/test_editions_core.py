"""Tests for editions_core (write side: schema, insert, chain tip).

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
                    "period varchar(6), value integer)")
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


class SpecValidation(unittest.TestCase):
    def test_bad_identifier_refused(self):
        with self.assertRaises(ValueError):
            core.EditionSpec(name="x", live_table="x; drop", editions_table="y",
                             key_cols=("lad24cd",), period_col="period",
                             value_cols=(), refresh_cols=())


if __name__ == "__main__":
    unittest.main()
