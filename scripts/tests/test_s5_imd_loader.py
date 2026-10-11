"""Tests for the S5 loader in s5_imd_editions (the commands, the live
vintage column, migrate-legacy, restore-edition) on throwaway tables.

No network: the content API replies and the downloads are stubbed, and every
.xlsx file is written by the test (write_file10 from test_s5_imd_pure).
Database tests run on a connection from _db.get_conn() inside a transaction
that is always rolled back (rolled_back below); nothing here commits. The
commands get a stand-in connection whose commit and rollback act on a
savepoint of that transaction, and a cursor proxy whose .connection does the
same, so the engine's per-period commit never reaches the real connection.
The only tables written are zz_s5_live (a LIKE copy of la_imd_2025, given
its vintage column by the ddl code under test), zz_s5_editions and its
ledger, created inside that transaction (refused if any already exists), so
they never persist. The run log is stubbed; the real la_imd_2025,
la_code_lookup, la_boundaries and information_schema are read (never
written).
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
import s5_imd_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s5_imd_pure import (ATT, BASE, CODES, HEADER_2030,  # noqa: E402
                              MODIFIED, TITLE, _Session, collection,
                              release_page, write_file10)

ZZ = dataclasses.replace(m.SPEC, name="zz_s5", live_table="zz_s5_live",
                         editions_table="zz_s5_editions")
LEDGER = "zz_s5_editions_file_checks"
N = len(CODES)
P = "IoD2025"
P30 = "IoD2030"
UTC = timezone.utc
LIVE_COLS = ("lad24cd", "la_name") + m.VALUES + ("source", "loaded_at")


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    SP = "zz_s5_cmd"

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
def specs(headers=None):
    with mock.patch.object(m, "SPEC", ZZ), \
            mock.patch.object(m, "EXPECTED_AREAS", N), \
            mock.patch.dict(m.FILE10_HEADERS, headers or {}):
        yield


@contextmanager
def rolled_back(conn, *, ddl=True, real_rows=False):
    cur = conn.cursor()
    try:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('zz_s5_live', 'zz_s5_editions', %s)", (LEDGER,))
        if cur.fetchall():
            raise RuntimeError("a zz_s5 table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s5_live (LIKE "
                    "public.la_imd_2025 INCLUDING ALL)")
        # the real la_imd_2025 gained its vintage column on 2026-10-11; the
        # copy starts from the held shape, before the ddl under test adds it
        cur.execute("ALTER TABLE public.zz_s5_live DROP COLUMN IF EXISTS "
                    "vintage")
        if real_rows:
            cols = ", ".join(LIVE_COLS)
            cur.execute(f"INSERT INTO public.zz_s5_live ({cols}) "
                        f"SELECT {cols} FROM public.la_imd_2025")
        with specs({P30: tuple(HEADER_2030)}):
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
                "public.zz_s5_editions WHERE vintage = %s "
                "GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur):
    cur.execute(f"SELECT vintage, source_file, outcome, edition "
                f"FROM public.{LEDGER} ORDER BY id")
    return cur.fetchall()


def live_val(cur, la, col="imd_rank_of_average_rank"):
    cur.execute(f"SELECT {col} FROM public.zz_s5_live WHERE lad24cd = %s",
                (la,))
    r = cur.fetchone()
    return r[0] if r else None


def other_cols_md5(cur, table="zz_s5_live"):
    expr = "||'|'||".join(f"coalesce({c}::text,'~')" for c in LIVE_COLS)
    cur.execute(f"SELECT COUNT(*), md5(string_agg({expr}, E'\\n' ORDER BY "
                f"lad24cd)) FROM public.{table}")
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
        self.api = {m.COLLECTION_PATH: collection()}
        self.urls = {}
        self.final = {}
        self.fetched = []
        self.n = 0

    def page(self, year, url):
        docs = self.api[m.COLLECTION_PATH]["links"]["documents"]
        if not any(d["title"] == TITLE.format(year) for d in docs):
            docs.append({"title": TITLE.format(year),
                         "base_path": BASE.format(year)})
        self.api[BASE.format(year)] = release_page(year, [
            (TITLE.format(year) + ": statistical release",
             BASE.format(year) + "/release"), (ATT, url)])

    def file(self, vintage=P, modified=MODIFIED, *, register=True, url=None,
             **kw):
        """Write a File 10; register its release page and download."""
        self.n += 1
        year = int(vintage[3:])
        name = (f"File_10_-_{vintage}_Local_Authority_District_Summaries__"
                "lower-tier__v2.xlsx")
        path = write_file10(self.root / f"t{self.n}" / name, vintage=vintage,
                            modified=modified, **kw)
        if register:
            url = url or f"https://assets.example/media/m{self.n}/{name}"
            self.urls[url] = path
            self.page(year, url)
            self.last_url = url
        return path

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, log)."""
        borrowed = _Borrowed(cur)

        def fetch(url, dest, session=None):
            self.fetched.append(url)
            return self.urls[url], self.final.get(url, url)

        out = io.StringIO()
        with specs({P30: tuple(HEADER_2030)}), \
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


class LiveColumn(Fixture):

    def test_the_vintage_alter_on_a_copy_is_additive(self):
        with rolled_back(self.conn, ddl=False, real_rows=True) as cur:
            before = other_cols_md5(cur)
            self.assertEqual(before[0], 296)
            m.create_all(cur)
            self.assertEqual(other_cols_md5(cur), before)
            self.assertEqual(count(cur, "zz_s5_live",
                                   "WHERE vintage = 'IoD2025'"), 296)
            cur.execute("SELECT data_type, is_nullable, column_default FROM "
                        "information_schema.columns WHERE table_schema = "
                        "'public' AND table_name = 'zz_s5_live' AND "
                        "column_name = 'vintage'")
            self.assertEqual(cur.fetchone(), ("text", "NO",
                                              "'IoD2025'::text"))
            # the CHECK makes a second vintage in live impossible
            cur.execute("SAVEPOINT zz_s5_chk")
            with self.assertRaises(Exception) as e:
                cur.execute("UPDATE public.zz_s5_live SET vintage = "
                            "'IoD2030' WHERE lad24cd = 'E06000001'")
            self.assertIn("check", str(e.exception).lower())
            cur.execute("ROLLBACK TO SAVEPOINT zz_s5_chk")
            # idempotent
            m.create_all(cur)
            self.assertEqual(other_cols_md5(cur), before)

    def test_a_vintage_column_of_another_shape_halts(self):
        with rolled_back(self.conn, ddl=False) as cur:
            cur.execute("ALTER TABLE public.zz_s5_live ADD COLUMN vintage "
                        "text")
            with self.assertRaises(SystemExit) as e:
                m.create_all(cur)
            self.assertIn("vintage", str(e.exception.code))

    def test_spec_types_match_the_live_table(self):
        with rolled_back(self.conn, ddl=False) as cur:
            cur.execute("SELECT column_name, data_type, numeric_precision, "
                        "numeric_scale FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = "
                        "'la_imd_2025'")
            live = {r[0]: r[1:] for r in cur.fetchall()}
            for col, sql in m.SPEC.value_cols:
                want = live[col]
                if sql.startswith("integer"):
                    self.assertEqual(want[0], "integer", col)
                else:
                    p, s = m.SCALES_SQL[col]
                    self.assertEqual(want, ("numeric", p, s), col)
            self.assertEqual(live["la_name"][0], "text")
            self.assertEqual(live["source"][0], "text")
            # the real DDL ran on 2026-10-11 (the loader's own definition
            # is checked on the copy in the tests above)
            self.assertEqual(live["vintage"][0], "text")

    def test_ddl_preview_shows_the_alter_then_commit_then_status(self):
        with rolled_back(self.conn, ddl=False) as cur:
            text, _ = self.ok(cur, ["ddl"])
            self.assertIn(m.live_alter_sql(ZZ), text)
            self.assertIn("DRY RUN", text)
            self.assertFalse(pe.table_exists(cur, "zz_s5_editions"))
            cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_name = 'zz_s5_live' AND column_name = "
                        "'vintage'")
            self.assertEqual(cur.fetchone()[0], 0)
            self.ok(cur, ["ddl", "--commit"])
            for t in ("zz_s5_editions", LEDGER):
                self.assertTrue(pe.table_exists(cur, t), t)
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 0, text)
            text, _ = self.ok(cur, ["ddl"])
            self.assertIn("vintage column present", text)


class Load(Fixture):

    def test_first_vintage_into_an_empty_table_inserts_live(self):
        with rolled_back(self.conn) as cur:
            self.file()
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P}: new", text)
            self.assertIn("checks passed", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s5_live"), N)
            self.assertEqual(core.rows_differing(cur, ZZ, P, 1), 0)
            (v, src, outcome, ed), = ledger(cur)
            self.assertEqual((v, outcome, ed), (P, "new", 1))
            self.assertEqual(src, f"{self.last_url} (vintage IoD2025; "
                                  "docProps modified 2025-11-14T12:48:06Z)")
            cur.execute("SELECT DISTINCT source_file, published_date, "
                        "release_label FROM public.zz_s5_editions")
            (sf, pd_, label), = cur.fetchall()
            self.assertEqual(m.release_rank(sf), MODIFIED)
            self.assertEqual(pd_, date(2025, 11, 14))
            self.assertIn("sha256", label)
            cur.execute("SELECT DISTINCT source, vintage FROM "
                        "public.zz_s5_live")
            self.assertEqual(cur.fetchall(), [(label, P)])
            # published zeros stay 0
            self.assertEqual(live_val(cur, "E08000019",
                                      "imd_proportion_most_deprived_10"), 0)
            self.assertEqual(live_val(cur, "E09000033", "imd_extent"), 0)
            self.assertEqual(live_val(cur, "E08000019",
                                      "imd_rank_of_proportion"), 4)
            logged.assert_called_once()
            self.assertIn(f"{P} new", logged.call_args[0][2])
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_failing_ledger_insert_rolls_back_and_logs_a_partial_run(self):
        with rolled_back(self.conn) as cur:
            self.file()
            with mock.patch.object(pe, "record_file_check",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P}: FAILED", text)
            self.assertEqual(editions(cur), [])
            self.assertEqual(count(cur, "zz_s5_live"), 0)
            self.assertEqual(ledger(cur), [])
            logged.assert_called_once()
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN", notes)
            self.assertIn(f"failed ['{P}']", notes)
            self.assertIn("not attempted none", notes)

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            self.file()
            text, logged = self.ok(cur, ["load"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            for t in ("zz_s5_editions", "zz_s5_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--simulate"])
            self.assertIn("SIMULATION", text)
            for t in ("zz_s5_editions", "zz_s5_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            self.ok(cur, ["load", "--commit"])
            calls = []
            real = m.read_file10
            with mock.patch.object(m, "read_file10",
                                   side_effect=lambda p: calls.append(p)
                                   or real(p)):
                text, logged = self.ok(cur, ["load", "--commit"])
            self.assertEqual(calls, [])
            self.assertIn("nothing parsed", text)
            logged.assert_not_called()
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 1)

    def test_a_second_vintage_is_stored_as_editions_only(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = other_cols_md5(cur)
            self.file(P30, modified=datetime(2030, 11, 1, 9, 0, 0,
                                             tzinfo=UTC),
                      overrides={("E06000001", "imd_average_rank"): 26000.5})
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P30}: held-not-live", text)
            self.assertIn("stored edition 1 only", text)
            self.assertEqual(editions(cur, P30), [(1, None, N)])
            self.assertEqual(other_cols_md5(cur), before)
            cur.execute("SELECT DISTINCT vintage FROM public.zz_s5_live")
            self.assertEqual(cur.fetchall(), [(P,)])
            self.assertEqual([(v, o) for v, _, o, _ in ledger(cur)],
                             [(P, "new"), (P30, "held-not-live")])
            self.assertIn(f"{P30} held-not-live", logged.call_args[0][2])
            st = pe.status(cur, m.profile())
            self.assertTrue(st["ok"], st)
            self.assertEqual(st["held_not_live"], [P30])
            text, _ = self.ok(cur, ["status"])
            self.assertIn(f"held, not live: {P30}", text)
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit",
                                              "--accept-drift", P30])
            self.assertEqual(rc, "halt", text)
            self.assertIn(P30, text)
            self.assertEqual(other_cols_md5(cur), before)
            # the same file again: ledger skip; --recheck compares it
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn("nothing parsed", text)
            text, _ = self.ok(cur, ["load", "--recheck", P30, "--commit"])
            self.assertIn(f"{P30}: unchanged", text)
            self.assertEqual(editions(cur, P30), [(1, None, N)])
            self.assertEqual(other_cols_md5(cur), before)

    def test_another_vintage_never_enters_an_empty_live_table(self):
        with rolled_back(self.conn) as cur:
            self.file(P30, modified=datetime(2030, 11, 1, tzinfo=UTC))
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P30}: REJECTED", text)
            self.assertEqual(count(cur, "zz_s5_live"), 0)
            self.assertEqual(count(cur, "zz_s5_editions"), 0)
            self.assertIn(f"rejected ['{P30}'", logged.call_args[0][2])

    def test_a_newer_reissue_is_the_next_edition_live_waits(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.file(modified=datetime(2025, 12, 1, 10, 0, 0, tzinfo=UTC),
                      overrides={("E06000002", "imd_average_score"): 35.6})
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P}: revised", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertEqual(str(live_val(cur, "E06000002",
                                          "imd_average_score")), "35.500")
            self.assertEqual(pe.status(cur, m.profile())["pending_refresh"],
                             [P])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(str(live_val(cur, "E06000002",
                                          "imd_average_score")), "35.600")
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s5_editions WHERE edition = 2")
            label = cur.fetchone()[0]
            cur.execute("SELECT DISTINCT source FROM public.zz_s5_live")
            self.assertEqual(cur.fetchall(), [(label,)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_ledger_skip_needs_the_pair_for_the_vintage(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            path = self.file(modified=datetime(2025, 12, 1, tzinfo=UTC),
                             overrides={("E06000002", "imd_average_score"):
                                        35.6})
            # the file's (URL, sha) recorded under the other vintage only
            cur.execute(f"INSERT INTO public.{LEDGER} (vintage, source_file, "
                        "file_sha256, outcome, edition) VALUES (%s, %s, %s, "
                        "'unchanged', 1)",
                        (P30, m.ledger_source(self.last_url, P,
                                              datetime(2025, 12, 1,
                                                       tzinfo=UTC)),
                         m.content_sha256(path)))
            text, _ = self.ok(cur, ["load"])
            self.assertNotIn("nothing parsed", text)
            self.assertIn(f"{P}: revised", text)

    def older(self, cur, how):
        """Seed from the file modified 2025-11-14, then offer a file
        modified 2025-10-28 with a different value by the page, --release
        or --file."""
        self.seed(cur)
        path = self.file(modified=datetime(2025, 10, 28, 9, 0, 0,
                                           tzinfo=UTC),
                         register=how != "file",
                         overrides={("E06000002", "imd_average_score"): 35.6})
        argv = ["load", "--commit"]
        if how == "release":
            argv += ["--release", "2025"]
        if how == "file":
            argv += ["--file", path]
        return argv

    def test_an_older_file_halts_on_every_path(self):
        for how in ("page", "release", "file"):
            with self.subTest(how=how), rolled_back(self.conn) as cur:
                rc, text, logged = self.run_main(cur, self.older(cur, how))
                self.assertEqual(rc, "halt", text)
                self.assertIn(f"{P} (older", text)
                self.assertIn("older file", text)
                self.assertEqual(editions(cur), [(1, None, N)])
                self.assertEqual(len(ledger(cur)), 1)
                logged.assert_not_called()

    def test_allow_older_file_overrides_and_is_logged(self):
        with rolled_back(self.conn) as cur:
            argv = self.older(cur, "file") + ["--allow-older-file"]
            text, logged = self.ok(cur, argv)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertIn("--allow-older-file given", logged.call_args[0][2])
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s5_editions WHERE edition = 2")
            self.assertIn("--allow-older-file given", cur.fetchone()[0])

    def test_equal_docprops_with_different_content_stops_unless_accepted(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.file(overrides={("E06000002", "imd_average_score"): 35.6})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P}: REJECTED", text)
            self.assertIn("two files claim the same release", text)
            self.assertIn("--accept-reissue", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 1)
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN (exit 1)", notes)
            self.assertIn(f"rejected ['{P}'", notes)
            # preview of the same: exit 1, nothing logged
            rc, text, logged = self.run_main(cur, ["load"])
            self.assertEqual(rc, 1, text)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--commit",
                                         "--accept-reissue", P])
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertIn("--accept-reissue", logged.call_args[0][2])

    def test_an_acceptance_must_name_a_compared_period(self):
        with rolled_back(self.conn) as cur:
            self.file()
            rc, text, logged = self.run_main(cur, ["load", "--commit",
                                                   "--accept-reissue",
                                                   "IoD2019"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("IoD2019", text)
            self.assertEqual(count(cur, "zz_s5_editions"), 0)
            logged.assert_not_called()

    def test_a_file_missing_an_authority_halts(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.file(modified=datetime(2025, 12, 1, tzinfo=UTC),
                      codes=CODES[:-1])
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn(f"{N - 1} authorities", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            logged.assert_not_called()

    def test_a_stranded_vintage_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("DELETE FROM public.zz_s5_live")
            self.assertEqual(pe.status(cur, m.profile())["live_missing"], [P])
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn("live-missing", text)
            self.assertEqual(count(cur, "zz_s5_live"), N)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.file()
            final = "https://assets.example/media/new/F10.xlsx"
            self.final[self.last_url] = final
            self.ok(cur, ["load", "--commit"])
            (_, src, _, _), = ledger(cur)
            self.assertTrue(src.startswith(final + " (vintage IoD2025"))

    def test_same_name_download_with_different_content_is_kept_beside(self):
        with rolled_back(self.conn) as cur:
            held = self.file(register=False)
            new = self.file(modified=datetime(2025, 12, 1, tzinfo=UTC),
                            register=False)
            raw = self.root / "raw"
            raw.mkdir()
            (raw / held.name).write_bytes(held.read_bytes())
            url = f"https://assets.example/media/z/{held.name}"
            self.page(2025, url)
            borrowed = _Borrowed(cur)
            out = io.StringIO()
            with specs(), mock.patch.object(m, "_conn",
                                            return_value=borrowed), \
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
            self.assertEqual((raw / held.name).read_bytes(),
                             held.read_bytes())
            beside = [p for p in raw.iterdir() if p.name != held.name]
            self.assertEqual(len(beside), 1)
            self.assertEqual(beside[0].read_bytes(), new.read_bytes())
            self.assertIn("kept it untouched", text)
            self.assertIn("2025-12-01T00:00:00Z", text)

    def test_file_paths_and_no_page(self):
        with rolled_back(self.conn) as cur:
            path = self.file(register=False)
            rc, text, _ = self.run_main(cur, ["load", "--file", path])
            self.assertEqual(rc, "halt", text)            # no page found
            self.assertIn("--no-page", text)
            text, logged = self.ok(cur, ["load", "--file", path,
                                         "--no-page", "--commit"])
            self.assertIn(f"{P}: new", text)
            self.assertIn("--no-page", logged.call_args[0][2])
            rc, text, _ = self.run_main(cur, ["load", "--no-page"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--no-page", text)

    def test_identity_halts(self):
        with rolled_back(self.conn) as cur:
            self.file()
            self.api[BASE.format(2025)]["title"] = TITLE.format(2019)
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            path = self.file(register=False)
            rc, text, _ = self.run_main(cur, ["load", "--file", path,
                                              "--release", "2019"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--release 2019", text)
            # the page says 2030, the file is IoD2025
            self.urls["https://assets.example/media/q/F10.xlsx"] = path
            self.page(2030, "https://assets.example/media/q/F10.xlsx")
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("IoD2030", text)
            self.assertEqual(count(cur, "zz_s5_editions"), 0)

    def test_new_codes_halt_against_the_old_declaration(self):
        with rolled_back(self.conn) as cur:
            self.file(codes=["E06000001", "E06000002", "E08000038",
                             "E08000019", "E09000033"])
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("E08000038", text)
            self.assertIn("'old'", text)
            self.assertEqual(count(cur, "zz_s5_editions"), 0)
            logged.assert_not_called()

    def test_a_blank_cell_or_a_bad_rank_halts(self):
        with rolled_back(self.conn) as cur:
            self.file(overrides={("E06000002", "imd_extent"): None})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("blank", text)
            self.file(overrides={("E06000002", "imd_rank_of_average_rank"):
                                 1})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("imd_rank_of_average_rank", text)
            self.assertEqual(count(cur, "zz_s5_editions"), 0)
            logged.assert_not_called()

    def test_discovery_fails_loudly(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["load"])
            self.assertIn("no newer release", text)
            docs = self.api[m.COLLECTION_PATH]["links"]["documents"]
            docs.append({"title": "English Indices of Deprivation 2030",
                         "base_path": "/government/statistics/x"})
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("English Indices of Deprivation 2030", text)
            self.assertIn("2025", text)
            self.assertNotIn("nothing to do", text)
            docs.pop()
            self.api[BASE.format(2025)]["details"]["attachments"] = [
                {"title": "File 11: Local Authority District summaries - "
                          "upper tier", "url": "x.xlsx"}]
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("File 11", text)
            self.api[m.COLLECTION_PATH]["title"] = "Deprivation"
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Deprivation", text)

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.file(modified=datetime(2025, 12, 1, tzinfo=UTC),
                      overrides={("E06000002", "imd_average_score"): 35.6})
            self.ok(cur, ["load", "--commit"])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(str(live_val(cur, "E06000002",
                                          "imd_average_score")), "35.600")
            text, logged = self.ok(cur, ["restore-edition", P, "1",
                                         "--commit"])
            self.assertIn("as edition 3", text)
            self.assertEqual(editions(cur)[-1], (3, 2, N))
            logged.assert_called_once()
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(str(live_val(cur, "E06000002",
                                          "imd_average_score")), "35.500")
            rc, text, _ = self.run_main(cur, ["restore-edition", P, "3"])
            self.assertEqual(rc, "halt")
            self.assertIn("is the tip", text)


class Migrate(Fixture):

    URL = ("https://assets.example/media/h/File_10_-_IoD2025_Local_"
           "Authority_District_Summaries__lower-tier__v2.xlsx")

    def seed_legacy(self, cur, **kw):
        """Held-style live rows (no vintage column yet) built from a fixture
        file, one loaded_at and one source."""
        path = self.file(register=False, **kw)
        f = m.read_file10(path)
        recs = m.build_records(f, lambda c: c)
        for r in recs:
            cur.execute(
                "INSERT INTO public.zz_s5_live (lad24cd, la_name, "
                + ", ".join(m.VALUES) + ", source, loaded_at) VALUES ("
                + ", ".join(["%s"] * (len(m.VALUES) + 4)) + ")",
                [r["lad24cd"], r["la_name"]] + [r[c] for c in m.VALUES]
                + ["MHCLG English Indices of Deprivation 2025, File 10 (test)",
                   "2026-04-01 10:58:50.329755+00"])
        files = {"name": path.name, "sha256": m.content_sha256(path),
                 "url": self.URL}
        return path, files

    def legacy(self, cur):
        return m.live_state(cur)[:2]

    def migrate(self, cur, path, files, **kw):
        with quiet() as out:
            plan = m.migrate_legacy(cur, path, write=True,
                                    legacy=kw.get("legacy") or self.legacy(cur),
                                    files=files)
        return plan, out.getvalue()

    def test_preview_before_ddl_reads_live_without_a_vintage_column(self):
        with rolled_back(self.conn, ddl=False) as cur:
            path, files = self.seed_legacy(cur)
            with quiet() as out:
                plan = m.migrate_legacy(cur, path, write=False,
                                        legacy=self.legacy(cur), files=files)
            self.assertIn("0 differences", out.getvalue())
            self.assertEqual(plan["cells"], N * 11)
            self.assertFalse(pe.table_exists(cur, "zz_s5_editions"))
            cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_name = 'zz_s5_live' AND column_name = "
                        "'vintage'")
            self.assertEqual(cur.fetchone()[0], 0)
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
            m.create_all(cur)
            plan, text = self.migrate(cur, path, files, legacy=legacy)
            self.assertIn("0 differences", text)
            self.assertEqual(plan["rows"], N)
            self.assertEqual(editions(cur), [(1, None, N)])
            cur.execute("SELECT DISTINCT source_file, release_label, "
                        "published_date FROM public.zz_s5_editions")
            (sf, label, pd_), = cur.fetchall()
            self.assertEqual(sf, f"as loaded: {path.name}; vintage IoD2025; "
                                 "docProps modified 2025-11-14T12:48:06Z")
            self.assertEqual(label, core.AS_LOADED_LABEL)
            self.assertEqual(pd_, date(2026, 4, 1))
            self.assertEqual(ledger(cur), [(
                P, f"{self.URL} (vintage IoD2025; docProps modified "
                   "2025-11-14T12:48:06Z)", "unchanged", 1)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            source_before = live_val(cur, "E06000001", "source")
            # the same bytes on every path: unchanged against edition 1
            self.urls[self.URL] = path
            self.page(2025, self.URL)
            text, _ = self.ok(cur, ["load"])
            self.assertIn("nothing parsed", text)
            for argv in (["load", "--recheck", P],
                         ["load", "--release", "2025", "--recheck", P],
                         ["load", "--file", path],
                         ["load", "--file", path, "--no-page"],
                         ["load", "--file", path, "--commit"],
                         ["load", "--file", path, "--no-page", "--recheck",
                          P, "--commit"],
                         ["load", "--recheck", P, "--commit"]):
                with self.subTest(argv=argv):
                    text, _ = self.ok(cur, argv)
                    self.assertIn(f"{P}: unchanged", text)
                    self.assertNotIn("REJECTED", text)
                    self.assertNotIn("two files claim", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual([o for _, _, o, _ in ledger(cur)],
                             ["unchanged"] * 4)
            self.assertEqual(live_val(cur, "E08000019",
                                      "imd_proportion_most_deprived_10"), 0)
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)
            self.assertEqual(live_val(cur, "E06000001", "source"),
                             source_before)

    def test_a_planted_held_difference_stops_and_rolls_back(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            cur.execute("UPDATE public.zz_s5_live SET la_name = 'Hartlepol' "
                        "WHERE lad24cd = 'E06000001'")
            with mock.patch.object(m, "LEGACY_LIVE", self.legacy(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                rc, text, logged = self.run_main(
                    cur, ["migrate-legacy", path, "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("proof failed", text)
            self.assertIn("E06000001", text)
            self.assertEqual(count(cur, "zz_s5_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)
            logged.assert_not_called()

    def test_the_command_previews_then_commits(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            with mock.patch.object(m, "LEGACY_LIVE", self.legacy(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                text, logged = self.ok(cur, ["migrate-legacy", path])
                self.assertIn("PREVIEW", text)
                self.assertEqual(count(cur, "zz_s5_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, ["migrate-legacy", path,
                                             "--simulate"])
                self.assertIn("SIMULATION", text)
                self.assertEqual(count(cur, "zz_s5_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, ["migrate-legacy", path,
                                             "--commit"])
            self.assertEqual(editions(cur), [(1, None, N)])
            logged.assert_called_once()

    def test_a_wrong_file_sha_or_live_state_stops(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            bad = dict(files, sha256="0" * 64)
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, path, write=True,
                                 legacy=self.legacy(cur), files=bad)
            self.assertIn("expected 00000000", str(e.exception.code))
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, path, write=True, legacy=(N, "x"),
                                 files=files)
            self.assertIn("not as surveyed", str(e.exception.code))
            cur.execute("UPDATE public.zz_s5_live SET loaded_at = now() "
                        "WHERE lad24cd = 'E06000001'")
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, path, write=True,
                                 legacy=self.legacy(cur), files=files)
            self.assertIn("loaded_at", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s5_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)

    def test_a_file_of_another_vintage_stops(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            other = self.file(P30, register=False)
            files = dict(files, sha256=m.content_sha256(other))
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, other, write=True,
                                 legacy=self.legacy(cur), files=files)
            self.assertIn(P30, str(e.exception.code))
            self.assertEqual(count(cur, "zz_s5_editions"), 0)

    def test_a_non_empty_editions_table_stops(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            legacy = self.legacy(cur)
            self.migrate(cur, path, files, legacy=legacy)
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
            self.assertFalse(pe.table_exists(cur, "zz_s5_editions"))
            self.assertFalse(pe.table_exists(cur, LEDGER))

    def test_refresh_latest_without_tables_names_migrate_legacy(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["refresh-latest"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("migrate-legacy", text)
            self.assertNotIn("sync-new --commit", text)

    def test_status_lines_after_a_load(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["status"])
            self.assertIn("docProps modified 2025-11-14T12:48:06Z", text)
            self.assertIn("ledger latest: new", text)


if __name__ == "__main__":
    unittest.main()
