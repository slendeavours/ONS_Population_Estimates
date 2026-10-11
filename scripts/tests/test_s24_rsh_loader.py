"""Tests for the S24 loader in s24_rsh_register_editions (load, migrate-legacy,
refresh-latest, restore-edition, status) on throwaway tables.

No network: the content API replies and the downloads are stubbed, and every
workbook is written by the test (test_s24_rsh_pure's writers, each register
workbook with a veryHidden sentinel sheet). Database tests run on a
connection from _db.get_conn() inside a transaction that is always rolled
back (rolled_back below); nothing here commits. The commands get a stand-in
connection whose commit and rollback act on a savepoint of that transaction.
The only tables written are zz_s24_* (LIKE copies of the three live tables,
their editions tables and ledgers), created inside that transaction
(refused if any already exists), so they never persist. The run log is
stubbed.
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
import s24_rsh_register_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s24_rsh_pure import (HIDDEN_TITLE, JROWS, MEDIA, NROWS,  # noqa: E402
                               REG_ROWS, SENTINEL, jrow, judgements_page,
                               register_page, write_judgements,
                               write_register)

ZZ = {
    "register": dataclasses.replace(
        m.SPECS["register"], name="zz_s24_reg", live_table="zz_s24_reg_live",
        editions_table="zz_s24_reg_editions",
        expected_rows_per_period=m.tip_row_count("zz_s24_reg_editions",
                                                 "snapshot_date")),
    "judgements": dataclasses.replace(
        m.SPECS["judgements"], name="zz_s24_jud",
        live_table="zz_s24_jud_live", editions_table="zz_s24_jud_editions",
        expected_rows_per_period=m.tip_row_count("zz_s24_jud_editions",
                                                 "edition_date")),
    "notices": dataclasses.replace(
        m.SPECS["notices"], name="zz_s24_not", live_table="zz_s24_not_live",
        editions_table="zz_s24_not_editions",
        expected_rows_per_period=m.tip_row_count("zz_s24_not_editions",
                                                 "edition_date")),
}
LIVE_OF = {"register": "rsh_registered_providers",
           "judgements": "rsh_regulatory_judgements",
           "notices": "rsh_enforcement_notices"}
SEP = "2026-09-17"
J_SEP = "2026-09-30"
J_AUG = "2026-08-12"


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    SP = "zz_s24_cmd"

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
    with mock.patch.object(m, "SPECS", ZZ):
        yield


def tables():
    out = []
    for s in ZZ.values():
        out += [s.live_table, s.editions_table, f"{s.editions_table}_file_checks"]
    return out


@contextmanager
def rolled_back(conn, *, ddl=True):
    cur = conn.cursor()
    try:
        for t in tables():
            cur.execute("SELECT to_regclass(%s)", (f"public.{t}",))
            if cur.fetchone()[0] is not None:
                raise RuntimeError(f"{t} already exists as a real table; "
                                   "refusing to run")
        for name, s in ZZ.items():
            cur.execute(f"CREATE TABLE public.{s.live_table} (LIKE "
                        f"public.{LIVE_OF[name]} INCLUDING ALL)")
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


def editions(cur, name, period):
    s = ZZ[name]
    cur.execute(f"SELECT edition, supersedes, COUNT(*) FROM "
                f"public.{s.editions_table} WHERE {s.period_col} = %s "
                "GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur, name):
    s = ZZ[name]
    cur.execute(f"SELECT {s.period_col}::text, source_file, outcome, edition "
                f"FROM public.{s.editions_table}_file_checks ORDER BY id")
    return cur.fetchall()


def live_periods(cur, name):
    s = ZZ[name]
    cur.execute(f"SELECT {s.period_col}::text, COUNT(*) FROM "
                f"public.{s.live_table} GROUP BY 1 ORDER BY 1")
    return cur.fetchall()


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
        self.urls = {}
        self.final = {}
        self.fetched = []
        self.n = 0

    def register(self, day="17 September 2026", *, rows=None,
                 modified=datetime(2026, 9, 17, 14, 47), url=None,
                 page=True):
        self.n += 1
        name = f"List_of_registered_providers_{day.replace(' ', '_')}.xlsx"
        path = write_register(self.root / f"r{self.n}" / name, rows=rows,
                              modified=modified)
        url = url or MEDIA.format(f"m{self.n}", name)
        self.urls[url] = path
        if page:
            self.api[m.REGISTER_PATH] = register_page(day, url)
        self.last_url = url
        return path

    def judgements(self, stamp="20260930", *, jrows=None, nrows=None,
                   modified=None, url=None, page=True, changes=None):
        self.n += 1
        d = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:]))
        name = f"{stamp}_RegulatoryJudgementsNotices_published.xlsx"
        path = write_judgements(self.root / f"j{self.n}" / name, jrows=jrows,
                                nrows=nrows,
                                modified=modified or datetime(d.year, d.month,
                                                              d.day, 11))
        url = url or MEDIA.format(f"m{self.n}", name)
        self.urls[url] = path
        if page:
            self.api[m.JUDGEMENTS_PATH] = judgements_page(
                url, changes=changes or (d.isoformat(), "2026-08-12"),
                updated=f"{d.isoformat()}T09:30:04+01:00")
        self.last_url = url
        return path

    def run_main(self, cur, argv):
        borrowed = _Borrowed(cur)

        def fetch(url, dest, session=None):
            self.fetched.append(url)
            return self.urls[url], self.final.get(url, url)

        out, err = io.StringIO(), io.StringIO()
        with specs(), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda p, s=None: self.api[p]), \
                mock.patch.object(m, "fetch", side_effect=fetch), \
                mock.patch.object(m, "_today", return_value=date(2026, 10, 11)), \
                mock.patch.object(m, "log_run") as logged, \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                rc = m.main([str(a) for a in argv])
            except SystemExit as e:
                rc = "halt"
                out.write(f"\n{e.code}")
            finally:
                borrowed.tx.done()
        text = out.getvalue() + err.getvalue()
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn(HIDDEN_TITLE, text)
        for c in logged.call_args_list:
            self.assertNotIn(SENTINEL, str(c))
        return rc, text, logged

    def ok(self, cur, argv):
        rc, text, logged = self.run_main(cur, argv)
        self.assertEqual(rc, 0, text)
        return text, logged


class LoadRegister(Fixture):

    def test_new_date_stores_edition_live_and_one_ledger_row(self):
        with rolled_back(self.conn) as cur:
            path = self.register()
            text, logged = self.ok(cur, ["load", "--only", "register",
                                         "--commit"])
            self.assertIn(f"register {SEP}: new", text)
            self.assertEqual(editions(cur, "register", SEP), [(1, None, 4)])
            self.assertEqual(live_periods(cur, "register"), [(SEP, 4)])
            self.assertEqual(core.rows_differing(cur, ZZ["register"], SEP, 1),
                             0)
            (_, src, outcome, ed), = ledger(cur, "register")
            self.assertEqual((outcome, ed), ("new", 1))
            self.assertTrue(src.startswith(self.last_url + " (register "
                                           f"{SEP}; modified 2026-09-17T"))
            cur.execute("SELECT DISTINCT source_url, source_file, "
                        "release_page_url FROM public.zz_s24_reg_live")
            self.assertEqual(cur.fetchall(), [(
                self.last_url, path.name, m.GOV_UK + m.REGISTER_PATH)])
            cur.execute("SELECT registration_number, null_reasons FROM "
                        "public.zz_s24_reg_editions ORDER BY 1")
            got = dict(cur.fetchall())
            self.assertEqual(got["4001"], {"notes": "blank"})
            self.assertEqual(got["4747"], {})
            logged.assert_called_once()
            self.assertIn(f"register {SEP} new", logged.call_args[0][2])
            self.assertTrue(pe.status(cur, m.profile("register"))["ok"])

    def test_a_failing_ledger_insert_rolls_back_everything(self):
        with rolled_back(self.conn) as cur:
            self.register()
            with mock.patch.object(pe, "record_file_check",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(
                    cur, ["load", "--only", "register", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("FAILED", text)
            self.assertEqual(editions(cur, "register", SEP), [])
            self.assertEqual(count(cur, "zz_s24_reg_live"), 0)
            self.assertEqual(ledger(cur, "register"), [])
            logged.assert_called_once()
            self.assertIn("PARTIAL RUN", logged.call_args[0][2])
            self.assertIn(f"failed ['register {SEP}']",
                          logged.call_args[0][2])

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            self.register()
            text, logged = self.ok(cur, ["load", "--only", "register"])
            self.assertIn("would store edition 1", text)
            self.assertIn("PREVIEW: nothing written", text)
            for t in tables():
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--only", "register",
                                         "--simulate"])
            self.assertIn("SIMULATION", text)
            for t in tables():
                self.assertEqual(count(cur, t), 0, t)
            logged.assert_not_called()
            self.ok(cur, ["load", "--only", "register", "--commit"])
            calls = []
            real = m.register_identity
            with mock.patch.object(m, "register_identity",
                                   side_effect=lambda *a: calls.append(a)
                                   or real(*a)):
                text, logged = self.ok(cur, ["load", "--only", "register",
                                             "--commit"])
            self.assertEqual(calls, [])
            self.assertIn("already in the ledger", text)
            logged.assert_not_called()
            self.assertEqual(editions(cur, "register", SEP), [(1, None, 4)])
            self.assertEqual(len(ledger(cur, "register")), 1)

    def test_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.register()
            final = MEDIA.format("moved", "List.xlsx")
            self.final[self.last_url] = final
            self.ok(cur, ["load", "--only", "register", "--commit"])
            cur.execute("SELECT DISTINCT source_url FROM "
                        "public.zz_s24_reg_live")
            self.assertEqual(cur.fetchall(), [(final,)])
            self.assertTrue(ledger(cur, "register")[0][1].startswith(final))

    def test_one_url_two_files_are_two_periods_never_one(self):
        url = MEDIA.format("same", "List_of_registered_providers.xlsx")
        with rolled_back(self.conn) as cur:
            self.register("17 August 2026", url=url,
                          modified=datetime(2026, 8, 17, 10))
            self.ok(cur, ["load", "--only", "register", "--commit"])
            sep_rows = REG_ROWS[:3] + [("New Homes", "5300",
                                        datetime(2026, 9, 1), "Profit",
                                        "Company", None)]
            self.register("17 September 2026", url=url, rows=sep_rows)
            text, _ = self.ok(cur, ["load", "--only", "register",
                                    "--commit"])
            self.assertIn(f"register {SEP}: new", text)
            self.assertEqual(live_periods(cur, "register"),
                             [("2026-08-17", 4), (SEP, 4)])
            # the September bytes at that URL under the August title halt
            self.register("17 August 2026", url=url, rows=sep_rows)
            rc, text, _ = self.run_main(cur, ["load", "--only", "register",
                                              "--commit", "--recheck",
                                              "2026-08-17"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2026-08-17", text)
            self.assertEqual(editions(cur, "register", "2026-08-17"),
                             [(1, None, 4)])

    def test_equal_rank_different_content_stops_unless_accept_reissue(self):
        with rolled_back(self.conn) as cur:
            self.register()
            self.ok(cur, ["load", "--only", "register", "--commit"])
            rows = list(REG_ROWS)
            rows[0] = rows[0][:3] + ("Profit",) + rows[0][4:]
            self.register(rows=rows)            # same date, same modified
            rc, text, logged = self.run_main(
                cur, ["load", "--only", "register", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("REJECTED", text)
            self.assertIn("--accept-reissue", text)
            self.assertEqual(editions(cur, "register", SEP), [(1, None, 4)])
            self.assertEqual(len(ledger(cur, "register")), 1)
            logged.assert_called_once()
            self.assertIn("PARTIAL RUN", logged.call_args[0][2])
            self.assertIn(f"rejected ['register {SEP}']",
                          logged.call_args[0][2])
            rc, text, _ = self.run_main(
                cur, ["load", "--only", "register", "--commit",
                      "--accept-reissue", SEP])
            self.assertEqual(rc, 1, text)          # still needs --acknowledge
            text, logged = self.ok(cur, ["load", "--only", "register",
                                         "--commit", "--accept-reissue", SEP,
                                         "--acknowledge", SEP])
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertEqual(editions(cur, "register", SEP),
                             [(1, None, 4), (2, 1, 4)])
            self.assertIn("--accept-reissue", logged.call_args[0][2])

    def test_a_stop_condition_rejects_and_stores_nothing(self):
        many = [(f"P{i}", f"{9000 + i}", datetime(2001, 1, 1), "Profit",
                 "Company", None) for i in range(40)]
        with rolled_back(self.conn) as cur:
            self.register("17 August 2026", rows=many,
                          modified=datetime(2026, 8, 17, 10))
            self.ok(cur, ["load", "--only", "register", "--commit"])
            self.register()                    # 4 rows: 40 removed
            rc, text, logged = self.run_main(
                cur, ["load", "--only", "register", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("STOP CONDITION", text)
            self.assertEqual(editions(cur, "register", SEP), [])
            self.assertEqual(len(ledger(cur, "register")), 1)
            self.assertIn("PARTIAL RUN", logged.call_args[0][2])
            text, logged = self.ok(cur, ["load", "--only", "register",
                                         "--commit", "--acknowledge", SEP])
            self.assertIn("ACKNOWLEDGED", text)
            self.assertEqual(editions(cur, "register", SEP), [(1, None, 4)])
            self.assertIn(f"ACKNOWLEDGED {SEP}", logged.call_args[0][2])

    def test_an_acknowledgement_must_name_a_period_of_the_run(self):
        with rolled_back(self.conn) as cur:
            self.register()
            rc, text, logged = self.run_main(
                cur, ["load", "--only", "register", "--commit",
                      "--acknowledge", "2026-01-01"])
            self.assertEqual(rc, "halt", text)
            self.assertEqual(count(cur, "zz_s24_reg_editions"), 0)
            logged.assert_not_called()

    def test_file_paths(self):
        with rolled_back(self.conn) as cur:
            path = self.register(page=False)
            rc, text, _ = self.run_main(cur, ["load", "--only", "register",
                                              "--file", path])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--title-date", text)
            self.api[m.REGISTER_PATH] = register_page("17 August 2026",
                                                      "https://x/aug.xlsx")
            text, _ = self.ok(cur, ["load", "--only", "register", "--file",
                                    path, "--title-date", SEP, "--commit"])
            self.assertIn(f"register {SEP}: new", text)
            cur.execute("SELECT DISTINCT source_url, release_page_url FROM "
                        "public.zz_s24_reg_live")
            self.assertEqual(cur.fetchall(), [(f"file:{path.name}",
                                               m.GOV_UK + m.REGISTER_PATH)])
            rc, text, _ = self.run_main(cur, ["load", "--file", path,
                                              "--title-date", SEP])
            self.assertEqual(rc, "halt", text)          # --file needs --only
            rc, text, _ = self.run_main(cur, ["load", "--only", "register",
                                              "--no-page"])
            self.assertEqual(rc, "halt", text)          # --no-page needs --file


class LoadJudgements(Fixture):

    def test_a_newer_edition_replaces_live_and_keeps_the_older(self):
        with rolled_back(self.conn) as cur:
            self.judgements("20260812", jrows=JROWS[:4])
            text, _ = self.ok(cur, ["load", "--only", "judgements",
                                    "--commit"])
            self.assertIn(f"judgements {J_AUG}: new", text)
            self.assertIn(f"notices {J_AUG}: new", text)
            self.judgements("20260930")
            text, logged = self.ok(cur, ["load", "--only", "judgements",
                                         "--commit"])
            self.assertIn(f"judgements {J_SEP}: new", text)
            self.assertEqual(live_periods(cur, "judgements"),
                             [(J_SEP, len(JROWS))])
            self.assertEqual(live_periods(cur, "notices"), [(J_SEP, 1)])
            self.assertEqual(editions(cur, "judgements", J_AUG),
                             [(1, None, 4)])
            self.assertEqual(editions(cur, "judgements", J_SEP),
                             [(1, None, len(JROWS))])
            st = pe.status(cur, m.profile("judgements"))
            self.assertTrue(st["ok"], st)
            self.assertEqual(st["history"], [J_AUG])
            self.assertEqual([r[2] for r in ledger(cur, "notices")],
                             ["new", "new"])
            self.assertIn("2026-08-12", text)          # newest dates printed

    def test_reasons_are_stored_for_every_null_grade(self):
        with rolled_back(self.conn) as cur:
            self.judgements()
            self.ok(cur, ["load", "--only", "judgements", "--commit"])
            cur.execute("SELECT r.reason, COUNT(*) FROM "
                        "public.zz_s24_jud_editions e, jsonb_each_text("
                        "e.null_reasons) r(col, reason) WHERE r.col IN "
                        "('consumer_grade', 'governance_grade', "
                        "'viability_grade', 'rent_grade') GROUP BY 1")
            got = dict(cur.fetchall())
            self.assertEqual(got, {"not_applicable_local_authority": 2,
                                   "not_assessed_yet": 1,
                                   "marked_dash_no_reason": 3 + 5})
            for col in ("consumer_grade", "governance_grade",
                        "viability_grade", "rent_grade"):
                cur.execute(f"SELECT COUNT(*) FROM public.zz_s24_jud_live "
                            f"WHERE {col} IS NULL")
                n_live = cur.fetchone()[0]
                cur.execute(f"SELECT COUNT(*) FROM public.zz_s24_jud_editions"
                            f" WHERE {col} IS NULL AND null_reasons ? %s",
                            (col,))
                self.assertEqual(cur.fetchone()[0], n_live, col)

    def test_an_older_file_halts_on_every_path(self):
        with rolled_back(self.conn) as cur:
            self.judgements("20260930")
            self.ok(cur, ["load", "--only", "judgements", "--commit"])
            old = self.judgements("20260812", jrows=JROWS[:4])
            for argv in (["load", "--only", "judgements", "--commit"],
                         ["load", "--only", "judgements", "--file", old,
                          "--commit"],
                         ["load", "--only", "judgements", "--file", old,
                          "--no-page", "--commit"]):
                with self.subTest(argv=argv):
                    rc, text, logged = self.run_main(cur, argv)
                    self.assertEqual(rc, "halt", text)
                    self.assertIn("older", text)
                    self.assertIn("--allow-older-file", text)
                    self.assertEqual(editions(cur, "judgements", J_AUG), [])
                    logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--only", "judgements",
                                         "--allow-older-file", "--commit"])
            self.assertIn("held-not-live", text)
            self.assertEqual(editions(cur, "judgements", J_AUG),
                             [(1, None, 4)])
            self.assertEqual(live_periods(cur, "judgements"),
                             [(J_SEP, len(JROWS))])
            self.assertIn("--allow-older-file", logged.call_args[0][2])

    def test_a_file_whose_date_the_page_does_not_list_halts(self):
        with rolled_back(self.conn) as cur:
            self.judgements("20260930", changes=("2026-09-09",))
            self.api[m.JUDGEMENTS_PATH]["public_updated_at"] = \
                "2026-09-10T09:30:00+01:00"
            rc, text, _ = self.run_main(cur, ["load", "--only", "judgements"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2026-09-09", text)

    def test_a_new_period_waits_while_live_holds_two_periods(self):
        with rolled_back(self.conn) as cur:
            self.judgements("20260812", jrows=JROWS[:4] + [
                jrow(reg="L0009", landlord="Gone Homes")])
            self.ok(cur, ["load", "--only", "judgements", "--commit"])
            self.judgements("20260930")
            self.ok(cur, ["load", "--only", "judgements", "--commit"])
            # the old build's left-over row of an older edition, put back
            cols = ", ".join(("registration_number", "publication_date",
                              "edition_date") + m.JUDGEMENT_VALUES)
            cur.execute(f"INSERT INTO public.zz_s24_jud_live ({cols}, "
                        "source_url, source_file, release_page_url) SELECT "
                        f"{cols}, file_source_url, file_name, "
                        "file_release_page_url FROM public.zz_s24_jud_editions"
                        " WHERE registration_number = 'L0009'")
            self.judgements("20261007", changes=("2026-10-07",))
            rc, text, _ = self.run_main(cur, ["load", "--only", "judgements",
                                              "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("refresh-latest", text)
            self.assertEqual(editions(cur, "judgements", "2026-10-07"), [])


class Fetch(unittest.TestCase):
    """fetch() itself with a stand-in session (no network)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def resp(self, body, url):
        return mock.Mock(content=body, url=url, raise_for_status=lambda: None)

    def test_same_name_rule_and_final_url(self):
        a = write_register(self.root / "src" / "a.xlsx").read_bytes()
        b = write_register(self.root / "src" / "b.xlsx",
                           rows=REG_ROWS[:2]).read_bytes()
        dest = self.root / "raw"
        url = "https://assets.example/media/1/List.xlsx"
        final = "https://assets.example/media/2/List.xlsx"
        sess = mock.Mock()
        sess.get.return_value = self.resp(a, final)
        self.enterContext(mock.patch.object(m, "MIN_FILE_BYTES", 1000))
        with quiet():
            p1, f1 = m.fetch(url, dest, session=sess)
        self.assertEqual((p1.name, f1), ("List.xlsx", final))
        with quiet():
            p2, _ = m.fetch(url, dest, session=sess)   # same bytes: kept
        self.assertEqual(p2, p1)
        self.assertEqual(sess.get.call_count, 2)       # never skipped
        sess.get.return_value = self.resp(b, final)
        with quiet():
            p3, _ = m.fetch(url, dest, session=sess)
        self.assertEqual(p3.name, f"List-{m.content_sha256(p3)[:8]}.xlsx")
        self.assertEqual(p1.read_bytes(), a)           # never replaced
        sess.get.return_value = self.resp(b"<html>no</html>", final)
        with self.assertRaises(SystemExit), quiet():
            m.fetch(url, dest, session=sess)


# ---------------------------------------------------------------------------
# migrate-legacy, the held state, corrections, refresh, restore
# ---------------------------------------------------------------------------

JUL = "2026-07-24"
AUG = "2026-08-17"
AUG_ROWS = JROWS[:4] + [jrow(reg="L0009", landlord="Gone Homes")]


class Held(Fixture):
    """The held state seeded into the throwaway live tables as the old build
    left it: register July and September from files, August with notes
    'None' and no file; judgements of 30 September from its file plus one
    left-over row of 12 August; notices of 30 September."""

    def seed(self, cur, *, plant=False):
        jul = self.register("24 July 2026", modified=datetime(2026, 7, 27, 13),
                            page=False)
        sep = self.register("17 September 2026", rows=REG_ROWS[:3],
                            page=False)
        jaug = self.judgements("20260812", jrows=AUG_ROWS, page=False)
        jsep = self.judgements("20260930", page=False)
        self.paths = {("register", JUL): jul, ("register", SEP): sep,
                      ("judgements", J_AUG): jaug,
                      ("judgements", J_SEP): jsep}
        self.urls_of = {k: next(u for u, p in self.urls.items() if p == v)
                        for k, v in self.paths.items()}
        held = {k: {"name": p.name, "sha256": m.content_sha256(p),
                    "url": self.urls_of[k],
                    "modified": m._iso(m.docprops_modified(p))}
                for k, p in self.paths.items()}
        page = m.GOV_UK + m.REGISTER_PATH
        loaded = {JUL: "2026-08-14 21:26:52+00", AUG: "2026-08-20 14:25:27+00",
                  SEP: "2026-10-01 21:59:19+00"}
        for k in (("register", JUL), ("register", SEP)):
            ident = m.register_identity(self.paths[k], k[1])
            for r in m.build_register(ident["rows"], ident, rules="old"):
                self.insert(cur, "register", r, held[k], page, loaded[k[1]])
        ident = m.register_identity(jul, JUL)
        for r in m.build_register(ident["rows"], ident, rules="old"):
            r = dict(r, snapshot_date=AUG, notes="None")
            self.insert(cur, "register", r, {
                "url": "https://x/media/aug/List_17_August_2026.xlsx",
                "name": "List_17_August_2026.xlsx"}, page, loaded[AUG])
        jpage = m.GOV_UK + m.JUDGEMENTS_PATH
        ident = m.judgements_identity(jsep, jsep.name, None)
        for r in m.build_judgements(ident["judgement_rows"], ident,
                                    rules="old"):
            if plant and r["registration_number"] == "L0004":
                r = dict(r, rent_grade=None)
            self.insert(cur, "judgements", r, held[("judgements", J_SEP)],
                        jpage, loaded[SEP])
        for r in m.build_notices(ident["notice_rows"], ident, rules="old"):
            self.insert(cur, "notices", r, held[("judgements", J_SEP)],
                        jpage, loaded[SEP])
        ident = m.judgements_identity(jaug, jaug.name, None)
        for r in m.build_judgements(ident["judgement_rows"], ident,
                                    rules="old"):
            if r["registration_number"] == "L0009":
                self.insert(cur, "judgements", r,
                            held[("judgements", J_AUG)], jpage,
                            "2026-08-14 21:26:52+00")
        self.legacy = {n: m.live_state(cur, n) for n in ZZ}
        self.held = held
        self.run_rows = 4 + len(AUG_ROWS) + len(NROWS)

    def insert(self, cur, name, r, held, page, loaded):
        s = ZZ[name]
        cols = (tuple(s.key_cols) + (s.period_col,)
                + tuple(c for c, _ in s.value_cols))
        vals = [r[c] for c in cols] + [held["url"], held["name"], page,
                                       loaded]
        cur.execute(f"INSERT INTO public.{s.live_table} ({', '.join(cols)}, "
                    "source_url, source_file, release_page_url, loaded_at) "
                    f"VALUES ({', '.join(['%s'] * len(vals))})", vals)

    def migrate(self, cur, *, write=True, **kw):
        args = dict(legacy=self.legacy, held_files=self.held)
        args.update(kw)
        with specs(), \
                mock.patch.object(m, "AUGUST_REGISTER",
                                  dict(m.AUGUST_REGISTER, period=AUG)), \
                mock.patch.object(m, "LEGACY_RUN",
                                  dict(m.LEGACY_RUN, rows=self.run_rows,
                                       register=JUL, judgements=J_AUG)), \
                mock.patch.object(m, "legacy_run_rows",
                                  return_value=self.run_rows), quiet() as out:
            plan = m.migrate_legacy(cur, self.paths, write=write, **args)
        return plan, out


class Migrate(Held):

    def test_the_proof_passes_and_edition_1_is_as_held(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = {n: m.live_state(cur, n) for n in ZZ}
            plan, out = self.migrate(cur)
            self.assertEqual({n: m.live_state(cur, n) for n in ZZ}, before)
            self.assertEqual(editions(cur, "register", JUL), [(1, None, 4)])
            self.assertEqual(editions(cur, "register", AUG), [(1, None, 4)])
            self.assertEqual(editions(cur, "register", SEP), [(1, None, 3)])
            self.assertEqual(editions(cur, "judgements", J_SEP),
                             [(1, None, len(JROWS))])
            self.assertEqual(editions(cur, "judgements", J_AUG),
                             [(1, None, len(AUG_ROWS))])
            self.assertEqual(editions(cur, "notices", J_SEP), [(1, None, 1)])
            self.assertEqual(editions(cur, "notices", J_AUG), [(1, None, 1)])
            for p in (JUL, AUG, SEP):
                self.assertEqual(core.rows_differing(cur, ZZ["register"], p,
                                                     1), 0)
            self.assertEqual(core.rows_differing(cur, ZZ["judgements"], J_SEP,
                                                 1), 0)
            # the register's reasons come from the proved file; the August
            # snapshot keeps its 'None' text; judgements say not recorded
            cur.execute("SELECT null_reasons FROM public.zz_s24_reg_editions "
                        "WHERE registration_number = '4001' ORDER BY "
                        "snapshot_date")
            self.assertEqual([r[0] for r in cur.fetchall()],
                             [{"notes": "blank"}, {}, {"notes": "blank"}])
            cur.execute("SELECT DISTINCT r.reason FROM "
                        "public.zz_s24_jud_editions e, jsonb_each_text("
                        "e.null_reasons) r(col, reason)")
            self.assertEqual(cur.fetchall(),
                             [("reason_not_recorded_as_loaded",)])
            cur.execute("SELECT DISTINCT release_label, source_file FROM "
                        "public.zz_s24_reg_editions WHERE snapshot_date = %s",
                        (AUG,))
            (label, src), = cur.fetchall()
            self.assertIn("file not obtainable", label)
            self.assertIsNone(m.release_rank(src))
            self.assertEqual(len(ledger(cur, "register")), 2)
            self.assertEqual(len(ledger(cur, "judgements")), 2)
            self.assertEqual(len(ledger(cur, "notices")), 2)
            self.assertTrue(all("as loaded" in r[1]
                                for r in ledger(cur, "judgements")))
            self.assertIn("0 differences", out.getvalue())

    def test_a_preview_writes_nothing(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.migrate(cur, write=False)
            for t in tables():
                if not t.endswith("_live"):
                    self.assertEqual(count(cur, t), 0, t)

    def test_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur, plant=True)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur)
            self.assertIn("proof failed", str(e.exception.code))
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            held = {k: dict(v) for k, v in self.held.items()}
            held[("judgements", J_SEP)]["sha256"] = "0" * 32 + "f" * 32
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, held_files=held)
            self.assertIn("sha256", str(e.exception.code))
            for t in tables():
                if not t.endswith("_live"):
                    self.assertEqual(count(cur, t), 0, t)
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            legacy = dict(self.legacy)
            legacy["register"] = dict(legacy["register"], rows=1)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, legacy=legacy)
            self.assertIn("not as surveyed", str(e.exception.code))
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.migrate(cur)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur)
            self.assertIn("already holds", str(e.exception.code))


class BeforeDdl(Held):

    def test_a_preview_against_live_compares_values_only(self):
        with rolled_back(self.conn, ddl=False) as cur:
            self.seed(cur)
            url = self.urls_of[("register", SEP)]
            self.api[m.REGISTER_PATH] = register_page("17 September 2026",
                                                      url)
            self.urls[url] = self.paths[("register", SEP)]
            jurl = self.urls_of[("judgements", J_SEP)]
            self.api[m.JUDGEMENTS_PATH] = judgements_page(jurl)
            self.urls[jurl] = self.paths[("judgements", J_SEP)]
            with mock.patch.object(m, "LEGACY_FILES", self.held):
                text, _ = self.ok(cur, ["load"])
            self.assertIn("compares the file with the LIVE table", text)
            self.assertIn(f"register {SEP}: unchanged", text)
            self.assertIn(f"judgements {J_SEP}: unchanged", text)
            self.assertIn(f"notices {J_SEP}: unchanged", text)
            self.assertNotIn("REJECTED", text)


class AfterMigrate(Held):

    def prepared(self, cur):
        self.seed(cur)
        self.migrate(cur)

    def test_a_byte_identical_reread_is_unchanged_on_every_path(self):
        with rolled_back(self.conn) as cur:
            self.prepared(cur)
            sep = self.paths[("register", SEP)]
            self.api[m.REGISTER_PATH] = register_page(
                "17 September 2026", self.urls_of[("register", SEP)])
            self.urls[self.urls_of[("register", SEP)]] = sep
            text, logged = self.ok(cur, ["load", "--only", "register",
                                         "--commit"])
            self.assertIn("already in the ledger", text)
            logged.assert_not_called()
            text, _ = self.ok(cur, ["load", "--only", "register",
                                    "--recheck", SEP, "--commit"])
            self.assertIn(f"register {SEP}: unchanged", text)
            text, _ = self.ok(cur, ["load", "--only", "register", "--file",
                                    sep, "--title-date", SEP, "--commit"])
            self.assertIn(f"register {SEP}: unchanged", text)
            jul = self.paths[("register", JUL)]
            text, _ = self.ok(cur, ["load", "--only", "register", "--file",
                                    jul, "--title-date", JUL, "--no-page",
                                    "--commit"])
            self.assertIn("s24-aug-notes-none-2026-10", text)   # pending note
            self.assertIn(f"register {JUL}: unchanged", text)
            for p in (JUL, SEP):
                self.assertEqual(editions(cur, "register", p)[-1][0], 1)

    def test_the_judgements_reread_is_edition_2_with_its_reasons(self):
        with rolled_back(self.conn) as cur:
            self.prepared(cur)
            jsep = self.paths[("judgements", J_SEP)]
            rc, text, logged = self.run_main(
                cur, ["load", "--only", "judgements", "--file", jsep,
                      "--no-page", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("REJECTED", text)
            self.assertIn("--acknowledge", text)
            self.assertNotIn("--accept-reissue", text)   # the same bytes
            self.assertEqual(editions(cur, "judgements", J_SEP),
                             [(1, None, len(JROWS))])
            text, logged = self.ok(cur, ["load", "--only", "judgements",
                                         "--file", jsep, "--no-page",
                                         "--commit", "--acknowledge", J_SEP])
            self.assertIn(f"judgements {J_SEP}: revised", text)
            self.assertEqual(editions(cur, "judgements", J_SEP),
                             [(1, None, len(JROWS)), (2, 1, len(JROWS))])
            self.assertEqual(editions(cur, "notices", J_SEP),
                             [(1, None, 1), (2, 1, 1)])
            # values unchanged, so live equals edition 2 on every compared
            # column; the provenance is the tip's (the same bytes)
            self.assertEqual(core.rows_differing(cur, ZZ["judgements"], J_SEP,
                                                 2), 0)
            cur.execute("SELECT DISTINCT file_source_url FROM "
                        "public.zz_s24_jud_editions WHERE edition = 2")
            self.assertEqual(cur.fetchall(),
                             [(self.urls_of[("judgements", J_SEP)],)])
            text, _ = self.ok(cur, ["load", "--only", "judgements", "--file",
                                    jsep, "--recheck", J_SEP, "--no-page"])
            self.assertIn(f"judgements {J_SEP}: unchanged", text)

    def test_the_august_correction_is_named_and_acknowledged(self):
        with rolled_back(self.conn) as cur:
            self.prepared(cur)
            self.api[m.REGISTER_PATH] = register_page(
                "17 September 2026", self.urls_of[("register", SEP)])
            corr = {"s24-aug-notes-none-2026-10": dict(
                m.CORRECTIONS["s24-aug-notes-none-2026-10"], period=AUG,
                cells=4)}
            name = "s24-aug-notes-none-2026-10"
            # the real count (1,578) does not fit the fixture: rejected
            rc, text, _ = self.run_main(cur, ["load", "--only", "register",
                                              "--correction", name,
                                              "--acknowledge", AUG,
                                              "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("1,578", text)
            with mock.patch.object(m, "CORRECTIONS", corr):
                rc, text, _ = self.run_main(cur, ["load", "--only",
                                                  "register", "--commit",
                                                  "--correction", name])
                self.assertEqual(rc, 1, text)
                self.assertIn(name, text)
                self.assertIn("--acknowledge", text)
                self.assertEqual(editions(cur, "register", AUG),
                                 [(1, None, 4)])
                text, logged = self.ok(cur, ["load", "--only", "register",
                                             "--commit", "--correction",
                                             name, "--acknowledge", AUG])
                self.assertEqual(editions(cur, "register", AUG),
                                 [(1, None, 4), (2, 1, 4)])
                cur.execute("SELECT DISTINCT notes, null_reasons FROM "
                            "public.zz_s24_reg_editions WHERE snapshot_date "
                            "= %s AND edition = 2", (AUG,))
                self.assertEqual(cur.fetchall(), [(None, {
                    "notes": "stored_text_none_not_from_file"})])
                self.assertIn("s24-aug-notes-none-2026-10",
                              logged.call_args[0][2])
                # applied once: the next run proposes nothing
                text, _ = self.ok(cur, ["load", "--only", "register",
                                        "--correction", name])
                self.assertIn("already applied", text)
                # refresh-latest takes it into live
                text, _ = self.ok(cur, ["refresh-latest", "--accept-key-"
                                        "changes", J_SEP, "--commit"])
                cur.execute("SELECT COUNT(*) FROM public.zz_s24_reg_live "
                            "WHERE snapshot_date = %s AND notes IS NULL",
                            (AUG,))
                self.assertEqual(cur.fetchone()[0], 4)

    def test_status_and_refresh_with_the_legacy_two_periods(self):
        with rolled_back(self.conn) as cur:
            self.prepared(cur)
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1, text)
            self.assertIn("live holds 2 periods", text)
            rc, text, _ = self.run_main(cur, ["refresh-latest"])
            self.assertEqual(rc, 0, text)
            self.assertIn("1 left-over live row(s) of 2026-08-12", text)
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--accept-key-changes", text)
            self.assertEqual(live_periods(cur, "judgements"),
                             [(J_AUG, 1), (J_SEP, len(JROWS))])
            text, _ = self.ok(cur, ["refresh-latest", "--accept-key-changes",
                                    J_SEP, "--commit"])
            self.assertEqual(live_periods(cur, "judgements"),
                             [(J_SEP, len(JROWS))])
            self.assertEqual(editions(cur, "judgements", J_AUG),
                             [(1, None, len(AUG_ROWS))])
            st = pe.status(cur, m.profile("judgements"))
            self.assertTrue(st["ok"], st)
            self.assertEqual(st["history"], [J_AUG])

    def test_restore_edition_round_trip_for_each_spec(self):
        with rolled_back(self.conn) as cur:
            self.prepared(cur)
            self.ok(cur, ["load", "--only", "judgements", "--file",
                          self.paths[("judgements", J_SEP)], "--no-page",
                          "--commit", "--acknowledge", J_SEP])
            for name, p in (("judgements", J_SEP), ("notices", J_SEP)):
                s = ZZ[name]
                text, logged = self.ok(cur, ["restore-edition", s.live_table,
                                             p, "1", "--commit"])
                self.assertIn("as edition 3", text)
                a = m.records(cur, s, p, 1)
                b = m.records(cur, s, p, 3)
                self.assertEqual(m.rows_content_sha(a),
                                 m.rows_content_sha(b))
            with specs():
                corr = m.CORRECTIONS["s24-aug-notes-none-2026-10"]
                tip = m.records(cur, ZZ["register"], AUG, 1)
                fixed, n = m.correction_records(tip, dict(corr, cells=4))
                info = m.PeriodInfo("correction", "test correction",
                                    date(2026, 10, 11), ())
                cur.execute("SAVEPOINT t")
                m.apply_part(cur, m.profile("register"), AUG, fixed,
                             info=info)
                cur.execute("RELEASE SAVEPOINT t")
            text, _ = self.ok(cur, ["restore-edition", "register", AUG, "1",
                                    "--commit"])
            self.assertIn("as edition 3", text)
            rc, text, _ = self.run_main(cur, ["restore-edition", "register",
                                              AUG, "3", "--commit"])
            self.assertEqual(rc, "halt", text)            # the tip


class Status(Fixture):

    def test_no_editions_tables_is_a_clean_message(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1, text)
            self.assertIn("not present yet", text)
            self.assertIn("ddl --commit", text)
            cur.execute("SELECT to_regclass('public.zz_s24_reg_editions')")
            self.assertIsNone(cur.fetchone()[0])

    def test_preview_before_ddl_compares_with_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            self.register()
            text, _ = self.ok(cur, ["load", "--only", "register"])
            self.assertIn("compares the file with the LIVE table", text)
            self.assertIn(f"register {SEP}: new", text)
            rc, text, _ = self.run_main(cur, ["load", "--only", "register",
                                              "--commit"])
            self.assertEqual(rc, "halt", text)


if __name__ == "__main__":
    unittest.main()
