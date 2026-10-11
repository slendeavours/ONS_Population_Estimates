"""Tests for the reissue checks in check_sources.py (S5 File 10, S7 May 2024
layer): a reissue of the held vintage is not a newer period, so these read
the publisher's rank for the held vintage and compare it with the rank the
loader recorded.

No network and no database: fetch() and held_sources() are stubbed, and
check_one() is called directly (main(), which writes the check log, is never
run).
"""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import check_sources as cs  # noqa: E402
import s5_imd_editions as s5  # noqa: E402
import s7_boundaries_editions as s7  # noqa: E402

UTC = timezone.utc
CHECKED = datetime(2026, 10, 11, 13, 58, 56, tzinfo=UTC)
V2_URL = s5.LEGACY_FILE["url"]
V3_URL = ("https://assets.publishing.service.gov.uk/media/"
          "aaaabbbbccccddddeeeeffff/File_10_-_IoD2025_Local_Authority_"
          "District_Summaries__lower-tier__v3.xlsx")
PAGE_2025 = "/government/statistics/english-indices-of-deprivation-2025"
PAGE_2030 = "/government/statistics/english-indices-of-deprivation-2030"
COLLECTION = ("https://www.gov.uk/api/content/government/collections/"
              "english-indices-of-deprivation")
HELD_MS = s7.LEGACY_RANK_MS
SERVICES = s7.list_url()
LAYER = s7.layer_url(s7.service_of(s7.LIVE_VINTAGE))


def s5_held(url=V2_URL, checked=CHECKED):
    return [(s5.edition_source(
                {"vintage": "IoD2025", "modified": s5.LEGACY_RANK},
                {"file_name": url.rsplit("/", 1)[-1], "where": url}), None),
            (s5.ledger_source(url, "IoD2025", s5.LEGACY_RANK), checked)]


def s5_page(url=V2_URL, notes=(("2025-10-30", "First published."),
                               ("2025-11-17", "File 10 reissued (v2)."))):
    return {"title": "English indices of deprivation 2025",
            "details": {
                "attachments": [
                    {"title": "File 1: Index of Multiple Deprivation",
                     "url": "https://assets.example/File_1.xlsx"},
                    {"title": "File 10: Local Authority District summaries "
                              "- lower tier", "url": url}],
                "change_history": [{"public_timestamp": d + "T09:30:00Z",
                                    "note": n} for d, n in notes]}}


def collection(*years):
    return {"links": {"documents": [
        {"title": f"English indices of deprivation {y}",
         "base_path": f"/government/statistics/english-indices-of-"
                      f"deprivation-{y}",
         "public_updated_at": f"{y}-11-17T09:30:00Z"} for y in years]}}


def s5_reg(loaded="IoD2025"):
    return {"source_code": "5", "landing_page_url": None,
            "latest_period_loaded": loaded, "last_seen_fingerprint": None,
            "revises_back_series": True, "target_table": "la_imd_2025",
            "detected_period_type": "reference_period"}


def s7_reg():
    return {"source_code": "7", "landing_page_url": None,
            "latest_period_loaded": "2024-05-01",
            "last_seen_fingerprint": None, "revises_back_series": False,
            "target_table": "la_boundaries", "detected_period_type": None}


def layer(ms=HELD_MS):
    fields = [{"name": n, "type": t or "esriFieldTypeString"}
              for n, t in s7._expected_fields("24").items()]
    return {"name": "LAD_MAY_2024_UK_BGC", "type": "Feature Layer",
            "geometryType": "esriGeometryPolygon", "fields": fields,
            "editingInfo": {"lastEditDate": ms}}


def services(*vintages):
    return {"services": [{"name": s7.service_of(v), "type": "FeatureServer"}
                         for v in vintages]}


def s7_held(ms=HELD_MS):
    rank = s7._ms_rank(ms)
    return [(f"as loaded: LAD_MAY_2024.geojson; source_date 2024-05-01; "
             f"{s7.rank_text(rank)}", None),
            (s7.ledger_source(s7.LEGACY_FILE["url"], "2024-05-01", rank),
             CHECKED)]


def router(pages):
    """A fetch() stand-in: (200, the JSON text) for a known URL, else an
    error (a test must never reach the network)."""
    def fetch(url):
        if url not in pages:
            raise AssertionError(f"unexpected fetch {url}")
        body = pages[url]
        if isinstance(body, Exception):
            raise body
        return 200, json.dumps(body)
    return fetch


def run_s5(pages, held):
    with mock.patch.object(cs, "fetch", router(pages)), \
            mock.patch.object(cs, "held_sources", return_value=held) as hs:
        row = cs.check_one("5", s5_reg())
    return row, hs


def run_s7(pages, held):
    with mock.patch.object(cs, "fetch", router(pages)), \
            mock.patch.object(cs, "held_sources", return_value=held) as hs:
        row = cs.check_one("7", s7_reg())
    return row, hs


class S5Reissue(unittest.TestCase):
    def test_held_file_and_no_later_note_is_no_change(self):
        row, hs = run_s5({COLLECTION: collection(2019, 2025),
                          cs.GOVUK_CONTENT + PAGE_2025: s5_page()},
                         s5_held())
        self.assertEqual(row["outcome"], "no_change")
        self.assertIn("reissue check clean", row["notes"])
        self.assertIsNone(row["error_detail"])
        hs.assert_called_once_with("la_imd_2025_editions", "vintage",
                                   "IoD2025")

    def test_a_new_file10_attachment_is_reissue_possible(self):
        row, _ = run_s5({COLLECTION: collection(2025),
                         cs.GOVUK_CONTENT + PAGE_2025: s5_page(V3_URL)},
                        s5_held())
        self.assertEqual(row["outcome"], "revision_detected")
        self.assertIn("reissue possible", row["error_detail"])
        self.assertIn("lower-tier__v3.xlsx", row["error_detail"])
        self.assertIn("docProps modified 2025-11-14T12:48:06Z",
                      row["error_detail"])
        self.assertIn("REISSUE POSSIBLE", row["notes"])

    def test_a_change_note_after_the_last_check_is_reissue_possible(self):
        notes = (("2025-10-30", "First published."),
                 ("2026-11-02", "File 10 corrected."))
        row, _ = run_s5({COLLECTION: collection(2025),
                         cs.GOVUK_CONTENT + PAGE_2025: s5_page(notes=notes)},
                        s5_held())
        self.assertEqual(row["outcome"], "revision_detected")
        self.assertIn("2026-11-02", row["error_detail"])
        self.assertIn("File 10 corrected.", row["error_detail"])

    def test_no_file10_attachment_is_check_failed_not_no_change(self):
        page = s5_page()
        page["details"]["attachments"] = page["details"]["attachments"][:1]
        row, _ = run_s5({COLLECTION: collection(2025),
                         cs.GOVUK_CONTENT + PAGE_2025: page}, s5_held())
        self.assertEqual(row["outcome"], "check_failed")
        self.assertIn("reissue check not possible", row["error_detail"])

    def test_nothing_held_is_check_failed(self):
        row, _ = run_s5({COLLECTION: collection(2025),
                         cs.GOVUK_CONTENT + PAGE_2025: s5_page()}, [])
        self.assertEqual(row["outcome"], "check_failed")
        self.assertIn("no edition or ledger row", row["error_detail"])

    def test_a_newer_year_stays_new_edition_and_the_held_page_is_read(self):
        row, _ = run_s5({COLLECTION: collection(2025, 2030),
                         cs.GOVUK_CONTENT + PAGE_2025: s5_page(V3_URL)},
                        s5_held())
        self.assertEqual(row["outcome"], "new_edition")
        self.assertEqual(row["detected_period"], "IoD2030")
        self.assertIn("REISSUE POSSIBLE", row["notes"])
        self.assertIn("reissue possible", row["error_detail"])


class S7Reissue(unittest.TestCase):
    def test_same_last_edit_date_is_no_change(self):
        row, hs = run_s7({SERVICES: services("May_2024"), LAYER: layer()},
                         s7_held())
        self.assertEqual(row["outcome"], "no_change")
        self.assertIn("reissue check clean", row["notes"])
        self.assertIsNone(row["error_detail"])
        hs.assert_called_once_with("la_boundaries_editions", "source_date",
                                   "2024-05-01")

    def test_a_later_last_edit_date_is_reissue_possible(self):
        later = HELD_MS + 86_400_000
        row, _ = run_s7({SERVICES: services("May_2024"),
                         LAYER: layer(later)}, s7_held())
        self.assertEqual(row["outcome"], "revision_detected")
        self.assertIn("reissue possible", row["error_detail"])
        self.assertIn(str(later), row["error_detail"])
        self.assertIn(str(HELD_MS), row["error_detail"])

    def test_the_ledger_rank_counts_as_held(self):
        # an unchanged re-read records a later rank in the ledger only
        later = HELD_MS + 1000
        held = s7_held()[:1] + s7_held(later)[1:]
        row, _ = run_s7({SERVICES: services("May_2024"),
                         LAYER: layer(later)}, held)
        self.assertEqual(row["outcome"], "no_change")

    def test_a_newer_vintage_keeps_new_edition_with_the_finding(self):
        later = HELD_MS + 86_400_000
        row, _ = run_s7({SERVICES: services("May_2024", "May_2026"),
                         LAYER: layer(later)}, s7_held())
        self.assertEqual(row["outcome"], "new_edition")
        self.assertIn("REISSUE POSSIBLE", row["notes"])

    def test_an_unreadable_layer_is_check_failed(self):
        bad = layer()
        del bad["editingInfo"]
        row, _ = run_s7({SERVICES: services("May_2024"), LAYER: bad},
                        s7_held())
        self.assertEqual(row["outcome"], "check_failed")
        self.assertIn("reissue check not possible", row["error_detail"])

    def test_nothing_held_is_check_failed(self):
        row, _ = run_s7({SERVICES: services("May_2024"), LAYER: layer()},
                        [])
        self.assertEqual(row["outcome"], "check_failed")


class Outcomes(unittest.TestCase):
    def test_reissue_outcome_is_allowed_by_the_check_log(self):
        sql = (SCRIPTS.parent / "sql" / "source_registry.sql").read_text(
            encoding="utf-8")
        self.assertIn("'revision_detected'", sql)


if __name__ == "__main__":
    unittest.main()
