"""Tests that the real-table gates of s19_pip_editions_verify fail on a short,
empty or unmigrated state. The gate bodies (real_*) take a spec, so they are
called here against the throwaway zz_s19_live / zz_s19_editions tables
(created inside a transaction that is always rolled back); no real table is
touched."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import editions_core as core  # noqa: E402
import s19_pip_editions as m  # noqa: E402
import s19_pip_editions_verify as v  # noqa: E402
from _db import get_conn  # noqa: E402

ZZ = v.ZZ
GATES = (v.real_edition1_present, v.real_latest_equals_live, v.real_coverage,
         v.real_null_vs_zero, v.real_zero_listing, v.real_period_keys)


class VerifyGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def setUp(self):
        self.cur = self.conn.cursor()
        v.setup_throwaway(self.cur)

    def tearDown(self):
        self.cur.close()
        self.conn.rollback()

    def _insert(self, records, month, supersedes, sha):
        return core.insert_edition(
            self.cur, ZZ, records, month, release_label="t",
            published_date=date(2026, 10, 8), source_file="t",
            source_sha256=sha, supersedes=supersedes)

    def test_empty_state_fails_gates_2_3_4_6_7_17(self):
        for gate in GATES:
            ok, detail = gate(self.cur, ZZ)
            self.assertFalse(ok, f"{gate.__name__}: {detail}")
            self.assertIn("no ", detail)

    def test_complete_state_passes(self):
        full = v.recs("202601")
        m.apply_month(self.cur, ZZ, "202601", full, fetched_on=v.FETCHED)
        for gate in GATES + (v.real_no_negatives,):
            ok, detail = gate(self.cur, ZZ)
            self.assertTrue(ok, f"{gate.__name__}: {detail}")

    def test_short_edition_1_fails_even_when_edition_2_is_complete(self):
        full = v.recs("202601")
        short = v.recs("202601", n=m.EXPECTED_AREAS - 1)
        v.seed_live(self.cur, full)
        self._insert(short, "202601", None, "s1")
        self._insert(full, "202601", 1, "s2")
        ok, detail = v.real_coverage(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("ed1", detail)
        self.assertNotIn("ed2", detail)

    def test_short_middle_edition_is_caught(self):
        full = v.recs("202601")
        v.seed_live(self.cur, full)
        self._insert(full, "202601", None, "s1")
        self._insert(v.recs("202601", n=m.EXPECTED_AREAS - 1), "202601", 1, "s2")
        self._insert(full, "202601", 2, "s3")
        ok, detail = v.real_coverage(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("ed2", detail)

    def test_unmigrated_labels_fail_gate_17(self):
        v.seed_live(self.cur, v.recs("Sep-25") + v.recs("Apr-26"))
        ok, detail = v.real_period_keys(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertIn("not yyyymm keys", detail)
        # the alphabetical MAX picks the older month: the defect
        self.assertIn("MAX(month) selects 'Sep-25'", detail)
        self.assertFalse(m.months_migrated(self.cur, ZZ))

    def test_migrated_months_pass_gate_17(self):
        v.seed_live(self.cur, v.recs("Sep-25") + v.recs("Apr-26"))
        m.migrate_months(self.cur, ZZ.live_table)
        self.cur.execute(f"SELECT DISTINCT month FROM public.{ZZ.live_table} "
                         "ORDER BY 1")
        self.assertEqual([r[0] for r in self.cur.fetchall()],
                         ["202509", "202604"])
        for kind in ("202509", "202604"):
            self._insert(v.recs(kind), kind, None, "s" + kind)
        ok, detail = v.real_period_keys(self.cur, ZZ)
        self.assertTrue(ok, detail)

    def test_mixed_live_months_fail_gate_17_not_pending(self):
        v.seed_live(self.cur, v.recs("202601") + v.recs("Apr-26"))
        ok, detail, pending = v.period_key_status(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertFalse(pending, detail)
        self.assertIn("Apr-26", detail)

    def test_malformed_month_fails_gate_17_not_pending(self):
        v.seed_live(self.cur, v.recs("2026-04"))
        ok, detail, pending = v.period_key_status(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertFalse(pending, detail)

    def test_all_labels_is_pending_for_gate_17(self):
        v.seed_live(self.cur, v.recs("Sep-25") + v.recs("Apr-26"))
        ok, detail, pending = v.period_key_status(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertTrue(pending, detail)

    def test_empty_existing_state_is_a_plain_fail_for_gate_17(self):
        ok, detail, pending = v.period_key_status(self.cur, ZZ)
        self.assertFalse(ok)
        self.assertFalse(pending)

    def test_enhanced_measure_alone_is_a_revision(self):
        e = v.seeded_enhanced_only(self.cur)
        self.assertEqual(e["kind"], "revised")
        self.assertTrue(v.enhanced_only_ok(e), e)

    def test_enhanced_null_zero_variants_are_revisions(self):
        s = v.seeded_null_zero(self.cur)
        for label, col, _a, want in v.VARIANTS:
            kind, flagged, stored, cell = s[label]
            self.assertEqual((kind, len(stored), cell), ("revised", 2, (want,)),
                             label)
            self.assertEqual(len(flagged), 1, label)
        self.assertEqual(len(v.VARIANTS), 4)

    def test_guard_halt_must_carry_the_guard_message(self):
        def other():
            raise SystemExit("some unrelated halt")

        def guard():
            raise SystemExit("schema discovery chose database 'x'")
        self.assertFalse(v._guard_halts(other))
        self.assertTrue(v._guard_halts(guard))
        self.assertFalse(v._guard_halts(lambda: None))

    def test_period_key_mapping_round_trips(self):
        ok, detail = v.period_key_mapping()
        self.assertTrue(ok, detail)
        self.assertIn("Sep-30", detail)  # max over labels: the documented defect


if __name__ == "__main__":
    unittest.main()
