"""Tests for the S12 loader in s12_financial_stress_editions (the commands,
migrate-legacy, restore-edition) on throwaway tables.

No network: the content API replies are stubbed (fetch_json, fetch_page) and
every page JSON and S.114 register is written by the test into a temporary
directory that the tests make the only allowed root. Database tests run on a
connection from _db.get_conn() inside a transaction that is always rolled
back (rolled_back below); nothing here commits. The commands get a stand-in
connection whose commit and rollback act on a savepoint of that transaction,
and a cursor proxy whose .connection does the same, so the engine's
per-period commit never reaches the real connection. The only tables written
are zz_s12_efs_live (a LIKE copy of la_efs_support), zz_s12_s114_live (a LIKE
copy of la_s114_notices, its CHECK included), their zz_ editions tables and
ledgers, all created inside that transaction (refused if any already
exists), so they never persist. The run log is stubbed; the real
la_boundaries and la_code_lookup are read (never written).
"""
import contextlib
import dataclasses
import io
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s12_financial_stress_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s12_efs_pure import (S114_HEADER, YEAR_BASE, collection,  # noqa: E402
                               page)

ZZ_EFS = dataclasses.replace(
    m.SPEC_EFS, name="zz_s12_efs", live_table="zz_s12_efs_live",
    editions_table="zz_s12_efs_editions",
    expected_rows_per_period=m.tip_row_count("zz_s12_efs_editions"))
ZZ_S114 = dataclasses.replace(
    m.SPEC_S114, name="zz_s12_s114", live_table="zz_s12_s114_live",
    editions_table="zz_s12_s114_editions",
    expected_rows_per_period=m.tip_row_count("zz_s12_s114_editions"))
LEDGER_EFS = "zz_s12_efs_editions_file_checks"
LEDGER_S114 = "zz_s12_s114_editions_file_checks"
TABLES = ("zz_s12_efs_live", "zz_s12_efs_editions", LEDGER_EFS,
          "zz_s12_s114_live", "zz_s12_s114_editions", LEDGER_S114)

R1 = "2026-02-23T15:02:38+00:00"
R2 = "2026-08-18T14:12:33+00:00"
R3 = "2026-09-30T09:00:00+00:00"
LOADED = datetime(2026, 3, 31, 22, 34, 16, 793348, tzinfo=timezone.utc)
N8N_EFS = "MHCLG Exceptional Financial Support for local authorities for {}"
N8N_S114 = "IfG / Wikipedia / primary sources — compiled manually"
W = "Council provided with in-principle support but withdrew its request"


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def p25(updated=R2, croydon="£136.0m (support agreed in-principle)",
        extra_rows=(), drop=(), **kw):
    rows = [("Barnet", "£55.7m (support agreed in-principle)"),
            ("Bradford", "£113.0m"), ("Croydon", croydon)] + list(extra_rows)
    rows = [r for r in rows if r[0] not in drop]
    return page("2025-26", rows,
                hra=[("Lambeth", "£40.0m (support agreed in-principle)")],
                police=[("South Yorkshire Mayoral Combined Authority",
                         "£17.0m (support agreed for 2024-25)")],
                directions=("Bradford capitalisation direction 2025-26",),
                updated=updated, history=[], **kw)


def p26(updated=R2, croydon_note=None, **kw):
    croydon = "£119.0m (support agreed in-principle)"
    if croydon_note:
        croydon += ("<br><br>Note: For support agreed in-principle for "
                    f"2025-26, this has been revised to £{croydon_note}m "
                    "(from £136.0m)")
    return page("2026-27", [
        ("Barnet", "£79.6m (support agreed in-principle)"),
        ("Croydon", croydon),
        ("Haringey", "£84.0m (support agreed in-principle)"),
        ("East Sussex", "£70.0m (support agreed in-principle)")],
        hra=[("City of London", "£2.65m (support agreed in-principle)")],
        updated=updated, history=[], **kw)


def p21(updated="2025-03-13T17:20:32+00:00"):
    return page("2021-22", [("Bexley", W), ("Croydon", "£50.0m"),
                            ("Luton", W)],
                directions=("Croydon capitalisation direction 2021-22",),
                updated=updated, history=[])


class _Tx:
    SP = "zz_s12_cmd"

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
    with mock.patch.dict(m.SPECS, {"s12_efs": ZZ_EFS, "s12_s114": ZZ_S114}):
        yield


@contextmanager
def rolled_back(conn, *, ddl=True):
    cur = conn.cursor()
    try:
        cur.execute("SELECT " + ", ".join(f"to_regclass('public.{t}')"
                                          for t in TABLES))
        if any(cur.fetchone()):
            raise RuntimeError("a zz_s12 table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s12_efs_live (LIKE "
                    "public.la_efs_support INCLUDING ALL)")
        cur.execute("CREATE TABLE public.zz_s12_s114_live (LIKE "
                    "public.la_s114_notices INCLUDING ALL)")
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


def editions(cur, period, table="zz_s12_efs_editions"):
    cur.execute(f"SELECT edition, supersedes, COUNT(*) FROM public.{table} "
                "WHERE financial_year = %s GROUP BY 1, 2 ORDER BY 1",
                (period,))
    return cur.fetchall()


def ledger(cur, table=LEDGER_EFS):
    cur.execute(f"SELECT financial_year, source_file, outcome, edition "
                f"FROM public.{table} ORDER BY id")
    return cur.fetchall()


def live(cur, la, col="amount_m", period="2025-26"):
    cur.execute(f"SELECT {col} FROM public.zz_s12_efs_live WHERE lad24cd = %s "
                "AND financial_year = %s", (la, period))
    r = cur.fetchone()
    return r[0] if r else "absent"


def seed_live_efs(cur, rows):
    """rows: (lad24cd, year, amount, status); hra_only false, the n8n
    source and one loaded_at, as the n8n load stored them."""
    for la, y, a, s in rows:
        cur.execute("INSERT INTO public.zz_s12_efs_live (lad24cd, "
                    "financial_year, amount_m, status, hra_only, source, "
                    "loaded_at) VALUES (%s, %s, %s, %s, false, %s, %s)",
                    (la, y, a, s, N8N_EFS.format(y), LOADED))


S114_LIVE = [
    ("E09000008", date(2020, 11, 11), "2020-21", "Overspend", "exact",
     "direct", None, None),
    ("E09000008", date(2022, 1, 1), "2021-22", "Unlawful expenditure",
     "approximate - month only confirmed", "direct", None, None),
    ("E10000021", date(2018, 2, 2), "2017-18", "Overspend", "exact",
     "predecessor", ["E06000061", "E06000062"],
     "Northamptonshire County Council, abolished 31 March 2021."),
]
LEGACY_CSV = ("la_name,lad24cd,notice_date,financial_year,reason,"
              "date_confirmed\n"
              "Croydon,E09000008,2020-11-11,2020-21,Overspend,exact\n"
              "Croydon,E09000008,2022-01-01,2021-22,Unlawful expenditure,"
              "approximate - month only confirmed\n"
              "Northamptonshire,E10000021,2018-02-02,2017-18,Overspend,"
              "exact\n")


def seed_live_s114(cur, rows=S114_LIVE):
    for r in rows:
        cur.execute("INSERT INTO public.zz_s12_s114_live (lad24cd, "
                    "notice_date, financial_year, reason, date_confirmed, "
                    "source, loaded_at, attribution, successor_codes, "
                    "attribution_note) VALUES (%s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s)", r[:5] + (N8N_S114, LOADED) + r[5:])


def register_row(la_name, code, d, fy, reason, conf, attr="direct",
                 succ="", note="", url="", title="", checked=""):
    note = f"\"{note}\"" if note else ""
    return (f"{la_name},{code},{d},{fy},{reason},{conf},{attr},{succ},{note},"
            f"{url},{title},{checked}")


ROWS_AS_HELD = [
    register_row("Croydon", "E09000008", "2020-11-11", "2020-21", "Overspend",
                 "exact"),
    register_row("Croydon", "E09000008", "2022-01-01", "2021-22",
                 "Unlawful expenditure", "approximate - month only confirmed"),
    register_row("Northamptonshire", "E10000021", "2018-02-02", "2017-18",
                 "Overspend", "exact", "predecessor", "E06000061;E06000062",
                 "Northamptonshire County Council, abolished 31 March 2021."),
]


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
        self.api = {}
        self.fetched = []
        self.register = self.root / "la_s114_notices.csv"

    def site(self, *pages):
        years = [m.page_identity(p)["year"] for p in pages]
        self.api[m.COLLECTION_PATH] = collection(years)
        for y, p in zip(years, pages):
            self.api[YEAR_BASE.format(y)] = p

    def save(self, pg, name=None):
        y = m.page_identity(pg)["year"]
        path = self.root / (name or f"efs_{y}_content_2026-10-10.json")
        path.write_text(json.dumps(pg, ensure_ascii=False), encoding="utf-8")
        return path

    def write_register(self, rows, as_at="2026-10-10", path=None):
        path = path or self.register
        path.write_text(f"register_as_at,{as_at}\n{S114_HEADER}\n"
                        + "".join(r + "\n" for r in rows), encoding="utf-8")
        return path

    def run_main(self, cur, argv):
        borrowed = _Borrowed(cur)

        def fetch_page(base, session=None):
            self.fetched.append(base)
            return self.api[base], m.API + base

        out = io.StringIO()
        with specs(), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda p, s=None: self.api[p]), \
                mock.patch.object(m, "fetch_page", side_effect=fetch_page), \
                mock.patch.object(m, "PAGE_ROOTS", (self.root,)), \
                mock.patch.object(m, "S114_ROOTS", (self.root,)), \
                mock.patch.object(m, "S114_FILE", self.register), \
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

    def seed(self, cur, *pages):
        self.site(*pages)
        self.ok(cur, ["load", "--only", "efs", "--commit"])
        self.fetched.clear()


# ---------------------------------------------------------------------------
# EFS load
# ---------------------------------------------------------------------------

class EfsLoad(Fixture):

    def test_new_years_edition_live_and_ledger_in_one_transaction(self):
        with rolled_back(self.conn) as cur:
            self.site(p25(), p26())
            text, logged = self.ok(cur, ["load", "--only", "efs", "--commit"])
            self.assertEqual(editions(cur, "2025-26"), [(1, None, 4)])
            self.assertEqual(editions(cur, "2026-27"), [(1, None, 4)])
            for p in ("2025-26", "2026-27"):
                self.assertEqual(core.rows_differing(cur, ZZ_EFS, p, 1), 0)
            self.assertEqual(count(cur, "zz_s12_efs_live"), 8)
            lg = ledger(cur)
            self.assertEqual([(p, o, e) for p, _, o, e in lg],
                             [("2025-26", "new", 1), ("2026-27", "new", 1)])
            self.assertEqual(lg[0][1], m.ledger_source(
                m.API + YEAR_BASE.format("2025-26"), "2025-26",
                datetime(2026, 8, 18, 14, 12, 33, tzinfo=timezone.utc)))
            self.assertEqual(live(cur, "E08000032", "status"),
                             "capitalisation-direction")
            self.assertIs(live(cur, "E09000022", "hra_only"), True)
            self.assertIs(live(cur, "E09000001", "hra_only", "2026-27"), True)
            self.assertEqual(live(cur, "E09000014", period="2026-27"),
                             Decimal("84.000"))
            self.assertEqual(live(cur, "E09000013", period="2026-27"),
                             "absent")
            src = live(cur, "E09000003", "source")
            self.assertTrue(src.startswith(
                "MHCLG Exceptional Financial Support for local authorities "
                "for 2025-26; page updated 2026-08-18T14:12:33Z"), src)
            self.assertIn("excluded (not a lower-tier or unitary authority): "
                          "South Yorkshire Mayoral Combined Authority", text)
            self.assertIn("East Sussex", text)
            logged.assert_called_once()
            self.assertIn("2025-26 new", logged.call_args[0][2])
            self.assertTrue(m.status_ok(cur, "s12_efs"))

    def test_a_failing_ledger_insert_rolls_back_and_logs_a_partial_run(self):
        with rolled_back(self.conn) as cur:
            self.site(p25(), p26())
            with mock.patch.object(pe, "record_file_check",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(
                    cur, ["load", "--only", "efs", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2025-26: FAILED", text)
            self.assertEqual(count(cur, "zz_s12_efs_editions"), 0)
            self.assertEqual(count(cur, "zz_s12_efs_live"), 0)
            self.assertEqual(ledger(cur), [])
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN", notes)
            self.assertIn("failed ['2025-26']", notes)
            self.assertIn("not attempted ['2026-27']", notes)

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            self.site(p25(), p26())
            text, logged = self.ok(cur, ["load", "--only", "efs"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            for t in TABLES[1:3]:
                self.assertEqual(count(cur, t), 0, t)
            self.assertEqual(count(cur, "zz_s12_efs_live"), 0)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--only", "efs",
                                         "--simulate"])
            self.assertIn("SIMULATION", text)
            self.assertEqual(count(cur, "zz_s12_efs_editions"), 0)
            logged.assert_not_called()
            self.ok(cur, ["load", "--only", "efs", "--commit"])
            text, logged = self.ok(cur, ["load", "--only", "efs", "--commit"])
            self.assertIn("2025-26: unchanged", text)
            self.assertIn("nothing to do", text)
            logged.assert_not_called()
            self.assertEqual(len(ledger(cur)), 2)
            self.assertEqual(editions(cur, "2025-26"), [(1, None, 4)])

    def test_a_revised_page_is_the_next_edition_live_untouched(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, p25(updated=R1))
            revised = p25(updated=R2, croydon=(
                "£136.0m (support agreed in-principle)<br><br>This was "
                "subsequently revised to £110.3m (support agreed "
                "in-principle)"))
            self.site(revised)
            rc, text, logged = self.run_main(cur, ["load", "--only", "efs",
                                                   "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("REJECTED", text)
            self.assertIn("--acknowledge 2025-26", text)
            self.assertEqual(editions(cur, "2025-26"), [(1, None, 4)])
            text, logged = self.ok(cur, ["load", "--only", "efs",
                                         "--acknowledge", "2025-26",
                                         "--commit"])
            self.assertIn("2025-26: revised", text)
            self.assertEqual(editions(cur, "2025-26"),
                             [(1, None, 4), (2, 1, 4)])
            self.assertIn("ACKNOWLEDGED", logged.call_args[0][2])
            self.assertEqual(live(cur, "E09000008"), Decimal("136.000"))
            st = pe.status(cur, m.profile("s12_efs"))
            self.assertEqual(st["pending_refresh"], ["2025-26"])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("2025-26=1", text)
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live(cur, "E09000008"), Decimal("110.300"))
            cur.execute("SELECT loaded_at, release_label FROM "
                        "public.zz_s12_efs_editions WHERE financial_year = "
                        "'2025-26' AND edition = 2 LIMIT 1")
            ed_loaded, label = cur.fetchone()
            self.assertEqual(live(cur, "E09000008", "loaded_at"), ed_loaded)
            self.assertEqual(live(cur, "E09000008", "source"), label)
            self.assertTrue(m.status_ok(cur, "s12_efs"))

    def test_an_authority_added_to_a_year_needs_accept_key_changes(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, p25(updated=R1))
            self.site(p25(updated=R2, extra_rows=[
                ("Haringey", "£40.6m (support agreed in-principle)")]))
            text, _ = self.ok(cur, ["load", "--only", "efs", "--acknowledge",
                                    "2025-26", "--commit"])
            self.assertIn("E09000014", text)
            self.assertEqual(editions(cur, "2025-26"),
                             [(1, None, 4), (2, 1, 5)])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("keys added 1 (E09000014)", text)
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--accept-key-changes", text)
            self.assertEqual(live(cur, "E09000014"), "absent")
            self.ok(cur, ["refresh-latest", "--accept-key-changes", "2025-26",
                          "--commit"])
            self.assertEqual(live(cur, "E09000014"), Decimal("40.600"))
            self.assertTrue(m.status_ok(cur, "s12_efs"))

    def test_an_authority_dropped_by_a_page_is_rejected_whatever_the_flags(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, p25(updated=R1))
            self.site(p25(updated=R2, drop=("Barnet",)))
            rc, text, _ = self.run_main(cur, ["load", "--only", "efs",
                                              "--acknowledge", "2025-26",
                                              "--allow-older-file",
                                              "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("PARTIAL PAGE", text)
            self.assertIn("E09000003", text)
            self.assertEqual(editions(cur, "2025-26"), [(1, None, 4)])

    def test_an_older_page_halts(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, p25(updated=R2))
            self.site(p25(updated=R1, croydon="£130.0m (support agreed "
                                              "in-principle)"))
            rc, text, _ = self.run_main(cur, ["load", "--only", "efs",
                                              "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("older", text)
            self.assertEqual(editions(cur, "2025-26"), [(1, None, 4)])
            text, _ = self.ok(cur, ["load", "--only", "efs",
                                    "--allow-older-file", "--acknowledge",
                                    "2025-26", "--commit"])
            self.assertIn("--allow-older-file given", text)
            self.assertEqual(editions(cur, "2025-26"),
                             [(1, None, 4), (2, 1, 4)])

    def test_equal_rank_with_different_content_needs_accept_reissue(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, p25(updated=R2))
            self.site(p25(updated=R2, croydon="£130.0m (support agreed "
                                              "in-principle)"))
            rc, text, _ = self.run_main(cur, ["load", "--only", "efs",
                                              "--acknowledge", "2025-26",
                                              "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("--accept-reissue 2025-26", text)
            text, logged = self.ok(cur, ["load", "--only", "efs",
                                         "--acknowledge", "2025-26",
                                         "--accept-reissue", "2025-26",
                                         "--commit"])
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertIn("--accept-reissue 2025-26", logged.call_args[0][2])

    def test_byte_identical_rereads_are_unchanged_on_every_path(self):
        with rolled_back(self.conn) as cur:
            pg = p25()
            self.seed(cur, pg)
            path = self.save(pg)
            for argv in (["load", "--only", "efs"],
                         ["load", "--only", "efs", "--recheck", "2025-26"],
                         ["load", "--only", "efs", "--page-file", path],
                         ["load", "--only", "efs", "--page-file", path,
                          "--no-page", "--commit"],
                         ["load", "--only", "efs", "--page-file", path,
                          "--no-page", "--commit"],
                         ["load", "--only", "efs", "--recheck", "2025-26",
                          "--commit"]):
                with self.subTest(argv=argv):
                    text, _ = self.ok(cur, argv)
                    self.assertNotIn(": revised", text)
                    self.assertNotIn("REJECTED", text)
                    self.assertIn("2025-26: unchanged", text)
            self.assertEqual(editions(cur, "2025-26"), [(1, None, 4)])
            self.assertEqual({o for _, _, o, _ in ledger(cur)[1:]},
                             {"unchanged"})
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)

    def test_a_page_file_outside_the_roots_halts(self):
        with rolled_back(self.conn) as cur:
            other = tempfile.TemporaryDirectory()
            self.addCleanup(other.cleanup)
            path = Path(other.name) / "page.json"
            path.write_text(json.dumps(p25()), encoding="utf-8")
            rc, text, _ = self.run_main(cur, ["load", "--only", "efs",
                                              "--page-file", path,
                                              "--no-page"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("not under an allowed root", text)

    def test_other_year_disagreement_needs_acknowledge_own_page_wins(self):
        with rolled_back(self.conn) as cur:
            self.site(p25(), p26(croydon_note="110.3"))
            rc, text, logged = self.run_main(cur, ["load", "--only", "efs",
                                                   "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("2025-26: REJECTED", text)
            self.assertIn("differs", text)
            self.assertEqual(editions(cur, "2025-26"), [])
            self.assertEqual(editions(cur, "2026-27"), [(1, None, 4)])
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN", notes)
            self.assertIn("rejected ['2025-26']", notes)
            self.ok(cur, ["load", "--only", "efs", "--acknowledge", "2025-26",
                          "--commit"])
            self.assertEqual(live(cur, "E09000008"), Decimal("136.000"))

    def test_an_unknown_name_halts_and_stores_nothing(self):
        with rolled_back(self.conn) as cur:
            self.site(p25(extra_rows=[("Gloucestershire", "£5.0m (support "
                                                          "agreed in-principle)")]))
            rc, text, _ = self.run_main(cur, ["load", "--only", "efs",
                                              "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Gloucestershire", text)
            self.assertEqual(count(cur, "zz_s12_efs_editions"), 0)

    def test_a_missing_year_page_halts(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, p25(), p26())
            self.site(p26())
            rc, text, _ = self.run_main(cur, ["load", "--only", "efs"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2025-26", text)


# ---------------------------------------------------------------------------
# migrate-legacy, withdrawn-only rule, flips, restore
# ---------------------------------------------------------------------------

class MigrateBase(Fixture):

    def legacy(self, cur):
        """Seed the zz live tables as the n8n load left them (2021-22 with
        Bexley's withdrawn request, 2025-26 as the first figures) and patch
        the held-state constants to them."""
        seed_live_efs(cur, [
            ("E09000004", "2021-22", None, "withdrawn"),
            ("E09000008", "2021-22", Decimal("50.000"), "agreed-in-principle"),
            ("E06000032", "2021-22", None, "withdrawn"),
            ("E09000003", "2025-26", Decimal("55.700"), "agreed-in-principle"),
            ("E08000032", "2025-26", Decimal("127.100"),
             "agreed-in-principle"),
            ("E09000008", "2025-26", Decimal("136.000"),
             "agreed-in-principle"),
            ("E09000022", "2025-26", Decimal("40.000"),
             "agreed-in-principle"),
            ("E06000032", "2025-26", Decimal("1.000"),
             "agreed-in-principle"),
        ])
        seed_live_s114(cur)
        f21 = self.save(p21())
        f25 = self.save(p25(extra_rows=[("Luton", "£1.0m (support agreed "
                                                  "in-principle)")]))
        csv_path = self.root / "la_s114_notices_legacy.csv"
        csv_path.write_text(LEGACY_CSV, encoding="utf-8")
        with specs():
            efs_state = m.live_state(cur, ZZ_EFS)
            s114_state = m.live_state(cur, ZZ_S114)
        pages = {"2021-22": {"path": f21, "sha256": m.content_sha256(f21)},
                 "2025-26": {"path": f25, "sha256": m.content_sha256(f25)}}
        s114 = {"path": csv_path, "sha256": m.content_sha256(csv_path)}
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(m, "LEGACY_EFS", efs_state))
        stack.enter_context(mock.patch.object(m, "LEGACY_S114", s114_state))
        stack.enter_context(mock.patch.object(m, "LEGACY_PAGES", pages))
        stack.enter_context(mock.patch.object(m, "LEGACY_S114_FILE", s114))
        self.addCleanup(stack.close)
        return f21, f25


class Migrate(MigrateBase):

    def test_migrate_legacy_preview_simulate_commit_and_proof(self):
        with rolled_back(self.conn) as cur:
            f21, f25 = self.legacy(cur)
            text, logged = self.ok(cur, ["migrate-legacy"])
            self.assertIn("PREVIEW", text)
            self.assertIn("E08000032", text)
            self.assertIn("127.100", text)
            self.assertIn("113.000", text)
            self.assertIn("unevidenced: 3 of 3", text)
            for t in ("zz_s12_efs_editions", "zz_s12_s114_editions",
                      LEDGER_EFS, LEDGER_S114):
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            self.ok(cur, ["migrate-legacy", "--simulate"])
            self.assertEqual(count(cur, "zz_s12_efs_editions"), 0)
            text, logged = self.ok(cur, ["migrate-legacy", "--commit"])
            logged.assert_called_once()
            for p, n in (("2021-22", 3), ("2025-26", 5)):
                self.assertEqual(editions(cur, p), [(1, None, n)])
                self.assertEqual(core.rows_differing(cur, ZZ_EFS, p, 1), 0)
            for p in ("2017-18", "2020-21", "2021-22"):
                self.assertEqual(editions(cur, p, "zz_s12_s114_editions"),
                                 [(1, None, 1)])
            # neither page reproduces its year (status, hra_only, amounts)
            self.assertEqual(ledger(cur), [])
            self.assertEqual(len(ledger(cur, LEDGER_S114)), 3)
            cur.execute("SELECT COUNT(*) FROM public.zz_s12_s114_editions "
                        "WHERE evidence_url IS NULL")
            self.assertEqual(cur.fetchone()[0], 3)
            self.assertTrue(m.status_ok(cur, "s12_efs"))
            self.assertTrue(m.status_ok(cur, "s12_s114"))
            text, _ = self.ok(cur, ["status"])
            self.assertIn("3 notices without evidence", text)

    def test_migrate_failure_modes(self):
        with rolled_back(self.conn) as cur:
            f21, f25 = self.legacy(cur)
            cases = {
                "not as surveyed": lambda: mock.patch.object(
                    m, "LEGACY_EFS", (1, "x", 1, ())),
                "sha256": lambda: mock.patch.dict(
                    m.LEGACY_PAGES, {"2021-22": {"path": f21,
                                                 "sha256": "0" * 64}}),
                "s114 file": lambda: mock.patch.dict(
                    m.LEGACY_S114_FILE, {"sha256": "0" * 64}),
            }
            for want, patcher in cases.items():
                with self.subTest(want=want), patcher():
                    rc, text, _ = self.run_main(cur, ["migrate-legacy",
                                                      "--commit"])
                    self.assertEqual(rc, "halt", text)
                    self.assertEqual(count(cur, "zz_s12_efs_editions"), 0)
            # a held authority the page does not list: the proof fails
            short = self.save(p25(), name="short.json")
            with mock.patch.dict(m.LEGACY_PAGES, {"2025-26": {
                    "path": short, "sha256": m.content_sha256(short)}}):
                rc, text, _ = self.run_main(cur, ["migrate-legacy",
                                                  "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("E06000032", text)
            # a held S.114 row the legacy file does not reproduce
            cur.execute("UPDATE public.zz_s12_s114_live SET reason = 'x' "
                        "WHERE financial_year = '2020-21'")
            with specs():
                st = m.live_state(cur, ZZ_S114)
            with mock.patch.object(m, "LEGACY_S114", st):
                rc, text, _ = self.run_main(cur, ["migrate-legacy",
                                                  "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2020-21", text)
            self.assertEqual(count(cur, "zz_s12_s114_editions"), 0)

    def test_migrate_runs_once(self):
        with rolled_back(self.conn) as cur:
            self.legacy(cur)
            self.ok(cur, ["migrate-legacy", "--commit"])
            rc, text, _ = self.run_main(cur, ["migrate-legacy", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("runs once", text)

    def test_reproduced_year_unchanged_on_every_path_against_as_loaded(self):
        with rolled_back(self.conn) as cur:
            # live 2025-26 exactly as this loader reads the page
            seed_live_efs(cur, [
                ("E09000003", "2025-26", Decimal("55.700"),
                 "agreed-in-principle"),
                ("E08000032", "2025-26", Decimal("113.000"),
                 "capitalisation-direction"),
                ("E09000008", "2025-26", Decimal("136.000"),
                 "agreed-in-principle")])
            cur.execute("INSERT INTO public.zz_s12_efs_live (lad24cd, "
                        "financial_year, amount_m, status, hra_only, source, "
                        "loaded_at) VALUES ('E09000022', '2025-26', 40, "
                        "'agreed-in-principle', true, %s, %s)",
                        (N8N_EFS.format("2025-26"), LOADED))
            seed_live_s114(cur)
            pg = p25()
            f25 = self.save(pg)
            csv_path = self.root / "legacy.csv"
            csv_path.write_text(LEGACY_CSV, encoding="utf-8")
            with specs():
                efs_state = m.live_state(cur, ZZ_EFS)
                s114_state = m.live_state(cur, ZZ_S114)
            with mock.patch.object(m, "LEGACY_EFS", efs_state), \
                    mock.patch.object(m, "LEGACY_S114", s114_state), \
                    mock.patch.object(m, "LEGACY_PAGES", {"2025-26": {
                        "path": f25, "sha256": m.content_sha256(f25)}}), \
                    mock.patch.object(m, "LEGACY_S114_FILE", {
                        "path": csv_path,
                        "sha256": m.content_sha256(csv_path)}):
                self.ok(cur, ["migrate-legacy", "--commit"])
            self.assertEqual([(p, o, e) for p, _, o, e in ledger(cur)],
                             [("2025-26", "unchanged", 1)])
            self.site(pg)
            for argv in (["load", "--only", "efs", "--page-file", f25,
                          "--no-page"],
                         ["load", "--only", "efs"],
                         ["load", "--only", "efs", "--commit"],
                         ["load", "--only", "efs", "--recheck", "2025-26",
                          "--commit"]):
                with self.subTest(argv=argv):
                    text, _ = self.ok(cur, argv)
                    self.assertIn("2025-26: unchanged", text)
                    self.assertNotIn("REJECTED", text)
            self.assertEqual(editions(cur, "2025-26"), [(1, None, 4)])

    def test_withdrawn_only_rule_is_an_additive_edition(self):
        with rolled_back(self.conn) as cur:
            self.legacy(cur)
            self.ok(cur, ["migrate-legacy", "--commit"])
            self.site(p21(), p25(extra_rows=[("Luton", "£1.0m (support "
                                                       "agreed in-principle)")]))
            text, _ = self.ok(cur, ["load", "--only", "efs", "--acknowledge",
                                    "2021-22", "--acknowledge", "2025-26",
                                    "--commit"])
            self.assertIn("withdrawn-only", text)
            self.assertIn("E09000004", text)
            # edition 1 keeps Bexley as held; edition 2 does not
            self.assertEqual(editions(cur, "2021-22"),
                             [(1, None, 3), (2, 1, 2)])
            cur.execute("SELECT edition FROM public.zz_s12_efs_editions "
                        "WHERE lad24cd = 'E09000004'")
            self.assertEqual([r[0] for r in cur.fetchall()], [1])
            # Luton's withdrawn 2021-22 row stays: it had support in 2025-26
            cur.execute("SELECT status FROM public.zz_s12_efs_editions WHERE "
                        "lad24cd = 'E06000032' AND financial_year = '2021-22' "
                        "AND edition = 2")
            self.assertEqual(cur.fetchone()[0], "withdrawn")
            self.assertEqual(live(cur, "E09000004", "status", "2021-22"),
                             "withdrawn")
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("removed 1 (E09000004)", text)
            self.ok(cur, ["refresh-latest", "--accept-key-changes", "2021-22",
                          "--commit"])
            self.assertEqual(live(cur, "E09000004", "status", "2021-22"),
                             "absent")
            cur.execute("SELECT DISTINCT lad24cd FROM public.zz_s12_efs_live "
                        "ORDER BY 1")
            self.assertEqual([r[0] for r in cur.fetchall()],
                             ["E06000032", "E08000032", "E09000003",
                              "E09000008", "E09000022"])

    def test_a_value_going_to_null_needs_a_named_flip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, p25(updated=R1, extra_rows=[
                ("Shropshire", "£26.9m (support agreed in-principle)")]))
            self.site(p25(updated=R2, extra_rows=[
                ("Shropshire", "£26.9m for 2024-25")]))
            rc, text, _ = self.run_main(cur, ["load", "--only", "efs",
                                              "--acknowledge", "2025-26",
                                              "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("--acknowledge-flips", text)
            flips = {"zz-flip": {
                "decided": "test", "why": "test",
                "periods": {"2025-26": {"E06000051": ("26.900", None)}}}}
            with mock.patch.object(m, "ACKNOWLEDGED_FLIPS", flips):
                text, _ = self.ok(cur, ["load", "--only", "efs",
                                        "--acknowledge", "2025-26",
                                        "--acknowledge-flips", "zz-flip",
                                        "--commit"])
            cur.execute("SELECT amount_m, status FROM "
                        "public.zz_s12_efs_editions WHERE lad24cd = "
                        "'E06000051' AND edition = 2")
            self.assertEqual(cur.fetchone(), (None, "other-years-only"))

    def test_restore_edition(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, p25(updated=R1))
            self.site(p25(updated=R2, croydon="£110.3m (support agreed "
                                              "in-principle)"))
            self.ok(cur, ["load", "--only", "efs", "--acknowledge", "2025-26",
                          "--commit"])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live(cur, "E09000008"), Decimal("110.300"))
            text, _ = self.ok(cur, ["restore-edition", "s12_efs", "2025-26",
                                    "1"])
            self.assertIn("would be stored as edition 3", text)
            self.assertEqual(editions(cur, "2025-26"),
                             [(1, None, 4), (2, 1, 4)])
            text, logged = self.ok(cur, ["restore-edition", "s12_efs",
                                         "2025-26", "1", "--commit"])
            logged.assert_called_once()
            self.assertEqual(editions(cur, "2025-26"),
                             [(1, None, 4), (2, 1, 4), (3, 2, 4)])
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertEqual(live(cur, "E09000008"), Decimal("136.000"))
            rc, text, _ = self.run_main(cur, ["restore-edition", "s12_efs",
                                              "2025-26", "3"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("tip", text)


# ---------------------------------------------------------------------------
# The S.114 register
# ---------------------------------------------------------------------------

class S114Load(MigrateBase):

    def migrated(self, cur):
        self.legacy(cur)
        self.ok(cur, ["migrate-legacy", "--commit"])

    def test_register_as_held_is_unchanged_and_rereads_skip(self):
        with rolled_back(self.conn) as cur:
            self.migrated(cur)
            self.write_register(ROWS_AS_HELD)
            text, logged = self.ok(cur, ["load", "--only", "s114",
                                         "--commit"])
            self.assertIn("MANUAL INPUT", text)
            self.assertIn("unevidenced: 3 of 3", text)
            for p in ("2017-18", "2020-21", "2021-22"):
                self.assertIn(f"{p}: unchanged", text)
                self.assertEqual(editions(cur, p, "zz_s12_s114_editions"),
                                 [(1, None, 1)])
            text, logged = self.ok(cur, ["load", "--only", "s114",
                                         "--commit"])
            self.assertIn("already in the ledger", text)
            logged.assert_not_called()

    def test_evidence_filled_is_the_next_edition(self):
        with rolled_back(self.conn) as cur:
            self.migrated(cur)
            rows = list(ROWS_AS_HELD)
            rows[0] = register_row(
                "Croydon", "E09000008", "2020-11-11", "2020-21", "Overspend",
                "exact", url="https://democracy.croydon.gov.uk/s114.pdf",
                title="Section 114 notice", checked="2026-10-10")
            self.write_register(rows)
            text, _ = self.ok(cur, ["load", "--only", "s114", "--commit"])
            self.assertIn("2020-21: revised", text)
            self.assertIn("unevidenced: 2 of 3", text)
            self.assertEqual(editions(cur, "2020-21", "zz_s12_s114_editions"),
                             [(1, None, 1), (2, 1, 1)])
            text, _ = self.ok(cur, ["status"])
            self.assertIn("2 notices without evidence", text)
            self.ok(cur, ["refresh-latest", "--commit"])
            self.assertTrue(m.status_ok(cur, "s12_s114"))

    def test_the_loader_never_removes_a_notice(self):
        with rolled_back(self.conn) as cur:
            self.migrated(cur)
            self.write_register([ROWS_AS_HELD[0], ROWS_AS_HELD[2],
                                 register_row(
                                     "Croydon", "E09000008", "2022-02-15",
                                     "2021-22", "Unlawful expenditure",
                                     "exact")])
            rc, text, _ = self.run_main(cur, ["load", "--only", "s114",
                                              "--acknowledge", "2021-22",
                                              "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("never removes", text)
            self.assertEqual(editions(cur, "2021-22", "zz_s12_s114_editions"),
                             [(1, None, 1)])

    def test_a_date_correction_is_listed_and_needs_accept_key_changes(self):
        with rolled_back(self.conn) as cur:
            self.migrated(cur)
            self.write_register([ROWS_AS_HELD[0], ROWS_AS_HELD[2],
                                 register_row(
                                     "Croydon", "E09000008", "2022-01-18",
                                     "2021-22", "Unlawful expenditure",
                                     "exact", url="https://x.gov.uk/r.pdf",
                                     title="Report", checked="2026-10-10")])
            text, _ = self.ok(cur, ["load", "--only", "s114", "--commit"])
            self.assertIn("date correction", text)
            self.assertEqual(editions(cur, "2021-22", "zz_s12_s114_editions"),
                             [(1, None, 1), (2, 1, 1)])
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.ok(cur, ["refresh-latest", "--accept-key-changes", "2021-22",
                          "--commit"])
            cur.execute("SELECT notice_date, date_confirmed FROM "
                        "public.zz_s12_s114_live WHERE financial_year = "
                        "'2021-22'")
            self.assertEqual(cur.fetchall(), [(date(2022, 1, 18), "exact")])

    def test_an_added_notice_needs_acknowledge(self):
        with rolled_back(self.conn) as cur:
            self.migrated(cur)
            self.write_register(ROWS_AS_HELD + [register_row(
                "Woking", "E07000217", "2023-06-07", "2023-24",
                "Risky investments", "exact")])
            rc, text, _ = self.run_main(cur, ["load", "--only", "s114",
                                              "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("--acknowledge 2023-24", text)
            self.ok(cur, ["load", "--only", "s114", "--acknowledge",
                          "2023-24", "--commit"])
            self.assertEqual(editions(cur, "2023-24", "zz_s12_s114_editions"),
                             [(1, None, 1)])

    def test_an_older_register_halts(self):
        with rolled_back(self.conn) as cur:
            self.migrated(cur)
            rows = list(ROWS_AS_HELD)
            rows[0] = register_row(
                "Croydon", "E09000008", "2020-11-11", "2020-21", "Overspend",
                "exact", url="https://x.gov.uk/a.pdf", title="A",
                checked="2026-10-10")
            self.write_register(rows, as_at="2026-10-10")
            self.ok(cur, ["load", "--only", "s114", "--commit"])
            rows[0] = rows[0].replace("/a.pdf", "/b.pdf")
            self.write_register(rows, as_at="2026-10-01")
            rc, text, _ = self.run_main(cur, ["load", "--only", "s114",
                                              "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("older", text)


if __name__ == "__main__":
    unittest.main()
