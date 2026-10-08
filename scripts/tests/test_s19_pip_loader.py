"""Tests for the S19 loader in s19_pip_editions (spec, fetch, compare,
commands, the migrate-months command and the unmigrated-months stop).

No network: load_months takes an injected fetch function, and fetch_month is
tested with the Stat-Xplore client's api_post patched. Database tests run on a
connection from _db.get_conn() inside a transaction that is always rolled back
(rolled_back below); nothing here commits. The only tables touched are the
throwaway zz_s19_live and zz_s19_editions (a copy of SPEC named zz_s19),
created inside that transaction, so they never persist. Transaction control
(commit per month, stop on failure) and the commands are tested on a mock
connection.
"""
import contextlib
import dataclasses
import io
import pathlib
import sys
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg2  # noqa: E402

import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
import s19_pip_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402

ZZ = dataclasses.replace(
    m.SPEC, name="zz_s19", live_table="zz_s19_live",
    editions_table="zz_s19_editions", fk_la_boundaries=False,
    key_types=m.key_types_for("zz_s19_editions"))
CODES = [f"E09{n:06d}" for n in range(m.EXPECTED_AREAS)]
FETCHED = date(2026, 10, 8)
N = m.EXPECTED_AREAS


def recs(month, n=N, total=lambda i: 100 * i, enh=lambda i: i):
    return [{"lad24cd": CODES[i], "month": month,
             "pip_total_claimants": total(i),
             "pip_enhanced_daily_living": enh(i)} for i in range(n)]


def fetcher(by_month):
    return lambda month: by_month[month]


@contextmanager
def rolled_back(conn):
    """A cursor in a transaction that is rolled back whatever happens."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_s19_live'), "
                    "to_regclass('public.zz_s19_editions')")
        if cur.fetchone() != (None, None):
            raise RuntimeError("zz_s19_live or zz_s19_editions already exists "
                               "as a real table; refusing to run")
        cur.execute("""CREATE TABLE public.zz_s19_live (
            lad24cd text NOT NULL, month text NOT NULL,
            pip_total_claimants integer, pip_enhanced_daily_living integer,
            loaded_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (lad24cd, month))""")
        m.create_schema(cur, ZZ)
        yield cur
    finally:
        cur.close()
        conn.rollback()


def editions(cur, month=None):
    """[(edition, supersedes, rows)] of the throwaway editions table."""
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM public.zz_s19_editions"
                + (" WHERE month = %s" if month else "")
                + " GROUP BY 1, 2 ORDER BY 1", (month,) if month else None)
    return cur.fetchall()


def live(cur, month):
    """{lad24cd: (total, enhanced)} of the throwaway live table."""
    cur.execute("SELECT lad24cd, pip_total_claimants, pip_enhanced_daily_living "
                "FROM public.zz_s19_live WHERE month = %s", (month,))
    return {a: (b, c) for a, b, c in cur.fetchall()}


def as_live(records):
    return {r["lad24cd"]: (r["pip_total_claimants"],
                           r["pip_enhanced_daily_living"]) for r in records}


def seed_live(cur, records):
    for x in records:
        cur.execute("INSERT INTO public.zz_s19_live (lad24cd, month, "
                    "pip_total_claimants, pip_enhanced_daily_living) "
                    "VALUES (%s, %s, %s, %s)",
                    (x["lad24cd"], x["month"], x["pip_total_claimants"],
                     x["pip_enhanced_daily_living"]))


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

    def test_new_month_becomes_edition_1_and_reaches_live_in_one_transaction(self):
        with rolled_back(self.conn) as cur:
            r = recs("202604", total=lambda i: None if i == 0 else (
                0 if i == 1 else 100 * i))
            self.assertEqual(m.classify_month(cur, ZZ, "202604", r), "new")
            self.assertEqual(m.apply_month(cur, ZZ, "202604", r,
                                           fetched_on=FETCHED), "new")
            self.assertEqual(editions(cur), [(1, None, N)])
            cur.execute("""SELECT DISTINCT release_label, published_date,
                           source_file, source_sha256 FROM public.zz_s19_editions""")
            self.assertEqual(cur.fetchall(), [(
                "stat-xplore fetch 2026-10-08", FETCHED,
                "Stat-Xplore table query PIP_Monthly_new by local authority",
                m.content_sha256(r))])
            self.assertEqual(live(cur, "202604"), as_live(r))
            self.assertEqual(core.rows_differing(cur, ZZ, "202604", 1), 0)
            self.assertEqual(m.check_live_equals_edition(cur, ZZ, "202604", 1),
                             [])
            st = m.status(cur, ZZ)
            self.assertEqual(st["live_missing"], [])
            self.assertTrue(st["ok"], st)
            # through load_months on commit: one savepoint, one commit for
            # the month (edition 1 and the live rows together)
            cur2 = mock.MagicMock()
            with mock.patch.object(m, "compare_month", return_value={
                    "kind": "new", "changed": N, "examples": [],
                    "against": "no edition"}), \
                    mock.patch.object(m, "apply_month", return_value="new"), \
                    quiet():
                rc = m.load_months(cur2, ZZ, ["202605"],
                                   fetcher({"202605": recs("202605")}),
                                   FETCHED, True)
            self.assertEqual(rc, 0)
            cur2.connection.commit.assert_called_once()
            self.assertIn(mock.call("SAVEPOINT s19_month"),
                          cur2.execute.call_args_list)

    def test_failure_inserting_live_rolls_back_edition_and_live(self):
        seen = {}
        real_insert = m.insert_live

        def boom(cur, spec, month, records):
            cur.execute("SELECT COUNT(*) FROM public.zz_s19_editions")
            seen["editions_before_failure"] = cur.fetchone()[0]
            real_insert(cur, spec, month, records[:10])  # part-way, then fail
            raise RuntimeError("live insert failed")

        with rolled_back(self.conn) as cur:
            with mock.patch.object(m, "insert_live", side_effect=boom), quiet():
                rc = m.load_months(cur, ZZ, ["202604"],
                                   fetcher({"202604": recs("202604")}),
                                   FETCHED, False, simulate=True)
            self.assertEqual(rc, 1)
            self.assertEqual(seen["editions_before_failure"], N)
            self.assertEqual(editions(cur), [])
            self.assertEqual(live(cur, "202604"), {})

        # live rows that differ from the edition halt the month
        def wrong(cur, spec, month, records):
            bad = [dict(x, pip_total_claimants=(x["pip_total_claimants"] or 0)
                        + 1) if i == 0 else x for i, x in enumerate(records)]
            real_insert(cur, spec, month, bad)

        with rolled_back(self.conn) as cur:
            cur.execute("SAVEPOINT t")
            with mock.patch.object(m, "insert_live", side_effect=wrong), \
                    self.assertRaises(SystemExit) as ctx:
                m.apply_month(cur, ZZ, "202604", recs("202604"),
                              fetched_on=FETCHED)
            self.assertIn("live rows differ from edition 1",
                          str(ctx.exception.code))
            cur.execute("ROLLBACK TO SAVEPOINT t")
            self.assertEqual(editions(cur), [])
            self.assertEqual(live(cur, "202604"), {})

    def test_unchanged_stores_nothing(self):
        with rolled_back(self.conn) as cur:
            m.apply_month(cur, ZZ, "202604", recs("202604"), fetched_on=FETCHED)
            cur.execute("SELECT COUNT(*), MAX(loaded_at) FROM public.zz_s19_live")
            before = cur.fetchone()
            again = recs("202604")
            self.assertEqual(m.classify_month(cur, ZZ, "202604", again),
                             "unchanged")
            with mock.patch.object(m, "insert_live") as ins:
                self.assertEqual(m.apply_month(cur, ZZ, "202604", again,
                                               fetched_on=date(2026, 11, 8)),
                                 "unchanged")
            ins.assert_not_called()
            self.assertEqual(editions(cur), [(1, None, N)])
            cur.execute("SELECT COUNT(*), MAX(loaded_at) FROM public.zz_s19_live")
            self.assertEqual(cur.fetchone(), before)

    def test_revised_becomes_edition_2_and_does_not_touch_live(self):
        with rolled_back(self.conn) as cur:
            r = recs("202604")
            m.apply_month(cur, ZZ, "202604", r, fetched_on=FETCHED)
            rev = recs("202604", enh=lambda i: i + (1 if i in (5, 6) else 0))
            cmp = m.compare_month(cur, ZZ, "202604", rev)
            self.assertEqual((cmp["kind"], cmp["changed"]), ("revised", 2))
            self.assertLessEqual(len(cmp["examples"]), 3)
            with mock.patch.object(m, "insert_live") as ins:
                self.assertEqual(m.apply_month(cur, ZZ, "202604", rev,
                                               fetched_on=date(2026, 11, 8)),
                                 "revised")
            ins.assert_not_called()
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertEqual(core.chain_tip(cur, ZZ, "202604"), 2)
            self.assertEqual(live(cur, "202604"), as_live(r))
            self.assertEqual(m.status(cur, ZZ)["pending_refresh"], ["202604"])
            res = core.refresh_latest(cur, ZZ)
            self.assertEqual(res["rows"], 2)
            self.assertEqual(live(cur, "202604"), as_live(rev))

    def test_revert_is_stored_as_a_new_edition(self):
        with rolled_back(self.conn) as cur:
            a = recs("202604")
            b = recs("202604", total=lambda i: 100 * i + 1)
            m.apply_month(cur, ZZ, "202604", a, fetched_on=FETCHED)
            m.apply_month(cur, ZZ, "202604", b, fetched_on=FETCHED)
            self.assertEqual(m.classify_month(cur, ZZ, "202604", a), "revised")
            self.assertEqual(m.apply_month(cur, ZZ, "202604", a,
                                           fetched_on=FETCHED), "revised")
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N), (3, 2, N)])
            self.assertEqual(m.apply_month(cur, ZZ, "202604", a,
                                           fetched_on=FETCHED), "unchanged")
            self.assertEqual(len(editions(cur)), 3)

    def test_null_versus_zero_counts_as_revised(self):
        with rolled_back(self.conn) as cur:
            m.apply_month(cur, ZZ, "202604",
                          recs("202604", enh=lambda i: 0 if i == 0 else i),
                          fetched_on=FETCHED)
            nulls = recs("202604", enh=lambda i: None if i == 0 else i)
            self.assertEqual(m.classify_month(cur, ZZ, "202604", nulls),
                             "revised")
            self.assertEqual(m.apply_month(cur, ZZ, "202604", nulls,
                                           fetched_on=FETCHED), "revised")
            cur.execute("""SELECT COUNT(*) FROM public.zz_s19_editions
                           WHERE edition = 2
                             AND pip_enhanced_daily_living IS NULL""")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("""SELECT pip_enhanced_daily_living
                           FROM public.zz_s19_editions
                           WHERE edition = 1 AND lad24cd = %s""", (CODES[0],))
            self.assertEqual(cur.fetchone()[0], 0)
            self.assertNotEqual(m.content_sha256(nulls), m.content_sha256(
                recs("202604", enh=lambda i: 0 if i == 0 else i)))

    def test_short_month_rejected_and_nothing_stored(self):
        with rolled_back(self.conn) as cur:
            short = recs("202604", n=N - 1)
            with quiet():
                rc = m.load_months(cur, ZZ, ["202604"],
                                   fetcher({"202604": short}), FETCHED, False,
                                   simulate=True)
            self.assertNotEqual(rc, 0)
            self.assertEqual(editions(cur), [])
            self.assertEqual(live(cur, "202604"), {})
            with self.assertRaises(ValueError):
                m.check_month_records(short, "202604")
            dup = recs("202604") + recs("202604", n=1)
            with self.assertRaises(ValueError):
                m.check_month_records(dup, "202604")

    def test_month_with_one_measure_missing_rejected(self):
        absent = [{k: v for k, v in r.items()
                   if k != "pip_enhanced_daily_living"}
                  for r in recs("202604")]
        all_null = recs("202604", total=lambda i: None)
        for bad in (absent, all_null):
            with self.assertRaises(ValueError):
                m.check_month_records(bad, "202604")
            with rolled_back(self.conn) as cur:
                with quiet():
                    rc = m.load_months(cur, ZZ, ["202604"],
                                       fetcher({"202604": bad}), FETCHED,
                                       False, simulate=True)
                self.assertEqual(rc, 1)
                self.assertEqual(editions(cur), [])
                self.assertEqual(live(cur, "202604"), {})
        # some NULLs in a measure are fine (the DWP '..' marker)
        m.check_month_records(recs("202604", enh=lambda i: None if i % 7 == 0
                                   else i), "202604")

    def test_default_is_preview_no_commit(self):
        with rolled_back(self.conn) as cur:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = m.load_months(cur, ZZ, ["202604"],
                                   fetcher({"202604": recs("202604")}),
                                   FETCHED, False)
            self.assertEqual(rc, 0)
            self.assertEqual(editions(cur), [])
            self.assertEqual(live(cur, "202604"), {})
            self.assertIn("would store edition 1 and insert 296 live rows",
                          out.getvalue())
        cur = mock.MagicMock()
        with mock.patch.object(m, "compare_month", return_value={
                "kind": "new", "changed": N, "examples": [],
                "against": "no edition"}) as cmp, \
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
            self.assertEqual(live(cur, "202604"), {})
            cur.execute("SELECT to_regclass('public.zz_s19_editions')")
            self.assertIsNotNone(cur.fetchone()[0])  # savepoint, not the txn
        cur = mock.MagicMock()
        with mock.patch.object(m, "compare_month", return_value={
                "kind": "new", "changed": N, "examples": [],
                "against": "no edition"}), \
                mock.patch.object(m, "apply_month", return_value="new") as apply, \
                quiet():
            m.load_months(cur, ZZ, ["202604"],
                          fetcher({"202604": recs("202604")}), FETCHED, False,
                          simulate=True)
        apply.assert_called_once()
        cur.connection.commit.assert_not_called()
        self.assertIn(mock.call("ROLLBACK TO SAVEPOINT s19_month"),
                      cur.execute.call_args_list)
        with self.assertRaises(ValueError):
            m.load_months(cur, ZZ, [], fetcher({}), FETCHED, True,
                          simulate=True)

    def test_status_flags_editions_month_missing_from_live(self):
        with rolled_back(self.conn) as cur:
            r = recs("202604")
            m.apply_month(cur, ZZ, "202604", r, fetched_on=FETCHED)
            self.assertTrue(m.status(cur, ZZ)["ok"])
            # the stranded shape: edition 1 only, no live rows
            core.insert_edition(cur, ZZ, recs("202605"), "202605",
                                release_label="t", published_date=FETCHED,
                                source_file="t", source_sha256="h",
                                supersedes=None)
            st = m.status(cur, ZZ)
            self.assertEqual(st["live_missing"], ["202605"])
            self.assertFalse(st["ok"])
            text = m.format_status(st)
            self.assertIn("editions month missing from live: 202605", text)
            self.assertIn("ACTION NEEDED", text)

    def test_stranded_month_repaired_without_new_edition(self):
        with rolled_back(self.conn) as cur:
            r = recs("202605")
            core.insert_edition(cur, ZZ, r, "202605", release_label="t",
                                published_date=FETCHED, source_file="t",
                                source_sha256=m.content_sha256(r),
                                supersedes=None)
            self.assertEqual(m.classify_month(cur, ZZ, "202605", r),
                             m.LIVE_MISSING)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):   # preview: nothing written
                m.load_months(cur, ZZ, ["202605"], fetcher({"202605": r}),
                              FETCHED, False)
            self.assertIn("would insert 296 live rows", out.getvalue())
            self.assertEqual(live(cur, "202605"), {})
            cur.execute("SAVEPOINT t")
            self.assertEqual(m.apply_month(cur, ZZ, "202605", r,
                                           fetched_on=FETCHED), m.LIVE_MISSING)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(live(cur, "202605"), as_live(r))
            self.assertEqual(m.status(cur, ZZ)["live_missing"], [])
            self.assertTrue(m.status(cur, ZZ)["ok"])
            cur.execute("ROLLBACK TO SAVEPOINT t")
            stats = {}
            with quiet():
                m.load_months(cur, ZZ, ["202605"], fetcher({"202605": r}),
                              FETCHED, False, simulate=True, stats=stats)
            self.assertEqual((stats["live_rows"], stats["stored_rows"]), (N, 0))
            self.assertIn("missing from live: 202605",
                          m.load_run_notes(stats, ["202605"], ["202605"]))
            rev = recs("202605", total=lambda i: 100 * i + 1)
            self.assertEqual(m.classify_month(cur, ZZ, "202605", rev),
                             "revised")

    def test_months_are_keys_not_labels(self):
        with self.assertRaises(ValueError):
            m.check_month_records(recs("Apr-26"), "Apr-26")
        with self.assertRaises(ValueError):
            m.fetch_month("Apr-26", FetchMonth.disc(), FetchMonth.geo(N))
        with rolled_back(self.conn) as cur:
            cur.execute("SAVEPOINT bad")
            with self.assertRaises(psycopg2.errors.CheckViolation):
                cur.execute("""INSERT INTO public.zz_s19_editions
                    (lad24cd, month, edition, pip_total_claimants,
                     pip_enhanced_daily_living)
                    VALUES ('E09000001', 'Apr-26', 1, 1, 1)""")
            cur.execute("ROLLBACK TO SAVEPOINT bad")
            # status and load see a live table of labels as unmigrated
            seed_live(cur, recs("Apr-26", n=3))
            self.assertFalse(m.months_migrated(cur, ZZ))
            st = m.status(cur, ZZ)
            self.assertFalse(st["months_migrated"])
            self.assertFalse(st["ok"])
            self.assertIn("migrate-months", m.format_status(st))

    def test_months_migrated(self):
        with rolled_back(self.conn) as cur:
            self.assertTrue(m.months_migrated(cur, ZZ))      # empty
            seed_live(cur, recs("202604", n=3))
            self.assertTrue(m.months_migrated(cur, ZZ))      # keys only
            seed_live(cur, recs("Jul-26", n=3))
            self.assertFalse(m.months_migrated(cur, ZZ))     # mixed
            cur.execute("DELETE FROM public.zz_s19_live WHERE month = '202604'")
            self.assertFalse(m.months_migrated(cur, ZZ))     # labels only

    def test_ddl_idempotent(self):
        with rolled_back(self.conn) as cur:
            m.create_schema(cur, ZZ)   # second time (rolled_back made it once)
            cur.execute("""SELECT column_name, data_type,
                                  character_maximum_length
                           FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = 'zz_s19_editions'""")
            cols = {r[0]: r[1:] for r in cur.fetchall()}
            self.assertEqual(set(cols), {
                "lad24cd", "month", "edition", "pip_total_claimants",
                "pip_enhanced_daily_living", "release_label",
                "published_date", "source_file", "source_sha256",
                "supersedes", "loaded_at"})
            self.assertEqual(cols["month"], ("character varying", 6))
            self.assertEqual(cols["pip_total_claimants"][0], "integer")
            self.assertEqual(cols["pip_enhanced_daily_living"][0], "integer")
            cur.execute("""SELECT a.attname FROM pg_index i
                           JOIN pg_attribute a ON a.attrelid = i.indrelid
                            AND a.attnum = ANY(i.indkey)
                           WHERE i.indrelid = 'public.zz_s19_editions'::regclass
                             AND i.indisprimary""")
            self.assertEqual({r[0] for r in cur.fetchall()},
                             {"lad24cd", "month", "edition"})
            cur.execute("""SELECT tgname FROM pg_trigger
                           WHERE tgrelid = 'public.zz_s19_editions'::regclass
                             AND NOT tgisinternal ORDER BY 1""")
            self.assertEqual([r[0] for r in cur.fetchall()],
                             ["zz_s19_editions_immutable",
                              "zz_s19_editions_no_truncate"])

    def test_real_spec(self):
        self.assertEqual(m.SPEC.name, "s19")
        self.assertEqual(m.SPEC.live_table, "la_pip_claimants")
        self.assertEqual(m.SPEC.editions_table, "la_pip_claimants_editions")
        self.assertEqual(m.SPEC.key_cols, ("lad24cd",))
        self.assertEqual(m.SPEC.period_col, "month")
        self.assertEqual(m.SPEC.value_cols,
                         (("pip_total_claimants", "integer"),
                          ("pip_enhanced_daily_living", "integer")))
        self.assertEqual(m.SPEC.refresh_cols,
                         ("pip_total_claimants", "pip_enhanced_daily_living"))
        self.assertTrue(m.SPEC.fk_la_boundaries)

    def test_preview_without_editions_table_compares_with_live(self):
        with rolled_back(self.conn) as cur:
            r = recs("202604")
            seed_live(cur, r)
            cmp = m.compare_month(cur, ZZ, "202604", r, against="live")
            self.assertEqual((cmp["kind"], cmp["against"]),
                             ("unchanged", "live"))
            self.assertEqual(m.compare_month(cur, ZZ, "202605",
                                             recs("202605"),
                                             against="live")["kind"], "new")


class FetchMonth(unittest.TestCase):
    """fetch_month through a patched client: two queries a month, 15 areas a
    query, 3 s between batches, never a short month, no files written."""

    @staticmethod
    def disc():
        return {"database": {"id": "str:database:PIP_Monthly_new"},
                "measure": {"id": "str:count:PIP_Monthly_new:V"},
                "geography_field_id": "GEO", "date_field_id": "DATE",
                "daily_living_field_id": "DL", "enhanced_member_id": "DL:1",
                "date_members": [{"id": "str:value:PIP:DATE2:C_PIP_DATE:202606"},
                                 {"id": "str:value:PIP:DATE2:C_PIP_DATE:202607"}]}

    @staticmethod
    def geo(n):
        lad_to_uris = {c: [f"str:value:PIP:X:{c}"] for c in CODES[:n]}
        return {"lad_to_uris": lad_to_uris,
                "all_query_uris": [u for v in lad_to_uris.values() for u in v]}

    def _cube(self, body, drop_enhanced=0):
        uris = [u[0] for u in body["recodes"]["GEO"]["map"]]
        enhanced = "DL" in body["recodes"]
        if enhanced and drop_enhanced:
            uris = uris[:max(0, len(uris) - drop_enhanced)]
        v = 7 if enhanced else 70
        return {"fields": [{"items": [{"uris": [u]} for u in uris]}],
                "cubes": {"k": {"values": [[[v]] for _ in uris]}}}

    def _run(self, n, month="202607", drop_enhanced=0):
        self.bodies = []

        def post(path, body):
            self.bodies.append(body)
            return self._cube(body, drop_enhanced)

        with mock.patch("statxplore_client.api_post", side_effect=post), \
                mock.patch.object(m.time, "sleep") as self.sleep, \
                mock.patch.object(pathlib.Path, "write_text",
                                  side_effect=AssertionError("file written")), \
                quiet():
            return m.fetch_month(month, self.disc(), self.geo(n))

    def test_full_month_two_queries_batched(self):
        r = self._run(N)
        self.assertEqual(len(r), N)
        self.assertEqual({(x["pip_total_claimants"],
                           x["pip_enhanced_daily_living"]) for x in r},
                         {(70, 7)})
        self.assertEqual({x["month"] for x in r}, {"202607"})
        batches = -(-N // 15)
        self.assertEqual(len(self.bodies), 2 * batches)
        self.assertTrue(all(len(b["recodes"]["GEO"]["map"]) <= 15
                            for b in self.bodies))
        self.assertEqual(sum("DL" in b["recodes"] for b in self.bodies), batches)
        self.assertEqual({b["recodes"]["DATE"]["map"][0][0] for b in self.bodies},
                         {"str:value:PIP:DATE2:C_PIP_DATE:202607"})
        enh = [b for b in self.bodies if "DL" in b["recodes"]][0]
        self.assertEqual(enh["recodes"]["DL"]["map"], [["DL:1"]])
        self.assertEqual(self.sleep.call_count, 2 * (batches - 1))
        self.sleep.assert_called_with(3)

    def test_295_areas_rejected(self):
        with self.assertRaises(ValueError):
            self._run(N - 1)

    def test_enhanced_measure_missing_from_response_rejected(self):
        with self.assertRaises(ValueError):
            self._run(N, drop_enhanced=15)

    def test_month_not_in_date_valueset_rejected(self):
        with self.assertRaises(ValueError):
            self._run(N, month="202608")


class Commands(unittest.TestCase):
    """The CLI on a mock connection (no database, no network)."""

    def _conn(self):
        conn = mock.MagicMock()
        return conn, conn.cursor.return_value.__enter__.return_value

    def _inserts(self, cur):
        return [c for c in cur.execute.call_args_list
                if "INSERT INTO pipeline_run_log" in str(c.args[0])]

    def _load(self, argv, kinds=None, rc=0, held=("202607",), migrated=True):
        conn, cur = self._conn()
        kinds = kinds or {}
        calls = {}

        def fake_load(cur_, spec, months, fetch, fetched_on, commit, **kw):
            kw["stats"].update(months=list(kinds), kinds=dict(kinds),
                               stored_rows=sum(N for k in kinds.values()
                                               if k != "unchanged"),
                               live_rows=sum(N for k in kinds.values()
                                             if k == "new"))
            return rc

        with mock.patch.object(m, "_conn", return_value=conn), \
                mock.patch.object(m, "months_migrated",
                                  return_value=migrated), \
                mock.patch.object(m, "table_exists", return_value=True), \
                mock.patch.object(core, "latest_map",
                                  return_value=(None, [], [])), \
                mock.patch.object(m, "held_months", return_value=list(held)), \
                mock.patch.object(m, "live_missing_months", return_value=[]), \
                mock.patch.object(m, "get_discovery",
                                  return_value={}) as calls["disc"], \
                mock.patch.object(m, "get_available_months",
                                  return_value=["202606", "202607"]) \
                as calls["avail"], \
                mock.patch.object(m, "resolve_geography",
                                  return_value={}) as calls["geo"], \
                mock.patch.object(m, "load_months",
                                  side_effect=fake_load) as calls["load"], \
                quiet():
            got = m.main(["load"] + argv)
        return got, conn, cur, calls

    def test_run_log_row_only_on_commit(self):
        rc, conn, cur, _ = self._load(["--months", "202607", "--commit"],
                                      {"202607": "unchanged"})
        self.assertEqual(rc, 0)
        (ins,) = self._inserts(cur)
        sql, args = ins.args
        self.assertIn("'success'", sql)
        agent, number, code, rows, started, notes = args
        self.assertEqual((agent, number, code, rows),
                         ("Source 19 - PIP Claimants", "19", "19", 0))
        self.assertIn("Nothing new", notes)
        self.assertIn("API latest 202607", notes)
        conn.commit.assert_called()
        rc, conn, cur, _ = self._load(["--months", "202607", "--commit"],
                                      {"202606": "unchanged", "202607": "new"})
        (ins,) = self._inserts(cur)
        self.assertEqual(ins.args[1][3], 2 * N)   # edition rows + live rows
        self.assertIn("new 202607", ins.args[1][5])
        for flags in ([], ["--simulate"]):
            rc, conn, cur, _ = self._load(["--months", "202607"] + flags,
                                          {"202607": "unchanged"})
            self.assertEqual(self._inserts(cur), [], flags)
            conn.commit.assert_not_called()
        rc, conn, cur, _ = self._load(["--months", "202607", "--commit"],
                                      {"202607": "new"}, rc=1)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self._inserts(cur), [])
        # sync-new: --commit logs and commits; --simulate rolls back
        for flag, committed in (("--commit", True), ("--simulate", False)):
            conn, cur = self._conn()
            cur.fetchone.return_value = (N,)
            with mock.patch.object(m, "_conn", return_value=conn), \
                    mock.patch.object(m, "months_migrated", return_value=True), \
                    mock.patch.object(m, "table_exists", return_value=True), \
                    mock.patch.object(core, "latest_map",
                                      return_value=(None, ["202607"], [])), \
                    mock.patch.object(core, "sync_new",
                                      return_value=["202607"]) as sync, \
                    mock.patch.object(load_checks, "check_latest_equals_live",
                                      return_value=[]), \
                    mock.patch.object(m, "status", return_value={
                        "new_periods": [], "chain_errors": {}}), \
                    quiet():
                rc = m.main(["sync-new", flag, "--expected-authorities", "296"])
            self.assertEqual(rc, 0)
            self.assertEqual(sync.call_args.args[2], 296)
            (ins,) = self._inserts(cur)
            self.assertEqual(ins.args[1][:3],
                             ("Source 19 - PIP Claimants", "19", "19"))
            self.assertEqual(conn.commit.called, committed, flag)

    def test_one_month_failure_leaves_earlier_months_committed_and_exits_non_zero(self):
        cur = mock.MagicMock()
        with mock.patch.object(m, "compare_month", return_value={
                "kind": "new", "changed": N, "examples": [],
                "against": "no edition"}), \
                mock.patch.object(m, "apply_month",
                                  side_effect=["new", RuntimeError("boom")]), \
                quiet():
            rc = m.load_months(cur, ZZ, ["202604", "202605", "202606"],
                               fetcher({mo: recs(mo) for mo in
                                        ("202604", "202605", "202606")}),
                               FETCHED, True)
        self.assertNotEqual(rc, 0)
        cur.connection.commit.assert_called_once()   # 202604 only
        self.assertIn(mock.call("ROLLBACK TO SAVEPOINT s19_month"),
                      cur.execute.call_args_list)
        cur.connection.rollback.assert_called()
        rc, conn, cur, _ = self._load(["--months", "202607", "--commit"],
                                      {"202607": "new"}, rc=1)
        self.assertEqual(rc, 1)

    def test_nothing_held_requires_months(self):
        with self.assertRaises(SystemExit) as ctx:
            self._load([], held=())
        self.assertIn("--months", str(ctx.exception.code))
        rc, conn, cur, calls = self._load(["--months", "202607"], held=())
        self.assertEqual(rc, 0)
        self.assertEqual(calls["load"].call_args.args[2], ["202607"])

    def test_load_stops_while_months_unmigrated_before_any_api_call(self):
        for flags in ([], ["--commit"], ["--months", "202607"]):
            with self.assertRaises(SystemExit) as ctx:
                self._load(flags, migrated=False)
            msg = str(ctx.exception.code)
            self.assertIn("not been migrated", msg)
            self.assertIn("migrate-months", msg)
        # and the API was never reached
        conn, cur = self._conn()
        with mock.patch.object(m, "_conn", return_value=conn), \
                mock.patch.object(m, "months_migrated", return_value=False), \
                mock.patch.object(m, "get_discovery") as disc, \
                mock.patch.object(m, "get_available_months") as avail, \
                mock.patch.object(m, "discover_schema") as ds, \
                self.assertRaises(SystemExit), quiet():
            m.main(["load"])
        disc.assert_not_called()
        avail.assert_not_called()
        ds.assert_not_called()

    def test_status_reports_unmigrated_and_missing_table(self):
        conn, cur = self._conn()
        out = io.StringIO()
        with mock.patch.object(m, "_conn", return_value=conn), \
                mock.patch.object(m, "months_migrated", return_value=False), \
                mock.patch.object(m, "table_exists", return_value=False), \
                contextlib.redirect_stdout(out):
            rc = m.main(["status"])
        self.assertEqual(rc, 1)
        self.assertIn("migrate-months", out.getvalue())
        self.assertIn("does not exist yet", out.getvalue())

    def test_migrate_months_preview_rolls_back_and_commit_commits_once(self):
        report = {"status": "migrated", "table": "la_pip_claimants",
                  "committed": False,
                  "mapping": {"Apr-26": "202604", "Jul-26": "202607"},
                  "reverse": {"202604": "Apr-26", "202607": "Jul-26"},
                  "latest_label": "Jul-26", "latest_key": "202607",
                  "before": {"rows": 592, "months": ["Apr-26", "Jul-26"],
                             "per_month": {
                                 "Apr-26": {"rows": 296, "sum_total": 1,
                                            "sum_enhanced": 1,
                                            "null_total": 0,
                                            "null_enhanced": 0},
                                 "Jul-26": {"rows": 296, "sum_total": 2,
                                            "sum_enhanced": 2,
                                            "null_total": 0,
                                            "null_enhanced": 0}},
                             "latest": {}},
                  "after": None}
        report["after"] = {
            "rows": 592, "months": ["202604", "202607"], "latest": {},
            "per_month": {report["mapping"][k]: v for k, v in
                          report["before"]["per_month"].items()}}
        for flags, committed in (([], False), (["--simulate"], False),
                                 (["--commit"], True)):
            conn, cur = self._conn()
            order = []
            cur.execute.side_effect = lambda sql, *a: order.append(sql)

            def mig(cur_, table, commit=False):
                order.append(("migrate", table, commit))
                return dict(report, committed=commit)

            out = io.StringIO()
            with mock.patch.object(m, "_conn", return_value=conn), \
                    mock.patch.object(m, "migrate_months", side_effect=mig), \
                    contextlib.redirect_stdout(out):
                rc = m.main(["migrate-months"] + flags)
            self.assertEqual(rc, 0)
            self.assertEqual(order[0], "LOCK TABLE public.la_pip_claimants "
                                       "IN SHARE ROW EXCLUSIVE MODE")
            self.assertEqual(order[1], ("migrate", "la_pip_claimants",
                                        committed))
            self.assertEqual(conn.commit.call_count, 1 if committed else 0)
            if not committed:
                conn.rollback.assert_called()
            text = out.getvalue()
            self.assertIn("Apr-26 -> 202604", text)
            self.assertIn("Jul-26 -> 202607", text)
            self.assertIn("PASS", text)

    def test_commit_and_simulate_mutually_exclusive(self):
        for cmd in (["load"], ["sync-new"], ["refresh-latest"], ["ddl"],
                    ["migrate-months"]):
            with self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                m.main(cmd + ["--commit", "--simulate"])
            self.assertEqual(ctx.exception.code, 2)

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


if __name__ == "__main__":
    unittest.main()
