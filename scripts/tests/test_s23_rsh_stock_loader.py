"""Tests for the S23 loader in s23_rsh_stock_editions (the commands,
migrate-legacy, restore-edition) on throwaway tables.

No network: the content API replies and the downloads are stubbed, and every
workbook is written by the test (write_tool from test_s23_rsh_stock_pure).
Database tests run on a connection from _db.get_conn() inside a transaction
that is always rolled back (rolled_back below); nothing here commits. The
commands get a stand-in connection whose commit and rollback act on a
savepoint of that transaction, and a cursor proxy whose .connection does the
same, so the engine's per-period commit never reaches the real connection.
The only tables written are zz_s23_live (a LIKE copy of rsh_rp_stock_by_la),
zz_s23_editions and its ledger, created inside that transaction (refused if
any already exists), so they never persist. The run log is stubbed; the real
la_code_lookup and la_boundaries are read (never written).
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
import s23_rsh_stock_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s23_rsh_stock_pure import (BASE, CODES, ROWS_PER_AREA,  # noqa: E402
                                     TITLE, TOOL_TITLE, collection,
                                     release_page, write_tool)

ZZ = dataclasses.replace(
    m.SPEC, name="zz_s23", live_table="zz_s23_live",
    editions_table="zz_s23_editions",
    expected_rows_per_period=m.tip_row_count("zz_s23_editions"))
LEDGER = "zz_s23_editions_file_checks"
N = len(CODES) * ROWS_PER_AREA
P = "2025-03-31"
COLL = m.COLLECTION_PATH
SMALL = len(CODES)          # the fixture's Small PRP rows (H0375, each area)
ACK = "not-counted-lcho-2025"
LCHO = "low_cost_home_ownership"


def ack_cells(cells=SMALL):
    """The real acknowledgement with the fixture's cell count."""
    return mock.patch.dict(m.ACKNOWLEDGED_FLIPS, {ACK: dict(
        m.ACKNOWLEDGED_FLIPS[ACK], cells=cells)})


def edition_diff(cur, a, b, period=P):
    """{(rp_code, lad24cd): {column: (in a, in b)}} of the key + ROW_COLS
    cells differing between editions a and b."""
    ra = {(r["rp_code"], r["lad24cd"]): r for r in m.records(cur, ZZ, period, a)}
    rb = {(r["rp_code"], r["lad24cd"]): r for r in m.records(cur, ZZ, period, b)}
    out = {}
    for k in sorted(set(ra) | set(rb)):
        for c in m.ROW_COLS:
            x, y = ra.get(k, {}).get(c), rb.get(k, {}).get(c)
            if x != y:
                out.setdefault(k, {})[c] = (x, y)
    return out


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    """commit / rollback of a command, acting on a savepoint of the test's
    own transaction."""
    SP = "zz_s23_cmd"

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
            mock.patch.object(m, "EXPECTED_AREAS", len(CODES)), \
            mock.patch.object(m, "EXPECTED_REGIONS", 2):
        yield


@contextmanager
def rolled_back(conn, *, ddl=True):
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_s23_live'), "
                    "to_regclass('public.zz_s23_editions'), "
                    f"to_regclass('public.{LEDGER}')")
        if any(cur.fetchone()):
            raise RuntimeError("a zz_s23 table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s23_live (LIKE "
                    "public.rsh_rp_stock_by_la INCLUDING ALL)")
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
                "public.zz_s23_editions WHERE stock_date = %s "
                "GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur):
    """[(stock_date, source_file, outcome, edition)] in order."""
    cur.execute(f"SELECT stock_date::text, source_file, outcome, edition "
                f"FROM public.{LEDGER} ORDER BY id")
    return cur.fetchall()


def provenance(cur, period=P):
    cur.execute("SELECT DISTINCT edition, publication_date, source_url, "
                "source_file, release_page_url FROM public.zz_s23_live "
                "WHERE stock_date = %s", (period,))
    return cur.fetchall()


def live_val(cur, rp, la, col="general_needs_self_contained", period=P):
    cur.execute(f"SELECT {col} FROM public.zz_s23_live WHERE rp_code = %s "
                "AND lad24cd = %s AND stock_date = %s", (rp, la, period))
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
        self.api = {COLL: {"links": {"documents": []}}}
        self.urls = {}
        self.final = {}
        self.fetched = []
        self.n = 0

    def page(self, y1, y2, url):
        docs = self.api[COLL]["links"]["documents"]
        if not any(d["title"] == TITLE.format(y1, y2) for d in docs):
            docs.append({"title": TITLE.format(y1, y2),
                         "base_path": BASE.format(y1, y2)})
        self.api[BASE.format(y1, y2)] = release_page(y1, y2, [
            ("Registered providers additional tables", "x/add.xlsx"),
            (TOOL_TITLE, url)])

    def tool(self, year=2025, version="1.1", month="November 2025", *,
             register=True, url=None, **kw):
        """Write a look-up tool; register its release page and download."""
        self.n += 1
        name = f"RP_COMBINED_TOOL_{year}_FINAL_V{version}.xlsx"
        path = write_tool(self.root / f"t{self.n}" / name, year=year,
                          version=version, month=month, **kw)
        if register:
            url = url or f"https://assets.example/media/m{self.n}/{name}"
            self.urls[url] = path
            self.page(year - 1, year, url)
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
        self.tool(**kw)
        self.ok(cur, ["load", "--commit"])
        self.fetched.clear()


class Load(Fixture):

    def test_new_period_edition_live_and_one_ledger_row(self):
        with rolled_back(self.conn) as cur:
            path = self.tool()
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P}: new", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s23_live"), N)
            self.assertEqual(core.rows_differing(cur, ZZ, P, 1), 0)
            (_, src, outcome, ed), = ledger(cur)
            self.assertEqual((outcome, ed), ("new", 1))
            self.assertEqual(src, f"{self.last_url} (version 1.1; dated "
                                  f"2025-11; stock date {P})")
            self.assertEqual(provenance(cur), [(
                "2024 to 2025", date(2025, 11, 1), self.last_url, path.name,
                "https://www.gov.uk" + BASE.format(2024, 2025))])
            cur.execute("SELECT DISTINCT file_edition, file_name, "
                        "file_source_url, source_file, published_date "
                        "FROM public.zz_s23_editions")
            (fe, fn, fu, sf, pd), = cur.fetchall()
            self.assertEqual((fe, fn, fu), ("2024 to 2025", path.name,
                                            self.last_url))
            self.assertEqual(m.release_rank(sf), (date(2025, 11, 1), (1, 1)))
            self.assertEqual(pd, date(2025, 11, 1))
            cur.execute("SELECT COUNT(*) FROM public.zz_s23_live WHERE "
                        "total_social_stock = 0")
            self.assertEqual(cur.fetchone()[0], 1)    # the published zeros
            logged.assert_called_once()
            self.assertIn(f"{P} new", logged.call_args[0][2])
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_load_stores_the_not_counted_cells_null_with_the_reason(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            for table, extra in (("zz_s23_editions", " AND edition = 1"),
                                 ("zz_s23_live", "")):
                cur.execute(f"SELECT rp_code, {LCHO}, null_reasons, "
                            f"total_social_stock FROM public.{table} WHERE "
                            f"({LCHO} IS NULL OR null_reasons IS NOT NULL)"
                            f"{extra} ORDER BY lad24cd")
                rows = cur.fetchall()
                self.assertEqual(len(rows), SMALL, table)
                self.assertEqual({r[0] for r in rows}, {"H0375"})
                self.assertEqual({r[1:] for r in rows},
                                 {(None, m.NOT_COUNTED_REASON, 3)})
            # a covered published zero stays 0: the first LARP owns nothing
            self.assertEqual(live_val(cur, "00C0", "E06000001", LCHO), 0)
            self.assertEqual(live_val(cur, "00C0", "E06000001",
                                      "total_social_stock"), 0)
            # Large PRPs keep their published LCHO
            self.assertEqual(count(cur, "zz_s23_live", "WHERE rp_code = "
                                   f"'4865' AND {LCHO} IS NULL"), 0)
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            # a re-read is unchanged (the rule never flips back to 0)
            text, _ = self.ok(cur, ["load", "--recheck", P])
            self.assertIn(f"{P}: unchanged", text)

    def test_the_rule_halts_without_the_note_or_on_a_small_lcho_number(self):
        with rolled_back(self.conn) as cur:
            for kw, needle in (({"lcho_note": False}, "Area Summary"),
                               ({"values": {("H0375", "E06000002"): {
                                   "LA_LCHO_Less_100_Eqty_Own": 4}}},
                                "LARPs and Large PRPs only")):
                with self.subTest(kw=kw):
                    self.tool(**kw)
                    rc, text, logged = self.run_main(cur, ["load",
                                                           "--commit"])
                    self.assertEqual(rc, "halt", text)
                    self.assertIn(needle, text)
                    self.assertIn("H0375", text)
                    self.assertEqual(count(cur, "zz_s23_editions"), 0)
                    self.assertEqual(count(cur, "zz_s23_live"), 0)
                    logged.assert_not_called()

    def test_an_acknowledgement_is_named_and_needs_its_stock_date(self):
        with rolled_back(self.conn) as cur:
            self.tool()
            # only a name in ACKNOWLEDGED_FLIPS is accepted
            rc, text, _ = self.run_main(cur, ["load", "--acknowledge-flips",
                                              "anything"])
            self.assertEqual(rc, "halt", text)
            # a run that does not compare its stock date as a held one
            rc, text, logged = self.run_main(cur, ["load", "--commit",
                                                   "--acknowledge-flips",
                                                   ACK])
            self.assertEqual(rc, "halt", text)
            self.assertIn("does not compare", text)
            self.assertEqual(count(cur, "zz_s23_editions"), 0)
            logged.assert_not_called()

    def test_a_failing_ledger_insert_rolls_back_everything(self):
        with rolled_back(self.conn) as cur:
            self.tool()
            with mock.patch.object(pe, "record_file_check",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P}: FAILED", text)
            self.assertEqual(editions(cur), [])
            self.assertEqual(count(cur, "zz_s23_live"), 0)
            self.assertEqual(ledger(cur), [])
            logged.assert_called_once()
            self.assertIn("PARTIAL RUN", logged.call_args[0][2])
            self.assertIn(f"failed or not attempted ['{P}']",
                          logged.call_args[0][2])

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            cur.execute("SELECT COUNT(*) FROM pipeline_run_log")
            runs = cur.fetchone()[0]
            self.tool()
            text, logged = self.ok(cur, ["load"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            for t in ("zz_s23_editions", "zz_s23_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--simulate"])
            self.assertIn("SIMULATION", text)
            for t in ("zz_s23_editions", "zz_s23_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            self.ok(cur, ["load", "--commit"])
            calls = []
            real = m.read_tool
            with mock.patch.object(m, "read_tool",
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

    def test_unchanged_recheck_records_the_ledger_only_on_commit(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["load", "--recheck", P])
            self.assertIn(f"{P}: unchanged", text)
            self.assertEqual(len(ledger(cur)), 1)
            self.ok(cur, ["load", "--recheck", P, "--commit"])
            self.assertEqual([o for _, _, o, _ in ledger(cur)],
                             ["new", "unchanged"])
            self.assertEqual(editions(cur), [(1, None, N)])

    def test_ledger_skip_needs_the_pair_for_the_period(self):
        with rolled_back(self.conn) as cur:
            path = self.tool()
            rank = (date(2025, 11, 1), (1, 1))
            # the file's (URL, sha) recorded, but under another stock date
            cur.execute(f"INSERT INTO public.{LEDGER} (stock_date, "
                        "source_file, file_sha256, outcome, edition) VALUES "
                        "('2024-03-31', %s, %s, 'new', 1)",
                        (m.ledger_source(self.last_url, rank, "1.1", P),
                         m.content_sha256(path)))
            text, _ = self.ok(cur, ["load"])
            self.assertNotIn("nothing parsed", text)
            self.assertIn(f"{P}: new", text)

    def revise(self, **kw):
        return self.tool(version="1.2", month="December 2025", **kw)

    def test_revised_period_waits_for_refresh_latest(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("UPDATE public.zz_s23_live SET loaded_at = "
                        "'2026-01-01'")
            before = live_val(cur, "L0055", "E06000002")
            path = self.revise(values={("L0055", "E06000002"):
                                       {"LA_GN_SC_Own": before + 3}})
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P}: revised", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertEqual(live_val(cur, "L0055", "E06000002"), before)
            self.assertEqual([o for _, _, o, _ in ledger(cur)],
                             ["new", "revised"])
            st = pe.status(cur, m.profile())
            self.assertEqual(st["pending_refresh"], [P])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn(f"{P}=1", text)
            self.assertEqual(live_val(cur, "L0055", "E06000002"), before)
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "L0055", "E06000002"), before + 3)
            self.assertEqual(provenance(cur), [(
                "2024 to 2025", date(2025, 12, 1), self.last_url, path.name,
                "https://www.gov.uk" + BASE.format(2024, 2025))])
            cur.execute("SELECT loaded_at FROM public.zz_s23_editions WHERE "
                        "edition = 2 LIMIT 1")
            ed_loaded = cur.fetchone()[0]
            self.assertEqual(live_val(cur, "L0055", "E06000002", "loaded_at"),
                             ed_loaded)
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)

    def test_key_changes_need_accept_key_changes(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.revise(drop=[("H0375", "E06000002")],
                        add=[["New Small Limited", "H9999", "Small",
                              "Short Form", "Signed_Off", "E06000001",
                              (1, 0, 0, 0)]])
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P}: revised", text)
            self.assertIsNone(live_val(cur, "H9999", "E06000001"))
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("keys added 1 (H9999/E06000001); removed 1 "
                          "(H0375/E06000002)", text)
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("not named", text)
            self.assertIsNone(live_val(cur, "H9999", "E06000001"))
            text, _ = self.ok(cur, ["refresh-latest", "--commit",
                                    "--accept-key-changes", P])
            self.assertIn("1 inserted, 1 deleted", text)
            self.assertEqual(live_val(cur, "H9999", "E06000001"), 1)
            self.assertIsNone(live_val(cur, "H0375", "E06000002"))
            self.assertEqual(count(cur, "zz_s23_live"), N)
            self.assertEqual(len(provenance(cur)), 1)
            self.assertEqual(provenance(cur)[0][1], date(2025, 12, 1))
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def older(self, cur, how):
        """Seed from V1.1 (November 2025), then offer V1.0 (October 2025)
        with different values by the page, --release or --file."""
        self.seed(cur)
        path = self.tool(version="1.0", month="October 2025",
                         register=how != "file",
                         values={("L0055", "E06000002"): {"LA_GN_SC_Own": 110}})
        argv = ["load", "--commit"]
        if how == "release":
            argv += ["--release", "2024-2025"]
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
            argv = self.older(cur, "file") + ["--allow-older-file"]
            text, logged = self.ok(cur, argv)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertIn("--allow-older-file given", logged.call_args[0][2])
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s23_editions WHERE edition = 2")
            self.assertIn("--allow-older-file given", cur.fetchone()[0])

    def test_equal_rank_with_different_content_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.tool(values={("L0055", "E06000002"): {"LA_GN_SC_Own": 140}})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("two files claim the same version", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 1)
            logged.assert_called_once()
            self.assertIn(f"rejected ['{P}']", logged.call_args[0][2])

    def test_a_revision_breaking_a_stop_condition_is_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.revise(values={("L0055", "E06000002"): {"LA_GN_SC_Own": 5000}})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P}: REJECTED", text)
            self.assertIn("E06000002", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 1)
            logged.assert_called_once()
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN (exit 1)", notes)
            self.assertIn(f"rejected ['{P}']", notes)
            # preview of the same: nothing logged
            rc, text, logged = self.run_main(cur, ["load"])
            self.assertEqual(rc, 1, text)
            logged.assert_not_called()

    def test_a_new_period_breaking_a_stop_condition_is_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, year=2024, month="November 2024")
            # every LARP doubles: national total far more than 5%
            self.tool(values={(f"00C{i}", c): {"LA_GN_SC_Own": 5000}
                              for i, c in enumerate(CODES)})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P}: REJECTED", text)
            self.assertIn("national total_social_stock", text)
            self.assertEqual(editions(cur), [])
            self.assertEqual(count(cur, "zz_s23_live",
                                   "WHERE stock_date = %s", (P,)), 0)
            self.assertEqual([p for p, _, _, _ in ledger(cur)], ["2024-03-31"])
            self.assertIn(f"rejected ['{P}']", logged.call_args[0][2])

    def test_a_second_period_is_new_and_compared_with_the_previous(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, year=2024, month="November 2024")
            self.tool()
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P}: new", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(editions(cur, "2024-03-31"), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s23_live"), 2 * N)
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_stranded_period_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("DELETE FROM public.zz_s23_live")
            self.assertEqual(pe.status(cur, m.profile())["live_missing"], [P])
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn("live-missing", text)
            self.assertEqual(count(cur, "zz_s23_live"), N)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            self.assertEqual(len(provenance(cur)), 1)

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.tool()
            final = "https://assets.example/media/new/RP_TOOL_2025.xlsx"
            self.final[self.last_url] = final
            self.ok(cur, ["load", "--commit"])
            (_, src, _, _), = ledger(cur)
            self.assertTrue(src.startswith(final + " (version"))
            self.assertEqual(provenance(cur)[0][2], final)
            self.assertEqual(provenance(cur)[0][3], "RP_TOOL_2025.xlsx")

    def test_file_paths_and_no_page(self):
        with rolled_back(self.conn) as cur:
            path = self.tool(register=False)
            rc, text, _ = self.run_main(cur, ["load", "--file", path])
            self.assertEqual(rc, "halt", text)            # no page found
            self.assertIn("--no-page", text)
            text, logged = self.ok(cur, ["load", "--file", path,
                                         "--no-page", "--commit"])
            self.assertIn(f"{P}: new", text)
            (e, _, url, name, page), = provenance(cur)
            self.assertEqual((url, name), (f"file:{path.name}", path.name))
            self.assertIn("--no-page", page)
            self.assertIn("--no-page", logged.call_args[0][2])
            rc, text, _ = self.run_main(cur, ["load", "--no-page"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--no-page", text)

    def test_identity_and_reconciliation_halt(self):
        with rolled_back(self.conn) as cur:
            self.tool()
            self.api[BASE.format(2024, 2025)]["title"] = TITLE.format(2023,
                                                                      2024)
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.tool(subtotal={"E06000001": {"LA_SHHOP": 1}})
            rc, text, _ = self.run_main(cur, ["load", "--release",
                                              "2024-2025"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("reconciliation", text)
            path = self.tool(register=False)
            rc, text, _ = self.run_main(cur, ["load", "--file", path,
                                              "--release", "2023-2024"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--release 2023-2024", text)
            self.assertEqual(count(cur, "zz_s23_editions"), 0)

    def test_new_codes_halt_against_the_old_declaration(self):
        with rolled_back(self.conn) as cur:
            self.tool(codes=["E06000001", "E06000002", "E08000038",
                             "E08000019"])
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("E08000038", text)
            self.assertIn("'old'", text)
            self.assertEqual(count(cur, "zz_s23_editions"), 0)
            logged.assert_not_called()

    def test_discovery_fails_loudly(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.api[COLL]["links"]["documents"].append(
                {"title": "Registered provider social housing stock and "
                          "rents in England 2025/26",
                 "base_path": "/government/statistics/x"})
            rc, text, logged = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2025/26", text)
            self.assertNotIn("nothing to do", text)
            self.api[BASE.format(2024, 2025)]["details"]["attachments"] = [
                (dict(title="Registered providers additional tables",
                      url="x.xlsx"))]
            self.api[COLL]["links"]["documents"].pop()
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("additional tables", text)

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            first = live_val(cur, "L0055", "E06000002")
            self.revise(values={("L0055", "E06000002"):
                                {"LA_GN_SC_Own": first + 3}})
            self.ok(cur, ["load", "--commit"])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "L0055", "E06000002"), first + 3)
            text, logged = self.ok(cur, ["restore-edition", P, "1",
                                         "--commit"])
            self.assertIn("as edition 3", text)
            self.assertEqual(editions(cur)[-1], (3, 2, N))
            logged.assert_called_once()
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "L0055", "E06000002"), first)
            rc, text, _ = self.run_main(cur, ["restore-edition", P, "3"])
            self.assertEqual(rc, "halt")
            self.assertIn("is the tip", text)


class Migrate(Fixture):

    URL = "https://assets.example/media/h/RP_COMBINED_TOOL_2025_FINAL_V1.1.xlsx"
    PAGE = "https://www.gov.uk" + BASE.format(2024, 2025)

    def seed_legacy(self, cur, **kw):
        """Held-style live rows built from a fixture file as published (the
        old build stored the Small PRP LCHO zeros), with the page's
        provenance (first published 2025-10-28) and one loaded_at."""
        path = self.tool(register=False, **kw)
        t = m.read_tool(path)
        rmap, problems = m.resolve_codes(cur, m.tool_codes(t), P)
        self.assertEqual(problems, [])
        prov = {"file_edition": "2024 to 2025",
                "file_publication_date": date(2025, 10, 28),
                "file_source_url": self.URL, "file_name": path.name,
                "file_release_page_url": self.PAGE}
        recs = m.build_rows(t, lambda c: rmap[c], prov, rule=False)
        m.insert_live(cur, m.profile(), P, recs)
        cur.execute("UPDATE public.zz_s23_live SET loaded_at = "
                    "'2026-08-14 21:24:42.442022+00'")
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

    def check_reread_after_migration(self, cur, path, files):
        """After migrate-legacy (edition 1 holds the published Small PRP
        LCHO zeros), the same bytes read again carry the not-counted rule:
        on every path the stock date is REVISED in exactly the Small rows'
        LCHO cells, and the 0/NULL stop rejects it unless the named
        acknowledgement matches the count. With it, edition 2 stores the
        rule's NULLs (provenance kept: same bytes), refresh-latest brings
        live to it, and every path is then unchanged."""
        self.urls[files["url"]] = path
        self.page(2024, 2025, files["url"])
        text, _ = self.ok(cur, ["load"])
        self.assertIn("nothing parsed", text)
        for argv in (["load", "--recheck", P],
                     ["load", "--release", "2024-2025", "--recheck", P],
                     ["load", "--file", path],
                     ["load", "--file", path, "--no-page"],
                     ["load", "--file", path, "--commit"],
                     ["load", "--recheck", P, "--commit"]):
            with self.subTest(argv=argv):
                rc, text, _ = self.run_main(cur, argv)
                self.assertEqual(rc, 1, text)
                self.assertIn(f"{P}: REJECTED", text)
                self.assertIn(f"{SMALL} cell(s) go from 0 to NULL", text)
                self.assertIn("--acknowledge-flips", text)
                self.assertNotIn("two files claim", text)
                self.assertEqual(editions(cur), [(1, None, N)])
        self.assertEqual([o for _, _, o, _ in ledger(cur)], ["unchanged"])
        argv = ["load", "--file", path, "--no-page", "--acknowledge-flips",
                ACK]
        # the count must match exactly
        with ack_cells(SMALL + 1):
            rc, text, _ = self.run_main(cur, argv + ["--commit"])
        self.assertEqual(rc, 1, text)
        self.assertIn(f"expects exactly {SMALL + 1} cell(s)", text)
        self.assertEqual(editions(cur), [(1, None, N)])
        with ack_cells():
            text, _ = self.ok(cur, argv)
            self.assertIn(f"{P}: revised, {SMALL} rows changed", text)
            self.assertIn("ACKNOWLEDGED", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            text, logged = self.ok(cur, argv + ["--commit"])
        self.assertIn("the tip's provenance is kept", text)
        self.assertIn(f"ACKNOWLEDGED {ACK}", logged.call_args[0][2])
        self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
        diff = edition_diff(cur, 1, 2)
        self.assertEqual(len(diff), SMALL)
        for k, cells in diff.items():
            self.assertEqual(k[0], "H0375")
            self.assertEqual(cells, {LCHO: (0, None), "null_reasons": (
                None, m.NOT_COUNTED_REASON)})
        cur.execute("SELECT COUNT(DISTINCT (file_edition, "
                    "file_publication_date, file_source_url, file_name, "
                    "file_release_page_url)) FROM public.zz_s23_editions")
        self.assertEqual(cur.fetchone()[0], 1)        # provenance kept
        cur.execute("SELECT DISTINCT release_label FROM public.zz_s23_editions "
                    "WHERE edition = 2")
        self.assertIn(f"ACKNOWLEDGED {ACK}", cur.fetchone()[0])
        self.assertEqual([o for _, _, o, _ in ledger(cur)],
                         ["unchanged", "revised"])
        # live waits for refresh-latest
        self.assertEqual(core.rows_differing(cur, ZZ, P, 1), 0)
        self.assertEqual(pe.status(cur, m.profile())["pending_refresh"], [P])
        before = provenance(cur)
        text, _ = self.ok(cur, ["refresh-latest"])
        self.assertIn(f"{P}={SMALL}", text)
        self.ok(cur, ["refresh-latest", "--commit"])
        self.assertEqual(m.live_equals_tip(cur, ZZ, [P]), [])
        self.assertEqual(provenance(cur), before)
        self.assertEqual(count(cur, "zz_s23_live", f"WHERE {LCHO} IS NULL"),
                         SMALL)
        self.assertTrue(pe.status(cur, m.profile())["ok"])
        # the rule holds on every path: nothing flips back to 0
        text, _ = self.ok(cur, ["load"])
        self.assertIn("nothing parsed", text)
        for argv in (["load", "--recheck", P],
                     ["load", "--file", path, "--recheck", P],
                     ["load", "--file", path, "--no-page", "--recheck", P,
                      "--commit"]):
            with self.subTest(argv=argv):
                text, _ = self.ok(cur, argv)
                self.assertIn(f"{P}: unchanged", text)
                self.assertNotIn("REJECTED", text)
        self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
        text, _ = self.ok(cur, ["refresh-latest"])
        self.assertIn("would write: none", text)

    def test_proof_passes_and_edition_1_and_ledger_are_stored(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            plan, text = self.migrate(cur, path, files)
            self.assertIn("0 differences", text)
            self.assertEqual(plan["rows"], N)
            self.assertEqual(editions(cur), [(1, None, N)])
            cur.execute("SELECT DISTINCT source_file, release_label, "
                        "published_date, file_publication_date FROM "
                        "public.zz_s23_editions")
            (sf, label, pd, fpd), = cur.fetchall()
            self.assertEqual(sf, f"as loaded: {path.name}; version 1.1; "
                                 "dated 2025-11")
            self.assertEqual(label, core.AS_LOADED_LABEL)
            self.assertEqual((pd, fpd), (date(2026, 8, 14), date(2025, 10, 28)))
            self.assertEqual(ledger(cur), [(
                P, f"{self.URL} (version 1.1; dated 2025-11; stock date {P})",
                "unchanged", 1)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            # the same bytes, read again on every path: the not-counted
            # rule's effect, released only by the named acknowledgement
            self.check_reread_after_migration(cur, path, files)

    def test_a_published_blank_rechecks_with_the_rule(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(
                cur, values={("H0375", "E06000002"): {"Survey_Status": None,
                                                      "SDR_Size": None}})
            cur.execute("SELECT COUNT(*) FROM public.zz_s23_live WHERE "
                        "survey_status IS NULL AND rp_size_band IS NULL")
            self.assertEqual(cur.fetchone()[0], 1)
            self.migrate(cur, path, files)
            self.check_reread_after_migration(cur, path, files)

    def test_a_planted_held_difference_stops_and_rolls_back(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            cur.execute("UPDATE public.zz_s23_live SET "
                        "general_needs_self_contained = "
                        "general_needs_self_contained + 1, total_social_stock "
                        "= total_social_stock + 1 WHERE rp_code = 'L0055' "
                        "AND lad24cd = 'E06000002'")
            legacy = self.legacy(cur)
            with mock.patch.object(m, "LEGACY_LIVE", legacy), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                rc, text, logged = self.run_main(
                    cur, ["migrate-legacy", path, "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("proof failed", text)
            self.assertEqual(count(cur, "zz_s23_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)
            logged.assert_not_called()

    def test_the_command_previews_then_commits(self):
        with rolled_back(self.conn) as cur:
            path, files = self.seed_legacy(cur)
            with mock.patch.object(m, "LEGACY_LIVE", self.legacy(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                text, logged = self.ok(cur, ["migrate-legacy", path])
                self.assertIn("PREVIEW", text)
                self.assertEqual(count(cur, "zz_s23_editions"), 0)
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
                m.migrate_legacy(cur, path, write=False,
                                 legacy=self.legacy(cur), files=bad)
            self.assertIn("expected 00000000", str(e.exception.code))
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, path, write=False, legacy=(N, "x"),
                                 files=files)
            self.assertIn("not as surveyed", str(e.exception.code))

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
            self.assertFalse(pe.table_exists(cur, "zz_s23_editions"))

    def test_ddl_preview_then_commit_then_status(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["ddl"])
            self.assertEqual(rc, 0)
            self.assertIn("DRY RUN", text)
            self.assertFalse(pe.table_exists(cur, "zz_s23_editions"))
            self.ok(cur, ["ddl", "--commit"])
            for t in ("zz_s23_editions", LEDGER):
                self.assertTrue(pe.table_exists(cur, t), t)
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 0, text)

    def test_status_lines_after_a_load(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["status"])
            self.assertIn("version 1.1, dated 2025-11", text)
            self.assertIn("provenance uniform, the tip's", text)
            cur.execute("UPDATE public.zz_s23_live SET source_file = 'x' "
                        "WHERE rp_code = '4865' AND lad24cd = 'E06000001'")
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1, text)
            self.assertIn("not uniform", text)

    def test_load_preview_before_ddl_compares_with_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            self.tool()
            text, _ = self.ok(cur, ["load"])
            self.assertIn("no editions table yet", text)
            self.assertIn(f"{P}: new", text)
            rc, text, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt")
            self.assertIn("run `ddl --commit`", text)


if __name__ == "__main__":
    unittest.main()
