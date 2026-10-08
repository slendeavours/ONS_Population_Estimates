"""Tests for migrate_months in s19_pip_editions (the one-off relabel of the
live table's months from text labels such as 'Apr-26' to 'yyyymm' keys).

Database tests run on a connection from _db.get_conn() inside a transaction
that is always rolled back (rolled_back below); nothing here commits. The only
table touched is the throwaway zz_s19_live, created inside that transaction
with the real la_pip_claimants column layout, so it never persists. The real
table is never read or written here.
"""
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s19_pip_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402

T = "zz_s19_live"
CODES = [f"E06{n:06d}" for n in range(6)]
OLD = "2026-05-01 09:00:00+00"


@contextmanager
def rolled_back(conn):
    """A cursor in a transaction that is rolled back whatever happens."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.zz_s19_live')")
        if cur.fetchone() != (None,):
            raise RuntimeError("zz_s19_live already exists as a real table; "
                               "refusing to run")
        cur.execute("""CREATE TABLE public.zz_s19_live (
            lad24cd text NOT NULL, month text NOT NULL,
            pip_total_claimants integer, pip_enhanced_daily_living integer,
            loaded_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (lad24cd, month))""")
        yield cur
    finally:
        cur.close()
        conn.rollback()


def seed(cur, months):
    """Rows for each month: a NULL and a 0 among the values, varied per
    month so the per-month totals differ; loaded_at set to a fixed past
    time so any rewrite of it would show."""
    for j, month in enumerate(months):
        for i, lad in enumerate(CODES):
            total = None if i == 0 else (0 if i == 1 else 100 * i + j)
            enh = None if i == 2 else 10 * i + j
            cur.execute(f"INSERT INTO public.{T} (lad24cd, month, "
                        "pip_total_claimants, pip_enhanced_daily_living, "
                        "loaded_at) VALUES (%s, %s, %s, %s, %s)",
                        (lad, month, total, enh, OLD))


def snapshot(cur):
    cur.execute(f"SELECT lad24cd, month, pip_total_claimants, "
                f"pip_enhanced_daily_living, loaded_at FROM public.{T} "
                "ORDER BY 1, 2")
    return cur.fetchall()


def latest_rows(cur):
    """What workflow 1 selects: the rows of MAX(month), by area."""
    cur.execute(f"SELECT lad24cd, pip_total_claimants, "
                f"pip_enhanced_daily_living FROM public.{T} WHERE month = "
                f"(SELECT MAX(month) FROM public.{T}) ORDER BY 1")
    return cur.fetchall()


def months(cur):
    cur.execute(f"SELECT DISTINCT month FROM public.{T} ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


class Migration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_relabels_and_preserves_totals_and_rows(self):
        with rolled_back(self.conn) as cur:
            seed(cur, ["Apr-26", "Jul-26"])
            cur.execute(f"SELECT month, COUNT(*), SUM(pip_total_claimants), "
                        f"SUM(pip_enhanced_daily_living) FROM public.{T} "
                        "GROUP BY 1")
            before = {r[0]: r[1:] for r in cur.fetchall()}
            res = m.migrate_months(cur, T)
            self.assertEqual(res["status"], "migrated")
            self.assertEqual(months(cur), ["202604", "202607"])
            cur.execute(f"SELECT month, COUNT(*), SUM(pip_total_claimants), "
                        f"SUM(pip_enhanced_daily_living) FROM public.{T} "
                        "GROUP BY 1")
            after = {r[0]: r[1:] for r in cur.fetchall()}
            self.assertEqual(after, {"202604": before["Apr-26"],
                                     "202607": before["Jul-26"]})
            self.assertEqual(res["before"]["rows"], 2 * len(CODES))
            self.assertEqual(res["after"]["rows"], 2 * len(CODES))
            self.assertEqual(res["before"]["months"], ["Apr-26", "Jul-26"])
            self.assertEqual(res["after"]["months"], ["202604", "202607"])

    def test_idempotent_second_run_changes_nothing(self):
        with rolled_back(self.conn) as cur:
            seed(cur, ["Apr-26", "Jul-26"])
            m.migrate_months(cur, T)
            snap = snapshot(cur)
            self.assertEqual(m.migrate_months(cur, T),
                             {"status": "already migrated"})
            self.assertEqual(snapshot(cur), snap)

    def test_unknown_label_halts_and_changes_nothing(self):
        with rolled_back(self.conn) as cur:
            seed(cur, ["Apr-26", "Sept-26"])
            snap = snapshot(cur)
            with self.assertRaises(SystemExit) as ctx:
                m.migrate_months(cur, T)
            self.assertIn("Sept-26", str(ctx.exception.code))
            self.assertEqual(snapshot(cur), snap)

    def test_mixed_forms_halt(self):
        with rolled_back(self.conn) as cur:
            seed(cur, ["Apr-26", "202607"])
            snap = snapshot(cur)
            with self.assertRaises(SystemExit) as ctx:
                m.migrate_months(cur, T)
            self.assertIn("mix", str(ctx.exception.code))
            self.assertEqual(snapshot(cur), snap)

    def test_latest_month_values_identical_before_after(self):
        with rolled_back(self.conn) as cur:
            seed(cur, ["Apr-26", "Jul-26"])
            before = latest_rows(cur)
            res = m.migrate_months(cur, T)
            self.assertEqual(latest_rows(cur), before)
            self.assertEqual(res["latest_key"], "202607")
            self.assertEqual(res["latest_label"], "Jul-26")
        # where the alphabetical maximum is wrong ('Jul-26' > 'Aug-26'), the
        # check is on the label the relabelled MAX(month) will select
        with rolled_back(self.conn) as cur:
            seed(cur, ["Apr-26", "Jul-26", "Aug-26"])
            cur.execute(f"SELECT lad24cd, pip_total_claimants, "
                        f"pip_enhanced_daily_living FROM public.{T} "
                        "WHERE month = 'Aug-26' ORDER BY 1")
            aug = cur.fetchall()
            res = m.migrate_months(cur, T)
            self.assertEqual((res["latest_label"], res["latest_key"]),
                             ("Aug-26", "202608"))
            self.assertEqual(latest_rows(cur), aug)

    def test_returns_mapping_both_ways(self):
        with rolled_back(self.conn) as cur:
            seed(cur, ["Apr-26", "Jul-26"])
            res = m.migrate_months(cur, T)
            self.assertEqual(res["mapping"], {"Apr-26": "202604",
                                              "Jul-26": "202607"})
            self.assertEqual(res["reverse"], {"202604": "Apr-26",
                                              "202607": "Jul-26"})
            self.assertFalse(res["committed"])
            self.assertTrue(m.migrate_months(cur, T, commit=True)
                            ["status"] == "already migrated")

    def test_updates_only_the_month_column(self):
        with rolled_back(self.conn) as cur:
            seed(cur, ["Apr-26", "Jul-26"])
            plan = {"Apr-26": "202604", "Jul-26": "202607"}
            expect = sorted((lad, plan[mo], t, e, at)
                            for lad, mo, t, e, at in snapshot(cur))
            m.migrate_months(cur, T)
            self.assertEqual(snapshot(cur), expect)

    def test_failed_check_rolls_back_its_own_writes(self):
        with rolled_back(self.conn) as cur:
            seed(cur, ["Apr-26", "Jul-26"])
            snap = snapshot(cur)
            with mock.patch.object(m, "_compare_states",
                                   return_value=["forced difference"]), \
                    self.assertRaises(SystemExit) as ctx:
                m.migrate_months(cur, T)
            self.assertIn("forced difference", str(ctx.exception.code))
            self.assertEqual(snapshot(cur), snap)   # the caller's txn usable
            self.assertEqual(months(cur), ["Apr-26", "Jul-26"])

    def test_table_name_must_be_plain_identifier(self):
        with rolled_back(self.conn) as cur:
            with self.assertRaises(ValueError):
                m.migrate_months(cur, "zz_s19_live; DROP TABLE x")


if __name__ == "__main__":
    unittest.main()
