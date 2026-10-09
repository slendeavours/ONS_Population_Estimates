"""Tests for the S22 loader in s22_ctb_editions (geography, the commands,
migrate-legacy, restore-edition).

No network: the content API replies and the downloads are stubbed, and every
workbook is written by the test (write_ctb and write_615 from
test_s22_ctb_pure). Database tests run on a connection from _db.get_conn()
inside a transaction that is always rolled back (rolled_back below); nothing
here commits. The commands get a stand-in connection whose commit and
rollback act on a savepoint of that transaction (so a command's rollback
really undoes its writes and its commit only keeps them inside the test's
transaction), and a cursor proxy whose .connection does the same, so the
engine's per-period commit never reaches the real connection. The only
tables written are the throwaway zz_s22_* copies of the three live tables,
their editions tables and the two ledgers, created inside that transaction
(refused if any already exists), so they never persist. The run log is
stubbed; the real la_code_lookup and la_boundaries are read (never written).
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
import s22_ctb_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s22_ctb_pure import (CODES, PUB_FIRST, PUB_REVISED,  # noqa: E402
                               T615_CODES, collection, release_page,
                               tables_page, write_615, write_ctb)

ZZ_CTB = dataclasses.replace(m.SPEC_CTB, name="zz_s22_ctb",
                             live_table="zz_s22_ctb_live",
                             editions_table="zz_s22_ctb_editions")
ZZ_CLS = dataclasses.replace(m.SPEC_CLASSES, name="zz_s22_cls",
                             live_table="zz_s22_cls_live",
                             editions_table="zz_s22_cls_editions")
ZZ_615 = dataclasses.replace(
    m.SPEC_615, name="zz_s22_615", live_table="zz_s22_615_live",
    editions_table="zz_s22_615_editions",
    expected_rows_per_period=m.tip_row_count("zz_s22_615_editions", "year"))
ZZ = (ZZ_CTB, ZZ_CLS, ZZ_615)
LIVE = {ZZ_CTB: "la_council_taxbase_empties",
        ZZ_CLS: "la_ctb_exemption_classes",
        ZZ_615: "la_vacant_dwellings_615"}
L_CTB = "zz_s22_ctb_editions_file_checks"
L_615 = "zz_s22_615_editions_file_checks"
N = len(CODES)
BOUNDS = {"E06000001", "E06000002", "E07000096", "E08000016"}
COLL = "/government/collections/council-taxbase-statistics"
REL = "/government/statistics/council-taxbase-{y}-in-england"


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    """commit / rollback of a command, acting on a savepoint of the test's
    own transaction."""
    SP = "zz_s22_cmd"

    def __init__(self, cur):
        self.cur = cur
        self.commits = 0
        cur.execute(f"SAVEPOINT {self.SP}")

    def commit(self):
        self.commits += 1
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
def rolled_back(conn, *, ddl=True):
    cur = conn.cursor()
    try:
        names = [s.live_table for s in ZZ] + [s.editions_table for s in ZZ] \
            + [L_CTB, L_615]
        cur.execute("SELECT " + ", ".join(f"to_regclass('public.{n}')"
                                          for n in names))
        if any(cur.fetchone()):
            raise RuntimeError("a zz_s22 table already exists as a real "
                               "table; refusing to run")
        for s in ZZ:
            cur.execute(f"CREATE TABLE public.{s.live_table} (LIKE "
                        f"public.{LIVE[s]} INCLUDING ALL)")
        with specs():
            if ddl:
                m.create_all(cur, ZZ)
            yield cur
    finally:
        cur.close()
        conn.rollback()


@contextmanager
def specs():
    with mock.patch.object(m, "SPEC_CTB", ZZ_CTB), \
            mock.patch.object(m, "SPEC_CLASSES", ZZ_CLS), \
            mock.patch.object(m, "SPEC_615", ZZ_615), \
            mock.patch.object(m, "CTB_AUTHORITIES", N):
        yield


def count(cur, table, where="", args=None):
    cur.execute(f"SELECT COUNT(*) FROM public.{table} {where}", args)
    return cur.fetchone()[0]


def editions(cur, spec, period):
    """[(edition, supersedes, rows)]."""
    cur.execute(f"SELECT edition, supersedes, COUNT(*) FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur, table, period=None):
    """[(source_file, outcome, edition)] in order."""
    pc = "taxbase_year" if table == L_CTB else "year"
    cur.execute(f"SELECT source_file, outcome, edition FROM public.{table}"
                + (f" WHERE {pc} = %s" if period else "") + " ORDER BY id",
                (period,) if period else None)
    return cur.fetchall()


def live_col(cur, spec, period, col):
    k = spec.key_cols[0]
    cur.execute(f"SELECT {k}, {col} FROM public.{spec.live_table} WHERE "
                f"{spec.period_col} = %s" + (" AND exemption_class = 'K'"
                                             if spec is ZZ_CLS else ""),
                (period,))
    return dict(cur.fetchall())


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
        self.api = {}            # content API path -> reply
        self.urls = {}           # url -> path
        self.final = {}          # url -> final url (a redirect)
        self.fetched = []
        self.n = 0
        self.docs = []

    def ctb(self, year=2025, *, pub=PUB_REVISED, register=True, **kw):
        """Write a CTB workbook; register its release page and download."""
        self.n += 1
        path = write_ctb(self.root / f"c{self.n}" / f"{year}_LA.xlsx",
                         year=year, pub=pub, **kw)
        if register:
            url = f"https://assets.example/media/c{self.n}/{year}_LA.xlsx"
            self.urls[url] = path
            title = f"Council Taxbase {year} in England"
            if title not in [t for t, _ in self.docs]:
                self.docs.append((title, REL.format(y=year)))
            self.api[COLL] = {"links": {"documents": [
                {"title": t, "base_path": b} for t, b in self.docs]}}
            self.api[REL.format(y=year)] = release_page(year, [
                ("Council Taxbase: Local authority level data for "
                 f"{year} (revised)", url)])
            self.last_url = url
        return path

    def t615(self, years=(2023, 2024, 2025), *, register=True, **kw):
        self.n += 1
        path = write_615(self.root / f"t{self.n}" / "Live_Table_615.ods",
                         years, **kw)
        if register:
            url = f"https://assets.example/media/t{self.n}/Live_Table_615.ods"
            self.urls[url] = path
            self.api[m.LIVE_TABLES_PATH] = tables_page([
                ("Table 100: dwellings", "x"),
                ("Table 615: vacant dwellings by local authority district: "
                 "England, from 2004", url)])
            self.last_url = url
        return path

    def run_main(self, cur, argv, *, bounds=BOUNDS):
        """main(argv) on the throwaway tables. Returns (rc or 'halt', text,
        log mock)."""
        borrowed = _Borrowed(cur)

        def fetch(url, dest, session=None):
            self.fetched.append(url)
            return self.urls[url], self.final.get(url, url)

        out = io.StringIO()
        with specs(), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda p, s=None: self.api[p]), \
                mock.patch.object(m, "fetch", side_effect=fetch), \
                mock.patch.object(m, "_boundaries", return_value=set(bounds)), \
                mock.patch.object(m, "log_run") as logged, \
                contextlib.redirect_stdout(out):
            try:
                rc = m.main(list(argv))
            except SystemExit as e:
                rc = "halt"
                out.write(f"\n{e.code}")
            finally:
                borrowed.tx.done()
        return rc, out.getvalue(), logged

    def ok(self, cur, argv, **kw):
        rc, text, logged = self.run_main(cur, argv, **kw)
        self.assertEqual(rc, 0, text)
        return text, logged

    def seed_ctb(self, cur, **kw):
        self.ctb(**kw)
        self.ok(cur, ["load", "--commit"])
        self.fetched.clear()

    def seed_615(self, cur, years=(2023, 2024), **kw):
        self.t615(years, **kw)
        self.ok(cur, ["load-615", "--commit"])
        self.fetched.clear()


class Geography(Fixture):
    """resolve_codes on the real la_code_lookup and la_boundaries (read)."""

    def resolve(self, by):
        with rolled_back(self.conn, ddl=False) as cur:
            return m.resolve_codes(cur, by)

    def test_direct_recode_and_abolished(self):
        out, problems = self.resolve({
            2019: {"E06000001", "E07000004", "E08000016", "E07000001"},
            2008: {"E07000001"},
            2025: {"E06000001", "E08000038"}})
        self.assertEqual(problems, [])
        self.assertEqual(out["E06000001"], ("E06000001", "direct"))
        self.assertEqual(out["E08000038"], ("E08000016", "resolved_via_lookup"))
        self.assertEqual(out["E08000016"], ("E08000016", "direct"))
        # abolished 2020 (la_code_lookup) with numbers to 2019: unmapped,
        # never mapped to its successor
        self.assertEqual(out["E07000004"], (None, "unmapped"))
        # absent from la_code_lookup, no number from 2023: unmapped
        self.assertEqual(out["E07000001"], (None, "unmapped"))

    def test_both_forms_of_barnsley_in_one_year_halt(self):
        _, problems = self.resolve({2025: {"E08000038", "E08000016"}})
        self.assertTrue(any("carries both E08000016 and E08000038" in p
                            for p in problems), problems)
        # E08000016 with no number in 2025 is not a use: fine
        _, problems = self.resolve({2024: {"E08000016"},
                                    2025: {"E08000038"}})
        self.assertEqual(problems, [])

    def test_unknown_or_unabolished_codes_are_unexplained(self):
        _, problems = self.resolve({2025: {"E07000999"}})
        self.assertTrue(any("UNEXPLAINED E07000999" in p for p in problems))
        _, problems = self.resolve({2023: {"E07000001"}})
        self.assertTrue(any("UNEXPLAINED E07000001" in p for p in problems))
        # abolished 2020 but a number in 2020
        _, problems = self.resolve({2020: {"E07000004"}})
        self.assertTrue(any("UNEXPLAINED E07000004" in p for p in problems))


class LoadCTB(Fixture):

    def test_new_year_stores_both_editions_live_and_one_ledger_row(self):
        with rolled_back(self.conn) as cur:
            self.ctb()
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn("2025: new", text)
            self.assertEqual(editions(cur, ZZ_CTB, "2025"), [(1, None, N)])
            self.assertEqual(editions(cur, ZZ_CLS, "2025"),
                             [(1, None, 11 * N)])
            self.assertEqual(count(cur, ZZ_CTB.live_table), N)
            self.assertEqual(count(cur, ZZ_CLS.live_table), 11 * N)
            self.assertEqual(core.rows_differing(cur, ZZ_CTB, "2025", 1), 0)
            self.assertEqual(core.rows_differing(cur, ZZ_CLS, "2025", 1), 0)
            (src, outcome, ed), = ledger(cur, L_CTB)
            self.assertEqual((outcome, ed), ("new", 1))
            self.assertEqual(src, f"{self.last_url} (file dated 2026-01-21; "
                                  "years 2025)")
            cur.execute("SELECT DISTINCT source_publication FROM "
                        "public.zz_s22_ctb_live")
            (sp,), = cur.fetchall()
            self.assertTrue(sp.endswith(f"revised 2026-01-21; {self.last_url}"))
            cur.execute("SELECT DISTINCT source_file, published_date FROM "
                        "public.zz_s22_ctb_editions")
            self.assertEqual(cur.fetchall(), [(sp, m.date(2026, 1, 21))])
            cur.execute("SELECT DISTINCT source_sha256 FROM "
                        "public.zz_s22_cls_editions")
            cls_sha = cur.fetchall()
            cur.execute("SELECT DISTINCT source_sha256 FROM "
                        "public.zz_s22_ctb_editions")
            self.assertEqual(cur.fetchall(), cls_sha)
            logged.assert_called_once()
            self.assertIn(f"{N + 11 * N} edition rows", logged.call_args[0][2])
            self.assertTrue(pe.status(cur, m.profiles("ctb")[0])["ok"])

    def test_a_failing_classes_insert_rolls_back_everything(self):
        with rolled_back(self.conn) as cur:
            self.ctb()
            real = m.insert_live

            def ins(c, profile, period, records):
                if profile.spec.name == "zz_s22_cls":
                    raise RuntimeError("boom")
                return real(c, profile, period, records)
            with mock.patch.object(m, "insert_live", side_effect=ins):
                rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2025: FAILED", text)
            for s in (ZZ_CTB, ZZ_CLS):
                self.assertEqual(editions(cur, s, "2025"), [])
                self.assertEqual(count(cur, s.live_table), 0)
            self.assertEqual(ledger(cur, L_CTB), [])
            logged.assert_not_called()

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            cur.execute("SELECT COUNT(*) FROM pipeline_run_log")
            runs = cur.fetchone()[0]
            self.ctb()
            text, logged = self.ok(cur, ["load"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            for t in (ZZ_CTB.editions_table, ZZ_CLS.editions_table,
                      ZZ_CTB.live_table, ZZ_CLS.live_table, L_CTB):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            text, _ = self.ok(cur, ["load", "--simulate"])
            self.assertIn("SIMULATION", text)
            self.assertEqual(count(cur, ZZ_CTB.editions_table), 0)
            self.ok(cur, ["load", "--commit"])
            # rerun: the file's pair is in the ledger, nothing parsed
            calls = []
            real = m.read_ctb
            with mock.patch.object(m, "read_ctb",
                                   side_effect=lambda p: calls.append(p)
                                   or real(p)):
                text, logged = self.ok(cur, ["load", "--commit"])
            self.assertEqual(calls, [])
            self.assertIn("nothing parsed", text)
            logged.assert_not_called()
            self.assertEqual(editions(cur, ZZ_CTB, "2025"), [(1, None, N)])
            self.assertEqual(len(ledger(cur, L_CTB)), 1)
            cur.execute("SELECT COUNT(*) FROM pipeline_run_log")
            self.assertEqual(cur.fetchone()[0], runs)

    def test_unchanged_recheck_records_the_ledger_only_on_commit(self):
        with rolled_back(self.conn) as cur:
            self.seed_ctb(cur)
            text, _ = self.ok(cur, ["load", "--recheck", "2025"])
            self.assertIn("2025: unchanged", text)
            self.assertEqual(len(ledger(cur, L_CTB)), 1)
            self.ok(cur, ["load", "--recheck", "2025", "--commit"])
            self.assertEqual([o for _, o, _ in ledger(cur, L_CTB)],
                             ["new", "unchanged"])
            self.assertEqual(editions(cur, ZZ_CTB, "2025"), [(1, None, N)])

    def test_classes_differ_but_main_does_not_both_get_the_next_edition(self):
        with rolled_back(self.conn) as cur:
            self.seed_ctb(cur, pub=PUB_FIRST)
            # move one dwelling from class B to class D: the 11-class total
            # (and every main column) is unchanged
            self.ctb(values={"E06000002": {"class B": 12, "class D": 12}})
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn("2025: revised", text)
            self.assertEqual(editions(cur, ZZ_CTB, "2025"),
                             [(1, None, N), (2, 1, N)])
            self.assertEqual(editions(cur, ZZ_CLS, "2025"),
                             [(1, None, 11 * N), (2, 1, 11 * N)])
            self.assertEqual([o for _, o, _ in ledger(cur, L_CTB)],
                             ["new", "revised"])

    def test_revised_year_waits_for_refresh_latest(self):
        with rolled_back(self.conn) as cur:
            self.seed_ctb(cur, pub=PUB_FIRST)
            cur.execute("UPDATE public.zz_s22_ctb_live SET loaded_at = "
                        "'2026-01-01'")
            cur.execute("UPDATE public.zz_s22_cls_live SET loaded_at = "
                        "'2026-01-01'")
            before = live_col(cur, ZZ_CTB, "2025", "second_homes")
            self.ctb(values={"E06000001": {"second_homes": 101,
                                           "class K": 9}})
            self.ok(cur, ["load", "--commit"])
            self.assertEqual(live_col(cur, ZZ_CTB, "2025", "second_homes"),
                             before)
            st = pe.status(cur, m.profiles("ctb")[0])
            self.assertEqual(st["pending_refresh"], ["2025"])
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--part",
                                              "ctb"])
            self.assertEqual(rc, 0, text)
            self.assertIn("2025=1", text)
            self.assertEqual(live_col(cur, ZZ_CTB, "2025", "second_homes"),
                             before)
            self.ok(cur, ["refresh-latest", "--part", "ctb", "--commit"])
            self.assertEqual(live_col(cur, ZZ_CTB, "2025",
                                      "second_homes")["E06000001"], 101)
            self.assertEqual(live_col(cur, ZZ_CLS, "2025",
                                      "dwellings")["E06000001"], 9)
            cur.execute("SELECT DISTINCT source_publication FROM "
                        "public.zz_s22_ctb_live")
            (sp,), = cur.fetchall()
            self.assertTrue(sp.endswith(self.last_url))
            self.assertIn("revised 2026-01-21", sp)
            cur.execute("SELECT loaded_at FROM public.zz_s22_ctb_editions "
                        "WHERE edition = 2 LIMIT 1")
            ed_loaded = cur.fetchone()[0]
            cur.execute("SELECT loaded_at FROM public.zz_s22_ctb_live WHERE "
                        "lad24cd = 'E06000001'")
            self.assertEqual(cur.fetchone()[0], ed_loaded)
            for prof in m.profiles("ctb"):
                self.assertTrue(pe.status(cur, prof)["ok"])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)

    def test_refresh_latest_ctb_refuses_differing_plans(self):
        with rolled_back(self.conn) as cur:
            self.seed_ctb(cur)
            recs = m.records(cur, ZZ_CLS, "2025", 1)
            recs[0]["dwellings"] = 999
            core.insert_edition(cur, ZZ_CLS, recs, "2025",
                                release_label="planted", published_date=None,
                                source_file="x", source_sha256="planted",
                                supersedes=1)
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--part",
                                              "ctb", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("main and classes plans differ", text)
            self.assertEqual(count(cur, ZZ_CLS.live_table,
                                   "WHERE dwellings = 999"), 0)

    def older(self, cur, argv_extra, *, by_file=False):
        """Seed from the revised file, then offer the original (older)
        file with different values."""
        self.seed_ctb(cur)                                  # dated 2026-01-21
        path = self.ctb(pub=PUB_FIRST, values={"E06000001":
                                               {"second_homes": 101}},
                        register=not by_file)
        argv = ["load", "--commit"] + argv_extra
        if by_file:
            argv += ["--file", str(path)]
        return self.run_main(cur, argv)

    def test_an_older_file_is_skipped_and_halts_on_every_path(self):
        for extra, by_file in (([], False), (["--release", "2025"], False),
                               ([], True)):
            with self.subTest(extra=extra, by_file=by_file), \
                    rolled_back(self.conn) as cur:
                rc, text, logged = self.older(cur, extra, by_file=by_file)
                self.assertEqual(rc, "halt", text)
                self.assertIn("2025 (older", text)
                self.assertIn("older than the tips of all its periods", text)
                self.assertEqual(editions(cur, ZZ_CTB, "2025"),
                                 [(1, None, N)])
                logged.assert_not_called()

    def test_allow_older_file_overrides_and_is_logged(self):
        with rolled_back(self.conn) as cur:
            rc, text, logged = self.older(cur, ["--allow-older-file"],
                                          by_file=True)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, ZZ_CTB, "2025"),
                             [(1, None, N), (2, 1, N)])
            self.assertIn("--allow-older-file given", logged.call_args[0][2])
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s22_ctb_editions WHERE edition = 2")
            self.assertIn("--allow-older-file given", cur.fetchone()[0])

    def test_equal_rank_with_different_content_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed_ctb(cur)
            self.ctb(values={"E06000001": {"second_homes": 101}})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("two files claim the same date", text)
            self.assertEqual(editions(cur, ZZ_CTB, "2025"), [(1, None, N)])
            self.assertEqual(len(ledger(cur, L_CTB)), 1)
            logged.assert_not_called()

    def test_zero_to_null_needs_acknowledge(self):
        with rolled_back(self.conn) as cur:
            self.seed_ctb(cur, pub=PUB_FIRST,
                          values={"E06000003": {"class K": 0}})
            self.ctb(values={"E06000003": {"class K": "[x]"}})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("REJECTED", text)
            self.assertIn("--acknowledge 2025", text)
            self.assertEqual(editions(cur, ZZ_CTB, "2025"), [(1, None, N)])
            self.assertEqual(len(ledger(cur, L_CTB)), 1)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--acknowledge", "2025",
                                         "--commit"])
            self.assertEqual(editions(cur, ZZ_CLS, "2025"),
                             [(1, None, 11 * N), (2, 1, 11 * N)])
            self.assertIn("ACKNOWLEDGED", logged.call_args[0][2])
            cur.execute("SELECT unoccupied_exemptions_total, null_reasons "
                        "FROM public.zz_s22_ctb_editions WHERE edition = 2 "
                        "AND lad24cd = 'E06000003'")
            self.assertEqual(cur.fetchone(),
                             (None, "unoccupied_exemptions_total="
                                    "built_from_null"))

    def test_a_stop_condition_rejects_and_stores_nothing(self):
        with rolled_back(self.conn) as cur:
            self.ctb(codes=CODES[:-1])                     # 4 of 5
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("4 authorities, expected 5", text)
            self.assertEqual(count(cur, ZZ_CTB.editions_table), 0)
            self.assertEqual(ledger(cur, L_CTB), [])
            logged.assert_not_called()

    def test_identity_and_reconciliation_halt(self):
        with rolled_back(self.conn) as cur:
            self.ctb(year=2024)
            self.api[REL.format(y=2024)]["title"] = "Council Taxbase 2025 in England"
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.ctb(england={"second_homes": 1})
            rc, text, _ = self.run_main(cur, ["load", "--release", "2025"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("do not sum to England", text)
            path = self.ctb(register=False)
            rc, text, _ = self.run_main(cur, ["load", "--file", str(path),
                                              "--release", "2024"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--release 2024, the cover says 2025", text)
            self.assertEqual(count(cur, ZZ_CTB.editions_table), 0)

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.ctb()
            final = "https://assets.example/media/new/2025_LA.xlsx"
            self.final[self.last_url] = final
            self.ok(cur, ["load", "--commit"])
            (src, _, _), = ledger(cur, L_CTB)
            self.assertTrue(src.startswith(final + " (file dated"))
            cur.execute("SELECT DISTINCT source_publication FROM "
                        "public.zz_s22_ctb_live")
            self.assertTrue(cur.fetchone()[0].endswith(final))

    def test_a_stranded_year_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed_ctb(cur)
            cur.execute("DELETE FROM public.zz_s22_ctb_live")
            self.assertEqual(pe.status(cur, m.profiles("ctb")[0])
                             ["live_missing"], ["2025"])
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn("live-missing", text)
            self.assertEqual(count(cur, ZZ_CTB.live_table), N)
            self.assertEqual(editions(cur, ZZ_CTB, "2025"), [(1, None, N)])
            self.assertTrue(pe.status(cur, m.profiles("ctb")[0])["ok"])

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed_ctb(cur, pub=PUB_FIRST)
            first = live_col(cur, ZZ_CTB, "2025", "second_homes")
            self.ctb(values={"E06000001": {"second_homes": 101}})
            self.ok(cur, ["load", "--commit"])
            self.ok(cur, ["refresh-latest", "--part", "ctb", "--commit"])
            self.assertEqual(live_col(cur, ZZ_CTB, "2025",
                                      "second_homes")["E06000001"], 101)
            text, logged = self.ok(cur, ["restore-edition", "ctb", "2025",
                                         "1", "--commit"])
            self.assertIn("as edition 3", text)
            for s, n in ((ZZ_CTB, N), (ZZ_CLS, 11 * N)):
                self.assertEqual(editions(cur, s, "2025")[-1], (3, 2, n))
            self.ok(cur, ["refresh-latest", "--part", "ctb", "--commit"])
            self.assertEqual(live_col(cur, ZZ_CTB, "2025", "second_homes"),
                             first)
            rc, text, _ = self.run_main(cur, ["restore-edition", "ctb",
                                              "2025", "3"])
            self.assertEqual(rc, "halt")
            self.assertIn("is the tip", text)


class Load615(Fixture):

    def test_new_years_and_the_ledger(self):
        with rolled_back(self.conn) as cur:
            self.t615()
            self.ok(cur, ["load-615", "--commit"])
            for y in ("2023", "2024", "2025"):
                self.assertEqual(len(editions(cur, ZZ_615, y)), 1)
            # rows of [x] on both sheets are not stored
            cur.execute("SELECT year, published_la_code, lad24cd, "
                        "mapping_status FROM public.zz_s22_615_live WHERE "
                        "published_la_code LIKE 'E080000%' ORDER BY 1, 2")
            self.assertEqual(cur.fetchall(), [
                (2023, "E08000016", "E08000016", "direct"),
                (2024, "E08000016", "E08000016", "direct"),
                (2025, "E08000038", "E08000016", "resolved_via_lookup")])
            self.assertEqual(len(ledger(cur, L_615)), 3)
            self.assertTrue(pe.status(cur, m.profiles("615")[0])["ok"])

    def test_rejected_years_leave_no_ledger_row_and_are_retried(self):
        with rolled_back(self.conn) as cur:
            # 2025 is rejected (an authority without a number): the pair is
            # in the ledger for 2023 and 2024 only
            self.t615()
            rc, text, logged = self.run_main(
                cur, ["load-615", "--commit"],
                bounds=BOUNDS | {"E06000003"})
            self.assertEqual(rc, 1, text)
            self.assertIn("2025: REJECTED", text)
            self.assertIn("2023: REJECTED", text)
            self.assertEqual(ledger(cur, L_615), [])
            logged.assert_not_called()
            # a fix of the stop (the declared set) and the rerun stores all
            text, logged = self.ok(cur, ["load-615", "--commit"])
            self.assertEqual(len(ledger(cur, L_615)), 3)

    def test_a_partial_ledger_is_read_and_a_partial_run_is_logged(self):
        with rolled_back(self.conn) as cur:
            self.seed_615(cur, (2023, 2024))
            # the new file revises 2024 (stored) and its 2025 is rejected
            vals = {("All_vacants", "E06000001", 2024): 330,
                    ("All_vacants", "E06000002", 2025): "[x]"}
            self.t615(values=vals, latest="26 June 2026")
            rc, text, logged = self.run_main(cur, ["load-615", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertEqual(len(editions(cur, ZZ_615, "2024")), 2)
            self.assertEqual(editions(cur, ZZ_615, "2025"), [])
            logged.assert_called_once()
            self.assertIn("PARTIAL RUN", logged.call_args[0][2])
            self.assertIn("[2025]", logged.call_args[0][2])
            # the pair is held for 2023 and 2024 but not 2025: the rerun
            # reads the file again (and rejects 2025 again)
            calls = []
            real = m.read_615
            with mock.patch.object(m, "read_615",
                                   side_effect=lambda p: calls.append(p)
                                   or real(p)):
                rc, text, logged = self.run_main(cur, ["load-615",
                                                       "--commit"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(rc, 1, text)
            self.assertIn("2023 (checked", text)
            logged.assert_not_called()

    def test_an_older_615_file_is_skipped_per_year(self):
        with rolled_back(self.conn) as cur:
            self.seed_615(cur, (2023, 2024, 2025))
            self.t615((2023, 2024), latest="January 2026",
                      values={("All_vacants", "E06000001", 2024): 1})
            rc, text, logged = self.run_main(cur, ["load-615", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("older than the tips", text)
            self.assertEqual(len(editions(cur, ZZ_615, "2024")), 1)

    def test_revised_615_year_waits_for_refresh(self):
        with rolled_back(self.conn) as cur:
            self.seed_615(cur, (2023, 2024))
            self.t615((2023, 2024), latest="26 June 2026",
                      values={("All_long_term_vacants", "E06000002", 2023):
                              115})
            self.ok(cur, ["load-615", "--commit"])
            self.assertEqual(len(editions(cur, ZZ_615, "2023")), 2)
            self.assertEqual(live_col(cur, ZZ_615, "2023",
                                      "long_term_vacant_dwellings")
                             ["E06000002"], 114)
            self.ok(cur, ["refresh-latest", "--part", "615", "--commit"])
            self.assertEqual(live_col(cur, ZZ_615, "2023",
                                      "long_term_vacant_dwellings")
                             ["E06000002"], 115)

    def test_a_code_appearing_in_a_revised_year_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed_615(cur, (2023, 2024))
            self.t615((2023, 2024), latest="26 June 2026",
                      codes=T615_CODES + ["E06000003"])
            rc, text, _ = self.run_main(cur, ["load-615", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("appearing: E06000003", text)


class Migrate(Fixture):

    def seed_legacy(self, cur):
        """Held-style live rows built from fixture files, one loaded_at."""
        ctb = self.ctb(register=False)
        # 615 2025 all vacants = CTB empty_total + the 11 classes, as
        # published (write_ctb's values: 1200 + 10i and 80 + 110i)
        t615 = self.t615((2023, 2024, 2025), register=False,
                         codes=T615_CODES + ["E06000003"],
                         values={("All_vacants", c, 2025): 1280 + 120 * i
                                 for i, c in enumerate(CODES)})
        fc = m.read_ctb(ctb)
        fc["source"] = "u"
        rmap, problems = m.resolve_codes(cur, m.codes_with_numbers(fc))
        main, cls = m.build_ctb(fc, lambda c: rmap[c][0])
        ft = m.read_615(t615)
        rmap, _ = m.resolve_codes(cur, m.codes_with_numbers(ft))
        rows = m.build_615(ft, lambda c: rmap[c])
        pm, pc = m.profiles("ctb")
        pt = m.profiles("615")[0]
        for r in main:
            r["source_publication"] = ("Council Taxbase 2025 in England - "
                                       "first published 2025-11-06, revised "
                                       "2026-01-21")
        m.insert_live(cur, pm, "2025", main)
        m.insert_live(cur, pc, "2025", cls)
        for y, rs in rows.items():
            m.insert_live(cur, pt, str(y), rs)
        for s in ZZ:
            cur.execute(f"UPDATE public.{s.live_table} SET loaded_at = "
                        "'2026-08-13 01:19:03+00'")
        files = {"ctb": {"sha256": m.content_sha256(ctb),
                         "url": "https://assets.example/media/h/ctb.xlsx"},
                 "615": {"sha256": m.content_sha256(t615),
                         "url": "https://assets.example/media/h/615.ods"}}
        return ctb, t615, files

    def legacy(self, cur):
        return {k: m.live_state(cur, s)[:2] for k, s in
                (("ctb", ZZ_CTB), ("classes", ZZ_CLS), ("615", ZZ_615))}

    def test_proof_passes_and_edition_1_and_ledgers_are_stored(self):
        with rolled_back(self.conn) as cur:
            ctb, t615, files = self.seed_legacy(cur)
            with quiet() as out:
                plan = m.migrate_legacy(cur, ctb, t615, write=True,
                                        legacy=self.legacy(cur), files=files)
            text = out.getvalue()
            self.assertIn("0 differences", text)
            self.assertIn(f"for {N} of {N} authorities", text)
            self.assertEqual(plan["proof"]["ctb"], N * 8)
            self.assertEqual(editions(cur, ZZ_CTB, "2025"), [(1, None, N)])
            self.assertEqual(editions(cur, ZZ_CLS, "2025"),
                             [(1, None, 11 * N)])
            for y in ("2023", "2024", "2025"):
                self.assertEqual(len(editions(cur, ZZ_615, y)), 1)
            self.assertEqual(ledger(cur, L_CTB), [(
                "https://assets.example/media/h/ctb.xlsx (file dated "
                "2026-01-21; years 2025)", "unchanged", 1)])
            self.assertEqual(len(ledger(cur, L_615)), 3)
            cur.execute("SELECT DISTINCT source_file FROM "
                        "public.zz_s22_615_editions")
            self.assertTrue(cur.fetchone()[0].endswith(
                "file dated 2026-06-25"))
            for part in m.PARTS:
                for prof in m.profiles(part):
                    self.assertTrue(pe.status(cur, prof)["ok"])
            # the held file is then skipped by (URL, sha) on load
            self.urls[files["ctb"]["url"]] = ctb
            self.api[COLL] = collection("Council Taxbase 2025 in England")
            self.api[REL.format(y=2025)] = release_page(2025, [(
                "Council Taxbase: Local authority level data for 2025 "
                "(revised)", files["ctb"]["url"])])
            text, _ = self.ok(cur, ["load"])
            self.assertIn("nothing parsed", text)
            text, _ = self.ok(cur, ["load", "--recheck", "2025"])
            self.assertIn("2025: unchanged", text)
            self.urls[files["615"]["url"]] = t615
            self.api[m.LIVE_TABLES_PATH] = tables_page([(
                "Table 615: vacant dwellings", files["615"]["url"])])
            text, _ = self.ok(cur, ["load-615"])
            self.assertIn("nothing parsed", text)
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertEqual(text.count("would write: none"), 3)

    def test_a_planted_held_difference_stops_and_rolls_back(self):
        with rolled_back(self.conn) as cur:
            ctb, t615, files = self.seed_legacy(cur)
            cur.execute("UPDATE public.zz_s22_615_live SET vacant_dwellings "
                        "= vacant_dwellings + 1 WHERE published_la_code = "
                        "'E06000002' AND year = 2024")
            cur.execute("SAVEPOINT t")
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, ctb, t615, write=True,
                                 legacy=self.legacy(cur), files=files)
            self.assertIn("proof failed", str(e.exception.code))
            cur.execute("ROLLBACK TO SAVEPOINT t")
            for s in ZZ:
                self.assertEqual(count(cur, s.editions_table), 0)

    def test_a_wrong_file_sha_or_live_state_stops(self):
        with rolled_back(self.conn) as cur:
            ctb, t615, files = self.seed_legacy(cur)
            bad = {**files, "615": {**files["615"], "sha256": "0" * 64}}
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, ctb, t615, write=False,
                                 legacy=self.legacy(cur), files=bad)
            self.assertIn("expected 00000000", str(e.exception.code))
            legacy = {**self.legacy(cur), "classes": (1, "x")}
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, ctb, t615, write=False, legacy=legacy,
                                 files=files)
            self.assertIn("not as surveyed", str(e.exception.code))

    def test_runs_once_on_empty_tables(self):
        with rolled_back(self.conn) as cur:
            ctb, t615, files = self.seed_legacy(cur)
            legacy = self.legacy(cur)
            with quiet():
                m.migrate_legacy(cur, ctb, t615, write=True, legacy=legacy,
                                 files=files)
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, ctb, t615, write=True, legacy=legacy,
                                 files=files)
            self.assertIn("runs once", str(e.exception.code))


class Commands(Fixture):

    def test_status_without_editions_tables_is_clean_and_creates_nothing(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1)
            self.assertIn("not present yet", text)
            self.assertFalse(pe.table_exists(cur, ZZ_CTB.editions_table))

    def test_ddl_preview_then_commit(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["ddl"])
            self.assertEqual(rc, 0)
            self.assertIn("DRY RUN", text)
            self.assertFalse(pe.table_exists(cur, ZZ_CTB.editions_table))
            self.ok(cur, ["ddl", "--commit"])
            for t in (ZZ_CTB.editions_table, ZZ_CLS.editions_table,
                      ZZ_615.editions_table, L_CTB, L_615):
                self.assertTrue(pe.table_exists(cur, t), t)
            for s in ZZ:
                self.assertTrue(m.column_exists(cur, s.live_table,
                                                "null_reasons"))
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 0, text)

    def test_load_preview_before_ddl_compares_with_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            self.ctb()
            text, _ = self.ok(cur, ["load"])
            self.assertIn("compares each period with the LIVE tables", text)
            self.assertIn("2025: new", text)
            rc, text, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt")
            self.assertIn("run `ddl --commit`", text)


if __name__ == "__main__":
    unittest.main()
