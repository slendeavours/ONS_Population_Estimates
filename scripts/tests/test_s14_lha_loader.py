"""Tests for the S14 loader in s14_lha_editions (the commands, migrate-legacy,
restore-edition, check-mapping) on throwaway tables.

No network: the GOV.UK replies and the downloads are stubbed, and every CSV
and GML file is written by the test (writers from test_s14_lha_pure).
Database tests run on a connection from _db.get_conn() inside a transaction
that is always rolled back (rolled_back below); nothing here commits. The
commands get a stand-in connection whose commit and rollback act on a
savepoint of that transaction, and a cursor proxy whose .connection does
the same, so the engine's per-period commit never reaches the real
connection. The only tables written are zz_s14_rates_live and
zz_s14_map_live (LIKE copies of brma_lha_rates and la_brma_mapping),
zz_s14_rates_editions, zz_s14_map_editions and their ledgers, created
inside that transaction (refused if any already exists), so they never
persist. la_boundaries is replaced by the fixture's points and the run log
is stubbed; no real table is read for a value or written. No retired
script is imported or run.
"""
import contextlib
import dataclasses
import io
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s14_lha_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s14_lha_pure import (FY, POINTS, RATES, YEARS, _Session,  # noqa: E402
                               boundaries, collection, page, polygons_for,
                               write_csv, write_gml)

ZR = dataclasses.replace(
    m.RATES, name="zz_s14_rates", live_table="zz_s14_rates_live",
    editions_table="zz_s14_rates_editions",
    expected_rows_per_period=m.tip_row_count("zz_s14_rates_editions",
                                             m.FY_COL))
ZM = dataclasses.replace(
    m.MAPPING, name="zz_s14_map", live_table="zz_s14_map_live",
    editions_table="zz_s14_map_editions", fk_la_boundaries=False)
RL = "zz_s14_rates_editions_file_checks"
ML = "zz_s14_map_editions_file_checks"
TABLES = ("zz_s14_rates_live", "zz_s14_map_live", "zz_s14_rates_editions",
          "zz_s14_map_editions", RL, ML)
N = len(RATES)
NA = len(POINTS)
V = m.LIVE_VINTAGE
TS = "2026-04-08T15:31:30Z"
TS_DT = datetime(2026, 4, 8, 15, 31, 30, tzinfo=timezone.utc)
ALIASES = {"Hull & East Riding": "Hull and East Riding",
           "Medway & Swale": "Medway and Swale"}
_STATE = m.boundaries_state      # unpatched, for the fixture's vintage


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    SP = "zz_s14_cmd"

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
def specs(gml_sha=None, extra_vintages=None):
    """The throwaway specs, the fixture's la_boundaries and BRMA layer
    vintage, and the fixture's alias table."""
    rows = boundaries()
    vint = {V: dict(m.VINTAGES[V], boundaries=_STATE(rows),
                    gml_sha256=gml_sha or "none")}
    vint.update(extra_vintages or {})
    with mock.patch.dict(m.SPECS, {"rates": ZR, "mapping": ZM}), \
            mock.patch.object(m, "EXPECTED_AUTHORITIES", NA), \
            mock.patch.object(m, "EXPECTED_POLYGONS", NA - 1), \
            mock.patch.object(m, "VINTAGES", vint), \
            mock.patch.object(m, "_boundaries", return_value=rows), \
            mock.patch.object(m, "brma_aliases", return_value=dict(ALIASES)):
        yield


@contextmanager
def rolled_back(conn, *, ddl=True):
    cur = conn.cursor()
    try:
        cur.execute("SELECT " + ", ".join(f"to_regclass('public.{t}')"
                                          for t in TABLES))
        if any(cur.fetchone()):
            raise RuntimeError("a zz_s14 table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s14_rates_live (LIKE "
                    "public.brma_lha_rates INCLUDING ALL)")
        cur.execute("CREATE TABLE public.zz_s14_map_live (LIKE "
                    "public.la_brma_mapping INCLUDING ALL)")
        # the real table has carried the additive vintage column since the
        # first load of 2026-10-11 (the CHECK goes with the column); the
        # throwaway copy starts in the shape the loader's ddl has to alter
        cur.execute("ALTER TABLE public.zz_s14_map_live "
                    "DROP COLUMN IF EXISTS vintage")
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


def editions(cur, period=FY, table="zz_s14_rates_editions",
             col="financial_year"):
    cur.execute(f"SELECT edition, supersedes, COUNT(*) FROM public.{table} "
                f"WHERE {col} = %s GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur, table=RL, col="financial_year"):
    cur.execute(f"SELECT {col}, source_file, outcome, edition FROM "
                f"public.{table} ORDER BY id")
    return cur.fetchall()


def rate(cur, brma="Ashford", col="sar_monthly", period=FY):
    cur.execute(f"SELECT {col} FROM public.zz_s14_rates_live WHERE "
                "brma_name = %s AND financial_year = %s", (brma, period))
    r = cur.fetchone()
    return r[0] if r else None


def mapped(cur, lad, col="brma_name"):
    cur.execute(f"SELECT {col} FROM public.zz_s14_map_live WHERE lad24cd = %s",
                (lad,))
    r = cur.fetchone()
    return r[0] if r else None


def bump(rates, name, i=0, by="1.00"):
    """RATES with one monthly amount of `name` raised by `by`."""
    vals = list(rates[name])
    v = Decimal(vals[i].replace(",", "")) + Decimal(by)
    vals[i] = f"{v:,.2f}"
    return dict(rates, **{name: tuple(vals)})


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
        self.docs = [("2026 to 2027", TS), ("2025 to 2026", "x"),
                     ("2014 to 2019", "x")]
        self.api = {}
        self.urls = {}
        self.final = {}
        self.fetched = []
        self.n = 0
        self.gml = write_gml(self.root / "gml" / "GML.zip")
        self.gml_sha = m.content_sha256(self.gml)

    def publish(self, years=YEARS, rates=None, history=None, media=None,
                **kw):
        """Write a CSV, list its year page in the collection, register the
        page and its attachment's download."""
        self.n += 1
        fn = years.replace(" ", "-")
        path = write_csv(self.root / f"t{self.n}" / f"england-rates-{fn}.csv",
                         years, rates, **kw)
        if not any(y == years for y, _ in self.docs):
            self.docs.insert(0, (years, "x"))
        pg = page(years, history=history)
        if media:
            att = pg["details"]["attachments"][0]
            att["url"] = att["url"].replace("/abc123/", f"/{media}/")
        url = pg["details"]["attachments"][0]["url"]
        self.api[pg["base_path"]] = pg
        self.urls[url] = path
        self.last_url = url
        return path

    def local(self, years=YEARS, rates=None, **kw):
        self.n += 1
        fn = years.replace(" ", "-")
        return write_csv(self.root / f"t{self.n}" / f"england-rates-{fn}.csv",
                         years, rates, **kw)

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, log)."""
        borrowed = _Borrowed(cur)
        self.api[m.COLLECTION_PATH] = collection(self.docs)

        def fetch(url, dest, session=None):
            self.fetched.append(url)
            return self.urls[url], self.final.get(url, url)

        out = io.StringIO()
        with specs(self.gml_sha), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda p, s=None: self.api[p]), \
                mock.patch.object(m, "fetch", side_effect=fetch), \
                mock.patch.object(m, "GML_FILE", self.gml), \
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
        """Load the 2026-27 rates and the crosswalk through the loader."""
        self.publish(history=[{"note": "x", "public_timestamp": TS}], **kw)
        self.ok(cur, ["load", "--commit"])
        self.fetched.clear()


# ---------------------------------------------------------------------------
# load: rates
# ---------------------------------------------------------------------------

class Rates(Fixture):

    def test_new_year_edition_live_and_one_ledger_row(self):
        with rolled_back(self.conn) as cur:
            self.publish()
            text, logged = self.ok(cur, ["load", "--only", "rates",
                                         "--commit"])
            self.assertIn(f"{FY}: new", text)
            self.assertIn("change_history 2026-04-08T15:31:30Z", text)
            self.assertIn(self.last_url, text)
            self.assertIn("ignored", text)            # the 2014 to 2019 page
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s14_rates_live"), N)
            self.assertEqual(core.rows_differing(cur, ZR, FY, 1), 0)
            (p, src, outcome, ed), = ledger(cur)
            self.assertEqual((p, outcome, ed), (FY, "new", 1))
            self.assertEqual(src, f"{self.last_url} (financial year {FY}; "
                                  f"page change {TS})")
            cur.execute("SELECT DISTINCT source_file, published_date, "
                        "release_label FROM public.zz_s14_rates_editions")
            (sf, pd_, label), = cur.fetchall()
            self.assertEqual(m.release_rank(sf), (FY, TS_DT))
            self.assertEqual(pd_, TS_DT.date())
            self.assertIn("weekly derived", label)
            self.assertEqual(rate(cur, "Ashford", "three_bed_monthly"),
                             Decimal("1097.80"))
            self.assertEqual(rate(cur, "Ashford", "three_bed_weekly"),
                             Decimal("253.34"))
            cur.execute("SELECT DISTINCT source FROM public.zz_s14_rates_live")
            self.assertEqual(cur.fetchall(), [(
                f"DWP UC LHA rates {FY}, https://www.gov.uk/government/"
                "publications/universal-credit-local-housing-allowance-"
                "rates-2026-to-2027",)])
            logged.assert_called_once()
            self.assertIn(f"{FY} new", logged.call_args[0][2])
            self.assertTrue(pe.status(cur, m.profile("rates"))["ok"])

    def test_a_failing_ledger_insert_rolls_back_everything(self):
        with rolled_back(self.conn) as cur:
            self.publish()
            with mock.patch.object(pe, "record_file_check",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(cur, ["load", "--only",
                                                       "rates", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{FY}: FAILED", text)
            self.assertEqual(editions(cur), [])
            self.assertEqual(count(cur, "zz_s14_rates_live"), 0)
            self.assertEqual(ledger(cur), [])
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN", notes)
            self.assertIn(f"failed ['{FY}']", notes)

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            cur.execute("SELECT COUNT(*) FROM pipeline_run_log")
            runs = cur.fetchone()[0]
            self.publish()
            text, logged = self.ok(cur, ["load"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            for t in TABLES:
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--simulate"])
            self.assertIn("SIMULATION", text)
            for t in TABLES:
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            self.ok(cur, ["load", "--commit"])
            calls = []
            real = m.read_csv
            with mock.patch.object(m, "read_csv", side_effect=lambda p:
                                   calls.append(p) or real(p)):
                text, logged = self.ok(cur, ["load", "--commit"])
            self.assertEqual(calls, [])
            self.assertIn("nothing parsed", text)
            self.assertIn(f"{V}: skipped (checked", text)
            logged.assert_not_called()
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 1)
            self.assertEqual(len(ledger(cur, ML, "vintage")), 1)
            cur.execute("SELECT COUNT(*) FROM pipeline_run_log")
            self.assertEqual(cur.fetchone()[0], runs)

    def test_a_reissue_equal_is_unchanged(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.publish(media="def456",
                         history=[{"note": "Reissued, no change",
                                   "public_timestamp": "2026-05-01T10:00:00Z"},
                                  {"note": "x", "public_timestamp": TS}])
            text, logged = self.ok(cur, ["load", "--only", "rates",
                                         "--commit"])
            self.assertIn(f"{FY}: unchanged", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual([o for _, _, o, _ in ledger(cur)],
                             ["new", "unchanged"])

    def test_a_reissue_that_differs_needs_acknowledge(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = rate(cur)
            self.publish(rates=bump(RATES, "Ashford"),
                         history=[{"note": "Corrected Ashford",
                                   "public_timestamp": "2026-05-01T10:00:00Z"}])
            rc, text, logged = self.run_main(cur, ["load", "--only", "rates",
                                                   "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{FY}: REJECTED", text)
            self.assertIn(f"--acknowledge {FY}", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertIn("PARTIAL RUN (exit 1)", logged.call_args[0][2])
            text, logged = self.ok(cur, ["load", "--only", "rates",
                                         "--acknowledge", FY, "--commit"])
            self.assertIn(f"{FY}: revised", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertEqual(rate(cur), before)        # live waits
            self.assertIn(f"ACKNOWLEDGED", logged.call_args[0][2])
            self.assertEqual(pe.status(cur, m.profile("rates"))
                             ["pending_refresh"], [FY])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rate(cur), before + 1)
            self.assertEqual(rate(cur, col="sar_weekly"),
                             m.weekly(before + 1))
            cur.execute("SELECT DISTINCT loaded_at FROM "
                        "public.zz_s14_rates_editions WHERE edition = 2")
            ed_loaded = cur.fetchone()[0]
            self.assertEqual(rate(cur, col="loaded_at"), ed_loaded)
            self.assertTrue(pe.status(cur, m.profile("rates"))["ok"])

    def test_equal_rank_different_content_stops_unless_accepted(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.publish(rates=bump(RATES, "Ashford"),
                         history=[{"note": "x", "public_timestamp": TS}])
            rc, text, logged = self.run_main(cur, ["load", "--only", "rates",
                                                   "--acknowledge", FY,
                                                   "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("two files claim the same release", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            text, logged = self.ok(cur, ["load", "--only", "rates",
                                         "--acknowledge", FY,
                                         "--accept-reissue", FY, "--commit"])
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertIn("--accept-reissue", logged.call_args[0][2])

    def test_file_no_page_different_content_needs_accept_reissue(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            path = self.local(rates=bump(RATES, "Kernow West", 2))
            rc, text, _ = self.run_main(cur, ["load", "--only", "rates",
                                              "--file", path, "--no-page",
                                              "--acknowledge", FY])
            self.assertEqual(rc, 1, text)
            self.assertIn("no within-year rank", text)
            self.assertIn(f"--accept-reissue {FY}", text)
            text, _ = self.ok(cur, ["load", "--only", "rates", "--file", path,
                                    "--no-page", "--acknowledge", FY,
                                    "--accept-reissue", FY, "--commit"])
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])

    def older(self, cur, how):
        """Seed from the page of TS, then offer an older file with a
        different value: on the page and by --release, the same year's page
        with an earlier change; by --file, the 2025-26 year."""
        self.seed(cur)
        argv = ["load", "--only", "rates", "--commit"]
        if how == "file":
            path = self.local("2025 to 2026")
            return argv + ["--file", path, "--no-page"]
        self.publish(rates=bump(RATES, "Ashford"),
                     history=[{"note": "x",
                               "public_timestamp": "2026-01-30T09:30:00Z"}])
        if how == "release":
            argv += ["--release", FY]
        return argv

    def test_an_older_file_is_skipped_and_halts_on_every_path(self):
        for how in ("page", "release", "file"):
            with self.subTest(how=how), rolled_back(self.conn) as cur:
                rc, text, logged = self.run_main(cur, self.older(cur, how))
                self.assertEqual(rc, "halt", text)
                self.assertIn("older file", text)
                self.assertIn("skipped (older", text)
                self.assertEqual(editions(cur), [(1, None, N)])
                self.assertEqual(len(ledger(cur)), 1)
                logged.assert_not_called()

    def test_allow_older_file_overrides_and_is_logged(self):
        with rolled_back(self.conn) as cur:
            argv = self.older(cur, "page") + ["--allow-older-file",
                                              "--acknowledge", FY]
            text, logged = self.ok(cur, argv)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertIn("--allow-older-file given", logged.call_args[0][2])
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s14_rates_editions WHERE edition = 2")
            self.assertIn("--allow-older-file given", cur.fetchone()[0])

    def test_a_header_year_differing_from_the_page(self):
        with rolled_back(self.conn) as cur:
            bad = ["BRMA", "Monthly UC LHA rates 2024 to 2025 - SAR",
                   f"1 Bed {YEARS}", f"2 Bed {YEARS}", f"3 Bed {YEARS}",
                   f"4 Bed {YEARS}"]
            self.publish(header=bad)
            rc, text, logged = self.run_main(cur, ["load", "--only", "rates",
                                                   "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("names 2024-25, not 2026-27", text)
            self.assertIn(f"--accept-header-year {FY}", text)
            self.assertEqual(editions(cur), [])
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--only", "rates",
                                         "--accept-header-year", FY,
                                         "--commit"])
            self.assertIn("ACCEPTED HEADER YEAR", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertIn("--accept-header-year", logged.call_args[0][2])
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s14_rates_editions")
            self.assertIn("--accept-header-year", cur.fetchone()[0])
            # naming it when the header agrees is refused
            self.publish()
            rc, text, _ = self.run_main(cur, ["load", "--only", "rates",
                                              "--recheck", FY,
                                              "--accept-header-year", FY])
            self.assertEqual(rc, "halt", text)
            self.assertIn("nothing to accept", text)

    def test_the_older_layout_and_an_ampersand_name_halt(self):
        with rolled_back(self.conn) as cur:
            self.publish(old_layout=True)
            rc, text, _ = self.run_main(cur, ["load", "--only", "rates"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("older layout", text)
            rates = dict(RATES)
            rates["Medway & Swale"] = rates.pop("Medway and Swale")
            self.publish(rates=rates)
            rc, text, _ = self.run_main(cur, ["load", "--only", "rates"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Medway & Swale", text)
            self.assertEqual(count(cur, "zz_s14_rates_editions"), 0)

    def test_a_blank_amount_halts_and_nothing_is_stored(self):
        with rolled_back(self.conn) as cur:
            self.publish(rates=dict(RATES, **{"Kernow West": (
                "392.50", "", "695.00", "815.00", "1,100.00")}))
            rc, text, logged = self.run_main(cur, ["load", "--only", "rates",
                                                   "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("blank", text)
            self.assertIn("Kernow West", text)
            self.assertEqual(count(cur, "zz_s14_rates_editions"), 0)
            logged.assert_not_called()

    def test_a_new_year_breaking_a_stop_condition_is_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.publish("2027 to 2028", rates=bump(RATES, "Ashford",
                                                    by="200.00"))
            rc, text, logged = self.run_main(cur, ["load", "--only", "rates",
                                                   "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("NEW RELEASE", text)
            self.assertIn("2027-28: REJECTED", text)
            self.assertIn("Ashford", text)
            self.assertEqual(editions(cur, "2027-28"), [])
            self.assertNotIn("2027-28", [p for p, _, _, _ in ledger(cur)])
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN (exit 1)", notes)
            self.assertIn("rejected ['2027-28'", notes)
            rc, text, logged = self.run_main(cur, ["load", "--only", "rates"])
            self.assertEqual(rc, 1, text)
            logged.assert_not_called()
            text, _ = self.ok(cur, ["load", "--only", "rates", "--acknowledge",
                                    "2027-28", "--commit"])
            self.assertIn("2027-28: new", text)
            self.assertEqual(count(cur, "zz_s14_rates_live",
                                   "WHERE financial_year = '2027-28'"), N)

    def test_frozen_rates_in_a_new_year_are_normal(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.publish("2027 to 2028")
            text, _ = self.ok(cur, ["load", "--only", "rates", "--commit"])
            self.assertIn("2027-28: new", text)

    def test_a_brma_set_change_needs_accept_key_changes(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            rates = dict(RATES, **{"Bath": ("400.00",) * 5})
            self.publish("2027 to 2028", rates=rates)
            rc, text, _ = self.run_main(cur, ["load", "--only", "rates",
                                              "--acknowledge", "2027-28"])
            self.assertEqual(rc, 1, text)
            self.assertIn(m.KEYS, text)
            self.assertIn("--accept-key-changes 2027-28", text)
            text, logged = self.ok(cur, ["load", "--only", "rates",
                                         "--accept-key-changes", "2027-28",
                                         "--commit"])
            self.assertIn("ACCEPTED KEY CHANGES", text)
            self.assertIn("--accept-key-changes", logged.call_args[0][2])

    def test_a_new_year_missing_a_crosswalk_name_is_a_join_stop(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            rates = dict(RATES)
            rates.pop("Kernow West")
            self.publish("2027 to 2028", rates=rates)
            rc, text, _ = self.run_main(cur, ["load", "--only", "rates",
                                              "--accept-key-changes",
                                              "2027-28", "--acknowledge",
                                              "2027-28", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(m.JOIN, text)
            self.assertIn("Kernow West", text)
            self.assertEqual(editions(cur, "2027-28"), [])

    def test_an_acknowledgement_must_name_a_compared_period(self):
        with rolled_back(self.conn) as cur:
            self.publish()
            for flag in ("--acknowledge", "--accept-reissue",
                         "--accept-key-changes"):
                rc, text, logged = self.run_main(cur, ["load", "--commit",
                                                       flag, "2019-20"])
                self.assertEqual(rc, "halt", text)
                self.assertIn("2019-20", text)
                self.assertEqual(count(cur, "zz_s14_rates_editions"), 0)
                logged.assert_not_called()

    def test_a_stranded_period_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("DELETE FROM public.zz_s14_rates_live")
            self.assertEqual(pe.status(cur, m.profile("rates"))
                             ["live_missing"], [FY])
            text, _ = self.ok(cur, ["load", "--only", "rates", "--commit"])
            self.assertIn("live-missing", text)
            self.assertEqual(count(cur, "zz_s14_rates_live"), N)
            self.assertTrue(pe.status(cur, m.profile("rates"))["ok"])

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.publish()
            final = "https://assets.example/media/9/england-rates-2026-to-2027.csv"
            self.final[self.last_url] = final
            text, _ = self.ok(cur, ["load", "--only", "rates", "--commit"])
            self.assertIn(final, text)
            (_, src, _, _), = ledger(cur)
            self.assertTrue(src.startswith(final + " (financial year"))

    def test_same_name_download_with_different_content_is_kept_beside(self):
        with rolled_back(self.conn) as cur:
            held = self.local()
            new = self.local(rates=bump(RATES, "Ashford"))
            raw = self.root / "raw"
            raw.mkdir()
            (raw / held.name).write_bytes(held.read_bytes())
            self.publish()
            url = self.last_url
            self.api[m.COLLECTION_PATH] = collection(self.docs)
            borrowed = _Borrowed(cur)
            out = io.StringIO()
            with specs(self.gml_sha), \
                    mock.patch.object(m, "_conn", return_value=borrowed), \
                    mock.patch.object(m, "fetch_json", side_effect=lambda p,
                                      s=None: self.api[p]), \
                    mock.patch.object(m, "RAW_DIR", raw), \
                    mock.patch.object(m, "_session", return_value=_Session(
                        new.read_bytes(), url)), \
                    mock.patch.object(m, "log_run"), \
                    contextlib.redirect_stdout(out):
                try:
                    rc = m.main(["load", "--only", "rates"])
                finally:
                    borrowed.tx.done()
            text = out.getvalue()
            self.assertEqual(rc, 0, text)
            self.assertEqual((raw / held.name).read_bytes(), held.read_bytes())
            beside = [p for p in raw.iterdir() if p.name != held.name]
            self.assertEqual(len(beside), 1)
            self.assertEqual(beside[0].read_bytes(), new.read_bytes())
            self.assertIn("kept it untouched", text)

    def test_file_with_page_takes_the_rank_only_for_the_current_file(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            same = self.local()
            text, _ = self.ok(cur, ["load", "--only", "rates", "--file", same])
            self.assertIn("equals the page's current CSV", text)
            self.assertIn(f"{FY}: unchanged", text)
            other = self.local(rates=bump(RATES, "Ashford"))
            rc, text, _ = self.run_main(cur, ["load", "--only", "rates",
                                              "--file", other,
                                              "--acknowledge", FY])
            self.assertEqual(rc, 1, text)
            self.assertIn("not the page's current CSV", text)
            self.assertIn("--accept-reissue", text)

    def test_discovery_fails_loudly(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["load", "--only", "rates"])
            self.assertIn("no newer page is listed", text)
            self.docs.append(("!Universal Credit Local Housing Allowance "
                              "rates 2027 to 2028 (draft)", "x"))
            rc, text, _ = self.run_main(cur, ["load", "--only", "rates"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2027 to 2028", text)
            self.docs.pop()
            pg = self.api[page()["base_path"]]
            pg["details"]["attachments"] = []
            rc, text, _ = self.run_main(cur, ["load", "--only", "rates",
                                              "--recheck", FY])
            self.assertEqual(rc, "halt", text)
            self.assertIn("0 attachments", text)

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            first = rate(cur)
            self.publish(rates=bump(RATES, "Ashford"), history=[
                {"note": "x", "public_timestamp": "2026-05-01T10:00:00Z"}])
            self.ok(cur, ["load", "--only", "rates", "--acknowledge", FY,
                          "--commit"])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rate(cur), first + 1)
            text, logged = self.ok(cur, ["restore-edition", "brma_lha_rates",
                                         FY, "1", "--commit"])
            self.assertIn("as edition 3", text)
            self.assertEqual(editions(cur)[-1], (3, 2, N))
            logged.assert_called_once()
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rate(cur), first)
            rc, text, _ = self.run_main(cur, ["restore-edition",
                                              "brma_lha_rates", FY, "3"])
            self.assertEqual(rc, "halt")
            self.assertIn("is the tip", text)


# ---------------------------------------------------------------------------
# load: the crosswalk
# ---------------------------------------------------------------------------

class Crosswalk(Fixture):

    def test_the_first_vintage_into_empty_live(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.assertEqual(editions(cur, V, "zz_s14_map_editions",
                                      "vintage"), [(1, None, NA)])
            self.assertEqual(count(cur, "zz_s14_map_live"), NA)
            self.assertEqual(mapped(cur, "E06000010"), "Hull and East Riding")
            self.assertEqual(mapped(cur, "E06000053"), "Kernow West")
            self.assertEqual(mapped(cur, "E06000053", "mapping_method"),
                             "nearest_fallback")
            self.assertEqual(mapped(cur, "E06000053", "vintage"), V)
            self.assertIsNone(mapped(cur, "E06000053", "brma_secondary"))
            self.assertEqual(mapped(cur, "E06000053", "source"),
                             m.MAPPING_SOURCE)
            self.assertTrue(pe.status(cur, m.profile("mapping"))["ok"])

    def test_another_vintage_is_held_not_live_and_never_promoted(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            other = "brma-2027-05_lad-2024-05"
            gml2 = write_gml(self.root / "gml2" / "GML.zip",
                             polygons_for()[::-1])
            borrowed = _Borrowed(cur)
            extra = {other: dict(m.VINTAGES[V], gml_sha256=m.content_sha256(
                gml2), boundaries=_STATE(boundaries()))}
            out = io.StringIO()
            with specs(self.gml_sha, extra), \
                    mock.patch.object(m, "_conn", return_value=borrowed), \
                    mock.patch.object(m, "log_run"), \
                    contextlib.redirect_stdout(out):
                try:
                    rc = m.main(["load", "--only", "mapping", "--gml",
                                 str(gml2), "--commit"])
                    rc2 = m.main(["refresh-latest", "--commit"])
                    rc3 = m.main(["status"])
                finally:
                    borrowed.tx.done()
            text = out.getvalue()
            self.assertEqual((rc, rc2, rc3), (0, 0, 0), text)
            self.assertIn("held-not-live", text)
            self.assertIn(f"held, not live: {other}", text)
            self.assertEqual(editions(cur, other, "zz_s14_map_editions",
                                      "vintage"), [(1, None, NA)])
            cur.execute("SELECT DISTINCT vintage FROM public.zz_s14_map_live")
            self.assertEqual(cur.fetchall(), [(V,)])

    def test_a_revised_crosswalk_needs_acknowledge_and_names_a_correction(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            # the held edition with 'NaN' as the old build wrote it
            cur.execute("UPDATE public.zz_s14_map_live SET mapping_method = "
                        "'NaN' WHERE mapping_method = 'centroid_spatial_join'")
            recs = m.records(cur, ZM, V)
            core.insert_edition(cur, ZM, recs, V, release_label="as loaded",
                                published_date=None, source_file="held",
                                source_sha256=m.mapping_content_sha(recs),
                                supersedes=1, strict=True)
            corr = dict(m.MAPPING_CORRECTIONS["s14-mapping-method-2026-10"],
                        count=NA - 1)
            with mock.patch.dict(m.MAPPING_CORRECTIONS,
                                 {"s14-mapping-method-2026-10": corr}):
                rc, text, logged = self.run_main(cur, ["load", "--only",
                                                       "mapping", "--recheck",
                                                       V, "--commit"])
                self.assertEqual(rc, 1, text)
                self.assertIn(f"{V}: REJECTED", text)
                self.assertIn("mapping_method", text)
                self.assertIn("s14-mapping-method-2026-10", text)
                text, logged = self.ok(cur, ["load", "--only", "mapping",
                                             "--recheck", V, "--acknowledge",
                                             V, "--commit"])
            self.assertIn(f"{V}: revised", text)
            self.assertEqual([e for e, _, _ in editions(
                cur, V, "zz_s14_map_editions", "vintage")], [1, 2, 3])
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s14_map_editions WHERE edition = 3")
            self.assertIn("named correction s14-mapping-method-2026-10",
                          cur.fetchone()[0])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn(f"{V}={NA - 1}", text)
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(mapped(cur, "E06000010", "mapping_method"),
                             "centroid_spatial_join")
            self.assertEqual(mapped(cur, "E06000010", "brma_name"),
                             "Hull and East Riding")

    def test_an_unknown_layer_or_boundaries_state_halts(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            gml2 = write_gml(self.root / "gml2" / "GML.zip",
                             polygons_for()[::-1])
            rc, text, _ = self.run_main(cur, ["load", "--only", "mapping",
                                              "--gml", gml2])
            self.assertEqual(rc, "halt", text)
            self.assertIn("not a known BRMA layer", text)
            with mock.patch.object(m, "boundaries_state",
                                   return_value=(NA, "x")):
                rc, text, _ = self.run_main(cur, ["load", "--only",
                                                  "mapping", "--recheck", V])
            self.assertEqual(rc, "halt", text)
            self.assertIn("new LAD vintage", text)

    def test_check_mapping_lists_the_differences(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["check-mapping"])
            self.assertIn(f"brma_name: reproduced for {NA} of {NA}", text)
            cur.execute("UPDATE public.zz_s14_map_live SET mapping_method = "
                        "'NaN' WHERE mapping_method = 'centroid_spatial_join'")
            text, _ = self.ok(cur, ["check-mapping"])
            self.assertIn("compared with edition 1", text)
            cur.execute("SELECT COUNT(*) FROM public.zz_s14_map_editions")
            n = cur.fetchone()[0]
            self.assertEqual(n, NA)


# ---------------------------------------------------------------------------
# migrate-legacy
# ---------------------------------------------------------------------------

class Migrate(Fixture):

    def seed_legacy(self, cur, *, nan=True):
        """Held-style live rows: the rates from a fixture CSV (one row with
        its own source text, two load times), the crosswalk from the
        fixture layer with 'NaN' where the centroid is within a polygon."""
        path = self.publish(history=[{"note": "x", "public_timestamp": TS}])
        f = m.read_csv(path)
        recs = m.build_rates(f, FY, {m.SOURCE_TEXT: "held source"})
        recs[0][m.SOURCE_TEXT] = "restored by hand"
        m.insert_live(cur, m.profile("rates"), FY, recs)
        cur.execute("UPDATE public.zz_s14_rates_live SET loaded_at = "
                    "'2026-08-20 20:23:00.558032+00'")
        cur.execute("UPDATE public.zz_s14_rates_live SET loaded_at = "
                    "'2026-08-20 23:40:31.748925+00' WHERE brma_name = %s",
                    (recs[0]["brma_name"],))
        g = m.read_gml(self.gml)
        maps = m.build_mapping(g, boundaries(), ALIASES, set(RATES))
        for r in maps:
            if nan and r["mapping_method"] == m.WITHIN:
                r["mapping_method"] = "NaN"
        m.insert_live(cur, m.profile("mapping"), V, maps)
        cur.execute("UPDATE public.zz_s14_map_live SET loaded_at = "
                    "'2026-08-20 20:23:00.558032+00'")
        rf = dict(m.LEGACY_RATES_FILE, sha256=m.content_sha256(path),
                  url=self.last_url, rank=(FY, TS_DT))
        rn, rh, rl = m.live_state(cur, ZR)
        mn, mh, ml = m.live_state(cur, ZM)
        legacy = {"rates": (rn, rh), "rates_loaded": rl,
                  "mapping": (mn, mh), "mapping_loaded": ml,
                  "rates_file": rf,
                  "method_form": ("NaN", m.WITHIN, NA - 1)}
        return path, legacy

    def migrate(self, cur, path, legacy, write=True):
        with quiet() as out, specs(self.gml_sha):
            plan = m.migrate_legacy(cur, write=write, rates_file=path,
                                    gml_file=self.gml, legacy=legacy)
        return plan, out.getvalue()

    def test_proof_passes_and_byte_identical_rereads_are_unchanged(self):
        with rolled_back(self.conn) as cur:
            path, legacy = self.seed_legacy(cur)
            plan, text = self.migrate(cur, path, legacy)
            self.assertIn("0 differences", text)
            self.assertIn(f"{NA - 1} rows hold 'NaN'", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(editions(cur, V, "zz_s14_map_editions",
                                      "vintage"), [(1, None, NA)])
            cur.execute("SELECT source_text FROM public.zz_s14_rates_editions"
                        " ORDER BY brma_name LIMIT 1")
            self.assertEqual(cur.fetchone()[0], "restored by hand")
            cur.execute("SELECT DISTINCT source_file, release_label, "
                        "published_date FROM public.zz_s14_rates_editions")
            (sf, label, pd_), = cur.fetchall()
            self.assertTrue(sf.startswith("as loaded: "))
            self.assertEqual(m.release_rank(sf), (FY, TS_DT))
            self.assertEqual(label, core.AS_LOADED_LABEL)
            self.assertEqual(str(pd_), "2026-08-20")
            self.assertEqual(ledger(cur), [(
                FY, f"{self.last_url} (financial year {FY}; page change "
                    f"{TS})", "unchanged", 1)])
            self.assertEqual(ledger(cur, ML, "vintage"), [])
            for part in ("rates", "mapping"):
                self.assertTrue(pe.status(cur, m.profile(part))["ok"], part)
            # the same bytes on every path: unchanged against edition 1
            text, _ = self.ok(cur, ["load", "--only", "rates"])
            self.assertIn("nothing parsed", text)
            for argv in (["load", "--only", "rates", "--recheck", FY],
                         ["load", "--only", "rates", "--release", FY,
                          "--recheck", FY],
                         ["load", "--only", "rates", "--file", path],
                         ["load", "--only", "rates", "--file", path,
                          "--no-page"],
                         ["load", "--only", "rates", "--file", path,
                          "--commit"],
                         ["load", "--only", "rates", "--file", path,
                          "--no-page", "--recheck", FY, "--commit"],
                         ["load", "--only", "rates", "--recheck", FY,
                          "--commit"]):
                with self.subTest(argv=argv):
                    text, _ = self.ok(cur, argv)
                    self.assertIn(f"{FY}: unchanged", text)
                    self.assertNotIn("REJECTED", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual([o for _, _, o, _ in ledger(cur)],
                             ["unchanged"] * 4)
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)

    def test_the_command_previews_then_commits(self):
        with rolled_back(self.conn) as cur:
            path, legacy = self.seed_legacy(cur)
            with mock.patch.object(m, "LEGACY_RATES", legacy["rates"]), \
                    mock.patch.object(m, "LEGACY_RATES_LOADED",
                                      legacy["rates_loaded"]), \
                    mock.patch.object(m, "LEGACY_MAPPING", legacy["mapping"]), \
                    mock.patch.object(m, "LEGACY_MAPPING_LOADED",
                                      legacy["mapping_loaded"]), \
                    mock.patch.object(m, "LEGACY_RATES_FILE",
                                      legacy["rates_file"]), \
                    mock.patch.object(m, "LEGACY_METHOD_FORM",
                                      legacy["method_form"]), \
                    mock.patch.object(m, "RATES_FILE", path):
                text, logged = self.ok(cur, ["migrate-legacy"])
                self.assertIn("PREVIEW", text)
                self.assertEqual(count(cur, "zz_s14_rates_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, ["migrate-legacy", "--simulate"])
                self.assertIn("SIMULATION", text)
                self.assertEqual(count(cur, "zz_s14_rates_editions"), 0)
                text, logged = self.ok(cur, ["migrate-legacy", "--commit"])
            self.assertEqual(editions(cur), [(1, None, N)])
            logged.assert_called_once()

    def test_a_planted_difference_stops_and_rolls_back(self):
        for table, sql in (
                ("rates", "UPDATE public.zz_s14_rates_live SET sar_weekly = "
                          "sar_weekly + 0.01 WHERE brma_name = 'Ashford'"),
                ("mapping", "UPDATE public.zz_s14_map_live SET brma_name = "
                            "'Ashford' WHERE lad24cd = 'E06000010'")):
            with self.subTest(table=table), rolled_back(self.conn) as cur:
                path, _ = self.seed_legacy(cur)
                cur.execute(sql)
                _, legacy = self.seed_legacy_state(cur, path)
                with self.assertRaises(SystemExit) as e:
                    self.migrate(cur, path, legacy)
                self.assertIn("proof failed", str(e.exception.code))
                for t in TABLES[2:]:
                    self.assertEqual(count(cur, t), 0, t)

    def seed_legacy_state(self, cur, path):
        rn, rh, rl = m.live_state(cur, ZR)
        mn, mh, ml = m.live_state(cur, ZM)
        rf = dict(m.LEGACY_RATES_FILE, sha256=m.content_sha256(path),
                  url=self.last_url, rank=(FY, TS_DT))
        return path, {"rates": (rn, rh), "rates_loaded": rl,
                      "mapping": (mn, mh), "mapping_loaded": ml,
                      "rates_file": rf,
                      "method_form": ("NaN", m.WITHIN, NA - 1)}

    def test_a_wrong_sha_live_state_or_form_count_stops(self):
        with rolled_back(self.conn) as cur:
            path, legacy = self.seed_legacy(cur)
            cases = (
                (dict(legacy, rates_file=dict(legacy["rates_file"],
                                              sha256="0" * 64)), "not the held"),
                (dict(legacy, rates=(N, "x")), "not as surveyed"),
                (dict(legacy, mapping_loaded={}), "loaded_at"),
                (dict(legacy, method_form=("NaN", m.WITHIN, 295)),
                 "expected 295"))
            for lg, needle in cases:
                with self.subTest(needle=needle):
                    with self.assertRaises(SystemExit) as e:
                        self.migrate(cur, path, lg)
                    self.assertIn(needle, str(e.exception.code))
            for t in TABLES[2:]:
                self.assertEqual(count(cur, t), 0, t)

    def test_a_non_empty_editions_table_stops(self):
        with rolled_back(self.conn) as cur:
            path, legacy = self.seed_legacy(cur)
            self.migrate(cur, path, legacy)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, path, legacy)
            self.assertIn("runs once", str(e.exception.code))

    def test_then_the_named_correction_is_the_next_edition(self):
        with rolled_back(self.conn) as cur:
            path, legacy = self.seed_legacy(cur)
            self.migrate(cur, path, legacy)
            corr = dict(m.MAPPING_CORRECTIONS["s14-mapping-method-2026-10"],
                        count=NA - 1)
            with mock.patch.dict(m.MAPPING_CORRECTIONS,
                                 {"s14-mapping-method-2026-10": corr}):
                text, _ = self.ok(cur, ["load", "--only", "mapping",
                                        "--acknowledge", V, "--commit"])
            self.assertIn(f"{V}: revised", text)
            self.assertIn("named correction s14-mapping-method-2026-10", text)
            self.assertEqual(editions(cur, V, "zz_s14_map_editions",
                                      "vintage"), [(1, None, NA), (2, 1, NA)])
            self.ok(cur, ["refresh-latest", "--commit"])
            cur.execute("SELECT mapping_method, COUNT(*) FROM "
                        "public.zz_s14_map_live GROUP BY 1 ORDER BY 1")
            self.assertEqual(cur.fetchall(), [("centroid_spatial_join",
                                               NA - 1),
                                              ("nearest_fallback", 1)])


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

class Commands(Fixture):

    def test_status_without_editions_tables_is_clean_and_creates_nothing(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1)
            self.assertIn("not present yet", text)
            for t in TABLES[2:]:
                self.assertFalse(pe.table_exists(cur, t), t)
            self.assertFalse(m.live_has_vintage(cur, ZM))

    def test_ddl_preview_then_commit_adds_the_vintage_column(self):
        with rolled_back(self.conn, ddl=False) as cur:
            text, _ = self.ok(cur, ["ddl"])
            self.assertIn("DRY RUN", text)
            self.assertIn("ADD COLUMN vintage", text)
            self.assertFalse(m.live_has_vintage(cur, ZM))
            self.ok(cur, ["ddl", "--commit"])
            for t in TABLES[2:]:
                self.assertTrue(pe.table_exists(cur, t), t)
            self.assertTrue(m.live_has_vintage(cur, ZM))
            self.assertEqual(m.ensure_live_vintage(cur, ZM), "present")
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 0, text)

    def test_load_preview_before_ddl_compares_with_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            self.publish()
            text, _ = self.ok(cur, ["load"])
            self.assertIn("compares with the LIVE table", text)
            self.assertIn(f"{FY}: new", text)
            rc, text, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt")
            self.assertIn("run `ddl --commit`", text)

    def test_refresh_latest_without_tables_names_migrate_legacy(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["refresh-latest"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("migrate-legacy", text)

    def test_status_lines_after_a_load(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["status"])
            self.assertIn(f"financial year {FY}; page change {TS}", text)
            self.assertIn("ledger latest: new", text)


if __name__ == "__main__":
    unittest.main()
