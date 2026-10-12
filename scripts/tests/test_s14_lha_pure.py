"""Pure tests for s14_lha_editions: discovery on the GOV.UK collection, the
England rates CSV reader, money and the derived weekly values, the BRMA
layer reader, the committed alias table, the crosswalk, ranks, ledger and
planning, and the stop conditions.

No database and no network: every CSV and GML file is written by the
tests, and the GOV.UK replies are stubbed. The fixture writers are reused
by test_s14_lha_loader. Nothing here imports or runs a retired script.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s14_lha_editions as m  # noqa: E402

FY = "2026-27"
YEARS = "2026 to 2027"
# fixture BRMAs and monthly rates (SAR, 1 to 4 bed); the names are real
# BRMA names, two of them in the VOA layer's "X & Y" form
RATES = {
    "Ashford": ("393.24", "750.00", "850.00", "1,097.80", "1,440.00"),
    "Greater Liverpool": ("344.38", "500.00", "575.00", "650.00", "795.00"),
    "Hull and East Riding": ("335.83", "380.00", "475.00", "550.00",
                             "700.00"),
    "Kernow West": ("392.50", "550.00", "695.00", "815.00", "1,100.00"),
    "Medway and Swale": ("400.00", "675.00", "800.00", "950.00",
                         "1,250.00"),
}
# fixture authorities: (lad24cd, lad24nm, longitude, latitude, BRMA polygon
# it falls in, or None for the one placed outside every polygon)
POINTS = [
    ("E06000010", "Kingston upon Hull, City of", -0.3274, 53.7676,
     "Hull & East Riding"),
    ("E07000105", "Ashford", 0.8741, 51.1465, "Ashford"),
    ("E08000012", "Liverpool", -2.9779, 53.4084, "Greater Liverpool"),
    ("E06000052", "Cornwall", -5.0527, 50.2660, "Kernow West"),
    ("E06000035", "Medway", 0.5605, 51.3880, "Medway & Swale"),
    ("E06000053", "Isles of Scilly", -6.3200, 49.9300, None),
]
OGR_LAYER = "English_BRMA_LHA__Layer"


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def money_cell(v) -> str:
    """The CSV form of a fixture amount: '£' and thousands separators, quoted
    by the csv writer when it carries a comma. '' and None are a blank cell,
    'n/a' is written as it is, 'RAW:<text>' writes <text> as it is."""
    if v is None or v == "":
        return ""
    if v == "n/a":
        return v
    if str(v).startswith("RAW:"):
        return str(v)[4:]
    return f"£{v}"


def write_csv(path, years=YEARS, rates=None, *, header=None, encoding="cp1252",
              raw=None, extra_lines=(), old_layout=False):
    """An England rates CSV as DWP publishes it (2026-27 layout: header row
    first, '£' amounts, CRLF). raw: bytes written as they are."""
    import csv
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
        return path
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    if old_layout:
        w.writerow(["", "Monthly UC LHA rates 24/25", "", "", "", ""])
        w.writerow(["BRMA", "SAR", "1 Bed", "2 bed", "3 bed", "4 Bed"])
    else:
        w.writerow(header or [
            "BRMA", f"Monthly UC LHA rates {years} - SAR", f"1 Bed {years}",
            f"2 Bed {years}", f"3 Bed {years}", f"4 Bed {years}"])
    for name, vals in (RATES if rates is None else rates).items():
        w.writerow([name] + [money_cell(v) for v in vals])
    for line in extra_lines:
        w.writerow(line)
    path.write_bytes(buf.getvalue().encode(encoding))
    return path


def _square(x, y, half=4000):
    return [(x - half, y - half), (x + half, y - half), (x + half, y + half),
            (x - half, y + half), (x - half, y - half)]


def to_bng(lon, lat):
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
    return t.transform(lon, lat)


def polygons_for(points=None):
    """[(VOA name, ring)]: a square around each fixture authority's point,
    the Isles of Scilly left outside (its nearest is Kernow West)."""
    out = []
    for _, _, lon, lat, brma in (POINTS if points is None else points):
        if brma is None:
            continue
        x, y = to_bng(lon, lat)
        out.append((brma, _square(x, y)))
    return out


GML_HEAD = ('<?xml version="1.0" encoding="utf-8" ?>\n'
            '<ogr:FeatureCollection\n'
            '     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '     xsi:schemaLocation="http://ogr.maptools.org/ English '
            'BRMA(LHA) Layer.xsd"\n'
            '     xmlns:ogr="http://ogr.maptools.org/"\n'
            '     xmlns:gml="http://www.opengis.net/gml">\n')
XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema targetNamespace="http://ogr.maptools.org/" xmlns:ogr="http://ogr.maptools.org/" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:gml="http://www.opengis.net/gml" elementFormDefault="qualified" version="1.0">
<xs:import namespace="http://www.opengis.net/gml" schemaLocation="http://schemas.opengis.net/gml/2.1.2/feature.xsd"/>
<xs:element name="FeatureCollection" type="ogr:FeatureCollectionType" substitutionGroup="gml:_FeatureCollection"/>
<xs:complexType name="FeatureCollectionType">
  <xs:complexContent>
    <xs:extension base="gml:AbstractFeatureCollectionType">
      <xs:attribute name="lockId" type="xs:string" use="optional"/>
      <xs:attribute name="scope" type="xs:string" use="optional"/>
    </xs:extension>
  </xs:complexContent>
</xs:complexType>
<xs:element name="{layer}" type="ogr:{layer}_Type" substitutionGroup="gml:_Feature"/>
<xs:complexType name="{layer}_Type">
  <xs:complexContent>
    <xs:extension base="gml:AbstractFeatureType">
      <xs:sequence>
        <xs:element name="geometryProperty" type="gml:PolygonPropertyType" nillable="true" minOccurs="0" maxOccurs="1"/>
        <xs:element name="{field}" nillable="true" minOccurs="0" maxOccurs="1">
          <xs:simpleType><xs:restriction base="xs:string"><xs:maxLength value="100"/></xs:restriction></xs:simpleType>
        </xs:element>
      </xs:sequence>
    </xs:extension>
  </xs:complexContent>
</xs:complexType>
</xs:schema>
"""


def write_gml(path, polygons=None, *, layer=OGR_LAYER, field="Name",
              member="English BRMA(LHA) Layer.gml", srs="EPSG:27700",
              extra_members=()):
    """A zip holding an OGR GML 2 layer like the VOA's: one polygon per
    BRMA, each with srsName, the layer CRS absent (no .prj)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    feats = []
    for i, (name, ring) in enumerate(polygons_for() if polygons is None
                                     else polygons):
        coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in ring)
        srs_attr = f' srsName="{srs}"' if srs else ""
        feats.append(
            f'  <gml:featureMember>\n    <ogr:{layer} fid="{layer}.{i}">\n'
            f'      <ogr:geometryProperty><gml:Polygon{srs_attr}>'
            '<gml:outerBoundaryIs><gml:LinearRing><gml:coordinates>'
            f'{coords}</gml:coordinates></gml:LinearRing>'
            '</gml:outerBoundaryIs></gml:Polygon></ogr:geometryProperty>\n'
            f'      <ogr:{field}>{escape(name)}</ogr:{field}>\n'
            f'    </ogr:{layer}>\n  </gml:featureMember>\n')
    body = GML_HEAD + "".join(feats) + "</ogr:FeatureCollection>\n"
    stem = member[:-4] if member.endswith(".gml") else member
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(member, body.encode("utf-8"))
        z.writestr(stem + ".xsd", XSD.format(layer=layer, field=field))
        for name, data in extra_members:
            z.writestr(name, data)
    return path


def boundaries(points=None):
    return [{"lad24cd": c, "lad24nm": n, "longitude": Decimal(str(lo)),
             "latitude": Decimal(str(la))}
            for c, n, lo, la, _ in (POINTS if points is None else points)]


ALIASES = {"Hull & East Riding": "Hull and East Riding",
           "Medway & Swale": "Medway and Swale"}

COLL_DOCS = [("2026 to 2027", "2026-04-08T15:31:30Z"),
             ("2025 to 2026", "2025-02-03T15:17:04Z"),
             ("2014 to 2019", "2019-06-20T12:55:00Z")]


def collection(docs=None, title=None):
    docs = COLL_DOCS if docs is None else docs
    return {"title": title or m.COLLECTION_TITLE,
            "base_path": m.COLLECTION_PATH,
            "links": {"documents": [
                {"title": f"Universal Credit Local Housing Allowance rates: "
                          f"{y}" if not y.startswith("!") else y[1:],
                 "base_path": "/government/publications/universal-credit-"
                              "local-housing-allowance-rates-"
                              + y.replace(" ", "-").lstrip("!"),
                 "public_updated_at": ts} for y, ts in docs]}}


def page(years=YEARS, *, attachments=None, history=None, title=None):
    fn = years.replace(" ", "-")
    return {
        "title": title or f"Universal Credit Local Housing Allowance rates: "
                          f"{years}",
        "base_path": "/government/publications/universal-credit-local-"
                     f"housing-allowance-rates-{fn}",
        "public_updated_at": "2026-04-08T16:31:30+01:00",
        "details": {
            "attachments": attachments if attachments is not None else [
                {"title": "England: Universal Credit Local Housing Allowance "
                          f"monthly rates {years}",
                 "url": f"https://assets.publishing.service.gov.uk/media/"
                        f"abc123/england-rates-{fn}.csv",
                 "content_type": "text/csv"},
                {"title": "Scotland: Universal Credit Local Housing "
                          f"Allowance monthly rates {years}",
                 "url": f"https://assets.publishing.service.gov.uk/media/"
                        f"abc124/scotland-rates-{fn}.csv",
                 "content_type": "text/csv"}],
            "change_history": history if history is not None else [
                {"note": "Corrected date error", "public_timestamp":
                 "2026-04-08T15:31:30Z"},
                {"note": "First published.", "public_timestamp":
                 "2026-01-30T09:30:00Z"}]}}


class _Resp:
    def __init__(self, body, url):
        self.content, self.url = body, url
        self.text = body.decode("utf-8", "replace")

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, body, final):
        self.body, self.final = body, final

    def get(self, url, **kw):
        return _Resp(self.body, self.final)


class Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.n = 0

    def path(self, name="f.csv"):
        self.n += 1
        return self.root / f"t{self.n}" / name

    def csv(self, **kw):
        return write_csv(self.path(), **kw)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class Discovery(unittest.TestCase):

    def test_the_newest_year_page_is_chosen(self):
        fy, bp = m.latest_year_page(collection())
        self.assertEqual(fy, FY)
        self.assertTrue(bp.endswith("2026-to-2027"))
        pages = m.year_pages(collection())
        self.assertEqual(sorted(pages), ["2025-26", "2026-27"])
        self.assertEqual(pages.ignored, ["Universal Credit Local Housing "
                                         "Allowance rates: 2014 to 2019"])

    def test_none_halts_listing_titles(self):
        with self.assertRaises(ValueError) as e:
            m.latest_year_page(collection([("2014 to 2019", "x")]))
        self.assertIn("2014 to 2019", str(e.exception))

    def test_two_pages_for_one_year_halt(self):
        with self.assertRaises(ValueError) as e:
            m.latest_year_page(collection(COLL_DOCS + [("2026 to 2027",
                                                        "x")]))
        self.assertIn("2026-27", str(e.exception))

    def test_a_series_title_with_a_newer_year_that_does_not_fit_halts(self):
        docs = COLL_DOCS + [("!Universal Credit Local Housing Allowance "
                             "rates 2027 to 2028 (provisional)", "x")]
        with self.assertRaises(ValueError) as e:
            m.latest_year_page(collection(docs))
        self.assertIn("2027 to 2028", str(e.exception))

    def test_a_retitled_collection_halts(self):
        with self.assertRaises(ValueError) as e:
            m.latest_year_page(collection(title="LHA rates"))
        self.assertIn("LHA rates", str(e.exception))

    def test_year_page_for_a_release(self):
        self.assertEqual(m.year_page_for(collection(), "2025-26")[0],
                         "2025-26")
        with self.assertRaises(ValueError):
            m.year_page_for(collection(), "2024-25")

    def test_england_attachment_exactly_one_csv(self):
        a = m.england_attachment(page())
        self.assertEqual(a["fy"], FY)
        self.assertTrue(a["url"].endswith("england-rates-2026-to-2027.csv"))
        p = page()
        p["details"]["attachments"].append(dict(
            p["details"]["attachments"][0]))
        with self.assertRaises(ValueError) as e:
            m.england_attachment(p)
        self.assertIn("2 attachments", str(e.exception))
        p = page(attachments=[])
        with self.assertRaises(ValueError) as e:
            m.england_attachment(p)
        self.assertIn("0 attachments", str(e.exception))
        p = page()
        p["details"]["attachments"][0]["url"] = "https://x/england.xlsx"
        with self.assertRaises(ValueError) as e:
            m.england_attachment(p)
        self.assertIn(".csv", str(e.exception))
        p = page()
        p["details"]["attachments"][0]["title"] = (
            "England: Universal Credit Local Housing Allowance monthly rates "
            "2025 to 2026")
        with self.assertRaises(ValueError) as e:
            m.england_attachment(p)
        self.assertIn("2025-26", str(e.exception))

    def test_change_history_and_page_rank(self):
        hist = m.change_history(page())
        self.assertEqual(hist[0][0], datetime(2026, 4, 8, 15, 31, 30,
                                              tzinfo=timezone.utc))
        self.assertEqual(m.page_rank_ts(page()), hist[0][0])
        with self.assertRaises(ValueError):
            m.page_rank_ts(page(history=[]))
        self.assertEqual(m.page_year(page()), FY)

    def test_discovery_note_names_the_newest_and_warns_when_overdue(self):
        lines = m.discovery_note(collection(), ["2026-27"],
                                 today=date(2027, 2, 28))
        self.assertIn("2026-27", lines[0])
        self.assertFalse(any("WARNING" in x for x in lines))
        lines = m.discovery_note(collection(), ["2026-27"],
                                 today=date(2027, 3, 1))
        self.assertTrue(any("WARNING" in x and "2027-28" in x
                            for x in lines))
        self.assertEqual(m.expected_next_release("2026-27"),
                         ("2027-28", date(2027, 2, 28)))
        # a page newer than the newest held year is named
        lines = m.discovery_note(collection([("2027 to 2028", "x")]
                                            + COLL_DOCS), ["2026-27"],
                                 today=date(2027, 2, 1))
        self.assertTrue(any("NEW RELEASE" in x and "2027-28" in x
                            for x in lines))

    def test_fy_label(self):
        self.assertEqual(m.fy_label("2026 to 2027"), "2026-27")
        with self.assertRaises(ValueError):
            m.fy_label("2014 to 2019")
        self.assertEqual(m.fy_text("2026-27"), "2026 to 2027")


# ---------------------------------------------------------------------------
# The CSV
# ---------------------------------------------------------------------------

class Csv(Tmp):

    def test_the_2026_27_layout_cp1252_pound(self):
        f = m.read_csv(self.csv())
        self.assertEqual(f["encoding"], "cp1252")
        self.assertEqual(f["year"], FY)
        self.assertEqual(f["names"], sorted(RATES))
        recs = m.build_rates(f, FY)
        ash = [r for r in recs if r["brma_name"] == "Ashford"][0]
        self.assertEqual(ash["three_bed_monthly"], Decimal("1097.80"))
        self.assertEqual(ash["three_bed_weekly"], Decimal("253.34"))
        self.assertEqual(ash["sar_weekly"], Decimal("90.75"))
        self.assertEqual(ash["financial_year"], FY)

    def test_utf8_pound_read(self):
        f = m.read_csv(self.csv(encoding="utf-8"))
        self.assertEqual(f["encoding"], "utf-8")
        self.assertEqual(len(m.build_rates(f, FY)), len(RATES))

    def test_an_undecodable_byte_halts(self):
        p = self.csv()
        p.write_bytes(p.read_bytes().replace(b"Ashford", b"Ash\x81ord"))
        with self.assertRaises(ValueError) as e:
            m.read_csv(p)
        self.assertIn("cp1252", str(e.exception))

    def test_the_older_layout_halts_naming_what_it_saw(self):
        with self.assertRaises(ValueError) as e:
            m.read_csv(self.csv(old_layout=True))
        self.assertIn("24/25", str(e.exception))
        self.assertIn("older layout", str(e.exception))

    def test_a_changed_header_halts(self):
        with self.assertRaises(ValueError) as e:
            m.read_csv(self.csv(header=["BRMA", "SAR 2026 to 2027",
                                        "1 Bed 2026 to 2027",
                                        "2 Bed 2026 to 2027",
                                        "3 Bed 2026 to 2027",
                                        "4 Bed 2026 to 2027"]))
        self.assertIn("SAR 2026 to 2027", str(e.exception))

    def test_an_ampersand_name_halts(self):
        rates = dict(RATES)
        rates["Medway & Swale"] = rates.pop("Medway and Swale")
        with self.assertRaises(ValueError) as e:
            m.read_csv(self.csv(rates=rates))
        self.assertIn("Medway & Swale", str(e.exception))

    def test_a_header_year_differing_from_the_page(self):
        y = "2024 to 2025"
        f = m.read_csv(self.csv(header=[
            "BRMA", f"Monthly UC LHA rates {y} - SAR", f"1 Bed {YEARS}",
            f"2 Bed {YEARS}", f"3 Bed {YEARS}", f"4 Bed {YEARS}"]))
        self.assertIsNone(f["year"])
        self.assertEqual(m.header_year_problems(f, FY), [
            f"header cell 'Monthly UC LHA rates {y} - SAR' names 2024-25, "
            f"not {FY}"])
        f = m.read_csv(self.csv())
        self.assertEqual(m.header_year_problems(f, FY), [])
        self.assertTrue(m.header_year_problems(f, "2025-26"))

    def test_bad_amounts_halt_and_no_row_is_skipped(self):
        for bad in ("", "n/a", "RAW:0.00", "RAW:-5.00", "RAW:393.245",
                    "RAW:£0", "RAW:abc"):
            with self.subTest(bad=bad):
                rates = dict(RATES)
                rates["Kernow West"] = ("392.50", bad, "695.00", "815.00",
                                        "1,100.00")
                f = m.read_csv(self.csv(rates=rates))
                with self.assertRaises(ValueError) as e:
                    m.build_rates(f, FY)
                self.assertIn("Kernow West", str(e.exception))

    def test_short_rows_blank_names_and_repeats_halt(self):
        for extra, needle in (([["Bath", "£1.00"]], "Bath"),
                              ([["", "£1", "£1", "£1", "£1", "£1"]], "blank"),
                              ([["Ashford", "£1", "£1", "£1", "£1", "£1"]],
                               "twice")):
            with self.subTest(needle=needle):
                with self.assertRaises(ValueError) as e:
                    m.read_csv(self.csv(extra_lines=extra))
                self.assertIn(needle, str(e.exception))

    def test_trailing_empty_rows_allowed_data_after_them_halts(self):
        f = m.read_csv(self.csv(extra_lines=[[], []]))
        self.assertEqual(len(f["rows"]), len(RATES))
        with self.assertRaises(ValueError):
            m.read_csv(self.csv(extra_lines=[[], ["Bath", "£1", "£1", "£1",
                                                  "£1", "£1"]]))


class Money(unittest.TestCase):

    def test_money(self):
        self.assertEqual(m.money("£1,097.80", "x"), Decimal("1097.80"))
        self.assertEqual(m.money("£750", "x"), Decimal("750.00"))
        self.assertEqual(m.money(" £393.2 ", "x"), Decimal("393.20"))
        self.assertEqual(str(m.money("£750", "x")), "750.00")
        for bad in ("", " ", None, "n/a", "£0.00", "0", "-5", "£-5.00",
                    "393.245", "£1,09.80", "1.2.3", "£"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as e:
                    m.money(bad, "row 9 SAR")
                self.assertIn("row 9 SAR", str(e.exception))

    def test_weekly_is_decimal_half_up(self):
        self.assertEqual(m.weekly(Decimal("1097.80")), Decimal("253.34"))
        self.assertEqual(m.weekly(Decimal("335.83")), Decimal("77.50"))
        # 0.195 x 12 / 52 = 0.045 exactly: half-up gives 0.05, float rounding
        # 0.04 (and half-even 0.04): the loader uses Decimal half-up
        self.assertEqual(m.weekly(Decimal("0.195")), Decimal("0.05"))
        self.assertEqual(round(0.195 * 12 / 52, 2), 0.04)

    def test_no_two_place_amount_meets_a_half_cent(self):
        # a monthly amount of c pence gives 3c/13 pence weekly, never an
        # exact half penny, so for every amount money() accepts, Decimal
        # half-up and float rounding agree (the held weekly values stand)
        for c in range(1, 300001):
            mo = Decimal(c) / 100
            self.assertEqual(m.weekly(mo),
                             Decimal(str(round(c * 12 / 52 / 100, 2)))
                             .quantize(Decimal("0.01")), c)


# ---------------------------------------------------------------------------
# The BRMA layer, aliases and the crosswalk
# ---------------------------------------------------------------------------

class Gml(Tmp):

    def gml(self, **kw):
        return write_gml(self.path("GML.zip"), **kw)

    def test_the_layer_is_read_and_the_crs_set(self):
        with unittest.mock.patch.object(m, "EXPECTED_POLYGONS", 5):
            g = m.read_gml(self.gml())
        self.assertEqual(g["layer"], OGR_LAYER)
        self.assertEqual(g["member"], "English BRMA(LHA) Layer.gml")
        self.assertEqual(sorted(g["names"]), sorted(
            b for *_, b in POINTS if b))
        self.assertEqual(str(g["gdf"].crs).upper(), "EPSG:27700")
        self.assertIn(g["crs_read"], (None, "EPSG:27700"))

    def test_identity_halts(self):
        cases = ({"layer": "Other_Layer"}, {"field": "BRMA"},
                 {"member": "Other.gml"}, {"srs": "EPSG:4326"})
        for kw in cases:
            with self.subTest(kw=kw), \
                    unittest.mock.patch.object(m, "EXPECTED_POLYGONS", 5):
                with self.assertRaises(ValueError):
                    m.read_gml(self.gml(**kw))
        with unittest.mock.patch.object(m, "EXPECTED_POLYGONS", 152):
            with self.assertRaises(ValueError) as e:
                m.read_gml(self.gml())
            self.assertIn("152", str(e.exception))


class Aliases(unittest.TestCase):

    def test_the_committed_table_has_its_18_exact_pairs(self):
        a = m.brma_aliases()
        self.assertEqual(len(a), 18)
        for k, v in a.items():
            self.assertIn(" & ", k)
            self.assertNotIn("&", v)
        self.assertEqual(a["Hull & East Riding"], "Hull and East Riding")
        self.assertEqual(a["Mid & East Devon"], "Mid and East Devon")

    def test_convert_exactly_and_an_unknown_ampersand_halts(self):
        a = m.brma_aliases()
        self.assertEqual(m.convert_name("Hull & East Riding", a),
                         "Hull and East Riding")
        self.assertEqual(m.convert_name("Greater Liverpool", a),
                         "Greater Liverpool")
        for bad in ("Hull &  East Riding", "Hull&East Riding",
                    "Bath & Wells"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as e:
                    m.convert_name(bad, a)
                self.assertIn(bad, str(e.exception))

    def test_a_malformed_table_halts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "n.json"
            for aliases in ({"A and B": {"name": "A and B", "seen": "x"}},
                            {"A & B": {"name": "A & B", "seen": "x"}},
                            {"A & B": {"name": "A and B"}}):
                p.write_text(json.dumps({"aliases": aliases}),
                             encoding="utf-8")
                with self.subTest(aliases=aliases):
                    with self.assertRaises(ValueError):
                        m.brma_aliases(p)


class Mapping(Tmp):

    def setUp(self):
        super().setUp()
        with unittest.mock.patch.object(m, "EXPECTED_POLYGONS", 5):
            self.g = m.read_gml(write_gml(self.path("GML.zip")))

    def test_centroid_within_else_nearest(self):
        recs = m.build_mapping(self.g, boundaries(), ALIASES, set(RATES))
        by = {r["lad24cd"]: r for r in recs}
        self.assertEqual(by["E06000010"]["brma_name"], "Hull and East Riding")
        self.assertEqual(by["E06000010"]["mapping_method"],
                         "centroid_spatial_join")
        self.assertEqual(by["E06000035"]["brma_name"], "Medway and Swale")
        self.assertEqual(by["E06000053"]["brma_name"], "Kernow West")
        self.assertEqual(by["E06000053"]["mapping_method"],
                         "nearest_fallback")
        self.assertEqual(sum(r["mapping_method"] == "nearest_fallback"
                             for r in recs), 1)
        self.assertEqual(by["E08000012"]["la_name"], "Liverpool")
        self.assertEqual(by["E08000012"]["vintage"], m.LIVE_VINTAGE)

    def test_a_mapped_name_with_no_rate_row_halts(self):
        names = set(RATES) - {"Kernow West"}
        with self.assertRaises(ValueError) as e:
            m.build_mapping(self.g, boundaries(), ALIASES, names)
        self.assertIn("Kernow West", str(e.exception))
        self.assertIn("E06000052", str(e.exception))

    def test_an_ampersand_name_not_in_the_table_halts(self):
        with self.assertRaises(ValueError) as e:
            m.build_mapping(self.g, boundaries(),
                            {"Hull & East Riding": "Hull and East Riding"},
                            set(RATES))
        self.assertIn("Medway & Swale", str(e.exception))

    def test_a_missing_coordinate_halts(self):
        b = boundaries()
        b[0]["longitude"] = None
        with self.assertRaises(ValueError) as e:
            m.build_mapping(self.g, b, ALIASES, set(RATES))
        self.assertIn("E06000010", str(e.exception))

    def test_overlapping_polygons_halt(self):
        polys = polygons_for()
        polys.append(("Peaks & Dales", polys[0][1]))
        with unittest.mock.patch.object(m, "EXPECTED_POLYGONS", 6):
            g = m.read_gml(write_gml(self.path("GML.zip"), polys))
        with self.assertRaises(ValueError) as e:
            m.build_mapping(g, boundaries(), dict(ALIASES, **{
                "Peaks & Dales": "Peaks and Dales"}),
                set(RATES) | {"Peaks and Dales"})
        self.assertIn("E06000010", str(e.exception))


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

TS = datetime(2026, 4, 8, 15, 31, 30, tzinfo=timezone.utc)
TS0 = datetime(2026, 1, 30, 9, 30, tzinfo=timezone.utc)


class Ranks(unittest.TestCase):

    def test_rank_from_the_header_year_and_change_history_never_the_url(self):
        src = m.edition_source("england-rates-2026-to-2027.csv",
                               (FY, TS), "https://assets/media/1/x.csv")
        self.assertEqual(m.release_rank(src), (FY, TS))
        self.assertEqual(m.release_rank(
            "https://assets.publishing.service.gov.uk/media/69d654a2e1430e837"
            "a86f64a/england-rates-2026-to-2027.csv"), None)
        self.assertEqual(m.release_rank(m.edition_source("x.csv", (FY, None),
                                                         "data/raw/x.csv")),
                         (FY, None))

    def test_compare_rank(self):
        self.assertEqual(m.compare_rank((FY, TS0), (FY, TS)), "older")
        self.assertEqual(m.compare_rank((FY, TS), (FY, TS0)), "newer")
        self.assertEqual(m.compare_rank((FY, TS), (FY, TS)), "equal")
        self.assertEqual(m.compare_rank((FY, None), (FY, TS)), "unknown")
        self.assertEqual(m.compare_rank(("2025-26", TS), (FY, TS0)), "older")
        self.assertEqual(m.compare_rank((FY, None), ("2025-26", TS)),
                         "newer")
        self.assertEqual(m.compare_rank((FY, TS), None), "unknown")

    def test_ledger_source_round_trip_and_complete(self):
        s = m.ledger_source("https://a/x.csv", (FY, TS))
        self.assertEqual(m.parse_ledger_source(s), ("https://a/x.csv",
                                                    (FY, TS)))
        checked = {FY: {(s, "abc")}}
        self.assertTrue(m.ledger_complete(checked, "https://a/x.csv", "abc",
                                          FY))
        self.assertFalse(m.ledger_complete(checked, "https://a/x.csv",
                                           "abd", FY))
        self.assertFalse(m.ledger_complete(checked, "data/raw/x.csv", "abc",
                                           FY))
        self.assertFalse(m.ledger_complete(checked, "https://a/x.csv", "abc",
                                           FY, stranded=[FY]))

    def test_plan_new_revised_and_skipped(self):
        ranks = {FY: (FY, TS)}
        new, rev, sk = m.plan_periods(FY, (FY, TS), ranks, {}, "u", "s",
                                      recheck=None, allow_older=False)
        self.assertEqual((new, rev), ([], [FY]))
        new, rev, sk = m.plan_periods(FY, (FY, TS0), ranks, {}, "u", "s",
                                      recheck=None, allow_older=False)
        self.assertTrue(sk[FY].startswith("older"))
        new, rev, sk = m.plan_periods(FY, (FY, TS0), ranks, {}, "u", "s",
                                      recheck=None, allow_older=True)
        self.assertEqual(rev, [FY])
        new, rev, sk = m.plan_periods("2025-26", ("2025-26", TS), ranks, {},
                                      "u", "s", recheck=None,
                                      allow_older=False)
        self.assertTrue(sk["2025-26"].startswith("older"))
        new, rev, sk = m.plan_periods("2027-28", ("2027-28", None), ranks,
                                      {}, "u", "s", recheck=None,
                                      allow_older=False)
        self.assertEqual(new, ["2027-28"])
        src = m.ledger_source("u", (FY, TS))
        new, rev, sk = m.plan_periods(FY, (FY, TS), ranks, {FY: {(src, "s")}},
                                      src, "s", recheck=None,
                                      allow_older=False)
        self.assertTrue(sk[FY].startswith("checked"))
        new, rev, sk = m.plan_periods(FY, (FY, TS), ranks, {FY: {(src, "s")}},
                                      src, "s", recheck=FY, allow_older=False)
        self.assertEqual(rev, [FY])
        with self.assertRaises(ValueError):
            m.plan_periods(FY, (FY, TS), ranks, {}, "u", "s",
                           recheck="2025-26", allow_older=False)

    def test_tip_ranks_take_the_newest_file_seen(self):
        tips = {FY: {"rank": (FY, TS0)}}
        checked = {FY: {(m.ledger_source("u", (FY, TS)), "s")}}
        self.assertEqual(m.tip_ranks(tips, checked), {FY: (FY, TS)})


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

def recs(scale=None, names=None, year=FY):
    """Rate records: every monthly value 400 (or scale[name])."""
    out = []
    for n in (names or sorted(RATES)):
        v = Decimal(str((scale or {}).get(n, 400))).quantize(Decimal("0.01"))
        r = {"brma_name": n, "financial_year": year}
        for c in m.MONTHLY:
            r[c] = v
        for c, w in zip(m.MONTHLY, m.WEEKLY):
            r[w] = m.weekly(v)
        out.append(r)
    return out


class Stops(unittest.TestCase):

    def test_new_year_frozen_rates_are_normal(self):
        self.assertEqual(m.period_problems(recs(), None, recs(year="2025-26"),
                                           kind="new"), [])
        self.assertEqual(m.period_problems(recs(), None, None, kind="new"),
                         [])

    def test_new_year_authority_limit_both_sides(self):
        prev = recs(year="2025-26")
        ok = recs({"Ashford": 500})              # +25.00%: at the limit
        self.assertEqual(m.period_problems(ok, None, prev, kind="new"), [])
        bad = recs({"Ashford": "500.04"})        # +25.01%
        p = m.period_problems(bad, None, prev, kind="new")
        self.assertEqual(len(p), 1)
        self.assertIn("Ashford", p[0])
        self.assertFalse(p[0].startswith(m.KEYS))

    def test_new_year_median_limit_both_sides(self):
        prev = recs(year="2025-26")
        names = sorted(RATES)
        up = {n: 440 for n in names}             # +10.00% everywhere
        self.assertEqual(m.period_problems(recs(up), None, prev, kind="new"),
                         [])
        up = {n: "440.04" for n in names}        # +10.01%
        p = m.period_problems(recs(up), None, prev, kind="new")
        self.assertTrue(any("median" in x for x in p))

    def test_a_reissue_changing_any_value_needs_acknowledge(self):
        tip = recs()
        self.assertEqual(m.period_problems(recs(), tip, None, kind="revised"),
                         [])
        p = m.period_problems(recs({"Ashford": "400.01"}), tip, None,
                              kind="revised")
        self.assertEqual(len(p), 1)
        self.assertIn("Ashford", p[0])
        self.assertFalse(p[0].startswith(m.KEYS))

    def test_a_brma_set_differing_from_the_tip_is_key_changes(self):
        tip = recs()
        fewer = recs(names=sorted(RATES)[1:])
        for kind, t, prev in (("revised", tip, None), ("new", None, tip)):
            with self.subTest(kind=kind):
                p = m.period_problems(fewer, t, prev, kind=kind)
                self.assertTrue(any(x.startswith(m.KEYS) for x in p), p)

    def test_mapping_problems(self):
        a = [{"lad24cd": "E1", "la_name": "A", "brma_name": "X",
              "mapping_method": "NaN"}]
        b = [dict(a[0], mapping_method="centroid_spatial_join")]
        self.assertEqual(m.mapping_problems(a, a), [])
        p = m.mapping_problems(b, a)
        self.assertEqual(len(p), 1)
        self.assertIn("mapping_method", p[0])
        p = m.mapping_problems([], a)
        self.assertTrue(p[0].startswith(m.KEYS))

    def test_named_correction_matches_only_its_change(self):
        a = [{"lad24cd": f"E{i}", "la_name": "A", "brma_name": "X",
              "mapping_method": "NaN"} for i in range(3)]
        b = [dict(r, mapping_method="centroid_spatial_join") for r in a]
        corr = dict(m.MAPPING_CORRECTIONS["s14-mapping-method-2026-10"],
                    count=3)
        with unittest.mock.patch.dict(m.MAPPING_CORRECTIONS, {
                "s14-mapping-method-2026-10": corr}):
            self.assertEqual(m.named_correction(b, a),
                             "s14-mapping-method-2026-10")
            b[0]["brma_name"] = "Y"
            self.assertIsNone(m.named_correction(b, a))

    def test_contents_shas(self):
        r = recs()
        self.assertEqual(m.rows_content_sha(r),
                         m.rows_content_sha(list(reversed(r))))
        r2 = [dict(x, source_text="other") for x in r]
        self.assertEqual(m.rows_content_sha(r), m.rows_content_sha(r2))
        r2[0]["sar_weekly"] = Decimal("1.00")
        self.assertNotEqual(m.rows_content_sha(r), m.rows_content_sha(r2))


class Download(Tmp):

    def test_same_name_different_content_is_kept_beside(self):
        held = self.csv()
        new = self.csv(rates=dict(RATES, **{"Ashford": ("1.00",) * 5}))
        raw = self.root / "raw"
        raw.mkdir()
        (raw / "england-rates-2026-to-2027.csv").write_bytes(
            held.read_bytes())
        url = "https://a/media/1/england-rates-2026-to-2027.csv"
        with quiet() as out:
            path, final = m.fetch(url, raw, session=_Session(new.read_bytes(),
                                                             url))
        self.assertEqual(final, url)
        self.assertEqual((raw / "england-rates-2026-to-2027.csv")
                         .read_bytes(), held.read_bytes())
        self.assertNotEqual(path.name, "england-rates-2026-to-2027.csv")
        self.assertTrue(path.name.startswith("england-rates-2026-to-2027-"))
        self.assertEqual(path.read_bytes(), new.read_bytes())
        self.assertIn("kept it untouched", out.getvalue())
        # the same bytes again: kept as it is, read from the same name
        with quiet():
            p2, _ = m.fetch(url, raw, session=_Session(held.read_bytes(),
                                                       url))
        self.assertEqual(p2.name, "england-rates-2026-to-2027.csv")

    def test_a_redirect_records_the_final_url(self):
        body = self.csv().read_bytes()
        final = "https://b/media/2/england-rates-2026-to-2027.csv"
        with quiet():
            path, got = m.fetch("https://a/x", self.root / "raw",
                                session=_Session(body, final))
        self.assertEqual(got, final)
        self.assertEqual(path.name, "england-rates-2026-to-2027.csv")

    def test_not_a_rates_csv_halts(self):
        with self.assertRaises(SystemExit), quiet():
            m.fetch("https://a/x.csv", self.root / "raw",
                    session=_Session(b"<html>no</html>", "https://a/x.csv"))
        with self.assertRaises(SystemExit), quiet():
            m.fetch("https://a/x.xlsx", self.root / "raw",
                    session=_Session(self.csv().read_bytes(),
                                     "https://a/x.xlsx"))


if __name__ == "__main__":
    unittest.main()
