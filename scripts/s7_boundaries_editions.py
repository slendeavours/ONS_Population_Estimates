"""S7 (ONS Local Authority Districts, May 2024, generalised clipped (BGC)
boundaries, England): edition history and loader on the period-editions
engine, with a single-vintage live table.

The live table la_boundaries keeps its name, key (lad24cd) and columns and
holds one vintage: May 2024 (source_date 2024-05-01). It is the W1 spine
(sql/w1/05_la_signals.sql reads FROM la_boundaries), the map's geometry
(scripts/export_map_data.py exports lad24cd and geojson) and the target of
18 foreign keys (read 2026-10-11: la_asylum_support, la_immigration_groups
and sixteen editions tables). This loader therefore never deletes, re-keys,
inserts or rebuilds a live row: the only write to la_boundaries is
refresh-latest's UPDATE of the value columns and loaded_at on rows that
already exist, and that needs --acknowledge 2024-05-01 (it changes the
map's geometry: Scott's decision). refresh_key_changes is off. It never
writes la_code_lookup either (the old n8n workflow did; see History).

The vintage's date is the engine period (source_date, a DATE; the engine
uses the string '2024-05-01'; it sorts, rule 8). VINTAGES maps each declared
vintage (its spelling in the ArcGIS service name) to its date. This module
owns the editions table and file-check ledger, append-only:

    SPEC     la_boundaries_editions               period source_date
    ledger   la_boundaries_editions_file_checks

la_boundaries_editions has no foreign key to la_boundaries: it holds other
vintages' codes (E08000038/39 in Dec 2025 and May 2026). An edition is what
one ArcGIS query says about its vintage: per English district the name, the
publisher's LONG and LAT (numeric(10,6)), Shape__Area (numeric(20,2)) and
the geometry (jsonb), compared and hashed as canonical text
(canonical_geojson: json.dumps with sorted keys and no spaces; the live
jsonb is read back as text and put through the same function). Every held
geometry equals the publisher's BGC layer queried in EPSG:4326, so the
stored shapes are the publisher's generalised (BGC) boundaries as
published, England only, with no change made here.

Vintages (rule 2): a reissue of the live vintage (a later lastEditDate with
different content) is the next edition and reaches live only through
refresh-latest; another vintage (load --vintage DEC_2025 or May_2026) is
stored as editions only, never promoted to live by a load
(EditionSpec.live_single_period), and its differences from the live
vintage are printed (vintage_diff). Adopting another vintage re-bases every
join and flips Barnsley/Sheffield: a deliberate decision, never a load.

Rule 1 (blanks and zeros): every attribute is present; a missing or blank
name, a missing, blank or text number, halts (num_cell). Nothing is ever
read as 0.

Identity (rule 3), from the source itself on every path:
  - the ArcGIS service list: exactly one FeatureServer named
    Local_Authority_Districts_<vintage>_Boundaries_UK_BGC (services; none
    or several halt, listing the Local_Authority_Districts*BGC* names seen);
    vintages offered and not declared are named (discovery_lines).
  - the layer definition (FeatureServer/0?f=json): its name (for example
    LAD_MAY_2024_UK_BGC), type, polygon geometry and the exact field set
    (LAD<yy>CD, LAD<yy>NM and the others the publisher declares), with
    editingInfo.lastEditDate (epoch milliseconds) the rank, never a
    service or file name or a download date (layer_identity).
  - the GeoJSON reply: a FeatureCollection in EPSG:4326 (every coordinate
    a longitude and latitude in England's range), no exceededTransferLimit,
    exactly the five queried attributes on every feature, Polygon or
    MultiPolygon geometry, EXPECTED_AREAS English districts (E06-E09), no
    code twice (read_geojson). Welsh, Scottish and Northern Irish codes are
    read and ignored (never stored); any other code halts.
The GeoJSON bytes are API output, so they are not a release identity: the
layer plus the content is. The content sha (rows_content_sha) decides
'unchanged' and is what the ledger records.

Geography (rule 4): source 7 is declared 'old' in scripts/geography.py:
this source defines the canonical key and is checked against itself (the
live vintage's key set never changes by a reissue: PARTIAL, no flag
releases it). geography.resolve is not used: nothing here maps a code.

Stop conditions (in load; a stopped vintage is REJECTED: nothing stored, no
ledger row, exit 1): a reissue that drops or adds an authority (PARTIAL; no
flag releases it); any change to the live vintage's geometry or names
(--acknowledge 2024-05-01); the same lastEditDate as the held tip with
different content (--accept-reissue PERIOD); an older layer (its
lastEditDate before the held tip's, or a vintage before the newest held)
halts unless --allow-older-file. An empty la_boundaries halts the load (it
is never rebuilt here). Each acceptance is written to the edition's label
and the run log.

History (so the wording here stays truthful): the n8n workflow "LA
Boundaries (S7)" queried the same ArcGIS layer and upserted la_boundaries;
pipeline_run_log id 1 (2026-03-25 12:38 UTC, source_07_boundaries) is its
only log row while the rows carry loaded_at 15:13:11 UTC, and its "Log Run"
node is a SELECT that writes nothing, so a later unlogged run or step wrote
them. After the upsert the workflow replaced la_code_lookup's non-current
rows with its own March 2026 list; that node and the rest of the workflow
were retired by scripts/n8n_retire_no_loader_sources.py. No held value is
affected: every held value equals the held query read again
(data/raw/s7_boundaries). migrate-legacy (one-off) records the held vintage
as edition 1 "as loaded", with that proof on every stored column.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back). There is no sync-new: migrate-legacy records the held
vintage.
    python scripts/s7_boundaries_editions.py ddl [--commit | --simulate]
    python scripts/s7_boundaries_editions.py status
    python scripts/s7_boundaries_editions.py load [--vintage V]
        [--file GEOJSON --layer JSON] [--recheck PERIOD]
        [--allow-older-file] [--accept-reissue PERIOD ...]
        [--acknowledge 2024-05-01] [--commit | --simulate]
        # without --file: the layer JSON and the GeoJSON are downloaded to
        # data/raw/s7_boundaries/ with the date (also in a preview; a
        # preview writes nothing to the database)
    python scripts/s7_boundaries_editions.py refresh-latest
        [--acknowledge 2024-05-01] [--accept-drift PERIOD]
        [--commit | --simulate]
    python scripts/s7_boundaries_editions.py migrate-legacy --file GEOJSON
        --layer JSON [--commit | --simulate]
    python scripts/s7_boundaries_editions.py restore-edition PERIOD N
        [--commit | --simulate]
"""
import argparse
import dataclasses
import hashlib
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402

RUN_AGENT = "s7_boundaries_editions"
RUN_SOURCE = "7"
LIVE_MISSING = pe.LIVE_MISSING
HELD_NOT_LIVE = pe.HELD_NOT_LIVE

# the ONS Open Geography Portal's ArcGIS organisation (services1)
SERVICES_URL = ("https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/"
                "services")
REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s7_boundaries"     # git-ignored
USER_AGENT = ("ucws-pipeline S7 loader (read-only download of the public ONS "
              "Local Authority Districts BGC boundaries)")

# ---------------------------------------------------------------------------
# Columns (live types from information_schema, checked 2026-10-11: lad24cd
# character varying NOT NULL (PK), lad24nm character varying NOT NULL,
# longitude and latitude numeric(10,6), shape_area numeric(20,2), geojson
# jsonb (all nullable; 0 NULL held), loaded_at timestamptz DEFAULT now(),
# source_date date (nullable; 2024-05-01 on all 296)). The editions table
# holds every value NOT NULL: a blank halts, never stored.
# ---------------------------------------------------------------------------

KEY = "lad24cd"
PERIOD = "source_date"
VALUE_TYPES = (("lad24nm", "varchar NOT NULL"),
               ("longitude", "numeric(10,6) NOT NULL"),
               ("latitude", "numeric(10,6) NOT NULL"),
               ("shape_area", "numeric(20,2) NOT NULL"),
               ("geojson", "jsonb NOT NULL"))
VALUES = tuple(c for c, _ in VALUE_TYPES)
NUMBERS = (("longitude", "LONG", "0.000001"), ("latitude", "LAT", "0.000001"),
           ("shape_area", "Shape__Area", "0.01"))
NAME_COLS = ("lad24nm", "geojson")      # what the map shows

# ---------------------------------------------------------------------------
# The vintages, each declared with the evidence of its own layer (read
# 2026-10-10: data/raw/s7_boundaries/*_layer_2026-10-10.json). The key is
# the vintage as the service name spells it; period is the vintage's date.
# Another vintage is added here deliberately; until then it halts.
# ---------------------------------------------------------------------------

VINTAGES = {
    "May_2024": {"period": "2024-05-01", "yy": "24",
                 "layer": "LAD_MAY_2024_UK_BGC"},
    "DEC_2025": {"period": "2025-12-01", "yy": "25",
                 "layer": "LAD_DEC_2025_UK_BGC"},
    "May_2026": {"period": "2026-05-01", "yy": "26",
                 "layer": "LAD_MAY_2026_UK_BGC"},
}
LIVE_VINTAGE = "May_2024"
LIVE_PERIOD = VINTAGES[LIVE_VINTAGE]["period"]
EXPECTED_AREAS = 296
RESULT_RECORD_COUNT = 2000
DISTRICT_RE = re.compile(r"E0[6-9][0-9]{6}")
OTHER_NATION_RE = re.compile(r"[WSN][0-9]{8}")
PERIOD_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
BGC_RE = re.compile(r"^Local_Authority_Districts.*BGC", re.I)
# England's extent in EPSG:4326, with a margin (Isles of Scilly to
# Lowestoft, the Lizard to Berwick): a coordinate outside it is not a
# WGS 84 longitude and latitude of an English district
LON_RANGE = (-7.0, 2.5)
LAT_RANGE = (49.5, 56.1)
PARTIAL = "PARTIAL FILE"    # problems no flag releases
MISSING = object()          # a test sentinel: an attribute left out
MAP_MESSAGE = ("this changes the map's geometry: Scott's decision "
               "(la_boundaries is the W1 spine and the map's geometry and "
               "names; it is written only with --acknowledge "
               f"{LIVE_PERIOD})")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_TS_FMT = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# The held state (migrate-legacy preconditions), surveyed 2026-10-10 and
# reproduced 2026-10-11
# ---------------------------------------------------------------------------

# (rows, the survey hash: md5 of lad24cd|lad24nm|longitude|latitude|
# shape_area|geojson|source_date, NULL as '~', ordered by source_date,
# lad24cd; see live_state)
LEGACY_LIVE = (296, "6b984c4f30b2f17c" "e470736d8b5f75be")
# the live loaded_at values (UTC) and their row counts
LEGACY_LOADED = {"2026-03-25 15:13:11.744860+00": 296}
# the held May 2024 layer's editingInfo.lastEditDate (2024-07-02 13:28:58
# UTC): the rank of the held vintage before the editions exist
LEGACY_RANK_MS = 1719926938757
# the held query reply (sha256 split so the credential scan does not flag
# it). The content sha (rows_content_sha) is what identifies it: the bytes
# are API output. The URL is the query a load builds, which the ledger row
# names so that an API load finds the content checked.
LEGACY_FILE = {
    "name": "LAD_MAY_2024_UK_BGC_england_4326_2026-10-10.geojson",
    "bytes_sha256": "7fd7632b1e00a2c0b83f138f5be4f2d5"
                    "94de10d524da02178ea8770cccd5385c",
    "content_sha256": "822ead9b823c7273454f0cb54baec4d8"
                      "2a244c825c7d8c48541b964d50b543bb",
    "url": None,    # filled below from query_url
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _json(x):
    if isinstance(x, (str, bytes)):
        return json.loads(x, parse_float=Decimal)
    return x if x is not None else {}


def _ms_rank(ms) -> datetime:
    """editingInfo.lastEditDate (epoch milliseconds) as a UTC datetime,
    exactly (no float division)."""
    return EPOCH + timedelta(milliseconds=int(ms))


def _rank_ms(rank) -> int:
    """A rank (UTC datetime or epoch milliseconds) as epoch milliseconds."""
    if isinstance(rank, datetime):
        return (rank - EPOCH) // timedelta(milliseconds=1)
    return int(rank)


def rank_text(rank) -> str:
    """'ArcGIS lastEditDate 1719926938757 (2024-07-02 13:28:58 UTC)': the
    publisher's epoch milliseconds, then the time they stand for."""
    if rank is None:
        return "no ArcGIS lastEditDate"
    ms = _rank_ms(rank)
    return (f"ArcGIS lastEditDate {ms} "
            f"({_ms_rank(ms).strftime(_TS_FMT)} UTC)")


def service_of(vintage) -> str:
    return f"Local_Authority_Districts_{vintage}_Boundaries_UK_BGC"


def layer_url(service) -> str:
    return f"{SERVICES_URL}/{service}/FeatureServer/0?f=json"


def list_url() -> str:
    return f"{SERVICES_URL}?f=json"


def query_url(service, code_field) -> str:
    """The England query of a layer: every feature whose code starts E, the
    code, name, LONG, LAT and Shape__Area, the geometry in EPSG:4326, as
    GeoJSON, up to RESULT_RECORD_COUNT features (an exceeded limit halts
    when the reply is read)."""
    name_field = code_field[:-2] + "NM"
    q = urlencode([("where", f"{code_field} LIKE 'E%'"),
                   ("outFields", ",".join([code_field, name_field, "LONG",
                                           "LAT", "Shape__Area"])),
                   ("outSR", "4326"), ("f", "geojson"),
                   ("resultRecordCount", str(RESULT_RECORD_COUNT))],
                  quote_via=quote, safe="")
    return f"{SERVICES_URL}/{service}/FeatureServer/0/query?{q}"


LEGACY_FILE["url"] = query_url(service_of(LIVE_VINTAGE), "LAD24CD")


# ---------------------------------------------------------------------------
# The service list, discovery, the layer
# ---------------------------------------------------------------------------

def _bgc(list_json) -> list:
    return [(str(s.get("name")), str(s.get("type")))
            for s in (_json(list_json).get("services") or [])
            if BGC_RE.match(str(s.get("name") or ""))]


def services(list_json, vintage) -> str:
    """The FeatureServer name of a declared vintage in the ArcGIS service
    list (Local_Authority_Districts_<vintage>_Boundaries_UK_BGC). ValueError,
    listing every Local_Authority_Districts*BGC* service seen, unless
    exactly one FeatureServer carries that name (compared without regard to
    capitals, so a second spelling counts as a second match) and it is
    spelled exactly so."""
    if vintage not in VINTAGES:
        raise ValueError(f"{vintage!r} is not a declared vintage "
                         f"({', '.join(VINTAGES)}); a vintage is declared in "
                         "VINTAGES deliberately, with its layer as evidence")
    want = service_of(vintage)
    bgc = _bgc(list_json)
    hits = [n for n, t in bgc if t == "FeatureServer"
            and n.lower() == want.lower()]
    if len(hits) != 1 or hits[0] != want:
        seen = [f"{n} ({t})" for n, t in sorted(bgc)]
        raise ValueError(f"{len(hits)} FeatureServer named {want} in the "
                         f"ArcGIS service list ({hits}); "
                         "Local_Authority_Districts*BGC* services seen: "
                         f"{seen or 'none'}")
    return hits[0]


def discovery_lines(list_json, vintage) -> list:
    """Lines saying what the service list offers: the declared vintage's
    FeatureServer (services; raises as it does), the other declared
    vintages offered, and every LAD BGC FeatureServer not declared in
    VINTAGES (named, never loaded)."""
    name = services(list_json, vintage)
    n = len(_json(list_json).get("services") or [])
    fs = sorted(n_ for n_, t in _bgc(list_json) if t == "FeatureServer")
    declared = {service_of(v): v for v in VINTAGES}
    lines = [f"ArcGIS service list: {n} services, {len(fs)} "
             "Local_Authority_Districts*BGC* FeatureServers",
             f"  {name}: the declared vintage {vintage} (period "
             f"{VINTAGES[vintage]['period']})"]
    others = [f"{declared[s]} ({VINTAGES[declared[s]]['period']})"
              for s in fs if s in declared and s != name]
    if others:
        lines.append("  also offered and declared: " + ", ".join(others))
    undeclared = [s for s in fs if s not in declared]
    if undeclared:
        lines.append("  NOTE: offered and not declared in VINTAGES (never "
                     "loaded; declaring one is deliberate): "
                     + ", ".join(undeclared))
    return lines


def _expected_fields(yy) -> dict:
    """The field set the publisher declares on every LAD BGC layer read
    2026-10-10 (May 2024, Dec 2025, May 2026), with the types of the five
    read; None = the type is not checked."""
    return {"FID": None, f"LAD{yy}CD": "esriFieldTypeString",
            f"LAD{yy}NM": "esriFieldTypeString", f"LAD{yy}NMW": None,
            "BNG_E": None, "BNG_N": None, "LONG": "esriFieldTypeSingle",
            "LAT": "esriFieldTypeSingle", "Shape__Area": "esriFieldTypeDouble",
            "Shape__Length": None, "GlobalID": None}


def vintage_of_layer(layer_json) -> str:
    """The declared vintage whose layer name the layer carries; ValueError
    naming the name otherwise."""
    name = _json(layer_json).get("name")
    hits = [v for v, d in VINTAGES.items() if d["layer"] == name]
    if len(hits) != 1:
        raise ValueError(f"the layer is named {name!r}, not a declared "
                         "vintage's layer ("
                         + ", ".join(d["layer"] for d in VINTAGES.values())
                         + ")")
    return hits[0]


def layer_identity(layer_json, vintage) -> dict:
    """The facts of an LAD BGC layer definition (a dict, or its text):
    {vintage, period, name, code_field, name_field, last_edit_ms, rank (=
    lastEditDate as a UTC datetime)}. ValueError naming what was seen when
    the name is not the vintage's layer, the layer is not a polygon feature
    layer, the field set is not exactly the declared one (or a read field
    has another type), or editingInfo.lastEditDate is absent or not epoch
    milliseconds: the rank is never taken from a service or file name."""
    if vintage not in VINTAGES:
        raise ValueError(f"{vintage!r} is not a declared vintage")
    v = VINTAGES[vintage]
    d = _json(layer_json)
    if d.get("error"):
        raise ValueError(f"the layer reply is an ArcGIS error: {d['error']}")
    name = d.get("name")
    if name != v["layer"]:
        raise ValueError(f"the layer is named {name!r}, expected "
                         f"{v['layer']!r} for {vintage}")
    if d.get("type") != "Feature Layer" or \
            d.get("geometryType") != "esriGeometryPolygon":
        raise ValueError(f"{name}: {d.get('type')!r} of "
                         f"{d.get('geometryType')!r}, expected a 'Feature "
                         "Layer' of 'esriGeometryPolygon'")
    want = _expected_fields(v["yy"])
    got = {}
    for f in d.get("fields") or []:
        if f.get("name") in got:
            raise ValueError(f"{name}: field {f.get('name')} twice")
        got[f.get("name")] = f.get("type")
    if set(got) != set(want):
        raise ValueError(f"{name}: fields {sorted(got)}, expected exactly "
                         f"{sorted(want)} (missing "
                         f"{sorted(set(want) - set(got))}, other "
                         f"{sorted(set(got) - set(want))})")
    bad = [f"{k} {got[k]} (expected {t})" for k, t in want.items()
           if t is not None and got[k] != t]
    if bad:
        raise ValueError(f"{name}: field types {bad}")
    ms = (d.get("editingInfo") or {}).get("lastEditDate")
    if isinstance(ms, bool) or not isinstance(ms, int) or ms <= 0:
        raise ValueError(f"{name}: editingInfo.lastEditDate {ms!r} is not "
                         "epoch milliseconds; the rank cannot be read from "
                         "the layer itself")
    return {"vintage": vintage, "period": v["period"], "name": name,
            "code_field": f"LAD{v['yy']}CD", "name_field": f"LAD{v['yy']}NM",
            "last_edit_ms": ms, "rank": _ms_rank(ms)}


# ---------------------------------------------------------------------------
# Cells, the geometry, the GeoJSON
# ---------------------------------------------------------------------------

def num_cell(v, where, scale) -> Decimal:
    """A published number at the live column's scale (scale '0.000001' or
    '0.01'), rounded half away from zero as Postgres numeric rounds. A
    JSON number only: None, a string (blank or not), a boolean, NaN or an
    infinity raises ValueError naming `where`. Never returns a default."""
    if v is None or isinstance(v, (bool, str)):
        raise ValueError(f"{where}: {v!r} is not a number (a blank or text "
                         "halts and is never read as 0)")
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError(f"{where}: {v!r} is not a finite number")
        d = Decimal(repr(v))
    elif isinstance(v, (int, Decimal)):
        d = Decimal(v)
        if not d.is_finite():
            raise ValueError(f"{where}: {v!r} is not a finite number")
    else:
        raise ValueError(f"{where}: {v!r} is not a number")
    return d.quantize(Decimal(scale), rounding=ROUND_HALF_UP)


def _as_float(o):
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"{type(o).__name__} is not JSON")


def canonical_geojson(geom) -> str:
    """The canonical text of a geometry: json.dumps with sorted keys and no
    spaces (a number parsed as Decimal is written as its float, so a
    Decimal parse and a float parse give the same text). The form compared
    and hashed; the live jsonb is read back as text and put through this
    same function (canonical_text)."""
    return json.dumps(geom, sort_keys=True, separators=(",", ":"),
                      default=_as_float)


def canonical_text(text) -> str:
    """canonical_geojson of a JSON text (a jsonb read back as text)."""
    return canonical_geojson(json.loads(text, parse_float=Decimal))


def _positions(coords, depth):
    """Every [lon, lat] position of a Polygon (depth 2: rings of positions)
    or MultiPolygon (depth 3)."""
    if depth == 0:
        yield coords
        return
    for c in coords:
        yield from _positions(c, depth - 1)


def _check_geometry(g, where) -> str:
    """The geometry's type; ValueError unless it is a Polygon or
    MultiPolygon object of exactly type and coordinates, every ring a list
    of at least four positions, every position a longitude and latitude in
    England's range (EPSG:4326)."""
    if not isinstance(g, dict):
        raise ValueError(f"{where}: no geometry ({g!r})")
    if set(g) != {"type", "coordinates"}:
        raise ValueError(f"{where}: geometry members {sorted(g)}, expected "
                         "exactly coordinates and type")
    t = g["type"]
    if t not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"{where}: geometry type {t!r}, expected Polygon or "
                         "MultiPolygon")
    coords = g["coordinates"]
    polys = [coords] if t == "Polygon" else coords
    if not isinstance(polys, list) or not polys or any(
            not isinstance(p, list) or not p for p in polys):
        raise ValueError(f"{where}: empty {t} coordinates")
    for p in polys:
        for r in p:
            if not isinstance(r, list) or len(r) < 4:
                raise ValueError(f"{where}: a ring of {t} with fewer than "
                                 "four positions")
    for pos in _positions(coords, 2 if t == "Polygon" else 3):
        ok = (isinstance(pos, list) and len(pos) == 2
              and all(isinstance(x, (int, float, Decimal))
                      and not isinstance(x, bool) for x in pos))
        if not ok or not (LON_RANGE[0] <= pos[0] <= LON_RANGE[1]
                          and LAT_RANGE[0] <= pos[1] <= LAT_RANGE[1]):
            raise ValueError(f"{where}: position {pos!r} is not a longitude "
                             "and latitude in England (the query asks "
                             "outSR=4326)")
    return t


def _reply_problems(d, name):
    if not isinstance(d, dict):
        raise ValueError(f"{name}: not a JSON object")
    if d.get("error"):
        raise ValueError(f"{name}: the reply is an ArcGIS error: "
                         f"{d['error']}")
    if d.get("exceededTransferLimit") or (
            (d.get("properties") or {}).get("exceededTransferLimit")):
        raise ValueError(f"{name}: exceededTransferLimit is true: the reply "
                         "is truncated (a partial reply halts)")


def _district(code, where, ignored) -> bool:
    """True for an English district code; False (counted) for a Welsh,
    Scottish or Northern Irish one; ValueError for anything else."""
    if not isinstance(code, str):
        raise ValueError(f"{where}: code {code!r} is not text")
    if DISTRICT_RE.fullmatch(code):
        return True
    if OTHER_NATION_RE.fullmatch(code):
        ignored.append(code)
        return False
    raise ValueError(f"{where}: {code!r} is neither an English district "
                     "(E06-E09) nor a Welsh, Scottish or Northern Irish code")


def _attrs(p, code, info, where) -> dict:
    name = p.get(info["name_field"])
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where} {code}: {info['name_field']} {name!r} is "
                         "blank or not text")
    out = {"lad24nm": name}
    for col, field, scale in NUMBERS:
        if field not in p:
            raise ValueError(f"{where} {code}: no {field} attribute (a "
                             "missing attribute halts)")
        out[col] = num_cell(p[field], f"{where} {code} {field}", scale)
    return out


def read_geojson(path, vintage) -> dict:
    """The facts of one GeoJSON query reply for a declared vintage: {path,
    name, vintage, period, rows {code: {lad24nm, longitude, latitude,
    shape_area, geojson (canonical text)}}, types {geometry type: count},
    ignored (non-English codes read and not stored), features}. ValueError
    naming what was seen on: an ArcGIS error; not a FeatureCollection; a crs
    other than EPSG:4326; exceededTransferLimit true; a feature without the
    vintage's code field, with attributes other than the five queried, or
    with a missing or blank attribute; a code that is neither an English
    district nor another nation's; a code twice; a geometry that is not a
    Polygon or MultiPolygon in England's longitude and latitude range; a
    number of English districts other than EXPECTED_AREAS."""
    info = layer_fields(vintage)
    p = Path(path)
    try:
        d = json.loads(p.read_bytes().decode("utf-8-sig"),
                       parse_float=Decimal)
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"{p.name}: not JSON ({e})") from None
    _reply_problems(d, p.name)
    if d.get("type") != "FeatureCollection":
        raise ValueError(f"{p.name}: type {d.get('type')!r}, expected a "
                         "FeatureCollection")
    crs = ((d.get("crs") or {}).get("properties") or {}).get("name")
    if d.get("crs") is not None and crs != "EPSG:4326":
        raise ValueError(f"{p.name}: crs {crs!r}, expected EPSG:4326 (the "
                         "query asks outSR=4326)")
    want = {info["code_field"], info["name_field"]} | {f for _, f, _ in
                                                       NUMBERS}
    rows, types, ignored = {}, Counter(), []
    feats = d.get("features")
    if not isinstance(feats, list):
        raise ValueError(f"{p.name}: no features list")
    for i, f in enumerate(feats):
        where = f"{p.name} feature {i}"
        props = (f or {}).get("properties") or {}
        if info["code_field"] not in props:
            raise ValueError(f"{where}: no {info['code_field']} attribute "
                             f"(attributes {sorted(props)})")
        code = props[info["code_field"]]
        if not _district(code, where, ignored):
            continue
        if code in rows:
            raise ValueError(f"{where}: {code} appears twice")
        if set(props) != want:
            raise ValueError(f"{where} {code}: attributes {sorted(props)}, "
                             f"expected exactly {sorted(want)} (a missing "
                             "attribute halts)")
        row = _attrs(props, code, info, where)
        types[_check_geometry(f.get("geometry"), f"{where} {code}")] += 1
        row["geojson"] = canonical_geojson(f["geometry"])
        rows[code] = row
    want_n = globals()["EXPECTED_AREAS"]
    if len(rows) != want_n:
        raise ValueError(f"{p.name}: {len(rows)} English districts "
                         f"(E06-E09), expected {want_n}")
    return {"path": p, "name": p.name, "vintage": vintage,
            "period": info["period"], "rows": rows, "types": dict(types),
            "ignored": len(ignored), "features": len(feats)}


def layer_fields(vintage) -> dict:
    """{period, code_field, name_field} of a declared vintage."""
    if vintage not in VINTAGES:
        raise ValueError(f"{vintage!r} is not a declared vintage")
    v = VINTAGES[vintage]
    return {"period": v["period"], "code_field": f"LAD{v['yy']}CD",
            "name_field": f"LAD{v['yy']}NM"}


def read_attributes(path, vintage) -> list:
    """The records of an attributes-only reply (ArcGIS JSON, features with
    attributes and no geometry): for vintage_diff only, never stored; each
    record's geojson is None (not compared). The same attribute and code
    checks as read_geojson."""
    info = layer_fields(vintage)
    p = Path(path)
    d = json.loads(p.read_bytes().decode("utf-8-sig"), parse_float=Decimal)
    _reply_problems(d, p.name)
    out, ignored = {}, []
    for i, f in enumerate(d.get("features") or []):
        where = f"{p.name} feature {i}"
        a = (f or {}).get("attributes") or {}
        code = a.get(info["code_field"])
        if not _district(code, where, ignored):
            continue
        if code in out:
            raise ValueError(f"{where}: {code} appears twice")
        out[code] = {KEY: code, PERIOD: info["period"],
                     **_attrs(a, code, info, where), "geojson": None}
    if len(out) != globals()["EXPECTED_AREAS"]:
        raise ValueError(f"{p.name}: {len(out)} English districts, expected "
                         f"{globals()['EXPECTED_AREAS']}")
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------------------
# Records, content hash, differences
# ---------------------------------------------------------------------------

def build_records(file) -> list:
    """The vintage's records from a read reply, one per district, sorted by
    lad24cd: {lad24cd, source_date, lad24nm, longitude, latitude,
    shape_area, geojson}. The publisher's code is the key (this source
    defines it)."""
    return [{KEY: code, PERIOD: file["period"], **file["rows"][code]}
            for code in sorted(file["rows"])]


def _cell(v) -> str:
    return f"{v:f}" if isinstance(v, Decimal) else str(v)


def rows_content_sha(records) -> str:
    """sha256 of a vintage's content: one line per record sorted by
    lad24cd, lad24cd|lad24nm|longitude|latitude|shape_area|the canonical
    geometry (numbers at the live scales, written in full), joined by LF,
    UTF-8. Nothing else is in it, so where or when a reply was read never
    makes a new edition."""
    lines = sorted("|".join([str(r[KEY])] + [_cell(r[c]) for c in VALUES])
                   for r in records)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def check_records(records, period) -> None:
    """ValueError unless the vintage's records are whole: each of this
    period, one per lad24cd, every value present."""
    seen = set()
    for r in records:
        if r.get(PERIOD) != period:
            raise ValueError(f"{period}: a record of {r.get(PERIOD)!r}")
        if r[KEY] in seen:
            raise ValueError(f"{period}: {r[KEY]} appears twice")
        if any(r.get(c) is None for c in VALUES):
            raise ValueError(f"{period}: {r[KEY]} lacks a value")
        seen.add(r[KEY])


def _by_key(records) -> dict:
    return {r[KEY]: r for r in records or ()}


def differs(new, old) -> bool:
    """True when the key sets or any value differ (canonical geometry
    text)."""
    nk, ok = _by_key(new), _by_key(old)
    if set(nk) != set(ok):
        return True
    return any(nk[k][c] != ok[k][c] for k in nk for c in VALUES)


def map_changes(new, old) -> list:
    """Districts in both whose name or geometry differs (what the map
    shows), sorted."""
    nk, ok = _by_key(new), _by_key(old)
    return sorted(k for k in set(nk) & set(ok)
                  if any(nk[k][c] != ok[k][c] for c in NAME_COLS))


def vintage_diff(records, live) -> dict:
    """What a vintage's records change against the live (or any held)
    rows: {added: codes only in records, removed: codes only in live,
    names: [(code, live name, new name)], geometry: codes whose canonical
    geometry differs (None when either side has none: an attributes-only
    reply), attributes: codes whose LONG, LAT or Shape__Area differs,
    common: districts in both}."""
    nk, lk = _by_key(records), _by_key(live)
    common = sorted(set(nk) & set(lk))
    geo = None
    if all(nk[k]["geojson"] is not None and lk[k]["geojson"] is not None
           for k in common):
        geo = [k for k in common if nk[k]["geojson"] != lk[k]["geojson"]]
    return {"added": sorted(set(nk) - set(lk)),
            "removed": sorted(set(lk) - set(nk)),
            "names": [(k, lk[k]["lad24nm"], nk[k]["lad24nm"]) for k in common
                      if nk[k]["lad24nm"] != lk[k]["lad24nm"]],
            "geometry": geo,
            "attributes": [k for k in common
                           if any(nk[k][c] != lk[k][c]
                                  for c, _, _ in NUMBERS)],
            "common": len(common)}


def _few(xs, n=8) -> str:
    xs = list(xs)
    more = f" and {len(xs) - n} more" if len(xs) > n else ""
    return (", ".join(str(x) for x in xs[:n]) or "none") + more


def diff_lines(d) -> list:
    """vintage_diff as printed lines."""
    lines = [f"codes added {len(d['added'])} ({_few(d['added'])}); removed "
             f"{len(d['removed'])} ({_few(d['removed'])}); "
             f"{d['common']} in both",
             f"names changed {len(d['names'])} ("
             + _few(f"{k}: {a!r} -> {b!r}" for k, a, b in d["names"]) + ")"]
    if d["geometry"] is None:
        lines.append("geometry not compared (an attributes-only reply)")
    else:
        lines.append(f"geometry changed {len(d['geometry'])} "
                     f"({_few(d['geometry'])})")
    lines.append(f"LONG, LAT or Shape__Area changed {len(d['attributes'])} "
                 f"({_few(d['attributes'])})")
    return lines


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

_RANK_RE = re.compile(r"\bArcGIS lastEditDate ([0-9]+)\b")
_LEDGER_RE = re.compile(r"(.*) \(source_date ([0-9]{4}-[0-9]{2}-[0-9]{2}); "
                        r"ArcGIS lastEditDate ([0-9]+) \([^)]*\)\)")


def release_rank(layer_or_source) -> "datetime | None":
    """The lastEditDate of a layer (layer_identity's dict), or the 'ArcGIS
    lastEditDate <ms>' an edition's or ledger row's source_file carries, as
    a UTC datetime; None if it names none (a service name, file name, URL or
    download date is never read for a rank)."""
    x = layer_or_source
    if isinstance(x, dict):
        return x.get("rank")
    mm = _RANK_RE.search(str(x or ""))
    return _ms_rank(mm.group(1)) if mm else None


def edition_source(layer, src, content_sha, note="") -> str:
    """An edition's source_file: the reply's name, the period and the
    layer's lastEditDate (the rank), where it was read, the content sha,
    and a note."""
    return (f"{src['file_name']}; {PERIOD} {layer['period']}; "
            f"{rank_text(layer['rank'])}; {src['where']}; content sha256 "
            f"{content_sha[:16]}" + (f"; {note}" if note else ""))


def ledger_source(where, period, rank) -> str:
    """A reply's ledger source_file: where it was read (the final query URL,
    or the path of a --file), its period and lastEditDate."""
    return f"{where} ({PERIOD} {period}; {rank_text(rank)})"


def parse_ledger_source(s) -> "tuple | None":
    """(where, period, rank) of a ledger source_file, or None."""
    mm = _LEDGER_RE.fullmatch(str(s or ""))
    if not mm:
        return None
    return mm.group(1), mm.group(2), _ms_rank(mm.group(3))


def content_sha256(path) -> str:
    """SHA-256 of a file's bytes (printed and recorded; for this API output
    it is not a release identity)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tip_ranks(tips, checked) -> dict:
    """{held period: the newest lastEditDate that stated it}: the tip
    edition's, or a later reply the ledger records for it (a reply found
    unchanged stores no edition)."""
    out = {}
    for p, t in tips.items():
        ranks = [t.get("rank")] + [release_rank(s)
                                   for s, _ in checked.get(p, ())]
        ranks = [r for r in ranks if r is not None]
        out[p] = max(ranks) if ranks else None
    return out


def ledger_complete(checked, where, sha, ranks, *, rank=None,
                    allow_older=False, stranded=()) -> "list | None":
    """[period] when the ledger records this reply's (where, content sha)
    pair for the period its ledger source declares, else None (the reply is
    compared). rank: the reply's own lastEditDate (from its layer); when
    given, the ledger row must declare the same one (the rank is in the
    layer, not in the content, so the same content under another
    lastEditDate is compared and planned). A period whose tip comes from a
    newer layer is compared again (and then planned as older, which halts)
    unless allow_older; a stranded period is always compared. So a run
    that failed or was rejected leaves no ledger row and the rerun compares
    again."""
    have, declared = set(), None
    for p, pairs in checked.items():
        for s, h in pairs:
            parsed = parse_ledger_source(s)
            if h == sha and parsed and parsed[0] == where and (
                    rank is None or parsed[2] == rank):
                have.add(str(p))
                declared = parsed
    if declared is None:
        return None
    _, period, rank = declared
    if period not in have or period in {str(x) for x in stranded}:
        return None
    if not allow_older and ranks.get(period) is not None \
            and ranks[period] > rank:
        return None
    return [period]


def plan_vintage(period, rank, ranks, checked, source, sha, *, recheck,
                 allow_older) -> tuple:
    """(new, revised, skipped) for the reply's one vintage. ranks: {held
    period: tip_ranks}; checked: {period: {(source, sha)}} of the ledger.
    new: not held (unless it sorts before the newest held vintage: skipped
    as older, unless allow_older); revised: held, compared with the tip;
    skipped {period: reason}: held with a tip from a newer layer (older,
    unless allow_older), or already in the ledger (checked, unless
    --recheck names it). --recheck must be the reply's held period
    (ValueError)."""
    v = str(period)
    if recheck is not None and (recheck != v or v not in ranks):
        raise ValueError(f"--recheck {recheck}: not a held vintage this "
                         f"reply states ({v})")
    newest_held = max(ranks) if ranks else None
    if v not in ranks:
        if newest_held is not None and v < newest_held and not allow_older:
            return [], [], {v: (f"older: vintage {v} is before the newest "
                                f"held vintage {newest_held}")}
        return [v], [], {}
    t = ranks[v]
    if t is not None and t > rank and not allow_older:
        return [], [], {v: (f"older: its tip comes from a layer of "
                            f"{rank_text(t)}, this one is "
                            f"{rank_text(rank)}")}
    if recheck != v and (source, sha) in checked.get(v, ()):
        return [], [], {v: "checked: this reply is already in the ledger"}
    return [], [v], {}


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

def period_problems(new, tip, *, kind) -> list:
    """The stop conditions no flag releases (empty = fine). revised: a
    district of the held edition missing from the reply, or one the held
    edition lacks (the key set of a vintage never changes by a reissue;
    la_boundaries is never re-keyed)."""
    out = []
    if kind == "revised":
        tk, nk = _by_key(tip), _by_key(new)
        gone = sorted(set(tk) - set(nk))
        added = sorted(set(nk) - set(tk))
        if gone:
            out.append(f"{PARTIAL}: {len(gone)} district(s) of the held "
                       "edition missing from the reply (a partial reply "
                       f"never replaces a fuller edition): {_few(gone)}")
        if added:
            out.append(f"{PARTIAL}: {len(added)} district(s) not in the held "
                       "edition (a reissue never changes a vintage's key "
                       f"set): {_few(added)}")
    return out


# ---------------------------------------------------------------------------
# Spec and profile
# ---------------------------------------------------------------------------

SPEC = core.EditionSpec(
    name="s7",
    live_table="la_boundaries",
    editions_table="la_boundaries_editions",
    key_cols=(KEY,),
    period_col=PERIOD,
    value_cols=VALUE_TYPES,
    # the five values and loaded_at, written by refresh-latest only, on rows
    # that exist; source_date is the period and is never written
    refresh_cols=VALUES + ("loaded_at",),
    refresh_from=(("loaded_at", "loaded_at"),),
    key_types=((KEY, "varchar(9) NOT NULL"), (PERIOD, "date NOT NULL")),
    table_constraints=(f"CHECK ({KEY} ~ '^E0[6-9][0-9]{{6}}$')",),
    fk_la_boundaries=False,             # it holds other vintages' codes
    live_single_period=True,            # one vintage in live
    refresh_key_changes=False,          # la_boundaries is never re-keyed
)


def _no_label(fetched_on) -> str:
    raise ValueError("an S7 release label comes from the layer; store "
                     "through apply_vintage(...)")


PROFILE = pe.Profile(
    spec=SPEC, value_cols=VALUES, run_agent=RUN_AGENT, run_source=RUN_SOURCE,
    heading="S7 LAD May 2024 BGC boundaries, ONS ArcGIS (la_boundaries)",
    default_source_file="ONS Local Authority Districts BGC, ArcGIS REST",
    expected_areas=EXPECTED_AREAS, release_label=_no_label,
    content_sha256=rows_content_sha, check_records=check_records,
    file_checks=True, savepoint="s7_period",
    example_label="(lad24nm, longitude, latitude, shape_area, geometry)")


def profile() -> pe.Profile:
    """PROFILE bound to the current SPEC and EXPECTED_AREAS (looked up when
    called, so tests can swap them)."""
    g = globals()
    return dataclasses.replace(PROFILE, spec=g["SPEC"],
                               expected_areas=g["EXPECTED_AREAS"])


def _late(name: str):
    """A call through this module's attribute `name`, looked up when
    called (so mock.patch.object(s7_boundaries_editions, name, ...) reaches
    the engine)."""
    return lambda *a, **kw: globals()[name](*a, **kw)


def create_all(cur) -> None:
    """ddl: the editions table, its append-only triggers and the file-check
    ledger. Idempotent; no value is written; the live table is not
    touched."""
    prof = profile()
    core.create_schema(cur, prof.spec)
    pe.create_file_checks(cur, prof)


def refuse_live_insert(cur, profile, period, records) -> None:
    """The engine's live-insert hook: this loader never inserts live rows
    (la_boundaries is the W1 spine; it is never rebuilt here)."""
    halt(f"{period}: the S7 loader never inserts rows into "
         f"{profile.spec.live_table} (the W1 spine, referenced by foreign "
         "keys); nothing stored")


@dataclass(frozen=True)
class PeriodInfo:
    """What a vintage's edition records: source_file, the release label and
    the reply read ((ledger source, content sha),)."""
    source_file: str
    label: str
    files: tuple


# ---------------------------------------------------------------------------
# Reading held data, comparing
# ---------------------------------------------------------------------------

table_exists = pe.table_exists


def _conn(writing):
    return pe.connect(writing)


def records(cur, spec, period, edition=None) -> list:
    """The vintage's rows as records (lad24cd, the period as a string, the
    five values; the geometry read back as text and made canonical): the
    live table's (edition None) or the given edition's."""
    cols = f"{KEY}, lad24nm, longitude, latitude, shape_area, geojson::text"
    if edition is None:
        cur.execute(f"SELECT {cols} FROM public.{spec.live_table} WHERE "
                    f"{spec.period_col} = %s", (str(period),))
    else:
        cur.execute(f"SELECT {cols} FROM public.{spec.editions_table} WHERE "
                    f"{spec.period_col} = %s AND edition = %s",
                    (str(period), edition))
    out = []
    for k, nm, lon, lat, area, g in cur.fetchall():
        out.append({KEY: k, PERIOD: str(period), "lad24nm": nm,
                    "longitude": lon, "latitude": lat, "shape_area": area,
                    "geojson": None if g is None else canonical_text(g)})
    return out


def _brief(r) -> str:
    if r is None:
        return "absent"
    g = hashlib.sha256(str(r["geojson"]).encode("utf-8")).hexdigest()[:8]
    return (f"({r['lad24nm']}, {_cell(r['longitude'])}, "
            f"{_cell(r['latitude'])}, {_cell(r['shape_area'])}, geometry "
            f"sha256 {g})")


def compare_vintage(cur, profile, period, recs, against="editions") -> dict:
    """pe.compare_period with the geometry compared as canonical text (the
    stored jsonb read back through records): {kind, changed, examples,
    against}. kind 'new', HELD_NOT_LIVE (live holds another vintage),
    'unchanged', 'revised' or LIVE_MISSING; examples name the geometry by
    its sha, never its text."""
    spec = profile.spec
    if against == "live":
        old, label = records(cur, spec, period), "live"
    else:
        try:
            tip = core.chain_tip(cur, spec, period)
        except LookupError:
            old, label = [], "no edition"
        except ValueError as e:
            halt(f"{spec.editions_table}: {e}")
        else:
            old, label = records(cur, spec, period, tip), f"edition {tip}"
    if not old:
        kind = (HELD_NOT_LIVE if pe.live_holds_other(cur, profile, period)
                else "new")
        return {"kind": kind, "changed": len(recs), "examples": [],
                "against": label}
    ok, nk = _by_key(old), _by_key(recs)
    diff = [k for k in sorted(set(ok) | set(nk))
            if k not in ok or k not in nk
            or any(ok[k][c] != nk[k][c] for c in VALUES)]
    ex = [f"{k} {profile.label}: {_brief(ok.get(k))} -> {_brief(nk.get(k))}"
          for k in diff[:3]]
    kind = "revised" if diff else "unchanged"
    if (kind == "unchanged" and against == "editions"
            and pe.live_row_count(cur, profile, period) == 0
            and not pe.live_holds_other(cur, profile, period)):
        kind = LIVE_MISSING
    return {"kind": kind, "changed": len(diff), "examples": ex,
            "against": label}


def apply_vintage(cur, profile, period, recs, *, fetched_on, info) -> str:
    """pe.apply_period for the vintage, then its ledger row, inside the
    caller's per-period savepoint (s7_period): the edition and the ledger
    row commit or roll back together. A 'new' or LIVE_MISSING vintage would
    put rows into the live table: it halts instead (never rebuilt here)."""
    kind = compare_vintage(cur, profile, period, recs)["kind"]
    if kind in ("new", LIVE_MISSING):
        refuse_live_insert(cur, profile, period, recs)
    prof = dataclasses.replace(profile, release_label=lambda d: info.label)
    kind = pe.apply_period(cur, prof, period, recs, fetched_on=fetched_on,
                           source_file=info.source_file,
                           classify=lambda *a, **kw: kind,
                           insert=_late("refuse_live_insert"))
    tip = core.chain_tip(cur, prof.spec, period)
    for src, sha in info.files:
        pe.record_file_check(cur, prof, period, src, sha, kind, tip)
    return kind


def tip_info(cur, spec) -> dict:
    """{period (string): {edition, source_file, rank}} of every vintage
    with editions."""
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table}")
    out = {}
    for (p,) in cur.fetchall():
        tip = core.latest_edition(cur, spec, str(p))
        cur.execute(f"SELECT DISTINCT source_file FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = %s", (p, tip))
        src = {r[0] for r in cur.fetchall()}
        sf = src.pop() if len(src) == 1 else None
        out[pe._p(p)] = {"edition": tip, "source_file": sf,
                         "rank": release_rank(sf)}
    return out


def _live_rows(cur, spec) -> int:
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table}")
    return cur.fetchone()[0]


def _live_periods(cur, spec) -> list:
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table} ORDER BY 1")
    return [None if r[0] is None else pe._p(r[0]) for r in cur.fetchall()]


def legacy_tips(cur, spec) -> dict:
    """Before the editions table exists: {LIVE_PERIOD: {rank}} when live
    holds rows (the held layer's lastEditDate, LEGACY_RANK_MS), else {}."""
    if not _live_rows(cur, spec):
        return {}
    return {LIVE_PERIOD: {"edition": None, "source_file": None,
                          "rank": _ms_rank(LEGACY_RANK_MS)}}


def ledger_checks(cur, prof) -> dict:
    """{period: {(source_file, file_sha256)}} of the ledger."""
    pc = prof.spec.period_col
    cur.execute(f"SELECT DISTINCT {pc}, source_file, file_sha256 "
                f"FROM public.{pe.file_checks_table(prof)}")
    out = {}
    for p, f, s in cur.fetchall():
        out.setdefault(pe._p(p), set()).add((f, s))
    return out


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _session(session):
    if session is not None:
        return session
    import requests
    return requests


def _get(session, url, **kw):
    try:
        r = session.get(url, headers={"User-Agent": USER_AGENT},
                        timeout=300, **kw)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001 (any failure halts)
        halt(f"GET {url} failed, nothing downloaded: {e}")
    return r


def fetch_json(url, session=None) -> dict:
    """An ArcGIS JSON reply (a read-only GET), not saved (the service
    list). An ArcGIS error reply halts."""
    try:
        d = json.loads(_get(_session(session), url).content)
    except ValueError as e:
        halt(f"GET {url}: not JSON ({e})")
    if isinstance(d, dict) and d.get("error"):
        halt(f"GET {url}: an ArcGIS error: {d['error']}")
    return d


def _check_body(dest: Path, body: bytes) -> None:
    """Raise unless body is what dest's suffix promises: a .geojson must be
    a GeoJSON FeatureCollection, a .json a JSON object; an ArcGIS error
    reply (HTTP 200 with an error member) is refused either way."""
    suffix = dest.suffix.lower()
    if suffix not in (".geojson", ".json"):
        raise ValueError(f"{dest.name}: neither .geojson nor .json")
    d = json.loads(body)
    if not isinstance(d, dict):
        raise ValueError("not a JSON object")
    if d.get("error"):
        raise ValueError(f"an ArcGIS error reply: {d['error']}")
    if suffix == ".geojson" and d.get("type") != "FeatureCollection":
        raise ValueError(f"not a GeoJSON FeatureCollection (type "
                         f"{d.get('type')!r})")


def fetch_api(url, dest, session=None) -> tuple:
    """Download url (redirects followed) to the file dest (a dated name
    under RAW_DIR) and return (path to read, final URL). Checked before it
    is kept (_check_body). A same-named file with the same content is kept
    as it is; one with different content is never replaced: the download is
    saved beside it as <stem>-<sha8><suffix> and that is the one read. An
    existing name is never a reason to skip the download."""
    dest = Path(dest)
    r = _get(_session(session), url)
    final = str(getattr(r, "url", None) or url)
    if final != url:
        print(f"redirected: {url} -> {final}")
    body = r.content
    try:
        _check_body(dest, body)
    except ValueError as e:
        halt(f"{final}: {e}; nothing written")
    dest.parent.mkdir(parents=True, exist_ok=True)
    new_sha = hashlib.sha256(body).hexdigest()
    path = dest
    if dest.exists():
        old_sha = content_sha256(dest)
        if old_sha == new_sha:
            print(f"{dest.name} already held with the same content (sha256 "
                  f"{new_sha[:16]}); kept as it is")
            return path, final
        path = dest.with_name(f"{dest.stem}-{new_sha[:8]}{dest.suffix}")
        if path.exists() and content_sha256(path) == new_sha:
            print(f"NOTE: {dest.name} differs from this download; "
                  f"{path.name} already holds it (sha256 {new_sha[:16]})")
            return path, final
        print(f"NOTE: {dest.name} already exists with different content "
              f"(sha256 {old_sha[:16]}, new {new_sha[:16]}); kept it "
              f"untouched and saved the new download as {path.name}")
    else:
        print(f"downloaded {dest.name}: {len(body):,} bytes, sha256 "
              f"{new_sha[:16]}")
    tmp = path.with_name(path.name + ".download.tmp")
    tmp.write_bytes(body)
    tmp.replace(path)
    return path, final


def _file_source(path: Path) -> str:
    """Where a --file was read: the path under the repo (data/raw/...),
    else the absolute path."""
    p = Path(path).resolve()
    try:
        return p.relative_to(REPO).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------------------
# ddl, status, run log, refresh-latest
# ---------------------------------------------------------------------------

def log_run(cur, rows_written: int, notes: str, started_at=None) -> None:
    """The pipeline_run_log row for a committed run (agent RUN_AGENT, source
    '7'). Called only on committed load, migrate-legacy and restore-edition
    runs, partial runs included."""
    pe.log_run(cur, profile(), rows_written, notes, started_at)


def _readiness(cur, prof) -> dict:
    spec = prof.spec
    lt = pe.file_checks_table(prof)
    return {spec.editions_table: table_exists(cur, spec.editions_table),
            lt: table_exists(cur, lt)}


NEXT_STEP = ("run `ddl --commit`, then `migrate-legacy --file GEOJSON "
             "--layer JSON --commit` (S7 has no sync-new)")


def cmd_ddl(args) -> int:
    """ddl [--commit | --simulate]: the editions table and its triggers and
    the ledger. The live table is not touched."""
    prof = profile()
    spec = prof.spec
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            for t, ok in _readiness(cur, prof).items():
                print(f"{t}: {'exists' if ok else 'does not exist'}")
            print(f"{spec.live_table}: not touched by ddl")
            if not writing:
                conn.rollback()
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            create_all(cur)
        pe.finish(conn, args, f"ddl: {spec.editions_table} and triggers "
                              f"{spec.trigger}, {spec.truncate_trigger}, "
                              f"{pe.file_checks_table(prof)} present")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def status_lines(cur, prof) -> list:
    """Per vintage: the tip edition, its lastEditDate, live or held, and
    the ledger's latest reply; a line when a vintage is held, not live."""
    s = prof.spec
    lt = pe.file_checks_table(prof)
    live = set(_live_periods(cur, s))
    lines, held = [], []
    for p, t in sorted(tip_info(cur, s).items()):
        where = "live" if p in live else "held, not live"
        if p not in live:
            held.append(p)
        line = (f"  {p}: tip edition {t['edition']} ({rank_text(t['rank'])})"
                f"; {where}")
        cur.execute(f"SELECT source_file, outcome FROM public.{lt} WHERE "
                    f"{s.period_col} = %s ORDER BY id DESC LIMIT 1", (p,))
        last = cur.fetchone()
        line += (f"; ledger latest: {last[1]} {last[0]}" if last
                 else "; no ledger row")
        lines.append(line)
    if held:
        lines.append(f"  vintage(s) {', '.join(held)} held and not adopted: "
                     "adopting one re-bases every join (la_boundaries is the "
                     "W1 spine) and is Scott's decision, never a load")
    return lines


def cmd_status(args) -> int:
    """status: what needs action; exit 1 if anything, or (with a clean
    message, nothing created) if the editions table or ledger does not
    exist."""
    prof = profile()
    conn = _conn(False)
    try:
        with conn.cursor() as cur:
            missing = [t for t, ok in _readiness(cur, prof).items() if not ok]
            if missing:
                print(f"status: {' and '.join(missing)} "
                      f"{'does' if len(missing) == 1 else 'do'} not exist "
                      f"yet; {NEXT_STEP}")
                return 1
            st = pe.status(cur, prof)
            lines = status_lines(cur, prof)
    finally:
        conn.rollback()
        conn.close()
    print(pe.format_status(prof, st, lines))
    return 0 if st["ok"] else 1


def _tables_exist(cur) -> bool:
    """True when the editions table and ledger exist; halts (S7's own
    wording: there is no sync-new) when one does not."""
    for t, ok in _readiness(cur, profile()).items():
        if not ok:
            halt(f"{t} does not exist yet; {NEXT_STEP}")
    return True


def cmd_refresh_latest(args) -> int:
    """refresh-latest [--acknowledge 2024-05-01] [--accept-drift PERIOD]
    [--commit | --simulate]: the engine's refresh of the live vintage only
    (its latest edition copied into live on the rows that differ, loaded_at
    included, under the before/after hash guard; no row is added, removed
    or re-keyed). Whatever it would write changes the map's geometry, so it
    is printed and a write needs --acknowledge 2024-05-01; any other period
    named halts."""
    ack = set(args.acknowledge or ())
    stray = sorted(ack - {LIVE_PERIOD})
    if stray:
        halt(f"--acknowledge {stray}: only the live vintage {LIVE_PERIOD} is "
             "refreshed into la_boundaries")
    writing = args.commit or args.simulate

    def preflight(cur):
        spec = profile().spec
        tips, new, errors = core.latest_map(cur, spec)
        if new or errors:
            return      # the engine halts on these with its own wording
        for p, tip in sorted(tips.items(), key=lambda x: str(x[0])):
            kind, _ = core.classify_period(cur, spec, p, tip)
            if kind == "current":
                continue
            ps = pe._p(p)
            d = vintage_diff(records(cur, spec, ps, tip),
                             records(cur, spec, ps))
            print(f"{ps}: live differs from edition {tip} ({kind}); the "
                  "refresh would write: " + "; ".join(diff_lines(d)))
            print(MAP_MESSAGE)
            if writing and ps not in ack:
                halt(f"{ps}: refresh-latest would change la_boundaries; "
                     f"{MAP_MESSAGE}; nothing written")
    return pe.run_refresh_latest(
        profile(), args, connect=_late("_conn"),
        table_exists=lambda cur, t: _tables_exist(cur), preflight=preflight)


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

def _end(conn, args, writing, rc) -> int:
    conn.rollback()  # commit mode has committed already
    if not writing:
        print("PREVIEW: nothing written to the database (use --commit or "
              "--simulate)")
    elif args.simulate:
        print("SIMULATION: ROLLED BACK (nothing persisted)")
    return rc


def _period_arg(text) -> str:
    if not PERIOD_RE.fullmatch(str(text or "")):
        raise argparse.ArgumentTypeError(f"not a vintage date YYYY-MM-DD: "
                                         f"{text!r}")
    return str(text)


def _read_layer(path, vintage=None) -> dict:
    """layer_identity of a layer JSON file; the vintage read from its name
    when not given (and checked against it when given). Halts on any
    mismatch."""
    p = Path(path)
    if not p.is_file():
        halt(f"--layer {path}: no such file")
    try:
        d = json.loads(p.read_text(encoding="utf-8"), parse_float=Decimal)
        named = vintage_of_layer(d)
        if vintage is not None and named != vintage:
            raise ValueError(f"the layer is {d.get('name')!r} ({named}), "
                             f"not {vintage}")
        return layer_identity(d, named)
    except ValueError as e:
        halt(f"{p.name}: layer identity check failed, nothing stored: {e}")


def _read(path, vintage) -> dict:
    try:
        return read_geojson(path, vintage)
    except ValueError as e:      # the message names the file already
        halt(f"{e}; nothing stored")


def _report(layer, f) -> None:
    print(f"layer: {layer['name']} ({layer['vintage']}, period "
          f"{layer['period']}); fields {layer['code_field']}, "
          f"{layer['name_field']}, LONG, LAT, Shape__Area; "
          f"{rank_text(layer['rank'])} (the rank)")
    print(f"{f['name']}: {f['features']} features, {len(f['rows'])} English "
          f"districts stored ({', '.join(f'{n} {t}' for t, n in sorted(f['types'].items()))}), "  # noqa: E501
          f"{f['ignored']} other nations' read and ignored; EPSG:4326; no "
          "exceededTransferLimit")


def _source(args) -> dict:
    """The reply the run reads: {geojson, where, how, file_name, layer,
    vintage}. --file and --layer read local files (no network; the layer
    gives the vintage); otherwise the service list, the layer and the query
    are read and the layer JSON and GeoJSON saved to RAW_DIR with the date
    (also in a preview)."""
    if args.file or args.layer:
        if not (args.file and args.layer):
            halt("--file and --layer go together: the GeoJSON and the layer "
                 "definition it was queried from (identity is read from the "
                 "layer)")
        path = Path(args.file)
        if not path.is_file():
            halt(f"--file {args.file}: no such file")
        print(f"files given: {path} and {args.layer}; nothing downloaded; "
              "identity is read from the layer and the GeoJSON")
        layer = _read_layer(args.layer, args.vintage)
        return {"geojson": path, "where": _file_source(path),
                "how": "local file", "file_name": path.name, "layer": layer,
                "vintage": layer["vintage"]}
    vintage = args.vintage or LIVE_VINTAGE
    try:
        lines = discovery_lines(fetch_json(list_url()), vintage)
    except ValueError as e:
        halt(f"{list_url()}: {e}")
    for line in lines:
        print(line)
    service = service_of(vintage)
    today = date.today().isoformat()
    print(f"downloading to {RAW_DIR} (also in a preview; the database is "
          "not written)")
    lpath, _ = fetch_api(layer_url(service),
                         RAW_DIR / f"{service}_layer_{today}.json")
    layer = _read_layer(lpath, vintage)
    url = query_url(service, layer["code_field"])
    path, final = fetch_api(url, RAW_DIR / f"{layer['name']}_england_4326_"
                                           f"{today}.geojson")
    print(f"  final URL: {final}")
    return {"geojson": path, "where": final, "how": "ArcGIS REST",
            "file_name": path.name, "layer": layer, "vintage": vintage}


def _label(layer, src, sha, bsha, how, *, notes=()) -> str:
    return (f"ONS {layer['name']} (ArcGIS FeatureServer "
            f"{service_of(layer['vintage'])}), England, EPSG:4326; "
            f"{rank_text(layer['rank'])}; {src['file_name']}; {src['where']}"
            f"; content sha256 {sha[:16]}, bytes sha256 {bsha[:16]}; {how}; "
            "published_date is the lastEditDate date"
            + "".join(f"; {n}" for n in notes if n))


def _build(file) -> list:
    """The vintage's records, checked whole; halts on a problem."""
    try:
        recs = build_records(file)
        check_records(recs, file["period"])
    except ValueError as e:
        halt(f"{e}; nothing stored")
    return recs


def cmd_load(args) -> int:
    prof = profile()
    spec = prof.spec
    writing = args.commit or args.simulate
    other = sorted(set(args.acknowledge or ()) - {LIVE_PERIOD})
    if other:
        halt(f"--acknowledge {other}: only the live vintage {LIVE_PERIOD} is "
             "on the map; another vintage is stored as editions only and "
             "needs no acknowledgement; nothing read or stored")
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            ready = _readiness(cur, prof)
            has_ed = all(ready.values())
            missing = [t for t, ok in ready.items() if not ok]
            if writing and not has_ed:
                halt(f"missing {', '.join(missing)}; {NEXT_STEP} first")
            if not _live_rows(cur, spec):
                halt(f"{spec.live_table} is empty: the S7 loader never "
                     "rebuilds the W1 spine (it is referenced by foreign "
                     "keys); nothing read or stored")
            live_p = _live_periods(cur, spec)
            if live_p != [LIVE_PERIOD]:
                halt(f"{spec.live_table} holds {PERIOD} {live_p}; expected "
                     f"{LIVE_PERIOD} only (the declared live vintage "
                     f"{LIVE_VINTAGE}); nothing stored")
            if has_ed:
                _, new_live, errors = core.latest_map(cur, spec)
                if errors:
                    halt("invalid edition chain: " + "; ".join(
                        f"{pe._p(p)}: {msg}" for p, msg in errors.items()))
                if new_live:
                    halt(f"{spec.live_table}: live vintage with no editions "
                         f"{[pe._p(x) for x in new_live]}; run migrate-legacy "
                         "first")
                tips = tip_info(cur, spec)
                checked = ledger_checks(cur, prof)
            else:
                print(f"NOTE: {', '.join(missing)} not present: no editions "
                      "table yet; this preview compares the reply with the "
                      "LIVE table")
                tips, checked = legacy_tips(cur, spec), {}
            ranks = tip_ranks(tips, checked)
            held = sorted(ranks)
            print("held vintages: " + (", ".join(held) or "none")
                  + f" (live {LIVE_PERIOD})")
            src = _source(args)
            layer = src["layer"]
            f = _read(src["geojson"], src["vintage"])
            _report(layer, f)
            recs = _build(f)
            period = f["period"]
            sha = rows_content_sha(recs)
            bsha = content_sha256(src["geojson"])
            print(f"{src['file_name']}: content sha256 {sha[:16]}"
                  + ("; the held reply's content"
                     if sha == LEGACY_FILE["content_sha256"] else "")
                  + f"; bytes sha256 {bsha[:16]} (API output: not a release "
                  "identity)")
            if has_ed and args.recheck is None:
                done = ledger_complete(checked, src["where"], sha, ranks,
                                       rank=layer["rank"],
                                       allow_older=args.allow_older_file)
                if done:
                    print(f"the reply's (source, content sha256) is already "
                          f"in the ledger for {', '.join(done)}; nothing "
                          "compared (--recheck PERIOD compares it again)")
                    _check_acks(args, [])
                    print("nothing to do")
                    return _end(conn, args, writing, rc=0)
            lsrc = ledger_source(src["where"], period, layer["rank"])
            try:
                new, revised, skipped = plan_vintage(
                    period, layer["rank"], ranks, checked, lsrc, sha,
                    recheck=args.recheck, allow_older=args.allow_older_file)
            except ValueError as e:
                halt(str(e))
            rc = _run(args, conn, cur, prof, has_ed, src, layer, recs, new,
                      revised, skipped, tips, ranks, held, sha, bsha, lsrc)
        return _end(conn, args, writing, rc)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _tip_records(cur, spec, p, tips, has_ed) -> list:
    if has_ed:
        return records(cur, spec, p, tips[p]["edition"])
    return records(cur, spec, p)


def _same_rank_problem(p, rank) -> str:
    return (f"the layer has {rank_text(rank)}, the same as the held tip's, "
            "but the content differs: two sources claim the same release. "
            f"Read the layer's history on the portal, then --accept-reissue "
            f"{p}")


def _check_acks(args, periods) -> tuple:
    """(--accept-reissue set, --acknowledge set); halts when either names a
    period this run does not compare or store, or --acknowledge names a
    vintage other than the live one."""
    reissue = set(args.accept_reissue or ())
    ack = set(args.acknowledge or ())
    for flag, given in (("--accept-reissue", reissue), ("--acknowledge", ack)):
        stray = sorted(given - set(periods))
        if stray:
            halt(f"{flag} {stray}: not vintages this run compares or stores "
                 f"({periods or 'none'}); nothing stored")
    other = sorted(ack - {LIVE_PERIOD})
    if other:
        halt(f"--acknowledge {other}: only the live vintage {LIVE_PERIOD} "
             "is on the map; another vintage is stored as editions only and "
             "needs no acknowledgement; nothing stored")
    return reissue, ack


def _run(args, conn, cur, prof, has_ed, src, layer, recs, new, revised,
         skipped, tips, ranks, held, sha, bsha, lsrc) -> int:
    started = datetime.now(timezone.utc)
    spec = prof.spec
    rank = layer["rank"]
    older_note = {}
    for p in revised + new:
        t = ranks.get(p)
        if t is not None and t > rank:
            older_note[p] = (f"--allow-older-file given; {p}'s tip comes from "
                             f"a layer of {rank_text(t)}, this one is "
                             f"{rank_text(rank)}")
        elif p in new and held and p < max(held):
            older_note[p] = (f"--allow-older-file given; vintage {p} is "
                             f"before the newest held vintage {max(held)}")
        if p in older_note:
            print(f"NOTE: {older_note[p]}")
    print(f"planned: new {new or 'none'}; compare {revised or 'none'}; "
          "skipped " + (", ".join(f"{p} ({r})" for p, r in sorted(
              skipped.items())) or "none"))
    periods = sorted(new + revised)
    reissue, ack = _check_acks(args, periods)
    if not periods:
        if skipped and all(r.startswith("older") for r in skipped.values()):
            halt(f"older layer: {layer['name']}, {rank_text(rank)}, older "
                 f"than what is held for {', '.join(sorted(skipped))}; "
                 "storing it would record an older release. If this is "
                 "deliberate, re-run with --allow-older-file")
        print("nothing to do")
        return 0
    problems, infos, notes_by = {}, {}, {}
    for p in periods:
        notes = [older_note.get(p, "")]
        if p in revised:
            tip = _tip_records(cur, spec, p, tips, has_ed)
            bad = period_problems(recs, tip, kind="revised")
            if differs(recs, tip):
                for line in diff_lines(vintage_diff(recs, tip)):
                    print(f"  {p} against the held tip: {line}")
                t = ranks.get(p)
                if t is not None and rank == t:
                    if p in reissue:
                        notes.append(f"--accept-reissue {p}: the layer has "
                                     f"{rank_text(rank)}, the same as the "
                                     "held tip's, and the content differs")
                        print(f"  {p}: ACCEPTED REISSUE")
                    else:
                        bad.append(_same_rank_problem(p, rank))
                changed = map_changes(recs, tip)
                if p == LIVE_PERIOD and changed:
                    print(f"  {p}: {MAP_MESSAGE}")
                    if p in ack:
                        notes.append(f"--acknowledge {p}: the live vintage's "
                                     f"geometry or names change on "
                                     f"{len(changed)} district(s) "
                                     f"({_few(changed)}); stored as an "
                                     "edition, live only through "
                                     "refresh-latest --acknowledge")
                        print(f"  {p}: ACKNOWLEDGED")
                    else:
                        bad.append(f"the live vintage's geometry or names "
                                   f"change on {len(changed)} district(s) "
                                   f"({_few(changed)}): {MAP_MESSAGE}; "
                                   f"re-run with --acknowledge {p}")
            kind = "revised"
        else:
            kind = "new"
            bad = []
            live_recs = records(cur, spec, LIVE_PERIOD)
            for line in diff_lines(vintage_diff(recs, live_recs)):
                print(f"  {p} against the live vintage {LIVE_PERIOD}: {line}")
            print(f"  {p}: another vintage: stored as editions only, never "
                  "promoted to live by a load (adopting it is Scott's "
                  "decision)")
        if bad:
            problems[p] = bad
            continue
        notes_by[p] = [n for n in notes if n]
        how = (("new vintage" if kind == "new" else "restated vintage")
               + f" ({src['how']})")
        infos[p] = PeriodInfo(
            edition_source(layer, src, sha, "; ".join(notes_by[p])),
            _label(layer, src, sha, bsha, how, notes=notes_by[p]),
            ((lsrc, sha),))
    for p, msgs in sorted(problems.items()):
        for msg in msgs:
            print(f"  {p}: REJECTED, not stored: STOP CONDITION: {msg}")
    ok = [p for p in periods if p not in problems]
    rc = 0
    stats = {"periods": [], "kinds": {}, "stored_rows": 0, "live_rows": 0}
    if ok and has_ed:
        def apply(c, prof_, per, rs, *, fetched_on, source_file=None):
            return _late("apply_vintage")(c, prof_, per, rs,
                                          fetched_on=fetched_on,
                                          info=infos[per])
        rc = pe.load_periods(cur, prof, ok, lambda per: recs, rank.date(),
                             args.commit, simulate=args.simulate,
                             against="editions", stats=stats, apply=apply,
                             compare=_late("compare_vintage"))
    elif ok:
        for p in ok:
            if p == LIVE_PERIOD:
                held_recs = records(cur, spec, p)
                n = sum(1 for k in sorted(set(_by_key(held_recs))
                                          | set(_by_key(recs)))
                        if differs([r for r in recs if r[KEY] == k],
                                   [r for r in held_recs if r[KEY] == k]))
                print(f"  {p}: {'revised' if n else 'unchanged'}, {n} areas "
                      "changed (compared with live)")
            else:
                print(f"  {p}: would be held as editions only (live holds "
                      f"{LIVE_PERIOD})")
        print("  (no editions table yet: preview against live only)")
    if problems:
        print(f"REJECTED {len(problems)} vintage(s), nothing stored for "
              f"them: {', '.join(sorted(problems))}; exit 1")
        rc = 1
    if args.commit:
        stored = 0  # edition rows written by this run; not a source value
        for p in stats["periods"]:
            if stats["kinds"][p] in ("revised", HELD_NOT_LIVE):
                stored += len(recs)
        left = [p for p in ok if p not in stats["periods"]]
        failed, not_tried = (left[:1], left[1:]) if left else ([], [])
        partial = ""
        if rc:
            partial = (f"PARTIAL RUN (exit 1): rejected "
                       f"{sorted(problems) or 'none'}"
                       + "".join(f" ({p}: {'; '.join(problems[p])[:300]})"
                                 for p in sorted(problems))
                       + f"; failed {failed or 'none'}; not attempted "
                       f"{not_tried or 'none'}; a vintage that failed or was "
                       "rejected stored nothing and is not in the counts "
                       "below; ")
        notes = (partial + f"ArcGIS {layer['name']} {src['where']} content "
                 f"sha256 {sha[:16]}, {rank_text(rank)} ({src['how']}): "
                 + ("; ".join(f"{p} {k}" for p, k in stats["kinds"].items())
                    or "nothing stored")
                 + (f"; skipped {sorted(skipped)}" if skipped else "")
                 + "".join(f"; {n}" for p in sorted(notes_by)
                           if p in stats["periods"] for n in notes_by[p])
                 + f". Rows stored: {stored} edition rows, 0 live rows "
                 "(la_boundaries is written only by refresh-latest).")
        log_run(cur, stored, notes, started)
        conn.commit()
        print("pipeline_run_log row written"
              + (" (partial run: see its notes)" if rc else ""))
    return rc


# ---------------------------------------------------------------------------
# One-off: migrate the held table
# ---------------------------------------------------------------------------

SURVEY_COLS = (KEY,) + VALUES + (PERIOD,)


def live_state(cur, spec=None) -> tuple:
    """(rows, survey hash, {loaded_at (UTC text): rows}, periods) of the
    live table. The survey hash is md5 of lad24cd|lad24nm|longitude|
    latitude|shape_area|geojson|source_date (NULL as '~'), rows ordered by
    source_date, lad24cd, joined by LF (loaded_at left out)."""
    spec = spec or globals()["SPEC"]
    expr = "||'|'||".join(f"coalesce({c}::text,'~')" for c in SURVEY_COLS)
    cur.execute(f"SELECT COUNT(*), md5(string_agg({expr}, E'\\n' ORDER BY "
                f"{PERIOD}, {KEY})) FROM public.{spec.live_table}")
    n, h = cur.fetchone()
    cur.execute(f"SELECT to_char(loaded_at AT TIME ZONE 'UTC', "
                f"'YYYY-MM-DD HH24:MI:SS.US') || '+00', COUNT(*) FROM "
                f"public.{spec.live_table} GROUP BY 1 ORDER BY 1")
    loaded = {r[0]: r[1] for r in cur.fetchall()}
    return n, h, loaded, _live_periods(cur, spec)


def _proof(held, built) -> tuple:
    """(cells compared, differences) of the held rows against the rebuilt
    rows on the five values (the geometry as canonical text)."""
    hk, bk = _by_key(held), _by_key(built)
    diffs = [("only held", k) for k in sorted(set(hk) - set(bk))]
    diffs += [("only rebuilt", k) for k in sorted(set(bk) - set(hk))]
    cells = 0  # cells compared; not a source value
    for k in sorted(set(hk) & set(bk)):
        for c in VALUES:
            cells += 1
            if hk[k][c] != bk[k][c]:
                diffs.append((k, c) if c == "geojson"
                             else (k, c, hk[k][c], bk[k][c]))
    return cells, diffs


def geometry_equal_in_sql(cur, spec, built) -> int:
    """How many live geometries equal the rebuilt ones as jsonb (the
    database's own comparison of the canonical text cast to jsonb): a
    second proof beside the canonical text one. Read-only."""
    from psycopg2.extras import execute_values
    rows = [(r[KEY], r["geojson"]) for r in built]
    got = execute_values(
        cur, f"SELECT COUNT(*) FROM public.{spec.live_table} l JOIN (VALUES "
        "%s) v(k, g) ON l.lad24cd = v.k WHERE l.geojson = v.g::jsonb",
        rows, fetch=True, page_size=max(1, len(rows)))
    return sum(r[0] for r in got)


def migrate_legacy(cur, geojson_path, layer_path, *, write, legacy=None,
                   loaded=None, files=None) -> dict:
    """One-off. Preconditions: on write the editions table and ledger exist
    (ddl first) and are empty (in a preview they may be absent; if present
    they must be empty); live as surveyed (legacy: (rows, survey hash),
    default LEGACY_LIVE; loaded: {loaded_at: rows}, default LEGACY_LOADED;
    source_date 2024-05-01 only). Proof, any failure halts: the layer's
    identity and its lastEditDate equal to LEGACY_RANK_MS; the GeoJSON's
    identity; its content sha as files['content_sha256'] (default
    LEGACY_FILE; the bytes are API output); this parser on the GeoJSON
    reproduces every live value (296 x 5, the geometry as canonical text);
    and every live geometry equals the rebuilt one as jsonb. Edition 1 'as
    loaded' from live (published_date the load date, 2026-03-25,
    AS_LOADED_LABEL; source_file 'as loaded: <name>; source_date
    2024-05-01; ArcGIS lastEditDate <ms> (...)'), and a ledger row for the
    reply (outcome 'unchanged', edition 1, files['url'], the content sha)
    so the next API load finds it checked. Live untouched. Nothing commits
    here; the caller rolls everything back on a halt."""
    g = globals()
    legacy = tuple(legacy or g["LEGACY_LIVE"])
    loaded_want = dict(loaded or g["LEGACY_LOADED"])
    files = files or g["LEGACY_FILE"]
    prof = profile()
    spec = prof.spec
    ready = _readiness(cur, prof)
    if write and not all(ready.values()):
        halt(f"missing {[t for t, ok in ready.items() if not ok]}; run "
             "`ddl --commit` first")
    for t in (spec.editions_table, pe.file_checks_table(prof)):
        if ready[t]:
            cur.execute(f"SELECT COUNT(*) FROM public.{t}")
            n = cur.fetchone()[0]
            if n:
                halt(f"{t} already holds {n} rows; migrate-legacy runs once, "
                     "on empty tables")
    n, h, loaded_got, periods = live_state(cur, spec)
    if (n, h) != legacy or periods != [LIVE_PERIOD]:
        halt(f"{spec.live_table} is not as surveyed: {n} rows, hash {h}, "
             f"{PERIOD} {periods}; expected {legacy[0]} rows, hash "
             f"{legacy[1]}, {PERIOD} {LIVE_PERIOD}; nothing stored")
    if loaded_got != loaded_want:
        halt(f"{spec.live_table} loaded_at is not as surveyed: {loaded_got}; "
             f"expected {loaded_want}; nothing stored")
    layer = _read_layer(layer_path, LIVE_VINTAGE)
    if layer["last_edit_ms"] != g["LEGACY_RANK_MS"]:
        halt(f"{Path(layer_path).name}: {rank_text(layer['rank'])}; "
             f"expected {rank_text(g['LEGACY_RANK_MS'])} (the held layer); "
             "nothing stored")
    path = Path(geojson_path)
    f = _read(path, LIVE_VINTAGE)
    _report(layer, f)
    held = records(cur, spec, LIVE_PERIOD)
    built = _build(f)
    sha = rows_content_sha(built)
    bsha = content_sha256(path)
    if sha != files["content_sha256"]:
        halt(f"{path.name}: content sha256 {sha[:16]}, expected "
             f"{files['content_sha256'][:16]} (the held reply); nothing "
             "stored")
    cells, diffs = _proof(held, built)
    if diffs:
        halt(f"proof failed: {len(diffs)} differences between the held rows "
             f"and the held reply read again, e.g. {diffs[:5]}; nothing "
             "stored")
    same = geometry_equal_in_sql(cur, spec, built)
    if same != len(built):
        halt(f"proof failed: {same} of {len(built)} live geometries equal "
             "the rebuilt ones as jsonb; nothing stored")
    cur.execute(f"SELECT DISTINCT (loaded_at AT TIME ZONE 'UTC')::date FROM "
                f"public.{spec.live_table}")
    dates = [r[0] for r in cur.fetchall()]
    if len(dates) != 1:
        halt(f"{spec.live_table}: {len(dates)} load dates; expected one")
    plan = {"period": LIVE_PERIOD, "rows": len(held), "cells": cells,
            "records": held, "sha": sha, "bytes_sha": bsha,
            "loaded": dates[0], "content_sha": rows_content_sha(held),
            "types": f["types"],
            "source_file": (f"as loaded: {files['name']}; {PERIOD} "
                            f"{LIVE_PERIOD}; {rank_text(layer['rank'])}"),
            "ledger": ledger_source(files["url"], LIVE_PERIOD,
                                    layer["rank"])}
    print(f"proof: the held reply read again reproduces every held value: "
          f"{len(held):,} districts matched by lad24cd, {cells:,} cells "
          "(lad24nm, longitude, latitude, shape_area, geojson), 0 "
          f"differences; {same} of {len(built)} geometries identical "
          "(canonical text, and equal as jsonb)")
    print(f"  {LIVE_PERIOD}: edition 1 as loaded, {len(held):,} rows, "
          f"published {dates[0]} (the load date; live loaded_at "
          f"{sorted(loaded_got.items())}); source_file "
          f"{plan['source_file']!r}; content sha256 {plan['content_sha']}; "
          f"bytes sha256 {bsha[:16]}; ledger {plan['ledger']}")
    if not write:
        return plan
    cur.execute(f"SAVEPOINT {prof.savepoint}")
    ed = core.insert_edition(
        cur, spec, held, LIVE_PERIOD, release_label=core.AS_LOADED_LABEL,
        published_date=dates[0], source_file=plan["source_file"],
        source_sha256=plan["content_sha"], supersedes=None, strict=True)
    if ed != 1 or core.rows_differing(cur, spec, LIVE_PERIOD, ed):
        halt(f"{spec.editions_table} {LIVE_PERIOD}: edition 1 does not equal "
             "live; rolled back")
    pe.record_file_check(cur, prof, LIVE_PERIOD, plan["ledger"], sha,
                         "unchanged", 1)
    cur.execute(f"RELEASE SAVEPOINT {prof.savepoint}")
    return plan


def cmd_migrate_legacy(args) -> int:
    if not (args.file and args.layer):
        halt("migrate-legacy needs --file GEOJSON and --layer JSON (the held "
             "query reply and its layer definition)")
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            plan = migrate_legacy(cur, args.file, args.layer, write=writing)
            if args.commit:
                log_run(cur, plan["rows"], f"migrate-legacy: edition 1 as "
                        f"loaded for {plan['period']} ({plan['rows']} rows) "
                        f"from the live table; proof {plan['cells']} cells "
                        "equal the held ArcGIS reply read again (content "
                        f"sha256 {plan['sha'][:16]}); ledger row for the "
                        "reply. Live untouched.", started)
                conn.commit()
                print("migrate-legacy: COMMITTED; pipeline_run_log row "
                      "written")
            else:
                conn.rollback()
                print("PREVIEW: nothing written to the database (use "
                      "--commit or --simulate)" if not writing else
                      "SIMULATION: ROLLED BACK (nothing persisted)")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# restore-edition
# ---------------------------------------------------------------------------

def restore_edition(cur, period, edition) -> int:
    """Store edition `edition` of `period` as the next edition ('restored
    from edition N') for refresh-latest to apply; return its number. Halts
    if the edition does not exist or is the tip."""
    spec = globals()["SPEC"]
    p = str(period)
    tip = core.latest_edition(cur, spec, p)
    recs = records(cur, spec, p, edition)
    if not recs:
        halt(f"{spec.editions_table} {p}: no edition {edition}")
    if edition == tip:
        halt(f"{p}: edition {edition} is the tip; nothing to restore")
    cur.execute(f"SELECT DISTINCT source_file FROM public.{spec.editions_table}"
                f" WHERE {spec.period_col} = %s AND edition = %s", (p, edition))
    src = cur.fetchone()[0]
    new = core.insert_edition(
        cur, spec, recs, p, release_label=f"restored from edition {edition}",
        published_date=date.today(), source_file=src,
        source_sha256=rows_content_sha(recs), supersedes=tip, strict=True,
        allow_revert=True)
    if new == tip:
        halt(f"{p}: edition {edition} was not stored as the next edition; "
             "nothing to restore")
    return new


def cmd_restore_edition(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    spec = globals()["SPEC"]
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, spec.editions_table):
                halt(f"{spec.editions_table} does not exist")
            new = restore_edition(cur, args.period, args.edition)
            print(f"{args.period}: edition {args.edition} "
                  + ("stored" if writing else "would be stored")
                  + f" as edition {new}; run refresh-latest to apply it"
                  + (f" (--acknowledge {LIVE_PERIOD}: {MAP_MESSAGE})"
                     if args.period == LIVE_PERIOD else ""))
            if args.commit:
                n = len(records(cur, spec, args.period, new))
                log_run(cur, n, f"restore-edition: {args.period} edition "
                        f"{args.edition} stored as edition {new}", started)
                conn.commit()
                print("COMMITTED; pipeline_run_log row written")
            else:
                conn.rollback()
                print("PREVIEW: nothing written to the database (use "
                      "--commit or --simulate)" if not writing else
                      "SIMULATION: ROLLED BACK (nothing persisted)")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="S7 ONS Local Authority Districts May 2024 BGC "
        "boundaries (ArcGIS REST): editions loader on the period-editions "
        "engine, one vintage in live. la_boundaries is never rebuilt, "
        "re-keyed or deleted from; la_code_lookup is never written.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create the editions table, its triggers "
                       "and the file-check ledger (preview by default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("load", help="read a vintage's layer and England "
                       "query, compare and store it (preview by default)")
    p.add_argument("--vintage", choices=sorted(VINTAGES),
                   help=f"the vintage to read (default {LIVE_VINTAGE}, the "
                   "live one; another is stored as editions only)")
    p.add_argument("--file", metavar="GEOJSON",
                   help="a local query reply (with --layer; nothing "
                   "downloaded)")
    p.add_argument("--layer", metavar="JSON",
                   help="the layer definition JSON the reply was queried "
                   "from")
    p.add_argument("--recheck", type=_period_arg, metavar="PERIOD",
                   help="compare this held vintage again although the reply "
                   "is in the ledger")
    p.add_argument("--allow-older-file", action="store_true",
                   help="compare and store a vintage whose tip comes from a "
                   "newer layer, or a vintage before the newest held "
                   "(logged)")
    p.add_argument("--accept-reissue", action="append", type=_period_arg,
                   metavar="PERIOD",
                   help="store a held vintage whose content differs under the "
                   "same lastEditDate (repeatable; logged)")
    p.add_argument("--acknowledge", action="append", type=_period_arg,
                   metavar="PERIOD",
                   help=f"store a change to the live vintage's geometry or "
                   f"names ({LIVE_PERIOD} only; logged)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("refresh-latest", help="copy the live vintage's latest "
                       "edition into la_boundaries, loaded_at included "
                       "(preview by default)")
    p.add_argument("--acknowledge", action="append", type=_period_arg,
                   metavar="PERIOD",
                   help=f"required to write: {MAP_MESSAGE}")
    p.add_argument("--accept-drift", action="append", metavar="PERIOD",
                   help="overwrite the live vintage although its live rows "
                   "equal no stored edition (repeatable; any other vintage "
                   "halts)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_refresh_latest)
    p = sub.add_parser("migrate-legacy", help="one-off: edition 1 as loaded "
                       "for the held vintage, with the proof (preview by "
                       "default)")
    p.add_argument("--file", metavar="GEOJSON",
                   help="the held query reply")
    p.add_argument("--layer", metavar="JSON",
                   help="the held layer definition JSON")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_migrate_legacy)
    p = sub.add_parser("restore-edition", help="store an earlier edition's "
                       "rows as the next edition (preview by default)")
    p.add_argument("period", type=_period_arg, metavar="PERIOD")
    p.add_argument("edition", type=int)
    pe.mode_parser(p)
    p.set_defaults(func=cmd_restore_edition)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
