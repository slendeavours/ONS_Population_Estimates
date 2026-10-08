"""Tests for the pure functions in s19_pip_editions (no database, no network,
no Stat-Xplore key)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s19_pip_editions as m  # noqa: E402

URIS = {"L1": ["x:A", "x:B"], "L2": ["x:C"]}


def rec(lad, month, total, enh):
    return {"lad24cd": lad, "month": month, "pip_total_claimants": total,
            "pip_enhanced_daily_living": enh}


class Labels(unittest.TestCase):
    def test_label_to_key_valid(self):
        self.assertEqual(m.label_to_key("Jan-26"), "202601")
        self.assertEqual(m.label_to_key("Dec-25"), "202512")
        self.assertEqual(m.label_to_key("Apr-26"), "202604")

    def test_label_to_key_rejects(self):
        for bad in ("Sept-26", "Foo-26", "Apr-2026", "202604", "", "apr-26"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as cm:
                    m.label_to_key(bad)
                self.assertIn(repr(bad), str(cm.exception))

    def test_key_to_label_roundtrip(self):
        for mo in range(1, 13):
            key = f"2026{mo:02d}"
            self.assertEqual(m.label_to_key(m.key_to_label(key)), key)
        self.assertEqual(m.key_to_label("202604"), "Apr-26")

    def test_relabel_plan_ok_and_rejects_invalid_and_duplicates(self):
        self.assertEqual(m.relabel_plan(["Apr-26", "Jul-26", "Apr-26"]),
                         {"Apr-26": "202604", "Jul-26": "202607"})
        with self.assertRaises(ValueError):
            m.relabel_plan(["Apr-26", "Sept-26"])
        with self.assertRaises(ValueError):
            m.relabel_plan(["Apr-26", "202604"])

    def test_sorting_property(self):
        self.assertEqual(max(["Jul-26", "Aug-26"]), "Jul-26")
        self.assertEqual(max(["202607", "202608", "202610"]), "202610")


class BuildRecords(unittest.TestCase):
    def test_build_records_sums_parts(self):
        recs = m.build_records({"A": 1, "B": 2, "C": 5}, {"A": 10, "B": 20, "C": 50},
                               URIS, "202604")
        self.assertEqual(recs, [rec("L1", "202604", 3, 30),
                                rec("L2", "202604", 5, 50)])

    def test_build_records_null_part_gives_null_each_measure_independently(self):
        recs = m.build_records({"A": 1, "B": 2, "C": 5},
                               {"A": 10, "B": None, "C": 50}, URIS, "202604")
        self.assertEqual(recs[0]["pip_total_claimants"], 3)
        self.assertIsNone(recs[0]["pip_enhanced_daily_living"])
        self.assertEqual(recs[1]["pip_enhanced_daily_living"], 50)

    def test_build_records_zero_part_is_zero_not_missing(self):
        recs = m.build_records({"A": 0, "B": 0, "C": 0}, {"A": 0, "B": 4, "C": 0},
                               URIS, "202604")
        self.assertEqual(recs[0]["pip_total_claimants"], 0)
        self.assertEqual(recs[0]["pip_enhanced_daily_living"], 4)
        self.assertEqual(recs[1]["pip_total_claimants"], 0)

    def test_part_absent_gives_null(self):
        recs = m.build_records({"A": 1, "C": 5}, {"A": 1, "B": 2}, URIS, "202604")
        self.assertIsNone(recs[0]["pip_total_claimants"])
        self.assertEqual(recs[0]["pip_enhanced_daily_living"], 3)
        self.assertEqual(recs[1]["pip_total_claimants"], 5)
        self.assertIsNone(recs[1]["pip_enhanced_daily_living"])

    def test_build_records_sorted_and_no_invented_area(self):
        uris = {"L2": ["x:C"], "L1": ["x:A"]}
        recs = m.build_records({"A": 1, "C": 2, "Z": 9}, {"A": 1, "C": 2, "Z": 9},
                               uris, "202604")
        self.assertEqual([r["lad24cd"] for r in recs], ["L1", "L2"])
        for r in recs:
            self.assertEqual(set(r), {"lad24cd", "month", "pip_total_claimants",
                                      "pip_enhanced_daily_living"})
            self.assertEqual(r["month"], "202604")


class Hash(unittest.TestCase):
    def test_content_sha256_order_independent_null_distinct_from_zero_rejects_float(self):
        a = [rec("L1", "202604", 3, 30), rec("L2", "202604", 5, None)]
        self.assertEqual(m.content_sha256(a), m.content_sha256(list(reversed(a))))
        self.assertEqual(len(m.content_sha256(a)), 64)
        zero = [rec("L1", "202604", 3, 30), rec("L2", "202604", 5, 0)]
        self.assertNotEqual(m.content_sha256(a), m.content_sha256(zero))
        for bad in (1.5, "3", True):
            with self.assertRaises(ValueError):
                m.content_sha256([rec("L1", "202604", bad, 1)])
            with self.assertRaises(ValueError):
                m.content_sha256([rec("L1", "202604", 1, bad)])


if __name__ == "__main__":
    unittest.main()
