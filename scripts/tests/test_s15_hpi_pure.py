"""Tests for the pure functions in s15_hpi_editions (no database, no network,
no key). Fixtures are tiny literals written inline."""
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s15_hpi_editions as m  # noqa: E402

VALID = {"E06000001", "E08000016", "E08000019", "E09000001", "E07000008"}
LOOKUP = {"E07000999": "E07000008"}


def avg(code, d, price="100000", sa="101000", chg="1.5"):
    return {"Area_Code": code, "Date": d, "Average_Price": price,
            "Average_Price_SA": sa, "Annual_Change": chg}


def pt(code, d, det="200000", semi="150000", ter="120000", flat="90000"):
    return {"Area_Code": code, "Date": d, "Detached_Average_Price": det,
            "Semi_Detached_Average_Price": semi,
            "Terraced_Average_Price": ter, "Flat_Average_Price": flat}


def build(avg_rows, pt_rows, **kw):
    return m.build_records(avg_rows, pt_rows, valid_lads=VALID,
                           code_lookup=LOOKUP, **kw)


class Names(unittest.TestCase):
    def test_edition_from_filename_both_files_and_rejects(self):
        self.assertEqual(m.edition_from_filename("Average-prices-2026-07.csv"), "2026-07")
        self.assertEqual(
            m.edition_from_filename("Average-prices-Property-Type-2026-07.csv"), "2026-07")
        for bad in ("Average-prices-2026-7.csv", "Other-2026-07.csv", "",
                    "Average-prices-2026-07.xlsx"):
            with self.assertRaises(ValueError) as cm:
                m.edition_from_filename(bad)
            self.assertIn(repr(bad), str(cm.exception))


class Numbers(unittest.TestCase):
    def test_safe_numeric_blank_na_zero_nan(self):
        for v in (None, "", "NA", "nan", "inf", "-inf", "abc", "N/A"):
            self.assertIsNone(m.safe_numeric(v), v)
        self.assertEqual(m.safe_numeric("0"), 0)
        self.assertIsNotNone(m.safe_numeric("0"))
        self.assertEqual(m.safe_numeric("12.5"), 12.5)
        self.assertEqual(m.safe_numeric(0), 0)
        self.assertIsNotNone(m.safe_numeric(0))


class Reconcile(unittest.TestCase):
    def test_reconcile_order_hard_recode_valid_lookup_unresolved(self):
        kw = dict(valid_lads=VALID, code_lookup=LOOKUP, hard_recodes=m.HARD_RECODES)
        self.assertEqual(m.reconcile("E08000038", **kw), "E08000016")
        self.assertEqual(m.reconcile("E08000039", **kw), "E08000019")
        self.assertEqual(m.reconcile("E06000001", **kw), "E06000001")
        self.assertEqual(m.reconcile("E07000999", **kw), "E07000008")
        self.assertIsNone(m.reconcile("E06999999", **kw))
        # hard recode wins even when the code is itself valid
        kw2 = dict(valid_lads=VALID | {"E08000038"}, code_lookup=LOOKUP,
                   hard_recodes=m.HARD_RECODES)
        self.assertEqual(m.reconcile("E08000038", **kw2), "E08000016")
        # valid wins over lookup
        kw3 = dict(valid_lads=VALID | {"E07000999"}, code_lookup=LOOKUP,
                   hard_recodes={})
        self.assertEqual(m.reconcile("E07000999", **kw3), "E07000999")


class Build(unittest.TestCase):
    def test_build_records_filters_english_and_window_and_sorts(self):
        a = [avg("E09000001", "2022-02-01"), avg("E06000001", "2022-02-01"),
             avg("W06000001", "2022-02-01"), avg("S12000033", "2022-02-01"),
             avg("E06000001", "2021-12-01"), avg("E06000001", "2022-01-01"),
             avg("E08000038", "2022-02-01")]
        p = [pt(x["Area_Code"], x["Date"]) for x in a]
        recs, unres, nopt = build(a, p)
        self.assertEqual(sorted(recs), ["2022-01-01", "2022-02-01"])
        self.assertEqual([r["lad24cd"] for r in recs["2022-02-01"]],
                         ["E06000001", "E08000016", "E09000001"])
        self.assertEqual(len(recs["2022-01-01"]), 1)
        self.assertEqual(unres, {})
        self.assertEqual(nopt, 0)
        r = recs["2022-02-01"][0]
        self.assertEqual(set(r), {"lad24cd", "period", "avg_price_all",
                                  "avg_price_all_sa", "annual_change_pct",
                                  "avg_price_detached", "avg_price_semi",
                                  "avg_price_terraced", "avg_price_flat"})
        self.assertEqual(r["period"], "2022-02-01")

    def test_build_records_min_period_parameter(self):
        recs, _, _ = build([avg("E06000001", "2022-01-01")], [],
                           min_period=date(2022, 2, 1))
        self.assertEqual(recs, {})

    def test_build_records_suppressed_property_prices_are_null_not_zero(self):
        a = [avg("E06000001", "2022-02-01")]
        p = [pt("E06000001", "2022-02-01", det="", semi="NA", ter="0", flat="nan")]
        recs, _, nopt = build(a, p)
        r = recs["2022-02-01"][0]
        self.assertIsNone(r["avg_price_detached"])
        self.assertIsNone(r["avg_price_semi"])
        self.assertEqual(r["avg_price_terraced"], Decimal("0.00"))
        self.assertIsNotNone(r["avg_price_terraced"])
        self.assertIsNone(r["avg_price_flat"])
        self.assertEqual(nopt, 0)
        # zero change stays zero, blank all-price is None
        recs, _, _ = build([avg("E06000001", "2022-02-01", price="", chg="0")], [])
        r = recs["2022-02-01"][0]
        self.assertIsNone(r["avg_price_all"])
        self.assertEqual(r["annual_change_pct"], Decimal("0.00"))

    def test_build_records_property_type_missing_gives_none_and_is_counted(self):
        a = [avg("E06000001", "2022-02-01"), avg("E09000001", "2022-02-01")]
        p = [pt("E06000001", "2022-02-01")]
        recs, unres, nopt = build(a, p)
        self.assertEqual(nopt, 1)
        r = {x["lad24cd"]: x for x in recs["2022-02-01"]}["E09000001"]
        for c in ("avg_price_detached", "avg_price_semi",
                  "avg_price_terraced", "avg_price_flat"):
            self.assertIsNone(r[c])
        self.assertEqual(r["avg_price_all"], Decimal("100000.00"))

    def test_property_type_joined_on_original_code_not_recoded(self):
        a = [avg("E08000038", "2022-02-01")]
        p = [pt("E08000038", "2022-02-01", det="321")]
        recs, _, nopt = build(a, p)
        self.assertEqual(nopt, 0)
        self.assertEqual(recs["2022-02-01"][0]["avg_price_detached"], Decimal("321.00"))

    def test_unresolved_codes_are_returned_not_dropped_silently(self):
        a = [avg("E06999999", "2022-02-01"), avg("E06999999", "2022-03-01"),
             avg("E07999999", "2022-02-01"), avg("E06000001", "2022-02-01")]
        recs, unres, _ = build(a, [])
        self.assertEqual(unres, {"E06999999": 2, "E07999999": 1})
        self.assertEqual(len(recs["2022-02-01"]), 1)
        self.assertNotIn("2022-03-01", recs)

    def test_decimals_rounded_to_column_scale(self):
        a = [avg("E06000001", "2022-02-01", price="123456.789", sa="99.995",
                 chg="-3.456")]
        p = [pt("E06000001", "2022-02-01", det="1.004")]
        r = build(a, p)[0]["2022-02-01"][0]
        self.assertEqual(r["avg_price_all"], Decimal("123456.79"))
        self.assertEqual(r["avg_price_all_sa"], Decimal("100.00"))
        self.assertEqual(r["annual_change_pct"], Decimal("-3.46"))
        self.assertEqual(r["avg_price_detached"], Decimal("1.00"))
        for c in ("avg_price_all", "annual_change_pct"):
            self.assertIsInstance(r[c], Decimal)
            self.assertEqual(r[c].as_tuple().exponent, -2)

    def test_recoded_duplicates_in_same_period_raise(self):
        a = [avg("E08000016", "2022-02-01"), avg("E08000038", "2022-02-01")]
        with self.assertRaises(ValueError) as cm:
            build(a, [])
        msg = str(cm.exception)
        for part in ("E08000016", "E08000038", "2022-02-01"):
            self.assertIn(part, msg)
        a = [avg("E08000019", "2022-02-01"), avg("E08000039", "2022-02-01")]
        with self.assertRaises(ValueError):
            build(a, [])

    def test_same_codes_in_different_months_are_fine(self):
        a = [avg("E08000016", "2022-02-01"), avg("E08000038", "2022-03-01")]
        recs, _, _ = build(a, [])
        self.assertEqual(len(recs["2022-02-01"]), 1)
        self.assertEqual(len(recs["2022-03-01"]), 1)

    def test_repeated_average_price_row_raises(self):
        a = [avg("E06000001", "2022-02-01"), avg("E06000001", "2022-02-01")]
        with self.assertRaises(ValueError) as cm:
            build(a, [])
        self.assertIn("E06000001", str(cm.exception))

    def test_repeated_property_type_row_raises(self):
        a = [avg("E06000001", "2022-02-01")]
        p = [pt("E06000001", "2022-02-01"), pt("E06000001", "2022-02-01", det="1")]
        with self.assertRaises(ValueError) as cm:
            build(a, p)
        self.assertIn("E06000001", str(cm.exception))
        self.assertIn("2022-02-01", str(cm.exception))

    def test_rounding_half_up_on_float_repr(self):
        a = [avg("E06000001", "2022-02-01", price="0.285", sa="1234.565",
                 chg="-0.285")]
        r = build(a, [])[0]["2022-02-01"][0]
        self.assertEqual(r["avg_price_all"], Decimal("0.29"))
        self.assertEqual(r["avg_price_all_sa"], Decimal("1234.57"))
        self.assertEqual(r["annual_change_pct"], Decimal("-0.29"))

    def test_column_quanta_cover_all_value_columns(self):
        self.assertEqual(set(m.COLUMN_QUANTA), set(m.VALUE_COLUMNS))
        self.assertEqual(len(m.VALUE_COLUMNS), 7)

    def test_file_period_range(self):
        recs, _, _ = build([avg("E06000001", "2022-03-01"),
                            avg("E06000001", "2022-01-01")], [])
        self.assertEqual(m.file_period_range(recs), ("2022-01-01", "2022-03-01"))
        with self.assertRaises(ValueError):
            m.file_period_range({})


class Identity(unittest.TestCase):
    RECS = {"2026-05-01": [], "2026-06-01": []}

    def test_check_identity_mismatched_names_and_stale_latest_period(self):
        self.assertEqual(m.check_identity("Average-prices-2026-06.csv",
                                          "Average-prices-Property-Type-2026-06.csv",
                                          self.RECS), [])
        p = m.check_identity("Average-prices-2026-06.csv",
                             "Average-prices-Property-Type-2026-05.csv", self.RECS)
        self.assertTrue(any("differ" in x for x in p))
        p = m.check_identity("Average-prices-2026-07.csv",
                             "Average-prices-Property-Type-2026-07.csv", self.RECS)
        self.assertEqual(len(p), 1)
        self.assertIn("2026-06", p[0])
        self.assertIn("2026-07", p[0])
        with self.assertRaises(ValueError):
            m.check_identity("bad.csv", "Average-prices-Property-Type-2026-07.csv",
                             self.RECS)


def hrec(lad, **kw):
    r = {"lad24cd": lad, "period": "2026-06-01",
         "avg_price_all": Decimal("1.00"), "avg_price_all_sa": Decimal("2.00"),
         "annual_change_pct": Decimal("0.50"), "avg_price_detached": None,
         "avg_price_semi": None, "avg_price_terraced": None,
         "avg_price_flat": None}
    r.update(kw)
    return r


class Hash(unittest.TestCase):
    def test_content_sha256_order_independent_null_vs_zero(self):
        a, b = hrec("E06000001"), hrec("E09000001")
        self.assertEqual(m.content_sha256([a, b]), m.content_sha256([b, a]))
        z = hrec("E06000001", avg_price_flat=Decimal("0.00"))
        self.assertNotEqual(m.content_sha256([a, b]), m.content_sha256([z, b]))
        c = hrec("E06000001", avg_price_all=Decimal("1.01"))
        self.assertNotEqual(m.content_sha256([a]), m.content_sha256([c]))
        # same value, different trailing-zero form hashes the same
        d = hrec("E06000001", avg_price_all=Decimal("1.0"))
        self.assertEqual(m.content_sha256([a]), m.content_sha256([d]))
        for bad in (1.0, 5, "1.00", True):
            with self.assertRaises(ValueError):
                m.content_sha256([hrec("E06000001", avg_price_all=bad)])


LANDING = """<html><body>
<a href="/government/collections/other">x</a>
<a href="/government/statistical-data-sets/uk-house-price-index-data-downloads-june-2026">June 2026</a>
<a href="https://www.gov.uk/government/statistical-data-sets/uk-house-price-index-data-downloads-may-2026">May</a>
</body></html>"""

DATA_PAGE = """<html><body>
<a href="https://publicdata.landregistry.gov.uk/market-trend-data/house-price-index-data/Average-prices-Property-Type-2026-06.csv?x=1">pt</a>
<a href="https://publicdata.landregistry.gov.uk/market-trend-data/house-price-index-data/Average-prices-2026-06.csv?x=1">avg</a>
<a href="https://publicdata.landregistry.gov.uk/market-trend-data/house-price-index-data/Average-prices-2026-06.csv?x=2">dup</a>
</body></html>"""


class Pages(unittest.TestCase):
    def test_find_download_page_and_csv_links_on_sample_html(self):
        self.assertEqual(
            m.find_download_page(LANDING),
            "https://www.gov.uk/government/statistical-data-sets/uk-house-price-index-data-downloads-june-2026")
        absolute = LANDING.replace(
            'href="/government/statistical-data-sets/uk-house-price-index-data-downloads-june-2026"',
            'href="https://www.gov.uk/government/statistical-data-sets/uk-house-price-index-data-downloads-june-2026"')
        self.assertTrue(m.find_download_page(absolute).startswith(
            "https://www.gov.uk/government/statistical"))
        with self.assertRaises(ValueError) as cm:
            m.find_download_page("<html>nothing to see</html>")
        self.assertIn("nothing to see", str(cm.exception))
        a, p, ed = m.find_csv_links(DATA_PAGE)
        self.assertTrue(a.endswith("Average-prices-2026-06.csv?x=1"))
        self.assertIn("Average-prices-Property-Type-2026-06.csv", p)
        self.assertNotIn("Property-Type", a)
        self.assertEqual(ed, "2026-06")

    def test_find_csv_links_rejects_mismatched_editions(self):
        page = DATA_PAGE.replace("Property-Type-2026-06", "Property-Type-2026-05")
        with self.assertRaises(ValueError):
            m.find_csv_links(page)

    def test_find_csv_links_rejects_missing_file(self):
        only_pt = '<a href="https://h/Average-prices-Property-Type-2026-06.csv">x</a>'
        only_avg = '<a href="https://h/Average-prices-2026-06.csv">x</a>'
        for page in (only_pt, only_avg, "<html></html>"):
            with self.assertRaises(ValueError):
                m.find_csv_links(page)


if __name__ == "__main__":
    unittest.main()
