"""Tests for the S13 loader in s13_lahs_editions (the commands,
migrate-legacy, restore-edition) on throwaway tables.

No network: the content API replies and the downloads are stubbed, and every
.csv and .ods is written by the test (the writers of test_s13_lahs_pure).
Database tests run on a connection from _db.get_conn() inside a transaction
that is always rolled back (rolled_back below); nothing here commits. The
commands get a stand-in connection whose commit and rollback act on a
savepoint of that transaction, and a cursor proxy whose .connection does the
same, so the engine's per-period commit never reaches the real connection.
The only tables written are zz_s13_live (a LIKE copy of la_housing_register),
zz_s13_editions and its ledger, created inside that transaction (refused if
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
import s13_lahs_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s13_lahs_pure import (KEYS, LA, PREDECESSORS, YEAR_BASE,  # noqa: E402
                                YEARS, _Session, cell, collection, csv_rows,
                                open_data_page, write_csv, write_ods,
                                year_page)

ZZ = dataclasses.replace(m.SPEC, name="zz_s13", live_table="zz_s13_live",
                         editions_table="zz_s13_editions")
LEDGER = "zz_s13_editions_file_checks"
N = len(KEYS)
PERIODS = ["2018", "2024", "2025"]
JUNE = date(2026, 6, 25)
FEB = date(2026, 2, 12)


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def n(code, label):
    return int(cell(code, label, "cc1a").replace(",", ""))


class _Tx:
    SP = "zz_s13_cmd"

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
        cur.execute("SELECT to_regclass('public.zz_s13_live'), "
                    "to_regclass('public.zz_s13_editions'), "
                    f"to_regclass('public.{LEDGER}')")
        if any(cur.fetchone()):
            raise RuntimeError("a zz_s13 table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s13_live (LIKE "
                    "public.la_housing_register INCLUDING ALL)")
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


def editions(cur, period="2025"):
    """[(edition, supersedes, rows)]."""
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM "
                "public.zz_s13_editions WHERE reporting_year = %s "
                "GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur):
    """[(reporting_year, source_file, outcome, edition)] in order."""
    cur.execute(f"SELECT reporting_year::text, source_file, outcome, edition "
                f"FROM public.{LEDGER} ORDER BY id")
    return cur.fetchall()


def live_val(cur, la, col="households_on_register", period="2025"):
    cur.execute(f"SELECT {col} FROM public.zz_s13_live WHERE lad24cd = %s "
                "AND reporting_year = %s", (la, period))
    r = cur.fetchone()
    return r[0] if r else "absent"


def ed_val(cur, la, col, period="2025", edition=None):
    cur.execute(f"SELECT {col} FROM public.zz_s13_editions WHERE lad24cd = %s "
                "AND reporting_year = %s ORDER BY edition DESC", (la, period))
    rows = [r[0] for r in cur.fetchall()]
    if edition is None:
        return rows[0]
    return rows[-edition]


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
        self.api = {m.COLLECTION_PATH: collection("2023-24", "2024-25")}
        self.urls = {}
        self.final = {}
        self.fetched = []
        self.k = 0

    def files(self, years=YEARS, *, latest="25 June 2026", register=True,
              ods=None, **kw):
        """Write a CSV and its newest year's ODS; register the open data
        page, the year page and the downloads. Returns (csv, ods) paths."""
        self.k += 1
        label = years[-1]
        d = self.root / f"t{self.k}"
        csv_path = write_csv(d / f"LAHS_open_data_1978-79_to_{label}.csv",
                             years, **kw)
        # a year before 2024-25 publishes cc2a in its ODS (as 2023-24 does)
        ods = dict({"cc2a": label < "2024-25"}, **(ods or {}))
        ods_path = write_ods(d / f"LAHS_accessible_{label}_tables.ods", label,
                             cover={"latest": latest}, **ods)
        if register:
            cu = f"https://assets.example/media/c{self.k}/{csv_path.name}"
            ou = f"https://assets.example/media/o{self.k}/{ods_path.name}"
            self.urls[cu], self.urls[ou] = csv_path, ods_path
            self.api[m.OPEN_DATA_PATH] = open_data_page(label, cu)
            a, b = int(label[:4]), int(label[:4]) + 1
            self.api[YEAR_BASE.format(a, b)] = year_page(label, ou)
            self.csv_url, self.ods_url = cu, ou
        return csv_path, ods_path

    def run_main(self, cur, argv):
        borrowed = _Borrowed(cur)

        def fetch(url, dest, session=None, kind=None):
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
        self.files(**kw)
        self.ok(cur, ["load", "--commit"])
        self.fetched.clear()


class Load(Fixture):

    def test_new_periods_edition_live_and_ledger_in_one_transaction(self):
        with rolled_back(self.conn) as cur:
            self.files()
            text, logged = self.ok(cur, ["load", "--commit"])
            for p in PERIODS:
                self.assertIn(f"{p}: new", text)
                self.assertEqual(editions(cur, p), [(1, None, N)])
                self.assertEqual(core.rows_differing(cur, ZZ, p, 1), 0)
            self.assertEqual(count(cur, "zz_s13_live"), 3 * N)
            lg = ledger(cur)
            self.assertEqual([(p, o, e) for p, _, o, e in lg],
                             [(p, "new", 1) for p in PERIODS])
            self.assertTrue(lg[0][1].startswith(self.csv_url + " (LAHS "
                                                "2024-25; latest update "
                                                "2026-06-25;"))
            self.assertIn("ALLERDALE_ZERO", text)
            self.assertIn("TELFORD_NO_REGISTER", text)
            # blanks NULL with their reason, never 0; a published 0 stays 0
            self.assertIsNone(live_val(cur, "E06000020"))
            self.assertEqual(ed_val(cur, "E06000020", "value_flag"),
                             "households_on_register=not_applicable; "
                             "jointly_managed_register=not_applicable")
            self.assertEqual(live_val(cur, "E06000001", period="2024"), 0)
            self.assertIsNone(live_val(cur, "E06000063", period="2018"))
            self.assertEqual(ed_val(cur, "E06000063", "predecessor_codes",
                                    "2018"), ";".join(PREDECESSORS))
            self.assertEqual(live_val(cur, "E06000063", period="2024"),
                             n("E06000063", "2023-24"))
            # Barnsley stored on the canonical code
            self.assertEqual(live_val(cur, "E08000016"),
                             n("E08000016", "2024-25"))
            self.assertEqual(live_val(cur, "E08000038"), "absent")
            # cc5a filled; imputed flag from the ODS for the newest year
            self.assertEqual(live_val(cur, "E06000002",
                                      "reasonable_preference"),
                             int(cell("E06000002", "2024-25", "cc5a")))
            self.assertIs(ed_val(cur, "E06000002", "imputed_cc1a"), True)
            self.assertIsNone(ed_val(cur, "E06000002", "imputed_cc1a",
                                     "2024"))
            # live source: the edition's label plus the rule note verbatim
            src = live_val(cur, "E06000020", "source")
            self.assertTrue(src.endswith("[" + m.TELFORD_NO_REGISTER["note"]
                                         + "]"), src)
            self.assertNotIn("[", live_val(cur, "E06000001", "source"))
            logged.assert_called_once()
            self.assertIn("2025 new", logged.call_args[0][2])
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_failing_ledger_insert_rolls_back_the_period(self):
        with rolled_back(self.conn) as cur:
            self.files()
            with mock.patch.object(pe, "record_file_check",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2018: FAILED", text)
            for p in PERIODS:
                self.assertEqual(editions(cur, p), [])
            self.assertEqual(count(cur, "zz_s13_live"), 0)
            self.assertEqual(ledger(cur), [])
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN", notes)
            self.assertIn("failed ['2018']", notes)
            self.assertIn("not attempted ['2024', '2025']", notes)

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            self.files()
            text, logged = self.ok(cur, ["load"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            for t in ("zz_s13_editions", "zz_s13_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--simulate"])
            self.assertIn("SIMULATION", text)
            for t in ("zz_s13_editions", "zz_s13_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            self.ok(cur, ["load", "--commit"])
            calls = []
            real = m.read_open_data
            with mock.patch.object(m, "read_open_data", side_effect=lambda
                                   *a, **kw: calls.append(a) or real(*a,
                                                                     **kw)):
                text, logged = self.ok(cur, ["load", "--commit"])
            self.assertEqual(calls, [])
            self.assertIn("nothing parsed", text)
            logged.assert_not_called()
            self.assertEqual(len(ledger(cur)), 3)

    def test_a_restating_file_stores_only_changed_years(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, latest="12 February 2026")
            before = live_val(cur, "E06000002", period="2024")
            self.files(overrides={("E06000002", "2023-24", "cc1a"):
                                  str(before + 3)})
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn("2018: unchanged", text)
            self.assertIn("2024: revised", text)
            self.assertIn("2025: unchanged", text)
            self.assertEqual(editions(cur, "2024"), [(1, None, N), (2, 1, N)])
            self.assertEqual(editions(cur, "2018"), [(1, None, N)])
            outcomes = [(p, o) for p, _, o, _ in ledger(cur)][3:]
            self.assertEqual(outcomes, [("2018", "unchanged"),
                                        ("2024", "revised"),
                                        ("2025", "unchanged")])
            # live untouched until refresh-latest
            self.assertEqual(live_val(cur, "E06000002", period="2024"),
                             before)
            self.assertEqual(pe.status(cur, m.profile())["pending_refresh"],
                             ["2024"])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("2024=1", text)
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "E06000002", period="2024"),
                             before + 3)
            cur.execute("SELECT loaded_at FROM public.zz_s13_editions WHERE "
                        "reporting_year = 2024 AND edition = 2 LIMIT 1")
            ed_loaded = cur.fetchone()[0]
            self.assertEqual(live_val(cur, "E06000002", "loaded_at", "2024"),
                             ed_loaded)
            self.assertIn("2026-06-25", live_val(cur, "E06000002", "source",
                                                 "2024"))
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_byte_identical_rereads_are_unchanged_on_every_path(self):
        with rolled_back(self.conn) as cur:
            csv_path, ods_path = self.files(overrides={
                ("E06000002", "2023-24", "cc5a"): "[s]"})
            self.ok(cur, ["load", "--commit"])
            text, _ = self.ok(cur, ["load"])
            self.assertIn("nothing parsed", text)
            for argv in (["load", "--recheck", "2025"],
                         ["load", "--release", "2024-25", "--recheck",
                          "2024"],
                         ["load", "--file", csv_path],
                         ["load", "--file", csv_path, "--ods", ods_path],
                         ["load", "--file", csv_path, "--ods", ods_path,
                          "--no-page", "--commit"],
                         ["load", "--file", csv_path, "--no-page", "--ods",
                          ods_path, "--recheck", "2018", "--commit"],
                         ["load", "--recheck", "2024", "--commit"]):
                with self.subTest(argv=argv):
                    text, _ = self.ok(cur, argv)
                    self.assertNotIn(": revised", text)
                    self.assertNotIn("REJECTED", text)
                    self.assertTrue(" unchanged" in text, text)
            for p in PERIODS:
                self.assertEqual(editions(cur, p), [(1, None, N)])
            self.assertEqual({o for _, _, o, _ in ledger(cur)[3:]},
                             {"unchanged"})
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)

    def test_ledger_skip_needs_the_pair_for_every_period(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, latest="12 February 2026")
            csv_path, _ = self.files(overrides={("E06000001", "2024-25",
                                                 "cc5a"): "1"},
                                     ods={"overrides": {("E06000001",
                                                         "cc5a"): 1}})
            src = m.ledger_source(self.csv_url, JUNE, "2024-25", PERIODS)
            cur.execute(f"INSERT INTO public.{LEDGER} (reporting_year, "
                        "source_file, file_sha256, outcome, edition) VALUES "
                        "(2018, %s, %s, 'unchanged', 1)",
                        (src, m.content_sha256(csv_path)))
            text, _ = self.ok(cur, ["load"])
            self.assertNotIn("nothing parsed", text)
            self.assertIn("2025: revised", text)

    def older(self, cur, how):
        """Seed from a file of latest update 2026-06-25, then offer a file
        of 2026-02-12 with a different value by the page, --release or
        --file."""
        self.seed(cur)
        csv_path, ods_path = self.files(
            latest="12 February 2026", register=how != "file",
            overrides={("E06000002", "2024-25", "cc1a"): "99"},
            ods={"overrides": {("E06000002", "cc1a"): 99}})
        argv = ["load", "--commit"]
        if how == "release":
            argv += ["--release", "2024-25"]
        if how == "file":
            argv += ["--file", csv_path, "--ods", ods_path]
        return argv

    def test_an_older_file_is_skipped_and_halts_on_every_path(self):
        for how in ("page", "release", "file"):
            with self.subTest(how=how), rolled_back(self.conn) as cur:
                rc, text, logged = self.run_main(cur, self.older(cur, how))
                self.assertEqual(rc, "halt", text)
                self.assertIn("2025 (older", text)
                self.assertIn("older file", text)
                self.assertEqual(editions(cur), [(1, None, N)])
                self.assertEqual(len(ledger(cur)), 3)
                logged.assert_not_called()

    def test_allow_older_file_overrides_and_is_logged(self):
        with rolled_back(self.conn) as cur:
            argv = self.older(cur, "file") + ["--allow-older-file",
                                              "--acknowledge", "2025"]
            text, logged = self.ok(cur, argv)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertIn("--allow-older-file given", logged.call_args[0][2])
            self.assertIn("--allow-older-file given",
                          ed_val(cur, "E06000002", "release_label"))

    def test_equal_rank_with_different_content_stops_unless_accepted(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.files(overrides={("E06000002", "2023-24", "cc5a"): "1"})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("two files claim the same release", text)
            self.assertIn("--accept-reissue 2024", text)
            self.assertEqual(editions(cur, "2024"), [(1, None, N)])
            self.assertIn("rejected ['2024'", logged.call_args[0][2])
            text, logged = self.ok(cur, ["load", "--commit",
                                         "--accept-reissue", "2024"])
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertEqual(editions(cur, "2024"), [(1, None, N), (2, 1, N)])
            self.assertIn("--accept-reissue", logged.call_args[0][2])

    def test_a_new_year_breaking_a_stop_condition_is_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, years=YEARS[:-1], latest="12 February 2026")
            big = {(c, "2024-25", "cc1a"): "90,000" for c in LA}
            self.files(overrides=big, ods={"overrides": {
                (c, "cc1a"): 90000 for c in LA}})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2025: REJECTED", text)
            self.assertIn("England", text)
            self.assertEqual(editions(cur, "2025"), [])
            self.assertEqual(count(cur, "zz_s13_live",
                                   "WHERE reporting_year = 2025"), 0)
            self.assertNotIn("2025", [p for p, _, _, _ in ledger(cur)])
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN (exit 1)", notes)
            self.assertIn("rejected ['2025'", notes)
            self.assertIn("2018 unchanged", notes)
            rc, text, logged = self.run_main(cur, ["load"])
            self.assertEqual(rc, 1, text)
            logged.assert_not_called()
            text, _ = self.ok(cur, ["load", "--commit", "--acknowledge",
                                    "2025"])
            self.assertIn("2025: new", text)
            self.assertIn("ACKNOWLEDGED", text)

    def test_a_partial_file_never_replaces_a_fuller_edition(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, latest="12 February 2026")
            rows = [r for r in csv_rows() if not (
                r["local_authority_code"] == "E06000002"
                and r["Year"] == "2023-24")]
            self.files(rows=rows)
            rc, text, logged = self.run_main(cur, ["load", "--commit",
                                                   "--acknowledge", "2024"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2024: REJECTED", text)
            self.assertIn(m.PARTIAL, text)
            self.assertEqual(editions(cur, "2024"), [(1, None, N)])
            self.assertIn("rejected ['2024'", logged.call_args[0][2])

    def test_a_zero_null_flip_needs_a_named_correction(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, latest="12 February 2026")
            self.files(overrides={("E06000001", "2023-24", "cc1a"): "[x]"})
            rc, text, _ = self.run_main(cur, ["load", "--commit",
                                              "--acknowledge", "2024"])
            self.assertEqual(rc, 1, text)
            self.assertIn("0/NULL", text)
            self.assertIn("--acknowledge-correction", text)
            self.assertEqual(editions(cur, "2024"), [(1, None, N)])

    def test_a_stranded_period_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("DELETE FROM public.zz_s13_live WHERE "
                        "reporting_year = 2025")
            self.assertEqual(pe.status(cur, m.profile())["live_missing"],
                             ["2025"])
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn("live-missing", text)
            self.assertEqual(count(cur, "zz_s13_live"), 3 * N)
            self.assertTrue(live_val(cur, "E06000020", "source")
                            .endswith("]"))
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.files()
            final = "https://assets.example/media/new/LAHS.csv"
            self.final[self.csv_url] = final
            self.ok(cur, ["load", "--commit"])
            self.assertTrue(ledger(cur)[0][1].startswith(final + " (LAHS"))
            self.assertIn(final, ed_val(cur, "E06000001", "source_file"))

    def test_same_name_download_with_different_content_is_kept_beside(self):
        with rolled_back(self.conn) as cur:
            held, _ = self.files(register=False)
            new, new_ods = self.files(overrides={
                ("E06000002", "2023-24", "cc5a"): "3"}, register=False)
            raw = self.root / "raw"
            raw.mkdir()
            (raw / held.name).write_bytes(held.read_bytes())
            cu = f"https://assets.example/media/z/{held.name}"
            ou = f"https://assets.example/media/z/{new_ods.name}"
            self.api[m.OPEN_DATA_PATH] = open_data_page("2024-25", cu)
            self.api[YEAR_BASE.format(2024, 2025)] = year_page("2024-25", ou)
            bodies = {cu: new.read_bytes(), ou: new_ods.read_bytes()}

            class S:
                def get(self, url, **kw):
                    return _Session(bodies[url], url).get(url)

            borrowed = _Borrowed(cur)
            out = io.StringIO()
            with specs(), mock.patch.object(m, "_conn",
                                            return_value=borrowed), \
                    mock.patch.object(m, "fetch_json", side_effect=lambda p,
                                      s=None: self.api[p]), \
                    mock.patch.object(m, "RAW_DIR", raw), \
                    mock.patch.object(m, "MIN_FILE_BYTES", 10), \
                    mock.patch.object(m, "_session", return_value=S()), \
                    mock.patch.object(m, "log_run"), \
                    contextlib.redirect_stdout(out):
                try:
                    rc = m.main(["load"])
                finally:
                    borrowed.tx.done()
            text = out.getvalue()
            self.assertEqual(rc, 0, text)
            self.assertEqual((raw / held.name).read_bytes(), held.read_bytes())
            beside = [p for p in raw.iterdir() if p.suffix == ".csv"
                      and p.name != held.name]
            self.assertEqual(len(beside), 1)
            self.assertEqual(beside[0].read_bytes(), new.read_bytes())
            self.assertIn("kept it untouched", text)

    def test_file_paths_ods_and_no_page(self):
        with rolled_back(self.conn) as cur:
            csv_path, ods_path = self.files(register=False)
            rc, text, _ = self.run_main(cur, ["load", "--file", csv_path,
                                              "--no-page"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--ods", text)
            rc, text, _ = self.run_main(cur, ["load", "--ods", ods_path])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--file", text)
            rc, text, _ = self.run_main(cur, ["load", "--no-page"])
            self.assertEqual(rc, "halt", text)
            text, logged = self.ok(cur, ["load", "--file", csv_path, "--ods",
                                         ods_path, "--no-page", "--commit"])
            self.assertIn("2025: new", text)
            self.assertIn("--no-page", logged.call_args[0][2])

    def test_identity_and_cross_check_halt(self):
        with rolled_back(self.conn) as cur:
            csv_path, _ = self.files(ods={"overrides": {("E06000002",
                                                         "cc1a"): 5}})
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("E06000002", text)
            self.assertIn("cc1a", text)
            _, old_ods = self.files(years=YEARS[:-1], register=False)
            rc, text, _ = self.run_main(cur, ["load", "--file", csv_path,
                                              "--ods", old_ods, "--no-page"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2023-24", text)
            self.files()
            rc, text, _ = self.run_main(cur, ["load", "--release", "2023-24"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--release 2023-24", text)
            self.assertEqual(count(cur, "zz_s13_editions"), 0)

    def test_discovery_fails_loudly(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["load"])
            self.assertIn("no newer", text)
            self.api[m.COLLECTION_PATH]["links"]["documents"].append(
                {"title": "Local authority housing statistics data returns "
                          "for 2025 to 2026", "base_path": "/x"})
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2025 to 2026", text)
            self.api[m.COLLECTION_PATH]["links"]["documents"].pop()
            self.api[m.OPEN_DATA_PATH]["title"] = "LAHS open data"
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("LAHS open data", text)
            self.api[m.OPEN_DATA_PATH]["title"] = m.OPEN_DATA_TITLE
            self.api[m.OPEN_DATA_PATH]["details"]["attachments"] = []
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("0 attachments", text)

    def test_unknown_code_halts(self):
        with rolled_back(self.conn) as cur:
            rows = csv_rows()
            i = [n for n, r in enumerate(rows) if r["Year"] == "2017-18"
                 and r["local_authority_code"] == "E06000002"][0]
            rows[i] = dict(rows[i], local_authority_code="E07000999",
                           LAD24CD="E07000999")
            self.files(rows=rows)
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("UNEXPLAINED E07000999", text)
            self.assertEqual(count(cur, "zz_s13_editions"), 0)
            logged.assert_not_called()

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, latest="12 February 2026")
            first = live_val(cur, "E06000002")
            self.files(overrides={("E06000002", "2024-25", "cc1a"):
                                  str(first + 3)},
                       ods={"overrides": {("E06000002", "cc1a"): first + 3}})
            self.ok(cur, ["load", "--commit"])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "E06000002"), first + 3)
            text, logged = self.ok(cur, ["restore-edition", "2025", "1",
                                         "--commit"])
            self.assertIn("as edition 3", text)
            self.assertEqual(editions(cur)[-1], (3, 2, N))
            logged.assert_called_once()
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "E06000002"), first)
            rc, text, _ = self.run_main(cur, ["restore-edition", "2025", "3"])
            self.assertEqual(rc, "halt")
            self.assertIn("is the tip", text)


# ---------------------------------------------------------------------------
# migrate-legacy
# ---------------------------------------------------------------------------

LOADED = "2026-04-01 01:25:26.774235+00"
LATER = "2026-08-20 08:17:46.988105+00"


class Migrate(Fixture):

    def n8n_live(self, cur, feb, *, pick=None, june=None, june_keys=()):
        """Seed live as the n8n load did: per resolved key the first source
        row in file order (pick {(key, period): index} for another one), no
        sums, no cc5a, [x] NULL, Yes/No as booleans; Telford's zeros from
        2022 NULL with the note in source (the 2026-09-30 direct SQL);
        june_keys taken from the June file with a later loaded_at."""
        pick = pick or {}

        def rows_of(path):
            od = m.read_open_data(path)
            out = {}
            for r in od["rows"]:
                lad = r["lad24cd_pub"]
                lad = {"E08000038": "E08000016"}.get(lad, lad)
                out.setdefault((lad, r["period"]), []).append(r)
            return out

        held = rows_of(feb)
        later = rows_of(june) if june else {}
        for key, rows in sorted(held.items()):
            src = later[key] if key in june_keys else rows
            r = src[pick.get(key, 0)]
            h, _ = m.cell_int(r["cc1a"], "t")
            j, _ = m.cell_yes_no(r["cc2a"], "t")
            source = f"MHCLG LAHS Section C, year ending 31 March {key[1]}"
            if key[0] == "E06000020" and int(key[1]) >= 2022 and h == 0:
                h = None
                source += " [" + m.TELFORD_NO_REGISTER["note"] + "]"
            cur.execute("INSERT INTO public.zz_s13_live (lad24cd, "
                        "reporting_year, households_on_register, "
                        "jointly_managed_register, reasonable_preference, "
                        "source, loaded_at) VALUES (%s, %s, %s, %s, NULL, %s, "
                        "%s)", (key[0], int(key[1]), h, j, source,
                                LATER if key in june_keys else LOADED))

    def legacy(self, path):
        return {"name": path.name, "sha256": m.content_sha256(path),
                "where": m._file_source(path)}

    def migrate(self, cur, feb, **kw):
        with quiet() as out:
            plan = m.migrate_legacy(
                cur, feb, write=kw.pop("write", True),
                legacy=kw.pop("legacy", None) or m.live_state(cur),
                files=kw.pop("files", None) or self.legacy(feb), **kw)
        return plan, out.getvalue()

    def test_proof_records_non_first_rows_and_edition_1_is_live(self):
        with rolled_back(self.conn) as cur:
            feb, _ = self.files(latest="12 February 2026", register=False)
            june, _ = self.files(register=False, overrides={
                ("E06000002", "2024-25", "cc1a"): "4,321"})
            self.n8n_live(cur, feb, pick={("E06000063", "2018"): 2},
                          june=june, june_keys={("E06000002", "2025")})
            plan, text = self.migrate(cur, feb, june={
                "path": june, "sha256": m.content_sha256(june)})
            self.assertIn("first row", text)
            self.assertIn("E06000063 2018", text)
            self.assertIn("E06000002 2025", text)
            self.assertIn("June", text)
            self.assertIn("TELFORD_NO_REGISTER", text)
            self.assertEqual(plan["not_first"], [("E06000063", "2018")])
            for p in PERIODS:
                self.assertEqual(editions(cur, p), [(1, None, N)])
                self.assertEqual(core.rows_differing(cur, ZZ, p, 1), 0)
            self.assertEqual(ed_val(cur, "E06000002", "households_on_register"),
                             4321)
            self.assertEqual(ed_val(cur, "E06000063", "predecessor_codes",
                                    "2018"), PREDECESSORS[2])
            self.assertEqual(ed_val(cur, "E06000020", "value_flag"),
                             "households_on_register=not_applicable; "
                             "jointly_managed_register=not_applicable; "
                             "reasonable_preference=not_loaded")
            self.assertTrue(ed_val(cur, "E06000020", "live_source")
                            .endswith("]"))
            self.assertIsNone(ed_val(cur, "E06000001", "imputed_cc1a"))
            cur.execute("SELECT DISTINCT release_label, published_date FROM "
                        "public.zz_s13_editions WHERE reporting_year = 2025")
            self.assertEqual(cur.fetchall(), [(core.AS_LOADED_LATEST_LABEL,
                                               date(2026, 8, 20))])
            cur.execute("SELECT DISTINCT published_date FROM "
                        "public.zz_s13_editions WHERE reporting_year = 2018")
            self.assertEqual(cur.fetchall(), [(date(2026, 4, 1),)])
            self.assertEqual(m.release_rank(ed_val(cur, "E06000001",
                                                   "source_file")), FEB)
            lg = ledger(cur)
            self.assertEqual([(p, o, e) for p, _, o, e in lg],
                             [(p, "unchanged", 1) for p in PERIODS])
            self.assertTrue(lg[0][1].startswith(m._file_source(feb) + " ("))
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_the_held_file_reread_is_unchanged_then_the_correction(self):
        with rolled_back(self.conn) as cur:
            feb, feb_ods = self.files(latest="12 February 2026",
                                      register=False)
            self.n8n_live(cur, feb)
            self.migrate(cur, feb)
            self.api[YEAR_BASE.format(2024, 2025)] = year_page("2024-25")
            # the held file again, by --file: the ledger holds its pair
            for argv in (["load", "--file", feb, "--ods", feb_ods,
                          "--no-page"],
                         ["load", "--file", feb, "--ods", feb_ods,
                          "--no-page", "--commit"],
                         ["load", "--file", feb, "--ods", feb_ods]):
                with self.subTest(argv=argv):
                    text, _ = self.ok(cur, argv)
                    self.assertIn("already in the ledger", text)
                    self.assertIn("nothing to do", text)
            for p in PERIODS:
                self.assertEqual(editions(cur, p), [(1, None, N)])
            # the current file: every year changes (cc5a; sums; Allerdale)
            self.files()
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("REJECTED", text)
            self.assertIn("--acknowledge-correction", text)
            for p in PERIODS:
                self.assertEqual(editions(cur, p), [(1, None, N)])
            groups = {"2018": {"cc5a_filled": N, "allerdale": 1},
                      "2024": {"cc5a_filled": N},
                      "2025": {"cc5a_filled": N}}
            fix = {"periods": groups, "decided": "Scott (test)",
                   "why": "test"}
            with mock.patch.dict(m.ACKNOWLEDGED_CORRECTIONS, {"t": fix}):
                bad = dict(fix, periods=dict(groups, **{"2024": {
                    "cc5a_filled": N - 1}}))
                with mock.patch.dict(m.ACKNOWLEDGED_CORRECTIONS,
                                     {"t2": bad}):
                    rc, text, _ = self.run_main(cur, [
                        "load", "--commit", "--acknowledge-correction", "t2"])
                self.assertEqual(rc, 1, text)
                self.assertIn("does not cover", text)
                text, logged = self.ok(cur, [
                    "load", "--commit", "--acknowledge-correction", "t"])
            for p in PERIODS:
                self.assertEqual(editions(cur, p), [(1, None, N), (2, 1, N)])
            self.assertIn("ACKNOWLEDGED t", logged.call_args[0][2])
            self.assertIsNone(ed_val(cur, "E06000063",
                                     "households_on_register", "2018"))
            self.assertEqual(live_val(cur, "E06000063", period="2018"),
                             n("E07000026", "2017-18"))
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertIsNone(live_val(cur, "E06000063", period="2018"))
            self.assertEqual(live_val(cur, "E06000002",
                                      "reasonable_preference"),
                             int(cell("E06000002", "2024-25", "cc5a")))
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_planted_difference_stops_and_rolls_back(self):
        with rolled_back(self.conn) as cur:
            feb, _ = self.files(latest="12 February 2026", register=False)
            self.n8n_live(cur, feb)
            cur.execute("UPDATE public.zz_s13_live SET "
                        "households_on_register = households_on_register + 1 "
                        "WHERE lad24cd = 'E06000002' AND reporting_year = "
                        "2024")
            with mock.patch.object(m, "LEGACY_LIVE", m.live_state(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", self.legacy(feb)), \
                    mock.patch.object(m, "LEGACY_JUNE", None):
                rc, text, logged = self.run_main(
                    cur, ["migrate-legacy", feb, "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("proof failed", text)
            self.assertIn("E06000002", text)
            self.assertEqual(count(cur, "zz_s13_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)
            logged.assert_not_called()

    def test_the_command_previews_then_commits(self):
        with rolled_back(self.conn) as cur:
            feb, _ = self.files(latest="12 February 2026", register=False)
            self.n8n_live(cur, feb)
            with mock.patch.object(m, "LEGACY_LIVE", m.live_state(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", self.legacy(feb)), \
                    mock.patch.object(m, "LEGACY_JUNE", None):
                text, logged = self.ok(cur, ["migrate-legacy", feb])
                self.assertIn("PREVIEW", text)
                self.assertEqual(count(cur, "zz_s13_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, ["migrate-legacy", feb,
                                             "--simulate"])
                self.assertIn("SIMULATION", text)
                self.assertEqual(count(cur, "zz_s13_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, ["migrate-legacy", feb,
                                             "--commit"])
            self.assertEqual(editions(cur), [(1, None, N)])
            logged.assert_called_once()

    def test_a_wrong_file_sha_live_state_or_non_empty_editions_stops(self):
        with rolled_back(self.conn) as cur:
            feb, _ = self.files(latest="12 February 2026", register=False)
            self.n8n_live(cur, feb)
            bad = dict(self.legacy(feb), sha256="0" * 64)
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, feb, write=True,
                                 legacy=m.live_state(cur), files=bad)
            self.assertIn("expected 00000000", str(e.exception.code))
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, feb, write=True,
                                 legacy=(3 * N, "x", 1, 3),
                                 files=self.legacy(feb))
            self.assertIn("not as surveyed", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s13_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)
            self.migrate(cur, feb)
            with self.assertRaises(SystemExit) as e, quiet():
                m.migrate_legacy(cur, feb, write=True,
                                 legacy=m.live_state(cur),
                                 files=self.legacy(feb))
            self.assertIn("runs once", str(e.exception.code))

    def test_later_rows_need_the_june_file(self):
        with rolled_back(self.conn) as cur:
            feb, _ = self.files(latest="12 February 2026", register=False)
            june, _ = self.files(register=False, overrides={
                ("E06000002", "2024-25", "cc1a"): "4,321"})
            self.n8n_live(cur, feb, june=june,
                          june_keys={("E06000002", "2025")})
            with self.assertRaises(SystemExit) as e, quiet():
                self.migrate(cur, feb, june=None)
            self.assertIn("June", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s13_editions"), 0)


class Commands(Fixture):

    def test_status_without_editions_table_is_clean_and_creates_nothing(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1)
            self.assertIn("not exist yet", text)
            self.assertFalse(pe.table_exists(cur, "zz_s13_editions"))
            self.assertFalse(pe.table_exists(cur, LEDGER))

    def test_refresh_latest_without_tables_names_migrate_legacy(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["refresh-latest"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("migrate-legacy", text)

    def test_ddl_preview_then_commit_then_status(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["ddl"])
            self.assertEqual(rc, 0)
            self.assertIn("DRY RUN", text)
            self.assertFalse(pe.table_exists(cur, "zz_s13_editions"))
            self.ok(cur, ["ddl", "--commit"])
            for t in ("zz_s13_editions", LEDGER):
                self.assertTrue(pe.table_exists(cur, t), t)
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'zz_s13_live'")
            self.assertNotIn("value_flag", {r[0] for r in cur.fetchall()})

    def test_status_lines_after_a_load(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["status"])
            self.assertIn("latest update 2026-06-25", text)
            self.assertIn("ledger latest: new", text)

    def test_load_preview_before_ddl_compares_with_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            self.files()
            text, _ = self.ok(cur, ["load"])
            self.assertIn("no editions table yet", text)
            self.assertIn("2025: new", text)
            rc, text, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt")
            self.assertIn("run `ddl --commit`", text)


if __name__ == "__main__":
    unittest.main()
