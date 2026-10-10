"""Tests for the S6 loader in s6_asylum_editions (the commands,
migrate-legacy, restore-edition) on throwaway tables.

First, the engine check of the S6 plan (Task 1, step 2): a spec whose
whole_period_cols source is the editions metadata column release_label
passes EditionSpec validation, and refresh_latest sets the live column on
every row of the period (MetaWholePeriod; no engine change was needed).

No network: the content API replies and the downloads are stubbed, and every
workbook is written by the tests (the writers in test_s6_asylum_pure).
Database tests run on a connection from _db.get_conn() inside a transaction
that is always rolled back (rolled_back below); nothing here commits. The
commands get a stand-in connection whose commit and rollback act on a
savepoint of that transaction, and a cursor proxy whose .connection does the
same, so the loader's per-period commit never reaches the real connection.
The only tables written are zz_s6_* (LIKE copies of the four live tables,
their editions tables and ledgers), created inside that transaction (refused
if any already exists), so they never persist. The run log is stubbed; the
real la_code_lookup and la_boundaries are read (never written).
"""
import contextlib
import dataclasses
import io
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s6_asylum_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s6_asylum_pure import (NA_SUB, Q3, Q4, d11_rows,  # noqa: E402
                                 reg02_body,
                                 regional_page, tables_page, write_d09,
                                 write_d11, write_reg02)

META = core.EditionSpec(
    name="zz_s6_meta",
    live_table="zz_s6_meta_live",
    editions_table="zz_s6_meta_editions",
    key_cols=("lad24cd", "support_type"),
    period_col="period_ending",
    value_cols=(("people", "integer NOT NULL"),),
    refresh_cols=("people", "source_edition", "loaded_at"),
    refresh_from=(("source_edition", "release_label"),
                  ("loaded_at", "loaded_at")),
    whole_period_cols=("source_edition",),
    key_types=(("lad24cd", "text NOT NULL"),
               ("support_type", "text NOT NULL"),
               ("period_ending", "date NOT NULL")),
    fk_la_boundaries=False,
    refresh_key_changes=True,
)


@contextmanager
def meta_tables(conn):
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_s6_meta_live'), "
                    "to_regclass('public.zz_s6_meta_editions')")
        if any(cur.fetchone()):
            raise RuntimeError("a zz_s6_meta table already exists; refusing")
        cur.execute("CREATE TABLE public.zz_s6_meta_live (period_ending date "
                    "NOT NULL, lad24cd text NOT NULL, support_type text NOT "
                    "NULL, people integer NOT NULL, source_edition text NOT "
                    "NULL, loaded_at timestamptz NOT NULL DEFAULT now())")
        core.create_schema(cur, META)
        yield cur
    finally:
        cur.close()
        conn.rollback()


class MetaWholePeriod(unittest.TestCase):
    """A META column (release_label) as the editions side of a whole-period
    pair: validation passes and refresh_latest sets the live column on every
    row of the refreshed period."""

    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_validation_passes(self):
        self.assertEqual(META.whole_period_pairs,
                         (("source_edition", "release_label"),))

    def test_refresh_sets_the_label_on_every_row(self):
        with meta_tables(self.conn) as cur:
            p = date(2026, 6, 30)
            rows = [("E06000001", "Section 95", 10),
                    ("E06000001", "Section 4", 3),
                    ("E06000002", "Section 95", 7)]
            for lad, st, n in rows:
                cur.execute("INSERT INTO public.zz_s6_meta_live (period_ending,"
                            " lad24cd, support_type, people, source_edition) "
                            "VALUES (%s, %s, %s, %s, 'year ending June 2026')",
                            (p, lad, st, n))
            recs = [{"lad24cd": lad, "support_type": st, "people": n}
                    for lad, st, n in rows]
            core.insert_edition(cur, META, recs, p,
                                release_label="year ending June 2026",
                                published_date=date(2026, 8, 27),
                                source_file="a", source_sha256="1",
                                supersedes=None)
            recs2 = [dict(r) for r in recs]
            recs2[0]["people"] = 11            # one row revised
            core.insert_edition(cur, META, recs2, p,
                                release_label="year ending September 2026",
                                published_date=date(2026, 11, 26),
                                source_file="b", source_sha256="2",
                                supersedes=1)
            cur.execute("UPDATE public.zz_s6_meta_live SET loaded_at = "
                        "'2026-09-04 18:20:20+00'")
            res = core.refresh_latest(cur, META)
            self.assertEqual(res["rows"], 1)
            cur.execute("SELECT DISTINCT source_edition FROM "
                        "public.zz_s6_meta_live")
            self.assertEqual(cur.fetchall(), [("year ending September 2026",)])
            cur.execute("SELECT COUNT(*) FROM public.zz_s6_meta_live l JOIN "
                        "public.zz_s6_meta_editions e USING (period_ending, "
                        "lad24cd, support_type) WHERE e.edition = 2 AND "
                        "l.loaded_at = e.loaded_at")
            self.assertEqual(cur.fetchone()[0], 1)    # the updated row


# ---------------------------------------------------------------------------
# The S6 loader on throwaway tables
# ---------------------------------------------------------------------------

LIVE = {"support": "la_asylum_support",
        "unallocated": "la_asylum_support_unallocated",
        "non_england": "asylum_support_non_england",
        "groups": "la_immigration_groups"}
ZZ = {t: dataclasses.replace(
    getattr(m, m.SPEC_NAMES[t]), name=f"zz_s6_{t}", live_table=f"zz_s6_{t}",
    editions_table=f"zz_s6_{t}_editions",
    expected_rows_per_period=m.tip_row_count(f"zz_s6_{t}_editions"))
    for t in m.TAGS}
ED = {t: f"zz_s6_{t}_editions" for t in m.TAGS}
LEDGER = {t: f"zz_s6_{t}_editions_file_checks" for t in m.TAGS}
ALL = ([f"zz_s6_{t}" for t in m.TAGS] + list(ED.values())
       + list(LEDGER.values()))
JUNE = "year ending June 2026"
MARCH = "year ending March 2026"


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    """commit / rollback of a command, acting on a savepoint of the test's
    own transaction."""
    SP = "zz_s6_cmd"

    def __init__(self, cur):
        self.cur = cur
        cur.execute(f"SAVEPOINT {self.SP}")

    def commit(self):
        self.cur.execute(f"RELEASE SAVEPOINT {self.SP}")
        self.cur.execute(f"SAVEPOINT {self.SP}")

    def rollback(self):
        self.cur.execute(f"ROLLBACK TO SAVEPOINT {self.SP}")

    def done(self):
        self.cur.execute(f"RELEASE SAVEPOINT {self.SP}")


class _FakeConn:
    def __init__(self, real, tx):
        self.encoding = real.encoding
        self.tx = tx

    def commit(self):
        self.tx.commit()

    def rollback(self):
        self.tx.rollback()


class _Cur:
    def __init__(self, cur, tx):
        self._cur = cur
        self.connection = _FakeConn(cur.connection, tx)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _Borrowed:
    def __init__(self, cur):
        self.tx = _Tx(cur)
        self.cur = _Cur(cur, self.tx)

    @contextmanager
    def cursor(self):
        yield self.cur

    def commit(self):
        self.tx.commit()

    def rollback(self):
        self.tx.rollback()

    def close(self):
        pass


@contextmanager
def specs():
    with contextlib.ExitStack() as st:
        for t in m.TAGS:
            st.enter_context(mock.patch.object(m, m.SPEC_NAMES[t], ZZ[t]))
        st.enter_context(mock.patch.object(m, "REG02_LAS", 4))
        yield


@contextmanager
def rolled_back(conn, *, ddl=True):
    cur = conn.cursor()
    try:
        cur.execute("SELECT " + ", ".join(f"to_regclass('public.{t}')"
                                          for t in ALL))
        if any(cur.fetchone()):
            raise RuntimeError("a zz_s6 table already exists as a real table; "
                               "refusing to run")
        for t in m.TAGS:
            cur.execute(f"CREATE TABLE public.zz_s6_{t} (LIKE "
                        f"public.{LIVE[t]} INCLUDING ALL)")
        with specs():
            if ddl:
                m.create_all(cur)
            yield cur
    finally:
        cur.close()
        conn.rollback()


def count(cur, table, where="", args=None):
    cur.execute(f"SELECT COUNT(*) FROM public.{table} {where}", args)
    return cur.fetchone()[0]


def editions(cur, tag, period):
    """[(edition, supersedes, rows)]."""
    cur.execute(f"SELECT edition, supersedes, COUNT(*) FROM public.{ED[tag]} "
                "WHERE period_ending = %s GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur, tag):
    """[(period, source_file, outcome, edition)] in order."""
    cur.execute(f"SELECT period_ending::text, source_file, outcome, edition "
                f"FROM public.{LEDGER[tag]} ORDER BY id")
    return cur.fetchall()


def labels(cur, tag, period):
    cur.execute(f"SELECT DISTINCT source_edition FROM public.zz_s6_{tag} "
                "WHERE period_ending = %s", (period,))
    return sorted(r[0] for r in cur.fetchall())


def live_people(cur, code, period, sup="Section 95",
                acc="Dispersal Accommodation"):
    cur.execute("SELECT people FROM public.zz_s6_support WHERE lad24cd = %s "
                "AND period_ending = %s AND support_type = %s AND "
                "accommodation_type = %s", (code, period, sup, acc))
    r = cur.fetchone()
    return r[0] if r else None


def zz_counts(cur):
    return {t: count(cur, t) for t in ALL}


class Fixture(unittest.TestCase):
    """Files written by the test; the content API and downloads stubbed."""

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
        self.root = Path(self.tmp.name)
        self.tables = tables_page(d11=None, d09=None)
        self.regional = regional_page([])
        self.api = {m.TABLES_PATH: self.tables, m.REGIONAL_PATH: self.regional}
        self.urls, self.final, self.fetched = {}, {}, []
        self.n = 0

    def _set_att(self, page, prefix, title, url):
        atts = page["details"]["attachments"]
        atts[:] = [a for a in atts if not a["title"].startswith(prefix)]
        atts.append({"title": title, "url": url})

    def d11(self, quarters=Q4, *, register=True, url=None, published=None,
            **kw):
        """Write an Asy_D11 and its Asy_D09; list both on the data tables
        page (it lists only the newest of each)."""
        self.n += 1
        d = date.fromisoformat(quarters[-1])
        tag = f"{d:%b-%Y}".lower()
        rows = d11_rows(quarters, **kw)
        p11 = write_d11(self.root / f"d{self.n}" /
                        f"support-local-authority-datasets-{tag}.xlsx",
                        quarters, rows=rows, published=published)
        p09 = write_d09(self.root / f"d{self.n}" /
                        f"asylum-seekers-receipt-support-datasets-{tag}.xlsx",
                        quarters, rows=rows)
        if register:
            u11 = url or f"https://assets.example/media/m{self.n}/{p11.name}"
            u09 = f"https://assets.example/media/n{self.n}/{p09.name}"
            self.urls[u11], self.urls[u09] = p11, p09
            ye = f"{d:%B %Y}"
            self._set_att(self.tables, "Asylum seekers in receipt of Home "
                          "Office support by local authority",
                          "Asylum seekers in receipt of Home Office support "
                          "by local authority detailed datasets, year ending "
                          f"{ye}", u11)
            self._set_att(self.tables, "Asylum seekers in receipt of Home "
                          "Office support detailed",
                          "Asylum seekers in receipt of Home Office support "
                          f"detailed datasets, year ending {ye}", u09)
            self.u11 = u11
        return p11, p09

    def reg(self, period="2026-06-30", *, register=True, url=None,
            published=None, **kw):
        self.n += 1
        d = date.fromisoformat(period)
        name = f"regional-and-local-authority-dataset-{d:%b-%Y}.ods".lower()
        p = write_reg02(self.root / f"r{self.n}" / name, period,
                        published=published, **kw)
        if register:
            u = url or f"https://assets.example/media/r{self.n}/{name}"
            self.urls[u] = p
            ye = f"{d:%B %Y}"
            atts = self.regional["details"]["attachments"]
            atts[:] = [a for a in atts if not a["title"].endswith(ye)]
            atts.append({"title": "Regional and local authority data on "
                         f"immigration groups, year ending {ye}", "url": u})
            self.ureg = u
        return p

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, log)."""
        borrowed = _Borrowed(cur)

        def fetch(url, dest, session=None, **kw):
            self.fetched.append(url)
            return self.urls[url], self.final.get(url, url)

        out = io.StringIO()
        with specs(), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda p, s=None: self.api[p]), \
                mock.patch.object(m, "fetch", side_effect=fetch), \
                mock.patch.object(m, "log_run") as logged, \
                contextlib.redirect_stdout(out):
            try:
                rc = m.main([str(a) for a in argv])
            except SystemExit as e:
                rc = "halt"
                out.write(f"\n{e.code}")
            finally:
                borrowed.tx.done()
        return rc, out.getvalue(), logged

    def ok(self, cur, argv):
        rc, text, logged = self.run_main(cur, argv)
        self.assertEqual(rc, 0, text)
        return text, logged

    def seed(self, cur, quarters=Q3, **kw):
        """Load an Asy_D11 release (page path, with its Asy_D09)."""
        paths = self.d11(quarters, **kw)
        self.ok(cur, ["load", "--only", "d11", "--commit"])
        self.fetched.clear()
        return paths


class Load(Fixture):

    def test_new_quarter_in_one_transaction_across_the_three_tables(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.assertEqual(editions(cur, "support", "2026-03-31"),
                             [(1, None, 5)])
            self.assertEqual(editions(cur, "unallocated", "2025-09-30"),
                             [(1, None, 1)])
            self.d11(Q4)
            text, logged = self.ok(cur, ["load", "--only", "d11", "--commit"])
            self.assertIn("2026-06-30: support new", text)
            for tag, n in (("support", 5), ("non_england", 2)):
                self.assertEqual(editions(cur, tag, "2026-06-30"),
                                 [(1, None, n)])
                self.assertEqual(count(cur, f"zz_s6_{tag}", "WHERE "
                                       "period_ending = '2026-06-30'"), n)
                self.assertEqual(labels(cur, tag, "2026-06-30"), [JUNE])
                rows = [x for x in ledger(cur, tag) if x[0] == "2026-06-30"]
                self.assertEqual([(o, e) for _, _, o, e in rows],
                                 [("new", 1)])
                self.assertTrue(rows[0][1].startswith(self.u11 + " (year "
                                                      "ending June 2026; "))
            # a quarter with no unallocated rows stores nothing there
            self.assertEqual(editions(cur, "unallocated", "2026-06-30"), [])
            self.assertEqual([x for x in ledger(cur, "unallocated")
                              if x[0] == "2026-06-30"], [])
            # the held quarters are unchanged: ledger rows only
            self.assertEqual(editions(cur, "support", "2026-03-31"),
                             [(1, None, 5)])
            self.assertEqual(labels(cur, "support", "2026-03-31"), [MARCH])
            self.assertEqual([o for p, _, o, _ in ledger(cur, "support")
                              if p == "2026-03-31"], ["new", "unchanged"])
            cur.execute(f"SELECT DISTINCT release_label, published_date, "
                        f"source_file FROM public.{ED['support']} WHERE "
                        "period_ending = '2026-06-30'")
            (lab, pd_, sf), = cur.fetchall()
            self.assertEqual((lab, pd_), (JUNE, date(2026, 8, 21)))
            self.assertEqual(m.release_rank(sf),
                             (date(2026, 6, 30), date(2026, 8, 21)))
            logged.assert_called_once()
            self.assertIn("2026-06-30 support new, non_england new",
                          logged.call_args[0][2])
            for t in m.D11_TAGS:
                self.assertTrue(pe.status(cur, m.profile(t))["ok"], t)

    def test_a_failing_ledger_insert_rolls_back_all_three(self):
        with rolled_back(self.conn) as cur:
            self.d11(Q3)
            real = pe.record_file_check

            def boom(c, prof, *a, **kw):
                if prof.spec.name == "zz_s6_non_england":
                    raise RuntimeError("boom")
                return real(c, prof, *a, **kw)
            with mock.patch.object(pe, "record_file_check", side_effect=boom):
                rc, text, logged = self.run_main(cur, ["load", "--only", "d11",
                                                       "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2025-09-30: FAILED", text)
            self.assertIn("not attempted: 2025-12-31, 2026-03-31", text)
            for t in m.D11_TAGS:
                self.assertEqual(count(cur, ED[t]), 0, t)
                self.assertEqual(count(cur, f"zz_s6_{t}"), 0, t)
                self.assertEqual(ledger(cur, t), [], t)
            logged.assert_called_once()
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN", notes)
            self.assertIn("failed 2025-09-30", notes)
            self.assertIn("not attempted ['2025-12-31', '2026-03-31']", notes)

    def test_a_held_unallocated_period_missing_from_a_file_is_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.d11(Q4, unallocated=())
            rc, text, logged = self.run_main(cur, ["load", "--only", "d11",
                                                   "--commit",
                                                   "--acknowledge",
                                                   "2025-09-30"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2025-09-30: REJECTED", text)
            self.assertIn("zz_s6_unallocated: the file has no rows", text)
            self.assertIn("2026-06-30: support new", text)
            self.assertEqual(editions(cur, "support", "2025-09-30"),
                             [(1, None, 5)])
            self.assertEqual([p for p, _, _, _ in ledger(cur, "unallocated")],
                             ["2025-09-30"])
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN (exit 1): rejected 2025-09-30", notes)
            self.assertIn("zz_s6_unallocated", notes)

    def test_a_small_unallocated_restatement_is_a_key_change_not_partial(self):
        # the publisher reassigns one of three unallocated rows: a key change
        # the period's --acknowledge releases, not a PARTIAL FILE
        extra = [["30 Sep 2025", "Section 4", NA_SUB, NA_SUB, NA_SUB,
                  "Subsistence Only", 1],
                 ["30 Sep 2025", "Section 98", NA_SUB, NA_SUB, NA_SUB,
                  "Subsistence Only", 1]]
        with rolled_back(self.conn) as cur:
            self.seed(cur, add=extra)
            self.assertEqual(editions(cur, "unallocated", "2025-09-30"),
                             [(1, None, 3)])
            self.d11(Q4, add=extra[:1])
            rc, text, _ = self.run_main(cur, ["load", "--only", "d11",
                                              "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2025-09-30: REJECTED", text)
            self.assertIn("zz_s6_unallocated: 1 of 3 rows removed", text)
            self.assertNotIn(m.PARTIAL, text)
            self.assertEqual(editions(cur, "unallocated", "2025-09-30"),
                             [(1, None, 3)])
            text, _ = self.ok(cur, ["load", "--only", "d11", "--commit",
                                    "--acknowledge", "2025-09-30"])
            self.assertIn("unallocated revised, 1 rows changed", text)
            self.assertEqual(editions(cur, "unallocated", "2025-09-30"),
                             [(1, None, 3), (2, 1, 2)])

    def test_a_new_period_breaking_a_stop_condition_is_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.d11(Q4, values={("2026-06-30", "E06000001", "Section 95"):
                                 2000})
            rc, text, logged = self.run_main(cur, ["load", "--only", "d11",
                                                   "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2026-06-30: REJECTED", text)
            self.assertIn("more than 1,500 people", text)
            self.assertEqual(editions(cur, "support", "2026-06-30"), [])
            self.assertEqual(editions(cur, "non_england", "2026-06-30"), [])
            self.assertEqual([x for x in ledger(cur, "support")
                              if x[0] == "2026-06-30"], [])
            notes = logged.call_args[0][2]
            self.assertIn("rejected 2026-06-30", notes)
            self.assertIn("zz_s6_support", notes)
            # named, it is stored and logged as acknowledged
            text, logged = self.ok(cur, ["load", "--only", "d11", "--commit",
                                         "--acknowledge", "2026-06-30"])
            self.assertIn("ACKNOWLEDGED", text)
            self.assertEqual(editions(cur, "support", "2026-06-30"),
                             [(1, None, 5)])
            self.assertIn("ACKNOWLEDGED 2026-06-30", logged.call_args[0][2])

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            cur.execute("SELECT COUNT(*) FROM pipeline_run_log")
            runs = cur.fetchone()[0]
            self.d11(Q3)
            self.reg("2026-03-31")
            text, logged = self.ok(cur, ["load"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            self.assertEqual(set(zz_counts(cur).values()), {0})
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--simulate"])
            self.assertIn("SIMULATION", text)
            self.assertEqual(set(zz_counts(cur).values()), {0})
            logged.assert_not_called()
            self.ok(cur, ["load", "--commit"])
            before = zz_counts(cur)
            calls = []
            real = m.read_d11
            with mock.patch.object(m, "read_d11", side_effect=lambda p:
                                   calls.append(p) or real(p)):
                text, logged = self.ok(cur, ["load", "--commit"])
            self.assertEqual(calls, [])
            self.assertEqual(text.count("nothing parsed"), 2)
            logged.assert_not_called()
            self.assertEqual(zz_counts(cur), before)
            cur.execute("SELECT COUNT(*) FROM pipeline_run_log")
            self.assertEqual(cur.fetchone()[0], runs)

    def test_unchanged_recheck_records_the_ledger_only_on_commit(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            n = len(ledger(cur, "support"))
            text, _ = self.ok(cur, ["load", "--only", "d11", "--recheck",
                                    "2026-03-31"])
            self.assertIn("2026-03-31: support unchanged", text)
            self.assertIn("2025-09-30: support unchanged", text)
            self.assertEqual(len(ledger(cur, "support")), n)
            self.ok(cur, ["load", "--only", "d11", "--recheck", "2026-03-31",
                          "--commit"])
            self.assertEqual([o for p, _, o, _ in ledger(cur, "support")
                              if p == "2026-03-31"], ["new", "unchanged"])
            self.assertEqual(editions(cur, "support", "2026-03-31"),
                             [(1, None, 5)])

    def test_ledger_skip_needs_the_pair_for_every_period_in_every_table(self):
        with rolled_back(self.conn) as cur:
            p11, _ = self.d11(Q3)
            sha = m.content_sha256(p11)
            f = m.read_d11(p11)
            by = m.build_d11_records(f, lambda c: {"E08000038": "E08000016"}
                                     .get(c, c))[0]
            src = m.ledger_source(self.u11, f["cover"], {
                t: [p for p in sorted(by) if by[p][t]] for t in m.D11_TAGS})
            for t in m.D11_TAGS:
                for p in sorted(by):
                    if not by[p][t] or (t == "non_england"
                                        and p == "2026-03-31"):
                        continue
                    cur.execute(f"INSERT INTO public.{LEDGER[t]} "
                                "(period_ending, source_file, file_sha256, "
                                "outcome, edition) VALUES (%s, %s, %s, "
                                "'new', 1)", (p, src, sha))
            text, _ = self.ok(cur, ["load", "--only", "d11"])
            self.assertNotIn("nothing parsed", text)
            self.assertIn("2026-03-31: support new", text)
            cur.execute(f"INSERT INTO public.{LEDGER['non_england']} "
                        "(period_ending, source_file, file_sha256, outcome, "
                        "edition) VALUES ('2026-03-31', %s, %s, 'new', 1)",
                        (src, sha))
            text, _ = self.ok(cur, ["load", "--only", "d11"])
            self.assertIn("nothing parsed", text)

    def revise(self, **kw):
        return self.d11(Q4, values={("2025-12-31", "E06000001",
                                     "Section 95"): 104}, **kw)

    def test_revised_period_waits_for_refresh_latest(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("UPDATE public.zz_s6_support SET loaded_at = "
                        "'2026-01-01'")
            self.assertEqual(live_people(cur, "E06000001", "2025-12-31"), 101)
            self.revise()
            text, _ = self.ok(cur, ["load", "--only", "d11", "--commit"])
            self.assertIn("2025-12-31: support revised, 1 rows changed", text)
            self.assertEqual(editions(cur, "support", "2025-12-31"),
                             [(1, None, 5), (2, 1, 5)])
            self.assertEqual(editions(cur, "non_england", "2025-12-31"),
                             [(1, None, 2)])
            self.assertEqual(live_people(cur, "E06000001", "2025-12-31"), 101)
            self.assertEqual(pe.status(cur, m.profile("support"))
                             ["pending_refresh"], ["2025-12-31"])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("2025-12-31=1", text)
            self.assertEqual(live_people(cur, "E06000001", "2025-12-31"), 101)
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_people(cur, "E06000001", "2025-12-31"), 104)
            self.assertEqual(labels(cur, "support", "2025-12-31"), [JUNE])
            self.assertEqual(count(cur, "zz_s6_support", "WHERE period_ending "
                                   "= '2025-12-31'"), 5)
            self.assertEqual(labels(cur, "support", "2025-09-30"), [MARCH])
            self.assertEqual(labels(cur, "non_england", "2025-12-31"), [MARCH])
            cur.execute(f"SELECT loaded_at FROM public.{ED['support']} WHERE "
                        "edition = 2 LIMIT 1")
            ed_loaded = cur.fetchone()[0]
            cur.execute("SELECT loaded_at FROM public.zz_s6_support WHERE "
                        "lad24cd = 'E06000001' AND period_ending = "
                        "'2025-12-31' AND support_type = 'Section 95'")
            self.assertEqual(cur.fetchone()[0], ed_loaded)
            self.assertTrue(pe.status(cur, m.profile("support"))["ok"])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertEqual(text.count("would write: none"), 4)

    def test_key_changes_need_accept_key_changes(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            q = "2025-12-31"
            self.d11(Q4, drop=[(q, "E07000223", "Section 95")], add=[[
                "31 Dec 2025", "Section 95", "North East",
                "Redcar and Cleveland", "E06000003",
                "Dispersal Accommodation", 6]])
            rc, text, _ = self.run_main(cur, ["load", "--only", "d11",
                                              "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("rows added", text)
            text, _ = self.ok(cur, ["load", "--only", "d11", "--commit",
                                    "--acknowledge", q])
            self.assertIn(f"{q}: support revised", text)
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("keys added 1 (E06000003/Section 95/Dispersal "
                          "Accommodation); removed 1 (E07000223/Section 95/"
                          "Subsistence Only)", text)
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("not named", text)
            self.assertIsNone(live_people(cur, "E06000003", q))
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit",
                                              "--accept-key-changes",
                                              "2025-09-30"])
            self.assertEqual(rc, "halt", text)
            text, _ = self.ok(cur, ["refresh-latest", "--commit",
                                    "--accept-key-changes", q])
            self.assertIn("1 inserted, 1 deleted", text)
            self.assertEqual(live_people(cur, "E06000003", q), 6)
            self.assertEqual(labels(cur, "support", q), [JUNE])
            self.assertTrue(pe.status(cur, m.profile("support"))["ok"])

    def older(self, cur, how):
        """Seed from the June release, then offer the March file (with a
        different value) by the page, --release or --file."""
        self.seed(cur, Q4)
        p11, p09 = self.d11(Q3, register=how != "file",
                            values={("2025-12-31", "E06000001",
                                     "Section 95"): 104})
        argv = ["load", "--only", "d11", "--commit"]
        if how == "release":
            argv += ["--release", "March 2026"]
        if how == "file":
            argv += ["--file", p11, "--d09-file", p09]
        return argv

    def test_an_older_file_is_skipped_and_halts_on_every_path(self):
        for how in ("page", "release", "file"):
            with self.subTest(how=how), rolled_back(self.conn) as cur:
                argv = self.older(cur, how)
                n = len(ledger(cur, "support"))
                rc, text, logged = self.run_main(cur, argv)
                self.assertEqual(rc, "halt", text)
                self.assertIn("2025-12-31 (older", text)
                self.assertIn("older file", text)
                self.assertEqual(editions(cur, "support", "2025-12-31"),
                                 [(1, None, 5)])
                self.assertEqual(len(ledger(cur, "support")), n)
                logged.assert_not_called()

    def test_allow_older_file_overrides_and_is_logged(self):
        with rolled_back(self.conn) as cur:
            argv = self.older(cur, "file") + ["--allow-older-file"]
            text, logged = self.ok(cur, argv)
            self.assertEqual(editions(cur, "support", "2025-12-31"),
                             [(1, None, 5), (2, 1, 5)])
            self.assertIn("--allow-older-file given", logged.call_args[0][2])
            cur.execute(f"SELECT DISTINCT release_label, source_file FROM "
                        f"public.{ED['support']} WHERE edition = 2")
            lab, sf = cur.fetchone()
            self.assertIn("--allow-older-file", lab)
            self.assertIn("--allow-older-file given", sf)

    def test_a_stranded_period_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("DELETE FROM public.zz_s6_support WHERE "
                        "period_ending = '2026-03-31'")
            self.assertEqual(pe.status(cur, m.profile("support"))
                             ["live_missing"], ["2026-03-31"])
            text, _ = self.ok(cur, ["load", "--only", "d11", "--commit"])
            self.assertIn("live-missing", text)
            self.assertEqual(count(cur, "zz_s6_support", "WHERE period_ending "
                                   "= '2026-03-31'"), 5)
            self.assertEqual(labels(cur, "support", "2026-03-31"), [MARCH])
            self.assertEqual(editions(cur, "support", "2026-03-31"),
                             [(1, None, 5)])
            self.assertTrue(pe.status(cur, m.profile("support"))["ok"])

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.d11(Q3)
            final = "https://assets.example/media/new/support-la-new.xlsx"
            self.final[self.u11] = final
            self.ok(cur, ["load", "--only", "d11", "--commit"])
            srcs = {s for _, s, _, _ in ledger(cur, "support")}
            self.assertEqual(len(srcs), 1)
            self.assertTrue(srcs.pop().startswith(final + " (year ending"))
            cur.execute(f"SELECT DISTINCT source_file FROM "
                        f"public.{ED['support']}")
            self.assertTrue(cur.fetchone()[0].startswith(final + "; year "))

    def test_release_for_asy_d11_must_name_the_newest_listed(self):
        with rolled_back(self.conn) as cur:
            self.d11(Q4)
            rc, text, _ = self.run_main(cur, ["load", "--only", "d11",
                                              "--release", "March 2026"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("lists only the newest Asy_D11 (year ending June "
                          "2026)", text)
            rc, text, _ = self.run_main(cur, ["load", "--release",
                                              "June 2026"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--only", text)
            text, _ = self.ok(cur, ["load", "--only", "d11", "--release",
                                    "June 2026"])
            self.assertIn("2026-06-30: support new", text)

    def test_discovery_fails_loudly_and_names_what_it_saw(self):
        with rolled_back(self.conn) as cur:
            self.d11(Q3)
            self.tables["details"]["attachments"].append({
                "title": "Asylum seekers in receipt of support by local "
                         "authority detailed datasets, year ending June 2026",
                "url": "x/y.xlsx"})
            rc, text, _ = self.run_main(cur, ["load", "--only", "d11"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("year ending June 2026", text)
            self.assertIn("do not fit", text)
            self.tables["details"]["attachments"].pop()
            self.tables["title"] = "Immigration statistics tables"
            rc, text, _ = self.run_main(cur, ["load", "--only", "d11"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("page title", text)

    def test_the_overdue_warning(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, Q4)
            self.reg("2026-06-30")
            self.ok(cur, ["load", "--only", "reg02", "--commit"])
            with mock.patch.object(m, "_today",
                                   return_value=date(2026, 11, 26)):
                text, _ = self.ok(cur, ["load"])
            self.assertIn("newest Asy_D11 release the page lists: year ending "
                          "June 2026 (period 2026-06-30); already held", text)
            self.assertNotIn("WARNING", text)
            with mock.patch.object(m, "_today",
                                   return_value=date(2026, 11, 27)):
                text, _ = self.ok(cur, ["load"])
            self.assertIn("WARNING: the next release was expected on 26 "
                          "November 2026", text)

    def test_file_needs_d09_or_no_d09_and_no_page_is_recorded(self):
        with rolled_back(self.conn) as cur:
            p11, p09 = self.d11(Q3, register=False)
            rc, text, _ = self.run_main(cur, ["load", "--file", p11])
            self.assertEqual(rc, "halt", text)      # the page lists no Asy_D11
            self.assertIn("--no-page", text)
            self.d11(Q4)
            rc, text, _ = self.run_main(cur, ["load", "--file", p11])
            self.assertEqual(rc, "halt", text)
            self.assertIn("lists year ending June 2026 as the newest", text)
            self.assertIn("--d09-file", text)
            text, logged = self.ok(cur, ["load", "--file", p11, "--no-page",
                                         "--no-d09", "--commit"])
            self.assertIn("2026-03-31: support new", text)
            self.assertIn("not done: --no-d09", logged.call_args[0][2])
            srcs = {s for _, s, _, _ in ledger(cur, "support")}
            self.assertTrue(srcs.pop().startswith(f"file:{p11.name} (year"))
            rc, text, _ = self.run_main(cur, ["load", "--no-page"])
            self.assertEqual(rc, "halt", text)


class Reg02(Fixture):

    def test_new_snapshot_reconciled_with_held_asy_d11(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, Q4)
            self.reg("2026-06-30")
            text, logged = self.ok(cur, ["load", "--only", "reg02",
                                         "--commit"])
            self.assertIn("supported_asylum against Asy_D11: the held Asy_D11 "
                          "rows", text)
            self.assertIn("2026-06-30: groups new", text)
            self.assertEqual(editions(cur, "groups", "2026-06-30"),
                             [(1, None, 48)])
            self.assertEqual(labels(cur, "groups", "2026-06-30"), [JUNE])
            cur.execute("SELECT people, suppressed, source_marker FROM "
                        "public.zz_s6_groups WHERE lad24cd = 'E07000223' AND "
                        "pathway = 'homes_for_ukraine'")
            self.assertEqual(cur.fetchone(), (None, True, "*"))
            # a supported asylum figure off against Asy_D11 halts
            self.reg("2026-06-30", values={"E06000001": {5: 999, 7: 999}})
            rc, text, _ = self.run_main(cur, ["load", "--only", "reg02"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("E06000001: Reg_02 supported_asylum 999", text)

    def test_a_backfill_of_an_earlier_snapshot_needs_allow_older_file(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, Q4)
            self.reg("2026-06-30")
            self.ok(cur, ["load", "--only", "reg02", "--commit"])
            self.reg("2026-03-31")
            argv = ["load", "--only", "reg02", "--commit", "--release",
                    "March 2026"]
            rc, text, logged = self.run_main(cur, argv)
            self.assertEqual(rc, "halt", text)
            self.assertIn("back-fill", text)
            self.assertIn("--allow-older-file", text)
            self.assertEqual(editions(cur, "groups", "2026-03-31"), [])
            logged.assert_not_called()
            self.ok(cur, argv + ["--allow-older-file"])
            self.assertEqual(editions(cur, "groups", "2026-03-31"),
                             [(1, None, 48)])

    def test_equal_rank_with_different_content_needs_accept_reissue(self):
        with rolled_back(self.conn) as cur:
            self.reg("2026-06-30")
            self.ok(cur, ["load", "--only", "reg02", "--commit"])
            self.reg("2026-06-30", values={"E06000001": {0: 11}},
                     url="https://assets.example/media/new/reissue.ods")
            rc, text, logged = self.run_main(cur, ["load", "--only", "reg02",
                                                   "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("two files claim the same release", text)
            self.assertIn("--accept-reissue 2026-06-30", text)
            self.assertEqual(editions(cur, "groups", "2026-06-30"),
                             [(1, None, 48)])
            self.assertIn("rejected 2026-06-30", logged.call_args[0][2])
            text, logged = self.ok(cur, ["load", "--only", "reg02", "--commit",
                                         "--accept-reissue", "2026-06-30"])
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertEqual(editions(cur, "groups", "2026-06-30"),
                             [(1, None, 48), (2, 1, 48)])
            self.assertIn("--accept-reissue 2026-06-30", logged.call_args[0][2])

    def test_a_zero_to_star_flip_needs_acknowledge(self):
        with rolled_back(self.conn) as cur:
            self.reg("2026-06-30")
            self.ok(cur, ["load", "--only", "reg02", "--commit"])
            self.reg("2026-06-30", values={"E06000001": {2: "*"}},
                     published=date(2026, 9, 1))
            rc, text, logged = self.run_main(cur, ["load", "--only", "reg02",
                                                   "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("1 cell(s) go from 0 to '*'", text)
            self.assertEqual(editions(cur, "groups", "2026-06-30"),
                             [(1, None, 48)])
            text, logged = self.ok(cur, ["load", "--only", "reg02", "--commit",
                                         "--acknowledge", "2026-06-30"])
            self.assertEqual(editions(cur, "groups", "2026-06-30"),
                             [(1, None, 48), (2, 1, 48)])
            self.assertIn("ACKNOWLEDGED 2026-06-30", logged.call_args[0][2])

    def test_an_older_reg02_file_halts_on_page_release_and_file(self):
        for how in ("page", "release", "file"):
            with self.subTest(how=how), rolled_back(self.conn) as cur:
                self.reg("2026-06-30")
                self.ok(cur, ["load", "--only", "reg02", "--commit"])
                p = self.reg("2026-06-30", register=how != "file",
                             published=date(2026, 8, 1),
                             values={"E06000001": {0: 11}},
                             url="https://assets.example/media/o/old.ods")
                argv = ["load", "--only", "reg02", "--commit"]
                if how == "release":
                    argv += ["--release", "June 2026"]
                if how == "file":
                    argv += ["--file", p]
                rc, text, logged = self.run_main(cur, argv)
                self.assertEqual(rc, "halt", text)
                self.assertIn("older file", text)
                self.assertEqual(editions(cur, "groups", "2026-06-30"),
                                 [(1, None, 48)])
                logged.assert_not_called()


class Migrate(Fixture):

    URL = "https://assets.example/media/h/"

    def seed_legacy(self, cur, *, plant=None):
        """Held-style live rows built from fixture files (Asy_D11 June,
        Reg_02 March and June) with the old build's loaded_at values."""
        p11, p09 = self.d11(Q4, register=False)
        rm = self.reg("2026-03-31", register=False)
        rj = self.reg("2026-06-30", register=False)
        f = m.read_d11(p11)
        rmap, bad = m.resolve_codes(cur, set(), m.d11_codes_by_period(f))
        self.assertEqual(bad, [])
        by, _, _ = m.build_d11_records(f, rmap)
        for tag in m.D11_TAGS:
            prof = dataclasses.replace(m.profile(tag),
                                       release_label=lambda d: JUNE)
            for p, t in by.items():
                if t[tag]:
                    m.insert_live(cur, prof, p, t[tag])
        for path, lab in ((rm, MARCH), (rj, JUNE)):
            g = m.read_reg02(path)
            rmap, _ = m.resolve_codes(cur, set(), {g["period"].isoformat(): {
                r["code"] for r in g["rows"]
                if r["code"][:3] in m.ENGLISH_PREFIXES}})
            p, recs = m.build_reg02_records(g, rmap)
            prof = dataclasses.replace(m.profile("groups"),
                                       release_label=lambda d, x=lab: x)
            m.insert_live(cur, prof, p, recs)
        stamp = "2026-09-04 18:20:20.096642+00"
        for t in m.TAGS:
            cur.execute(f"UPDATE public.zz_s6_{t} SET loaded_at = %s", (stamp,))
        cur.execute("UPDATE public.zz_s6_groups SET loaded_at = "
                    "'2026-07-26 19:33:11.02959+00' WHERE period_ending = "
                    "'2026-03-31'")
        if plant:
            cur.execute(plant)
        legacy = {}
        for t in m.TAGS:
            n, h, loaded, _ = m.live_state(cur, ZZ[t])
            legacy[f"zz_s6_{t}"] = (n, h, {p: str(v[0])
                                           for p, v in loaded.items()})
        files = {"d11": {"name": p11.name, "sha256": m.content_sha256(p11),
                         "url": self.URL + p11.name,
                         "rank": (date(2026, 6, 30), date(2026, 8, 21))},
                 "d09": {"name": p09.name, "sha256": m.content_sha256(p09),
                         "url": self.URL + p09.name,
                         "rank": (date(2026, 6, 30), date(2026, 8, 21))},
                 "reg02_mar": {"name": rm.name, "sha256": m.content_sha256(rm),
                               "url": self.URL + "mar/" + rm.name,
                               "rank": (date(2026, 3, 31), date(2026, 5, 21))},
                 "reg02_jun": {"name": rj.name, "sha256": m.content_sha256(rj),
                               "url": self.URL + "jun/" + rj.name,
                               "rank": (date(2026, 6, 30), date(2026, 8, 21))}}
        return (p11, p09, rm, rj), legacy, files

    def migrate(self, cur, paths, legacy, files, write=True):
        p11, p09, rm, rj = paths
        with quiet() as out:
            plan = m.migrate_legacy(cur, p11, [rm, rj], write=write, d09=p09,
                                    legacy=legacy, files=files)
        return plan, out.getvalue()

    def test_proof_then_every_path_rereads_unchanged(self):
        with rolled_back(self.conn) as cur:
            paths, legacy, files = self.seed_legacy(cur)
            p11, p09, rm, rj = paths
            plan, text = self.migrate(cur, paths, legacy, files)
            self.assertIn("0 differences", text)
            for t, n in (("support", 4), ("unallocated", 1),
                         ("non_england", 4), ("groups", 2)):
                self.assertEqual(len(plan["periods"][t]), n, t)
            cur.execute(f"SELECT DISTINCT release_label, published_date, "
                        f"source_file FROM public.{ED['support']}")
            (lab, pd_, sf), = cur.fetchall()
            self.assertEqual((lab, pd_), (JUNE, date(2026, 9, 4)))
            self.assertEqual(sf, f"as loaded: {p11.name}; {JUNE}; published "
                                 "2026-08-21")
            cur.execute(f"SELECT period_ending::text, release_label, "
                        f"published_date FROM public.{ED['groups']} GROUP BY "
                        "1, 2, 3 ORDER BY 1")
            self.assertEqual(cur.fetchall(), [
                ("2026-03-31", MARCH, date(2026, 7, 26)),
                ("2026-06-30", JUNE, date(2026, 9, 4))])
            for t in m.TAGS:
                self.assertTrue(pe.status(cur, m.profile(t))["ok"], t)
                self.assertEqual({o for _, _, o, _ in ledger(cur, t)},
                                 {"unchanged"})
            before = zz_counts(cur)
            # the same bytes on every path: unchanged, no new edition
            u11 = files["d11"]["url"]
            self.urls[u11] = p11
            self.urls[self.URL + "d09.xlsx"] = p09
            self.tables["details"]["attachments"] = [
                {"title": "Asylum seekers in receipt of Home Office support by "
                 "local authority detailed datasets, year ending June 2026",
                 "url": u11},
                {"title": "Asylum seekers in receipt of Home Office support "
                 "detailed datasets, year ending June 2026",
                 "url": self.URL + "d09.xlsx"}]
            for k, path in (("reg02_mar", rm), ("reg02_jun", rj)):
                self.urls[files[k]["url"]] = path
            ye = {"reg02_mar": "March 2026", "reg02_jun": "June 2026"}
            self.regional["details"]["attachments"] = [
                {"title": "Regional and local authority data on immigration "
                 f"groups, year ending {ye[k]}", "url": files[k]["url"]}
                for k in ye]
            text, _ = self.ok(cur, ["load"])
            self.assertEqual(text.count("nothing parsed"), 2)
            for argv in (["load", "--recheck", "2026-06-30"],
                         ["load", "--only", "reg02", "--release", "March 2026",
                          "--recheck", "2026-03-31"],
                         ["load", "--file", p11, "--d09-file", p09],
                         ["load", "--file", p11, "--no-page", "--no-d09"],
                         ["load", "--file", rm],
                         ["load", "--file", rj, "--no-page"],
                         ["load", "--file", p11, "--no-page", "--no-d09",
                          "--commit"],
                         ["load", "--file", rj, "--no-page", "--commit"],
                         ["load", "--recheck", "2026-06-30", "--commit"]):
                with self.subTest(argv=argv):
                    rc, text, _ = self.run_main(cur, argv)
                    self.assertEqual(rc, 0, text)
                    self.assertNotIn("REJECTED", text)
                    self.assertNotIn("revised", text)
                    self.assertNotIn(" new,", text)
                    self.assertIn("unchanged", text)
            after = zz_counts(cur)
            for t in m.TAGS:
                self.assertEqual(after[ED[t]], before[ED[t]], t)
                self.assertEqual(after[f"zz_s6_{t}"], before[f"zz_s6_{t}"])
                self.assertGreater(after[LEDGER[t]], before[LEDGER[t]], t)
                self.assertEqual({o for _, _, o, _ in ledger(cur, t)},
                                 {"unchanged"})
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertEqual(text.count("would write: none"), 4)

    def test_a_planted_held_difference_stops(self):
        plants = (
            "UPDATE public.zz_s6_support SET people = people + 1 WHERE "
            "lad24cd = 'E06000001' AND period_ending = '2026-06-30'",
            "UPDATE public.zz_s6_support SET published_la_name = 'Old Name' "
            "WHERE lad24cd = 'E06000002' AND period_ending = '2025-12-31' AND "
            "support_type = 'Section 4'",
            "UPDATE public.zz_s6_groups SET percentage_of_population = "
            "percentage_of_population + 0.0001 WHERE lad24cd = 'E06000001' "
            "AND pathway = 'all_pathways' AND period_ending = '2026-06-30'")
        for plant in plants:
            with self.subTest(plant=plant[:40]), \
                    rolled_back(self.conn) as cur:
                paths, legacy, files = self.seed_legacy(cur, plant=plant)
                with self.assertRaises(SystemExit) as e:
                    self.migrate(cur, paths, legacy, files)
                self.assertIn("proof failed", str(e.exception.code))
                for t in m.TAGS:
                    self.assertEqual(count(cur, ED[t]), 0)
                    self.assertEqual(count(cur, LEDGER[t]), 0)

    def test_a_wrong_sha_live_state_or_non_empty_editions_stops(self):
        with rolled_back(self.conn) as cur:
            paths, legacy, files = self.seed_legacy(cur)
            bad = dict(files, d11=dict(files["d11"], sha256="0" * 64))
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, paths, legacy, bad, write=False)
            self.assertIn("expected 0000000000000000", str(e.exception.code))
            off = dict(legacy, zz_s6_support=(1, "x", {}))
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, paths, off, files, write=False)
            self.assertIn("not as surveyed", str(e.exception.code))
            self.migrate(cur, paths, legacy, files)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, paths, legacy, files)
            self.assertIn("runs once", str(e.exception.code))

    def test_the_command_previews_then_commits(self):
        with rolled_back(self.conn) as cur:
            paths, legacy, files = self.seed_legacy(cur)
            p11, p09, rm, rj = paths
            argv = ["migrate-legacy", "--d11", p11, "--reg02", rm, "--reg02",
                    rj, "--d09", p09]
            with mock.patch.object(m, "LEGACY_LIVE", legacy), \
                    mock.patch.object(m, "LEGACY_FILES", files):
                text, logged = self.ok(cur, argv)
                self.assertIn("PREVIEW", text)
                self.assertEqual(count(cur, ED["support"]), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, argv + ["--commit"])
            self.assertEqual(count(cur, ED["support"]),
                             count(cur, "zz_s6_support"))
            logged.assert_called_once()


class Commands(Fixture):

    def test_status_without_editions_tables_is_clean(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1)
            self.assertIn("not present yet", text)
            self.assertFalse(pe.table_exists(cur, ED["support"]))

    def test_ddl_preview_then_commit_then_status(self):
        with rolled_back(self.conn, ddl=False) as cur:
            text, _ = self.ok(cur, ["ddl"])
            self.assertIn("DRY RUN", text)
            self.assertFalse(pe.table_exists(cur, ED["support"]))
            self.ok(cur, ["ddl", "--commit"])
            for t in m.TAGS:
                self.assertTrue(pe.table_exists(cur, ED[t]))
                self.assertTrue(pe.table_exists(cur, LEDGER[t]))
            text, _ = self.ok(cur, ["status"])
            self.assertIn("next expected release: 26 November 2026", text)

    def test_status_lines_after_a_load(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["status"])
            self.assertIn("live source_edition uniform, the tip's", text)
            cur.execute("UPDATE public.zz_s6_support SET source_edition = 'x' "
                        "WHERE lad24cd = 'E06000001'")
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1, text)
            self.assertIn("not uniform", text)

    def test_load_preview_before_ddl_compares_with_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            self.d11(Q3)
            text, _ = self.ok(cur, ["load", "--only", "d11"])
            self.assertIn("no editions tables yet", text)
            self.assertIn("2026-03-31: support new", text)
            rc, text, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt")
            self.assertIn("run `ddl --commit`", text)

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.d11(Q4, values={("2025-12-31", "E06000001",
                                  "Section 95"): 104})
            self.ok(cur, ["load", "--only", "d11", "--commit"])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_people(cur, "E06000001", "2025-12-31"), 104)
            text, logged = self.ok(cur, ["restore-edition", "zz_s6_support",
                                         "2025-12-31", "1", "--commit"])
            self.assertIn("as edition 3", text)
            self.assertEqual(editions(cur, "support", "2025-12-31")[-1],
                             (3, 2, 5))
            logged.assert_called_once()
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_people(cur, "E06000001", "2025-12-31"), 101)
            self.assertEqual(labels(cur, "support", "2025-12-31"), [MARCH])
            rc, text, _ = self.run_main(cur, ["restore-edition",
                                              "zz_s6_support", "2025-12-31",
                                              "3"])
            self.assertEqual(rc, "halt")
            self.assertIn("is the tip", text)


class NoTablesLeft(unittest.TestCase):
    """After the suite (and at any time): no zz% table persists."""

    def test_no_zz_table_exists(self):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT relname FROM pg_class WHERE relname LIKE "
                            "'zz%%'")
                self.assertEqual(cur.fetchall(), [])
        finally:
            conn.rollback()
            conn.close()


if __name__ == "__main__":
    unittest.main()
