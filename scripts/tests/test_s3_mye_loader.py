"""Tests for the S3 loader in s3_mye_editions (the commands, migrate-legacy,
restore-edition) on throwaway tables.

No network: the ONS replies and the downloads are stubbed, and every .xlsx
file is written by the test (write_file from test_s3_mye_pure). Database
tests run on a connection from _db.get_conn() inside a transaction that is
always rolled back (rolled_back below); nothing here commits. The commands
get a stand-in connection whose commit and rollback act on a savepoint of
that transaction, and a cursor proxy whose .connection does the same, so
the engine's per-period commit never reaches the real connection. The only
tables written are zz_s3_live (a LIKE copy of la_population), zz_s3_editions
and its ledger, created inside that transaction (refused if any already
exists), so they never persist. The run log is stubbed; the real
la_code_lookup and la_boundaries are read (never written). No retired
script is imported or run.
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
import s3_mye_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s3_mye_pure import (CODES, ED_URI, _Session, edition,  # noqa: E402
                              landing, value, write_file)

ZZ = dataclasses.replace(m.SPEC, name="zz_s3", live_table="zz_s3_live",
                         editions_table="zz_s3_editions")
LEDGER = "zz_s3_editions_file_checks"
N = len(CODES)
P = "2025"
PUB = date(2026, 7, 29)


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    """commit / rollback of a command, acting on a savepoint of the test's
    own transaction."""
    SP = "zz_s3_cmd"

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
    with mock.patch.object(m, "SPEC", ZZ), \
            mock.patch.object(m, "EXPECTED_AREAS", N):
        yield


@contextmanager
def rolled_back(conn, *, ddl=True):
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_s3_live'), "
                    "to_regclass('public.zz_s3_editions'), "
                    f"to_regclass('public.{LEDGER}')")
        if any(cur.fetchone()):
            raise RuntimeError("a zz_s3 table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s3_live (LIKE "
                    "public.la_population INCLUDING ALL)")
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
    """[(edition, supersedes, rows)]."""
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM "
                "public.zz_s3_editions WHERE reference_year = %s "
                "GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur):
    """[(reference_year, source_file, outcome, edition)] in order."""
    cur.execute(f"SELECT reference_year::text, source_file, outcome, edition "
                f"FROM public.{LEDGER} ORDER BY id")
    return cur.fetchall()


def live_val(cur, la, col="population", period=P):
    cur.execute(f"SELECT {col} FROM public.zz_s3_live WHERE lad24cd = %s "
                "AND reference_year = %s", (la, period))
    r = cur.fetchone()
    return r[0] if r else None


class Fixture(unittest.TestCase):
    """Files written by the test; the ONS replies and downloads stubbed."""

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

    def page(self, year, name):
        uri = ED_URI.format(year)
        ds = self.api[m.LANDING]["datasets"]
        if not any(d["uri"] == uri for d in ds):
            ds.append({"uri": uri})
        self.api[uri] = edition(year, files=[name])
        return m.download_url(uri, name)

    def file(self, year=2025, published=None, *, register=True, name=None,
             **kw):
        """Write a tables file; register its edition and download."""
        self.n += 1
        published = published or date(year + 1, 7, 29)
        name = name or f"mye{year % 100:02d}tablesew.xlsx"
        path = write_file(self.root / f"t{self.n}" / name, year,
                          published=published, **kw)
        if register:
            url = self.page(year, name)
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

    def seed(self, cur, **kw):
        self.file(**kw)
        self.ok(cur, ["load", "--commit"])
        self.fetched.clear()


class Load(Fixture):

    def test_new_period_edition_live_and_one_ledger_row(self):
        with rolled_back(self.conn) as cur:
            self.file()
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P}: new", text)
            self.assertIn("reconciled", text)
            self.assertIn("Mid-2025: 2023 local authority boundaries", text)
            self.assertIn(self.last_url, text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s3_live"), N)
            self.assertEqual(core.rows_differing(cur, ZZ, P, 1), 0)
            (_, src, outcome, ed), = ledger(cur)
            self.assertEqual((outcome, ed), ("new", 1))
            self.assertEqual(src, f"{self.last_url} (mid-2025; published "
                                  "2026-07-29; periods 2025)")
            cur.execute("SELECT DISTINCT source_file, published_date, "
                        "release_label FROM public.zz_s3_editions")
            (sf, pd_, label), = cur.fetchall()
            self.assertEqual(m.release_rank(sf), (PUB, 2025))
            self.assertEqual(pd_, PUB)
            self.assertIn("sha256", label)
            self.assertEqual(live_val(cur, "E08000016"),
                             value("E08000016", 2025))
            self.assertEqual(live_val(cur, "E06000002", "la_name"),
                             "Middlesbrough")
            logged.assert_called_once()
            self.assertIn(f"{P} new", logged.call_args[0][2])
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_failing_ledger_insert_rolls_back_everything(self):
        with rolled_back(self.conn) as cur:
            self.file()
            with mock.patch.object(pe, "record_file_check",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P}: FAILED", text)
            self.assertEqual(editions(cur), [])
            self.assertEqual(count(cur, "zz_s3_live"), 0)
            self.assertEqual(ledger(cur), [])
            logged.assert_called_once()
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN", notes)
            self.assertIn(f"failed ['{P}']", notes)
            self.assertIn("not attempted none", notes)

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            cur.execute("SELECT COUNT(*) FROM pipeline_run_log")
            runs = cur.fetchone()[0]
            self.file()
            text, logged = self.ok(cur, ["load"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            for t in ("zz_s3_editions", "zz_s3_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--simulate"])
            self.assertIn("SIMULATION", text)
            for t in ("zz_s3_editions", "zz_s3_live", LEDGER):
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
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 1)
            cur.execute("SELECT COUNT(*) FROM pipeline_run_log")
            self.assertEqual(cur.fetchone()[0], runs)

    def test_a_held_back_year_restated_equal_is_unchanged(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.file(2026)
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn("2025: unchanged", text)
            self.assertIn("2026: new", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(editions(cur, "2026"), [(1, None, N)])
            self.assertEqual([(p, o) for p, _, o, _ in ledger(cur)],
                             [("2025", "new"), ("2025", "unchanged"),
                              ("2026", "new")])
            self.assertEqual(count(cur, "zz_s3_live"), 2 * N)
            self.assertIn("2025 unchanged", logged.call_args[0][2])
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_restated_year_that_differs_needs_acknowledge(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live_val(cur, "E06000002")
            self.file(2026, values={("E06000002", 2025): before + 2})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2025: REJECTED", text)
            self.assertIn("--acknowledge 2025", text)
            self.assertIn("2026: new", text)        # the new year is stored
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(editions(cur, "2026"), [(1, None, N)])
            self.assertNotIn(("2025", "revised"),
                             [(p, o) for p, _, o, _ in ledger(cur)])
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN (exit 1)", notes)
            self.assertIn("rejected ['2025'", notes)
            # acknowledged: the next edition; live waits for refresh-latest
            text, logged = self.ok(cur, ["load", "--acknowledge", "2025",
                                         "--commit"])
            self.assertIn("2025: revised", text)
            self.assertIn("ACKNOWLEDGED", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertEqual(live_val(cur, "E06000002"), before)
            self.assertIn("ACKNOWLEDGED 2025", logged.call_args[0][2])
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s3_editions WHERE reference_year = 2025 "
                        "AND edition = 2")
            self.assertIn("ACKNOWLEDGED", cur.fetchone()[0])
            self.assertEqual(pe.status(cur, m.profile())["pending_refresh"],
                             ["2025"])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertEqual(live_val(cur, "E06000002"), before)
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "E06000002"), before + 2)
            self.assertEqual(live_val(cur, "E06000002", period="2026"),
                             value("E06000002", 2026))
            cur.execute("SELECT loaded_at FROM public.zz_s3_editions WHERE "
                        "reference_year = 2025 AND edition = 2 LIMIT 1")
            ed_loaded = cur.fetchone()[0]
            self.assertEqual(live_val(cur, "E06000002", "loaded_at"),
                             ed_loaded)
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_large_revision_needs_both_flags(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live_val(cur, "E06000002")
            self.file(2026, values={("E06000002", 2025): before * 2})
            rc, text, _ = self.run_main(cur, ["load", "--commit",
                                              "--acknowledge", "2025"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2025: REJECTED", text)
            self.assertIn(m.LARGE, text)
            self.assertIn("--accept-large-revision 2025", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            text, logged = self.ok(cur, ["load", "--commit", "--acknowledge",
                                         "2025", "--accept-large-revision",
                                         "2025"])
            self.assertIn("2025: revised", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertIn("--accept-large-revision", logged.call_args[0][2])

    def test_an_acknowledgement_must_name_a_compared_period(self):
        with rolled_back(self.conn) as cur:
            self.file()
            for flag in ("--acknowledge", "--accept-large-revision",
                         "--accept-reissue"):
                rc, text, logged = self.run_main(cur, ["load", "--commit",
                                                       flag, "2019"])
                self.assertEqual(rc, "halt", text)
                self.assertIn("2019", text)
                self.assertEqual(count(cur, "zz_s3_editions"), 0)
                logged.assert_not_called()

    def test_ledger_skip_needs_the_pair_for_every_period(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            path = self.file(2026)
            rank = (date(2027, 7, 29), 2026)
            # the file's (URL, sha) recorded for 2025 only, not for 2026
            cur.execute(f"INSERT INTO public.{LEDGER} (reference_year, "
                        "source_file, file_sha256, outcome, edition) VALUES "
                        "(2025, %s, %s, 'unchanged', 1)",
                        (m.ledger_source(self.last_url, rank, [2025, 2026]),
                         m.content_sha256(path)))
            text, _ = self.ok(cur, ["load"])
            self.assertNotIn("nothing parsed", text)
            self.assertIn("2026: new", text)
            self.assertIn("2025 (checked", text)

    def older(self, cur, how):
        """Seed from the file published 2026-07-29, then offer a mid-2025
        file published 2026-07-01 with a different value by the page,
        --release or --file."""
        self.seed(cur)
        path = self.file(published=date(2026, 7, 1),
                         register=how != "file",
                         values={("E06000002", 2025): 123456})
        argv = ["load", "--commit"]
        if how == "release":
            argv += ["--release", "2025"]
        if how == "file":
            argv += ["--file", path, "--no-page"]
        return argv

    def test_an_older_file_is_skipped_and_halts_on_every_path(self):
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
            argv = self.older(cur, "file") + ["--allow-older-file",
                                              "--acknowledge", P]
            text, logged = self.ok(cur, argv)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertIn("--allow-older-file given", logged.call_args[0][2])
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s3_editions WHERE edition = 2")
            self.assertIn("--allow-older-file given", cur.fetchone()[0])

    def test_equal_rank_with_different_content_stops_unless_accepted(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.file(values={("E06000002", 2025):
                              value("E06000002", 2025) + 7})
            rc, text, logged = self.run_main(cur, ["load", "--commit",
                                                   "--acknowledge", P])
            self.assertEqual(rc, 1, text)
            self.assertIn("two files claim the same release", text)
            self.assertIn("--accept-reissue", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 1)
            self.assertIn(f"rejected ['{P}'", logged.call_args[0][2])
            text, logged = self.ok(cur, ["load", "--commit", "--acknowledge",
                                         P, "--accept-reissue", P])
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertIn("--accept-reissue", logged.call_args[0][2])

    def test_a_new_period_breaking_a_stop_condition_is_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.file(2026, values={("E09000033", 2026): 900000})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2026: REJECTED", text)
            self.assertIn("E09000033", text)
            self.assertEqual(editions(cur, "2026"), [])
            self.assertEqual(count(cur, "zz_s3_live",
                                   "WHERE reference_year = 2026"), 0)
            self.assertNotIn("2026", [p for p, _, _, _ in ledger(cur)])
            logged.assert_called_once()
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN (exit 1)", notes)
            self.assertIn("rejected ['2026'", notes)
            self.assertIn("not attempted", notes)
            # preview of the same: exit 1, nothing logged
            rc, text, logged = self.run_main(cur, ["load"])
            self.assertEqual(rc, 1, text)
            logged.assert_not_called()
            # a movement threshold is released by --acknowledge
            text, _ = self.ok(cur, ["load", "--commit", "--acknowledge",
                                    "2026"])
            self.assertIn("2026: new", text)
            self.assertEqual(editions(cur, "2026"), [(1, None, N)])

    def test_a_partial_file_never_replaces_a_fuller_edition(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.file(2026, codes=CODES[:-1])
            rc, text, logged = self.run_main(cur, ["load", "--commit",
                                                   "--acknowledge", "2025",
                                                   "--acknowledge", "2026"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2025: REJECTED", text)
            self.assertIn("2026: REJECTED", text)
            self.assertIn(m.PARTIAL, text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(editions(cur, "2026"), [])
            self.assertEqual(len(ledger(cur)), 1)
            self.assertIn("rejected ['2025', '2026']",
                          logged.call_args[0][2])

    def test_a_stranded_period_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("DELETE FROM public.zz_s3_live")
            self.assertEqual(pe.status(cur, m.profile())["live_missing"], [P])
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn("live-missing", text)
            self.assertEqual(count(cur, "zz_s3_live"), N)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.file()
            final = "https://download.example/new/mye25tablesew.xlsx"
            self.final[self.last_url] = final
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn(final, text)
            (_, src, _, _), = ledger(cur)
            self.assertTrue(src.startswith(final + " (mid-2025"))
            cur.execute("SELECT DISTINCT source_file FROM "
                        "public.zz_s3_editions")
            self.assertIn(final, cur.fetchone()[0])

    def test_same_name_download_with_different_content_is_kept_beside(self):
        with rolled_back(self.conn) as cur:
            held = self.file(register=False)
            new = self.file(published=date(2026, 8, 5), register=False)
            raw = self.root / "raw"
            raw.mkdir()
            (raw / held.name).write_bytes(held.read_bytes())
            url = self.page(2025, held.name)
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
            self.assertIn("9.30am 5 August 2026", text)

    def test_file_paths_and_no_page(self):
        with rolled_back(self.conn) as cur:
            path = self.file(register=False)
            rc, text, _ = self.run_main(cur, ["load", "--file", path])
            self.assertEqual(rc, "halt", text)            # no edition found
            self.assertIn("--no-page", text)
            text, logged = self.ok(cur, ["load", "--file", path,
                                         "--no-page", "--commit"])
            self.assertIn(f"{P}: new", text)
            self.assertIn("--no-page", logged.call_args[0][2])
            rc, text, _ = self.run_main(cur, ["load", "--no-page"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--no-page", text)

    def test_identity_and_reconciliation_halt(self):
        with rolled_back(self.conn) as cur:
            self.file()
            self.api[ED_URI.format(2025)]["description"]["edition"] = (
                "Mid-2024: 2023 local authority boundaries")
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Mid-2024", text)
            total = sum(value(c, 2025) for c in CODES)
            self.file(england={("mye2", 2025): total + 1})
            rc, text, _ = self.run_main(cur, ["load", "--release", "2025"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("reconciliation", text)
            path = self.file(register=False)
            rc, text, _ = self.run_main(cur, ["load", "--file", path,
                                              "--release", "2024"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--release 2024", text)
            path = self.file(register=False, cover={"title_year": 2024})
            rc, text, _ = self.run_main(cur, ["load", "--file", path,
                                              "--no-page"])
            self.assertEqual(rc, "halt", text)
            self.assertEqual(count(cur, "zz_s3_editions"), 0)

    def test_new_codes_halt_against_the_old_declaration(self):
        with rolled_back(self.conn) as cur:
            self.file(codes=["E06000001", "E06000002", "E08000038",
                             "E08000019", "E09000033"])
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("E08000038", text)
            self.assertIn("'old'", text)
            self.assertEqual(count(cur, "zz_s3_editions"), 0)
            logged.assert_not_called()

    def test_a_blank_la_cell_halts_and_nothing_is_stored(self):
        with rolled_back(self.conn) as cur:
            self.file(mye2={"E06000002": None},
                      values={("E06000002", 2025): None})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("blank", text)
            self.assertIn("E06000002", text)
            self.assertEqual(count(cur, "zz_s3_editions"), 0)
            logged.assert_not_called()

    def test_discovery_fails_loudly(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["load"])
            self.assertIn("no newer edition", text)
            self.assertIn("mid-2025", text)
            self.api[m.LANDING]["datasets"].append(
                {"uri": m.LANDING + "/mid20262023localauthorityboundaires"})
            rc, text, logged = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("mid20262023localauthorityboundaires", text)
            self.assertNotIn("nothing to do", text)
            self.api[m.LANDING]["datasets"].pop()
            self.api[ED_URI.format(2025)]["downloads"] = []
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("download", text)
            self.api[m.LANDING]["description"]["title"] = "Population"
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Population", text)

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            first = live_val(cur, "E06000002")
            self.file(published=date(2026, 8, 5),
                      values={("E06000002", 2025): first + 3})
            self.ok(cur, ["load", "--commit", "--acknowledge", P])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "E06000002"), first + 3)
            text, logged = self.ok(cur, ["restore-edition", P, "1",
                                         "--commit"])
            self.assertIn("as edition 3", text)
            self.assertEqual(editions(cur)[-1], (3, 2, N))
            logged.assert_called_once()
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "E06000002"), first)
            rc, text, _ = self.run_main(cur, ["restore-edition", P, "3"])
            self.assertEqual(rc, "halt")
            self.assertIn("is the tip", text)


class Migrate(Fixture):

    def seed_legacy(self, cur, years=(2025,), names=None):
        """Held-style live rows built from fixture files (one per year, the
        year's own release), one loaded_at per year; names: {(year, code):
        held name} (a held name in another form)."""
        paths, files = [], {}
        loaded = {}
        for i, y in enumerate(years):
            name = f"mye{y % 100:02d}tablesew.xlsx"
            path = self.file(y, register=False, name=name)
            f = m.read_file(path)
            rmap, problems = m.resolve_codes(cur, set(f["mye2"]["las"]), y)
            self.assertEqual(problems, [])
            recs = m.build_records(f, y, rmap)
            for r in recs:
                r["la_name"] = (names or {}).get((y, r["lad24cd"]),
                                                 r["la_name"])
            pe.insert_live(cur, m.profile(), str(y), recs)
            stamp = f"2026-0{3 + 5 * i}-1{i} 09:40:44.303564+00"
            cur.execute("UPDATE public.zz_s3_live SET loaded_at = %s WHERE "
                        "reference_year = %s", (stamp, y))
            cur.execute("SELECT DISTINCT loaded_at FROM public.zz_s3_live "
                        "WHERE reference_year = %s", (y,))
            loaded[str(y)] = cur.fetchone()[0]
            url = m.download_url(ED_URI.format(y), name)
            files[str(y)] = {"name": name, "sha256": m.content_sha256(path),
                             "url": url, "loaded_by": f"fixture load {y}"}
            paths.append(path)
        return paths, files, loaded

    def legacy(self, cur, loaded):
        rows, h, _ = m.live_state(cur)
        return (rows, h, loaded)

    def migrate(self, cur, paths, files, loaded, **kw):
        with quiet() as out:
            plan = m.migrate_legacy(cur, paths, write=True,
                                    legacy=self.legacy(cur, loaded),
                                    legacy_files=files, **kw)
        return plan, out.getvalue()

    def test_proof_passes_and_byte_identical_rereads_are_unchanged(self):
        with rolled_back(self.conn) as cur:
            (path,), files, loaded = self.seed_legacy(cur)
            plan, text = self.migrate(cur, [path], files, loaded)
            self.assertIn("0 differences", text)
            self.assertEqual(plan["rows"], N)
            self.assertEqual(editions(cur), [(1, None, N)])
            cur.execute("SELECT DISTINCT source_file, release_label, "
                        "published_date FROM public.zz_s3_editions")
            (sf, label, pd_), = cur.fetchall()
            self.assertEqual(sf, "as loaded: fixture load 2025 "
                                 f"{path.name}; mid-2025; published "
                                 "2026-07-29")
            self.assertEqual(label, core.AS_LOADED_LABEL)
            self.assertEqual(pd_, loaded[P].date())
            self.assertEqual(ledger(cur), [(
                P, f"{files[P]['url']} (mid-2025; published 2026-07-29; "
                   "periods 2025)", "unchanged", 1)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            # the same bytes on every path: unchanged against edition 1
            url = self.page(2025, path.name)
            self.assertEqual(url, files[P]["url"])
            self.urls[url] = path
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
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)

    def test_two_years_with_a_named_name_form_then_the_revision(self):
        with rolled_back(self.conn) as cur:
            held_name = "Middlesbrough Borough"
            paths, files, loaded = self.seed_legacy(
                cur, years=(2024, 2025),
                names={(2024, "E06000002"): held_name})
            forms = {"2024": {"E06000002": (held_name, "Middlesbrough")}}
            plan, text = self.migrate(cur, paths, files, loaded,
                                      name_forms=forms)
            self.assertIn("E06000002", text)
            self.assertIn(held_name, text)
            self.assertEqual(editions(cur, "2024"), [(1, None, N)])
            self.assertEqual(editions(cur, "2025"), [(1, None, N)])
            # edition 1 is live as held, the held name included
            cur.execute("SELECT la_name FROM public.zz_s3_editions WHERE "
                        "reference_year = 2024 AND lad24cd = 'E06000002'")
            self.assertEqual(cur.fetchone()[0], held_name)
            # the 2024 file does not reproduce edition 1 (the name), so it
            # has no ledger row; the 2025 file's states 2024 and 2025
            self.assertEqual(ledger(cur), [(
                "2025", f"{files['2025']['url']} (mid-2025; published "
                        "2026-07-29; periods 2024, 2025)", "unchanged", 1)])
            # the 2025 file read by the page: 2024 is compared (the name
            # differs), 2025 is skipped as checked
            url = self.page(2025, paths[1].name)
            self.urls[url] = paths[1]
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2024: REJECTED", text)
            self.assertIn("la_name", text)
            self.assertIn("2025 (checked", text)
            text, _ = self.ok(cur, ["load", "--acknowledge", "2024",
                                    "--commit"])
            self.assertIn("2024: revised", text)
            self.assertEqual(editions(cur, "2024"), [(1, None, N),
                                                     (2, 1, N)])
            self.assertEqual(live_val(cur, "E06000002", "la_name", "2024"),
                             held_name)
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "E06000002", "la_name", "2024"),
                             "Middlesbrough")
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_name_difference_not_named_stops(self):
        with rolled_back(self.conn) as cur:
            paths, files, loaded = self.seed_legacy(
                cur, years=(2024,), names={(2024, "E06000002"): "Mbro"})
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, paths, write=True,
                                 legacy=self.legacy(cur, loaded),
                                 legacy_files=files, name_forms={})
            self.assertIn("proof failed", str(e.exception.code))
            self.assertIn("Mbro", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s3_editions"), 0)

    def test_a_planted_held_difference_stops_and_rolls_back(self):
        with rolled_back(self.conn) as cur:
            (path,), files, loaded = self.seed_legacy(cur)
            cur.execute("UPDATE public.zz_s3_live SET population = "
                        "population + 1 WHERE lad24cd = 'E06000002'")
            with mock.patch.object(m, "LEGACY_LIVE",
                                   self.legacy(cur, loaded)[:2]), \
                    mock.patch.object(m, "LEGACY_LOADED", loaded), \
                    mock.patch.object(m, "LEGACY_FILES", files):
                rc, text, logged = self.run_main(
                    cur, ["migrate-legacy", path, "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("proof failed", text)
            self.assertIn("E06000002", text)
            self.assertEqual(count(cur, "zz_s3_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)
            logged.assert_not_called()

    def test_the_command_previews_then_commits(self):
        with rolled_back(self.conn) as cur:
            (path,), files, loaded = self.seed_legacy(cur)
            with mock.patch.object(m, "LEGACY_LIVE",
                                   self.legacy(cur, loaded)[:2]), \
                    mock.patch.object(m, "LEGACY_LOADED", loaded), \
                    mock.patch.object(m, "LEGACY_FILES", files):
                text, logged = self.ok(cur, ["migrate-legacy", path])
                self.assertIn("PREVIEW", text)
                self.assertEqual(count(cur, "zz_s3_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, ["migrate-legacy", path,
                                             "--simulate"])
                self.assertIn("SIMULATION", text)
                self.assertEqual(count(cur, "zz_s3_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, ["migrate-legacy", path,
                                             "--commit"])
            self.assertEqual(editions(cur), [(1, None, N)])
            logged.assert_called_once()

    def test_a_wrong_file_sha_or_live_state_stops(self):
        with rolled_back(self.conn) as cur:
            (path,), files, loaded = self.seed_legacy(cur)
            bad = {P: dict(files[P], sha256="0" * 64)}
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, [path], write=True,
                                 legacy=self.legacy(cur, loaded),
                                 legacy_files=bad)
            self.assertIn("not a held file", str(e.exception.code))
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, [path], write=True,
                                 legacy=(N, "x", loaded), legacy_files=files)
            self.assertIn("not as surveyed", str(e.exception.code))
            other = {P: loaded[P].replace(year=2020)}
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, [path], write=True,
                                 legacy=self.legacy(cur, other),
                                 legacy_files=files)
            self.assertIn("loaded_at", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s3_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)

    def test_a_file_for_each_live_year_is_needed(self):
        with rolled_back(self.conn) as cur:
            paths, files, loaded = self.seed_legacy(cur, years=(2024, 2025))
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, paths[1:], write=False,
                                 legacy=self.legacy(cur, loaded),
                                 legacy_files=files)
            self.assertIn("2024", str(e.exception.code))

    def test_a_non_empty_editions_table_stops(self):
        with rolled_back(self.conn) as cur:
            (path,), files, loaded = self.seed_legacy(cur)
            self.migrate(cur, [path], files, loaded)
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, [path], write=True,
                                 legacy=self.legacy(cur, loaded),
                                 legacy_files=files)
            self.assertIn("runs once", str(e.exception.code))


class Commands(Fixture):

    def test_status_without_editions_table_is_clean_and_creates_nothing(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1)
            self.assertIn("not exist yet", text)
            self.assertFalse(pe.table_exists(cur, "zz_s3_editions"))
            self.assertFalse(pe.table_exists(cur, LEDGER))

    def test_refresh_latest_without_tables_names_migrate_legacy(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["refresh-latest"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("migrate-legacy", text)
            self.assertNotIn("sync-new --commit", text)

    def test_ddl_preview_then_commit_then_status(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["ddl"])
            self.assertEqual(rc, 0)
            self.assertIn("DRY RUN", text)
            self.assertFalse(pe.table_exists(cur, "zz_s3_editions"))
            self.ok(cur, ["ddl", "--commit"])
            for t in ("zz_s3_editions", LEDGER):
                self.assertTrue(pe.table_exists(cur, t), t)
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 0, text)

    def test_status_lines_after_a_load(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["status"])
            self.assertIn("mid-2025, published 2026-07-29", text)
            self.assertIn("ledger latest: new", text)

    def test_load_preview_before_ddl_compares_with_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            self.file()
            text, _ = self.ok(cur, ["load"])
            self.assertIn("no editions table yet", text)
            self.assertIn(f"{P}: new", text)
            rc, text, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt")
            self.assertIn("run `ddl --commit`", text)


if __name__ == "__main__":
    unittest.main()
