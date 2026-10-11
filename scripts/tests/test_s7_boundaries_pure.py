"""Pure tests for s7_boundaries_editions: the service list, the layer
identity, the query URL, the GeoJSON reader, the canonical geometry text,
records and content hash, ranks, ledger and planning, vintage differences,
the stop conditions, the download rules and the module's own write paths.

No database and no network: every JSON and GeoJSON is written by the
tests, and the ArcGIS replies and downloads are stubbed. The fixture
writers are reused by test_s7_boundaries_loader.
"""
import ast
import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s7_boundaries_editions as m  # noqa: E402

# 2024-07-02 13:28:58.757 UTC: the May 2024 BGC layer's lastEditDate
LAST_MS = 1719926938757
RANK = datetime(2024, 7, 2, 13, 28, 58, 757000, tzinfo=timezone.utc)
V24, V25, V26 = "May_2024", "DEC_2025", "May_2026"
P24, P25, P26 = "2024-05-01", "2025-12-01", "2026-05-01"
# the fixture's authorities (May 2024 codes), and the later vintages' codes
CODES = ["E06000001", "E06000002", "E08000016", "E08000019", "E09000033"]
CODES_NEW = ["E06000001", "E06000002", "E08000038", "E08000039", "E09000033"]
NAMES = {"E06000001": "Hartlepool", "E06000002": "Middlesbrough",
         "E08000016": "Barnsley", "E08000019": "Sheffield",
         "E08000038": "Barnsley", "E08000039": "Sheffield",
         "E09000033": "Westminster", "W06000001": "Isle of Anglesey",
         "S12000033": "Aberdeen City", "N09000001": "Antrim and Newtownabbey"}
SHA_GEOJSON_HELD_BYTES = ("7fd7632b1e00a2c0b83f138f5be4f2d5"
                          "94de10d524da02178ea8770cccd5385c")


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def vinfo(vintage):
    return m.VINTAGES[vintage]


def service_name(vintage):
    return f"Local_Authority_Districts_{vintage}_Boundaries_UK_BGC"


def services_json(names=None):
    """An ArcGIS service list: each (name, type); default the three
    declared vintages' FeatureServers and MapServers, an older vintage
    and an unrelated service."""
    if names is None:
        names = []
        for v in (V24, V25, V26):
            names += [(service_name(v), "FeatureServer"),
                      (service_name(v), "MapServer")]
        names += [("Local_Authority_Districts_December_2024_Boundaries_UK_BGC",
                   "FeatureServer"),
                  ("Wards_May_2024_Boundaries_UK_BGC", "FeatureServer")]
    base = "https://services1.arcgis.com/ESMARspQHYMw9BZ9/ArcGIS/rest/services"
    return {"currentVersion": 12, "services": [
        {"name": n, "type": t, "url": f"{base}/{n}/{t}"} for n, t in names]}


def layer_json(vintage=V24, *, last=LAST_MS, name=None, fields=None,
               drop=(), types=None, geometry="esriGeometryPolygon"):
    """An ArcGIS layer definition (FeatureServer/0?f=json) as the publisher
    writes it for an LAD BGC vintage."""
    yy = vinfo(vintage)["yy"]
    flds = fields or [
        ("FID", "esriFieldTypeOID"), (f"LAD{yy}CD", "esriFieldTypeString"),
        (f"LAD{yy}NM", "esriFieldTypeString"),
        (f"LAD{yy}NMW", "esriFieldTypeString"),
        ("BNG_E", "esriFieldTypeInteger"), ("BNG_N", "esriFieldTypeInteger"),
        ("LONG", "esriFieldTypeSingle"), ("LAT", "esriFieldTypeSingle"),
        ("Shape__Area", "esriFieldTypeDouble"),
        ("Shape__Length", "esriFieldTypeDouble"),
        ("GlobalID", "esriFieldTypeGlobalID")]
    flds = [(n, (types or {}).get(n, t)) for n, t in flds if n not in drop]
    info = ({} if last is None else
            {"lastEditDate": last, "schemaLastEditDate": last,
             "dataLastEditDate": last if isinstance(last, str)
             else last - 1000})
    return {"currentVersion": 12, "id": 0,
            "name": name or vinfo(vintage)["layer"], "type": "Feature Layer",
            "geometryType": geometry, "editingInfo": info,
            "fields": [{"name": n, "type": t, "alias": n} for n, t in flds],
            "maxRecordCount": 2000}


def ring(i, d=0.0):
    """A small closed ring near (-1 + i/10, 52 + i/10), shifted by d."""
    x, y = -1.0 + i / 10 + d, 52.0 + i / 10
    return [[x, y], [x + 0.01, y], [x + 0.01, y + 0.01], [x, y]]


def geometry(i, *, multi=None, d=0.0):
    multi = (i % 2 == 0) if multi is None else multi
    if multi:
        return {"type": "MultiPolygon",
                "coordinates": [[ring(i, d)], [ring(i, d + 0.05)]]}
    return {"type": "Polygon", "coordinates": [ring(i, d)]}


def props(code, i, vintage=V24, overrides=None):
    yy = vinfo(vintage)["yy"]
    p = {f"LAD{yy}CD": code, f"LAD{yy}NM": NAMES.get(code, "Somewhere"),
         "LONG": round(-1.0 + i / 10, 5), "LAT": round(52.0 + i / 10, 5),
         "Shape__Area": 93883460.626159668 + i}
    for (c, k), v in (overrides or {}).items():
        if c == code:
            if v is m.MISSING:
                p.pop(k, None)
            else:
                p[k] = v
    return p


def geojson_doc(codes=None, vintage=V24, *, overrides=None, geoms=None,
                extra=(), exceeded=None, crs="EPSG:4326"):
    """A FeatureCollection as the query writes it (f=geojson, outSR=4326)."""
    codes = list(CODES if codes is None else codes)
    feats = []
    for i, code in enumerate(codes):
        g = (geoms or {}).get(code, geometry(i))
        feats.append({"type": "Feature", "geometry": g,
                      "properties": props(code, i, vintage, overrides)})
    feats += list(extra)
    doc = {"type": "FeatureCollection", "features": feats}
    if crs is not None:
        doc["crs"] = {"type": "name", "properties": {"name": crs}}
    if exceeded is not None:
        doc["properties"] = {"exceededTransferLimit": exceeded}
    return doc


def write_json(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(doc).encode("utf-8"))
    return path


def write_geojson(path, codes=None, vintage=V24, **kw):
    return write_json(path, geojson_doc(codes, vintage, **kw))


def write_layer(path, vintage=V24, **kw):
    return write_json(path, layer_json(vintage, **kw))


class Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.n = 0
        p = mock.patch.object(m, "EXPECTED_AREAS", len(CODES))
        p.start()
        self.addCleanup(p.stop)

    def write(self, codes=None, vintage=V24, **kw):
        self.n += 1
        return write_geojson(self.root / f"g{self.n}.geojson", codes, vintage,
                             **kw)

    def read(self, codes=None, vintage=V24, **kw):
        return m.read_geojson(self.write(codes, vintage, **kw), vintage)

    def raises(self, *needles, codes=None, vintage=V24, **kw):
        path = self.write(codes, vintage, **kw)
        with self.assertRaises(ValueError) as e:
            m.read_geojson(path, vintage)
        for n in needles:
            self.assertIn(n, str(e.exception))
        return str(e.exception)


# ---------------------------------------------------------------------------
# The service list, the layer, the query URL
# ---------------------------------------------------------------------------

class Services(unittest.TestCase):

    def test_the_declared_vintage_feature_server_is_found(self):
        self.assertEqual(m.services(services_json(), V24), service_name(V24))
        self.assertEqual(m.services(services_json(), V26), service_name(V26))
        self.assertEqual(m.services(services_json(), V25), service_name(V25))

    def test_a_renamed_service_halts_listing_the_bgc_names(self):
        names = [("Local_Authority_Districts_May_2024_Boundaries_UK_BGC_V2",
                  "FeatureServer"),
                 ("Wards_May_2024_Boundaries_UK_BGC", "FeatureServer")]
        with self.assertRaises(ValueError) as e:
            m.services(services_json(names), V24)
        msg = str(e.exception)
        self.assertIn("_UK_BGC_V2", msg)
        self.assertIn("0 FeatureServer", msg)
        self.assertNotIn("Wards", msg)

    def test_two_matches_halt(self):
        names = [(service_name(V24), "FeatureServer"),
                 ("Local_Authority_Districts_MAY_2024_Boundaries_UK_BGC",
                  "FeatureServer")]
        with self.assertRaises(ValueError) as e:
            m.services(services_json(names), V24)
        self.assertIn("2 FeatureServer", str(e.exception))
        self.assertIn("MAY_2024", str(e.exception))

    def test_a_map_server_alone_is_not_a_match(self):
        with self.assertRaises(ValueError):
            m.services(services_json([(service_name(V24), "MapServer")]), V24)

    def test_an_undeclared_vintage_raises(self):
        with self.assertRaises(ValueError) as e:
            m.services(services_json(), "December_2024")
        self.assertIn("not a declared vintage", str(e.exception))

    def test_discovery_lines_name_the_undeclared_vintages_seen(self):
        lines = "\n".join(m.discovery_lines(services_json(), V24))
        self.assertIn(service_name(V24), lines)
        self.assertIn("December_2024", lines)
        self.assertIn("not declared", lines)


class Layer(unittest.TestCase):

    def test_identity_and_rank_from_last_edit_date(self):
        d = m.layer_identity(layer_json(), V24)
        self.assertEqual(d["name"], "LAD_MAY_2024_UK_BGC")
        self.assertEqual((d["code_field"], d["name_field"]),
                         ("LAD24CD", "LAD24NM"))
        self.assertEqual(d["last_edit_ms"], LAST_MS)
        self.assertEqual(d["rank"], RANK)
        self.assertEqual(d["period"], P24)
        self.assertEqual(m.release_rank(d), RANK)

    def test_later_vintages_have_their_own_fields(self):
        d = m.layer_identity(layer_json(V26, last=1788960865457), V26)
        self.assertEqual(d["code_field"], "LAD26CD")
        self.assertEqual(d["period"], P26)
        self.assertEqual(m.layer_identity(layer_json(V25), V25)["period"], P25)

    def test_a_layer_with_other_fields_halts(self):
        for kw, needle in ((dict(drop=("LAT",)), "LAT"),
                           (dict(fields=[("FID", "esriFieldTypeOID"),
                                         ("LAD23CD", "esriFieldTypeString")]),
                            "LAD23CD"),
                           (dict(drop=("Shape__Length",)), "Shape__Length"),
                           (dict(types={"LONG": "esriFieldTypeString"}),
                            "LONG")):
            with self.subTest(needle=needle), \
                    self.assertRaises(ValueError) as e:
                m.layer_identity(layer_json(**kw), V24)
            self.assertIn(needle, str(e.exception))

    def test_another_layer_name_or_geometry_halts(self):
        with self.assertRaises(ValueError) as e:
            m.layer_identity(layer_json(name="LAD_DEC_2025_UK_BGC"), V24)
        self.assertIn("LAD_DEC_2025_UK_BGC", str(e.exception))
        with self.assertRaises(ValueError):
            m.layer_identity(layer_json(geometry="esriGeometryPoint"), V24)

    def test_no_last_edit_date_halts(self):
        for last in (None, "2024-07-02"):
            with self.subTest(last=last), self.assertRaises(ValueError) as e:
                m.layer_identity(layer_json(last=last), V24)
            self.assertIn("lastEditDate", str(e.exception))

    def test_the_vintage_of_a_layer_is_read_from_its_name(self):
        self.assertEqual(m.vintage_of_layer(layer_json(V26)), V26)
        with self.assertRaises(ValueError) as e:
            m.vintage_of_layer(layer_json(name="LAD_DEC_2024_UK_BGC"))
        self.assertIn("LAD_DEC_2024_UK_BGC", str(e.exception))


class QueryUrl(unittest.TestCase):

    def test_the_england_geojson_query_in_4326(self):
        url = m.query_url(service_name(V24), "LAD24CD")
        self.assertTrue(url.startswith(
            f"{m.SERVICES_URL}/{service_name(V24)}/FeatureServer/0/query?"))
        self.assertIn("where=LAD24CD%20LIKE%20%27E%25%27", url)
        self.assertIn("outFields=LAD24CD%2CLAD24NM%2CLONG%2CLAT%2C"
                      "Shape__Area", url)
        for part in ("outSR=4326", "f=geojson", "resultRecordCount=2000"):
            self.assertIn(part, url)
        self.assertEqual(m.layer_url(service_name(V24)),
                         f"{m.SERVICES_URL}/{service_name(V24)}/FeatureServer"
                         "/0?f=json")


# ---------------------------------------------------------------------------
# The GeoJSON, cells, records
# ---------------------------------------------------------------------------

class ReadGeojson(Tmp):

    def test_identity_records_and_numbers_at_the_live_scales(self):
        f = self.read()
        self.assertEqual(sorted(f["rows"]), CODES)
        r = f["rows"]["E06000001"]
        self.assertEqual(r["lad24nm"], "Hartlepool")
        self.assertEqual(r["longitude"], Decimal("-1.000000"))
        self.assertEqual(r["shape_area"], Decimal("93883460.63"))
        self.assertEqual(f["types"], {"MultiPolygon": 3, "Polygon": 2})
        self.assertEqual(f["ignored"], 0)
        self.assertEqual(json.loads(r["geojson"]), geometry(0))

    def test_exceeded_transfer_limit_halts(self):
        self.raises("exceededTransferLimit", exceeded=True)
        self.assertEqual(len(self.read(exceeded=False)["rows"]), len(CODES))
        path = self.root / "x.geojson"
        doc = geojson_doc()
        doc["exceededTransferLimit"] = True
        write_json(path, doc)
        with self.assertRaises(ValueError):
            m.read_geojson(path, V24)

    def test_a_non_england_code_is_never_stored(self):
        extra = [{"type": "Feature", "geometry": geometry(9),
                  "properties": props(c, 9)}
                 for c in ("W06000001", "S12000033", "N09000001")]
        f = self.read(extra=extra)
        self.assertEqual(sorted(f["rows"]), CODES)
        self.assertEqual(f["ignored"], 3)
        recs = m.build_records(f)
        self.assertEqual([r["lad24cd"] for r in recs], CODES)
        # an English code that is not a district halts
        bad = [{"type": "Feature", "geometry": geometry(9),
                "properties": props("E10000002", 9)}]
        self.raises("E10000002", extra=bad)

    def test_a_missing_or_blank_attribute_halts(self):
        for k, v in (("LAT", m.MISSING), ("LONG", None), ("LAD24NM", " "),
                     ("Shape__Area", "93883460.6"), ("LAT", True)):
            with self.subTest(k=k, v=v):
                self.raises("E06000002", k,
                            overrides={("E06000002", k): v})

    def test_a_duplicate_code_halts(self):
        self.raises("E06000002", "twice",
                    codes=CODES[:4] + ["E06000002"])

    def test_the_count_must_be_the_expected_areas(self):
        self.raises(f"{len(CODES) - 1} English", codes=CODES[:-1])

    def test_geometry_type_and_form(self):
        self.raises("Point", geoms={"E06000001": {
            "type": "Point", "coordinates": [-1.0, 52.0]}})
        self.raises("geometry", geoms={"E06000001": None})
        self.raises("bbox", geoms={"E06000001": dict(geometry(0),
                                                       bbox=[0, 0, 1, 1])})
        self.raises("empty", geoms={"E06000001": {"type": "Polygon",
                                                    "coordinates": []}})

    def test_coordinates_must_be_wgs84_in_england(self):
        bng = {"type": "Polygon", "coordinates": [[[447161, 531473],
                                                   [447200, 531473],
                                                   [447161, 531500],
                                                   [447161, 531473]]]}
        self.raises("E06000001", "outSR=4326", geoms={"E06000001": bng})
        self.raises("EPSG:27700", crs="EPSG:27700")
        self.assertEqual(len(self.read(crs=None)["rows"]), len(CODES))

    def test_another_vintage_reads_its_own_fields(self):
        f = self.read(CODES_NEW, V26)
        self.assertEqual(sorted(f["rows"]), CODES_NEW)
        with self.assertRaises(ValueError) as e:
            m.read_geojson(self.write(CODES_NEW, V26), V24)
        self.assertIn("LAD24CD", str(e.exception))

    def test_an_arcgis_error_reply_halts(self):
        path = write_json(self.root / "err.geojson",
                          {"error": {"code": 400, "message": "Invalid query"}})
        with self.assertRaises(ValueError) as e:
            m.read_geojson(path, V24)
        self.assertIn("Invalid query", str(e.exception))


class Cells(unittest.TestCase):

    def test_num_cell(self):
        self.assertEqual(m.num_cell(-1.27017, "x", "0.000001"),
                         Decimal("-1.270170"))
        self.assertEqual(m.num_cell(Decimal("93883460.626159668"), "x",
                                    "0.01"), Decimal("93883460.63"))
        self.assertEqual(m.num_cell(5, "x", "0.01"), Decimal("5.00"))
        for bad in (None, "", "1.5", True, float("nan")):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as e:
                m.num_cell(bad, "here", "0.01")
            self.assertIn("here", str(e.exception))

    def test_half_rounds_away_from_zero_as_numeric_does(self):
        self.assertEqual(m.num_cell(Decimal("0.125"), "x", "0.01"),
                         Decimal("0.13"))
        self.assertEqual(m.num_cell(Decimal("-0.125"), "x", "0.01"),
                         Decimal("-0.13"))


class Canonical(unittest.TestCase):

    def test_equal_for_reordered_keys(self):
        a = {"type": "Polygon", "coordinates": [ring(1)]}
        b = {"coordinates": [ring(1)], "type": "Polygon"}
        self.assertEqual(m.canonical_geojson(a), m.canonical_geojson(b))
        self.assertEqual(m.canonical_geojson(a),
                         json.dumps(a, sort_keys=True, separators=(",", ":")))

    def test_decimal_and_float_parses_agree(self):
        text = '{"type":"Polygon","coordinates":[[[-1.22458507960738,' \
               '54.6241796529366],[1.2E-05,54.5],[-1.22458507960738,' \
               '54.6241796529366]]]}'
        self.assertEqual(
            m.canonical_geojson(json.loads(text)),
            m.canonical_geojson(json.loads(text, parse_float=Decimal)))

    def test_jsonb_text_reads_back_the_same_way(self):
        g = geometry(3)
        # jsonb's own output: ', ' and ': ' separators, keys reordered
        jsonb_text = ('{"type": "' + g["type"] + '", "coordinates": '
                      + json.dumps(g["coordinates"]) + '}')
        self.assertEqual(m.canonical_text(jsonb_text),
                         m.canonical_geojson(g))

    def test_a_coordinate_change_is_a_difference(self):
        self.assertNotEqual(m.canonical_geojson(geometry(1)),
                            m.canonical_geojson(geometry(1, d=1e-9)))


class Records(Tmp):

    def test_build_records_sorted_with_the_period(self):
        recs = m.build_records(self.read())
        self.assertEqual([r["lad24cd"] for r in recs], CODES)
        self.assertEqual({r["source_date"] for r in recs}, {P24})
        self.assertEqual(set(recs[0]), {"lad24cd", "source_date"}
                         | set(m.VALUES))

    def test_rows_content_sha_is_key_and_the_five_values(self):
        recs = m.build_records(self.read())
        sha = m.rows_content_sha(recs)
        self.assertEqual(sha, m.rows_content_sha(list(reversed(recs))))
        lines = sorted("|".join([r["lad24cd"], r["lad24nm"],
                                 f"{r['longitude']:f}", f"{r['latitude']:f}",
                                 f"{r['shape_area']:f}", r["geojson"]])
                       for r in recs)
        self.assertEqual(sha, hashlib.sha256(
            "\n".join(lines).encode("utf-8")).hexdigest())
        other = [dict(r) for r in recs]
        other[0]["geojson"] = m.canonical_geojson(geometry(0, d=0.001))
        self.assertNotEqual(m.rows_content_sha(other), sha)

    def test_check_records(self):
        recs = m.build_records(self.read())
        m.check_records(recs, P24)
        with self.assertRaises(ValueError):
            m.check_records(recs, P26)
        bad = [dict(recs[0], geojson=None)] + recs[1:]
        with self.assertRaises(ValueError):
            m.check_records(bad, P24)


# ---------------------------------------------------------------------------
# Ranks, ledger, planning, vintage differences, stops
# ---------------------------------------------------------------------------

class Ranks(unittest.TestCase):

    def test_rank_from_last_edit_date_never_the_service_name(self):
        self.assertIsNone(m.release_rank(
            "Local_Authority_Districts_May_2026_Boundaries_UK_BGC_layer_"
            "2026-10-10.json"))
        self.assertIsNone(m.release_rank("LAD_MAY_2026_UK_BGC_england_4326_"
                                         "2026-10-10.geojson"))
        # a May 2024 layer edited later than a May 2026 one ranks later
        d24 = m.layer_identity(layer_json(V24, last=1800000000000), V24)
        d26 = m.layer_identity(layer_json(V26, last=1788960865457), V26)
        self.assertGreater(m.release_rank(d24), m.release_rank(d26))

    def test_rank_text_round_trips(self):
        t = m.rank_text(LAST_MS)
        self.assertEqual(t, "ArcGIS lastEditDate 1719926938757 (2024-07-02 "
                            "13:28:58 UTC)")
        self.assertEqual(m.release_rank(f"x.geojson; source_date {P24}; {t}"),
                         RANK)
        src = m.ledger_source("data/raw/x.geojson", P24, LAST_MS)
        self.assertEqual(m.parse_ledger_source(src),
                         ("data/raw/x.geojson", P24, RANK))
        self.assertIsNone(m.parse_ledger_source("x"))


class Ledger(unittest.TestCase):

    def test_ledger_skip_needs_the_pair_and_the_rank(self):
        src = m.ledger_source("u", P24, LAST_MS)
        checked = {P24: {(src, "sha")}}
        ranks = {P24: RANK}
        self.assertEqual(m.ledger_complete(checked, "u", "sha", ranks,
                                           rank=RANK), [P24])
        self.assertIsNone(m.ledger_complete(checked, "u", "other", ranks,
                                            rank=RANK))
        self.assertIsNone(m.ledger_complete(checked, "v", "sha", ranks,
                                            rank=RANK))
        later = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.assertIsNone(m.ledger_complete(checked, "u", "sha", ranks,
                                            rank=later))
        # held from a newer layer: compared again (then planned as older)
        self.assertIsNone(m.ledger_complete(checked, "u", "sha",
                                            {P24: later}, rank=RANK))

    def test_plan(self):
        src = m.ledger_source("u", P24, LAST_MS)
        ranks = {P24: RANK}
        self.assertEqual(m.plan_vintage(P24, RANK, ranks, {}, src, "s",
                                        recheck=None, allow_older=False),
                         ([], [P24], {}))
        self.assertEqual(m.plan_vintage(P26, RANK, ranks, {}, src, "s",
                                        recheck=None, allow_older=False),
                         ([P26], [], {}))
        _, _, skipped = m.plan_vintage(
            P24, RANK, ranks, {P24: {(src, "s")}}, src, "s", recheck=None,
            allow_older=False)
        self.assertIn("checked", skipped[P24])
        self.assertEqual(m.plan_vintage(
            P24, RANK, ranks, {P24: {(src, "s")}}, src, "s", recheck=P24,
            allow_older=False), ([], [P24], {}))
        earlier = datetime(2024, 1, 1, tzinfo=timezone.utc)
        _, _, skipped = m.plan_vintage(P24, earlier, ranks, {}, src, "s",
                                       recheck=None, allow_older=False)
        self.assertIn("older", skipped[P24])
        self.assertEqual(m.plan_vintage(P24, earlier, ranks, {}, src, "s",
                                        recheck=None, allow_older=True),
                         ([], [P24], {}))
        with self.assertRaises(ValueError):
            m.plan_vintage(P24, RANK, ranks, {}, src, "s", recheck=P26,
                           allow_older=False)

    def test_a_vintage_before_the_newest_held_is_older(self):
        ranks = {P24: RANK, P26: RANK}
        _, _, skipped = m.plan_vintage(P25, RANK, ranks, {}, "s", "x",
                                       recheck=None, allow_older=False)
        self.assertIn("older", skipped[P25])


class VintageDiff(Tmp):

    def test_codes_added_and_removed_names_and_geometry(self):
        live = m.build_records(self.read())
        new = m.build_records(self.read(CODES_NEW, V26))
        d = m.vintage_diff(new, live)
        self.assertEqual(d["added"], ["E08000038", "E08000039"])
        self.assertEqual(d["removed"], ["E08000016", "E08000019"])
        self.assertEqual(d["names"], [])
        self.assertEqual(d["geometry"], [])
        self.assertEqual(d["attributes"], [])
        text = "\n".join(m.diff_lines(d))
        self.assertIn("E08000038", text)
        self.assertIn("added 2", text)

    def test_names_geometry_and_attributes_changed(self):
        live = m.build_records(self.read())
        new = m.build_records(self.read(
            overrides={("E06000001", "LAD24NM"): "Hartlepool Borough",
                       ("E06000002", "LONG"): -0.5},
            geoms={"E09000033": geometry(4, d=0.001)}))
        d = m.vintage_diff(new, live)
        self.assertEqual(d["names"], [("E06000001", "Hartlepool",
                                       "Hartlepool Borough")])
        self.assertEqual(d["geometry"], ["E09000033"])
        self.assertEqual(d["attributes"], ["E06000002"])

    def test_records_without_geometry_compare_codes_and_names_only(self):
        live = m.build_records(self.read())
        new = [dict(r, geojson=None) for r in live]
        d = m.vintage_diff(new, live)
        self.assertIsNone(d["geometry"])
        self.assertIn("not compared", "\n".join(m.diff_lines(d)))


class AttributeFile(Tmp):

    def test_an_attribute_reply_is_read_for_the_difference_only(self):
        feats = [{"attributes": dict(props(c, i, V26), FID=i + 1)}
                 for i, c in enumerate(CODES_NEW)]
        feats.append({"attributes": dict(props("W06000001", 9, V26), FID=9)})
        path = write_json(self.root / "a.json", {"features": feats})
        recs = m.read_attributes(path, V26)
        self.assertEqual([r["lad24cd"] for r in recs], CODES_NEW)
        self.assertTrue(all(r["geojson"] is None for r in recs))


class Stops(Tmp):

    def test_a_reissue_missing_or_adding_an_authority_is_partial(self):
        tip = m.build_records(self.read())
        new = m.build_records(self.read(CODES_NEW))
        bad = m.period_problems(new, tip, kind="revised")
        self.assertTrue(any(m.PARTIAL in b for b in bad))
        self.assertTrue(any("E08000016" in b for b in bad))
        self.assertTrue(any("E08000038" in b for b in bad))
        self.assertEqual(m.period_problems(tip, tip, kind="revised"), [])

    def test_map_changes_are_geometry_or_names_only(self):
        tip = m.build_records(self.read())
        moved = m.build_records(self.read(
            geoms={"E06000001": geometry(0, d=0.001)}))
        renamed = m.build_records(self.read(
            overrides={("E06000002", "LAD24NM"): "Middlesbrough Town"}))
        shifted = m.build_records(self.read(
            overrides={("E06000002", "LAT"): 52.3}))
        self.assertTrue(m.map_changes(moved, tip))
        self.assertTrue(m.map_changes(renamed, tip))
        self.assertFalse(m.map_changes(shifted, tip))
        self.assertTrue(m.differs(shifted, tip))
        self.assertFalse(m.differs(tip, tip))


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

URL = "https://example.invalid/query?f=geojson"


class _Resp:
    def __init__(self, body, url):
        self.content = body
        self.url = url

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, body, final):
        self.body, self.final, self.calls = body, final, []

    def get(self, url, **kw):
        self.calls.append(url)
        return _Resp(self.body, self.final)


class Download(Tmp):

    def body(self, **kw):
        return json.dumps(geojson_doc(**kw)).encode("utf-8")

    def test_the_geojson_is_saved_under_its_dated_name(self):
        body = self.body()
        dest = self.root / "raw" / "LAD_MAY_2024_UK_BGC_england_4326_" \
                                   "2026-10-11.geojson"
        with quiet() as out:
            path, final = m.fetch_api(URL, dest,
                                      session=_Session(body, URL + "&x"))
        self.assertEqual((path, final), (dest, URL + "&x"))
        self.assertEqual(path.read_bytes(), body)
        self.assertIn("redirected", out.getvalue())

    def test_same_name_different_content_saved_beside_never_overwritten(self):
        raw = self.root / "raw"
        raw.mkdir()
        old = raw / "a.geojson"
        old.write_bytes(self.body())
        before = old.read_bytes()
        new_body = self.body(overrides={("E06000001", "LAT"): 52.5})
        with quiet():
            path, _ = m.fetch_api(URL, old, session=_Session(new_body, URL))
        sha8 = hashlib.sha256(new_body).hexdigest()[:8]
        self.assertEqual(path.name, f"a-{sha8}.geojson")
        self.assertEqual(path.read_bytes(), new_body)
        self.assertEqual(old.read_bytes(), before)

    def test_an_existing_same_named_file_does_not_skip_the_download(self):
        raw = self.root / "raw"
        raw.mkdir()
        body = self.body()
        dest = raw / "a.geojson"
        dest.write_bytes(body)
        s = _Session(body, URL)
        with quiet() as out:
            path, _ = m.fetch_api(URL, dest, session=s)
        self.assertEqual(s.calls, [URL])
        self.assertEqual(path, dest)
        self.assertIn("same content", out.getvalue())

    def test_a_reply_that_is_not_the_expected_format_keeps_nothing(self):
        raw = self.root / "raw"
        err = json.dumps({"error": {"code": 400}}).encode()
        for name, body in (("a.geojson", b"<html>error</html>"),
                           ("b.geojson", err), ("c.json", err),
                           ("d.json", b"not json"), ("e.txt", b"{}")):
            with self.subTest(name=name), self.assertRaises(SystemExit), \
                    quiet():
                m.fetch_api(URL, raw / name, session=_Session(body, URL))
        self.assertEqual(sorted(p.name for p in raw.iterdir())
                         if raw.exists() else [], [])

    def test_the_layer_json_is_saved(self):
        body = json.dumps(layer_json()).encode()
        dest = self.root / "raw" / "layer.json"
        with quiet():
            path, _ = m.fetch_api(URL, dest, session=_Session(body, URL))
        self.assertEqual(m.layer_identity(json.loads(path.read_text()),
                                          V24)["rank"], RANK)


# ---------------------------------------------------------------------------
# The module: no la_code_lookup write, no DELETE, spec and constants
# ---------------------------------------------------------------------------

def _strings(tree):
    """Every string literal of a module, f-string parts included."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM|"
                   r"TRUNCATE|ALTER\s+TABLE|DROP\s+TABLE)\b", re.I)


class Module(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = Path(m.__file__).read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)

    def test_no_zero_coercion_commit_or_recode_dict(self):
        import check_loaders
        reasons = check_loaders.check_loader(Path(m.__file__),
                                             {"revises_back_series": False})
        self.assertFalse([r for r in reasons if "zero coercion" in r
                          or "commit" in r or "recode dict" in r
                          or "editions_core" in r], reasons)

    def test_no_la_code_lookup_write(self):
        for line, s in _strings(self.tree):
            if "la_code_lookup" in s:
                self.assertIsNone(WRITE.search(s), f"line {line}: {s!r}")

    def test_no_delete_truncate_or_drop_anywhere(self):
        for line, s in _strings(self.tree):
            self.assertIsNone(re.search(r"\b(DELETE\s+FROM|TRUNCATE\s+|"
                                        r"DROP\s+TABLE)", s, re.I),
                              f"line {line}: {s!r}")

    def test_no_insert_or_update_of_the_live_table(self):
        for line, s in _strings(self.tree):
            hit = re.search(r"\b(INSERT\s+INTO|UPDATE)\s+(public\.)?"
                            r"(la_boundaries|\{spec\.live_table\}|"
                            r"\{s\.live_table\})", s, re.I)
            self.assertIsNone(hit, f"line {line}: {s!r}")
        # f-string parts: the live table is never the target of a write
        for node in ast.walk(self.tree):
            if isinstance(node, ast.JoinedStr):
                parts = [v.value if isinstance(v, ast.Constant) else
                         ast.unparse(v) for v in node.values]
                text = "".join(str(p) for p in parts)
                self.assertIsNone(re.search(
                    r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+public\.\{?"
                    r"[\w.]*live_table", text, re.I), text)

    def test_the_unsupported_simplification_caveat_is_not_repeated(self):
        self.assertNotIn("20%", self.src)
        self.assertNotRegex(self.src, r"(?i)simplified to")

    def test_spec_and_profile(self):
        s = m.SPEC
        self.assertEqual((s.live_table, s.editions_table, s.period_col,
                          s.key_cols), ("la_boundaries",
                                        "la_boundaries_editions",
                                        "source_date", ("lad24cd",)))
        self.assertTrue(s.live_single_period)
        self.assertFalse(s.refresh_key_changes)
        self.assertFalse(s.fk_la_boundaries)
        self.assertEqual(dict(s.value_cols), {
            "lad24nm": "varchar NOT NULL",
            "longitude": "numeric(10,6) NOT NULL",
            "latitude": "numeric(10,6) NOT NULL",
            "shape_area": "numeric(20,2) NOT NULL",
            "geojson": "jsonb NOT NULL"})
        self.assertEqual(set(s.compare_cols), set(m.VALUES))
        self.assertEqual(dict(s.key_types)["source_date"], "date NOT NULL")
        p = m.PROFILE
        self.assertTrue(p.file_checks)
        self.assertEqual((p.run_agent, p.run_source, p.savepoint),
                         ("s7_boundaries_editions", "7", "s7_period"))

    def test_the_vintages_and_held_constants(self):
        self.assertEqual({v: d["period"] for v, d in m.VINTAGES.items()},
                         {V24: P24, V25: P25, V26: P26})
        self.assertEqual((m.LIVE_VINTAGE, m.LIVE_PERIOD), (V24, P24))
        self.assertEqual(m.LEGACY_LIVE, (296, "6b984c4f30b2f17c"
                                              "e470736d8b5f75be"))
        self.assertEqual(m.LEGACY_LOADED,
                         {"2026-03-25 15:13:11.744860+00": 296})
        self.assertEqual(m.LEGACY_RANK_MS, LAST_MS)
        self.assertEqual(m.LEGACY_FILE["bytes_sha256"],
                         SHA_GEOJSON_HELD_BYTES)
        self.assertEqual(len(m.LEGACY_FILE["content_sha256"]), 64)
        self.assertEqual(m.LEGACY_FILE["url"], m.query_url(
            service_name(V24), "LAD24CD"))


if __name__ == "__main__":
    unittest.main()
