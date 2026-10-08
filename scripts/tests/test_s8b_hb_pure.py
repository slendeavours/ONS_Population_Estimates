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

    def test_parse_cube_nested_values_and_null(self):
        r = resp_for(["E1", "E2", "E3", "E4"], [[[5]], [7], None, [[0]]])
        self.assertEqual(m.parse_cube(r), {"E1": 5, "E2": 7, "E3": None, "E4": 0})

    def test_parse_cube_rejects_text_value(self):
        for bad in ("1,200", ".."):
            with self.assertRaises(ValueError) as ctx:
                m.parse_cube(resp_for(["E9"], [[bad]]))
            self.assertIn("E9", str(ctx.exception))

    def test_parse_cube_rejects_bool_and_float(self):
        for bad in (True, 1.5):
            with self.assertRaises(ValueError):
                m.parse_cube(resp_for(["E9"], [bad]))


class ParseCubeShape(unittest.TestCase):
    def test_parse_cube_length_mismatch(self):
        for vals in ([1], [1, 2, 3]):
            with self.assertRaises(ValueError) as ctx:
                m.parse_cube(resp_for(["E1", "E2"], vals))
            self.assertIn("2", str(ctx.exception))

    def test_parse_cube_empty_list_names_code(self):
        for vals in ([[]], [[[]]]):
            with self.assertRaises(ValueError) as ctx:
                m.parse_cube(resp_for(["E7"], vals))
            self.assertIn("E7", str(ctx.exception))


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


class Months(unittest.TestCase):
    def test_month_from_member(self):
        self.assertEqual(m.month_from_member("str:value:hb:V:DATE:C_DATE:202509"), "202509")
        self.assertIsNone(m.month_from_member("str:value:hb:V:SATA:C_SATA:99"))
        self.assertIsNone(m.month_from_member("abc"))
        self.assertIsNone(m.month_from_member("x:2025091"))

    def test_month_from_member_strict(self):
        arabic = "".join(chr(0x660 + int(d)) for d in "202509")
        for bad in ("202613", "000000", "202509\n", arabic):
            self.assertIsNone(m.month_from_member("a:" + bad), repr(bad))

    def test_available_months_sorted_valid_only(self):
        dm = [{"id": "a:202509"}, {"id": "a:202401"}, {"id": "a:99"}, {"id": "a:202412"}]
        self.assertEqual(m.available_months(dm), ["202401", "202412", "202509"])

    def test_plan_months_new_only(self):
        self.assertEqual(
            m.plan_months({"202401", "202402"}, ["202401", "202402", "202403", "202404"], recheck_n=0),
            (["202403", "202404"], []))

    def test_plan_months_recheck_window(self):
        held = {f"2025{i:02d}" for i in range(1, 8)}
        avail = sorted(held) + ["202508"]
        new, rc = m.plan_months(held, avail, recheck_n=6)
        self.assertEqual(new, ["202508"])
        self.assertEqual(rc, [f"2025{i:02d}" for i in range(2, 8)])

    def test_plan_months_recheck_all(self):
        held = {f"2025{i:02d}" for i in range(1, 8)}
        new, rc = m.plan_months(held, sorted(held), recheck_all=True)
        self.assertEqual(new, [])
        self.assertEqual(rc, sorted(held))

    def test_plan_months_nothing_new(self):
        self.assertEqual(m.plan_months({"202401"}, ["202401"], recheck_n=6), ([], ["202401"]))

    def test_plan_months_recheck_larger_than_held(self):
        self.assertEqual(m.plan_months({"202401", "202402"}, ["202401", "202402"], recheck_n=6),
                         ([], ["202401", "202402"]))

    def test_plan_months_held_month_no_longer_available_is_not_rechecked(self):
        new, rc = m.plan_months({"202401", "202402"}, ["202402", "202403"], recheck_n=6)
        self.assertEqual(new, ["202403"])
        self.assertEqual(rc, ["202402"])


class PlanRuling(unittest.TestCase):
    def test_gap_inside_held_range_is_new(self):
        new, _ = m.plan_months({"202401", "202403"}, ["202401", "202402", "202403"])
        self.assertEqual(new, ["202402"])

    def test_empty_held(self):
        self.assertEqual(m.plan_months(set(), ["202401", "202402"]), ([], []))

    def test_recheck_larger_than_held_with_new(self):
        self.assertEqual(m.plan_months({"202401"}, ["202401", "202402"], recheck_n=6),
                         (["202402"], ["202401"]))

    def test_months_before_earliest_held_never_new(self):
        new, _ = m.plan_months({"202403"}, ["202401", "202402", "202403", "202404"])
        self.assertEqual(new, ["202404"])


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
