"""Tests for the S10 loader in s10_rough_sleeping_editions (the commands,
migrate-legacy, restore-edition) on throwaway tables.

No network: the content API replies and the downloads are stubbed, and every
.ods file is written by the test (write_file from
test_s10_rough_sleeping_pure). Database tests run on a connection from
_db.get_conn() inside a transaction that is always rolled back (rolled_back
below); nothing here commits. The commands get a stand-in connection whose
commit and rollback act on a savepoint of that transaction, and a cursor
proxy whose .connection does the same, so the engine's per-period commit
never reaches the real connection. The only tables written are zz_s10_live
(a LIKE copy of la_rough_sleeping), zz_s10_editions and its ledger, created
inside that transaction (refused if any already exists), so they never
persist. The run log is stubbed; the real la_code_lookup and la_boundaries
are read (never written).
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
import s10_rough_sleeping_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s10_rough_sleeping_pure import (BASE, CODES, TABLES,  # noqa: E402
                                          TITLE, _Session, collection,
                                          release_page, value, write_file)

ZZ = dataclasses.replace(m.SPEC, name="zz_s10", live_table="zz_s10_live",
                         editions_table="zz_s10_editions")
LEDGER = "zz_s10_editions_file_checks"
N = len(CODES)
P = "2025"
PUB = date(2026, 2, 26)
COLL = m.COLLECTION_PATH


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    """commit / rollback of a command, acting on a savepoint of the test's
    own transaction."""
    SP = "zz_s10_cmd"

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
        cur.execute("SELECT to_regclass('public.zz_s10_live'), "
                    "to_regclass('public.zz_s10_editions'), "
                    f"to_regclass('public.{LEDGER}')")
        if any(cur.fetchone()):
            raise RuntimeError("a zz_s10 table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s10_live (LIKE "
                    "public.la_rough_sleeping INCLUDING ALL)")
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
                "public.zz_s10_editions WHERE snapshot_year = %s "
                "GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur):
    """[(snapshot_year, source_file, outcome, edition)] in order."""
    cur.execute(f"SELECT snapshot_year::text, source_file, outcome, edition "
                f"FROM public.{LEDGER} ORDER BY id")
    return cur.fetchall()


def live_val(cur, la, col="rough_sleeping", period=P):
    cur.execute(f"SELECT {col} FROM public.zz_s10_live WHERE lad24cd = %s "
                "AND snapshot_year = %s", (la, period))
    r = cur.fetchone()
    return r[0] if r else None


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
        self.api = {COLL: collection()}
        self.urls = {}
        self.final = {}
        self.fetched = []
        self.n = 0

    def page(self, year, url):
        docs = self.api[COLL]["links"]["documents"]
        if not any(d["title"] == TITLE.format(year) for d in docs):
            docs.append({"title": TITLE.format(year),
                         "base_path": BASE.format(year)})
        self.api[BASE.format(year)] = release_page(year, [
            (TITLE.format(year), BASE.format(year) + "/html"),
            (TABLES.format(year), url)])

    def file(self, year=2025, published=None, *, register=True, url=None,
             **kw):
        """Write a snapshot file; register its release page and download."""
        self.n += 1
        published = published or date(year + 1, 2, 26)
        name = f"Rough_sleeping_snapshot_in_England__autumn_{year}.ods"
        path = write_file(self.root / f"t{self.n}" / name, year,
                          published=published, **kw)
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
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s10_live"), N)
            self.assertEqual(core.rows_differing(cur, ZZ, P, 1), 0)
            (_, src, outcome, ed), = ledger(cur)
            self.assertEqual((outcome, ed), ("new", 1))
            self.assertEqual(src, f"{self.last_url} (autumn 2025; published "
                                  "2026-02-26; periods 2025)")
            cur.execute("SELECT DISTINCT source_file, published_date, "
                        "release_label FROM public.zz_s10_editions")
            (sf, pd_, label), = cur.fetchall()
            self.assertEqual(m.release_rank(sf), (PUB, 2025))
            self.assertEqual(pd_, PUB)
            self.assertIn("sha256", label)
            # Barnsley and Sheffield stored on the canonical codes
            self.assertEqual(live_val(cur, "E08000016"),
                             value("E08000038", 2025))
            self.assertIsNone(live_val(cur, "E08000038"))
            # the published zeros stay 0
            self.assertEqual(live_val(cur, "E06000001"), 0)
            self.assertEqual(live_val(cur, "E06000001",
                                      "rough_sleeping_prev_year"), 0)
            self.assertEqual(live_val(cur, "E09000033",
                                      "rough_sleeping_prev_year"), 370)
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
            self.assertEqual(count(cur, "zz_s10_live"), 0)
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
            for t in ("zz_s10_editions", "zz_s10_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--simulate"])
            self.assertIn("SIMULATION", text)
            for t in ("zz_s10_editions", "zz_s10_live", LEDGER):
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

    def test_the_previous_year_restated_unchanged_and_a_new_year(self):
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
            self.assertEqual(count(cur, "zz_s10_live"), 2 * N)
            self.assertEqual(live_val(cur, "E09000033", period="2026"), 380)
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
            self.assertIn("never happened", text)
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
                        "public.zz_s10_editions WHERE snapshot_year = 2025 "
                        "AND edition = 2")
            self.assertIn("ACKNOWLEDGED", cur.fetchone()[0])
            self.assertEqual(pe.status(cur, m.profile())["pending_refresh"],
                             ["2025"])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("2025=1", text)
            self.assertEqual(live_val(cur, "E06000002"), before)
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "E06000002"), before + 2)
            cur.execute("SELECT loaded_at FROM public.zz_s10_editions WHERE "
                        "snapshot_year = 2025 AND edition = 2 LIMIT 1")
            ed_loaded = cur.fetchone()[0]
            self.assertEqual(live_val(cur, "E06000002", "loaded_at"),
                             ed_loaded)
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_an_acknowledgement_must_name_a_compared_period(self):
        with rolled_back(self.conn) as cur:
            self.file()
            rc, text, logged = self.run_main(cur, ["load", "--commit",
                                                   "--acknowledge", "2019"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2019", text)
            self.assertEqual(count(cur, "zz_s10_editions"), 0)
            logged.assert_not_called()

    def test_ledger_skip_needs_the_pair_for_every_period(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            path = self.file(2026)
            rank = (date(2027, 2, 26), 2026)
            # the file's (URL, sha) recorded for 2025 only, not for 2026
            cur.execute(f"INSERT INTO public.{LEDGER} (snapshot_year, "
                        "source_file, file_sha256, outcome, edition) VALUES "
                        "(2025, %s, %s, 'unchanged', 1)",
                        (m.ledger_source(self.last_url, rank, [2025, 2026]),
                         m.content_sha256(path)))
            text, _ = self.ok(cur, ["load"])
            self.assertNotIn("nothing parsed", text)
            self.assertIn("2026: new", text)
            self.assertIn("2025 (checked", text)

    def older(self, cur, how):
        """Seed from the file published 2026-02-26, then offer an autumn
        2025 file published 2026-01-15 with a different value by the page,
        --release or --file."""
        self.seed(cur)
        path = self.file(published=date(2026, 1, 15),
                         register=how != "file",
                         values={("E06000002", 2025): 99})
        argv = ["load", "--commit"]
        if how == "release":
            argv += ["--release", "2025"]
        if how == "file":
            argv += ["--file", path]
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
                        "public.zz_s10_editions WHERE edition = 2")
            self.assertIn("--allow-older-file given", cur.fetchone()[0])

    def test_equal_rank_with_different_content_stops_unless_accepted(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.file(values={("E06000002", 2025): 40})
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
            self.file(2026, values={("E09000033", 2026): 900})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2026: REJECTED", text)
            self.assertIn("E09000033", text)
            self.assertEqual(editions(cur, "2026"), [])
            self.assertEqual(count(cur, "zz_s10_live",
                                   "WHERE snapshot_year = 2026"), 0)
            self.assertNotIn("2026", [p for p, _, _, _ in ledger(cur)])
            logged.assert_called_once()
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN (exit 1)", notes)
            self.assertIn("rejected ['2026'", notes)
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
            cur.execute("DELETE FROM public.zz_s10_live")
            self.assertEqual(pe.status(cur, m.profile())["live_missing"], [P])
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn("live-missing", text)
            self.assertEqual(count(cur, "zz_s10_live"), N)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.file()
            final = "https://assets.example/media/new/RS_2025.ods"
            self.final[self.last_url] = final
            self.ok(cur, ["load", "--commit"])
            (_, src, _, _), = ledger(cur)
            self.assertTrue(src.startswith(final + " (autumn 2025"))
            cur.execute("SELECT DISTINCT source_file FROM "
                        "public.zz_s10_editions")
            self.assertIn(final, cur.fetchone()[0])

    def test_same_name_download_with_different_content_is_kept_beside(self):
        with rolled_back(self.conn) as cur:
            held = self.file(register=False)
            new = self.file(published=date(2026, 3, 5), register=False)
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
            self.assertIn("Publication Date: 5th March 2026", text)

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

    def test_identity_and_reconciliation_halt(self):
        with rolled_back(self.conn) as cur:
            self.file()
            self.api[BASE.format(2025)]["title"] = TITLE.format(2024)
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.file(england={2025: 1})
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
            self.assertEqual(count(cur, "zz_s10_editions"), 0)

    def test_old_codes_halt_against_the_new_declaration(self):
        with rolled_back(self.conn) as cur:
            self.file(codes=["E06000001", "E06000002", "E08000016",
                             "E08000039", "E09000033"])
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("E08000016", text)
            self.assertIn("'new'", text)
            self.assertEqual(count(cur, "zz_s10_editions"), 0)
            logged.assert_not_called()

    def test_a_marker_in_a_loaded_year_halts(self):
        with rolled_back(self.conn) as cur:
            self.file(values={("E06000002", 2024): "[x]"})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Not Available", text)
            self.assertEqual(count(cur, "zz_s10_editions"), 0)
            logged.assert_not_called()

    def test_discovery_fails_loudly(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["load"])
            self.assertIn("no newer release", text)
            self.api[COLL]["links"]["documents"].append(
                {"title": "Rough sleeping in England: autumn 2026",
                 "base_path": "/government/statistics/x"})
            rc, text, logged = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("autumn 2026", text)
            self.assertIn("autumn 2025", text)
            self.assertNotIn("nothing to do", text)
            self.api[COLL]["links"]["documents"].pop()
            self.api[BASE.format(2025)]["details"]["attachments"] = [
                {"title": TABLES.format(2025) + " (accessible)",
                 "url": "x.ods"}]
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("(accessible)", text)
            self.api[COLL]["title"] = "Homelessness"
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Homelessness", text)

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            first = live_val(cur, "E06000002")
            self.file(published=date(2026, 3, 5),
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

    URL = ("https://assets.example/media/h/"
           "Rough_sleeping_snapshot_in_England__autumn_2025.ods")

    def seed_legacy(self, cur, **kw):
        """Held-style live rows built from a fixture file, one loaded_at."""
        path = self.file(register=False, **kw)
        f = m.read_file(path)
        rmap, problems = m.resolve_codes(cur, set(f["las"]), 2025)
        self.assertEqual(problems, [])
        recs = m.build_records(f, 2025, rmap)
        pe.insert_live(cur, m.profile(), P, recs)
        cur.execute("UPDATE public.zz_s10_live SET loaded_at = "
                    "'2026-08-19 23:59:03.530757+00'")
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

    def test_proof_passes_and_byte_identical_rereads_are_unchanged(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            plan, text = self.migrate(cur, path, files)
            self.assertIn("0 differences", text)
            self.assertEqual(plan["rows"], N)
            self.assertEqual(editions(cur), [(1, None, N)])
            cur.execute("SELECT DISTINCT source_file, release_label, "
                        "published_date FROM public.zz_s10_editions")
            (sf, label, pd_), = cur.fetchall()
            self.assertEqual(sf, f"as loaded: {path.name}; autumn 2025; "
                                 "published 2026-02-26")
            self.assertEqual(label, core.AS_LOADED_LABEL)
            self.assertEqual(pd_, date(2026, 8, 19))
            self.assertEqual(ledger(cur), [(
                P, f"{self.URL} (autumn 2025; published 2026-02-26; "
                   "periods 2025)", "unchanged", 1)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])
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
            self.assertEqual(live_val(cur, "E06000001"), 0)
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)

    def test_a_planted_held_difference_stops_and_rolls_back(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            cur.execute("UPDATE public.zz_s10_live SET rough_sleeping = "
                        "rough_sleeping + 1 WHERE lad24cd = 'E06000002'")
            with mock.patch.object(m, "LEGACY_LIVE", self.legacy(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                rc, text, logged = self.run_main(
                    cur, ["migrate-legacy", path, "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("proof failed", text)
            self.assertIn("E06000002", text)
            self.assertEqual(count(cur, "zz_s10_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)
            logged.assert_not_called()

    def test_the_command_previews_then_commits(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            with mock.patch.object(m, "LEGACY_LIVE", self.legacy(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                text, logged = self.ok(cur, ["migrate-legacy", path])
                self.assertIn("PREVIEW", text)
                self.assertEqual(count(cur, "zz_s10_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, ["migrate-legacy", path,
                                             "--simulate"])
                self.assertIn("SIMULATION", text)
                self.assertEqual(count(cur, "zz_s10_editions"), 0)
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
            self.assertEqual(count(cur, "zz_s10_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)

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
            self.assertFalse(pe.table_exists(cur, "zz_s10_editions"))
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
            self.assertFalse(pe.table_exists(cur, "zz_s10_editions"))
            self.ok(cur, ["ddl", "--commit"])
            for t in ("zz_s10_editions", LEDGER):
                self.assertTrue(pe.table_exists(cur, t), t)
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 0, text)

    def test_status_lines_after_a_load(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["status"])
            self.assertIn("autumn 2025, published 2026-02-26", text)
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
