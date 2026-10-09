"""Tests for the S11 loader in s11_cqc_editions (download, the commands, the
one-off migration).

No network: the CQC page, the download and postcodes.io are stubbed, and
every file is an .ods built by the test (write_ods from test_s11_cqc_pure).
Database tests run on a connection from _db.get_conn() inside a transaction
that is always rolled back (rolled_back below); nothing here commits: the
commands get a stand-in connection (_Borrowed) whose commit only counts, and a
cursor proxy whose .connection is a stand-in too, so the engine's per-snapshot
commit never reaches the real connection. The only tables touched are the
throwaway zz_s11_* tables (a copy of SPEC named zz_s11, a seeded legacy table
and its unresolved table), created inside that transaction (refused if one
already exists), so they never persist. la_boundaries is replaced by test
squares and the run log is stubbed; the real la_code_lookup recode rows are
read (never written).
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

import period_editions as pe  # noqa: E402
import s11_cqc_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s11_cqc_pure import (Api, loc, ods_name, page_of,  # noqa: E402
                               square, write_ods)

ZZ = dataclasses.replace(
    m.SPEC, name="zz_s11", live_table="zz_s11_live",
    editions_table="zz_s11_editions",
    expected_rows_per_period=m.tip_row_count("zz_s11_editions"))
LIVE, ED = "zz_s11_live", "zz_s11_editions"
LEDGER = "zz_s11_editions_file_checks"
UNRES = "zz_s11_live_unresolved"
LEGACY, RENAMED, LEG_UNRES = ("zz_s11_legacy", "zz_s11_legacy_old",
                              "zz_s11_legacy_unresolved")
ALL = (LIVE, ED, LEDGER, UNRES, LEGACY, RENAMED, LEG_UNRES)

A, B, C = "E06000001", "E06000002", "E06000003"
BOUNDS = [(A, square(-1.6, 53.30, -1.4, 53.45)),
          (B, square(-1.6, 53.45, -1.4, 53.60)),
          (C, square(-1.4, 53.30, -1.2, 53.45))]
POINT = {0: ("53.380000", "-1.500000"), 1: ("53.500000", "-1.500000"),
         2: ("53.380000", "-1.300000")}
N = 60
D0, D1, D2, D3 = "2026-07-01", "2026-08-04", "2026-09-01", "2026-10-01"
NOXY = "1-NOXY"                       # no coordinates, unknown postcode


def url(d, folder=None):
    return (f"https://www.cqc.org.uk/system/files/{folder or d[:7]}/"
            + ods_name(date.fromisoformat(d)))


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _FakeConn:
    def __init__(self, real):
        self.encoding = real.encoding
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class _Cur:
    def __init__(self, cur):
        self._cur = cur
        self.connection = _FakeConn(cur.connection)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _Borrowed:
    """A stand-in connection: commit counts; rollback, when savepoint is
    set, rolls the real transaction back to it (what a real rollback of
    the command's own transaction does)."""

    def __init__(self, cur, savepoint=None):
        self.cur = _Cur(cur)
        self.commits = 0
        self.savepoint = savepoint
        if savepoint:
            cur.execute(f"SAVEPOINT {savepoint}")

    @contextmanager
    def cursor(self):
        yield self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        if self.savepoint:
            self.cur.execute(f"ROLLBACK TO SAVEPOINT {self.savepoint}")

    def close(self):
        pass


@contextmanager
def rolled_back(conn):
    cur = conn.cursor()
    try:
        cur.execute("SELECT " + ", ".join(f"to_regclass('public.{t}')"
                                          for t in ALL))
        if any(cur.fetchone()):
            raise RuntimeError("a zz_s11 table already exists as a real "
                               "table; refusing to run")
        m.create_all(cur, ZZ)
        pe.create_file_checks(cur, m._profile(ZZ))
        yield cur
    finally:
        cur.close()
        conn.rollback()


def count(cur, table, where="", args=None):
    cur.execute(f"SELECT COUNT(*) FROM public.{table} {where}", args)
    return cur.fetchone()[0]


def editions(cur, period=None):
    cur.execute(f"SELECT edition, supersedes, COUNT(*) FROM public.{ED}"
                + (" WHERE snapshot_date = %s" if period else "")
                + " GROUP BY 1, 2 ORDER BY 1", (period,) if period else None)
    return cur.fetchall()


def ledger(cur, period=None):
    cur.execute(f"SELECT source_file, outcome, edition FROM public.{LEDGER}"
                + (" WHERE snapshot_date = %s" if period else "")
                + " ORDER BY id", (period,) if period else None)
    return [(s.rsplit("/", 2)[-2] + "/" + s.rsplit("/", 1)[-1], o, e)
            for s, o, e in cur.fetchall()]


def unresolved(cur, period):
    cur.execute(f"SELECT edition, location_id FROM public.{UNRES} WHERE "
                "snapshot_date = %s ORDER BY 1, 2", (period,))
    return cur.fetchall()


def live(cur, period, col="latest_overall_rating"):
    cur.execute(f"SELECT location_id, {col} FROM public.{LIVE} WHERE "
                "snapshot_date = %s", (period,))
    return dict(cur.fetchall())


def snapshot_of(cur):
    return tuple(count(cur, t) for t in (LIVE, ED, LEDGER, UNRES))


def tail(u):
    return u.rsplit("/", 2)[-2] + "/" + u.rsplit("/", 1)[-1]


class _Base(unittest.TestCase):
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
        self.files = {}
        self.fetched = []
        self.n = 0
        self.api = Api()

    def locations(self, d, ids=None, over=None, extra=()):
        out = []
        for i in (range(N) if ids is None else ids):
            lat, lon = POINT[i % 3]
            v = loc(f"1-{i}", lat, lon, rating="Good",
                    supported_living="Y" if i % 10 == 0 else "")
            for k, val in (over or {}).get(i, {}).items():
                v[k] = val
            out.append(v)
        out.append(loc(NOXY, "", "", postcode="ZZ9 9ZZ"))
        out += list(extra)
        return out

    def make(self, d, link=None, **kw):
        """Write the .ods for snapshot d and register it under link."""
        self.n += 1
        folder = Path(self.tmp.name) / str(self.n)
        folder.mkdir()
        dd = date.fromisoformat(d)
        path = write_ods(folder / ods_name(dd),
                         as_at=f"{dd.day:02d} {m._MONTHS[dd.month - 1].title()}"
                               f" {dd.year}",
                         locations=self.locations(d, **kw))
        self.files[link or url(d)] = path
        return path

    def run_main(self, cur, argv, *, page=None, table=True, file=None,
                 savepoint=None):
        """main(argv) on the throwaway tables, the page stubbed to offer
        `page` (a link). Returns (rc or 'halt', text, log mock)."""
        borrowed = _Borrowed(cur, savepoint)
        args = list(argv) + (["--file", str(file)] if file else [])

        def fetch_file(link, dest=None, session=None):
            self.fetched.append(link)
            return self.files[link]

        html = page_of((page, "Care directory with filters (02 October "
                              "2026)")) if page else page_of()
        out = io.StringIO()
        with mock.patch.object(m, "SPEC", ZZ), \
                mock.patch.object(m, "LEGACY", LEGACY), \
                mock.patch.object(m, "LEGACY_RENAMED", RENAMED), \
                mock.patch.object(m, "LEGACY_UNRESOLVED", LEG_UNRES), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "load_boundaries", return_value=BOUNDS), \
                mock.patch.object(m, "_api", return_value=self.api), \
                mock.patch.object(m, "fetch_page", return_value=html), \
                mock.patch.object(m, "fetch_file", side_effect=fetch_file), \
                mock.patch.object(m, "log_run") as logged, \
                mock.patch.object(m, "table_exists",
                                  side_effect=lambda c, t: table
                                  and pe.table_exists(c, t)), \
                contextlib.redirect_stdout(out):
            try:
                rc = m.main(args)
            except SystemExit as e:
                return "halt", f"{out.getvalue()}\n{e.code}", logged
        return rc, out.getvalue(), logged

    def seed(self, cur, dates=(D1,)):
        for d in dates:
            self.make(d)
            rc, text, _ = self.run_main(cur, ["load", "--commit"], page=url(d))
            self.assertEqual(rc, 0, text)
        self.fetched.clear()


class Loader(_Base):
    def test_new_snapshot_stores_all_four_in_one_transaction(self):
        with rolled_back(self.conn) as cur:
            self.make(D1)
            rc, text, logged = self.run_main(cur, ["load", "--commit"],
                                             page=url(D1))
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [url(D1)])
            self.assertIn(f"{D1}: new", text)
            self.assertEqual(editions(cur, D1), [(1, None, N)])
            self.assertEqual(count(cur, LIVE), N)
            self.assertEqual(unresolved(cur, D1), [(1, NOXY)])
            self.assertEqual(ledger(cur, D1), [(tail(url(D1)), "new", 1)])
            cur.execute(f"SELECT DISTINCT source_file FROM public.{LIVE}")
            self.assertEqual(cur.fetchall(), [(url(D1),)])
            cur.execute(f"SELECT DISTINCT release_label, source_file, "
                        f"published_date FROM public.{ED}")
            (label, src, pub), = cur.fetchall()
            self.assertEqual(src, url(D1))
            self.assertIn(f"as at {D1}", label)
            self.assertIn("published 2026-10-02", label)
            self.assertIn(m.content_sha256(self.files[url(D1)])[:16], label)
            self.assertEqual(pub, date(2026, 10, 2))      # the page label
            logged.assert_called_once()
            self.assertIn("1 unresolved", logged.call_args[0][2])
            self.assertTrue(m.status(cur, ZZ)["ok"])
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.make(D2)
            with mock.patch.object(m, "insert_live",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(cur, ["load", "--commit"],
                                                 page=url(D2))
            self.assertEqual(rc, 1, text)
            self.assertIn("boom", text)
            self.assertEqual(editions(cur, D2), [])
            self.assertEqual(count(cur, LIVE, "WHERE snapshot_date = %s",
                                   (D2,)), 0)
            self.assertEqual(unresolved(cur, D2), [])
            self.assertEqual(ledger(cur, D2), [])
            logged.assert_not_called()

    def test_unchanged_recheck_records_the_ledger_only_on_commit(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = snapshot_of(cur)
            rc, text, logged = self.run_main(
                cur, ["load", "--recheck", D1], page=url(D1))
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [url(D1)])
            self.assertIn(f"{D1}: unchanged", text)
            self.assertEqual(snapshot_of(cur), before)
            logged.assert_not_called()
            rc, text, logged = self.run_main(
                cur, ["load", "--recheck", D1, "--commit"], page=url(D1))
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, D1), [(1, None, N)])
            self.assertEqual(ledger(cur, D1),
                             [(tail(url(D1)), "new", 1),
                              (tail(url(D1)), "unchanged", 1)])
            self.assertEqual(unresolved(cur, D1), [(1, NOXY)])
            logged.assert_called_once()

    def test_same_url_already_in_the_ledger_is_not_fetched(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = snapshot_of(cur)
            rc, text, logged = self.run_main(cur, ["load", "--commit"],
                                             page=url(D1))
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [])
            self.assertIn("already in the ledger", text)
            self.assertIn("nothing to fetch", text)
            self.assertEqual(snapshot_of(cur), before)
            logged.assert_not_called()

    def test_recheck_changed_values_edition_2_live_until_refresh(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, (D1, D2))
            before = live(cur, D2)
            reissue = url(D2, "2026-09-reissue")
            self.make(D2, reissue, over={
                3: {"Location Latest Overall Rating": "Outstanding"},
                7: {"Location Latest Overall Rating": "Inadequate"}})
            rc, text, _ = self.run_main(cur, ["load", "--commit"],
                                        page=reissue)
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [reissue])
            self.assertIn(f"{D2}: revised, 2 areas changed", text)
            self.assertEqual(editions(cur, D2), [(1, None, N), (2, 1, N)])
            self.assertEqual(live(cur, D2), before)
            self.assertEqual(unresolved(cur, D2), [(1, NOXY), (2, NOXY)])
            self.assertEqual(ledger(cur, D2)[-1],
                             (tail(reissue), "revised", 2))
            self.assertEqual(m.status(cur, ZZ)["pending_refresh"], [D2])
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, 0, text)
            after = live(cur, D2)
            self.assertEqual(after["1-3"], "Outstanding")
            self.assertEqual(after["1-7"], "Inadequate")
            self.assertEqual(after["1-4"], before["1-4"])
            # the source is the latest edition's on EVERY row of the snapshot
            cur.execute(f"SELECT DISTINCT source_file, COUNT(*) FROM "
                        f"public.{LIVE} WHERE snapshot_date = %s GROUP BY 1",
                        (D2,))
            self.assertEqual(cur.fetchall(), [(reissue, N)])
            cur.execute(f"SELECT DISTINCT source_file FROM public.{LIVE} "
                        "WHERE snapshot_date = %s", (D1,))
            self.assertEqual(cur.fetchall(), [(url(D1),)])
            self.assertTrue(m.status(cur, ZZ)["ok"])

    def test_recheck_with_a_changed_location_set_is_rejected(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = snapshot_of(cur)
            reissue = url(D1, "2026-08-reissue")
            self.make(D1, reissue, ids=range(N - 1))
            rc, text, logged = self.run_main(cur, ["load", "--commit"],
                                             page=reissue)
            self.assertEqual(rc, 1, text)
            self.assertIn("REJECTED", text)
            self.assertIn("location set differs", text)
            self.assertEqual(snapshot_of(cur), before)
            logged.assert_not_called()

    def test_older_as_at_halts_on_the_page_and_with_file(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, (D1, D2))
            before = snapshot_of(cur)
            f = self.make(D0)
            for argv, kw in ((["load"], {"page": url(D0)}),
                             (["load", "--commit"], {"page": url(D0)}),
                             (["load"], {"file": f}),
                             (["load", "--commit"], {"file": f}),
                             (["load", "--simulate"], {"file": f})):
                rc, text, _ = self.run_main(cur, argv, **kw)
                self.assertEqual(rc, "halt", text)
                self.assertIn(f"as at {D0}, earlier than the latest held "
                              f"snapshot {D2}", text)
                self.assertIn("--allow-older-file", text)
                self.assertEqual(snapshot_of(cur), before)
            rc, text, logged = self.run_main(
                cur, ["load", "--commit", "--allow-older-file"], file=f)
            self.assertEqual(rc, 0, text)
            self.assertIn("NOTE: --allow-older-file given", text)
            self.assertEqual(editions(cur, D0), [(1, None, N)])
            self.assertIn("--allow-older-file", logged.call_args[0][2])
            cur.execute(f"SELECT DISTINCT release_label, source_file FROM "
                        f"public.{ED} WHERE snapshot_date = %s", (D0,))
            (label, src), = cur.fetchall()
            self.assertIn("--allow-older-file", label)
            self.assertEqual(src, f.resolve().as_posix())

    def test_held_older_snapshot_from_a_file_needs_recheck_or_override(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, (D1, D2))
            f = self.make(D1, over={3: {"Location Latest Overall Rating":
                                        "Outstanding"}})
            rc, text, _ = self.run_main(cur, ["load", "--commit"], file=f)
            self.assertEqual(rc, "halt", text)
            rc, text, _ = self.run_main(
                cur, ["load", "--commit", "--recheck", D1], file=f)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, D1), [(1, None, N), (2, 1, N)])

    def test_rejected_snapshot_stores_nothing_and_exits_1(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = snapshot_of(cur)
            extra = [loc(f"1-X{i}", "", "", postcode=f"ZZ{i} 1ZZ")
                     for i in range(10)]                # 11 unresolved
            self.make(D2, extra=extra)
            for flag in ("--commit", "--simulate"):
                rc, text, logged = self.run_main(cur, ["load", flag],
                                                 page=url(D2))
                self.assertEqual(rc, 1, text)
                self.assertIn("11 unresolved locations (limit 10)", text)
                self.assertIn(f"REJECTED {D2}, nothing stored; exit 1", text)
                self.assertEqual(snapshot_of(cur), before)
                logged.assert_not_called()
            # an authority with no location
            self.make(D2, ids=[i for i in range(N + 30) if i % 3 != 2])
            rc, text, _ = self.run_main(cur, ["load", "--commit"],
                                        page=url(D2))
            self.assertEqual(rc, 1, text)
            self.assertIn(f"authorities with no location ['{C}']", text)
            self.assertEqual(snapshot_of(cur), before)

    def test_zero_to_null_on_a_recheck_needs_acknowledge(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)                  # beds 0 on every location
            before = snapshot_of(cur)
            reissue = url(D1, "2026-08-reissue")
            self.make(D1, reissue, over={5: {"Care homes beds": ""}})
            rc, text, logged = self.run_main(cur, ["load", "--commit"],
                                             page=reissue)
            self.assertEqual(rc, 1, text)
            self.assertIn("0 to NULL", text)
            self.assertIn(f"--acknowledge {D1}", text)
            self.assertEqual(snapshot_of(cur), before)
            rc, text, _ = self.run_main(
                cur, ["load", "--commit", "--acknowledge", D2], page=reissue)
            self.assertEqual(rc, "halt", text)
            rc, text, logged = self.run_main(
                cur, ["load", "--commit", "--acknowledge", D1], page=reissue)
            self.assertEqual(rc, 0, text)
            self.assertIn("ACKNOWLEDGED", text)
            self.assertEqual(editions(cur, D1), [(1, None, N), (2, 1, N)])
            cur.execute(f"SELECT DISTINCT release_label FROM public.{ED} "
                        "WHERE snapshot_date = %s AND edition = 2", (D1,))
            self.assertIn("ACKNOWLEDGED", cur.fetchone()[0])
            self.assertIn("ACKNOWLEDGED", logged.call_args[0][2])
            cur.execute(f"SELECT care_homes_beds FROM public.{ED} WHERE "
                        "snapshot_date = %s AND edition = 2 AND location_id "
                        "= '1-5'", (D1,))
            self.assertEqual(cur.fetchone(), (None,))

    def test_preview_and_simulate_write_nothing(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.make(D2)
            before = snapshot_of(cur)
            rc, text, logged = self.run_main(cur, ["load"], page=url(D2))
            self.assertEqual(rc, 0, text)
            self.assertIn(f"{D2}: new", text)
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            self.assertEqual(snapshot_of(cur), before)
            logged.assert_not_called()
            rc, text, logged = self.run_main(cur, ["load", "--simulate"],
                                             page=url(D2))
            self.assertEqual(rc, 0, text)
            self.assertIn("SIMULATION", text)
            self.assertEqual(snapshot_of(cur), before)
            logged.assert_not_called()

    def test_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, (D1, D2))
            before = snapshot_of(cur)
            for kw in ({"page": url(D2)}, {"file": self.files[url(D2)]}):
                rc, text, logged = self.run_main(cur, ["load", "--commit"],
                                                 **kw)
                self.assertEqual(rc, 0, text)
                self.assertEqual(snapshot_of(cur), before)
                logged.assert_not_called()
            self.assertEqual(self.fetched, [])
            self.assertIn("already checked", text)

    def test_stranded_snapshot_is_repaired(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            cur.execute(f"DELETE FROM public.{LIVE} WHERE snapshot_date = %s",
                        (D1,))
            self.assertEqual(m.status(cur, ZZ)["live_missing"], [D1])
            rc, text, _ = self.run_main(cur, ["load", "--commit"],
                                        page=url(D1))
            self.assertEqual(rc, 0, text)
            self.assertEqual(self.fetched, [url(D1)])
            self.assertIn("no new edition", text)
            self.assertEqual(count(cur, LIVE), N)
            self.assertEqual(editions(cur, D1), [(1, None, N)])
            self.assertEqual(ledger(cur, D1)[-1][1], "live-missing")
            self.assertEqual(unresolved(cur, D1), [(1, NOXY)])
            self.assertTrue(m.status(cur, ZZ)["ok"])

    def test_status_and_ddl(self):
        with rolled_back(self.conn) as cur:
            rc, text, _ = self.run_main(cur, ["status"], table=False)
            self.assertEqual(rc, 1, text)
            self.assertIn("does not exist yet", text)
            self.seed(cur)
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 0, text)
            self.assertIn(f"latest snapshot {D1} (edition 1): 1 unresolved",
                          text)
            self.assertIn(f"{LEGACY}: absent", text)
        with rolled_back(self.conn) as cur:
            for t in (LEDGER, UNRES, LIVE, ED):
                cur.execute(f"DROP TABLE public.{t}")
            rc, text, _ = self.run_main(cur, ["ddl"])
            self.assertEqual(rc, 0, text)
            self.assertFalse(pe.table_exists(cur, ED))
            rc, text, _ = self.run_main(cur, ["ddl", "--commit"])
            self.assertEqual(rc, 0, text)
            for t in (LEDGER, UNRES, LIVE, ED):
                self.assertTrue(pe.table_exists(cur, t), t)
            cur.execute("SELECT indexdef FROM pg_indexes WHERE tablename = "
                        "%s AND indexname = %s", (LIVE, f"{LIVE}_location_idx"))
            self.assertIn("snapshot_date DESC", cur.fetchone()[0])
            for t in (UNRES, LEDGER):
                cur.execute("SAVEPOINT zz_ro")
                with self.assertRaises(Exception):
                    cur.execute(f"TRUNCATE public.{t}")
                cur.execute("ROLLBACK TO SAVEPOINT zz_ro")

    def test_writing_without_tables_halts(self):
        with rolled_back(self.conn) as cur:
            self.make(D1)
            rc, text, _ = self.run_main(cur, ["load", "--commit"],
                                        page=url(D1), table=False)
            self.assertEqual(rc, "halt", text)
            self.assertIn("ddl --commit", text)

    def test_identity_failure_halts(self):
        with rolled_back(self.conn) as cur:
            p = Path(self.tmp.name) / ods_name(date(2026, 8, 4))
            write_ods(p, as_at="05 August 2026",
                      locations=self.locations(D1))
            rc, text, _ = self.run_main(cur, ["load"], file=p)
            self.assertEqual(rc, "halt", text)
            self.assertIn("identity", text)


class Migration(_Base):
    """migrate-legacy on a seeded legacy table: July D0, August D1,
    September D2. 1-62 is only in July (the July residue), 1-1 is gone in
    September (the August residue), 1-0 skips August and returns, 1-61 is
    new in September; NOXY is unresolved in every file."""

    FILES = {D0: dict(ids=[i for i in range(N) if i != 61] + [62]),
             D1: dict(ids=range(1, N), over={
                 4: {"Location Latest Overall Rating": "Outstanding"}}),
             D2: dict(ids=[0] + list(range(2, N)) + [61])}

    def seed_legacy(self, cur):
        paths, by = {}, {}
        for d, kw in self.FILES.items():
            paths[d] = self.make(d, **kw)
            rows, _ = m.parse_locations(paths[d])
            mapped, _ = m.map_locations(rows, BOUNDS, {}, {}, api=Api())
            by[d] = {r["location_id"]: r for r in mapped}
        cur.execute(f"CREATE TABLE public.{LEGACY} (LIKE public.cqc_locations "
                    "INCLUDING ALL)")
        cur.execute(f"CREATE TABLE public.{LEG_UNRES} (LIKE "
                    "public.cqc_unresolved_locations INCLUDING ALL)")
        dates = sorted(by)
        last = {}
        for d in dates:
            for k in by[d]:
                last[k] = d
        cols = ("location_id",) + m.VALUE_COLUMNS
        for k, d in last.items():
            i = dates.index(d)
            nxt = dates[i + 1] if i + 1 < len(dates) else None
            r = by[d][k]
            cur.execute(
                f"INSERT INTO public.{LEGACY} ({', '.join(cols)}, is_active, "
                "deregistered_seen_date, source_file_date) VALUES ("
                + ", ".join(["%s"] * (len(cols) + 3)) + ")",
                [r[c] for c in cols] + [nxt is None, nxt, d])
        cur.execute(f"INSERT INTO public.{LEG_UNRES} (location_id, "
                    "location_name, postcode, reason, first_seen_edition, "
                    "last_seen_edition, editions_seen) VALUES (%s, 'x', "
                    "'ZZ9 9ZZ', 'postcode absent', %s, %s, 2)",
                    (NOXY, D1, D2))
        self.runs = {d: [(d, len(by[d]))] for d in dates}
        return [paths[D2], paths[D0], paths[D1]]

    def migrate(self, cur, files, argv=("--commit",)):
        with mock.patch.object(m, "legacy_run_dates",
                               side_effect=lambda c: self.runs):
            return self.run_main(cur, ["migrate-legacy", *map(str, files),
                                       *argv], savepoint="zz_mig")

    def test_migration_stores_three_snapshots_and_swaps_in_the_view(self):
        with rolled_back(self.conn) as cur:
            files = self.seed_legacy(cur)
            cur.execute(f"SELECT * FROM public.{LEGACY} ORDER BY 1")
            legacy_rows = cur.fetchall()
            rc, text, _ = self.migrate(cur, files, ())
            self.assertEqual(rc, 0, text)
            self.assertIn("would be run in the commit", text)
            self.assertIn("PREVIEW", text)
            self.assertEqual(snapshot_of(cur), (0, 0, 0, 0))
            self.assertEqual(m.relkind(cur, LEGACY), "r")
            rc, text, logged = self.migrate(cur, files)
            self.assertEqual(rc, 0, text)
            self.assertEqual(editions(cur, D0), [(1, None, N + 1)])
            self.assertEqual(editions(cur, D1), [(1, None, N - 1)])
            self.assertEqual(editions(cur, D2), [(1, None, N)])
            for d in (D0, D1, D2):
                self.assertEqual(unresolved(cur, d), [(1, NOXY)])
                self.assertEqual(ledger(cur, d)[0][1:], ("new", 1))
            self.assertEqual(m.relkind(cur, LEGACY), "v")
            self.assertEqual(m.relkind(cur, RENAMED), "r")
            self.assertEqual(m.view_differences(cur, LEGACY, RENAMED), (0, 0))
            self.assertEqual(m.w1_counts(cur, LEGACY),
                             m.w1_counts(cur, RENAMED))
            cur.execute(f"SELECT * FROM public.{RENAMED} ORDER BY 1")
            self.assertEqual(cur.fetchall(), legacy_rows)
            cur.execute(f"SELECT is_active, deregistered_seen_date, "
                        f"source_file_date FROM public.{LEGACY} WHERE "
                        "location_id IN ('1-62', '1-1', '1-0') ORDER BY "
                        "location_id")
            self.assertEqual(cur.fetchall(), [
                (True, None, date(2026, 9, 1)),
                (False, date(2026, 9, 1), date(2026, 8, 4)),
                (False, date(2026, 8, 4), date(2026, 7, 1))])
            cur.execute(f"SELECT release_label FROM public.{ED} WHERE "
                        "snapshot_date = %s LIMIT 1", (D0,))
            self.assertIn("reconstructed from the file", cur.fetchone()[0])
            cur.execute(f"SELECT release_label FROM public.{ED} WHERE "
                        "snapshot_date = %s LIMIT 1", (D2,))
            self.assertIn("as loaded", cur.fetchone()[0])
            cur.execute("SELECT obj_description(%s::regclass)", (LEG_UNRES,))
            self.assertIn("FROZEN", cur.fetchone()[0])
            logged.assert_called_once()
            self.assertTrue(m.status(cur, ZZ)["ok"])
            # it runs once
            rc, text, _ = self.migrate(cur, files)
            self.assertEqual(rc, "halt", text)

    def assert_rolled_back(self, cur):
        self.assertEqual(snapshot_of(cur), (0, 0, 0, 0))
        self.assertEqual(m.relkind(cur, LEGACY), "r")
        self.assertIsNone(m.relkind(cur, RENAMED))

    def test_a_residue_difference_stops(self):
        with rolled_back(self.conn) as cur:
            files = self.seed_legacy(cur)
            cur.execute(f"UPDATE public.{LEGACY} SET provider_name = 'Other' "
                        "WHERE location_id = '1-62'")
            rc, text, logged = self.migrate(cur, files)
            self.assertEqual(rc, "halt", text)
            self.assertIn("differ from the file", text)
            self.assertIn("provider_name", text)
            self.assert_rolled_back(cur)
            logged.assert_not_called()

    def test_an_unresolved_mismatch_stops(self):
        with rolled_back(self.conn) as cur:
            files = self.seed_legacy(cur)
            cur.execute(f"UPDATE public.{LEG_UNRES} SET resolved_at = now()")
            rc, text, _ = self.migrate(cur, files)
            self.assertEqual(rc, "halt", text)
            self.assertIn("differ from the open rows", text)
            self.assert_rolled_back(cur)

    def test_a_run_log_count_mismatch_stops(self):
        with rolled_back(self.conn) as cur:
            files = self.seed_legacy(cur)
            self.runs[D1] = [(D1, 1)]
            rc, text, _ = self.migrate(cur, files)
            self.assertEqual(rc, "halt", text)
            self.assertIn("the legacy run log loaded [1]", text)

    def test_files_must_match_the_legacy_dates(self):
        with rolled_back(self.conn) as cur:
            files = self.seed_legacy(cur)
            rc, text, _ = self.migrate(cur, files[:2])
            self.assertEqual(rc, "halt", text)
            self.assertIn("one file per legacy snapshot", text)

    def test_a_wrong_view_definition_fails_and_everything_rolls_back(self):
        with rolled_back(self.conn) as cur:
            files = self.seed_legacy(cur)
            wrong = m.VIEW_SQL.replace("ELSE (SELECT MIN(s.snapshot_date)",
                                       "ELSE (SELECT MAX(s.snapshot_date)")
            self.assertNotEqual(wrong, m.VIEW_SQL)
            with mock.patch.object(m, "VIEW_SQL", wrong):
                rc, text, logged = self.migrate(cur, files)
            self.assertEqual(rc, "halt", text)
            self.assertIn(f"the view {LEGACY} differs from {RENAMED}", text)
            self.assert_rolled_back(cur)
            logged.assert_not_called()


class LegacyPreview(_Base):
    def test_preview_without_tables_compares_with_the_legacy_table(self):
        with rolled_back(self.conn) as cur:
            p = self.make(D1)
            rows, _ = m.parse_locations(p)
            mapped, _ = m.map_locations(rows, BOUNDS, {}, {}, api=Api())
            cur.execute(f"CREATE TABLE public.{LEGACY} (LIKE "
                        "public.cqc_locations INCLUDING ALL)")
            cols = ("location_id",) + m.VALUE_COLUMNS
            for r in mapped:
                cur.execute(f"INSERT INTO public.{LEGACY} ({', '.join(cols)}, "
                            "source_file_date) VALUES ("
                            + ", ".join(["%s"] * (len(cols) + 1)) + ")",
                            [r[c] for c in cols] + [D1])
            self.make(D2, ids=range(1, N + 1))
            rc, text, logged = self.run_main(cur, ["load"], page=url(D2),
                                             table=False)
            self.assertEqual(rc, 0, text)
            self.assertIn(f"compared with legacy {LEGACY} {D1}: rows 60 -> "
                          "60; gone 1; new 1", text)
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW", text)
            self.assertEqual(snapshot_of(cur), (0, 0, 0, 0))
            logged.assert_not_called()


class _Resp:
    def __init__(self, content=b"", text="", status=200):
        self.content, self.text, self.status_code = content, text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


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
        self.dest = Path(self.tmp.name) / "raw"
        self.link = url(D3)

    def body(self, rating="Good", readme_first=True):
        p = write_ods(Path(self.tmp.name) / f"src-{rating}.ods",
                      locations=[loc("1-1", rating=rating)],
                      readme_first=readme_first)
        return p.read_bytes()

    def fetch(self, body):
        s = _Session(_Resp(content=body))
        with quiet(), mock.patch.object(m, "MIN_FILE_BYTES", 100):
            return m.fetch_file(self.link, self.dest, s), s

    def test_download_kept_under_the_link_name_with_user_agent(self):
        path, s = self.fetch(self.body())
        self.assertEqual(path, self.dest / ods_name(date(2026, 10, 1)))
        self.assertIn("User-Agent", s.calls[0][1]["headers"])
        self.assertEqual(m.read_readme(path)["as_at"], date(2026, 10, 1))

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
        second, _ = self.fetch(self.body("Outstanding"))
        self.assertNotEqual(second, first)
        self.assertEqual(m.content_sha256(first), before)
        sha8 = m.content_sha256(second)[:8]
        self.assertEqual(second.name, f"{first.stem}-{sha8}.ods")
        self.assertEqual(m.name_date(second.name), date(2026, 10, 1))
        self.assertEqual(len(list(self.dest.iterdir())), 2)

    def test_small_or_wrong_files_halt_and_keep_nothing(self):
        s = _Session(_Resp(content=b"<html>error</html>"))
        with quiet(), self.assertRaises(SystemExit):
            m.fetch_file(self.link, self.dest, s)
        for body in (b"x" * 2000, self.body(readme_first=False)):
            s = _Session(_Resp(content=body))
            with quiet(), mock.patch.object(m, "MIN_FILE_BYTES", 100), \
                    self.assertRaises(SystemExit):
                m.fetch_file(self.link, self.dest, s)
        self.assertEqual(list(self.dest.glob("*")) if self.dest.exists()
                         else [], [])

    def test_http_failure_halts(self):
        with quiet(), self.assertRaises(SystemExit):
            m.fetch_file(self.link, self.dest, _Session(_Resp(status=500)))


class SpecTests(unittest.TestCase):
    def test_spec_matches_the_legacy_columns(self):
        conn = get_conn()
        try:
            cur = conn.cursor()
            # After the migration cqc_locations is a view (all columns
            # nullable); the old base table is cqc_locations_legacy.
            for table in ("cqc_locations_legacy", "cqc_locations"):
                cur.execute("SELECT column_name, data_type, "
                            "character_maximum_length, numeric_precision, "
                            "numeric_scale, is_nullable FROM "
                            "information_schema.columns WHERE table_schema = "
                            "'public' AND table_name = %s ORDER BY "
                            "ordinal_position", (table,))
                legacy = cur.fetchall()
                if legacy:
                    break
        finally:
            conn.rollback()
            conn.close()
        self.assertEqual(tuple(r[0] for r in legacy), m.LEGACY_COLUMNS)
        types = dict(m.COLUMN_TYPES)
        for name, typ, length, prec, scale, nullable in legacy:
            if name not in types:
                continue
            want = types[name]
            self.assertEqual("NOT NULL" in want, nullable == "NO", name)
            base = want.replace(" NOT NULL", "")
            got = {"character varying": f"varchar({length})",
                   "numeric": f"numeric({prec},{scale})"}.get(typ, typ)
            self.assertEqual(got, base, name)


if __name__ == "__main__":
    unittest.main()
