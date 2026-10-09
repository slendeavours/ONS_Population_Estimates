"""Tests for the S4 loader in s4_care_leaver_editions (geography, spec,
download, the commands, migrate-legacy, restore-edition).

No network: the content API, the data guidance page and the downloads are
stubbed, and every CSV is written by the test (file_rows from
test_s4_care_leaver_pure). Database tests run on a connection from
_db.get_conn() inside a transaction that is always rolled back (rolled_back
below); nothing here commits. The commands get a stand-in connection whose
commit and rollback act on a savepoint of that transaction (so a command's
rollback really undoes its writes and its commit only keeps them inside the
test's transaction), and a cursor proxy whose .connection does the same, so
the engine's per-year commit never reaches the real connection. The only
tables written are the throwaway zz_s4_live, zz_s4_editions and
zz_s4_editions_file_checks (a copy of SPEC named zz_s4), created inside that
transaction (refused if any already exists), so they never persist. The run
log is stubbed; the real la_code_lookup, la_boundaries and utla_lad_mapping
are read (never written).
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
import s4_care_leaver_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s4_care_leaver_pure import (T17_NEW, T17_OLD, T22_NEW,  # noqa: E402
                                      T22_OLD, dataset, file_rows, html_of,
                                      page_json, write_csv)

ZZ = dataclasses.replace(
    m.SPEC, name="zz_s4", live_table="zz_s4_live",
    editions_table="zz_s4_editions",
    expected_rows_per_period=m.tip_row_count("zz_s4_editions"))
LEDGER = "zz_s4_editions_file_checks"
CODES = ["E06000001", "E06000002", "E06000003", "E06000004", "E06000005",
         "E10000003"]
N = len(CODES)
SCHEMA = {("2023", "17-21"): "accommodation_type",
          ("2024", "17-21"): "accommodation_type",
          ("2024", "22-25"): "suitability_2024"}
AGE1 = {"accommodation_type": "17 to 18 years",
        "care_leaver_2025": "17 to 18 years"}


def schema_of(slug, cohort):
    return SCHEMA.get((slug, cohort), "care_leaver_2025")


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    """commit / rollback of a command, acting on a savepoint of the test's
    own transaction."""
    SP = "zz_s4_cmd"

    def __init__(self, cur):
        self.cur = cur
        self.commits = 0
        self.rollbacks = 0
        cur.execute(f"SAVEPOINT {self.SP}")

    def commit(self):
        self.commits += 1
        self.cur.execute(f"RELEASE SAVEPOINT {self.SP}")
        self.cur.execute(f"SAVEPOINT {self.SP}")

    def rollback(self):
        self.rollbacks += 1
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

    @property
    def commits(self):
        return self.tx.commits

    @property
    def rollbacks(self):
        return self.tx.rollbacks

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
def rolled_back(conn):
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_s4_live'), "
                    "to_regclass('public.zz_s4_editions'), "
                    f"to_regclass('public.{LEDGER}')")
        if cur.fetchone() != (None, None, None):
            raise RuntimeError("a zz_s4 table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s4_live (LIKE "
                    "public.care_leaver_accommodation INCLUDING ALL)")
        m.create_all(cur, ZZ)
        pe.create_file_checks(cur, m._profile(ZZ))
        yield cur
    finally:
        cur.close()
        conn.rollback()


def count(cur, table, where="", args=None):
    cur.execute(f"SELECT COUNT(*) FROM public.{table} {where}", args)
    return cur.fetchone()[0]


def editions(cur, year=None):
    """[(edition, supersedes, rows)] of the throwaway editions table."""
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM "
                "public.zz_s4_editions"
                + (" WHERE reporting_year = %s" if year else "")
                + " GROUP BY 1, 2 ORDER BY 1", (year,) if year else None)
    return cur.fetchall()


def ledger(cur, year=None):
    """[(source_file, outcome, edition)] in order."""
    cur.execute(f"SELECT source_file, outcome, edition FROM public.{LEDGER}"
                + (" WHERE reporting_year = %s" if year else "")
                + " ORDER BY id", (year,) if year else None)
    return cur.fetchall()


def live(cur, year, col="foyers", cohort="17-21"):
    cur.execute(f"SELECT lad24cd, {col} FROM public.zz_s4_live WHERE "
                "reporting_year = %s AND age_group = %s", (year, cohort))
    return dict(cur.fetchall())


def live_sources(cur, year):
    cur.execute("SELECT DISTINCT source FROM public.zz_s4_live WHERE "
                "reporting_year = %s", (year,))
    return sorted(r[0] for r in cur.fetchall())


def run_log_count(cur):
    return count(cur, "pipeline_run_log")


class Fixture(unittest.TestCase):
    """Releases built as files, the API reply, the pages and the downloads
    stubbed."""

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
        self.urls = {}          # csv url -> path
        self.pages = {}         # slug -> page json
        self.paths = {}         # (slug, cohort) -> path
        self.ids = {}           # (slug, cohort) -> dataset id
        self.latest = None
        self.fetched = []
        self.n = 0

    def release(self, slug, years17=None, years22=None, *, values17=None,
                values22=None, allz17=(), allz22=(), codes17=None,
                codes22=None, register=True):
        """Write a release's files; register its page and downloads."""
        self.n += 1
        d = self.root / f"r{self.n}"
        sets, out = [], {}
        for cohort, years, values, allz, codes in (
                ("17-21", years17, values17, allz17, codes17),
                ("22-25", years22, values22, allz22, codes22)):
            if not years:
                continue
            schema = schema_of(slug, cohort)
            rows = file_rows(schema, cohort, codes or CODES, list(years),
                             values=values, allz=allz)
            ds_id = f"{cohort[:2]}{slug}{self.n:02d}-0000-0000-0000-000000000000"
            path = write_csv(d / f"{cohort}.csv", rows)
            out[cohort] = path
            if register:
                self.urls[m.csv_url(ds_id)] = path
                self.paths[(slug, cohort)] = path
                self.ids[(slug, cohort)] = ds_id
            title = {("17-21", True): T17_NEW, ("17-21", False): T17_OLD,
                     ("22-25", True): T22_NEW, ("22-25", False): T22_OLD}[
                (cohort, schema == "care_leaver_2025")]
            sets.append(dataset(title, ds_id, min(years), max(years),
                                len(rows) - 1))
        if register:
            self.pages[slug] = page_json(slug, sets,
                                         published=f"{slug}-11-20T09:30:00")
        return out

    def run_main(self, cur, argv, *, table=True, latest=None):
        """main(argv) on the throwaway tables. Returns (rc or 'halt', text,
        borrowed, log mock)."""
        borrowed = _Borrowed(cur)
        api = {"latestRelease": {"slug": latest or self.latest},
               "nextReleaseDate": {"year": 2026, "month": 11}}

        def fetch_csv(url, dest, session=None):
            self.fetched.append(url)
            return self.urls[url]

        def fetch_page(slug, session=None):
            return html_of(self.pages[slug])

        out = io.StringIO()
        with mock.patch.object(m, "SPEC", ZZ), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_api", return_value=api), \
                mock.patch.object(m, "fetch_page", side_effect=fetch_page), \
                mock.patch.object(m, "fetch_csv", side_effect=fetch_csv), \
                mock.patch.object(m, "AUTHORITIES_RANGE", (1, 100)), \
                mock.patch.object(m, "log_run") as logged, \
                mock.patch.object(m, "table_exists",
                                  side_effect=lambda c, t: table
                                  and pe.table_exists(c, t)), \
                contextlib.redirect_stdout(out):
            try:
                rc = m.main(list(argv))
            except SystemExit as e:
                rc = "halt"
                out.write(f"\n{e.code}")
            finally:
                borrowed.tx.done()
        return rc, out.getvalue(), borrowed, logged

    def seed(self, cur, slug="2024", years17=range(2020, 2025),
             years22=(2023, 2024), **kw):
        self.release(slug, years17, years22, **kw)
        rc, text, _, _ = self.run_main(cur, ["load", "--release", slug,
                                             "--commit"])
        self.assertEqual(rc, 0, text)
        self.fetched.clear()
        return text


class LoaderDB(Fixture):

    def test_new_year_edition_1_live_and_two_ledger_rows_in_one_transaction(
            self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release("2025", range(2021, 2026), (2023, 2024, 2025))
            self.latest = "2025"
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertIn("2025: new", text)
            self.assertEqual(editions(cur, 2025), [(1, None, 2 * N)])
            self.assertEqual(count(cur, "zz_s4_live",
                                   "WHERE reporting_year = 2025"), 2 * N)
            self.assertEqual(core.rows_differing(cur, ZZ, "2025", 1), 0)
            self.assertEqual(live_sources(cur, 2025), [m.guidance_url("2025")])
            self.assertEqual(
                ledger(cur, 2025),
                [(m.ledger_source(m.csv_url(self.ids[("2025", c)]), "2025"),
                  "new", 1) for c in ("17-21", "22-25")])
            # the republished years are unchanged: ledger rows only
            self.assertEqual(editions(cur, 2023), [(1, None, 2 * N)])
            self.assertEqual([o for _, o, _ in ledger(cur, 2023)][-2:],
                             ["unchanged", "unchanged"])
            logged.assert_called_once()
            cur.execute("SELECT DISTINCT release_label, source_file FROM "
                        "public.zz_s4_editions WHERE reporting_year = 2025")
            (label, src), = cur.fetchall()
            self.assertEqual(src, m.guidance_url("2025"))
            self.assertIn("release 2025", label)
            self.assertIn(m.content_sha256(self.paths[("2025", "22-25")])[:16],
                          label)
            self.assertTrue(m.status(cur, ZZ)["ok"])

        # a failure inserting the live rows rolls back the edition and both
        # ledger rows too
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release("2025", range(2021, 2026), (2023, 2024, 2025))
            self.latest = "2025"
            with mock.patch.object(m, "insert_live",
                                   side_effect=RuntimeError("boom")):
                rc, text, _, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2025: FAILED", text)
            self.assertEqual(editions(cur, 2025), [])
            self.assertEqual(ledger(cur, 2025), [])
            self.assertEqual(count(cur, "zz_s4_live",
                                   "WHERE reporting_year = 2025"), 0)
            logged.assert_not_called()

    def test_unchanged_recheck_records_the_ledger_only_on_commit(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = ledger(cur)
            rc, text, _, _ = self.run_main(cur, ["load", "--release", "2024",
                                                 "--recheck", "2024"])
            self.assertEqual(rc, 0, text)
            self.assertIn("2024: unchanged", text)
            self.assertEqual(ledger(cur), before)
            rc, text, _, logged = self.run_main(
                cur, ["load", "--release", "2024", "--recheck", "2024",
                      "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertEqual(ledger(cur)[:len(before)], before)
            self.assertEqual([(o, e) for _, o, e in ledger(cur, 2024)],
                             [("new", 1), ("new", 1), ("unchanged", 1),
                              ("unchanged", 1)])
            self.assertEqual(editions(cur, 2024), [(1, None, 2 * N)])
            logged.assert_called_once()

    def test_a_url_and_sha_in_the_ledger_is_not_parsed_again(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            real = m.read_file
            calls = []
            with mock.patch.object(m, "read_file",
                                   side_effect=lambda p: calls.append(p)
                                   or real(p)):
                rc, text, _, logged = self.run_main(
                    cur, ["load", "--release", "2024", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertEqual(calls, [])
            self.assertIn("nothing parsed", text)
            self.assertIn("nothing to do", text)
            logged.assert_not_called()

    def test_revised_year_next_edition_live_untouched_until_refresh(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live(cur, 2023)
            self.release("2025", range(2021, 2026), (2023, 2024, 2025),
                         values17={(CODES[1], 2023, "17 to 18 years",
                                    "Foyers"): "5"})
            self.latest = "2025"
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertIn("2023: revised, 1 rows changed", text)
            self.assertEqual(editions(cur, 2023), [(1, None, 2 * N),
                                                   (2, 1, 2 * N)])
            self.assertEqual(live(cur, 2023), before)
            self.assertEqual(live_sources(cur, 2023), [m.guidance_url("2024")])
            self.assertEqual(ledger(cur, 2023)[-1][1:], ("revised", 2))
            self.assertEqual(m.status(cur, ZZ)["pending_refresh"], ["2023"])
            cur.execute("UPDATE public.zz_s4_live SET loaded_at = "
                        "'2020-01-01T00:00:00Z' WHERE reporting_year = 2023")
            rc, text, _, _ = self.run_main(cur, ["refresh-latest"])
            self.assertEqual(rc, 0, text)
            self.assertIn("2023=1", text)
            self.assertEqual(live(cur, 2023), before)
            rc, text, _, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, 0, text)
            after = live(cur, 2023)
            self.assertEqual(after[CODES[1]], before[CODES[1]] + 3)
            self.assertEqual(live(cur, 2023, "semi_independent")[CODES[1]],
                             12)
            # source is the latest edition's on EVERY row of the year
            self.assertEqual(live_sources(cur, 2023), [m.guidance_url("2025")])
            self.assertEqual(count(cur, "zz_s4_live",
                                   "WHERE reporting_year = 2023"), 2 * N)
            self.assertEqual(live_sources(cur, 2022), [m.guidance_url("2024")])
            # loaded_at copied from the edition (so refresh_map sees it)
            cur.execute("SELECT DISTINCT loaded_at FROM public.zz_s4_editions "
                        "WHERE reporting_year = 2023 AND edition = 2")
            (ed_at,), = cur.fetchall()
            cur.execute("SELECT loaded_at FROM public.zz_s4_live WHERE "
                        "reporting_year = 2023 AND lad24cd = %s AND "
                        "age_group = '17-21'", (CODES[1],))
            self.assertEqual(cur.fetchone()[0], ed_at)
            cur.execute("SELECT MAX(loaded_at) FROM public.zz_s4_live WHERE "
                        "reporting_year = 2023")
            self.assertEqual(cur.fetchone()[0], ed_at)
            self.assertTrue(m.status(cur, ZZ)["ok"])

    def test_key_changes_need_accept_key_changes(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            extra = "E10000007"
            years = range(2021, 2026)
            self.release("2025", years, (2023, 2024, 2025),
                         codes17=CODES + [extra], codes22=CODES,
                         allz17=[(extra, y) for y in years if y != 2023])
            self.latest = "2025"
            with mock.patch.object(m, "NATIONAL_CHANGE", 0.5):
                rc, text, _, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertIn("2023: revised", text)
            self.assertNotIn(extra, live(cur, 2023))
            rc, text, _, _ = self.run_main(cur, ["refresh-latest"])
            self.assertEqual(rc, 0, text)
            self.assertIn(f"2023: keys added 1 ({extra}/17-21)", text)
            rc, text, _, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("not named", text)
            self.assertNotIn(extra, live(cur, 2023))
            rc, text, _, _ = self.run_main(
                cur, ["refresh-latest", "--commit", "--accept-key-changes",
                      "2023"])
            self.assertEqual(rc, 0, text)
            self.assertIn(extra, live(cur, 2023))
            self.assertEqual(live_sources(cur, 2023), [m.guidance_url("2025")])
            self.assertTrue(m.status(cur, ZZ)["ok"])

    def seed_three(self, cur):
        """2019-2023 from a 2023 release (17-21 only, by --file), then the
        2025 release: 2021-2022 unchanged (their tip stays from 2023),
        2023 revised (22-25 added), 2024-2025 new."""
        r = self.release("2023", range(2019, 2024), None, register=False)
        rc, text, _, _ = self.run_main(
            cur, ["load", "--release", "2023", "--file-17-21",
                  str(r["17-21"]), "--commit"])
        self.assertEqual(rc, 0, text)
        self.release("2025", range(2021, 2026), (2023, 2024, 2025))
        self.latest = "2025"
        rc, text, _, _ = self.run_main(cur, ["load", "--commit"])
        self.assertEqual(rc, 0, text)
        self.fetched.clear()

    def test_older_release_is_skipped_per_year_on_every_path(self):
        with rolled_back(self.conn) as cur:
            self.seed_three(cur)
            self.assertEqual(editions(cur, 2021), [(1, None, N)])
            self.release("2024", range(2020, 2025), (2023, 2024),
                         values17={(CODES[0], 2020, "17 to 18 years",
                                    "Foyers"): "4",
                                   (CODES[0], 2022, "17 to 18 years",
                                    "Foyers"): "4"})
            rc, text, _, _ = self.run_main(cur, ["load", "--release", "2024",
                                                 "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertIn("compare [2020]", text)
            for y in (2021, 2022, 2023, 2024):
                self.assertIn(f"{y} (older: its tip is from the 2025 release",
                              text)
            self.assertEqual(editions(cur, 2020), [(1, None, N), (2, 1, N)])
            self.assertEqual(editions(cur, 2022), [(1, None, N)])

    def test_older_release_halts_when_every_year_is_skipped(self):
        with rolled_back(self.conn) as cur:
            self.seed_three(cur)
            snap = (editions(cur), ledger(cur))
            r = self.release("2024", range(2021, 2025), (2023, 2024),
                             values17={(CODES[0], 2022, "17 to 18 years",
                                        "Foyers"): "4"})
            paths = (
                ["load", "--release", "2024", "--commit"],
                ["load", "--release", "2024", "--commit",
                 "--dataset-17-21", self.ids[("2024", "17-21")],
                 "--dataset-22-25", self.ids[("2024", "22-25")]],
                ["load", "--release", "2024", "--commit",
                 "--file-17-21", str(r["17-21"]),
                 "--file-22-25", str(r["22-25"])],
                ["load", "--release", "2024"])
            for argv in paths:
                rc, text, _, logged = self.run_main(cur, argv)
                self.assertEqual(rc, "halt", (argv, text))
                self.assertIn("every year in the files is skipped", text)
                self.assertIn("--allow-older-file", text)
                self.assertEqual((editions(cur), ledger(cur)), snap)
                logged.assert_not_called()
            # the default path: the latest release older than held halts
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           latest="2024")
            self.assertEqual(rc, "halt", text)
            self.assertIn("older than the held latest year 2025", text)
            # --allow-older-file compares and stores anyway, and says so
            rc, text, _, logged = self.run_main(
                cur, ["load", "--release", "2024", "--commit",
                      "--allow-older-file"])
            self.assertEqual(rc, 0, text)
            self.assertIn("NOTE: --allow-older-file given", text)
            self.assertEqual(editions(cur, 2022), [(1, None, N), (2, 1, N)])
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s4_editions WHERE reporting_year = 2022 "
                        "AND edition = 2")
            self.assertIn("--allow-older-file", cur.fetchone()[0])
            self.assertIn("--allow-older-file", logged.call_args[0][2])

    def test_zero_to_null_needs_acknowledge(self):
        with rolled_back(self.conn) as cur:
            zeros = {(CODES[2], 2023, a, c): "0"
                     for a in ("17 to 18 years", "19 to 21 years")
                     for c in ("Bed and breakfast", "Emergency accommodation",
                               "No fixed abode/homeless")}
            self.seed(cur, values17=zeros)
            self.assertEqual(live(cur, 2023, "unsuitable")[CODES[2]], 0)
            flip = dict(zeros)
            flip[(CODES[2], 2023, "17 to 18 years", "Bed and breakfast")] = "c"
            self.release("2025", range(2021, 2026), (2023, 2024, 2025),
                         values17=flip)
            self.latest = "2025"
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2023: REJECTED, not stored", text)
            self.assertIn("0 to NULL", text)
            self.assertEqual(editions(cur, 2023), [(1, None, 2 * N)])
            self.assertEqual([o for _, o, _ in ledger(cur, 2023)],
                             ["new", "new"])
            logged.assert_not_called()
            self.assertEqual(editions(cur, 2025), [(1, None, 2 * N)])
            rc, text, _, _ = self.run_main(
                cur, ["load", "--commit", "--recheck", "2023",
                      "--acknowledge", "2024"])
            self.assertEqual(rc, "halt", text)
            rc, text, _, logged = self.run_main(
                cur, ["load", "--commit", "--recheck", "2023",
                      "--acknowledge", "2023"])
            self.assertEqual(rc, 0, text)
            self.assertIn("ACKNOWLEDGED", text)
            self.assertEqual(editions(cur, 2023), [(1, None, 2 * N),
                                                   (2, 1, 2 * N)])
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s4_editions WHERE reporting_year = 2023 "
                        "AND edition = 2")
            self.assertIn("ACKNOWLEDGED", cur.fetchone()[0])
            self.assertIn("ACKNOWLEDGED", logged.call_args[0][2])

    def test_one_cohort_release_year_against_a_two_cohort_tip_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            snap = editions(cur)
            r = self.release("2025", range(2021, 2026), None, register=False)
            rc, text, _, logged = self.run_main(
                cur, ["load", "--release", "2025", "--file-17-21",
                      str(r["17-21"]), "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2023: REJECTED", text)
            self.assertIn("2024: REJECTED", text)
            self.assertIn("no carry-forward", text)
            self.assertEqual(editions(cur, 2025), [(1, None, N)])
            self.assertEqual(len(snap), 1)
            self.assertEqual(editions(cur, 2023), [(1, None, 2 * N)])
            self.assertEqual(editions(cur, 2024), [(1, None, 2 * N)])
            logged.assert_not_called()

    def test_rejected_year_stores_nothing_and_exits_1(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            led = ledger(cur, 2023)
            big = {(c, 2023, "17 to 18 years", "Total"): "90"
                   for c in CODES[:4]}
            self.release("2025", range(2021, 2026), (2023, 2024, 2025),
                         values17=big)
            self.latest = "2025"
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("total_published changes by more than 25% in 4 "
                          "authorities", text)
            self.assertIn("REJECTED 1 year(s)", text)
            self.assertEqual(editions(cur, 2023), [(1, None, 2 * N)])
            self.assertEqual(ledger(cur, 2023), led)
            logged.assert_not_called()

    def test_preview_and_simulate_write_nothing(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release("2025", range(2021, 2026), (2023, 2024, 2025),
                         values17={(CODES[1], 2023, "17 to 18 years",
                                    "Foyers"): "5"})
            self.latest = "2025"

            def snap():
                cur.execute("SELECT md5(string_agg(t::text, ',' ORDER BY "
                            "t::text)) FROM public.zz_s4_live t")
                h = cur.fetchone()[0]
                return (editions(cur), ledger(cur), h, run_log_count(cur))
            before = snap()
            for flags in ([], ["--simulate"]):
                rc, text, borrowed, logged = self.run_main(cur,
                                                           ["load"] + flags)
                self.assertEqual(rc, 0, text)
                self.assertIn("2025: new", text)
                self.assertIn("2023: revised", text)
                self.assertIn("PREVIEW" if not flags else "SIMULATION", text)
                logged.assert_not_called()
                self.assertEqual(borrowed.commits, 0)
                self.assertEqual(snap(), before)
            rc, text, _, _ = self.run_main(cur, ["refresh-latest"])
            self.assertEqual(snap(), before)

    def test_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release("2025", range(2021, 2026), (2023, 2024, 2025))
            self.latest = "2025"
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 0, text)
            snap = (editions(cur), ledger(cur))
            rc, text, _, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertIn("nothing parsed", text)
            self.assertEqual((editions(cur), ledger(cur)), snap)
            logged.assert_not_called()

    def test_stranded_year_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("DELETE FROM public.zz_s4_live WHERE "
                        "reporting_year = 2022")
            self.assertEqual(m.status(cur, ZZ)["live_missing"], ["2022"])
            rc, text, _, _ = self.run_main(cur, ["load", "--release", "2024",
                                                 "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertIn("no new edition", text)
            self.assertEqual(editions(cur, 2022), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s4_live",
                                   "WHERE reporting_year = 2022"), N)
            self.assertEqual(ledger(cur, 2022)[-1][1], "live-missing")
            self.assertTrue(m.status(cur, ZZ)["ok"])

    def test_two_files_of_different_releases_halt(self):
        with rolled_back(self.conn) as cur:
            a = self.release("2024", range(2020, 2025), None, register=False)
            b = self.release("2025", None, (2023, 2024, 2025),
                             register=False)
            rc, text, _, _ = self.run_main(
                cur, ["load", "--release", "2025", "--file-17-21",
                      str(a["17-21"]), "--file-22-25", str(b["22-25"])])
            self.assertEqual(rc, "halt", text)
            self.assertIn("own latest year is 2024, the release is 2025",
                          text)

    def test_identity_from_the_page_is_checked(self):
        with rolled_back(self.conn) as cur:
            self.release("2024", range(2020, 2025), (2023, 2024))
            sets = self.pages["2024"]["props"]["pageProps"]["dataContent"][
                "dataSets"]
            sets[0]["meta"]["numDataFileRows"] += 1
            rc, text, _, _ = self.run_main(cur, ["load", "--release", "2024",
                                                 "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("identity check failed", text)
            self.assertEqual(editions(cur), [])
            # a page whose slug is not the API's release halts
            self.pages["2026"] = self.pages["2024"]
            rc, text, _, _ = self.run_main(cur, ["load"], latest="2026")
            self.assertEqual(rc, "halt", text)
            self.assertIn("data guidance page is release 2024", text)

    def test_barnsley_new_code_halts(self):
        with rolled_back(self.conn) as cur:
            self.release("2024", range(2020, 2025), (2023, 2024),
                         codes17=CODES + ["E08000038"])
            rc, text, _, _ = self.run_main(cur, ["load", "--release", "2024"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("declared 'old'", text)

    def test_status_ddl_and_restore_edition(self):
        with rolled_back(self.conn) as cur:
            rc, text, _, _ = self.run_main(cur, ["status"], table=False)
            self.assertEqual(rc, 1, text)
            self.assertIn("does not exist yet", text)
            self.assertIn("migrate-legacy", text)
            self.seed(cur)
            self.release("2025", range(2021, 2026), (2023, 2024, 2025),
                         values17={(CODES[1], 2023, "17 to 18 years",
                                    "Foyers"): "5"},
                         values22={(CODES[0], 2023, "22 years",
                                    "No information"): "c"})
            self.latest = "2025"
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 0, text)
            rc, text, _, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2023: tip edition 2", text)
            self.assertIn("not populated (0 rows with reasons, the tip 1",
                          text)
            self.assertIn("NEWER EDITION NOT YET IN LIVE (run refresh-latest): "
                          "2023", text)
            rc, text, _, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, 0, text)
            rc, text, _, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 0, text)
            self.assertIn("2023: tip edition 2 (release 2025", text)
            self.assertIn("live source uniform", text)
            self.assertIn("null_reasons populated (1 rows with reasons", text)
            # restore-edition: preview writes nothing; commit stores edition
            # 1's rows as edition 3, which refresh-latest then applies
            before = live(cur, 2023)
            rc, text, _, _ = self.run_main(cur, ["restore-edition", "2023",
                                                 "1"])
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, 2023)[-1][0], 2)
            rc, text, _, logged = self.run_main(
                cur, ["restore-edition", "2023", "1", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, 2023), [(1, None, 2 * N),
                                                   (2, 1, 2 * N),
                                                   (3, 2, 2 * N)])
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s4_editions WHERE reporting_year = 2023 "
                        "AND edition = 3")
            self.assertEqual(cur.fetchone()[0], "restored from edition 1")
            logged.assert_called_once()
            rc, text, _, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertEqual(live(cur, 2023)[CODES[1]], before[CODES[1]] - 3)
            self.assertEqual(core.rows_differing(cur, ZZ, "2023", 3), 0)
            rc, text, _, _ = self.run_main(cur, ["restore-edition", "2023",
                                                 "3"])
            self.assertEqual(rc, "halt", text)

    def test_ddl_adds_null_reasons_and_the_ledger(self):
        with rolled_back(self.conn) as cur:
            cur.execute(f"DROP TABLE public.{LEDGER}")
            cur.execute("ALTER TABLE public.zz_s4_live DROP COLUMN "
                        "null_reasons")
            rc, text, _, _ = self.run_main(cur, ["ddl"])
            self.assertEqual(rc, 0, text)
            self.assertFalse(pe.table_exists(cur, LEDGER))
            rc, text, _, _ = self.run_main(cur, ["ddl", "--commit"])
            self.assertEqual(rc, 0, text)
            self.assertTrue(pe.table_exists(cur, LEDGER))
            self.assertTrue(m.column_exists(cur, "zz_s4_live",
                                            "null_reasons"))
            # the editions table refuses update, delete and truncate
            self.seed(cur)
            for sql in ("UPDATE public.zz_s4_editions SET edition = 9",
                        "DELETE FROM public.zz_s4_editions",
                        "TRUNCATE public.zz_s4_editions"):
                cur.execute("SAVEPOINT t")
                with self.assertRaises(Exception):
                    cur.execute(sql)
                cur.execute("ROLLBACK TO SAVEPOINT t")

    def test_writing_without_tables_halts_and_preview_compares_with_live(
            self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release("2025", range(2021, 2026), (2023, 2024, 2025),
                         values17={(CODES[1], 2023, "17 to 18 years",
                                    "Foyers"): "5"})
            self.latest = "2025"
            rc, text, _, _ = self.run_main(cur, ["load", "--commit"],
                                           table=False)
            self.assertEqual(rc, "halt", text)
            self.assertIn("run `ddl --commit`", text)
            rc, text, _, _ = self.run_main(cur, ["load"], table=False)
            self.assertEqual(rc, 0, text)
            self.assertIn("compares each year with the LIVE table", text)
            self.assertIn("2023: revised, 1 rows changed (compared with live)",
                          text)
            self.assertIn("2025: new", text)


class Migration(Fixture):
    """migrate-legacy on a seeded legacy table and fixture files."""

    def files(self):
        d = self.root / "legacy"
        c = CODES[:4]
        f1 = write_csv(d / "f17_2023.csv", file_rows(
            "accommodation_type", "17-21", c + ["E06000028"],
            range(2019, 2024),
            values={(CODES[0], y, "17 to 18 years", "Foyers"): "c"
                    for y in range(2019, 2024)},
            allz=[("E06000028", y) for y in range(2020, 2024)]
            + [(CODES[3], 2019)]))
        f2 = write_csv(d / "f17_2025.csv", file_rows(
            "care_leaver_2025", "17-21", c, range(2021, 2026),
            values={(CODES[1], y, "19 to 21 years", "In custody"): "c"
                    for y in range(2021, 2026)}))
        f3 = write_csv(d / "f22_2025.csv", file_rows(
            "care_leaver_2025", "22-25", c + ["E10000003"], (2023, 2024, 2025),
            values={(CODES[2], y, "25 years",
                     "Accommodation considered suitable"): "c"
                    for y in (2023, 2024, 2025)}))
        self.fx = {
            "f17_2023": dict(m.LEGACY_FILES["f17_2023"],
                             sha256=m.content_sha256(f1)),
            "f17_2025": dict(m.LEGACY_FILES["f17_2025"],
                             sha256=m.content_sha256(f2)),
            "f22_2025": dict(m.LEGACY_FILES["f22_2025"],
                             sha256=m.content_sha256(f3))}
        return f1, f2, f3

    def seed_legacy(self, cur, f1, f2, f3):
        """The held table as the old builds left it: 17-21 with c added as
        0, BCP 2019 all zeros instead of Bournemouth, an all-z authority
        held as zeros; 22-25 the age-25 row, no county."""
        def coerced(path):
            text = path.read_text(encoding="utf-8")
            q = path.with_name(path.stem + "_coerced.csv")
            q.write_text(text.replace(",c,k", ",0,k").replace(",z,k", ",0,k"),
                         encoding="utf-8")
            return m.read_file(q)
        rows = []
        for path, years in ((f1, (2019, 2020)),
                            (f2, (2021, 2022, 2023, 2024, 2025))):
            b = m.build_rows(coerced(path), lambda c: c)
            for y in years:
                for r in b[y]:
                    if r["lad24cd"] == "E06000028":
                        if y != 2019:
                            continue      # all z: lost on the BCP key
                        r = dict(r, lad24cd="E06000058",
                                 **{c: 0 for c in m.BUILT["17-21"]})
                    rows.append(r)
        f22 = m.read_file(f3)
        for (code, y), cells in sorted(f22["rows"].items()):
            if code.startswith("E10"):
                continue
            r = {c: None for c in m.DATA_COLUMNS}
            r.update(lad24cd=code, age_group="22-25", reporting_year=str(y),
                     uasc_impact_flag=False, suppressed_flag=False)
            for col, cat in m.AGE25:
                r[col] = cells[("25", cat)][0]
            rows.append(r)
        for r in rows:
            r["attribution"] = r["successor_codes"] = None
            r["attribution_note"] = None
            r["null_reasons"] = None
            cols = ("lad24cd", "reporting_year", "age_group") + tuple(
                c for c in m.DATA_COLUMNS) + ("source",)
            cur.execute(f"INSERT INTO public.zz_s4_live ({', '.join(cols)}) "
                        f"VALUES ({', '.join(['%s'] * len(cols))})",
                        [r[c] for c in cols[:-1]]
                        + [f"DfE Children Looked After SSDA903, reporting "
                           f"year {r['reporting_year']}, age group "
                           f"{r['age_group']}"])
        return len(rows)

    def migrate(self, cur, files, *, write=True):
        n, h = m.live_state(cur, ZZ)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                plan = m.migrate_legacy(cur, *files, write=write, spec=ZZ,
                                        live_rows=n, live_hash=h,
                                        files=self.fx)
            except SystemExit as e:
                return "halt", out.getvalue() + f"\n{e.code}"
        return plan, out.getvalue()

    def test_migration_proof_passes_and_stores_two_editions_per_year(self):
        with rolled_back(self.conn) as cur:
            files = self.files()
            n = self.seed_legacy(cur, *files)
            state = m.live_state(cur, ZZ)
            plan, text = self.migrate(cur, files)
            self.assertNotEqual(plan, "halt", text)
            self.assertIn("0 differences", text)
            self.assertIn(f"{plan['rows25']} held 22-25 rows equal", text)
            self.assertEqual(plan["rows25"], 12)
            self.assertGreater(plan["cells"], 100)
            self.assertIn("dropped E06000058 17-21: BCP 2019", text)
            self.assertIn(f"dropped {CODES[3]} 17-21: all z in the file", text)
            self.assertIn("added E06000028 17-21: declared predecessor", text)
            self.assertIn("added E10000003 22-25: 22-25 county council", text)
            self.assertEqual(m.live_state(cur, ZZ), state)       # untouched
            for y in range(2019, 2026):
                eds = editions(cur, y)
                self.assertEqual([(e, s) for e, s, _ in eds],
                                 [(1, None), (2, 1)], y)
                self.assertEqual(core.rows_differing(cur, ZZ, str(y), 1), 0)
            self.assertEqual(sum(r for e, _, r in editions(cur) if e == 1), n)
            cur.execute("SELECT foyers, semi_independent, total_care_leavers,"
                        " null_reasons FROM public.zz_s4_editions WHERE "
                        "reporting_year = 2019 AND edition = 2 AND "
                        "lad24cd = %s", (CODES[0],))
            foy, semi, tot, why = cur.fetchone()
            self.assertEqual((foy, semi, tot), (None, None, None))
            self.assertIn("foyers=suppressed", why)
            cur.execute("SELECT total_care_leavers, total_published FROM "
                        "public.zz_s4_editions WHERE reporting_year = 2025 "
                        "AND edition = 2 AND lad24cd = %s AND age_group = "
                        "'22-25'", (CODES[0],))
            self.assertEqual(cur.fetchone(), (32, 32))           # four ages
            cur.execute("SELECT DISTINCT source_file FROM "
                        "public.zz_s4_editions WHERE edition = 2 ORDER BY 1")
            self.assertEqual([r[0] for r in cur.fetchall()],
                             [m.guidance_url("2023"), m.guidance_url("2025")])
            self.assertEqual(ledger(cur, 2019),
                             [(m.ledger_source(m.csv_url(
                                 m.LEGACY_FILES["f17_2023"]["dataset"]),
                                 2023), "revised", 2)])
            self.assertEqual(len(ledger(cur, 2024)), 2)
            st = m.status(cur, ZZ)
            self.assertEqual(st["pending_refresh"],
                             [str(y) for y in range(2019, 2026)])
            # the refresh with key changes then makes live the correction
            borrowed_args = ["refresh-latest", "--commit"] + [
                x for y in (2019, 2023, 2024, 2025)
                for x in ("--accept-key-changes", str(y))]
            rc, text, _, _ = Fixture.run_main(self, cur, borrowed_args)
            self.assertEqual(rc, 0, text)
            self.assertTrue(m.status(cur, ZZ)["ok"])
            self.assertEqual(live_sources(cur, 2019), [m.guidance_url("2023")])
            # a second run is refused
            plan, text = self.migrate(cur, files)
            self.assertEqual(plan, "halt", text)
            self.assertIn("runs once", text)

    def test_preview_writes_nothing(self):
        with rolled_back(self.conn) as cur:
            files = self.files()
            self.seed_legacy(cur, *files)
            plan, text = self.migrate(cur, files, write=False)
            self.assertNotEqual(plan, "halt", text)
            self.assertEqual(editions(cur), [])
            self.assertEqual(ledger(cur), [])

    def test_a_planted_held_difference_stops(self):
        with rolled_back(self.conn) as cur:
            files = self.files()
            self.seed_legacy(cur, *files)
            cur.execute("UPDATE public.zz_s4_live SET independent_living = "
                        "independent_living + 1 WHERE lad24cd = %s AND "
                        "reporting_year = 2024 AND age_group = '17-21'",
                        (CODES[1],))
            plan, text = self.migrate(cur, files)
            self.assertEqual(plan, "halt", text)
            self.assertIn("proof failed: 1 corrected 17-21 cells differ", text)
            self.assertEqual(editions(cur), [])
            cur.execute("UPDATE public.zz_s4_live SET independent_living = "
                        "independent_living - 1 WHERE lad24cd = %s AND "
                        "reporting_year = 2024 AND age_group = '17-21'",
                        (CODES[1],))
            cur.execute("UPDATE public.zz_s4_live SET not_known = 7 WHERE "
                        "lad24cd = %s AND reporting_year = 2024 AND "
                        "age_group = '22-25'", (CODES[1],))
            plan, text = self.migrate(cur, files)
            self.assertEqual(plan, "halt", text)
            self.assertIn("age-25 row", text)

    def test_an_unexplained_dropped_key_stops(self):
        with rolled_back(self.conn) as cur:
            files = self.files()
            self.seed_legacy(cur, *files)
            cur.execute("INSERT INTO public.zz_s4_live (lad24cd, "
                        "reporting_year, age_group, foyers) VALUES "
                        "('E06000005', 2022, '17-21', 4)")
            plan, text = self.migrate(cur, files)
            self.assertEqual(plan, "halt", text)
            self.assertIn("does not explain", text)
            self.assertIn("E06000005", text)
            self.assertEqual(editions(cur), [])

    def test_preconditions_live_state_and_file_sha(self):
        with rolled_back(self.conn) as cur:
            files = self.files()
            self.seed_legacy(cur, *files)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    self.assertRaises(SystemExit) as e:
                m.migrate_legacy(cur, *files, write=True, spec=ZZ,
                                 files=self.fx)
            self.assertIn("not as surveyed", str(e.exception.code))
            self.fx["f22_2025"]["sha256"] = "0" * 64
            plan, text = self.migrate(cur, files)
            self.assertEqual(plan, "halt", text)
            self.assertIn("expected 00000000", text)

    def test_a_failure_while_writing_rolls_everything_back(self):
        with rolled_back(self.conn) as cur:
            files = self.files()
            self.seed_legacy(cur, *files)
            n, h = m.live_state(cur, ZZ)
            real = m.migration_label

            def boom(y, py):
                if y == 2022:
                    raise RuntimeError("boom")
                return real(y, py)
            with mock.patch.object(m, "LIVE_ROWS", n), \
                    mock.patch.object(m, "LIVE_HASH", h), \
                    mock.patch.object(m, "LEGACY_FILES", self.fx), \
                    mock.patch.object(m, "migration_label", side_effect=boom):
                with self.assertRaises(RuntimeError):
                    Fixture.run_main(self, cur, ["migrate-legacy",
                                                 *map(str, files),
                                                 "--commit"])
            self.assertEqual(editions(cur), [])
            self.assertEqual(ledger(cur), [])


class Geography(unittest.TestCase):
    """resolve_codes against the real lookup tables (read only)."""

    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()
        cls.cur = cls.conn.cursor()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_north_yorkshire_resolves_one_to_one(self):
        rmap, problems = m.resolve_codes(self.cur, {"E10000023", "E08000016"},
                                         {2023: {"E10000023"}})
        self.assertEqual(problems, [])
        self.assertEqual(rmap["E10000023"], "E06000065")
        self.assertEqual(rmap["E08000016"], "E08000016")

    def test_barnsley_new_code_is_a_problem(self):
        _, problems = m.resolve_codes(self.cur, {"E08000038"}, {})
        self.assertTrue(any("declared 'old'" in p for p in problems))

    def test_predecessor_stays_on_its_code_and_merger_is_not_applied(self):
        rmap, problems = m.resolve_codes(
            self.cur, {"E06000028", "E06000029", "E10000006"}, {})
        self.assertEqual(problems, [])
        self.assertEqual(rmap["E06000028"], "E06000028")
        self.assertEqual(rmap["E10000006"], "E10000006")

    def test_undeclared_unknown_code_is_unexplained(self):
        _, problems = m.resolve_codes(self.cur, {"E06000099"}, {})
        self.assertTrue(any("UNEXPLAINED E06000099" in p for p in problems))

    def test_two_codes_to_one_key_raise(self):
        with self.assertRaises(ValueError) as e:
            m.resolve_codes(self.cur, {"E10000023", "E06000065"},
                            {2024: {"E10000023", "E06000065"}})
        self.assertIn("E06000065", str(e.exception))


class _Resp:
    def __init__(self, content=b"", text="", status=200):
        self.content, self.text, self.status = content, text, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, link, **kw):
        self.calls.append((link, kw))
        return self.responses.pop(0)


class Fetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "raw" / "2025_1721_accommodation_aaaaaaaa.csv"
        self.url = m.csv_url("aaaaaaaa-0000")

    def body(self, bump="2"):
        rows = file_rows("care_leaver_2025", "17-21", ["E06000001"], [2025],
                         values={("E06000001", 2025, "17 to 18 years",
                                  "Foyers"): bump})
        buf = io.StringIO()
        import csv
        csv.writer(buf).writerows(rows)
        return buf.getvalue().encode("utf-8")

    def fetch(self, body):
        s = _Session(_Resp(content=body))
        with quiet(), mock.patch.object(m, "MIN_FILE_BYTES", 100):
            return m.fetch_csv(self.url, self.dest, s), s

    def test_download_kept_under_the_given_name_with_user_agent(self):
        path, s = self.fetch(self.body())
        self.assertEqual(path, self.dest)
        self.assertTrue(path.exists())
        self.assertIn("User-Agent", s.calls[0][1]["headers"])
        self.assertEqual(m.read_file(path)["cohort"], "17-21")

    def test_same_name_same_content_is_kept(self):
        b = self.body()
        first, _ = self.fetch(b)
        mtime = first.stat().st_mtime_ns
        second, _ = self.fetch(b)
        self.assertEqual(second, first)
        self.assertEqual(second.stat().st_mtime_ns, mtime)

    def test_same_name_different_content_is_saved_beside_it(self):
        first, _ = self.fetch(self.body())
        before = m.content_sha256(first)
        second, _ = self.fetch(self.body("7"))
        self.assertEqual(m.content_sha256(first), before)
        sha8 = m.content_sha256(second)[:8]
        self.assertEqual(second.name, f"{first.stem}-{sha8}.csv")
        foy = m.read_file(second)["rows"][("E06000001", 2025)][
            ("17 to 18 years", "Foyers")]
        self.assertEqual(foy, (7, None))

    def test_small_or_wrong_files_halt_and_keep_nothing(self):
        s = _Session(_Resp(content=b"tiny"))
        with quiet(), self.assertRaises(SystemExit):
            m.fetch_csv(self.url, self.dest, s)
        bad = self.body().replace(b"care_leaver_count", b"leaver_count")
        s = _Session(_Resp(content=bad))
        with quiet(), mock.patch.object(m, "MIN_FILE_BYTES", 100), \
                self.assertRaises(SystemExit) as e:
            m.fetch_csv(self.url, self.dest, s)
        self.assertIn("unknown header", str(e.exception.code))
        self.assertFalse(self.dest.parent.exists()
                         and any(self.dest.parent.iterdir()))

    def test_http_failure_halts(self):
        s = _Session(_Resp(status=500))
        with quiet(), self.assertRaises(SystemExit):
            m.fetch_csv(self.url, self.dest, s)


class SpecTests(unittest.TestCase):
    def test_spec_matches_live_columns(self):
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT column_name, data_type FROM "
                        "information_schema.columns WHERE table_name = "
                        "'care_leaver_accommodation'")
            live_cols = dict(cur.fetchall())
        finally:
            conn.rollback()
            conn.close()
        want = {"integer": "integer", "numeric": "numeric",
                "boolean": "boolean", "text": "text", "text[]": "ARRAY"}
        for col, typ in m.SPEC.value_cols + m.SPEC.extra_cols:
            if col == "null_reasons" and col not in live_cols:
                continue           # added by ddl
            self.assertEqual(live_cols[col], want[typ], col)
        self.assertEqual(live_cols["lad24cd"], "character varying")
        self.assertEqual(live_cols["reporting_year"], "integer")
        self.assertEqual(m.SPEC.refresh_from, (("source", "source_file"),
                                               ("loaded_at", "loaded_at")))
        self.assertTrue(m.SPEC.refresh_key_changes)
        self.assertTrue(m.SPEC.refresh_source_whole_period)
        self.assertFalse(m.SPEC.fk_la_boundaries)
        self.assertTrue(m.PROFILE.file_checks)
        self.assertEqual(m.PROFILE.savepoint, "s4_year")
        self.assertEqual(m.PROFILE.run_agent,
                         "Source 4 - DfE Care Leaver Accommodation")
        # every spec data column exists in live (refresh inserts them)
        for c in m.DATA_COLUMNS:
            self.assertTrue(c in live_cols or c == "null_reasons", c)

    def test_no_sync_new_and_accept_key_changes_parsed(self):
        with self.assertRaises(SystemExit), quiet(), \
                contextlib.redirect_stderr(io.StringIO()):
            m.main(["sync-new"])
        with mock.patch.object(m, "cmd_refresh_latest",
                               side_effect=lambda a: a) as f:
            args = m.main(["refresh-latest", "--accept-key-changes", "2019",
                           "--accept-key-changes", "2020"])
        self.assertEqual(args.accept_key_changes, ["2019", "2020"])
        f.assert_called_once()


if __name__ == "__main__":
    unittest.main()
