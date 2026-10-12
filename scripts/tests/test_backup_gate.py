"""Tests for gate 22 of verify_source_registry (docs/RULES.md rule 9).

The gate body (backup_gate) is called against throwaway zz_bkt_* tables made
inside a transaction that is always rolled back; `like` narrows it to them,
so no real table is read except in the last test, which runs the gate on the
real register read-only. tearDownClass fails if a zz_bkt table was left."""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verify_source_registry as v  # noqa: E402
from _db import get_conn, get_readonly_conn  # noqa: E402

LIKE = "zz\\_bkt%\\_bak\\_%"
T = "zz_bkt_src_bak_20261012"
D = datetime.date(2026, 10, 12)
HEAD = ("| Table | Created | Source table | Rows | Size MB | Reason | Asked by "
        "| Expiry | Extended to | Status | Dropped |\n|---|---|---|---|---|---|"
        "---|---|---|---|---|\n")


def reg(*rows, log=""):
    body = "".join(
        f"| `{r.get('t', T)}` | 2026-10-12 | `zz_bkt_src` | {r.get('n', '3')} "
        f"| 0.01 | test | Scott | {r.get('exp', '2026-11-11')} "
        f"| {r.get('ext', '-')} | {r.get('st', 'active')} "
        f"| {r.get('dr', '-')} |\n" for r in rows)
    return "# Backup register\n\n" + HEAD + body + "\n## Log\n\n" + log


def leftovers():
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT relname FROM pg_class WHERE relname LIKE "
                        "'zz%%' AND relnamespace = 'public'::regnamespace")
            return sorted(r[0] for r in cur.fetchall())
    finally:
        conn.rollback()
        conn.close()


class BackupGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.rollback()
            cls.conn.close()
        finally:
            left = [t for t in leftovers() if t.startswith("zz_bkt")]
            if left:
                raise AssertionError(f"committed throwaway tables: {left}")

    def setUp(self):
        self.cur = self.conn.cursor()
        self.addCleanup(self.rollback)
        self.cur.execute("CREATE TABLE zz_bkt_src (id int PRIMARY KEY)")
        self.cur.execute("INSERT INTO zz_bkt_src VALUES (1), (2), (3)")
        self.cur.execute(f"CREATE TABLE {T} AS SELECT * FROM zz_bkt_src")

    def rollback(self):
        self.cur.close()
        self.conn.rollback()

    def run_gate(self, text, today=D):
        return v.backup_gate(self.cur, text, today, like=LIKE)

    def test_registered_and_in_date_passes(self):
        ok, detail = self.run_gate(reg({}))
        self.assertTrue(ok, detail)
        self.assertIn("1 backup table(s)", detail)
        self.assertIn("next expiry 2026-11-11", detail)

    def test_unregistered_fails(self):
        ok, detail = self.run_gate(reg({"t": "zz_bkt_other_bak_20261012"}))
        self.assertFalse(ok)
        self.assertIn(f"{T}: not in docs/BACKUPS.md", detail)
        self.assertIn("zz_bkt_other_bak_20261012: says active but the table "
                      "is absent", detail)

    def test_expiry_day_passes_day_after_fails(self):
        self.assertTrue(self.run_gate(reg({}), datetime.date(2026, 11, 11))[0])
        ok, detail = self.run_gate(reg({}), datetime.date(2026, 11, 12))
        self.assertFalse(ok)
        self.assertIn("expired 2026-11-11", detail)

    def test_extension_needs_log_line_and_at_most_30_days(self):
        late = datetime.date(2026, 11, 20)
        ok, detail = self.run_gate(reg({"ext": "2026-12-01"}), late)
        self.assertFalse(ok)
        self.assertIn("no dated line", detail)
        log = f"- 2026-11-10: {T} extended to 2026-12-01 (Scott).\n"
        self.assertTrue(self.run_gate(reg({"ext": "2026-12-01"}, log=log),
                                      late)[0])
        ok, detail = self.run_gate(reg({"ext": "2026-12-20"}, log=log), late)
        self.assertFalse(ok)
        self.assertIn("is not one of up to 30 days", detail)

    def test_row_count_drift_fails_unless_modified(self):
        self.cur.execute(f"INSERT INTO {T} VALUES (4)")
        ok, detail = self.run_gate(reg({}))
        self.assertFalse(ok)
        self.assertIn("holds 4 rows, registered 3", detail)
        self.assertTrue(self.run_gate(reg({"st": "modified"}))[0])

    def test_dependent_view_function_and_fk_fail(self):
        self.cur.execute(f"CREATE VIEW zz_bkt_v AS SELECT id FROM {T}")
        ok, detail = self.run_gate(reg({}))
        self.assertFalse(ok)
        self.assertIn("zz_bkt_v", detail)
        self.cur.execute("DROP VIEW zz_bkt_v")
        self.cur.execute(f"CREATE FUNCTION zz_bkt_f() RETURNS bigint "
                         f"LANGUAGE sql AS 'SELECT count(*) FROM {T}'")
        ok, detail = self.run_gate(reg({}))
        self.assertFalse(ok)
        self.assertIn("zz_bkt_f() (function)", detail)
        self.cur.execute("DROP FUNCTION zz_bkt_f()")
        self.cur.execute(f"ALTER TABLE {T} ADD CONSTRAINT zz_bkt_fk "
                         f"FOREIGN KEY (id) REFERENCES zz_bkt_src (id)")
        ok, detail = self.run_gate(reg({}))
        self.assertFalse(ok)
        self.assertIn("zz_bkt_fk (foreign key)", detail)

    def test_bad_name_fails(self):
        self.cur.execute("CREATE TABLE zz_bkt_x_bak_2026 AS SELECT 1 AS a")
        ok, detail = self.run_gate(reg({}, {"t": "zz_bkt_x_bak_2026", "n": "1"}))
        self.assertFalse(ok)
        self.assertIn("zz_bkt_x_bak_2026: name is not", detail)

    def test_dropped_row_kept_as_history(self):
        gone = "2026-11-11; 3 rows; sha256 " + "ab" * 32
        rows = ({}, {"t": "zz_bkt_old_bak_20260901", "st": "dropped",
                     "dr": gone})
        self.assertTrue(self.run_gate(reg(*rows))[0])
        rows = ({}, {"t": "zz_bkt_old_bak_20260901", "st": "dropped",
                     "dr": "2026-11-11"})
        ok, detail = self.run_gate(reg(*rows))
        self.assertFalse(ok)
        self.assertIn("lacks date, row count and sha256", detail)
        ok, detail = self.run_gate(reg({"st": "dropped", "dr": gone}))
        self.assertFalse(ok)
        self.assertIn("registered as dropped but still exists", detail)


class RealRegister(unittest.TestCase):
    def test_real_register_passes_today_read_only(self):
        conn = get_readonly_conn()
        try:
            with conn.cursor() as cur:
                ok, detail = v.backup_gate(
                    cur, v.BACKUP_REGISTER.read_text(encoding="utf-8"), D)
        finally:
            conn.rollback()
            conn.close()
        self.assertTrue(ok, detail)
        # All 19 grandfathered tables were dropped on 2026-10-12 (Scott);
        # their register rows stay as history, every one marked dropped.
        self.assertIn("0 backup table(s)", detail)
        rows, _ = v.parse_backup_register(
            v.BACKUP_REGISTER.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 19)
        self.assertEqual({r["status"] for r in rows}, {"dropped"})


if __name__ == "__main__":
    unittest.main()
