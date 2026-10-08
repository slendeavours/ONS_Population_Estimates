"""Tests for the pure functions in s8b_hb_editions (no database, no network)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s8b_hb_editions as m  # noqa: E402


def resp_for(codes, values):
    return {
        "fields": [{"items": [{"uris": ["str:value:geo:X:" + c]} for c in codes]}],
        "cubes": {"k": {"values": values}},
    }


class ParseCube(unittest.TestCase):
    def test_accom_types(self):
        self.assertEqual(m.ACCOM_TYPES, ("SA", "TA", "OTHER", "UNKNOWN"))


class BuildRecords(unittest.TestCase):
    URIS = {"L1": ["x:A", "x:B"]}

    def total(self, raw):
        recs = m.build_records(raw, self.URIS, "202509", "SA")
        self.assertEqual(len(recs), 1)
        self.assertEqual(set(recs[0]), {"lad24cd", "month", "accom_type", "claimants"})
        self.assertEqual(recs[0]["lad24cd"], "L1")
        self.assertEqual(recs[0]["month"], "202509")
        self.assertEqual(recs[0]["accom_type"], "SA")
        return recs[0]["claimants"]

    def test_build_records_sums_parts(self):
        self.assertEqual(self.total({"A": 3, "B": 4}), 7)

    def test_build_records_null_part_gives_null_total(self):
        self.assertIsNone(self.total({"A": 3, "B": None}))

    def test_build_records_zero_part_is_zero_not_missing(self):
        self.assertEqual(self.total({"A": 0, "B": 5}), 5)
        self.assertEqual(self.total({"A": 0, "B": 0}), 0)

    def test_part_absent_from_raw_gives_null(self):
        self.assertIsNone(self.total({"A": 3}))


class BuildRecordsEdges(unittest.TestCase):
    def test_lad_absent_from_mapping_not_invented(self):
        recs = m.build_records({"A": 1, "Z": 9}, {"L1": ["x:A"]}, "202509", "SA")
        self.assertEqual([r["lad24cd"] for r in recs], ["L1"])

    def test_empty_uri_list_gives_null(self):
        recs = m.build_records({"A": 1}, {"L1": []}, "202509", "SA")
        self.assertIsNone(recs[0]["claimants"])

    def test_output_order_deterministic(self):
        uris = {"L3": ["x:C"], "L1": ["x:A"], "L2": ["x:B"]}
        recs = m.build_records({"A": 1, "B": 2, "C": 3}, uris, "202509", "SA")
        self.assertEqual([r["lad24cd"] for r in recs], ["L1", "L2", "L3"])


class Hash(unittest.TestCase):
    def rec(self, lad, t, c):
        return {"lad24cd": lad, "month": "202509", "accom_type": t, "claimants": c}

    def test_content_sha256_order_independent_and_null_distinct_from_zero(self):
        a = [self.rec("L1", "SA", 1), self.rec("L2", "TA", None)]
        self.assertEqual(m.content_sha256(a), m.content_sha256(list(reversed(a))))
        b = [self.rec("L1", "SA", 1), self.rec("L2", "TA", 0)]
        self.assertNotEqual(m.content_sha256(a), m.content_sha256(b))
        self.assertEqual(len(m.content_sha256(a)), 64)

    def test_content_sha256_rejects_float_and_bool(self):
        for bad in (1.5, True):
            with self.assertRaises(ValueError):
                m.content_sha256([self.rec("L1", "SA", bad)])


if __name__ == "__main__":
    unittest.main()
