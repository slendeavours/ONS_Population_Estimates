"""Tests for the S8b loader in s8b_hb_editions (spec, fetch, compare, commands).

No network: load_months takes an injected fetch function, and fetch_month is
tested with the Stat-Xplore client's api_post patched. Database tests run on a
connection from _db.get_conn() inside a transaction that is always rolled back
(rolled_back below); nothing here commits. The only tables touched are the
throwaway zz_s8b_live and zz_s8b_editions (a copy of SPEC named zz_s8b),
created inside that transaction, so they never persist. Transaction control of
load_months (commit per month, stop on failure) is tested on a mock connection.
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
import psycopg2  # noqa: E402

import editions_core as core  # noqa: E402
import s8b_hb_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402

ZZ = dataclasses.replace(
    m.SPEC, name="zz_s8b", live_table="zz_s8b_live",
    editions_table="zz_s8b_editions", fk_la_boundaries=False,
    key_types=m.key_types_for("zz_s8b_editions"))
CODES = [f"E09{n:06d}" for n in range(m.EXPECTED_AREAS)]
FETCHED = date(2026, 10, 8)


def recs(month, n=m.EXPECTED_AREAS, value=lambda i, t: i, types=m.ACCOM_TYPES):
    return [{"lad24cd": CODES[i] if i < len(CODES) else f"E08{i:06d}",
             "month": month, "accom_type": t, "claimants": value(i, t)}
            for t in types for i in range(n)]


def fetcher(by_month):
    return lambda month: by_month[month]


@contextmanager
def rolled_back(conn):
    """A cursor in a transaction that is rolled back whatever happens."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_s8b_live'), "
                    "to_regclass('public.zz_s8b_editions')")
        if cur.fetchone() != (None, None):
            raise RuntimeError("zz_s8b_live or zz_s8b_editions already exists "
                               "as a real table; refusing to run")
        cur.execute("""CREATE TABLE public.zz_s8b_live (
            lad24cd text NOT NULL, month text NOT NULL,
            accom_type text NOT NULL, claimants integer,
            loaded_at timestamptz DEFAULT now(),
            PRIMARY KEY (lad24cd, month, accom_type))""")
        m.create_schema(cur, ZZ)
        yield cur
    finally:
        cur.close()
        conn.rollback()


def editions(cur, month=None):
    """[(edition, supersedes, rows)] of the throwaway editions table."""
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM public.zz_s8b_editions"
                + (" WHERE month = %s" if month else "")
                + " GROUP BY 1, 2 ORDER BY 1", (month,) if month else None)
    return cur.fetchall()


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class LoaderDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_new_month_becomes_edition_1(self):
        with rolled_back(self.conn) as cur:
            r = recs("202604")
            self.assertEqual(m.classify_month(cur, ZZ, "202604", r), "new")
            self.assertEqual(m.apply_month(cur, ZZ, "202604", r,
                                           fetched_on=FETCHED), "new")
            self.assertEqual(editions(cur), [(1, None, 4 * m.EXPECTED_AREAS)])
            cur.execute("""SELECT DISTINCT release_label, published_date,
                           source_file, source_sha256 FROM public.zz_s8b_editions""")
            self.assertEqual(cur.fetchall(), [(
                "stat-xplore fetch 2026-10-08", FETCHED,
                "Stat-Xplore table query hb_new by accommodation type",
                m.content_sha256(r))])

    def test_unchanged_month_stores_nothing(self):
        with rolled_back(self.conn) as cur:
            m.apply_month(cur, ZZ, "202604", recs("202604"), fetched_on=FETCHED)
            again = recs("202604")
            self.assertEqual(m.classify_month(cur, ZZ, "202604", again),
                             "unchanged")
            self.assertEqual(m.apply_month(cur, ZZ, "202604", again,
                                           fetched_on=date(2026, 11, 8)),
                             "unchanged")
            self.assertEqual(editions(cur), [(1, None, 4 * m.EXPECTED_AREAS)])

    def test_revised_month_becomes_edition_2_superseding_1(self):
        with rolled_back(self.conn) as cur:
            m.apply_month(cur, ZZ, "202604", recs("202604"), fetched_on=FETCHED)
            rev = recs("202604", value=lambda i, t: i + (1 if i == 5 else 0))
            cmp = m.compare_month(cur, ZZ, "202604", rev)
            self.assertEqual((cmp["kind"], cmp["changed"]), ("revised", 4))
            self.assertLessEqual(len(cmp["examples"]), 3)
            self.assertEqual(m.apply_month(cur, ZZ, "202604", rev,
                                           fetched_on=date(2026, 11, 8)),
                             "revised")
            n = 4 * m.EXPECTED_AREAS
            self.assertEqual(editions(cur), [(1, None, n), (2, 1, n)])
            self.assertEqual(core.chain_tip(cur, ZZ, "202604"), 2)

    def test_revert_to_earlier_content_is_stored_as_edition_3(self):
        with rolled_back(self.conn) as cur:
            a = recs("202604")
            b = recs("202604", value=lambda i, t: i + 1)
            m.apply_month(cur, ZZ, "202604", a, fetched_on=FETCHED)
            m.apply_month(cur, ZZ, "202604", b, fetched_on=FETCHED)
            self.assertEqual(m.classify_month(cur, ZZ, "202604", a), "revised")
            self.assertEqual(m.apply_month(cur, ZZ, "202604", a,
                                           fetched_on=FETCHED), "revised")
            n = 4 * m.EXPECTED_AREAS
            self.assertEqual(editions(cur), [(1, None, n), (2, 1, n), (3, 2, n)])
            self.assertEqual(m.apply_month(cur, ZZ, "202604", a,
                                           fetched_on=FETCHED), "unchanged")
            self.assertEqual(len(editions(cur)), 3)

    def test_months_with_recheck_flags_warns(self):
        err = io.StringIO()
        with mock.patch.object(m, "cmd_load", return_value=0), \
                contextlib.redirect_stderr(err):
            m.main(["load", "--months", "202603", "--recheck-all"])
            m.main(["load", "--months", "202603", "--recheck-n", "3"])
        self.assertEqual(err.getvalue().count("ignored"), 2)
        err = io.StringIO()
        with mock.patch.object(m, "cmd_load", return_value=0), \
                contextlib.redirect_stderr(err):
            m.main(["load", "--months", "202603"])
        self.assertEqual(err.getvalue(), "")

    def test_null_versus_zero_counts_as_revised(self):
        with rolled_back(self.conn) as cur:
            m.apply_month(cur, ZZ, "202604",
                          recs("202604", value=lambda i, t: 0 if i == 0 else i),
                          fetched_on=FETCHED)
            nulls = recs("202604", value=lambda i, t: None if i == 0 else i)
            self.assertEqual(m.classify_month(cur, ZZ, "202604", nulls),
                             "revised")
            self.assertEqual(m.apply_month(cur, ZZ, "202604", nulls,
                                           fetched_on=FETCHED), "revised")
            cur.execute("""SELECT COUNT(*) FROM public.zz_s8b_editions
                           WHERE edition = 2 AND claimants IS NULL""")
            self.assertEqual(cur.fetchone()[0], 4)

    def test_short_month_is_rejected_and_stored_nothing(self):
        with rolled_back(self.conn) as cur:
            short = recs("202604", n=m.EXPECTED_AREAS - 1)
            with quiet():
                rc = m.load_months(cur, ZZ, ["202604"],
                                   fetcher({"202604": short}), FETCHED, False,
                                   simulate=True)
            self.assertNotEqual(rc, 0)
            self.assertEqual(editions(cur), [])
            with self.assertRaises(ValueError):
                m.check_month_records(short, "202604")
            with self.assertRaises(ValueError):  # a type missing altogether
                m.check_month_records(recs("202604", types=("SA", "TA", "OTHER")),
                                      "202604")

    def test_default_is_preview_no_commit(self):
        with rolled_back(self.conn) as cur:
            with quiet():
                rc = m.load_months(cur, ZZ, ["202604"],
                                   fetcher({"202604": recs("202604")}),
                                   FETCHED, False)
            self.assertEqual(rc, 0)
            self.assertEqual(editions(cur), [])
        # and on a mock connection: classified, never applied, never committed
        cur = mock.MagicMock()
        with mock.patch.object(m, "compare_month", return_value={
                "kind": "new", "changed": 1184, "examples": [],
                "against": "edition"}) as cmp, \
                mock.patch.object(m, "apply_month") as apply, quiet():
            rc = m.load_months(cur, ZZ, ["202604"],
                               fetcher({"202604": recs("202604")}),
                               FETCHED, False)
        self.assertEqual(rc, 0)
        cmp.assert_called_once()
        apply.assert_not_called()
        cur.connection.commit.assert_not_called()

    def test_simulate_always_rolls_back(self):
        with rolled_back(self.conn) as cur:
            with quiet():
                rc = m.load_months(cur, ZZ, ["202604", "202605"],
                                   fetcher({"202604": recs("202604"),
                                            "202605": recs("202605")}),
                                   FETCHED, False, simulate=True)
            self.assertEqual(rc, 0)
            self.assertEqual(editions(cur), [])
            cur.execute("SELECT to_regclass('public.zz_s8b_editions')")
            self.assertIsNotNone(cur.fetchone()[0])  # savepoint, not the txn
        cur = mock.MagicMock()
        with mock.patch.object(m, "compare_month", return_value={
                "kind": "new", "changed": 1184, "examples": [],
                "against": "edition"}), \
                mock.patch.object(m, "apply_month", return_value="new") as apply, \
                quiet():
            m.load_months(cur, ZZ, ["202604"],
                          fetcher({"202604": recs("202604")}), FETCHED, False,
                          simulate=True)
        apply.assert_called_once()
        cur.connection.commit.assert_not_called()
        self.assertIn(mock.call("ROLLBACK TO SAVEPOINT s8b_month"),
                      cur.execute.call_args_list)
        with self.assertRaises(ValueError):
            m.load_months(cur, ZZ, [], fetcher({}), FETCHED, True,
                          simulate=True)

    def test_failure_on_one_month_leaves_others_committed(self):
        cur = mock.MagicMock()
        with mock.patch.object(m, "compare_month", return_value={
                "kind": "new", "changed": 1184, "examples": [],
                "against": "edition"}), \
                mock.patch.object(m, "apply_month",
                                  side_effect=["new", RuntimeError("boom")]), \
                quiet():
            rc = m.load_months(cur, ZZ, ["202604", "202605", "202606"],
                               fetcher({mo: recs(mo) for mo in
                                        ("202604", "202605", "202606")}),
                               FETCHED, True)
        self.assertNotEqual(rc, 0)
        cur.connection.commit.assert_called_once()   # 202604 only
        self.assertIn(mock.call("ROLLBACK TO SAVEPOINT s8b_month"),
                      cur.execute.call_args_list)    # 202605 untouched
        cur.connection.rollback.assert_called()

    def test_commit_and_simulate_are_mutually_exclusive(self):
        for cmd in (["load"], ["sync-new"], ["refresh-latest"], ["ddl"]):
            with self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                m.main(cmd + ["--commit", "--simulate"])
            self.assertEqual(ctx.exception.code, 2)

    def test_ddl_creates_editions_table_idempotently(self):
        with rolled_back(self.conn) as cur:
            m.create_schema(cur, ZZ)   # second time (rolled_back made it once)
            cur.execute("""SELECT column_name, data_type,
                                  character_maximum_length
                           FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = 'zz_s8b_editions'""")
            cols = {r[0]: r[1:] for r in cur.fetchall()}
            self.assertEqual(set(cols), {
                "lad24cd", "accom_type", "month", "edition", "claimants",
                "release_label", "published_date", "source_file",
                "source_sha256", "supersedes", "loaded_at"})
            self.assertEqual(cols["month"], ("character varying", 6))
            self.assertEqual(cols["accom_type"], ("character varying", 10))
            self.assertEqual(cols["claimants"][0], "integer")
            cur.execute("""SELECT a.attname FROM pg_index i
                           JOIN pg_attribute a ON a.attrelid = i.indrelid
                            AND a.attnum = ANY(i.indkey)
                           WHERE i.indrelid = 'public.zz_s8b_editions'::regclass
                             AND i.indisprimary""")
            self.assertEqual({r[0] for r in cur.fetchall()},
                             {"lad24cd", "accom_type", "month", "edition"})
            cur.execute("""SELECT tgname FROM pg_trigger
                           WHERE tgrelid = 'public.zz_s8b_editions'::regclass
                             AND NOT tgisinternal ORDER BY 1""")
            self.assertEqual([r[0] for r in cur.fetchall()],
                             ["zz_s8b_editions_immutable",
                              "zz_s8b_editions_no_truncate"])
            for month, accom in (("2026-4", "SA"), ("202604", "XX")):
                cur.execute("SAVEPOINT bad")
                with self.assertRaises(psycopg2.errors.CheckViolation):
                    cur.execute("""INSERT INTO public.zz_s8b_editions
                        (lad24cd, accom_type, month, edition, claimants)
                        VALUES ('E09000001', %s, %s, 1, 1)""", (accom, month))
                cur.execute("ROLLBACK TO SAVEPOINT bad")

    def test_real_spec_matches_live_table(self):
        self.assertEqual(m.SPEC.live_table, "la_hb_accom_type_caseload")
        self.assertEqual(m.SPEC.editions_table,
                         "la_hb_accom_type_caseload_editions")
        self.assertEqual(m.SPEC.key_cols, ("lad24cd", "accom_type"))
        self.assertEqual(m.SPEC.period_col, "month")
        self.assertTrue(m.SPEC.fk_la_boundaries)

    def test_preview_without_editions_table_compares_with_live(self):
        with rolled_back(self.conn) as cur:
            r = recs("202604")
            for x in r:
                cur.execute("INSERT INTO public.zz_s8b_live (lad24cd, month, "
                            "accom_type, claimants) VALUES (%s, %s, %s, %s)",
                            (x["lad24cd"], x["month"], x["accom_type"],
                             x["claimants"]))
            cmp = m.compare_month(cur, ZZ, "202604", r, against="live")
            self.assertEqual((cmp["kind"], cmp["against"]),
                             ("unchanged", "live"))
            self.assertEqual(m.compare_month(cur, ZZ, "202605",
                                             recs("202605"),
                                             against="live")["kind"], "new")

    def test_unresolvable_geography_code_halts(self):
        with rolled_back(self.conn) as cur:
            cur.execute("SELECT lad24cd FROM public.la_boundaries LIMIT 1")
            good = cur.fetchone()[0]
            members = [{"id": f"str:value:hb_new:X:{good}"},
                       {"id": "str:value:hb_new:X:E99999999"}]
            with self.assertRaises(SystemExit) as ctx, quiet():
                m.resolve_geography(members, self.conn)
            self.assertIn("E99999999", str(ctx.exception.code))


class FetchMonth(unittest.TestCase):
    """fetch_month through a patched client: never a short month."""

    def _cube(self, body, drop=0):
        uris = [u[0] for u in body["recodes"][m.GEO_FIELD_ID]["map"]]
        uris = uris[:len(uris) - drop] if drop else uris
        return {"fields": [{"items": [{"uris": [u]} for u in uris]}],
                "cubes": {"k": {"values": [[[7]] for _ in uris]}}}

    def _run(self, n, drop=0):
        lad_to_uris = {c: [f"str:value:hb_new:X:{c}"] for c in CODES[:n]}
        query = [u for v in lad_to_uris.values() for u in v]
        with mock.patch("statxplore_client.api_post",
                        side_effect=lambda path, body: self._cube(body, drop)), \
                mock.patch.object(m.time, "sleep"):
            return m.fetch_month("202604", lad_to_uris, query)

    def test_full_month(self):
        r = self._run(m.EXPECTED_AREAS)
        self.assertEqual(len(r), 4 * m.EXPECTED_AREAS)
        self.assertEqual({x["accom_type"] for x in r}, set(m.ACCOM_TYPES))

    def test_295_areas_rejected(self):
        with self.assertRaises(ValueError):
            self._run(m.EXPECTED_AREAS - 1)

    def test_area_missing_from_response_rejected(self):
        with self.assertRaises(ValueError):
            self._run(m.EXPECTED_AREAS, drop=1)


if __name__ == "__main__":
    unittest.main()
