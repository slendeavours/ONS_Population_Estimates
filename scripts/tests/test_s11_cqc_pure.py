"""Pure tests for s11_cqc_editions: the page, the file's identity, parsing,
mapping, stop conditions and the load plan. No network and no database:
every ODS file is built by the test (write_ods), the page is a copy with the
five CQC links, and postcodes.io is a stub (Api).
"""
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geography  # noqa: E402
import s11_cqc_editions as m  # noqa: E402

BASE = "https://www.cqc.org.uk/system/files/2026-10/"
FILTERS = BASE + "01_October_2026_HSCA_Active_Locations.ods"


def page_of(*links) -> str:
    """A copy of the CQC page's document links: (href, label) pairs."""
    parts = ["<html><body>"]
    for href, label in links:
        parts.append('<div class="document-sidebar">\n'
                     f'  <a href="{href}">{label}</a>\n'
                     '  <span class="document-info">(ods, 23.04MB)</span>'
                     "</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


FIVE = (
    (BASE + "07_October_2026_CQC_directory.csv", "CQC care directory - csv"),
    (BASE + "07_October_2026_CQC_directory.zip", "CQC care directory - zip"),
    (FILTERS, "Care directory with filters (02 October 2026)"),
    (BASE + "01_October_2026_Latest_ratings.ods",
     "Care directory with ratings (02 October 2026)"),
    (BASE + "01_October_2026_Deactivated_Locations.ods",
     "Deactivated locations (02 October 2026)"),
)

# --- fixture files ---------------------------------------------------------

NUMERIC = {"Location Latitude", "Location Longitude", "Care homes beds"}
DATES = {"Location HSCA start date", "Publication Date"}


def loc(lid, lat="53.380000", lon="-1.470000", **over) -> dict:
    """One location as {header: text}; keyword names use the legacy column
    names for the common overrides (see KW), anything else by header."""
    v = {h: "" for h in m.EXPECTED_HEADERS}
    v.update({
        "Location ID": lid, "Location HSCA start date": "2020-12-11T00:00:00",
        "Dormant (Y/N)": "N", "Care home?": "N", "Location Name": f"Home {lid}",
        "Care homes beds": "0",
        "Location Inspection Directorate": "Adult social care",
        "Location Primary Inspection Category":
            "Community based adult social care services",
        "Location Latest Overall Rating": "Good",
        "Publication Date": "2022-04-20T00:00:00",
        "Inherited Rating (Y/N)": "N",
        "Location Region": "Yorkshire and The Humber",
        "Location Local Authority": "Sheffield",
        "Location Postal Code": "S1 2AB", "Location Latitude": lat,
        "Location Longitude": lon, "Brand Name": "-",
        "Provider ID": "1-100", "Provider Name": "Provider Ltd",
        "Service user band - Older People": "Y",
        "Location ONSPD CCG Code": "E38000146",
    })
    for k, val in over.items():
        v[KW.get(k, k)] = val
    return v


KW = {"name": "Location Name", "beds": "Care homes beds",
      "care_home": "Care home?", "dormant": "Dormant (Y/N)",
      "brand": "Brand Name", "directorate": "Location Inspection Directorate",
      "supported_living": "Service type - Supported living service",
      "postcode": "Location Postal Code", "inherited": "Inherited Rating (Y/N)",
      "la_name": "Location Local Authority", "rating": "Location Latest "
      "Overall Rating"}


def _cell(header, value) -> str:
    if value == "":
        return "<table:table-cell/>"
    if header in NUMERIC and m._DECIMAL.fullmatch(value):
        return (f'<table:table-cell office:value-type="float" '
                f'office:value="{value}"><text:p>{escape(value)}</text:p>'
                "</table:table-cell>")
    if header in DATES and m._ISO_DATE.fullmatch(value):
        return (f'<table:table-cell office:value-type="date" '
                f'office:date-value="{value}"><text:p>x</text:p>'
                "</table:table-cell>")
    return ('<table:table-cell office:value-type="string"><text:p>'
            f"{escape(value)}</text:p></table:table-cell>")


def _row(cells, headers=None, repeat=1) -> str:
    """A row; runs of blank cells are written with number-columns-repeated
    and the row ends with a wide repeated filler, as CQC's files do."""
    out, blanks = [], 0
    for i, v in enumerate(cells):
        if v == "":
            blanks += 1
            continue
        if blanks:
            out.append(f'<table:table-cell table:number-columns-repeated='
                       f'"{blanks}"/>')
            blanks = 0
        out.append(_cell(headers[i] if headers else None, v))
    out.append('<table:table-cell table:number-columns-repeated="16262"/>')
    rep = (f' table:number-rows-repeated="{repeat}"' if repeat > 1 else "")
    return f"<table:table-row{rep}>{''.join(out)}</table:table-row>"


def write_ods(path, as_at="01 October 2026", locations=(), *,
              title=m.TITLE, header=None, raw_rows=(), repeat=None,
              readme_first=True) -> Path:
    """A CQC-like .ods: README (title, 'Source: CQC database as at ...'),
    HSCA_Active_Locations (header and one row per location) and
    Dual_Registration_Locations. raw_rows are extra rows of cell lists;
    repeat {index: n} writes that location row with number-rows-repeated."""
    header = list(header or m.EXPECTED_HEADERS)
    readme = [[title], [f"Source: CQC database as at {as_at}"],
              ["Management Information Requests Performance Team"]]
    t_readme = ('<table:table table:name="README">'
                + "".join(_row(r) for r in readme) + "</table:table>")
    rows = [_row(header)]
    for i, lv in enumerate(locations):
        rows.append(_row([lv.get(h, "") for h in m.EXPECTED_HEADERS],
                         m.EXPECTED_HEADERS, (repeat or {}).get(i, 1)))
    rows += [_row(r) for r in raw_rows]
    rows.append('<table:table-row table:number-rows-repeated="1048000">'
                '<table:table-cell table:number-columns-repeated="16384"/>'
                "</table:table-row>")
    t_data = (f'<table:table table:name="{m.DATA_SHEET}">' + "".join(rows)
              + "</table:table>")
    t_dual = ('<table:table table:name="Dual_Registration_Locations">'
              + _row(["Location ID", "Primary ID"]) + "</table:table>")
    tables = [t_readme, t_data, t_dual] if readme_first else [
        t_data, t_readme, t_dual]
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'office:version="1.2"><office:body><office:spreadsheet>'
        + "".join(tables)
        + "</office:spreadsheet></office:body></office:document-content>")
    path = Path(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype",
                   "application/vnd.oasis.opendocument.spreadsheet")
        z.writestr("content.xml", content)
    return path


def ods_name(d: date) -> str:
    return (f"{d.day:02d}_{m._MONTHS[d.month - 1].title()}_{d.year}_"
            "HSCA_Active_Locations.ods")


# --- boundaries and the postcode stub ---------------------------------------

def square(lon0, lat0, lon1, lat1) -> dict:
    return {"type": "Polygon", "coordinates": [[
        [lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}


SHEFFIELD, BARNSLEY = "E08000019", "E08000016"
BOUNDARIES = [(BARNSLEY, square(-1.6, 53.45, -1.4, 53.6)),
              (SHEFFIELD, square(-1.6, 53.30, -1.4, 53.45))]
RECODES = {**geography.RECODES_FALLBACK,
           **{v: v for v in geography.RECODES_FALLBACK.values()}}


class Api:
    """A postcodes.io stand-in: codes {postcode: admin_district}, term
    {postcode: (lat, lon)}."""

    def __init__(self, codes=None, term=None):
        self.codes, self.term = dict(codes or {}), dict(term or {})
        self.calls = []

    def lookup(self, postcodes):
        self.calls.append(("lookup", list(postcodes)))
        return {p: self.codes[p] for p in postcodes if p in self.codes}

    def terminated(self, postcode):
        self.calls.append(("terminated", postcode))
        return self.term.get(postcode)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def ods(self, locations, name=None, **kw):
        return write_ods(self.dir / (name or ods_name(date(2026, 10, 1))),
                         locations=locations, **kw)


# --- tests -----------------------------------------------------------------

class Page(unittest.TestCase):
    def test_only_the_filters_file_is_chosen(self):
        html = page_of(*FIVE)
        self.assertEqual(m.find_filters_link(html), FILTERS)
        self.assertEqual(m.page_date(html), date(2026, 10, 2))

    def test_two_filters_links_raise_naming_both(self):
        other = BASE + "15_October_2026_HSCA_Active_Locations.xlsx"
        html = page_of(*FIVE, (other, "Care directory with filters"))
        with self.assertRaises(ValueError) as e:
            m.find_filters_link(html)
        self.assertIn(FILTERS, str(e.exception))
        self.assertIn(other, str(e.exception))

    def test_no_filters_link_raises_listing_what_was_seen(self):
        html = page_of(*(x for x in FIVE if x[0] != FILTERS))
        with self.assertRaises(ValueError) as e:
            m.find_filters_link(html)
        self.assertIn("Latest_ratings", str(e.exception))
        self.assertIsNone(m.page_date(html))

    def test_relative_link_is_made_absolute(self):
        html = page_of(("/system/files/2026-10/01_October_2026_HSCA_Active_"
                        "Locations.ods", "Care directory with filters"))
        self.assertEqual(m.find_filters_link(html), FILTERS)
        self.assertIsNone(m.page_date(html))

    def test_name_date(self):
        self.assertEqual(m.name_date("01_October_2026_HSCA_Active_Locations"
                                     ".ods"), date(2026, 10, 1))
        self.assertEqual(m.name_date("04_August_2026_HSCA_Active_Locations-"
                                     "ae87c164.ods"), date(2026, 8, 4))
        with self.assertRaises(ValueError):
            m.name_date("HSCA_Active_Locations.ods")


class Identity(_Tmp):
    def test_readme_identity_ok(self):
        p = self.ods([loc("1-1")])
        rd = m.read_readme(p)
        self.assertEqual(rd, {"title": m.TITLE, "as_at": date(2026, 10, 1)})
        self.assertEqual(m.check_identity(rd, p.name), [])

    def test_as_at_differs_from_the_name(self):
        p = self.ods([loc("1-1")], as_at="02 October 2026")
        probs = m.check_identity(m.read_readme(p), p.name)
        self.assertEqual(len(probs), 1)
        self.assertIn("as at 2026-10-02", probs[0])

    def test_wrong_title_and_missing_as_at(self):
        p = self.ods([loc("1-1")], title="Deactivated locations",
                     as_at="sometime")
        probs = m.check_identity(m.read_readme(p), p.name)
        self.assertTrue(any("title" in x for x in probs), probs)
        self.assertTrue(any("as at DD Month" in x for x in probs), probs)

    def test_first_sheet(self):
        self.assertEqual(m.first_sheet(self.ods([loc("1-1")])), "README")
        p = self.ods([loc("1-1")], name="x.ods", readme_first=False)
        self.assertEqual(m.first_sheet(p), m.DATA_SHEET)


class Reading(_Tmp):
    def test_repeat_handling(self):
        p = self.ods([loc("1-1"), loc("1-2")], repeat={1: 3})
        rows = list(m.iter_sheet_rows(p, m.DATA_SHEET))
        self.assertEqual(rows[0], list(m.EXPECTED_HEADERS))
        self.assertEqual([r[0] for r in rows[1:]],
                         ["1-1", "1-2", "1-2", "1-2"])
        # blank runs are expanded in place; trailing filler dropped
        i = m.EXPECTED_HEADERS.index("Service user band - Older People")
        self.assertEqual(rows[1][i], "Y")
        self.assertLessEqual(len(rows[1]), len(m.EXPECTED_HEADERS))
        # a repeated row is a duplicate location: parse halts
        with self.assertRaises(ValueError) as e:
            m.parse_locations(p)
        self.assertIn("appears twice", str(e.exception))

    def test_numbers_and_dates_come_from_their_value_attribute(self):
        p = self.ods([loc("1-1", beds="66", care_home="Y")])
        rows = list(m.iter_sheet_rows(p, m.DATA_SHEET))
        idx = m.EXPECTED_HEADERS.index
        self.assertEqual(rows[1][idx("Care homes beds")], "66")
        self.assertEqual(rows[1][idx("Publication Date")],
                         "2022-04-20T00:00:00")

    def test_xlsx_route_and_other_formats(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "README"
        ws.append([m.TITLE])
        ws.append(["Source: CQC database as at 01 October 2026"])
        ws2 = wb.create_sheet(m.DATA_SHEET)
        ws2.append(list(m.EXPECTED_HEADERS))
        v = loc("1-9")
        cells = [v[h] for h in m.EXPECTED_HEADERS]
        idx = m.EXPECTED_HEADERS.index
        cells[idx("Location Latitude")] = 53.38
        cells[idx("Care homes beds")] = 0
        from datetime import datetime
        cells[idx("Publication Date")] = datetime(2022, 4, 20)
        ws2.append([c if c != "" else None for c in cells])
        p = self.dir / "01_October_2026_HSCA_Active_Locations.xlsx"
        wb.save(p)
        self.assertEqual(m.read_readme(p)["as_at"], date(2026, 10, 1))
        self.assertEqual(m.first_sheet(p), "README")
        rows, _ = m.parse_locations(p)
        self.assertEqual(rows[0]["latitude"], Decimal("53.38"))
        self.assertEqual(rows[0]["care_homes_beds"], 0)
        self.assertEqual(rows[0]["rating_publication_date"], date(2022, 4, 20))
        csv = self.dir / "a.csv"
        csv.write_text("x")
        with self.assertRaises(ValueError):
            m.iter_sheet_rows(csv, "README")
        with self.assertRaises(ValueError):
            m.first_sheet(csv)


class Parse(_Tmp):
    def parse(self, *locations, **kw):
        return m.parse_locations(self.ods(list(locations), **kw))

    def halts(self, *locations, contains="", **kw):
        with self.assertRaises(ValueError) as e:
            self.parse(*locations, **kw)
        self.assertIn(contains, str(e.exception))
        return str(e.exception)

    def test_rows_types_and_scope(self):
        rows, meta = self.parse(
            loc("1-1", care_home="Y", beds="66", brand="BRAND Greensleeves",
                supported_living="Y", inherited="Y"),
            loc("1-2", directorate="Hospitals"),
            loc("1-3", inherited="", **{"Location Dual Registered":
                                        "Dual Registration",
                                        "Primary ID (Dual registration "
                                        "locations)": "1-1"}))
        self.assertEqual([r["location_id"] for r in rows], ["1-1", "1-3"])
        self.assertEqual(meta["directorates"],
                         {"Adult social care": 2, "Hospitals": 1})
        self.assertEqual(meta["rows_total"], 3)
        a, b = rows
        self.assertEqual(set(a), {"location_id"} | set(m.PUBLISHER_COLUMNS))
        self.assertIs(a["care_home"], True)
        self.assertEqual(a["care_homes_beds"], 66)
        self.assertEqual(a["brand_name"], "BRAND Greensleeves")
        self.assertIs(a["supported_living"], True)
        self.assertIs(a["personal_care"], False)
        self.assertIs(a["inherited_rating"], True)
        self.assertIs(a["dual_registered"], False)
        self.assertEqual(a["latitude"], Decimal("53.380000"))
        self.assertEqual(a["location_hsca_start_date"], date(2020, 12, 11))
        self.assertIsNone(b["inherited_rating"])
        self.assertIs(b["dual_registered"], True)
        self.assertEqual(b["dual_primary_id"], "1-1")
        self.assertEqual(meta["markers"]["Care home?"], {"Y": 1, "N": 1})

    def test_header_change_halts_and_lists_the_column(self):
        h = list(m.EXPECTED_HEADERS)
        h[9] = "Care home beds"
        msg = self.halts(loc("1-1"), header=h, contains="header changed")
        self.assertIn("column 10: 'Care home beds', expected 'Care homes "
                      "beds'", msg)
        msg = self.halts(loc("1-1"), header=h[:-1], contains="column 122")

    def test_unknown_flag_markers_halt(self):
        self.halts(loc("1-1", supported_living="X"), contains="'X'")
        self.halts(loc("1-1", **{"Service user band - Dementia": "N"}),
                   contains="'N'")
        self.halts(loc("1-1", supported_living="y"), contains="'y'")
        self.halts(loc("1-1", dormant=""), contains="Dormant (Y/N)")
        self.halts(loc("1-1", care_home=""), contains="Care home?")
        self.halts(loc("1-1", inherited="X"), contains="Inherited")
        self.halts(loc("1-1", **{"Location Dual Registered": "Y"}),
                   contains="Dual Registered")

    def test_brand_dash_is_null_and_blank_text_is_null(self):
        rows, _ = self.parse(loc("1-1", brand="-", rating=""))
        self.assertIsNone(rows[0]["brand_name"])
        self.assertIsNone(rows[0]["latest_overall_rating"])

    def test_beds_zero_is_zero_blank_is_null_fraction_halts(self):
        rows, meta = self.parse(loc("1-1", beds="0"),
                                loc("1-2", beds="", care_home="Y"))
        self.assertEqual(rows[0]["care_homes_beds"], 0)
        self.assertIsNotNone(rows[0]["care_homes_beds"])
        self.assertIsNone(rows[1]["care_homes_beds"])
        self.assertEqual(meta["markers"]["Care homes beds"], {"0": 1, "": 1})
        self.halts(loc("1-1", beds="3.5"), contains="not a whole number")
        self.halts(loc("1-1", beds="many"), contains="not a whole number")

    def test_unparseable_date_or_coordinate_halts_no_silent_null(self):
        self.halts(loc("1-1", **{"Publication Date": "20/04/2022"}),
                   contains="not an ISO date")
        self.halts(loc("1-1", **{"Location HSCA start date": "2020-02-30"}),
                   contains="HSCA start date")
        self.halts(loc("1-1", lat="53,38"), contains="not a number")
        self.halts(loc("1-1", lon="n/a"), contains="not a number")
        self.halts(loc("1-1", lat="40.1"), contains="outside England")
        self.halts(loc("1-1", lat="53.3800001"),
                   contains="more than 6 decimal places")
        rows, _ = self.parse(loc("1-1", lat="", lon=""))
        self.assertIsNone(rows[0]["latitude"])

    def test_barnsley_sheffield_code_in_any_cell_halts(self):
        for code in ("E08000038", "E08000039", "E08000016", "E08000019"):
            self.halts(loc("1-1", **{"Location ONSPD CCG Code": code}),
                       contains=code)
        # also in a row outside the Adult social care scope
        self.halts(loc("1-1"), loc("1-2", directorate="Hospitals",
                                   **{"Location County": "x E08000038"}),
                   contains="E08000038")
        rows, _ = self.parse(loc("1-1", **{"Location ONSPD CCG Code":
                                           "E080000190"}))
        self.assertEqual(len(rows), 1)

    def test_duplicate_or_blank_location_id_halts(self):
        self.halts(loc("1-1"), loc("1-1"), contains="appears twice")
        self.halts(loc(""), contains="blank Location ID")
        # the same id out of scope is not a duplicate in scope
        rows, _ = self.parse(loc("1-1"), loc("1-1", directorate="Hospitals"))
        self.assertEqual(len(rows), 1)

    def test_a_cell_beyond_the_header_halts(self):
        cells = [loc("1-1")[h] for h in m.EXPECTED_HEADERS] + ["extra"]
        self.halts(raw_rows=[cells], contains="cells, the header has 122")


class Mapping(unittest.TestCase):
    def rec(self, lid, lat="53.38", lon="-1.47", postcode="S1 2AB",
            la="Sheffield"):
        return {"location_id": lid, "location_name": f"Home {lid}",
                "postcode": postcode, "la_name_cqc": la,
                "latitude": Decimal(lat) if lat else None,
                "longitude": Decimal(lon) if lon else None}

    def run_map(self, rows, api=None, lookup=None):
        return m.map_locations(rows, BOUNDARIES, RECODES, lookup or {},
                               api=api or Api())

    def test_point_inside(self):
        mapped, unres = self.run_map([self.rec("a"), self.rec("b", "53.5")])
        self.assertEqual([(r["lad24cd"], r["mapping_method"]) for r in mapped],
                         [(SHEFFIELD, "point_in_polygon"),
                          (BARNSLEY, "point_in_polygon")])
        self.assertEqual(unres, [])

    def test_outside_within_2km_takes_the_nearest(self):
        mapped, _ = self.run_map([self.rec("a", lon="-1.39")])  # ~660 m
        self.assertEqual(mapped[0]["lad24cd"], SHEFFIELD)
        self.assertEqual(mapped[0]["mapping_method"], "nearest_fallback")
        self.assertIn(" m", mapped[0]["mapping_note"])

    def test_outside_over_2km_halts(self):
        with self.assertRaises(ValueError) as e:
            self.run_map([self.rec("a", lon="-1.30")])            # ~6.6 km
        self.assertIn("over 2000 m", str(e.exception))

    def test_api_new_code_maps_through_canonical(self):
        api = Api({"S1 2AB": "E08000039"})
        mapped, unres = self.run_map([self.rec("a", "", "")], api)
        self.assertEqual(mapped[0]["lad24cd"], SHEFFIELD)
        self.assertEqual(mapped[0]["mapping_method"], "postcode_api_fallback")
        self.assertEqual(unres, [])

    def test_api_code_through_la_code_lookup(self):
        api = Api({"S1 2AB": "E07000999"})
        mapped, _ = self.run_map([self.rec("a", "", "")], api,
                                 lookup={"E07000999": "E08000039"})
        self.assertEqual(mapped[0]["lad24cd"], SHEFFIELD)

    def test_api_unknown_code_halts(self):
        api = Api({"S1 2AB": "E06000099"})
        with self.assertRaises(ValueError) as e:
            self.run_map([self.rec("a", "", "")], api)
        self.assertIn("UNEXPLAINED E06000099", str(e.exception))

    def test_api_nothing_gives_an_unresolved_row(self):
        api = Api()
        mapped, unres = self.run_map([self.rec("a", "", "", la=None)], api)
        self.assertEqual(mapped, [])
        self.assertEqual(unres[0]["location_id"], "a")
        self.assertIn("not live, not terminated", unres[0]["reason"])
        self.assertIn("no LA name", unres[0]["reason"])
        self.assertIn(("terminated", "S1 2AB"), api.calls)

    def test_terminated_postcode_coordinates(self):
        api = Api(term={"S1 2AB": (53.39, -1.46)})
        mapped, _ = self.run_map([self.rec("a", "", "")], api)
        self.assertEqual(mapped[0]["mapping_method"],
                         "postcode_terminated_fallback")
        api = Api(term={"S1 2AB": (51.5, -0.1)})
        mapped, unres = self.run_map([self.rec("a", "", "")], api)
        self.assertEqual(mapped, [])
        self.assertIn("outside every la_boundaries polygon",
                      unres[0]["reason"])

    def test_no_postcode_and_no_coordinates(self):
        mapped, unres = self.run_map([self.rec("a", "", "", postcode=None)])
        self.assertIn("no postcode", unres[0]["reason"])

    def test_one_coordinate_alone_goes_to_the_postcode(self):
        api = Api({"S1 2AB": SHEFFIELD})
        mapped, _ = self.run_map([self.rec("a", lon="")], api)
        self.assertEqual(mapped[0]["mapping_method"], "postcode_api_fallback")
        self.assertEqual(mapped[0]["latitude"], Decimal("53.38"))

    def test_point_in_two_polygons_halts(self):
        b = BOUNDARIES + [("E06000001", square(-1.5, 53.35, -1.45, 53.40))]
        with self.assertRaises(ValueError) as e:
            m.map_locations([self.rec("a")], b, RECODES, {}, api=Api())
        self.assertIn("refusing to choose", str(e.exception))


def recs(n, lad=SHEFFIELD, sl=0, start=0, dormant=False):
    return [{"location_id": f"1-{start + i}", "lad24cd": lad,
             "supported_living": i < sl, "dormant": dormant}
            for i in range(n)]


class Stops(unittest.TestCase):
    AUTH = {SHEFFIELD}

    def probs(self, new, prev, unresolved=0, auth=None):
        return m.snapshot_problems(
            {"records": new, "unresolved": unresolved,
             "authorities": auth or self.AUTH},
            None if prev is None else {"records": prev})

    def test_rows_change_two_percent_both_sides(self):
        self.assertEqual(self.probs(recs(1020), recs(1000)), [])
        self.assertEqual(self.probs(recs(980), recs(1000)), [])
        self.assertIn("2.10%", self.probs(recs(1021), recs(1000))[0])
        self.assertTrue(self.probs(recs(979), recs(1000)))

    def test_gone_and_new_over_1000(self):
        old = recs(60000)
        self.assertEqual(self.probs(recs(60000, start=1000), old), [])
        p = self.probs(recs(60000, start=1001), old)
        self.assertEqual(len(p), 2, p)
        self.assertIn("1,001 locations gone", p[0])
        self.assertIn("1,001 locations new", p[1])

    def test_unresolved_over_10(self):
        self.assertEqual(self.probs(recs(5), None, unresolved=10), [])
        self.assertIn("11 unresolved", self.probs(recs(5), None, 11)[0])

    def test_authority_with_no_location(self):
        self.assertEqual(self.probs(recs(5), None, auth={SHEFFIELD}), [])
        p = self.probs(recs(5), None, auth={SHEFFIELD, BARNSLEY})
        self.assertIn(BARNSLEY, p[0])

    def test_supported_living_over_50pct_in_more_than_5_authorities(self):
        def build(sl_counts):
            out = []
            for i, n in enumerate(sl_counts):
                out += recs(40, lad=f"E06{i:06d}", sl=n, start=100 * i)
            return out
        auth = {f"E06{i:06d}" for i in range(8)}
        base = build([10] * 8)
        # five authorities 10 -> 16 (60%) and three unchanged: allowed
        self.assertEqual(self.probs(build([16] * 5 + [10] * 3), base,
                                    auth=auth), [])
        # six: stopped
        p = self.probs(build([16] * 6 + [10] * 2), base, auth=auth)
        self.assertIn("in 6 authorities", p[0])
        # exactly 50% is not over
        self.assertEqual(self.probs(build([15] * 8), base, auth=auth), [])
        # below 10 on both sides does not count
        small = build([4] * 8)
        self.assertEqual(self.probs(build([9] * 8), small, auth=auth), [])
        # dormant locations are not counted
        dorm = [dict(r, dormant=True) for r in build([30] * 8)]
        self.assertEqual(m.sl_counts(dorm), {})

    def test_zero_null_cells(self):
        old = [{"location_id": "a", "care_homes_beds": 0,
                "supported_living": False, "latitude": None}]
        new = [{"location_id": "a", "care_homes_beds": None,
                "supported_living": False, "latitude": None}]
        self.assertEqual(m.zero_null_cells(old, new),
                         [("a", "care_homes_beds", 0, None)])
        self.assertEqual(m.zero_null_cells(old, old), [])
        # False is not 0
        new2 = [{"location_id": "a", "care_homes_beds": 0,
                 "supported_living": None, "latitude": None}]
        self.assertEqual(m.zero_null_cells(old, new2), [])


class Plan(unittest.TestCase):
    TIPS = {"2026-08-04": "u8", "2026-09-01": "u9"}
    CHECKED = {"2026-08-04": {("u8", "s8")}, "2026-09-01": {("u9", "s9")}}

    def plan(self, as_at, url, sha, recheck=None, allow_older=False,
             stranded=()):
        return m.plan_load("2026-09-01", as_at, self.TIPS, self.CHECKED, url,
                           sha, recheck=recheck, allow_older=allow_older,
                           stranded=stranded)

    def test_before_download(self):
        self.assertEqual(self.plan(None, "u9", None)[0], "skip")
        self.assertEqual(self.plan(None, "u10", None)[0], "fetch")
        self.assertEqual(self.plan(None, "u9", None, recheck="2026-09-01")[0],
                         "fetch")
        self.assertEqual(self.plan(None, "u9", None,
                                   stranded=["2026-09-01"])[0], "fetch")

    def test_after_reading_the_file(self):
        self.assertEqual(self.plan("2026-10-01", "u10", "s10"), ("new", ""))
        self.assertEqual(self.plan("2026-09-01", "f", "s9")[0], "skip")
        self.assertEqual(self.plan("2026-09-01", "f", "sX")[0], "recheck")
        self.assertEqual(self.plan("2026-09-01", "f", "s9",
                                   recheck="2026-09-01")[0], "recheck")
        self.assertEqual(self.plan("2026-09-01", "f", "s9",
                                   stranded=["2026-09-01"])[0], "recheck")

    def test_older_file_halts_unless_allowed(self):
        for as_at in ("2026-07-01", "2026-08-04"):
            with self.assertRaises(ValueError) as e:
                self.plan(as_at, "f", "new-sha")
            self.assertIn("--allow-older-file", str(e.exception))
        kind, note = self.plan("2026-07-01", "f", "x", allow_older=True)
        self.assertEqual(kind, "new")
        self.assertIn("--allow-older-file given", note)
        self.assertEqual(self.plan("2026-08-04", "f", "x",
                                   recheck="2026-08-04"), ("recheck", ""))

    def test_recheck_must_name_the_file_and_a_held_snapshot(self):
        with self.assertRaises(ValueError):
            self.plan("2026-09-01", "f", "s", recheck="2026-08-04")
        with self.assertRaises(ValueError):
            self.plan("2026-10-01", "f", "s", recheck="2026-10-01")


class Misc(_Tmp):
    def test_content_sha256(self):
        p = self.dir / "x.bin"
        p.write_bytes(b"abc")
        self.assertEqual(m.content_sha256(p), "ba7816bf" "8f01cfea414140de5d"
                         "ae2223b00361a396177a9cb410ff61f20015ad")

    def test_spec_profile_and_column_lists(self):
        self.assertEqual(len(m.EXPECTED_HEADERS), 122)
        self.assertEqual(len(set(m.EXPECTED_HEADERS)), 122)
        self.assertEqual(len(m.VALUE_COLUMNS), 36)
        self.assertEqual(m.SPEC.key_cols, ("location_id",))
        self.assertEqual(m.SPEC.period_col, "snapshot_date")
        self.assertEqual(m.SPEC.refresh_from, (("source_file", "source_file"),))
        self.assertEqual(m.SPEC.as_loaded_source_col, "source_file")
        self.assertTrue(m.SPEC.refresh_source_whole_period)
        self.assertFalse(m.SPEC.fk_la_boundaries)
        self.assertIsNone(m.PROFILE.expected_areas)
        self.assertTrue(m.PROFILE.file_checks)
        self.assertEqual(m.PROFILE.savepoint, "s11_snapshot")
        self.assertEqual(m.PROFILE.run_agent, "Source 11 - CQC Care directory")
        self.assertEqual(geography.DATASET_FORM["11"][0], "none")
        self.assertIn("October 2026", geography.DATASET_FORM["11"][1])

    def test_view_sql_names_every_legacy_column(self):
        sql = m.view_sql("v", "t")
        for c in m.LEGACY_COLUMNS:
            self.assertIn(c, sql)
        self.assertIn("FROM public.t", sql)

    def test_help_says_no_sync_new(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            m.main(["--help"])
        self.assertIn("no sync-new", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            m.main(["sync-new"])


class OdsTextRules(unittest.TestCase):
    def test_repeated_spaces_collapse_to_one(self):
        from xml.etree import ElementTree as ET
        xml = ('<c xmlns:t="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
               'xmlns:x="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
               '<x:p>Cossham Gardens <x:s/>- Care Home</x:p></c>')
        self.assertEqual(m._ods_cell(ET.fromstring(xml)),
                         "Cossham Gardens - Care Home")

    def test_set_change_reason_names_the_mapping(self):
        msg = m.set_change_reason({"a", "b"}, {"a", "b", "c"}, {"c", "d"})
        self.assertIn("mapping changed, not the file", msg)
        self.assertNotIn("reissued", msg)

    def test_set_change_reason_keeps_reissue_message(self):
        self.assertEqual(m.set_change_reason({"a"}, {"a", "z"}, set()),
                         m.ENGINE_GAP)
        self.assertEqual(m.set_change_reason({"a", "b"}, {"a"}, {"b"}),
                         m.ENGINE_GAP)


if __name__ == "__main__":
    unittest.main()
