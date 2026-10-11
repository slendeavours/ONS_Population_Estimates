"""Tests for the S3b loader in s3b_tenure_editions (the commands,
migrate-legacy, restore-edition) on throwaway tables.

No network: the NOMIS replies (search, codelist) and the downloads (the
definition JSON and the data CSV) are stubbed, and every file is written by
the test (write_csv and write_def from test_s3b_tenure_pure). Database tests
run on a connection from _db.get_conn() inside a transaction that is always
rolled back (rolled_back below); nothing here commits. The commands get a
stand-in connection whose commit and rollback act on a savepoint of that
transaction, and a cursor proxy whose .connection does the same, so the
engine's per-period commit never reaches the real connection. The only
tables written are zz_s3b_live (a LIKE copy of la_tenure_2021, generated
totals included), zz_s3b_editions and its ledger, created inside that
transaction (refused if any already exists), so they never persist. The run
log is stubbed; the real la_tenure_2021, la_code_lookup, la_boundaries and
information_schema are read (never written).
"""
import contextlib
import dataclasses
import io
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s3b_tenure_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s3b_tenure_pure import (CODES, DS_2031, LAST, PARTS,  # noqa: E402
                                  codelist_json, counts, search_json,
                                  write_csv, write_def)

ZZ = dataclasses.replace(m.SPEC, name="zz_s3b", live_table="zz_s3b_live",
                         editions_table="zz_s3b_editions")
LEDGER = "zz_s3b_editions_file_checks"
N = len(CODES)
P = "2021"
P31 = "2031"
LIVE_COLS = ("lad24cd",) + m.VALUES + ("census_year", "source", "loaded_at")
SRC_A = "ONS Census 2021 TS054 Tenure, Nomis NM_2072_1, Census Day 21 March 2021"
SRC_B = ("ONS Census 2021 TS054 Tenure, Nomis NM_2072_1, 2023 local "
         "authority boundaries")
LOADED_A = "2026-04-01 01:55:08.514719+00"
LOADED_B = "2026-08-20 14:20:00.090729+00"


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    SP = "zz_s3b_cmd"

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
            mock.patch.object(m, "EXPECTED_AREAS", N), \
            mock.patch.dict(m.DATASETS, {P31: DS_2031}):
        yield


@contextmanager
def rolled_back(conn, *, ddl=True):
    cur = conn.cursor()
    try:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('zz_s3b_live', 'zz_s3b_editions', %s)", (LEDGER,))
        if cur.fetchall():
            raise RuntimeError("a zz_s3b table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s3b_live (LIKE "
                    "public.la_tenure_2021 INCLUDING ALL)")
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
                "public.zz_s3b_editions WHERE census_year = %s "
                "GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur):
    cur.execute(f"SELECT census_year::text, source_file, outcome, edition "
                f"FROM public.{LEDGER} ORDER BY id")
    return cur.fetchall()


def live_val(cur, la, col="owned_outright"):
    cur.execute(f"SELECT {col} FROM public.zz_s3b_live WHERE lad24cd = %s",
                (la,))
    r = cur.fetchone()
    return r[0] if r else None


def live_md5(cur):
    expr = "||'|'||".join(f"coalesce({c}::text,'~')" for c in LIVE_COLS)
    cur.execute(f"SELECT COUNT(*), md5(string_agg({expr}, E'\\n' ORDER BY "
                f"lad24cd)) FROM public.zz_s3b_live")
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
        self.n = 0
        self.api = {m.search_url(): search_json(),
                    m.codelist_url(m.DATASETS[P]): codelist_json()}
        self.urls = {}
        self.final = {}
        self.fetched = []

    def release(self, *, last=LAST, register=True, ds=None, **kw):
        """Write a definition and a data CSV; register them as what NOMIS
        returns (the data URL is the same for every 2021 release)."""
        self.n += 1
        ds = ds or m.DATASETS[P]
        d = self.root / f"t{self.n}"
        defp = write_def(d / f"{ds['id']}_def.json", id=ds["id"],
                         mnemonic=ds["mnemonic"], last=last)
        if ds is not m.DATASETS[P]:
            kw.setdefault("date", ds["census_year"])
            kw.setdefault("geography_type", ds["geography_type"])
            kw.setdefault("dimension", ds["dimension"])
        csvp = write_csv(d / f"{ds['id']}_{ds['geography']}.csv", **kw)
        if register:
            self.urls[m.def_url(ds)] = defp
            self.urls[m.data_url(m.category_codes(codelist_json()),
                                 ds)] = csvp
        return csvp, defp

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, log)."""
        borrowed = _Borrowed(cur)

        def fetch_api(url, dest, session=None):
            self.fetched.append((url, Path(dest).name))
            return self.urls[url], self.final.get(url, url)

        out = io.StringIO()
        with specs(), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda u, s=None: self.api[u]), \
                mock.patch.object(m, "fetch_api", side_effect=fetch_api), \
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
        self.release(**kw)
        self.ok(cur, ["load", "--commit"])
        self.fetched.clear()


class Load(Fixture):

    def test_first_census_into_an_empty_table_inserts_live(self):
        with rolled_back(self.conn) as cur:
            self.release()
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P}: new", text)
            self.assertIn("NM_2072_1", text)
            self.assertIn("sum to the publisher's total", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(count(cur, "zz_s3b_live"), N)
            self.assertEqual(core.rows_differing(cur, ZZ, P, 1), 0)
            (y, src, outcome, ed), = ledger(cur)
            url = m.data_url(m.category_codes(codelist_json()))
            self.assertEqual((y, outcome, ed), (P, "new", 1))
            self.assertEqual(src, f"{url} (census_year 2021; NOMIS "
                                  "LastUpdated 2023-01-05 09:30:00)")
            cur.execute("SELECT DISTINCT source_file, published_date, "
                        "release_label FROM public.zz_s3b_editions")
            (sf, pd_, label), = cur.fetchall()
            self.assertEqual(m.release_rank(sf), datetime(2023, 1, 5, 9, 30))
            self.assertEqual(pd_, date(2023, 1, 5))
            self.assertIn("TS054 - Tenure", label)
            cur.execute("SELECT DISTINCT source, census_year FROM "
                        "public.zz_s3b_live")
            self.assertEqual(cur.fetchall(), [(label, 2021)])
            # the publisher's total is stored, and the generated totals
            # follow the parts
            self.assertEqual(live_val(cur, "E06000001", "total_households"),
                             40929)
            self.assertEqual(live_val(cur, "E06000001", "social_rented_total"),
                             3986 + 5608)
            logged.assert_called_once()
            self.assertIn(f"{P} new", logged.call_args[0][2])
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            # both downloads went to the dated names under RAW_DIR
            self.assertTrue(any(n.startswith("NM_2072_1_def_")
                                for _, n in self.fetched))
            self.assertTrue(any(n.startswith("NM_2072_1_TYPE424_")
                                for _, n in self.fetched))

    def test_a_failing_ledger_insert_rolls_back_and_logs_a_partial_run(self):
        with rolled_back(self.conn) as cur:
            self.release()
            with mock.patch.object(pe, "record_file_check",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P}: FAILED", text)
            self.assertEqual(editions(cur), [])
            self.assertEqual(count(cur, "zz_s3b_live"), 0)
            self.assertEqual(ledger(cur), [])
            logged.assert_called_once()
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN", notes)
            self.assertIn(f"failed ['{P}']", notes)
            self.assertIn("not attempted none", notes)

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            self.release()
            text, logged = self.ok(cur, ["load"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            for t in ("zz_s3b_editions", "zz_s3b_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--simulate"])
            self.assertIn("SIMULATION", text)
            for t in ("zz_s3b_editions", "zz_s3b_live", LEDGER):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            self.ok(cur, ["load", "--commit"])
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn("nothing compared", text)
            logged.assert_not_called()
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 1)

    def test_another_census_year_is_stored_as_editions_only(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live_md5(cur)
            csvp, defp = self.release(ds=DS_2031, last="2033-03-01 09:30:00",
                                      register=False)
            text, logged = self.ok(cur, ["load", "--file", csvp, "--def",
                                         defp, "--commit"])
            self.assertIn(f"{P31}: held-not-live", text)
            self.assertIn("stored edition 1 only", text)
            self.assertEqual(editions(cur, P31), [(1, None, N)])
            self.assertEqual(live_md5(cur), before)
            cur.execute("SELECT DISTINCT census_year FROM public.zz_s3b_live")
            self.assertEqual(cur.fetchall(), [(2021,)])
            self.assertEqual([(y, o) for y, _, o, _ in ledger(cur)],
                             [(P, "new"), (P31, "held-not-live")])
            self.assertIn(f"{P31} held-not-live", logged.call_args[0][2])
            st = pe.status(cur, m.profile())
            self.assertTrue(st["ok"], st)
            self.assertEqual(st["held_not_live"], [P31])
            text, _ = self.ok(cur, ["status"])
            self.assertIn(f"held, not live: {P31}", text)
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit",
                                              "--accept-drift", P31])
            self.assertEqual(rc, "halt", text)
            self.assertIn(P31, text)
            self.assertEqual(live_md5(cur), before)
            # the same file again: ledger skip; --recheck compares it
            text, _ = self.ok(cur, ["load", "--file", csvp, "--def", defp,
                                    "--commit"])
            self.assertIn("nothing compared", text)
            text, _ = self.ok(cur, ["load", "--file", csvp, "--def", defp,
                                    "--recheck", P31, "--commit"])
            self.assertIn(f"{P31}: unchanged", text)
            self.assertEqual(editions(cur, P31), [(1, None, N)])
            self.assertEqual(live_md5(cur), before)

    def test_another_census_year_never_enters_an_empty_live_table(self):
        with rolled_back(self.conn) as cur:
            csvp, defp = self.release(ds=DS_2031, last="2033-03-01 09:30:00",
                                      register=False)
            rc, text, logged = self.run_main(cur, ["load", "--file", csvp,
                                                   "--def", defp, "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P31}: REJECTED", text)
            self.assertEqual(count(cur, "zz_s3b_live"), 0)
            self.assertEqual(count(cur, "zz_s3b_editions"), 0)
            self.assertIn(f"rejected ['{P31}'", logged.call_args[0][2])

    def test_a_later_lastupdated_with_different_content_stops_unless_accepted(
            self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            ov = {("E06000002", "owned_outright"): 17225,
                  ("E06000002", "total_households"):
                  counts("E06000002")["total_households"] + 1}
            self.release(last="2024-06-01 09:30:00", overrides=ov)
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P}: REJECTED", text)
            self.assertIn("reissue", text)
            self.assertIn("--accept-reissue 2021", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 1)
            self.assertIn("PARTIAL RUN (exit 1)", logged.call_args[0][2])
            text, logged = self.ok(cur, ["load", "--commit",
                                         "--accept-reissue", P])
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertIn("--accept-reissue", logged.call_args[0][2])
            # live waits for refresh-latest
            self.assertEqual(live_val(cur, "E06000002"), 17224)
            self.assertEqual(pe.status(cur, m.profile())["pending_refresh"],
                             [P])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "E06000002"), 17225)
            self.assertEqual(live_val(cur, "E06000002", "total_households"),
                             counts("E06000002")["total_households"] + 1)
            # source: the changed row takes edition 2's label; the others
            # keep theirs (source is provenance, not compared)
            cur.execute("SELECT DISTINCT release_label FROM "
                        "public.zz_s3b_editions WHERE edition = 2")
            label2 = cur.fetchone()[0]
            self.assertEqual(live_val(cur, "E06000002", "source"), label2)
            self.assertNotEqual(live_val(cur, "E06000001", "source"), label2)
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_later_lastupdated_with_the_same_content_is_unchanged(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release(last="2024-06-01 09:30:00")
            text, logged = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P}: unchanged", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual([o for _, _, o, _ in ledger(cur)],
                             ["new", "unchanged"])
            self.assertIn("2024-06-01 09:30:00", ledger(cur)[1][1])
            # now the held tip's rank is the later one: the earlier
            # definition is older
            self.release()
            rc, text, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("older", text)

    def test_equal_lastupdated_with_different_content_stops_unless_accepted(
            self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            ov = {("E06000002", "owned_outright"): 17225,
                  ("E06000002", "total_households"):
                  counts("E06000002")["total_households"] + 1}
            self.release(overrides=ov)
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("two sources claim the same release", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            rc, text, logged = self.run_main(cur, ["load"])
            self.assertEqual(rc, 1, text)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--commit",
                                         "--accept-reissue", P])
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])

    def older(self, cur, how):
        """Seed from LastUpdated 2023-01-05, then offer a definition of
        LastUpdated 2022-11-02 (with the same content) by the API or by
        --file/--def."""
        self.seed(cur)
        csvp, defp = self.release(last="2022-11-02 09:30:00",
                                  register=how != "file")
        argv = ["load", "--commit"]
        if how == "file":
            argv += ["--file", csvp, "--def", defp]
        return argv

    def test_an_earlier_lastupdated_halts_on_every_path(self):
        for how in ("api", "file"):
            with self.subTest(how=how), rolled_back(self.conn) as cur:
                rc, text, logged = self.run_main(cur, self.older(cur, how))
                self.assertEqual(rc, "halt", text)
                self.assertIn(f"{P} (older", text)
                self.assertIn("older", text)
                self.assertEqual(editions(cur), [(1, None, N)])
                self.assertEqual(len(ledger(cur)), 1)
                logged.assert_not_called()

    def test_allow_older_file_overrides_and_is_logged(self):
        with rolled_back(self.conn) as cur:
            argv = self.older(cur, "file") + ["--allow-older-file"]
            text, logged = self.ok(cur, argv)
            self.assertIn(f"{P}: unchanged", text)
            self.assertIn("--allow-older-file given", logged.call_args[0][2])
            self.assertEqual(editions(cur), [(1, None, N)])

    def test_an_acceptance_must_name_a_compared_year(self):
        with rolled_back(self.conn) as cur:
            self.release()
            rc, text, logged = self.run_main(cur, ["load", "--commit",
                                                   "--accept-reissue",
                                                   "2011"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2011", text)
            self.assertEqual(count(cur, "zz_s3b_editions"), 0)
            logged.assert_not_called()

    def test_file_checks_halt_and_store_nothing(self):
        cases = (
            (dict(overrides={("E06000002", "total_households"): 60000}),
             "E06000002"),
            (dict(geography_type="local authorities: district / unitary "
                  "(as of April 2021)"), "April 2021"),
            (dict(codes=CODES[:-1]), f"{N - 1} authorities"),
            (dict(record_count=7), "RECORD_COUNT"),
            (dict(codes=["E06000001", "E06000002", "E08000038", "E08000019",
                         "E09000033"]), "E08000038"),
        )
        for kw, needle in cases:
            with self.subTest(needle=needle), rolled_back(self.conn) as cur:
                self.release(**kw)
                rc, text, logged = self.run_main(cur, ["load", "--commit"])
                self.assertEqual(rc, "halt", text)
                self.assertIn(needle, text)
                self.assertEqual(count(cur, "zz_s3b_editions"), 0)
                self.assertEqual(count(cur, "zz_s3b_live"), 0)
                logged.assert_not_called()

    def test_e08000038_names_the_old_declaration(self):
        with rolled_back(self.conn) as cur:
            self.release(codes=["E06000001", "E06000002", "E08000038",
                                "E08000019", "E09000033"])
            rc, text, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("'old'", text)

    def test_another_dataset_definition_halts(self):
        with rolled_back(self.conn) as cur:
            csvp, _ = self.release(register=False)
            defp = write_def(self.root / "other.json",
                             name="TS055 - Accommodation type",
                             mnemonic="c2021ts055")
            rc, text, _ = self.run_main(cur, ["load", "--file", csvp, "--def",
                                              defp])
            self.assertEqual(rc, "halt", text)
            self.assertIn("TS055", text)
            rc, text, _ = self.run_main(cur, ["load", "--file", csvp])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--def", text)

    def test_discovery_fails_loudly(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["load"])
            self.assertIn("no other TS054 dataset", text)
            self.assertIn("NM_2072_1", text)
            self.api[m.search_url()] = search_json(
                [("NM_2072_1", "TS054 - Tenure", "c2021ts054"),
                 ("NM_8888_1", "TS054 - Tenure", "c2031ts054")])
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("NM_8888_1", text)
            self.assertNotIn("nothing to do", text)
            self.api[m.search_url()] = search_json([])
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("NM_2072_1", text)
            self.api[m.search_url()] = search_json()
            # an aggregate code where a part should be: halts
            bad = codelist_json([(1, "Owned: Owns outright", 1001, "derived"),
                                 (0, "Total: All households", None, "total")])
            self.api[m.codelist_url(m.DATASETS[P])] = bad
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("aggregate", text)

    def test_a_stranded_year_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute("DELETE FROM public.zz_s3b_live")
            self.assertEqual(pe.status(cur, m.profile())["live_missing"], [P])
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn("live-missing", text)
            self.assertEqual(count(cur, "zz_s3b_live"), N)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.release()
            url = m.data_url(m.category_codes(codelist_json()))
            self.final[url] = url + "&final=1"
            self.ok(cur, ["load", "--commit"])
            (_, src, _, _), = ledger(cur)
            self.assertTrue(src.startswith(url + "&final=1 (census_year"))

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            ov = {("E06000002", "owned_outright"): 17225,
                  ("E06000002", "total_households"):
                  counts("E06000002")["total_households"] + 1}
            self.release(last="2024-06-01 09:30:00", overrides=ov)
            self.ok(cur, ["load", "--commit", "--accept-reissue", P])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "E06000002"), 17225)
            text, logged = self.ok(cur, ["restore-edition", P, "1",
                                         "--commit"])
            self.assertIn("as edition 3", text)
            self.assertEqual(editions(cur)[-1], (3, 2, N))
            logged.assert_called_once()
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live_val(cur, "E06000002"), 17224)
            rc, text, _ = self.run_main(cur, ["restore-edition", P, "3"])
            self.assertEqual(rc, "halt")
            self.assertIn("is the tip", text)


class Migrate(Fixture):

    def seed_legacy(self, cur, **kw):
        """Held-style live rows built from a fixture CSV: two source variants
        and two loaded_at values, as the real table holds them."""
        csvp, defp = self.release(register=False, **kw)
        f = m.read_csv(csvp)
        recs = m.build_records(f, lambda c: c)
        for i, r in enumerate(recs):
            cur.execute(
                "INSERT INTO public.zz_s3b_live (lad24cd, "
                + ", ".join(m.VALUES) + ", census_year, source, loaded_at) "
                "VALUES (" + ", ".join(["%s"] * (len(m.VALUES) + 4)) + ")",
                [r["lad24cd"]] + [r[c] for c in m.VALUES]
                + [2021, SRC_B if i == 1 else SRC_A,
                   LOADED_B if i == 1 else LOADED_A])
        url = m.data_url(m.category_codes(codelist_json()))
        files = {"name": csvp.name, "content_sha256": m.rows_content_sha(recs),
                 "bytes_sha256": m.content_sha256(csvp), "url": url}
        return csvp, defp, files

    def legacy(self, cur):
        return m.live_state(cur)[:2]

    def loaded(self, cur):
        return m.live_state(cur)[2]

    def migrate(self, cur, csvp, defp, files, write=True, **kw):
        with quiet() as out:
            plan = m.migrate_legacy(
                cur, csvp, defp, write=write,
                legacy=kw.get("legacy") or self.legacy(cur),
                loaded=kw.get("loaded") or self.loaded(cur), files=files)
        return plan, out.getvalue()

    def test_preview_before_ddl_reads_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            csvp, defp, files = self.seed_legacy(cur)
            plan, text = self.migrate(cur, csvp, defp, files, write=False)
            self.assertIn("0 differences", text)
            self.assertEqual(plan["cells"], N * 9)
            self.assertFalse(pe.table_exists(cur, "zz_s3b_editions"))
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, csvp, defp, files)
            self.assertIn("ddl", str(e.exception.code))

    def test_load_preview_before_ddl_compares_with_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            csvp, defp, _ = self.seed_legacy(cur)
            text, _ = self.ok(cur, ["load", "--file", csvp, "--def", defp])
            self.assertIn("compares the file with the LIVE table", text)
            self.assertIn(f"{P}: unchanged", text)
            rc, text, _ = self.run_main(cur, ["load", "--file", csvp, "--def",
                                              defp, "--commit"])
            self.assertEqual(rc, "halt")
            self.assertIn("ddl --commit", text)

    def test_proof_passes_and_byte_identical_rereads_are_unchanged(self):
        with rolled_back(self.conn) as cur:
            csvp, defp, files = self.seed_legacy(cur)
            before = live_md5(cur)
            plan, text = self.migrate(cur, csvp, defp, files)
            self.assertIn("0 differences", text)
            self.assertIn("parts sum to the publisher's total", text)
            self.assertEqual(plan["rows"], N)
            self.assertEqual(editions(cur), [(1, None, N)])
            cur.execute("SELECT DISTINCT source_file, release_label, "
                        "published_date FROM public.zz_s3b_editions")
            (sf, label, pd_), = cur.fetchall()
            self.assertEqual(sf, f"as loaded: {csvp.name}; census_year 2021; "
                                 "NOMIS LastUpdated 2023-01-05 09:30:00")
            self.assertEqual(label, core.AS_LOADED_LATEST_LABEL)
            self.assertEqual(pd_, date(2026, 8, 20))
            self.assertEqual(ledger(cur), [(
                P, f"{files['url']} (census_year 2021; NOMIS LastUpdated "
                   "2023-01-05 09:30:00)", "unchanged", 1)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            # the same content on every path: unchanged against edition 1
            self.urls[m.def_url(m.DATASETS[P])] = defp
            self.urls[files["url"]] = csvp
            text, _ = self.ok(cur, ["load"])
            self.assertIn("nothing compared", text)
            for argv in (["load", "--recheck", P],
                         ["load", "--file", csvp, "--def", defp],
                         ["load", "--file", csvp, "--def", defp, "--commit"],
                         ["load", "--file", csvp, "--def", defp, "--recheck",
                          P, "--commit"],
                         ["load", "--recheck", P, "--commit"]):
                with self.subTest(argv=argv):
                    text, _ = self.ok(cur, argv)
                    self.assertIn(f"{P}: unchanged", text)
                    self.assertNotIn("REJECTED", text)
                    self.assertNotIn("two sources claim", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual([o for _, _, o, _ in ledger(cur)],
                             ["unchanged"] * 4)
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)
            # live untouched: both source variants and both loaded_at kept
            self.assertEqual(live_md5(cur), before)
            cur.execute("SELECT COUNT(DISTINCT source), COUNT(DISTINCT "
                        "loaded_at) FROM public.zz_s3b_live")
            self.assertEqual(cur.fetchone(), (2, 2))

    def test_a_planted_held_difference_stops_and_rolls_back(self):
        with rolled_back(self.conn) as cur:
            csvp, defp, files = self.seed_legacy(cur)
            cur.execute("UPDATE public.zz_s3b_live SET owned_outright = "
                        "owned_outright + 1, total_households = "
                        "total_households + 1 WHERE lad24cd = 'E06000001'")
            with mock.patch.object(m, "LEGACY_LIVE", self.legacy(cur)), \
                    mock.patch.object(m, "LEGACY_LOADED", self.loaded(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                rc, text, logged = self.run_main(
                    cur, ["migrate-legacy", "--file", csvp, "--def", defp,
                          "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("proof failed", text)
            self.assertIn("E06000001", text)
            self.assertEqual(count(cur, "zz_s3b_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)
            logged.assert_not_called()

    def test_the_command_previews_then_commits(self):
        with rolled_back(self.conn) as cur:
            csvp, defp, files = self.seed_legacy(cur)
            argv = ["migrate-legacy", "--file", csvp, "--def", defp]
            with mock.patch.object(m, "LEGACY_LIVE", self.legacy(cur)), \
                    mock.patch.object(m, "LEGACY_LOADED", self.loaded(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                text, logged = self.ok(cur, argv)
                self.assertIn("PREVIEW", text)
                self.assertEqual(count(cur, "zz_s3b_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, argv + ["--simulate"])
                self.assertIn("SIMULATION", text)
                self.assertEqual(count(cur, "zz_s3b_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, argv + ["--commit"])
            self.assertEqual(editions(cur), [(1, None, N)])
            logged.assert_called_once()

    def test_a_wrong_content_live_state_or_loaded_at_stops(self):
        with rolled_back(self.conn) as cur:
            csvp, defp, files = self.seed_legacy(cur)
            bad = dict(files, content_sha256="0" * 64)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, csvp, defp, bad)
            self.assertIn("expected 00000000", str(e.exception.code))
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, csvp, defp, files, legacy=(N, "x"))
            self.assertIn("not as surveyed", str(e.exception.code))
            loaded = dict(self.loaded(cur))
            cur.execute("UPDATE public.zz_s3b_live SET loaded_at = now() "
                        "WHERE lad24cd = 'E06000001'")
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, csvp, defp, files, loaded=loaded)
            self.assertIn("loaded_at", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s3b_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)

    def test_a_definition_of_another_release_stops(self):
        with rolled_back(self.conn) as cur:
            csvp, _, files = self.seed_legacy(cur)
            defp = write_def(self.root / "later.json",
                             last="2024-06-01 09:30:00")
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, csvp, defp, files)
            self.assertIn("LastUpdated", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s3b_editions"), 0)

    def test_parts_not_summing_stop_the_migration(self):
        with rolled_back(self.conn) as cur:
            ov = {("E06000002", "total_households"): 60000}
            csvp, defp, files = self.seed_legacy(cur, overrides=ov)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, csvp, defp, files)
            self.assertIn("E06000002", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s3b_editions"), 0)

    def test_a_non_empty_editions_table_stops(self):
        with rolled_back(self.conn) as cur:
            csvp, defp, files = self.seed_legacy(cur)
            legacy, loaded = self.legacy(cur), self.loaded(cur)
            self.migrate(cur, csvp, defp, files)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, csvp, defp, files, legacy=legacy,
                             loaded=loaded)
            self.assertIn("runs once", str(e.exception.code))


class Commands(Fixture):

    def test_ddl_preview_then_commit_then_status(self):
        with rolled_back(self.conn, ddl=False) as cur:
            text, _ = self.ok(cur, ["ddl"])
            self.assertIn("DRY RUN", text)
            self.assertFalse(pe.table_exists(cur, "zz_s3b_editions"))
            self.ok(cur, ["ddl", "--commit"])
            for t in ("zz_s3b_editions", LEDGER):
                self.assertTrue(pe.table_exists(cur, t), t)
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 0, text)

    def test_status_without_editions_table_is_clean_and_creates_nothing(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1)
            self.assertIn("not exist yet", text)
            self.assertFalse(pe.table_exists(cur, "zz_s3b_editions"))
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
            self.assertIn("NOMIS LastUpdated 2023-01-05 09:30:00", text)
            self.assertIn("ledger latest: new", text)

    def test_spec_types_match_the_live_table(self):
        with rolled_back(self.conn, ddl=False) as cur:
            cur.execute("SELECT column_name, data_type, is_generated FROM "
                        "information_schema.columns WHERE table_schema = "
                        "'public' AND table_name = 'la_tenure_2021'")
            live = {r[0]: r[1:] for r in cur.fetchall()}
            for col in m.VALUES:
                self.assertEqual(live[col], ("integer", "NEVER"), col)
            for gen in ("social_rented_total", "private_rented_total"):
                self.assertEqual(live[gen][1], "ALWAYS")
            self.assertEqual(live["census_year"][0], "integer")
            self.assertEqual(live["source"][0], "text")
            self.assertEqual(set(PARTS) | {m.TOTAL}, set(m.VALUES))


if __name__ == "__main__":
    unittest.main()
