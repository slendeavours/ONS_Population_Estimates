"""Tests for the S21 loader in s21_neighbours_editions (the commands, the
live release_date column, LIVE_RULE in live, migrate-legacy,
restore-edition) on throwaway tables.

No network: the ONS /data replies and the downloads are stubbed, and every
.xlsx file is written by the test (write_s21 from test_s21_neighbours_pure).
Database tests run on a connection from _db.get_conn() inside a transaction
that is always rolled back (rolled_back below); nothing here commits. The
commands get a stand-in connection whose commit and rollback act on a
savepoint of that transaction, so the loader's per-period commit never
reaches the real connection. The only tables written are zz_s21_live (a
LIKE copy of la_statistical_neighbours, given its release_date column by
the ddl code under test), zz_s21_editions and its ledger, created inside
that transaction (refused if any already exists), so they never persist.
The run log is stubbed; the real la_statistical_neighbours, la_code_lookup,
la_boundaries and information_schema are read (never written). Nothing here
imports or runs the old build (scripts/s21_statistical_neighbours_build.py).
"""
import contextlib
import dataclasses
import io
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s21_neighbours_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s21_neighbours_pure import (EDITION_URI, ENGLISH, FILE,  # noqa: E402
                                      OVERRIDES, _Session, edition_page,
                                      landing, resolve_all, write_s21)

ZZ = dataclasses.replace(m.SPEC, name="zz_s21", live_table="zz_s21_live",
                         editions_table="zz_s21_editions")
LEDGER = "zz_s21_editions_file_checks"
P = "2026-03-18"
P27 = "2027-03-04"
P25 = "2025-03-12"
N_ED = len(ENGLISH) * 20          # editions rows per release
N_LIVE = len(ENGLISH) * 5         # live rows under LIVE_RULE
LIVE_COLS = ("lad24cd", "statistical_neighbour_lad24cd", "neighbour_rank",
             "source", "loaded_at")
HELD_SOURCE = ("ONS Clustering similar LAs and statistical nearest "
               "neighbours, Table 7a (LTLA global), Mar-2026 edition")
HELD_LOADED = "2026-07-24 08:56:26.598029+00"
CHANGED = dict(OVERRIDES, E09000033={2: "E07000008"})


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    SP = "zz_s21_cmd"

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
def specs(root=None):
    with mock.patch.object(m, "SPEC", ZZ), \
            mock.patch.object(m, "EXPECTED_SUBJECTS", len(ENGLISH)), \
            mock.patch.object(m, "FILE_ROOTS", (root,) if root else
                              m.FILE_ROOTS):
        yield


@contextmanager
def rolled_back(conn, *, ddl=True, real_rows=False):
    cur = conn.cursor()
    try:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('zz_s21_live', 'zz_s21_editions', %s)", (LEDGER,))
        if cur.fetchall():
            raise RuntimeError("a zz_s21 table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s21_live (LIKE "
                    "public.la_statistical_neighbours INCLUDING ALL)")
        # the copy starts from the held shape, before the ddl under test
        # adds the release_date column (the real table gains it in Task 3)
        cur.execute("ALTER TABLE public.zz_s21_live DROP COLUMN IF EXISTS "
                    "release_date")
        if real_rows:
            cols = ", ".join(LIVE_COLS)
            cur.execute(f"INSERT INTO public.zz_s21_live ({cols}) "
                        f"SELECT {cols} FROM public.la_statistical_neighbours")
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


def editions(cur, period=P):
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM "
                "public.zz_s21_editions WHERE release_date = %s "
                "GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur):
    cur.execute(f"SELECT release_date::text, source_file, outcome, edition "
                f"FROM public.{LEDGER} ORDER BY id")
    return cur.fetchall()


def live(cur, subject=None):
    """{(subject, neighbour): rank} of the throwaway live table."""
    cur.execute("SELECT lad24cd, statistical_neighbour_lad24cd, "
                "neighbour_rank FROM public.zz_s21_live"
                + (" WHERE lad24cd = %s" if subject else ""),
                (subject,) if subject else None)
    return {(a, b): r for a, b, r in cur.fetchall()}


def live_md5(cur):
    expr = "||'|'||".join(f"coalesce({c}::text,'~')" for c in LIVE_COLS)
    cur.execute(f"SELECT COUNT(*), md5(string_agg({expr}, E'\\n' ORDER BY "
                "lad24cd, statistical_neighbour_lad24cd)) FROM "
                "public.zz_s21_live")
    return cur.fetchone()


class Fixture(unittest.TestCase):

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
        self.api = {m.LANDING: landing()}
        self.urls = {}
        self.final = {}
        self.fetched = []
        self.n = 0

    def edition(self, edition, file_name, release):
        lp = self.api[m.LANDING]
        uri = EDITION_URI.format(edition)
        if not any(d["uri"] == uri for d in lp["datasets"]):
            lp["datasets"].append({"uri": uri})
        self.api[uri] = edition_page(edition, files=(file_name,),
                                     release=release)
        return m.download_url(uri, file_name)

    def file(self, published="18 March 2026", *, edition=None, release=None,
             register=True, name=None, **kw):
        """Write a workbook; register its edition page and download."""
        self.n += 1
        name = name or f"t{self.n}{FILE}"
        path = write_s21(self.root / f"t{self.n}" / name,
                         published=published, **kw)
        if register:
            d = datetime_of(published)
            url = self.edition(edition or str(d.year), name,
                               release or f"{d.isoformat()}T00:00:00.000Z")
            self.urls[url] = path
            self.last_url = url
        return path

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, log)."""
        borrowed = _Borrowed(cur)

        def fetch(url, dest, session=None):
            self.fetched.append(url)
            return self.urls[url], self.final.get(url, url)

        out = io.StringIO()
        with specs(self.root), \
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

    def seed(self, cur, **kw):
        self.file(**kw)
        self.ok(cur, ["load", "--commit"])
        self.fetched.clear()


def datetime_of(published):
    from datetime import datetime
    return datetime.strptime(published, "%d %B %Y").date()


class LiveColumn(Fixture):

    def test_the_release_date_alter_on_a_copy_is_additive(self):
        with rolled_back(self.conn, ddl=False, real_rows=True) as cur:
            before = live_md5(cur)
            self.assertEqual(before[0], 1480)
            m.create_all(cur)
            self.assertEqual(live_md5(cur), before)
            self.assertEqual(count(cur, "zz_s21_live",
                                   f"WHERE release_date = '{P}'"), 1480)
            cur.execute("SELECT data_type, is_nullable, column_default FROM "
                        "information_schema.columns WHERE table_schema = "
                        "'public' AND table_name = 'zz_s21_live' AND "
                        "column_name = 'release_date'")
            self.assertEqual(cur.fetchone(), ("date", "NO",
                                              f"'{P}'::date"))
            self.assertIn("ALTER TABLE public.zz_s21_live ADD COLUMN "
                          "release_date date NOT NULL DEFAULT '2026-03-18' "
                          "CHECK (release_date = '2026-03-18')",
                          m.live_alter_sql(ZZ))
            cur.execute("SAVEPOINT zz_s21_chk")
            with self.assertRaises(Exception) as e:
                cur.execute("UPDATE public.zz_s21_live SET release_date = "
                            "'2027-03-04' WHERE lad24cd = 'E06000001'")
            self.assertIn("check", str(e.exception).lower())
            cur.execute("ROLLBACK TO SAVEPOINT zz_s21_chk")
            m.create_all(cur)                       # idempotent
            self.assertEqual(live_md5(cur), before)

    def test_a_release_date_column_of_another_shape_halts(self):
        with rolled_back(self.conn, ddl=False) as cur:
            cur.execute("ALTER TABLE public.zz_s21_live ADD COLUMN "
                        "release_date text")
            with self.assertRaises(SystemExit) as e:
                m.create_all(cur)
            self.assertIn("release_date", str(e.exception.code))

    def test_spec_types_match_the_live_table(self):
        with rolled_back(self.conn, ddl=False) as cur:
            cur.execute("SELECT column_name, data_type, character_maximum_"
                        "length FROM information_schema.columns WHERE "
                        "table_schema = 'public' AND table_name = "
                        "'la_statistical_neighbours'")
            got = {r[0]: r[1:] for r in cur.fetchall()}
            self.assertEqual(got["lad24cd"], ("character varying", 9))
            self.assertEqual(got["statistical_neighbour_lad24cd"],
                             ("character varying", 9))
            self.assertEqual(got["neighbour_rank"][0], "integer")
            self.assertEqual(got["source"][0], "text")

    def test_ddl_preview_shows_the_alter_then_commit_then_status(self):
        with rolled_back(self.conn, ddl=False) as cur:
            text, _ = self.ok(cur, ["ddl"])
            self.assertIn(m.live_alter_sql(ZZ), text)
            self.assertIn("DRY RUN", text)
            self.assertFalse(pe.table_exists(cur, "zz_s21_editions"))
            self.ok(cur, ["ddl", "--commit"])
            for t in ("zz_s21_editions", LEDGER):
                self.assertTrue(pe.table_exists(cur, t), t)
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 0, text)
            text, _ = self.ok(cur, ["ddl"])
            self.assertIn("release_date column present", text)


class Load(Fixture):

    def test_first_vintage_stores_edition_live_and_ledger_together(self):
        with rolled_back(self.conn) as cur:
            self.file()
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P}: new", text)
            self.assertIn(f"{N_LIVE} live rows", text)
            self.assertEqual(editions(cur), [(1, None, N_ED)])
            self.assertEqual(count(cur, "zz_s21_live"), N_LIVE)
            self.assertEqual(m.live_equals_tip(cur, ZZ, [P]), [])
            (p, src, outcome, ed), = ledger(cur)
            self.assertEqual((p, outcome, ed), (P, "new", 1))
            self.assertEqual(src, f"{self.last_url} (release {P}; published "
                                  f"{P})")
            cur.execute("SELECT DISTINCT source_file, published_date, "
                        "release_label FROM public.zz_s21_editions")
            (sf, pd_, label), = cur.fetchall()
            self.assertEqual(m.release_rank(sf), date(2026, 3, 18))
            self.assertEqual(pd_, date(2026, 3, 18))
            self.assertIn(m.LIVE_RULE.name, label)
            cur.execute("SELECT DISTINCT source, release_date::text FROM "
                        "public.zz_s21_live")
            self.assertEqual(cur.fetchall(), [(label, P)])
            # Hartlepool: neighbour 3 is Welsh, so publisher 6 is live 5
            self.assertEqual(live(cur, "E06000001")[("E06000001",
                                                     "E06000008")], 5)
            self.assertNotIn(("E06000001", "W06000022"), live(cur))
            # Barnsley's new code resolved, as subject and as neighbour
            self.assertEqual(count(cur, "zz_s21_editions",
                                   "WHERE lad24cd = 'E08000016'"), 20)
            self.assertEqual(count(cur, "zz_s21_live",
                                   "WHERE lad24cd = 'E08000016'"), 5)
            self.assertEqual(count(cur, "zz_s21_editions",
                                   "WHERE lad24cd = 'E06000001' AND "
                                   "statistical_neighbour_lad24cd = "
                                   "'E08000016' AND neighbour_code_published "
                                   "= 'E08000038'"), 1)
            self.assertEqual(count(cur, "zz_s21_editions",
                                   "WHERE lad24cd = 'E08000038' OR "
                                   "statistical_neighbour_lad24cd IN "
                                   "('E08000038', 'E08000039')"), 0)
            # the Welsh neighbour verbatim, england_rank NULL with a reason
            self.assertEqual(count(cur, "zz_s21_editions",
                                   "WHERE statistical_neighbour_lad24cd = "
                                   "'W06000022' AND england_rank IS NULL "
                                   "AND null_reasons LIKE "
                                   "'%not_an_english_authority%'"), 1)
            logged.assert_called_once()
            self.assertIn(f"{P} new", logged.call_args[0][2])
            self.assertTrue(m.status(cur, m.profile())["ok"])

    def test_a_failing_ledger_insert_rolls_back_and_logs_a_partial_run(self):
        with rolled_back(self.conn) as cur:
            self.file()
            with mock.patch.object(pe, "record_file_check",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P}: FAILED", text)
            self.assertEqual(editions(cur), [])
            self.assertEqual(count(cur, "zz_s21_live"), 0)
            self.assertEqual(ledger(cur), [])
            logged.assert_called_once()
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN", notes)
            self.assertIn(f"failed ['{P}']", notes)

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            self.file()
            text, logged = self.ok(cur, ["load"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            for t in ("zz_s21_editions", "zz_s21_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--simulate"])
            self.assertIn("SIMULATION", text)
            for t in ("zz_s21_editions", "zz_s21_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            self.ok(cur, ["load", "--commit"])
            calls = []
            real = m.read_file
            with mock.patch.object(m, "read_file",
                                   side_effect=lambda p: calls.append(p)
                                   or real(p)):
                text, logged = self.ok(cur, ["load", "--commit"])
            self.assertEqual(calls, [])
            self.assertIn("nothing parsed", text)
            logged.assert_not_called()
            self.assertEqual(editions(cur), [(1, None, N_ED)])
            self.assertEqual(len(ledger(cur)), 1)
            text, _ = self.ok(cur, ["load", "--recheck", P, "--commit"])
            self.assertIn(f"{P}: unchanged", text)
            self.assertEqual(editions(cur), [(1, None, N_ED)])

    def test_a_second_edition_is_stored_as_editions_only(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live_md5(cur)
            self.file("4 March 2027", overrides=CHANGED)
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn("NEWER EDITION", text)
            self.assertIn(f"{P27}: held-not-live", text)
            self.assertIn("stored edition 1 only", text)
            self.assertEqual(editions(cur, P27), [(1, None, N_ED)])
            self.assertEqual(live_md5(cur), before)
            cur.execute("SELECT DISTINCT release_date::text FROM "
                        "public.zz_s21_live")
            self.assertEqual(cur.fetchall(), [(P,)])
            self.assertEqual([(p, o) for p, _, o, _ in ledger(cur)],
                             [(P, "new"), (P27, "held-not-live")])
            self.assertIn(f"{P27} held-not-live", logged.call_args[0][2])
            st = m.status(cur, m.profile())
            self.assertTrue(st["ok"], st)
            self.assertEqual(st["held_not_live"], [P27])
            text, _ = self.ok(cur, ["status"])
            self.assertIn(f"held, not live: {P27}", text)
            # neither load nor refresh-latest puts it in live
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)
            self.ok(cur, ["refresh-latest", "--commit"])
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit",
                                              "--acknowledge", P27])
            self.assertEqual(rc, "halt", text)
            self.assertIn(P27, text)
            text, _ = self.ok(cur, ["load", "--recheck", P27, "--commit"])
            self.assertIn(f"{P27}: unchanged", text)
            self.assertEqual(live_md5(cur), before)
            self.assertEqual(editions(cur, P27), [(1, None, N_ED)])

    def test_a_reissue_of_the_live_edition_waits_for_refresh(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live_md5(cur)
            self.file(overrides=CHANGED)
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P}: REJECTED", text)
            self.assertIn("two files claim the same release", text)
            self.assertIn("--acknowledge", text)
            self.assertEqual(editions(cur), [(1, None, N_ED)])
            self.assertEqual(len(ledger(cur)), 1)
            self.assertIn("PARTIAL RUN (exit 1)", logged.call_args[0][2])
            rc, text, _ = self.run_main(cur, ["load", "--commit",
                                              "--accept-reissue", P])
            self.assertEqual(rc, 1, text)                # still needs ack
            text, logged = self.ok(cur, ["load", "--commit",
                                         "--accept-reissue", P,
                                         "--acknowledge", P])
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertIn("ACKNOWLEDGED", text)
            self.assertIn(f"{P}: revised", text)
            self.assertEqual(editions(cur), [(1, None, N_ED), (2, 1, N_ED)])
            self.assertIn("--accept-reissue", logged.call_args[0][2])
            self.assertIn("ACKNOWLEDGED", logged.call_args[0][2])
            self.assertEqual(live_md5(cur), before)        # live waits
            self.assertEqual(m.status(cur, m.profile())["pending_refresh"],
                             [P])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("E09000033", text)
            self.assertIn("--acknowledge", text)
            self.assertEqual(live_md5(cur), before)
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--acknowledge", text)
            self.assertEqual(live_md5(cur), before)
            text, logged = self.ok(cur, ["refresh-latest", "--commit",
                                         "--acknowledge", P])
            self.assertIn("COMMITTED", text)
            logged.assert_called_once()
            self.assertIn("ACKNOWLEDGED", logged.call_args[0][2])
            self.assertEqual(live(cur, "E09000033")[("E09000033",
                                                     "E07000008")], 2)
            self.assertEqual(m.live_equals_tip(cur, ZZ, [P]), [])
            cur.execute("SELECT DISTINCT release_label, loaded_at FROM "
                        "public.zz_s21_editions WHERE edition = 2")
            label, loaded = cur.fetchone()
            cur.execute("SELECT DISTINCT source, loaded_at FROM "
                        "public.zz_s21_live")
            self.assertEqual(cur.fetchall(), [(label, loaded)])
            self.assertTrue(m.status(cur, m.profile())["ok"])

    def test_a_changed_live_rule_is_one_refresh_not_a_reload(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            alt = m.LiveRule(name="the publisher's top five, English only, "
                             "ranks as published",
                             english_prefixes=("E06", "E07", "E08", "E09"),
                             within_publisher_rank=5, take=5,
                             renumber=False)
            with mock.patch.object(m, "LIVE_RULE", alt):
                st = m.status(cur, m.profile())
                self.assertEqual(st["pending_refresh"], [P], st)
                rc, text, _ = self.run_main(cur, ["refresh-latest",
                                                  "--commit"])
                self.assertEqual(rc, "halt", text)
                self.ok(cur, ["refresh-latest", "--commit", "--acknowledge",
                              P])
                self.assertEqual(sorted(live(cur, "E06000001").values()),
                                 [1, 2, 4, 5])
                self.assertTrue(m.status(cur, m.profile())["ok"])
            self.assertEqual(editions(cur), [(1, None, N_ED)])

    def older(self, cur, how):
        """Seed from the 18 March 2026 file, then offer a file published
        12 March 2025 by the page, --file or --file --no-page."""
        self.seed(cur)
        if how == "page":
            self.api[m.LANDING] = landing()
        path = self.file("12 March 2025", register=how == "page",
                         overrides=CHANGED)
        argv = ["load", "--commit"]
        if how != "page":
            argv += ["--file", path]
        if how == "file-no-page":
            argv += ["--no-page"]
        return argv

    def test_an_older_file_halts_on_every_path(self):
        for how in ("page", "file", "file-no-page"):
            with self.subTest(how=how), rolled_back(self.conn) as cur:
                if how == "file":
                    # its own edition must be on the landing page
                    self.api = {m.LANDING: landing()}
                argv = self.older(cur, how)
                if how == "file":
                    self.edition("2025", "x.xlsx",
                                 "2025-03-12T00:00:00.000Z")
                rc, text, logged = self.run_main(cur, argv)
                self.assertEqual(rc, "halt", text)
                self.assertIn("older", text)
                self.assertIn("--allow-older-file", text)
                self.assertEqual(editions(cur), [(1, None, N_ED)])
                self.assertEqual(editions(cur, P25), [])
                self.assertEqual(len(ledger(cur)), 1)
                logged.assert_not_called()

    def test_allow_older_file_stores_editions_only_and_is_logged(self):
        with rolled_back(self.conn) as cur:
            before = None
            argv = self.older(cur, "file-no-page") + ["--allow-older-file"]
            before = live_md5(cur)
            text, logged = self.ok(cur, argv)
            self.assertEqual(editions(cur, P25), [(1, None, N_ED)])
            self.assertEqual(live_md5(cur), before)
            self.assertIn("--allow-older-file given", logged.call_args[0][2])

    def test_rejected_stores_nothing_and_logs_a_partial_run(self):
        with rolled_back(self.conn) as cur:
            self.file(subjects=ENGLISH[:4] + ["W06000022"])
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P}: REJECTED", text)
            self.assertIn(m.PARTIAL, text)
            for t in ("zz_s21_editions", "zz_s21_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_called_once()
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN (exit 1)", notes)
            self.assertIn(f"rejected ['{P}'", notes)
            rc, text, logged = self.run_main(cur, ["load"])
            self.assertEqual(rc, 1, text)
            logged.assert_not_called()

    def test_another_release_never_enters_an_empty_live_table(self):
        with rolled_back(self.conn) as cur:
            self.file("4 March 2027")
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P27}: REJECTED", text)
            self.assertEqual(count(cur, "zz_s21_live"), 0)
            self.assertEqual(count(cur, "zz_s21_editions"), 0)

    def test_an_acceptance_must_name_a_compared_period(self):
        with rolled_back(self.conn) as cur:
            self.file()
            for flag in ("--accept-reissue", "--acknowledge"):
                rc, text, logged = self.run_main(cur, ["load", "--commit",
                                                       flag, "2019-01-01"])
                self.assertEqual(rc, "halt", text)
                self.assertIn("2019-01-01", text)
                self.assertEqual(count(cur, "zz_s21_editions"), 0)
                logged.assert_not_called()

    def test_geography_old_codes_and_unexplained_codes_halt(self):
        with rolled_back(self.conn) as cur:
            self.file(overrides={"E06000001": {4: "E08000016"}})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("E08000016", text)
            self.assertIn("'new'", text)
            self.file(overrides={"E06000001": {4: "E06000099"}})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("UNEXPLAINED E06000099", text)
            self.assertEqual(count(cur, "zz_s21_editions"), 0)
            logged.assert_not_called()

    def test_a_blank_pair_or_marker_halts_before_anything_is_stored(self):
        with rolled_back(self.conn) as cur:
            self.file(cells={("E06000001", 8): "[x]"})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("note 12", text)
            self.file(cells={("E06000001", 8): None, ("E06000001", 9): None})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("blank", text)
            self.assertEqual(count(cur, "zz_s21_editions"), 0)
            logged.assert_not_called()

    def test_a_stranded_release_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("DELETE FROM public.zz_s21_live")
            self.assertEqual(m.status(cur, m.profile())["live_missing"], [P])
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn("live-missing", text)
            self.assertEqual(count(cur, "zz_s21_live"), N_LIVE)
            self.assertEqual(editions(cur), [(1, None, N_ED)])
            self.assertTrue(m.status(cur, m.profile())["ok"])

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.file()
            final = "https://www.ons.gov.uk/file?uri=/moved/2026/x.xlsx"
            self.final[self.last_url] = final
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn(final, text)
            (_, src, _, _), = ledger(cur)
            self.assertTrue(src.startswith(final + " (release"))

    def test_same_name_download_with_different_content_is_kept_beside(self):
        with rolled_back(self.conn) as cur:
            held = self.file(register=False, name=FILE)
            new = self.file(register=False, name=FILE, overrides=CHANGED)
            raw = self.root / "raw"
            raw.mkdir()
            (raw / FILE).write_bytes(held.read_bytes())
            url = self.edition("2026", FILE, "2026-03-18T00:00:00.000Z")
            borrowed = _Borrowed(cur)
            out = io.StringIO()
            with specs(self.root), \
                    mock.patch.object(m, "_conn", return_value=borrowed), \
                    mock.patch.object(m, "fetch_json", side_effect=lambda p,
                                      s=None: self.api[p]), \
                    mock.patch.object(m, "RAW_DIR", raw), \
                    mock.patch.object(m, "MIN_FILE_BYTES", 10), \
                    mock.patch.object(m, "_session", return_value=_Session(
                        new.read_bytes(), url)), \
                    mock.patch.object(m, "log_run"), \
                    contextlib.redirect_stdout(out):
                try:
                    rc = m.main(["load"])
                finally:
                    borrowed.tx.done()
            text = out.getvalue()
            self.assertEqual(rc, 0, text)
            self.assertEqual((raw / FILE).read_bytes(), held.read_bytes())
            beside = [p for p in raw.iterdir() if p.name != FILE]
            self.assertEqual(len(beside), 1)
            self.assertEqual(beside[0].read_bytes(), new.read_bytes())
            self.assertIn("kept it untouched", text)

    def test_file_paths_and_no_page(self):
        with rolled_back(self.conn) as cur:
            path = self.file(register=False)
            rc, text, _ = self.run_main(cur, ["load", "--file", path])
            self.assertEqual(rc, "halt", text)            # no edition 2026
            self.assertIn("--no-page", text)
            text, logged = self.ok(cur, ["load", "--file", path,
                                         "--no-page", "--commit"])
            self.assertIn("MANUAL INPUT", text)
            self.assertIn(f"{P}: new", text)
            self.assertIn("--no-page", logged.call_args[0][2])
            rc, text, _ = self.run_main(cur, ["load", "--no-page"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--no-page", text)
            other = tempfile.TemporaryDirectory()
            self.addCleanup(other.cleanup)
            outside = write_s21(Path(other.name) / "o.xlsx")
            rc, text, _ = self.run_main(cur, ["load", "--file", outside,
                                              "--no-page"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("not under an allowed root", text)

    def test_identity_halts(self):
        with rolled_back(self.conn) as cur:
            self.file(release="2026-03-11T00:00:00.000Z")
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2026-03-11", text)
            self.api = {m.LANDING: landing()}
            self.file(edition="2027")
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2027", text)
            self.assertEqual(count(cur, "zz_s21_editions"), 0)

    def test_discovery_fails_loudly(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["load"])
            self.assertIn("no newer edition", text)
            odd = m.LANDING + "/2027provisional"
            self.api[m.LANDING]["datasets"].append({"uri": odd})
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn(odd, text)
            self.assertNotIn("nothing to do", text)
            self.api[m.LANDING]["datasets"].pop()
            self.api[m.LANDING]["description"]["title"] = "Clusters"
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Clusters", text)
            self.api[m.LANDING] = landing()
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("no edition", text)

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live(cur)
            self.file(overrides=CHANGED)
            self.ok(cur, ["load", "--commit", "--accept-reissue", P,
                          "--acknowledge", P])
            self.ok(cur, ["refresh-latest", "--commit", "--acknowledge", P])
            self.assertNotEqual(live(cur), before)
            text, logged = self.ok(cur, ["restore-edition", P, "1",
                                         "--commit"])
            self.assertIn("as edition 3", text)
            self.assertEqual(editions(cur)[-1], (3, 2, N_ED))
            logged.assert_called_once()
            self.ok(cur, ["refresh-latest", "--commit", "--acknowledge", P])
            self.assertEqual(live(cur), before)
            rc, text, _ = self.run_main(cur, ["restore-edition", P, "3"])
            self.assertEqual(rc, "halt")
            self.assertIn("is the tip", text)


class Migrate(Fixture):

    def seed_legacy(self, cur, **kw):
        """Held-style live rows (no release_date column yet): LIVE_RULE's
        rows of a fixture file, one loaded_at and one source."""
        path = self.file(register=False, name=FILE, **kw)
        recs = m.build_records(m.read_file(path), resolve_all, {})
        for r in m.live_rows(recs):
            cur.execute("INSERT INTO public.zz_s21_live (lad24cd, "
                        "statistical_neighbour_lad24cd, neighbour_rank, "
                        "source, loaded_at) VALUES (%s, %s, %s, %s, %s)",
                        (r["lad24cd"], r["statistical_neighbour_lad24cd"],
                         r["neighbour_rank"], HELD_SOURCE, HELD_LOADED))
        url = m.download_url(EDITION_URI.format("2026"), FILE)
        files = {"name": FILE, "sha256": m.content_sha256(path), "url": url,
                 "loaded_by": "test build"}
        return path, files

    def legacy(self, cur):
        return m.live_state(cur)[:2]

    def migrate(self, cur, path, files, legacy):
        with quiet() as out:
            plan = m.migrate_legacy(cur, path, write=True, legacy=legacy,
                                    files=files)
        return plan, out.getvalue()

    def test_preview_before_ddl_reads_live_without_a_release_date(self):
        with rolled_back(self.conn, ddl=False) as cur:
            path, files = self.seed_legacy(cur)
            with quiet() as out:
                plan = m.migrate_legacy(cur, path, write=False,
                                        legacy=self.legacy(cur), files=files)
            self.assertIn("0 differences", out.getvalue())
            self.assertEqual(plan["rows"], N_ED)
            self.assertEqual(plan["live"], N_LIVE)
            self.assertFalse(pe.table_exists(cur, "zz_s21_editions"))
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, path, write=True,
                                 legacy=self.legacy(cur), files=files)
            self.assertIn("ddl", str(e.exception.code))

    def test_load_preview_before_ddl_compares_with_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            path, _ = self.seed_legacy(cur)
            text, _ = self.ok(cur, ["load", "--file", path, "--no-page"])
            self.assertIn("compares the file with the LIVE table", text)
            self.assertIn(f"{P}: unchanged", text)
            rc, text, _ = self.run_main(cur, ["load", "--file", path,
                                              "--no-page", "--commit"])
            self.assertEqual(rc, "halt")
            self.assertIn("ddl --commit", text)

    def test_proof_passes_and_byte_identical_rereads_are_unchanged(self):
        with rolled_back(self.conn, ddl=False) as cur:
            path, files = self.seed_legacy(cur)
            legacy = self.legacy(cur)
            source_before = live_md5(cur)
            m.create_all(cur)
            plan, text = self.migrate(cur, path, files, legacy)
            self.assertIn("0 differences", text)
            self.assertEqual(editions(cur), [(1, None, N_ED)])
            cur.execute("SELECT DISTINCT source_file, release_label, "
                        "published_date FROM public.zz_s21_editions")
            (sf, label, pd_), = cur.fetchall()
            self.assertTrue(sf.startswith(f"as loaded: test build {FILE}; "
                                          f"release {P}; published {P}"), sf)
            self.assertEqual(label, core.AS_LOADED_LABEL)
            self.assertEqual(pd_, date(2026, 7, 24))
            self.assertEqual(ledger(cur), [(
                P, f"{files['url']} (release {P}; published {P})",
                "unchanged", 1)])
            self.assertTrue(m.status(cur, m.profile())["ok"])
            self.assertEqual(live_md5(cur), source_before)
            # the same bytes on every path: unchanged against edition 1
            self.urls[files["url"]] = path
            self.edition("2026", FILE, "2026-03-18T00:00:00.000Z")
            text, _ = self.ok(cur, ["load"])
            self.assertIn("nothing parsed", text)
            for argv in (["load", "--recheck", P],
                         ["load", "--recheck", P, "--commit"],
                         ["load", "--file", path],
                         ["load", "--file", path, "--no-page"],
                         ["load", "--file", path, "--commit"],
                         ["load", "--file", path, "--no-page", "--recheck",
                          P, "--commit"]):
                with self.subTest(argv=argv):
                    text, _ = self.ok(cur, argv)
                    self.assertIn(f"{P}: unchanged", text)
                    self.assertNotIn("REJECTED", text)
                    self.assertNotIn("two files claim", text)
            self.assertEqual(editions(cur), [(1, None, N_ED)])
            self.assertEqual([o for _, _, o, _ in ledger(cur)],
                             ["unchanged"] * 4)
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)
            self.assertEqual(live_md5(cur), source_before)

    def test_a_planted_held_difference_stops_and_rolls_back(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            cur.execute("UPDATE public.zz_s21_live SET neighbour_rank = 6 "
                        "WHERE lad24cd = 'E06000001' AND neighbour_rank = 5")
            with mock.patch.object(m, "LEGACY_LIVE", self.legacy(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                rc, text, logged = self.run_main(
                    cur, ["migrate-legacy", path, "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("proof failed", text)
            self.assertIn("E06000001", text)
            self.assertEqual(count(cur, "zz_s21_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)
            logged.assert_not_called()

    def test_the_command_previews_then_commits(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            with mock.patch.object(m, "LEGACY_LIVE", self.legacy(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                text, logged = self.ok(cur, ["migrate-legacy", path])
                self.assertIn("PREVIEW", text)
                self.assertIn("rank differs from the publisher", text)
                self.assertEqual(count(cur, "zz_s21_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, ["migrate-legacy", path,
                                             "--simulate"])
                self.assertIn("SIMULATION", text)
                self.assertEqual(count(cur, "zz_s21_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, ["migrate-legacy", path,
                                             "--commit"])
            self.assertEqual(editions(cur), [(1, None, N_ED)])
            logged.assert_called_once()
            self.assertEqual(count(cur, LEDGER), 1)

    def test_a_wrong_file_sha_or_live_state_stops(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            bad = dict(files, sha256="0" * 64)
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, path, write=True,
                                 legacy=self.legacy(cur), files=bad)
            self.assertIn("expected 00000000", str(e.exception.code))
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, path, write=True, legacy=(N_LIVE, "x"),
                                 files=files)
            self.assertIn("not as surveyed", str(e.exception.code))
            cur.execute("UPDATE public.zz_s21_live SET loaded_at = now() "
                        "WHERE lad24cd = 'E06000001'")
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, path, write=True,
                                 legacy=self.legacy(cur), files=files)
            self.assertIn("loaded_at", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s21_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)

    def test_a_non_empty_editions_table_stops(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            legacy = self.legacy(cur)
            self.migrate(cur, path, files, legacy)
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, path, write=True, legacy=legacy,
                                 files=files)
            self.assertIn("runs once", str(e.exception.code))


class Commands(Fixture):

    def test_status_without_editions_table_is_clean_and_creates_nothing(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1)
            self.assertIn("not exist yet", text)
            self.assertFalse(pe.table_exists(cur, "zz_s21_editions"))
            self.assertFalse(pe.table_exists(cur, LEDGER))

    def test_refresh_latest_without_tables_names_migrate_legacy(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["refresh-latest"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("migrate-legacy", text)

    def test_status_lines_after_a_load(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["status"])
            self.assertIn(f"published {P}", text)
            self.assertIn("ledger latest: new", text)
            self.assertIn(m.LIVE_RULE.name, text)

    def test_drift_in_live_is_reported_and_refresh_refuses_it(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("UPDATE public.zz_s21_live SET neighbour_rank = 9 "
                        "WHERE lad24cd = 'E06000001' AND neighbour_rank = 5")
            st = m.status(cur, m.profile())
            self.assertEqual(st["drift_periods"], [P])
            self.assertFalse(st["ok"])
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit",
                                              "--acknowledge", P])
            self.assertEqual(rc, "halt", text)
            self.assertIn("drift", text.lower())


if __name__ == "__main__":
    unittest.main()
