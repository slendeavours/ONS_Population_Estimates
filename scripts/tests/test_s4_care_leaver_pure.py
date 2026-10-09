"""Pure tests for s4_care_leaver_editions: discovery (content API reply, the
data guidance page's __NEXT_DATA__), identity, reading the three header
schemas, the rule-1 records, release rank, planning and the stop
conditions. No database, no network; every CSV is written by the test."""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geography  # noqa: E402
import s4_care_leaver_editions as m  # noqa: E402

PUB = m.PUBLICATION


# ---------------------------------------------------------------------------
# Fixtures: files in the three schemas, the API reply and the page
# ---------------------------------------------------------------------------

def labels(schema, cohort):
    """(age labels, category labels) of a schema's cohort, in file order."""
    c = m.SCHEMAS[schema]["cohorts"][cohort]
    return list(c["ages"]), list(c["categories"])


def _default(cohort, cat_label, canon, ai):
    if cohort == "17-21":
        if canon == "Total":
            return str(17 * (2 - ai))   # 17 categories of 2 (or 1)
        return str(2 - ai)
    return {"suitable": "5", "unsuitable": "1", "no_information": "2",
            "total": "8"}[canon]


def file_rows(schema, cohort, codes, years, values=None, allz=(),
              national=True):
    """Rows of a DfE care leaver CSV (header first). values: {(code, year,
    age label, category label): text} overrides; allz: (code, year) pairs
    whose every cell is z."""
    sc = m.SCHEMAS[schema]
    cs = sc["cohorts"][cohort]
    ages, cats = labels(schema, cohort)
    out = [list(sc["columns"])]
    values = values or {}
    for y in years:
        if national:
            for a in ages:
                for c in cats:
                    out.append([str(y), "Reporting year", "National",
                                "E92000001", "England", "", "", "", "", "",
                                a, c, "9", "100"])
        for code in codes:
            for ai, a in enumerate(ages):
                for c in cats:
                    v = values.get((code, y, a, c))
                    if v is None:
                        v = ("z" if (code, y) in set(allz) else
                             _default(cohort, c, cs["categories"][c], ai))
                    out.append([str(y), "Reporting year", "Local authority",
                                "E92000001", "England", "E12000001",
                                "North East", "800", f"Area {code}", code,
                                a, c, v, "k"])
    return out


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    return path


def dataset(title, ds_id, start, end, rows, levels=("Local authority",
                                                     "National",
                                                     "Regional")):
    return {"dataSetFileId": ds_id, "fileId": "f", "subjectId": "s",
            "title": title, "summary": "",
            "meta": {"timePeriodRange": {"start": str(start), "end": str(end)},
                     "numDataFileRows": rows,
                     "geographicLevels": list(levels), "filters": [],
                     "indicators": []}}


def page_json(slug, datasets, *, published="2025-11-26T08:32:42+00:00",
              update_count=2, rv_id="rv-" + "1" * 8):
    return {"props": {"pageProps": {
        "releaseVersionSummary": {"id": rv_id, "slug": slug,
                                  "published": published,
                                  "updateCount": update_count,
                                  "isLatestRelease": True},
        "dataContent": {"releaseVersionId": rv_id, "dataSets": datasets}}}}


def html_of(page):
    return ("<html><head></head><body><div id='x'></div>"
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(page) + "</script></body></html>")


T17_NEW = "Care leavers (now 17-21 years) - accommodation - by local authority"
T22_NEW = ("Care leavers (now 22-25 years) - whether their accommodation is "
           "suitable - by local authority")
T17_OLD = "17-21 year old care leavers accommodation - LA"
T22_OLD = "22-25 year old care leavers by whether their accommodation is suitable - LA"


def three(path_dir, name, schema, cohort, codes, years, **kw):
    return write_csv(Path(path_dir) / name,
                     file_rows(schema, cohort, codes, years, **kw))


def identity(code):
    return code


# ---------------------------------------------------------------------------

class Discovery(unittest.TestCase):
    def test_latest_release(self):
        api = {"latestRelease": {"slug": "2025", "title": "Reporting year "
                                 "2025"},
               "nextReleaseDate": {"year": 2026, "month": 11}}
        self.assertEqual(m.latest_release(api), ("2025", "2026-11"))
        self.assertEqual(m.latest_release(json.dumps(api)), ("2025",
                                                             "2026-11"))
        with self.assertRaises(ValueError):
            m.latest_release({"title": "x"})

    def test_page_data_reads_the_next_data_block(self):
        page = page_json("2025", [])
        self.assertEqual(m.page_data(html_of(page)), page)

    def test_page_without_json_raises_quoting_200_characters(self):
        html = "<html>" + "x" * 500 + "</html>"
        with self.assertRaises(ValueError) as e:
            m.page_data(html)
        self.assertIn("no __NEXT_DATA__", str(e.exception))
        self.assertIn(html[:200], str(e.exception))
        self.assertNotIn(html[:300], str(e.exception))
        bad = ('<script id="__NEXT_DATA__" type="application/json">{not json'
               "</script>")
        with self.assertRaises(ValueError) as e:
            m.page_data(bad)
        self.assertIn("not JSON", str(e.exception))

    def test_choose_datasets_new_title_style(self):
        page = page_json("2025", [
            dataset(T17_NEW, "a504e4b8-x", 2021, 2025, 30060),
            dataset("Care leavers (now 17-21 years) - whether their "
                    "accommodation is suitable - by local authority",
                    "0048e808-x", 2021, 2025, 6680),
            dataset(T22_NEW, "bd5240e0-x", 2023, 2025, 7968)])
        ds = m.choose_datasets(page)
        self.assertEqual(ds["17-21"]["dataSetFileId"], "a504e4b8-x")
        self.assertEqual(ds["22-25"]["dataSetFileId"], "bd5240e0-x")
        self.assertEqual(ds["22-25"]["numDataFileRows"], 7968)
        self.assertEqual(ds["17-21"]["timePeriodRange"],
                         {"start": "2021", "end": "2025"})
        self.assertEqual(ds["17-21"]["published"], "2025-11-26")
        self.assertEqual(ds["17-21"]["updateCount"], 2)
        self.assertEqual(ds["17-21"]["slug"], "2025")
        self.assertEqual(ds["17-21"]["release_version_id"], "rv-" + "1" * 8)

    def test_choose_datasets_old_title_style(self):
        page = page_json("2024", [
            dataset("17-21 year old care leaver accommodation - NATIONAL",
                    "n1", 2020, 2024, 630, levels=("National",)),
            dataset(T17_OLD, "a8264b74-x", 2020, 2024, 30060),
            dataset("17-21 year old care leavers by whether their "
                    "accommodation is suitable - LA", "e5b3", 2020, 2024,
                    6680),
            dataset("22-25 year old care leavers by whether their "
                    "accommodation is suitable - NATIONAL", "n2", 2023, 2024,
                    32, levels=("National",)),
            dataset(T22_OLD, "a96ea38e-x", 2023, 2024, 5312)])
        ds = m.choose_datasets(page)
        self.assertEqual(ds["17-21"]["dataSetFileId"], "a8264b74-x")
        self.assertEqual(ds["22-25"]["dataSetFileId"], "a96ea38e-x")

    def test_two_candidates_or_none_raise_naming_them(self):
        page = page_json("2025", [
            dataset(T17_NEW, "one", 2021, 2025, 1),
            dataset(T17_OLD, "two", 2021, 2025, 1),
            dataset(T22_NEW, "three", 2023, 2025, 1)])
        with self.assertRaises(ValueError) as e:
            m.choose_datasets(page)
        self.assertIn("17-21: 2 datasets", str(e.exception))
        self.assertIn(T17_OLD, str(e.exception))
        page = page_json("2023", [dataset(T17_OLD, "one", 2019, 2023, 1)])
        with self.assertRaises(ValueError) as e:
            m.choose_datasets(page)
        self.assertIn("22-25: 0 datasets", str(e.exception))

    def test_changed_page_shape_raises(self):
        with self.assertRaises(ValueError):
            m.choose_datasets({"props": {"pageProps": {}}})


class Reading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def test_three_schemas_and_the_2025_pair(self):
        self.assertEqual(set(m.SCHEMAS), {"accommodation_type",
                                          "suitability_2024",
                                          "care_leaver_2025"})
        for schema, cohort in (("accommodation_type", "17-21"),
                               ("suitability_2024", "22-25"),
                               ("care_leaver_2025", "17-21"),
                               ("care_leaver_2025", "22-25")):
            p = three(self.d, f"{schema}{cohort}.csv", schema, cohort,
                      ["E06000001", "E06000002"], [2023, 2024])
            f = m.read_file(p)
            self.assertEqual((f["schema"], f["cohort"]), (schema, cohort))
            self.assertEqual(f["years"], [2023, 2024])
            self.assertEqual(f["max_year"], 2024)
            ages, cats = labels(schema, cohort)
            self.assertEqual(f["row_count"], 2 * len(ages) * len(cats) * 3)
            self.assertEqual(len(f["rows"]), 4)

    def test_the_2024_22_25_wording_maps(self):
        p = three(self.d, "s.csv", "suitability_2024", "22-25", ["E06000001"],
                  [2024], values={
                      ("E06000001", 2024, "Aged 22",
                       "Accommodation considered not suitable"): "3",
                      ("E06000001", 2024, "Aged 22", "Suitability total"):
                          "10"})
        f = m.read_file(p)
        cells = f["rows"][("E06000001", 2024)]
        self.assertEqual(cells[("22", "unsuitable")], (3, None))
        self.assertEqual(cells[("22", "total")], (10, None))
        self.assertEqual(cells[("23", "no_information")], (2, None))

    def test_unknown_header_raises_listing_columns(self):
        rows = file_rows("care_leaver_2025", "17-21", ["E06000001"], [2025])
        rows[0][12] = "count_of_leavers"
        p = write_csv(self.d / "h.csv", rows)
        with self.assertRaises(ValueError) as e:
            m.read_file(p)
        self.assertIn("unknown header", str(e.exception))
        self.assertIn("count_of_leavers", str(e.exception))
        self.assertIn("care_leaver_age", str(e.exception))

    def test_unknown_category_or_age_raises(self):
        rows = file_rows("accommodation_type", "17-21", ["E06000001"], [2023])
        rows[5][11] = "Caravan"
        with self.assertRaises(ValueError) as e:
            m.read_file(write_csv(self.d / "c.csv", rows))
        self.assertIn("Caravan", str(e.exception))
        rows = file_rows("accommodation_type", "17-21", ["E06000001"], [2023])
        for r in rows[1:]:
            if r[10] == "19 to 21 years":
                r[10] = "19 to 20 years"
        with self.assertRaises(ValueError) as e:
            m.read_file(write_csv(self.d / "a.csv", rows))
        self.assertIn("age set", str(e.exception))

    def test_time_identifier_level_grid_and_duplicates_raise(self):
        base = file_rows("care_leaver_2025", "22-25",
                         ["E06000001", "E06000002"], [2024, 2025])
        rows = [list(r) for r in base]
        rows[3][1] = "Financial year"
        with self.assertRaises(ValueError) as e:
            m.read_file(write_csv(self.d / "t.csv", rows))
        self.assertIn("time_identifier", str(e.exception))
        rows = [list(r) for r in base]
        rows[3][2] = "Ward"
        with self.assertRaises(ValueError) as e:
            m.read_file(write_csv(self.d / "l.csv", rows))
        self.assertIn("geographic_level", str(e.exception))
        rows = [r for r in base if not (r[9] == "E06000002" and r[0] == "2025"
                                        and r[10] == "24 years")]
        with self.assertRaises(ValueError) as e:
            m.read_file(write_csv(self.d / "g.csv", rows))
        self.assertIn("incomplete grid", str(e.exception))
        rows = [r for r in base if not (r[9] == "E06000002"
                                        and r[0] == "2025")]
        with self.assertRaises(ValueError) as e:
            m.read_file(write_csv(self.d / "g2.csv", rows))
        self.assertIn("incomplete grid for E06000002 2025", str(e.exception))
        rows = base + [base[-1]]
        with self.assertRaises(ValueError) as e:
            m.read_file(write_csv(self.d / "d.csv", rows))
        self.assertIn("duplicate", str(e.exception))

    def test_markers(self):
        self.assertEqual(m.cell("c"), (None, "suppressed"))
        self.assertEqual(m.cell("z"), (None, "not_applicable"))
        self.assertEqual(m.cell("x"), (None, "not_available"))
        self.assertEqual(m.cell("0"), (0, None))
        self.assertEqual(m.cell("634"), (634, None))
        for bad in ("k", "", " ", "-1", "1.5", "1,234", "low", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                m.cell(bad)

    def test_k_or_blank_in_a_count_halts(self):
        for bad in ("k", ""):
            p = three(self.d, f"k{bad}.csv", "care_leaver_2025", "17-21",
                      ["E06000001"], [2025], values={
                          ("E06000001", 2025, "17 to 18 years", "Foyers"):
                              bad})
            with self.assertRaises(ValueError) as e:
                m.read_file(p)
            self.assertIn("count value", str(e.exception))

    def test_check_identity(self):
        p = three(self.d, "i.csv", "care_leaver_2025", "22-25", ["E06000001"],
                  [2023, 2024, 2025])
        f = m.read_file(p)
        ds = {"cohort": "22-25", "timePeriodRange": {"start": "2023",
                                                      "end": "2025"},
              "numDataFileRows": f["row_count"], "slug": "2025"}
        self.assertEqual(m.check_identity(f, ds, "2025"), [])
        self.assertEqual(m.check_identity(f, None, "2025"), [])
        # the slug must be the file's own latest year (its release rank)
        self.assertTrue(any("own latest year is 2025, the release is 2024"
                            in x for x in m.check_identity(f, None, "2024")))
        bad = dict(ds, timePeriodRange={"start": "2022", "end": "2025"},
                   numDataFileRows=7968)
        probs = m.check_identity(f, bad, "2025")
        self.assertTrue(any("the page says 2022-2025" in x for x in probs))
        self.assertTrue(any("the page says 7968" in x for x in probs))
        self.assertTrue(any("the page's dataset is 17-21" in x for x in
                            m.check_identity(f, dict(ds, cohort="17-21"),
                                             "2025")))

    def test_release_rank(self):
        self.assertEqual(m.release_rank(m.guidance_url("2024")), 2024)
        self.assertIsNone(m.release_rank("as loaded: DfE Children Looked "
                                         "After SSDA903, reporting year 2024"))
        self.assertIsNone(m.release_rank(None))
        p = three(self.d, "r.csv", "accommodation_type", "17-21",
                  ["E06000001"], [2019, 2020, 2021, 2022, 2023])
        self.assertEqual(m.read_file(p)["max_year"], 2023)

    def test_content_sha256(self):
        p = self.d / "b.bin"
        p.write_bytes(b"abc")
        self.assertEqual(m.content_sha256(p),
                         "ba7816bf8f01cfea414140de5dae2223"
                         "b00361a396177a9cb410ff61f20015ad")


class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def build(self, schema, cohort, codes, years, **kw):
        f = m.read_file(three(self.d, "f.csv", schema, cohort, codes, years,
                              **kw))
        return m.build_rows(f, identity)

    def test_published_values_sum_over_bands(self):
        b = self.build("care_leaver_2025", "17-21", ["E06000001"], [2025])
        (r,) = b[2025]
        self.assertEqual(r["semi_independent_published"], 3)
        self.assertEqual(r["semi_independent"], 9)
        self.assertEqual(r["with_family"], 6)
        self.assertEqual(r["other"], 15)
        self.assertEqual(r["total_care_leavers"], 51)
        self.assertEqual(r["total_published"], 51)
        self.assertIs(r["suppressed_flag"], False)
        self.assertIsNone(r["null_reasons"])
        self.assertIsNone(r["suitable_count"])
        self.assertIsNone(r["unsuitable_pct"])
        self.assertIs(r["uasc_impact_flag"], False)
        self.assertEqual((r["lad24cd"], r["age_group"], r["reporting_year"]),
                         ("E06000001", "17-21", "2025"))
        self.assertIsNone(r["attribution"])

    def test_one_suppressed_part_makes_the_bucket_and_total_null(self):
        b = self.build("care_leaver_2025", "17-21", ["E06000001"], [2025],
                       values={("E06000001", 2025, "17 to 18 years",
                                "Foyers"): "c"})
        (r,) = b[2025]
        self.assertIsNone(r["semi_independent"])
        self.assertIsNone(r["foyers"])
        self.assertIsNone(r["total_care_leavers"])
        self.assertEqual(r["semi_independent_published"], 3)
        self.assertEqual(r["supported_lodgings"], 3)
        self.assertEqual(r["total_published"], 51)
        self.assertIs(r["suppressed_flag"], True)
        self.assertEqual(r["null_reasons"],
                         "foyers=suppressed;semi_independent=suppressed;"
                         "total_care_leavers=suppressed")

    def test_published_zero_stays_zero_and_mixed_reasons(self):
        b = self.build("accommodation_type", "17-21", ["E06000001"], [2023],
                       values={
                           ("E06000001", 2023, "17 to 18 years",
                            "Bed and breakfast"): "0",
                           ("E06000001", 2023, "19 to 21 years",
                            "Bed and breakfast"): "0",
                           ("E06000001", 2023, "17 to 18 years",
                            "Emergency accommodation"): "0",
                           ("E06000001", 2023, "19 to 21 years",
                            "Emergency accommodation"): "0",
                           ("E06000001", 2023, "17 to 18 years",
                            "No fixed abode/homeless"): "0",
                           ("E06000001", 2023, "19 to 21 years",
                            "No fixed abode/homeless"): "0",
                           ("E06000001", 2023, "17 to 18 years", "Deported"):
                               "x",
                           ("E06000001", 2023, "19 to 21 years", "Gone abroad"):
                               "c"})
        (r,) = b[2023]
        self.assertEqual(r["unsuitable"], 0)
        self.assertIsNone(r["other"])
        self.assertIn("other=mixed", r["null_reasons"])
        self.assertIn("total_care_leavers=mixed", r["null_reasons"])
        self.assertIs(r["suppressed_flag"], False)   # no c in semi parts

    def test_z_and_x_reasons(self):
        b = self.build("care_leaver_2025", "17-21", ["E06000001"], [2025],
                       values={("E06000001", 2025, "17 to 18 years", "Total"):
                               "x",
                               ("E06000001", 2025, "19 to 21 years",
                                "In custody"): "z"})
        (r,) = b[2025]
        self.assertIn("total_published=not_available", r["null_reasons"])
        self.assertIn("other=not_applicable", r["null_reasons"])

    def test_all_z_row_is_dropped_and_counted(self):
        b = self.build("care_leaver_2025", "17-21",
                       ["E06000001", "E06000002"], [2024, 2025],
                       allz=[("E06000002", 2024)])
        self.assertEqual([r["lad24cd"] for r in b[2024]], ["E06000001"])
        self.assertEqual(len(b[2025]), 2)
        self.assertEqual(b.dropped, [("E06000002", 2024)])

    def test_22_25_sums_all_four_ages_liverpool(self):
        code = "E08000012"
        vals = {}
        for age, n in (("22 years", 203), ("23 years", 166),
                       ("24 years", 148), ("25 years", 117)):
            vals[(code, 2025, age, "Total")] = str(n)
            vals[(code, 2025, age, "No information")] = str(n - 7)
            vals[(code, 2025, age, "Accommodation considered suitable")] = "5"
            vals[(code, 2025, age,
                  "Accommodation considered unsuitable")] = "2"
        b = self.build("care_leaver_2025", "22-25", [code], [2025],
                       values=vals)
        (r,) = b[2025]
        self.assertEqual(r["total_published"], 634)    # not 117
        self.assertEqual(r["total_care_leavers"], 634)
        self.assertEqual(r["suitable_count"], 20)
        self.assertEqual(r["unsuitable"], 8)
        self.assertEqual(r["not_known"], 606)
        self.assertIsNone(r["suppressed_flag"])
        self.assertIsNone(r["semi_independent"])
        # one suppressed age makes the cohort's column NULL
        vals[(code, 2025, "25 years", "Accommodation considered suitable")] = "c"
        (r,) = self.build("care_leaver_2025", "22-25", [code], [2025],
                          values=vals)[2025]
        self.assertIsNone(r["suitable_count"])
        self.assertIsNone(r["total_care_leavers"])
        self.assertEqual(r["total_published"], 634)
        self.assertEqual(r["null_reasons"], "suitable_count=suppressed;"
                                            "total_care_leavers=suppressed")

    def test_predecessor_attribution(self):
        b = self.build("accommodation_type", "17-21",
                       ["E06000028", "E10000006"], [2019])
        r = {x["lad24cd"]: x for x in b[2019]}
        self.assertEqual(r["E06000028"]["attribution"], "predecessor")
        self.assertEqual(r["E06000028"]["successor_codes"], ["E06000058"])
        self.assertIn("Bournemouth", r["E06000028"]["attribution_note"])
        self.assertIn("never applied", r["E06000028"]["attribution_note"])
        self.assertEqual(r["E10000006"]["successor_codes"],
                         ["E06000063", "E06000064"])
        self.assertNotIn("zero afterwards", r["E10000006"]["attribution_note"])

    def test_two_codes_to_one_key_raise_in_build(self):
        f = m.read_file(three(self.d, "f.csv", "accommodation_type", "17-21",
                              ["E10000023", "E06000065"], [2023]))
        with self.assertRaises(ValueError) as e:
            m.build_rows(f, {"E10000023": "E06000065",
                             "E06000065": "E06000065"})
        self.assertIn("both resolve to E06000065", str(e.exception))

    def test_buckets_partition_the_categories(self):
        cats = [c for _, cs in m.BUCKETS_1721 for c in cs]
        self.assertEqual(sorted(cats + ["Total"]), sorted(m.CATS_1721))

    def test_geography_declares_source_4_old(self):
        self.assertEqual(geography.DATASET_FORM["4"][0], "old")
        self.assertIn("data/raw/s4_cla", geography.DATASET_FORM["4"][1])
        self.assertTrue(geography.check_forms("4", {"E08000038"}))
        self.assertEqual(geography.check_forms("4", {"E08000016"}), [])


def rec(code, cohort="17-21", **v):
    r = {c: None for c in m.DATA_COLUMNS}
    r.update(lad24cd=code, age_group=cohort, reporting_year="2024")
    for c in m.BUILT[cohort]:
        r[c] = 10
    r["total_published"] = 100
    r.update(v)
    return r


class StopConditions(unittest.TestCase):
    def test_total_published_change_in_more_than_three_authorities(self):
        tip = [rec(f"E0600000{i}") for i in range(1, 9)]
        three_ = [rec(f"E0600000{i}", total_published=126 if i <= 3 else 100)
                  for i in range(1, 9)]
        four = [rec(f"E0600000{i}", total_published=126 if i <= 4 else 100)
                for i in range(1, 9)]
        self.assertFalse(any("more than 25%" in p for p in m.year_problems(
            three_, tip, kind="revised")))
        self.assertTrue(any("more than 25% in 4 authorities" in p for p in
                            m.year_problems(four, tip, kind="revised")))
        exactly = [rec(f"E0600000{i}", total_published=125 if i <= 5 else 100)
                   for i in range(1, 9)]
        self.assertFalse(any("more than 25%" in p for p in m.year_problems(
            exactly, tip, kind="revised")))

    def test_national_total_moving_more_than_five_percent(self):
        tip = [rec(f"E06{i:06d}") for i in range(1, 41)]   # national 4,000
        ok = [rec(f"E06{i:06d}", total_published=120 if i <= 10 else 100)
              for i in range(1, 41)]                       # +200 = 5.0%
        bad = [rec(f"E06{i:06d}", total_published=121 if i <= 10 else 100)
               for i in range(1, 41)]                      # +210 = 5.25%
        self.assertEqual(m.year_problems(ok, tip, kind="revised"), [])
        probs = m.year_problems(bad, tip, kind="revised")
        self.assertTrue(any("national total_published 4,000 -> 4,210" in p
                            for p in probs), probs)

    def test_held_authority_with_a_published_number_absent(self):
        tip = [rec("E06000001"), rec("E06000002")]
        probs = m.year_problems([rec("E06000001")], tip, kind="revised")
        self.assertTrue(any("absent from the release: E06000002" in p
                            for p in probs))
        # an authority the release publishes as all z is listed, not stopped
        self.assertFalse(any("absent" in p for p in m.year_problems(
            [rec("E06000001")], tip, kind="revised",
            all_z={("E06000002", "17-21")})))
        # an all-NULL held row disappearing is not a stop
        tip = [rec("E06000001"), rec("E06000002", **{c: None for c in
                                                     m.BUILT["17-21"]})]
        self.assertEqual(m.year_problems([rec("E06000001")], tip,
                                         kind="revised"), [])

    def test_new_year_authorities_range_both_sides(self):
        lo, hi = m.AUTHORITIES_RANGE
        for n, bad in ((lo - 1, True), (lo, False), (hi, False),
                       (hi + 1, True)):
            new = [rec(f"E06{i:06d}") for i in range(n)]
            probs = m.year_problems(new, [], kind="new")
            self.assertEqual(any("authorities, expected" in p for p in probs),
                             bad, (n, probs))

    def test_new_year_missing_held_latest_authority(self):
        held = [rec(f"E06{i:06d}") for i in range(150)]
        new = held[:-1]
        probs = m.year_problems(new, held, kind="new")
        self.assertTrue(any("absent from the release" in p for p in probs))

    def test_one_cohort_against_a_two_cohort_tip(self):
        tip = [rec("E06000001"), rec("E06000001", "22-25")]
        probs = m.year_problems([rec("E06000001")], tip, kind="revised")
        self.assertTrue(any("no carry-forward" in p for p in probs))
        self.assertEqual(m.year_problems(
            [rec("E06000001"), rec("E06000001", "22-25")],
            [rec("E06000001")], kind="revised"), [])

    def test_zero_null_flips(self):
        tip = [rec("E06000001", foyers=0, other=None),
               rec("E06000002", foyers=3)]
        new = [rec("E06000001", foyers=None, other=0),
               rec("E06000002", foyers=None)]
        self.assertEqual(m.zero_null_flips(new, tip), 2)
        self.assertEqual(m.zero_null_flips(tip, tip), 0)


class Planning(unittest.TestCase):
    def files(self, rank, years, cohorts=("17-21",)):
        return [{"years": list(years), "rank": rank, "source": f"u{c}",
                 "sha": f"s{c}{rank}", "cohort": c} for c in cohorts]

    def test_older_tip_is_skipped_per_year(self):
        f = self.files(2024, range(2020, 2025))
        tips = {2020: 2023, 2021: 2025, 2022: 2025, 2023: 2025, 2024: 2025}
        new, rev, skip = m.plan_years(f, tips, {}, recheck=None,
                                      allow_older=False)
        self.assertEqual((new, rev), ([], [2020]))
        self.assertEqual(sorted(skip), [2021, 2022, 2023, 2024])
        self.assertTrue(all(r.startswith("older") for r in skip.values()))
        new, rev, skip = m.plan_years(f, tips, {}, recheck=None,
                                      allow_older=True)
        self.assertEqual((rev, skip), ([2020, 2021, 2022, 2023, 2024], {}))

    def test_new_years_and_ledger_skip_and_recheck(self):
        f = self.files(2025, range(2021, 2026), ("17-21", "22-25"))
        f[1]["years"] = [2023, 2024, 2025]
        tips = {y: 2025 for y in range(2021, 2025)}
        checked = {2024: {("u17-21", "s17-212025"), ("u22-25", "s22-252025")},
                   2023: {("u17-21", "s17-212025")}}
        new, rev, skip = m.plan_years(f, tips, checked, recheck=None,
                                      allow_older=False)
        self.assertEqual(new, [2025])
        self.assertEqual(rev, [2021, 2022, 2023])
        self.assertEqual(list(skip), [2024])
        new, rev, skip = m.plan_years(f, tips, checked, recheck=2024,
                                      allow_older=False)
        self.assertEqual(rev, [2021, 2022, 2023, 2024])
        with self.assertRaises(ValueError):
            m.plan_years(f, tips, checked, recheck=2025, allow_older=False)

    def test_two_files_of_different_releases_raise(self):
        f = self.files(2024, range(2020, 2025)) + self.files(2025, [2023])
        with self.assertRaises(ValueError):
            m.plan_years(f, {}, {}, recheck=None, allow_older=False)


if __name__ == "__main__":
    unittest.main()
