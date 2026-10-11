"""Tests for the S17 loader in s17_marac_editions (the commands,
migrate-legacy, restore-edition) on throwaway tables.

No network: the data page and the downloads are stubbed, and every workbook
is written by the test (write_marac from test_s17_marac_pure). Database
tests run on a connection from _db.get_conn() inside a transaction that is
always rolled back (rolled_back below); nothing here commits. The commands
get a stand-in connection whose commit and rollback act on a savepoint of
that transaction, so the engine's per-year commit never reaches the real
connection. The only tables written are zz_s17_live (a LIKE copy of
marac_cases), zz_s17_editions and its ledger, created inside that
transaction (refused if any already exists), so they never persist. The run
log is stubbed; la_pfa_mapping is read (never written) only by the mapping
survey check.
"""
import contextlib
import dataclasses
import io
import sys
import tempfile
import unittest
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s17_marac_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s17_marac_pure import (PFAS, Resp, page_html,  # noqa: E402
                                 write_marac)

ZZ = dataclasses.replace(
    m.SPEC, name="zz_s17", live_table="zz_s17_live",
    editions_table="zz_s17_editions",
    expected_rows_per_period=m.tip_row_count("zz_s17_editions"))
LEDGER = "zz_s17_editions_file_checks"
N = len(PFAS)
Y = "2025-26"
Y0 = "2024-25"
LINK = "https://safelives.org.uk/wp-content/uploads/MARAC-DATA-{}-{}.xlsx"


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    SP = "zz_s17_cmd"

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
def specs(root=None, names=PFAS):
    with mock.patch.object(m, "SPEC", ZZ), \
            mock.patch.object(m, "pfa_names", return_value=names), \
            mock.patch.object(m, "FILE_ROOTS",
                              (root,) if root else m.FILE_ROOTS):
        yield


@contextmanager
def rolled_back(conn, *, ddl=True):
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_s17_live'), "
                    "to_regclass('public.zz_s17_editions'), "
                    f"to_regclass('public.{LEDGER}')")
        if any(cur.fetchone()):
            raise RuntimeError("a zz_s17 table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s17_live (LIKE "
                    "public.marac_cases INCLUDING ALL)")
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


def editions(cur, period=Y):
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM "
                "public.zz_s17_editions WHERE financial_year = %s "
                "GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur):
    cur.execute(f"SELECT financial_year, source_file, outcome, edition "
                f"FROM public.{LEDGER} ORDER BY id")
    return cur.fetchall()


def live_val(cur, pfa, col="cases_discussed", period=Y):
    cur.execute(f"SELECT {col} FROM public.zz_s17_live WHERE "
                "pfa_name_safelives = %s AND financial_year = %s",
                (pfa, period))
    r = cur.fetchone()
    return r[0] if r else "absent"


def ed_val(cur, pfa, col, edition, period=Y):
    cur.execute(f"SELECT {col} FROM public.zz_s17_editions WHERE "
                "pfa_name_safelives = %s AND financial_year = %s AND "
                "edition = %s", (pfa, period, edition))
    r = cur.fetchone()
    return r[0] if r else "absent"


class Fixture(unittest.TestCase):
    """Files written by the test; the data page and downloads stubbed."""

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
        self.links = {}          # year label -> link URL
        self.urls = {}           # link URL -> path
        self.final = {}
        self.fetched = []
        self.page_refused = False
        self.names = PFAS
        self.n = 0

    def file(self, year=2026, *, register=True, modified="2026-07-02T14:06:06Z",
             **kw):
        """Write a SafeLives workbook; list it on the data page."""
        self.n += 1
        name = f"MARAC-DATA-{year - 1}-{year}.xlsx"
        path = write_marac(self.root / f"f{self.n}" / name, year=year,
                           modified=modified, **kw)
        if register:
            url = LINK.format(year - 1, year)
            label = f"{year - 1}-{str(year)[2:]}"
            self.links[label] = url
            self.urls[url] = path
            self.last_url = url
        return path

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, log)."""
        borrowed = _Borrowed(cur)

        def fetch(url, dest, session=None):
            self.fetched.append(url)
            return self.urls[url], self.final.get(url, url)

        def fetch_page(url=None, session=None, dest=None):
            if self.page_refused:
                m.halt(m.page_refused_message(m.REGISTRY_URL))
            return page_html(self.links.values()), m.DATA_PAGE

        out = io.StringIO()
        with specs(self.root, self.names), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_page", side_effect=fetch_page), \
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

    def test_new_year_edition_live_and_ledger_in_one_transaction(self):
        with rolled_back(self.conn) as cur:
            path = self.file()
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{Y}: new", text)
            self.assertNotIn("LANCASHIRE", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s17_live"), N)
            self.assertEqual(core.rows_differing(cur, ZZ, Y, 1), 0)
            (_, src, outcome, ed), = ledger(cur)
            self.assertEqual((outcome, ed), ("new", 1))
            self.assertEqual(src, f"{self.last_url} (SafeLives {Y}; "
                                  "modified 2026-07-02T14:06:06Z)")
            self.assertEqual(live_val(cur, "City of London",
                                      "housing_referrals"), Decimal("0.00"))
            self.assertEqual(live_val(cur, "Lancashire",
                                      "housing_referrals"), Decimal("0.00"))
            self.assertIsNone(ed_val(cur, "Lancashire", "value_flag", 1))
            cur.execute("SELECT DISTINCT source FROM public.zz_s17_live")
            (source,), = cur.fetchall()
            self.assertIn(path.name, source)
            self.assertIn("sha256", source)
            logged.assert_called_once()
            self.assertIn(f"{Y} new", logged.call_args[0][2])
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_failing_ledger_insert_rolls_back_the_year(self):
        with rolled_back(self.conn) as cur:
            self.file()
            with mock.patch.object(pe, "record_file_check",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{Y}: FAILED", text)
            self.assertEqual(editions(cur), [])
            self.assertEqual(count(cur, "zz_s17_live"), 0)
            self.assertEqual(ledger(cur), [])
            logged.assert_called_once()
            self.assertIn("PARTIAL RUN", logged.call_args[0][2])

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            self.file()
            text, logged = self.ok(cur, ["load"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            for t in ("zz_s17_editions", "zz_s17_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--simulate"])
            self.assertIn("SIMULATION", text)
            for t in ("zz_s17_editions", "zz_s17_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            self.ok(cur, ["load", "--commit"])
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn("already in the ledger", text)
            logged.assert_not_called()
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 1)

    def test_a_byte_identical_reread_is_unchanged_on_every_path(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            path = self.urls[self.last_url]
            for argv in (["load", "--recheck", Y],
                         ["load", "--year", Y, "--recheck", Y],
                         ["load", "--file", path],
                         ["load", "--file", path, "--no-page"],
                         ["load", "--file", path, "--no-page", "--commit"]):
                with self.subTest(argv=argv):
                    text, _ = self.ok(cur, argv)
                    self.assertIn(f"{Y}: unchanged", text)
                    self.assertNotIn("REJECTED", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual([o for _, _, o, _ in ledger(cur)],
                             ["new", "unchanged"])

    def revise(self, **kw):
        return self.file(modified="2026-09-01T10:00:00Z", **kw)

    def test_a_reissued_file_stops_unless_accept_reissue(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.revise(values={"Cumbria": {"cases_discussed": 120.5}})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{Y}: REJECTED", text)
            self.assertIn("--accept-reissue", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertIn("PARTIAL RUN", logged.call_args[0][2])
            rc, text, _ = self.run_main(cur, ["load", "--commit",
                                              "--accept-reissue", Y])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"--acknowledge {Y}", text)
            text, logged = self.ok(cur, ["load", "--commit", "--accept-reissue",
                                         Y, "--acknowledge", Y])
            self.assertIn(f"{Y}: revised", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertEqual(live_val(cur, "Cumbria"), Decimal("100.50"))
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertIn("--accept-reissue", logged.call_args[0][2])
            # refresh-latest brings live to it
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "Cumbria"), Decimal("120.50"))
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_equal_rank_with_different_content_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.file(values={"Cumbria": {"cases_discussed": 120.5}})
            rc, text, _ = self.run_main(cur, ["load", "--commit",
                                              "--acknowledge", Y])
            self.assertEqual(rc, 1, text)
            self.assertIn("--accept-reissue", text)
            self.assertEqual(editions(cur), [(1, None, N)])

    def test_an_older_file_halts_on_every_path(self):
        for how in ("page", "file"):
            with self.subTest(how=how), rolled_back(self.conn) as cur:
                self.seed(cur)
                path = self.file(modified="2026-06-01T10:00:00Z",
                                 register=how == "page",
                                 values={"Cumbria": {"cases_discussed": 7}})
                argv = ["load", "--commit"] + (["--file", path, "--no-page"]
                                               if how == "file" else [])
                rc, text, logged = self.run_main(cur, argv)
                self.assertEqual(rc, "halt", text)
                self.assertIn("older", text)
                self.assertEqual(editions(cur), [(1, None, N)])
                logged.assert_not_called()
                text, logged = self.ok(cur, argv + [
                    "--allow-older-file", "--accept-reissue", Y,
                    "--acknowledge", Y])
                self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
                self.assertIn("--allow-older-file given",
                              logged.call_args[0][2])

    def test_flips_need_a_named_acknowledgement(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            argv = ["load", "--commit", "--accept-reissue", Y,
                    "--acknowledge", Y]
            # a published 0 going to NULL is a flip
            self.revise(values={"City of London": {
                "housing_referrals": "No Data"}})
            rc, text, logged = self.run_main(cur, argv)
            self.assertEqual(rc, 1, text)
            self.assertIn("--acknowledge-flips", text)
            self.assertIn("City of London/housing_referrals", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            # an entry that lists other cells does not release it
            entry = {"decided": "test", "why": "test", "periods": {Y: {
                "Cumbria/housing_referrals": ("0.00", None)}}}
            with mock.patch.dict(m.ACKNOWLEDGED_FLIPS, {"t": entry}):
                rc, text, _ = self.run_main(
                    cur, argv + ["--acknowledge-flips", "t"])
            self.assertEqual(rc, 1, text)
            self.assertEqual(editions(cur), [(1, None, N)])

    def test_a_named_flip_is_released_and_logged(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.revise(values={"Suffolk": {"repeat_cases": "No Data"}})
            entry = {"decided": "test", "why": "test", "periods": {Y: {
                "Suffolk/repeat_cases": ("180.00", None)}}}
            argv = ["load", "--commit", "--accept-reissue", Y,
                    "--acknowledge", Y, "--acknowledge-flips", "t"]
            with mock.patch.dict(m.ACKNOWLEDGED_FLIPS, {"t": entry}):
                text, logged = self.ok(cur, argv)
            self.assertIn("ACKNOWLEDGED", text)
            self.assertIsNone(ed_val(cur, "Suffolk", "repeat_cases", 2))
            self.assertEqual(ed_val(cur, "Suffolk", "value_flag", 2),
                             "not_submitted")
            self.assertIn("ACKNOWLEDGED FLIPS t", logged.call_args[0][2])

    def test_fewer_forces_than_the_tip_is_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            regions = (("North West", ("Cumbria", "Lancashire")),
                       ("West Midlands", ("Staffordshire", "West Midlands")),
                       ("East", ("Norfolk",)),
                       ("London", ("Metropolitan Police", "City of London")))
            self.revise(regions=regions)
            argv = ["load", "--commit", "--accept-reissue", Y,
                    "--acknowledge", Y]
            # against la_pfa_mapping the file misses a force: halts
            rc, text, _ = self.run_main(cur, argv)
            self.assertEqual(rc, "halt", text)
            self.assertIn("Suffolk", text)
            # were the mapping to lose it too, the tip still holds it: a
            # partial file never replaces a fuller edition, nothing releases
            self.names = PFAS - {"Suffolk"}
            rc, text, logged = self.run_main(cur, argv)
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{Y}: REJECTED", text)
            self.assertIn("Suffolk", text)
            self.assertEqual(editions(cur), [(1, None, N)])

    def test_a_new_year_breaking_a_stop_condition_is_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, year=2025, modified="2025-07-11T13:57:05Z")
            big = {n: {"cases_discussed": 1000} for n in PFAS}
            self.file(values=big)
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{Y}: REJECTED", text)
            self.assertIn("England", text)
            self.assertEqual(editions(cur), [])
            self.assertEqual(editions(cur, Y0), [(1, None, N)])
            self.assertIn(f"rejected ['{Y}']", logged.call_args[0][2])
            text, _ = self.ok(cur, ["load", "--commit", "--acknowledge", Y])
            self.assertEqual(editions(cur), [(1, None, N)])

    def test_a_second_year_is_new_and_both_are_held(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, year=2025, modified="2025-07-11T13:57:05Z")
            self.file()
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{Y}: new", text)
            self.assertIn(f"{Y0}: unchanged: this file is already in the "
                          "ledger", text)
            self.assertEqual(count(cur, "zz_s17_live"), 2 * N)
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            text, _ = self.ok(cur, ["load", "--year", Y0, "--recheck", Y0])
            self.assertIn(f"{Y0}: unchanged", text)
            self.assertNotIn(f"{Y}: unchanged", text)

    def test_a_stranded_year_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("DELETE FROM public.zz_s17_live")
            self.assertEqual(pe.status(cur, m.profile())["live_missing"], [Y])
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn("live-missing", text)
            self.assertEqual(count(cur, "zz_s17_live"), N)
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.file()
            final = "https://safelives.org.uk/wp-content/uploads/2026/07/" \
                    "MARAC-DATA-2025-2026.xlsx"
            self.final[self.last_url] = final
            self.ok(cur, ["load", "--commit"])
            (_, src, _, _), = ledger(cur)
            self.assertTrue(src.startswith(final + " (SafeLives"))

    def test_file_paths(self):
        with rolled_back(self.conn) as cur:
            path = self.file(register=False)
            # the page is read and must link the file's year
            rc, text, _ = self.run_main(cur, ["load", "--file", path])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--no-page", text)
            text, logged = self.ok(cur, ["load", "--file", path, "--no-page",
                                         "--commit"])
            self.assertIn("MANUAL INPUT", text)
            self.assertIn(f"{Y}: new", text)
            self.assertIn("--no-page", logged.call_args[0][2])
            rc, text, _ = self.run_main(cur, ["load", "--no-page"])
            self.assertEqual(rc, "halt", text)
            outside = write_marac(Path(tempfile.mkdtemp()) / "x.xlsx")
            try:
                rc, text, _ = self.run_main(cur, ["load", "--file", outside,
                                                  "--no-page"])
            finally:
                outside.unlink()
            self.assertEqual(rc, "halt", text)
            self.assertIn("not under an allowed root", text)
            rc, text, _ = self.run_main(cur, ["load", "--file", path,
                                              "--no-page", "--year", Y0])
            self.assertEqual(rc, "halt", text)

    def test_discovery_fails_loudly(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.links.clear()
            self.file(year=2025, modified="2025-07-11T13:57:05Z")
            rc, text, logged = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn(Y, text)
            self.assertIn(LINK.format(2024, 2025), text)
            self.page_refused = True
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("SafeLives refused the request", text)
            self.assertIn("--file", text)

    def test_identity_halts(self):
        with rolled_back(self.conn) as cur:
            self.file(notes_year=2025)
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Notes", text)
            self.file(england_delta=1)
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("reconciliation", text)
            # the link's years against the file's own titles
            path = self.file(year=2025, register=False)
            self.urls[LINK.format(2025, 2026)] = path
            self.links[Y] = LINK.format(2025, 2026)
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertEqual(count(cur, "zz_s17_editions"), 0)

    def test_the_same_name_download_rule_through_load(self):
        with rolled_back(self.conn) as cur:
            first = self.file()
            second = self.file(modified="2026-09-01T10:00:00Z",
                               values={"Cumbria": {"cases_discussed": 120.5}})
            bodies = [first.read_bytes(), second.read_bytes()]
            dl = self.root / "raw"

            class S:
                def get(self_, url, **kw):
                    return Resp(bodies[0], url=url)

            borrowed = _Borrowed(cur)
            for body in bodies:
                bodies[0] = body
                with specs(self.root), \
                        mock.patch.object(m, "_conn", return_value=borrowed), \
                        mock.patch.object(m, "RAW_DIR", dl), \
                        mock.patch.object(m, "_session", return_value=S()), \
                        mock.patch.object(m, "fetch_page", return_value=(
                            page_html([self.last_url]), m.DATA_PAGE)), \
                        mock.patch.object(m, "log_run"), quiet():
                    try:
                        m.main(["load"])
                    except SystemExit:
                        pass
            borrowed.tx.done()
            held = sorted(p.name for p in dl.glob("*.xlsx"))
            self.assertEqual(len(held), 2)
            self.assertIn("MARAC-DATA-2025-2026.xlsx", held)
            self.assertEqual((dl / "MARAC-DATA-2025-2026.xlsx").read_bytes(),
                             first.read_bytes())

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.revise(values={"Cumbria": {"cases_discussed": 120.5}})
            self.ok(cur, ["load", "--commit", "--accept-reissue", Y,
                          "--acknowledge", Y])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "Cumbria"), Decimal("120.50"))
            text, logged = self.ok(cur, ["restore-edition", Y, "1",
                                         "--commit"])
            self.assertIn("as edition 3", text)
            self.assertEqual(editions(cur)[-1], (3, 2, N))
            logged.assert_called_once()
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "Cumbria"), Decimal("100.50"))
            rc, text, _ = self.run_main(cur, ["restore-edition", Y, "3"])
            self.assertEqual(rc, "halt")
            self.assertIn("is the tip", text)


class Migrate(Fixture):
    """Held-style live rows built from a fixture file the way the n8n load
    stored them: published zeros as NULL (parseFloat(x) || null) and no
    row for the West Midlands force; loaded_at set."""

    def seed_legacy(self, cur, year=2026, *, defects=True, **kw):
        path = self.file(year=year, register=False, **kw)
        wb = m.read_workbook(path)
        label = wb["year"]
        recs = m.records(wb, label, PFAS)
        rows = []
        for r in recs:
            if defects and r["pfa_name_safelives"] == "West Midlands":
                continue
            r = dict(r)
            if defects:
                for c in m.VALUES:
                    if r[c] == 0:
                        r[c] = None
            r["live_source"] = f"SafeLives MARAC annual data {label}"
            rows.append(r)
        m.insert_live(cur, m.profile(), label, rows)
        cur.execute("UPDATE public.zz_s17_live SET loaded_at = "
                    "'2026-03-30 22:22:38.090587+00' WHERE financial_year "
                    "= %s", (label,))
        url = LINK.format(year - 1, year)
        files = {label: {"path": path, "sha256": m.content_sha256(path),
                         "url": url}}
        return path, files, label

    def migrate(self, cur, files, *, write=True, explained=None, **kw):
        with quiet() as out:
            plan = m.migrate_legacy(
                cur, write=write, legacy=kw.get("legacy") or m.live_state(cur),
                files=files, mapping=m.mapping_state(cur),
                explained=({"West Midlands": "test"} if explained is None
                           else explained))
        return plan, out.getvalue()

    def test_proof_lists_the_groups_and_stores_edition_1(self):
        with rolled_back(self.conn) as cur:
            path, files, label = self.seed_legacy(cur)
            plan, text = self.migrate(cur, files)
            self.assertIn("restored", text)
            self.assertIn("City of London housing_referrals", text)
            self.assertIn("Lancashire housing_referrals", text)
            self.assertNotIn("LANCASHIRE", text)
            self.assertIn("West Midlands", text)
            # edition 1 is live as held: no West Midlands row
            self.assertEqual(editions(cur), [(1, None, N - 1)])
            self.assertEqual(ed_val(cur, "West Midlands", "marac_count", 1),
                             "absent")
            cur.execute("SELECT DISTINCT release_label, source_file FROM "
                        "public.zz_s17_editions")
            (lb, sf), = cur.fetchall()
            self.assertEqual(lb, core.AS_LOADED_LABEL)
            self.assertTrue(sf.startswith("as loaded"))
            self.assertIsNone(ed_val(cur, "Lancashire", "value_flag", 1))
            self.assertIsNone(ed_val(cur, "City of London", "value_flag", 1))
            self.assertEqual(ledger(cur), [])      # the file differs
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            # the load stores the file as edition 2: the restored zero is a
            # named flip, West Midlands a key added
            self.urls[LINK.format(2025, 2026)] = path
            self.links[label] = LINK.format(2025, 2026)
            rc, text, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("West Midlands", text)
            entry = {"decided": "test", "why": "test", "periods": {label: {
                "City of London/housing_referrals": (None, "0.00"),
                "Lancashire/housing_referrals": (None, "0.00")}}}
            with mock.patch.dict(m.ACKNOWLEDGED_FLIPS, {"t": entry}):
                text, _ = self.ok(cur, ["load", "--commit", "--acknowledge",
                                        label, "--acknowledge-flips", "t"])
            self.assertIn(f"{label}: revised", text)
            self.assertEqual(editions(cur), [(1, None, N - 1), (2, 1, N)])
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("not named", text)
            self.ok(cur, ["refresh-latest", "--commit",
                          "--accept-key-changes", label])
            self.assertEqual(count(cur, "zz_s17_live"), N)
            self.assertEqual(live_val(cur, "City of London",
                                      "housing_referrals"), Decimal("0.00"))
            self.assertNotEqual(live_val(cur, "West Midlands"), "absent")
            self.assertEqual(ed_val(cur, "West Midlands", "marac_count", 2), 4)
            self.assertEqual(live_val(cur, "Lancashire", "housing_referrals"),
                             Decimal("0.00"))
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            text, _ = self.ok(cur, ["load", "--recheck", label])
            self.assertIn(f"{label}: unchanged", text)

    def test_a_faithful_load_rereads_unchanged_against_edition_1(self):
        with rolled_back(self.conn) as cur:
            nd = {c: "No Data" for c in m.CASES_COLUMNS
                  + ("housing_referrals",)}
            path, files, label = self.seed_legacy(cur, defects=False,
                                                  values={"Norfolk": nd})
            plan, text = self.migrate(cur, files, explained={})
            self.assertIn("0 differences", text)
            self.assertEqual(ed_val(cur, "Norfolk", "value_flag", 1),
                             "not_submitted")
            self.assertEqual(len(ledger(cur)), 1)
            self.urls[LINK.format(2025, 2026)] = path
            self.links[label] = LINK.format(2025, 2026)
            text, _ = self.ok(cur, ["load"])
            self.assertIn("already in the ledger", text)
            for argv in (["load", "--recheck", label],
                         ["load", "--file", path, "--no-page"],
                         ["load", "--file", path, "--recheck", label,
                          "--commit"]):
                with self.subTest(argv=argv):
                    text, _ = self.ok(cur, argv)
                    self.assertIn(f"{label}: unchanged", text)
            self.assertEqual(editions(cur), [(1, None, N)])

    def test_a_planted_held_difference_stops_and_rolls_back(self):
        with rolled_back(self.conn) as cur:
            path, files, label = self.seed_legacy(cur)
            cur.execute("UPDATE public.zz_s17_live SET cases_discussed = "
                        "cases_discussed + 1 WHERE pfa_name_safelives = "
                        "'Suffolk'")
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, files)
            self.assertIn("proof failed", str(e.exception.code))
            self.assertIn("Suffolk", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s17_editions"), 0)

    def test_an_unexplained_key_only_in_the_file_stops(self):
        with rolled_back(self.conn) as cur:
            path, files, label = self.seed_legacy(cur)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, files, explained={})
            self.assertIn("West Midlands", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s17_editions"), 0)

    def test_a_wrong_file_sha_or_live_state_stops(self):
        with rolled_back(self.conn) as cur:
            path, files, label = self.seed_legacy(cur)
            bad = {label: dict(files[label], sha256="0" * 64)}
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, bad, write=False)
            self.assertIn("expected 00000000", str(e.exception.code))
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, files, write=False,
                             legacy=(N, "x", 1, ((label, N),)))
            self.assertIn("not as surveyed", str(e.exception.code))

    def test_a_non_empty_editions_table_stops(self):
        with rolled_back(self.conn) as cur:
            path, files, label = self.seed_legacy(cur)
            legacy = m.live_state(cur)
            self.migrate(cur, files, legacy=legacy)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, files, legacy=legacy)
            self.assertIn("runs once", str(e.exception.code))

    def test_the_command_previews_then_commits(self):
        with rolled_back(self.conn) as cur:
            path, files, label = self.seed_legacy(cur)
            with mock.patch.object(m, "LEGACY_LIVE", m.live_state(cur)), \
                    mock.patch.object(m, "LEGACY_FILES", files), \
                    mock.patch.object(m, "LEGACY_PFA_MAPPING",
                                      m.mapping_state(cur)), \
                    mock.patch.object(m, "LEGACY_EXPLAINED_KEYS",
                                      {"West Midlands": "test"}):
                text, logged = self.ok(cur, ["migrate-legacy"])
                self.assertIn("PREVIEW", text)
                self.assertEqual(count(cur, "zz_s17_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, ["migrate-legacy", "--commit"])
            self.assertEqual(editions(cur), [(1, None, N - 1)])
            logged.assert_called_once()


class Commands(Fixture):

    def test_status_without_editions_table_is_clean(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1)
            self.assertIn("not present yet", text)
            self.assertFalse(pe.table_exists(cur, "zz_s17_editions"))

    def test_ddl_preview_then_commit_then_status(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["ddl"])
            self.assertEqual(rc, 0, text)
            self.assertIn("DRY RUN", text)
            self.assertFalse(pe.table_exists(cur, "zz_s17_editions"))
            self.ok(cur, ["ddl", "--commit"])
            for t in ("zz_s17_editions", LEDGER):
                self.assertTrue(pe.table_exists(cur, t), t)

    def test_the_live_types_are_checked(self):
        with rolled_back(self.conn, ddl=False) as cur:
            self.assertEqual(m.live_type_problems(cur), [])
            cur.execute("ALTER TABLE public.zz_s17_live ALTER COLUMN "
                        "repeat_cases_pct TYPE numeric(8,2)")
            self.assertTrue(m.live_type_problems(cur))

    def test_load_preview_before_ddl_compares_with_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            self.file()
            text, _ = self.ok(cur, ["load"])
            self.assertIn("LIVE table", text)
            self.assertIn(f"{Y}: new", text)
            rc, text, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt")
            self.assertIn("ddl --commit", text)

    def test_stray_acknowledgements_halt(self):
        with rolled_back(self.conn) as cur:
            self.file()
            rc, text, _ = self.run_main(cur, ["load", "--acknowledge",
                                              "2019-20"])
            self.assertEqual(rc, "halt", text)
            rc, text, _ = self.run_main(cur, ["load", "--acknowledge-flips",
                                              "anything"])
            self.assertEqual(rc, "halt", text)


if __name__ == "__main__":
    unittest.main()
