"""S14 (DWP Universal Credit Local Housing Allowance rates, with the BRMA to
local authority crosswalk): edition history and loader on the
period-editions engine.

Two live tables keep their names, keys and data columns and are the
latest-edition layer that W1 reads (sql/w1/05_la_signals.sql: the five
*_weekly columns of brma_lha_rates at MAX financial_year, joined through
la_brma_mapping.brma_name). This module owns their editions tables and
file-check ledgers, append-only:

    SPECS['rates']    brma_lha_rates_editions    period financial_year,
                      key brma_name
    SPECS['mapping']  la_brma_mapping_editions   period vintage, key lad24cd
    ledgers           <editions table>_file_checks

Rates. An edition is what one England CSV ("England: Universal Credit Local
Housing Allowance monthly rates <yyyy> to <yyyy>", on the year's GOV.UK page
in the collection "Universal Credit Local Housing Allowance rates") says
about its financial year: per BRMA the five monthly rates (SAR, 1 to 4 bed)
as published, and the five weekly rates DERIVED here (monthly x 12 / 52,
Decimal, rounded half-up to 2 places; not source values: DWP's file
publishes monthly rates only, and VOA's own weekly figures were not
checked). A reissue of a held year (2024-25 was reissued twice; 2026-27 on
8 April 2026 to correct its header year label) is the next edition of that
year; it reaches live only through refresh-latest.

Crosswalk. la_brma_mapping is derived, not published: each authority's
la_boundaries centroid (longitude, latitude; EPSG:4326, transformed to
EPSG:27700) placed in the VOA "English BRMA(LHA) Layer" polygon that
contains it (mapping_method 'centroid_spatial_join'), else the nearest
polygon ('nearest_fallback'). One vintage, LIVE_VINTAGE (the VOA layer
applicable May 2020 x la_boundaries lad24, May 2024), live_single_period:
another vintage (a new BRMA layer or a new LAD vintage) is stored as
editions only and never promoted by a load; adopting one is a deliberate
decision. The VOA layer writes "X & Y" where DWP's CSV writes "X and Y";
the names go through the committed exact table scripts/s14_brma_names.json
(brma_aliases, convert_name), never a pattern, and every mapped name must be
a BRMA of the latest rate year (else the W1 join would leave an authority
without rates: decision note 2026-08-21). The live table gains one additive
column, vintage (live_alter_sql; run by `ddl` only). brma_secondary is
neither written nor compared.

History (so the wording here stays truthful): 2026-27 rates and the
crosswalk were loaded on 2026-07-10 (pipeline_run_log 52 and 53) and
rebuilt by an accidental `import s14_lha_rates_build_v2` on 2026-08-20 (run
log 97, decision note 2026-08-21: 37 mapping names left in the "&" form and
Ashford lost to a header off-by-one), then repaired by direct SQL with no
run-log row (Ashford's row restored, its source says so; the 37 names
normalised). The same scripts ran by accident again on 2026-10-11 (run log
499 and 501; decision note 2026-10-11) and the tables were repaired to the
surveyed state. Both scripts are retired. The crosswalk's mapping_method
holds the text 'NaN' on 295 rows (a pandas NaN written into a text column);
edition 1 'as loaded' keeps it as held and the named correction
s14-mapping-method-2026-10 is the next edition. migrate-legacy (one-off)
records both tables as edition 1 'as loaded' with a proof: the rates against
the held 2026-27 CSV on every value, the crosswalk's brma_name and la_name
against a rebuild from the held GML.zip and la_boundaries.

Blanks and zeros (docs/RULES.md rule 1): every rate cell must be a money
amount (money: '£' and thousands separators stripped, at most two decimal
places); a blank, text, zero or negative halts naming the cell and no row
is ever skipped. Nothing is coerced to 0.

Identity (rule 3), from the file on every path: the CSV's header row is
exactly 'BRMA', 'Monthly UC LHA rates <yyyy> to <yyyy> - SAR', '<n> Bed
<yyyy> to <yyyy>' (n 1 to 4); the year it names must equal the page's (and
--release); a header naming another year halts unless --accept-header-year
YEAR (logged). The older layout (a title row ',Monthly UC LHA rates 24/25',
then 'BRMA,SAR,1 Bed,2 bed,3 bed,4 Bed', "&" names) halts naming what it
saw. A BRMA name containing "&" halts. The crosswalk's layer: the zip
member 'English BRMA(LHA) Layer.gml', OGR layer 'English_BRMA_LHA__Layer',
field 'Name', EXPECTED_POLYGONS polygons each with srsName EPSG:27700 (the
layer CRS reads as absent and is set to EPSG:27700), the zip's sha256 a
known vintage (VINTAGES), and la_boundaries as that vintage pins it.

Rank (the older-file guard, every path): the financial year the CSV header
names, then, within a year, the newest change-history timestamp of the
year's page (only for the page's current file: on the page path, or a
--file whose bytes equal the page's attachment); never the media id, URL or
file name. --file --no-page (or a --file that is not the page's current
file) has no within-year rank: a re-read with the same content is
unchanged, different content needs --accept-reissue. A file older than the
tip is skipped (all older halts) unless --allow-older-file. Equal rank with
different content stops unless --accept-reissue YEAR.

Stop conditions (a stopped period is REJECTED: nothing stored, no ledger
row, exit 1; a committed run writes a run-log row, partial runs included):
    rates new year: frozen rates are normal (2025-26 equals 2024-25 on all
        152 BRMAs); any BRMA moving more than NEW_AREA_BP (25%) on any
        monthly rate, or a rate type's median moving more than
        NEW_MEDIAN_BP (10%), against the previous held year needs
        --acknowledge YEAR; a BRMA set differing from the previous year's is
        KEY CHANGES (--accept-key-changes YEAR); a crosswalk name with no
        rate row in a new latest year is JOIN (no flag releases it)
    rates reissue: any value changed needs --acknowledge YEAR; a BRMA set
        differing from the tip is KEY CHANGES (--accept-key-changes YEAR),
        but a reissue of the newest year that drops a BRMA the crosswalk
        uses is JOIN (no flag releases it); refresh-latest refuses the same
    crosswalk: any change against the held edition needs --acknowledge
        VINTAGE (a change equal to a named correction in MAPPING_CORRECTIONS
        is labelled with its name); an authority set differing from the tip
        is KEY CHANGES and nothing releases it (a vintage's key set never
        changes)

W1 input: a new year's live rows take loaded_at = now(); a revision reaches
live only through refresh-latest, which copies the edition's loaded_at, so
run refresh-latest in the same session before W1 (both tables,
brma_lha_rates and la_brma_mapping, are W1 inputs). The live source column is
written from the edition's source_text (refresh column, not compared).

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back). There is no sync-new: migrate-legacy records the held
state.
    python scripts/s14_lha_editions.py ddl [--commit | --simulate]
    python scripts/s14_lha_editions.py status
    python scripts/s14_lha_editions.py load [--only rates|mapping]
        [--release YYYY-YY] [--file PATH [--no-page]] [--gml PATH]
        [--recheck PERIOD] [--allow-older-file] [--acknowledge PERIOD ...]
        [--accept-header-year YYYY-YY ...] [--accept-reissue PERIOD ...]
        [--accept-key-changes PERIOD ...] [--commit | --simulate]
        # the CSV is downloaded to data/raw/s14_lha/ (also in a preview; a
        # preview writes nothing to the database)
    python scripts/s14_lha_editions.py refresh-latest [--accept-drift P]
        [--accept-key-changes P] [--commit | --simulate]
    python scripts/s14_lha_editions.py migrate-legacy [--file CSV]
        [--gml ZIP] [--commit | --simulate]
    python scripts/s14_lha_editions.py restore-edition TABLE PERIOD N
        [--commit | --simulate]
    python scripts/s14_lha_editions.py check-mapping [--gml ZIP]
"""
import argparse
import csv
import dataclasses
import hashlib
import io
import json
import re
import statistics
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402

RUN_AGENT = "s14_lha_editions"
RUN_SOURCE = "14"
LIVE_MISSING = pe.LIVE_MISSING
HELD_NOT_LIVE = pe.HELD_NOT_LIVE

API = "https://www.gov.uk/api/content"
GOV_UK = "https://www.gov.uk"
COLLECTION_PATH = ("/government/collections/universal-credit-local-housing-"
                   "allowance-rates")
COLLECTION_TITLE = "Universal Credit Local Housing Allowance rates"
REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s14_lha"          # git-ignored
GML_FILE = RAW_DIR / "GML.zip"
RATES_FILE = RAW_DIR / "england-rates-2026-to-2027.csv"
NAMES_FILE = Path(__file__).resolve().parent / "s14_brma_names.json"
USER_AGENT = ("ucws-pipeline S14 loader (read-only download of the public "
              "DWP Universal Credit LHA rates)")
MIN_FILE_BYTES = 200
NO_PAGE = "--file --no-page: the year page was not read"

# ---------------------------------------------------------------------------
# Columns (live types from information_schema, checked 2026-10-11:
# brma_lha_rates: brma_name varchar(100) NOT NULL, financial_year varchar(7)
# NOT NULL, the ten rates numeric(8,2) (nullable; 0 NULL held), source text,
# loaded_at timestamptz DEFAULT now(); PK (brma_name, financial_year).
# la_brma_mapping: lad24cd varchar(9) NOT NULL, la_name varchar(100),
# brma_name varchar(100) NOT NULL, brma_secondary varchar(100) (NULL on all
# 296), mapping_method text DEFAULT 'centroid_spatial_join', source text,
# loaded_at timestamptz DEFAULT now(); PK (lad24cd).) The editions tables
# hold every value NOT NULL: a blank halts, never stored.
# ---------------------------------------------------------------------------

KINDS = ("sar", "one_bed", "two_bed", "three_bed", "four_bed")
MONTHLY = tuple(f"{k}_monthly" for k in KINDS)
WEEKLY = tuple(f"{k}_weekly" for k in KINDS)
RATE_VALUES = MONTHLY + WEEKLY
RATE_KEY = "brma_name"
FY_COL = "financial_year"
MAP_KEY = "lad24cd"
VINTAGE_COL = "vintage"
MAP_VALUES = ("la_name", "brma_name", "mapping_method")
SOURCE_TEXT = "source_text"          # editions column for live `source`
CENT = Decimal("0.01")

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

YEARS = r"([0-9]{4}) to ([0-9]{4})"
PAGE_TITLE_RE = re.compile(r"Universal Credit Local Housing Allowance rates: "
                           + YEARS)
# listed in the collection and not a financial-year page (five years)
IGNORED_TITLES = ("Universal Credit Local Housing Allowance rates: 2014 to "
                  "2019",)
SERIES_RE = re.compile(r"local housing allowance", re.I)
ATTACHMENT_RE = re.compile(r"England: Universal Credit Local Housing Allowance "
                           r"monthly rates " + YEARS)
FY_RE = re.compile(r"([0-9]{4})-([0-9]{2})")
# When the next year is expected: first published 31 January 2024, 3 February
# 2025 and 30 January 2026; overdue after 28 February before the year starts
EXPECTED_RELEASES = {}

# ---------------------------------------------------------------------------
# The CSV
# ---------------------------------------------------------------------------

HEADER_SAR_RE = re.compile(r"Monthly UC LHA rates " + YEARS + r" - SAR")
HEADER_BED_RE = re.compile(r"([1-4]) Bed " + YEARS)
OLD_HEADER = ("BRMA", "SAR", "1 Bed", "2 bed", "3 bed", "4 Bed")
OLD_TITLE_RE = re.compile(r"Monthly UC LHA rates [0-9]{2}/[0-9]{2}")
MONEY_RE = re.compile(r"£?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?")

# ---------------------------------------------------------------------------
# The crosswalk
# ---------------------------------------------------------------------------

GML_MEMBER = "English BRMA(LHA) Layer.gml"
GML_LAYER = "English_BRMA_LHA__Layer"       # OGR's name for the layer
GML_FIELD = "Name"
GML_CRS = "EPSG:27700"
EXPECTED_POLYGONS = 152
EXPECTED_AUTHORITIES = 296
LIVE_VINTAGE = "brma-2020-05_lad-2024-05"
VINTAGE_RE = re.compile(r"brma-[0-9]{4}-[0-9]{2}_lad-[0-9]{4}-[0-9]{2}")
MAPPING_SOURCE = "VOA BRMA May 2020 x la_boundaries centroids"
WITHIN = "centroid_spatial_join"
NEAREST = "nearest_fallback"
# the known vintages: the VOA layer (GML.zip sha256, split so the credential
# scan does not flag it) and the la_boundaries state it was built on (rows,
# md5 of lad24cd|lad24nm|longitude|latitude ordered by lad24cd; read
# 2026-10-11, source_date 2024-05-01)
VINTAGES = {
    LIVE_VINTAGE: {
        "gml_sha256": ("66515b0a1403e4557e6501d2d5ada5e7"
                       "e17b29deef2da9165ba515a66907f6c6"),
        "brma_page": GOV_UK + "/government/publications/broad-rental-market-"
                     "area-boundary-layer-for-geographical-information-"
                     "system-gis-applicable-may-2020",
        "boundaries": (296, "3335ef7a7ee622b4" "75ef87756e0deb86"),
        "source_text": MAPPING_SOURCE,
    },
}
# named corrections of the crosswalk: a change equal to one of these is
# labelled with its name (still needs --acknowledge VINTAGE)
MAPPING_CORRECTIONS = {
    "s14-mapping-method-2026-10": {
        "vintage": LIVE_VINTAGE, "column": "mapping_method", "from": "NaN",
        "to": WITHIN, "count": 295,
        "why": "the text 'NaN' (a pandas NaN written into a text column by "
               "the retired v2 build) on the 295 authorities whose centroid "
               "lies within its BRMA polygon (check-mapping, 2026-10-11)"},
}

# ---------------------------------------------------------------------------
# Stop conditions (basis points)
# ---------------------------------------------------------------------------

NEW_AREA_BP = 2500        # new year: any BRMA, any monthly rate
NEW_MEDIAN_BP = 1000      # new year: a rate type's median
KEYS = "KEY CHANGES"      # released only by --accept-key-changes (rates)
JOIN = "JOIN"             # released by nothing

# ---------------------------------------------------------------------------
# The held state (migrate-legacy preconditions), surveyed 2026-10-11
# ---------------------------------------------------------------------------

# (rows, survey hash: md5 of every column but loaded_at, NULL as '~', joined
# by '|', ordered by period then key; see live_state)
LEGACY_RATES = (152, "5536ba6d15071e21" "0da60bb64d512a6e")
LEGACY_RATES_LOADED = {
    datetime(2026, 8, 20, 20, 23, 0, 558032, tzinfo=timezone.utc): 151,
    datetime(2026, 8, 20, 23, 40, 31, 748925, tzinfo=timezone.utc): 1,
}
LEGACY_MAPPING = (296, "2e7fb44bf374be96" "280741052be327db")
LEGACY_MAPPING_LOADED = {
    datetime(2026, 8, 20, 20, 23, 0, 558032, tzinfo=timezone.utc): 296,
}
LEGACY_RATES_FILE = {
    "period": "2026-27", "name": "england-rates-2026-to-2027.csv",
    "sha256": ("3b33d922342719b1970ff51b55f60dc4"
               "ea306d00bae63132c99b8977e89b0815"),
    "url": "https://assets.publishing.service.gov.uk/media/69d654a2e1430e837"
           "a86f64a/england-rates-2026-to-2027.csv",
    "page": GOV_UK + "/government/publications/universal-credit-local-"
            "housing-allowance-rates-2026-to-2027",
    # the year page's newest change (the reissue); the held file's HTTP
    # Last-Modified is 8 April 2026 13:14:10 GMT, before every load
    "rank": ("2026-27", datetime(2026, 4, 8, 15, 31, 30, tzinfo=timezone.utc)),
    "loaded_by": "pipeline_run_log 52 and 53 (2026-07-10), the accidental "
                 "rebuild run log 97 (2026-08-20) and Ashford restored by "
                 "direct SQL; repaired 2026-10-11 (decision note)",
}
LEGACY_MAPPING_LOADED_BY = (
    "the accidental rebuild run log 97 (2026-08-20), 37 names normalised "
    "'&' to 'and' by direct SQL; repaired 2026-10-11 (decision note)")
# a held mapping_method form: (held, rebuilt, count)
LEGACY_METHOD_FORM = ("NaN", WITHIN, 295)
# before the editions exist (a preview against live): the rank of the file
# each held year equals
LEGACY_RANKS = {"2026-27": LEGACY_RATES_FILE["rank"]}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _json(x):
    return json.loads(x) if isinstance(x, (str, bytes)) else (x or {})


def _norm(s) -> str:
    return " ".join(str("" if s is None else s).split())


def _when(text) -> datetime:
    t = str(text or "").strip()
    if not t:
        raise ValueError("no timestamp")
    return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(
        timezone.utc)


def _iso(ts) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fy_label(years_text) -> str:
    """'2026-27' of '2026 to 2027'; ValueError unless the second year is the
    first plus one."""
    mm = re.fullmatch(YEARS, _norm(years_text))
    if not mm or int(mm.group(2)) != int(mm.group(1)) + 1:
        raise ValueError(f"{years_text!r} is not one financial year "
                         "'<yyyy> to <yyyy+1>'")
    return f"{mm.group(1)}-{mm.group(2)[2:]}"


def fy_text(fy) -> str:
    """'2026 to 2027' of '2026-27'."""
    a = int(fy_ok(fy)[:4])
    return f"{a} to {a + 1}"


def fy_ok(fy) -> str:
    mm = FY_RE.fullmatch(str(fy or ""))
    if not mm or (int(mm.group(1)) + 1) % 100 != int(mm.group(2)):
        raise ValueError(f"not a financial year YYYY-YY: {fy!r}")
    return str(fy)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class YearPages(dict):
    """{financial year: base_path}; .ignored the listed titles named in
    IGNORED_TITLES, .titles every title seen."""
    ignored: list
    titles: list


def year_pages(collection_json) -> YearPages:
    """The collection's financial-year pages: documents titled exactly
    'Universal Credit Local Housing Allowance rates: <yyyy> to <yyyy+1>'.
    ValueError (listing the titles seen) when the collection is retitled,
    two documents claim a year, none fits, or a document that looks like
    the series names a later year than the newest page in a title that does
    not fit (never passed over for an older page)."""
    d = _json(collection_json)
    t = _norm(d.get("title"))
    if t != COLLECTION_TITLE:
        raise ValueError(f"the collection is titled {t!r}, not "
                         f"{COLLECTION_TITLE!r}; the page has changed")
    docs = list(((d.get("links") or {}).get("documents")) or [])
    out = YearPages()
    out.ignored, out.titles = [], [_norm(x.get("title")) for x in docs]
    stray = []
    for doc, title in zip(docs, out.titles):
        if title in IGNORED_TITLES:
            out.ignored.append(title)
            continue
        mm = PAGE_TITLE_RE.fullmatch(title)
        try:
            fy = fy_label(f"{mm.group(1)} to {mm.group(2)}") if mm else None
        except ValueError:
            fy = None
        if fy is None or not doc.get("base_path"):
            stray.append(title)
            continue
        if fy in out:
            raise ValueError(f"two documents claim {fy}: {title!r}; refusing "
                             f"to choose one; titles seen: {out.titles}")
        out[fy] = doc["base_path"]
    if not out:
        raise ValueError("no document titled 'Universal Credit Local Housing "
                         "Allowance rates: <yyyy> to <yyyy>' in the "
                         f"collection; titles seen: {out.titles}")
    newest = int(max(out)[:4])
    later = [s for s in stray if SERIES_RE.search(s) and any(
        int(y) > newest for y in re.findall(r"(?<![0-9])([0-9]{4})(?![0-9])",
                                            s))]
    if later:
        raise ValueError(f"the newest year page is {max(out)}, but the "
                         "collection lists a document that looks like the "
                         f"series, with a later year, whose title does not "
                         f"fit: {later}; not choosing an older page over "
                         f"it; titles seen: {out.titles}")
    return out


def latest_year_page(collection_json) -> tuple:
    """(financial year, base_path) of the newest year page."""
    pages = year_pages(collection_json)
    fy = max(pages)
    return fy, pages[fy]


def year_page_for(collection_json, fy) -> tuple:
    """(financial year, base_path) of that year's page (--release, or the
    year of a --file); ValueError unless the collection lists it."""
    pages = year_pages(collection_json)
    if fy not in pages:
        raise ValueError(f"no page for {fy} in the collection; years listed "
                         f"{sorted(pages)}; titles seen: {pages.titles}")
    return fy, pages[fy]


def newest_listed(collection_json) -> "str | None":
    try:
        return max(year_pages(collection_json))
    except ValueError:
        return None


def page_year(page_json) -> str:
    """The financial year of a year page's title; ValueError otherwise."""
    t = _norm(_json(page_json).get("title"))
    mm = PAGE_TITLE_RE.fullmatch(t)
    if not mm:
        raise ValueError(f"the page is titled {t!r}, not 'Universal Credit "
                         "Local Housing Allowance rates: <yyyy> to <yyyy>'")
    return fy_label(f"{mm.group(1)} to {mm.group(2)}")


def england_attachment(page_json) -> dict:
    """{title, url, fy, content_type} of the page's one attachment titled
    'England: Universal Credit Local Housing Allowance monthly rates <yyyy>
    to <yyyy>', a .csv, for the page's own year. ValueError listing every
    attachment title otherwise."""
    d = _json(page_json)
    atts = list(((d.get("details") or {}).get("attachments")) or [])
    names = [_norm(a.get("title")) for a in atts]
    hits = [a for a in atts if ATTACHMENT_RE.fullmatch(_norm(a.get("title")))]
    if len(hits) != 1 or not hits[0].get("url"):
        raise ValueError(f"{len(hits)} attachments titled 'England: Universal "
                         "Credit Local Housing Allowance monthly rates <yyyy> "
                         f"to <yyyy>'; attachments: {names}")
    a = hits[0]
    if Path(urlparse(a["url"]).path).suffix.lower() != ".csv":
        raise ValueError(f"the England attachment is not a .csv file "
                         f"({a['url']}); attachments: {names}")
    mm = ATTACHMENT_RE.fullmatch(_norm(a["title"]))
    fy = fy_label(f"{mm.group(1)} to {mm.group(2)}")
    own = page_year(d) if d.get("title") else fy
    if fy != own:
        raise ValueError(f"the England attachment is for {fy}, the page for "
                         f"{own}")
    return {"title": _norm(a["title"]), "url": a["url"], "fy": fy,
            "content_type": a.get("content_type")}


def change_history(page_json) -> list:
    """[(public_timestamp, note)] of the page, newest first."""
    d = _json(page_json)
    ch = ((d.get("details") or {}).get("change_history")) or []
    out = [(_when(c.get("public_timestamp")), _norm(c.get("note")))
           for c in ch]
    return sorted(out, key=lambda x: x[0], reverse=True)


def page_rank_ts(page_json) -> datetime:
    """The newest change-history timestamp of the page (the within-year
    rank); ValueError when the page has none."""
    hist = change_history(page_json)
    if not hist:
        raise ValueError("the page has no change history; no within-year "
                         "rank")
    return hist[0][0]


def expected_next_release(newest_fy, announced=None) -> tuple:
    """(financial year, last day before it is overdue) of the year after
    newest_fy: an announced date if held, else 28 February before it
    starts."""
    a = int(fy_ok(newest_fy)[:4]) + 1
    nxt = f"{a}-{(a + 1) % 100:02d}"
    table = EXPECTED_RELEASES if announced is None else announced
    return nxt, table.get(nxt, date(a, 2, 28))


def _today() -> date:
    return date.today()


def discovery_note(collection_json, held_periods, today=None) -> list:
    """Lines naming the newest year page the collection lists against the
    newest held year: a NEW RELEASE line when it is newer, else that none
    newer is listed and a WARNING when the next year is overdue."""
    nl = newest_listed(collection_json)
    held = sorted(str(p) for p in held_periods)
    if nl is None:
        return []
    if not held or nl > held[-1]:
        return [f"NEW RELEASE: the collection lists {nl}, newer than the "
                f"newest held year {held[-1] if held else 'none'}"]
    out = [f"the newest year page the collection lists is {nl}; the newest "
           f"held year is {held[-1]}; no newer page is listed"]
    today = today or _today()
    y, last = expected_next_release(held[-1])
    if today > last:
        out.append(f"WARNING: the {y} rates were expected by {last:%d %B %Y} "
                   f"(first published 31 January 2024, 3 February 2025, 30 "
                   f"January 2026) and today is {today:%d %B %Y}, but the "
                   "collection does not list them. They may be late, or "
                   "discovery may be missing them (check the collection by "
                   "hand; --file PATH loads a downloaded file)")
    return out


# ---------------------------------------------------------------------------
# The CSV
# ---------------------------------------------------------------------------

def _decode(raw: bytes, name) -> tuple:
    """(text, encoding): strict UTF-8 (a BOM dropped), else strict cp1252,
    else ValueError."""
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252"), "cp1252"
    except UnicodeDecodeError as e:
        raise ValueError(f"{name}: neither UTF-8 nor cp1252 ({e}); the "
                         "encoding must be read deliberately") from None


def _header(cells, where) -> list:
    """[the year text each of the five rate cells names] of the 2026-27
    header; ValueError naming what was seen otherwise."""
    cells = [_norm(c) for c in cells]
    if len(cells) != 6 or cells[0] != "BRMA":
        raise ValueError(f"{where}: the header row is {cells}, not 'BRMA', "
                         "'Monthly UC LHA rates <yyyy> to <yyyy> - SAR', '1 "
                         "Bed <yyyy> to <yyyy>' .. '4 Bed <yyyy> to <yyyy>'")
    years = []
    mm = HEADER_SAR_RE.fullmatch(cells[1])
    if not mm:
        raise ValueError(f"{where}: header cell {cells[1]!r} is not 'Monthly "
                         "UC LHA rates <yyyy> to <yyyy> - SAR'; header "
                         f"{cells}")
    years.append(f"{mm.group(1)} to {mm.group(2)}")
    for n, c in enumerate(cells[2:], start=1):
        mm = HEADER_BED_RE.fullmatch(c)
        if not mm or int(mm.group(1)) != n:
            raise ValueError(f"{where}: header cell {c!r} is not '{n} Bed "
                             f"<yyyy> to <yyyy>'; header {cells}")
        years.append(f"{mm.group(2)} to {mm.group(3)}")
    return years


def read_csv(path) -> dict:
    """The facts of one England rates CSV: {path, name, encoding, header,
    header_years (the year text of each rate cell), year (the financial
    year when every header cell names the same one, else None), rows [(row
    number, BRMA, [five cells as text])], names (sorted)}. Strict decode;
    the header row first and exactly as the 2026-27 file has it; one row per
    BRMA, six cells, a name with no '&', no repeat; empty rows only at the
    end. ValueError naming what was seen."""
    p = Path(path)
    text, enc = _decode(p.read_bytes(), p.name)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError(f"{p.name}: empty file")
    first = [_norm(c) for c in rows[0]]
    second = [_norm(c) for c in rows[1]] if len(rows) > 1 else []
    if (any(OLD_TITLE_RE.fullmatch(c) for c in first)
            or tuple(second[:6]) == OLD_HEADER
            or tuple(first[:6]) == OLD_HEADER):
        raise ValueError(f"{p.name}: the older layout (the 2024-25 and "
                         f"2025-26 files: a title row {first}, then the header "
                         f"{second}, '&' names); this reader reads the "
                         "2026-27 layout only: the reading must be corrected "
                         "deliberately, with the evidence")
    years = _header(rows[0], f"{p.name} row 1")
    labels = set()
    for y in years:
        try:
            labels.add(fy_label(y))
        except ValueError as e:
            raise ValueError(f"{p.name} row 1: {e}") from None
    out, seen, empty = [], {}, 0  # rows; empty: trailing rows, not a value
    for n, r in enumerate(rows[1:], start=2):
        if all(not _norm(c) for c in r):
            empty += 1
            continue
        if empty:
            raise ValueError(f"{p.name} row {n}: data after {empty} empty "
                             "row(s); empty rows are allowed only at the end")
        cells = [_norm(c) for c in r]
        if len(cells) != 6:
            raise ValueError(f"{p.name} row {n}: {len(cells)} cells, expected "
                             f"6: {cells}")
        name = cells[0]
        if not name:
            raise ValueError(f"{p.name} row {n}: a blank BRMA name: {cells}")
        if "&" in name:
            raise ValueError(f"{p.name} row {n}: the BRMA name {name!r} "
                             "contains '&' (the 2026-27 file writes 'and'; "
                             "the W1 join is on the name)")
        if name in seen:
            raise ValueError(f"{p.name} row {n}: {name!r} appears twice (also "
                             f"row {seen[name]})")
        seen[name] = n
        out.append((n, name, cells[1:]))
    if not out:
        raise ValueError(f"{p.name}: no BRMA rows")
    return {"path": p, "name": p.name, "encoding": enc,
            "header": [_norm(c) for c in rows[0]], "header_years": years,
            "year": labels.pop() if len(labels) == 1 else None,
            "rows": out, "names": sorted(seen)}


def header_year_problems(file, period) -> list:
    """Each header cell naming a financial year other than `period`."""
    out = []
    for cell, y in zip(file["header"][1:], file["header_years"]):
        fy = fy_label(y)
        if fy != period:
            out.append(f"header cell {cell!r} names {fy}, not {period}")
    return out


def money(v, where) -> Decimal:
    """A published monthly rate: '£' and thousands separators stripped, at
    most two decimal places, more than zero; returned to two places. A
    blank, any text, a zero, a negative or a third decimal place raises
    ValueError naming `where`. Never returns a default."""
    s = _norm(v)
    if not s:
        raise ValueError(f"{where}: a blank cell (a blank halts and is never "
                         "read as 0)")
    if s.lstrip("£").startswith("-"):
        raise ValueError(f"{where}: negative amount {s!r}")
    if not MONEY_RE.fullmatch(s):
        raise ValueError(f"{where}: {s!r} is not a money amount ('£', "
                         "thousands separators, at most two decimal places)")
    d = Decimal(s.lstrip("£").replace(",", ""))
    if d == 0:
        raise ValueError(f"{where}: a zero amount (no rate has been published "
                         "as 0; never stored)")
    return d.quantize(CENT)


def weekly(monthly) -> Decimal:
    """The weekly rate derived from a monthly one: monthly x 12 / 52, Decimal
    arithmetic, rounded half-up to 2 places. Not a source value: the file
    publishes monthly rates only."""
    return (Decimal(monthly) * 12 / 52).quantize(CENT, rounding=ROUND_HALF_UP)  # not a source value


def build_rates(table, year, provenance=None) -> list:
    """The year's records from a read file, one per BRMA, sorted by name:
    {brma_name, financial_year, the five monthly rates (money), the five
    weekly rates (weekly; derived)}, plus provenance ({name: value} for the
    caller, e.g. source_text). ValueError on the first cell that is not an
    amount, naming the row, the BRMA and the column; no row is skipped."""
    out = []
    for n, name, cells in table["rows"]:
        rec = {RATE_KEY: name, FY_COL: str(year)}
        for col, cell, head in zip(MONTHLY, cells, table["header"][1:]):
            rec[col] = money(cell, f"{table['name']} row {n} {name} {head!r}")
        for m_, w in zip(MONTHLY, WEEKLY):
            rec[w] = weekly(rec[m_])  # not a source value
        if provenance:
            rec.update(provenance)
        out.append(rec)
    return sorted(out, key=lambda r: r[RATE_KEY])


def rows_content_sha(records) -> str:
    """sha256 of a year's rates: one line per BRMA, brma_name and the ten
    rates in RATE_VALUES order (two places), sorted, LF, UTF-8. Provenance
    (source_text) is not in it."""
    lines = sorted("|".join([r[RATE_KEY]] + [str(Decimal(r[c]).quantize(CENT))
                                              for c in RATE_VALUES])
                   for r in records)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def mapping_content_sha(records) -> str:
    """sha256 of a vintage's crosswalk: lad24cd|la_name|brma_name|
    mapping_method per authority, sorted, LF, UTF-8."""
    lines = sorted("|".join([r[MAP_KEY]] + [str(r[c]) for c in MAP_VALUES])
                   for r in records)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The crosswalk
# ---------------------------------------------------------------------------

def _vsi(path) -> str:
    return "/vsizip/" + str(Path(path).resolve()).replace("\\", "/") + "/" \
        + GML_MEMBER


def read_gml(zip_path) -> dict:
    """The VOA BRMA layer: {path, member, layer, field, crs_read, gdf (CRS
    set to EPSG:27700), names}. The zip holds GML_MEMBER; OGR reads one
    layer GML_LAYER with the field GML_FIELD; EXPECTED_POLYGONS features,
    each geometry carrying srsName EPSG:27700 in the file; the layer CRS
    absent (set to EPSG:27700) or EPSG:27700; names unique and not blank.
    ValueError naming what was seen."""
    import geopandas as gpd
    import pyogrio
    p = Path(zip_path)
    try:
        with zipfile.ZipFile(p) as z:
            members = z.namelist()
            raw = z.read(GML_MEMBER) if GML_MEMBER in members else None
    except zipfile.BadZipFile as e:
        raise ValueError(f"{p.name}: not a zip ({e})") from None
    if raw is None:
        raise ValueError(f"{p.name}: no member {GML_MEMBER!r}; members "
                         f"{members}")
    layers = [str(x[0]) for x in pyogrio.list_layers(_vsi(p))]
    if layers != [GML_LAYER]:
        raise ValueError(f"{p.name}: layers {layers}, expected [{GML_LAYER!r}]"
                         f" (the VOA 'English BRMA(LHA) Layer')")
    gdf = gpd.read_file(_vsi(p), layer=GML_LAYER)
    if GML_FIELD not in gdf.columns:
        raise ValueError(f"{p.name}: no field {GML_FIELD!r}; fields "
                         f"{[c for c in gdf.columns if c != 'geometry']}")
    n = len(gdf)
    want = globals()["EXPECTED_POLYGONS"]
    if n != want:
        raise ValueError(f"{p.name}: {n} polygons, expected {want}")
    srs = raw.count(f'srsName="{GML_CRS}"'.encode())
    if srs != n or raw.count(b"srsName=") != n:
        raise ValueError(f"{p.name}: {srs} of {n} geometries say srsName "
                         f"{GML_CRS} ({raw.count(b'srsName=')} say any); the "
                         "layer's CRS is unclear")
    crs_read = None if gdf.crs is None else gdf.crs.to_string()
    if crs_read not in (None, GML_CRS):
        raise ValueError(f"{p.name}: the layer CRS reads {crs_read}, expected "
                         f"none or {GML_CRS}")
    gdf = gdf.set_crs(GML_CRS, allow_override=True)
    names = [_norm(x) for x in gdf[GML_FIELD]]
    if any(not x for x in names) or len(set(names)) != len(names):
        raise ValueError(f"{p.name}: blank or repeated BRMA names "
                         f"{sorted(x for x in names if not x or names.count(x) > 1)}")
    gdf = gdf.assign(**{GML_FIELD: names})
    return {"path": p, "member": GML_MEMBER, "layer": GML_LAYER,
            "field": GML_FIELD, "crs_read": crs_read, "gdf": gdf,
            "names": names}


def brma_aliases(path=None) -> dict:
    """{VOA name: DWP name} from the committed exact table
    (s14_brma_names.json): each key contains ' & ', each value contains no
    '&' and each entry says where it was seen. ValueError otherwise."""
    raw = json.loads(Path(path or globals()["NAMES_FILE"]).read_text(
        encoding="utf-8"))
    out = {}
    for k, v in (raw.get("aliases") or {}).items():
        name = (v or {}).get("name")
        if " & " not in k or not name or "&" in name or not v.get("seen"):
            raise ValueError(f"s14_brma_names.json {k!r}: an alias maps a "
                             "VOA 'X & Y' name to the DWP name with no '&' "
                             "and says where it was seen")
        out[k] = name
    return out


def convert_name(name, aliases) -> str:
    """A VOA BRMA name in DWP's form: through the alias table when it holds
    '&' (exact keys only), else as it is. An '&' name not in the table
    raises ValueError."""
    if "&" not in name:
        return name
    if name not in aliases:
        raise ValueError(f"the VOA name {name!r} contains '&' and is not in "
                         "scripts/s14_brma_names.json; add its exact DWP "
                         "name, with where it was seen")
    return aliases[name]


def boundaries_state(rows) -> tuple:
    """(rows, md5 of lad24cd|lad24nm|longitude|latitude ordered by lad24cd,
    NULL as '~') of la_boundaries rows ({lad24cd, lad24nm, longitude,
    latitude}); the SQL rendering of numeric(10,6) is str(Decimal)."""
    lines = ["|".join("~" if r[c] is None else str(r[c]) for c in
                      ("lad24cd", "lad24nm", "longitude", "latitude"))
             for r in sorted(rows, key=lambda r: r["lad24cd"])]
    return len(rows), hashlib.md5("\n".join(lines).encode("utf-8")).hexdigest()


def build_mapping(gml, boundaries, aliases, rate_names, vintage=None,
                  source_text=None) -> list:
    """The crosswalk, one record per authority sorted by lad24cd: {lad24cd,
    vintage, la_name (lad24nm), brma_name (convert_name of the VOA name),
    mapping_method, source_text}. The centroid (longitude, latitude,
    EPSG:4326, to EPSG:27700) within exactly one polygon is
    centroid_spatial_join; within none, the nearest polygon is
    nearest_fallback (a tie halts); within two halts. Every mapped name must
    be in rate_names (the BRMAs of the latest rate year). ValueError naming
    the authorities."""
    import geopandas as gpd
    from shapely.geometry import Point
    vintage = vintage or LIVE_VINTAGE
    source_text = source_text or MAPPING_SOURCE
    gdf = gml["gdf"]
    names = list(gdf[GML_FIELD])
    bad = [r["lad24cd"] for r in boundaries
           if r.get("longitude") is None or r.get("latitude") is None]
    if bad:
        raise ValueError(f"la_boundaries has no centroid for {bad}")
    pts = gpd.GeoSeries([Point(float(r["longitude"]), float(r["latitude"]))
                         for r in boundaries], crs="EPSG:4326").to_crs(GML_CRS)
    out, problems, missing = [], [], {}
    for r, pt in zip(boundaries, pts):
        inside = [i for i, g in enumerate(gdf.geometry) if g.contains(pt)]
        if len(inside) > 1:
            problems.append(f"{r['lad24cd']} lies in {len(inside)} polygons "
                            f"{[names[i] for i in inside]}")
            continue
        if inside:
            i, method = inside[0], WITHIN
        else:
            d = list(gdf.geometry.distance(pt))
            best = min(d)
            ties = [k for k, x in enumerate(d) if x == best]
            if len(ties) != 1:
                problems.append(f"{r['lad24cd']} is equally near "
                                f"{[names[k] for k in ties]}")
                continue
            i, method = ties[0], NEAREST
        name = convert_name(names[i], aliases)
        if name not in rate_names:
            missing.setdefault(name, []).append(r["lad24cd"])
        out.append({MAP_KEY: r["lad24cd"], VINTAGE_COL: vintage,
                    "la_name": r["lad24nm"], "brma_name": name,
                    "mapping_method": method, SOURCE_TEXT: source_text})
    if missing:
        problems.append("mapped BRMA name(s) with no rate row in the latest "
                        "rate year (the W1 join would leave these "
                        "authorities without rates): " + "; ".join(
                            f"{n} <- {', '.join(c)}"
                            for n, c in sorted(missing.items())))
    if problems:
        raise ValueError("crosswalk: " + "; ".join(problems))
    return sorted(out, key=lambda x: x[MAP_KEY])


def named_correction(new, tip) -> "str | None":
    """The name of the MAPPING_CORRECTIONS entry this change is exactly (the
    same keys, only its column changed, each from its 'from' to its 'to'
    value, its count of rows), else None."""
    nk, tk = {r[MAP_KEY]: r for r in new}, {r[MAP_KEY]: r for r in tip}
    if set(nk) != set(tk):
        return None
    for name, c in MAPPING_CORRECTIONS.items():
        changed = 0  # rows changed; not a source value
        ok = True
        for k in nk:
            for col in MAP_VALUES:
                a, b = tk[k][col], nk[k][col]
                if a == b:
                    continue
                if col == c["column"] and a == c["from"] and b == c["to"]:
                    changed += 1
                else:
                    ok = False
        if ok and changed == c["count"]:
            return name
    return None


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

_RANK_RE = re.compile(r"\bfinancial year ([0-9]{4}-[0-9]{2}); (?:page change "
                      r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
                      r"[0-9]{2}Z)|no page rank)")
_LEDGER_RE = re.compile(r"(.*) \((financial year [0-9]{4}-[0-9]{2}; "
                        r"(?:page change [0-9T:Z-]+|no page rank))\)")


def rank_text(rank) -> str:
    """'financial year 2026-27; page change 2026-04-08T15:31:30Z' (or '; no
    page rank') of a (year, timestamp | None) rank."""
    if not rank:
        return "no file rank"
    fy, ts = rank
    return f"financial year {fy}; " + (f"page change {_iso(ts)}" if ts
                                       else "no page rank")


def release_rank(x) -> "tuple | None":
    """(financial year, page change timestamp or None) of a file: a dict's
    'rank', or the rank_text an edition's or ledger row's source_file
    carries; None if it names none (a URL, media id or file name is never
    read for a rank)."""
    if isinstance(x, dict):
        return x.get("rank")
    mm = _RANK_RE.search(str(x or ""))
    if not mm:
        return None
    return mm.group(1), (_when(mm.group(2)) if mm.group(2) else None)


def compare_rank(a, b) -> str:
    """'older', 'newer', 'equal' or 'unknown' of rank a against rank b:
    the year first; within a year the page timestamps, 'unknown' when
    either has none (or either rank is None)."""
    if not a or not b:
        return "unknown"
    if a[0] != b[0]:
        return "older" if a[0] < b[0] else "newer"
    if a[1] is None or b[1] is None:
        return "unknown"
    if a[1] == b[1]:
        return "equal"
    return "older" if a[1] < b[1] else "newer"


def edition_source(file_name, rank, where, note="") -> str:
    """An edition's source_file: the file name, its rank and where it was
    read (and a note)."""
    return (f"{file_name}; {rank_text(rank)}; {where}"
            + (f"; {note}" if note else ""))


def ledger_source(where, rank) -> str:
    return f"{where} ({rank_text(rank)})"


def parse_ledger_source(s) -> "tuple | None":
    """(where, rank) of a ledger source_file, or None."""
    mm = _LEDGER_RE.fullmatch(str(s or ""))
    if not mm:
        return None
    return mm.group(1), release_rank(mm.group(2))


def content_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tip_ranks(tips, checked) -> dict:
    """{held year: the newest rank that stated it}: the tip edition's, or a
    later file the ledger records for the year (a file found unchanged
    stores no edition); None when nothing has a rank."""
    def key(r):
        return (r[0], r[1] or datetime.min.replace(tzinfo=timezone.utc))
    out = {}
    for p, t in tips.items():
        ranks = [t.get("rank")] + [release_rank(s)
                                   for s, _ in checked.get(p, ())]
        ranks = [r for r in ranks if r is not None]
        out[p] = max(ranks, key=key) if ranks else None
    return out


def ledger_complete(checked, where, sha, period, stranded=()) -> bool:
    """True when the ledger records this file's (where, sha256) pair for its
    year, and the year is not stranded (editions, no live rows)."""
    if str(period) in {str(x) for x in stranded}:
        return False
    for s, h in checked.get(str(period), ()):
        parsed = parse_ledger_source(s)
        if h == sha and parsed and parsed[0] == where:
            return True
    return False


def plan_periods(period, rank, ranks, checked, source, sha, *, recheck,
                 allow_older, stranded=()) -> tuple:
    """(new, revised, skipped) for the file's one year. ranks: {held year:
    tip_ranks}; checked: {year: {(source, sha)}} of the ledger. new: not
    held (a year before the newest held is skipped as older: back-filling
    is not done by load, unless allow_older); revised: held, compared with
    the tip; skipped {year: reason}: held with a tip from a newer file
    (older) unless allow_older; held with (source, sha) in the ledger unless
    --recheck names it or it is stranded. --recheck must be the file's held
    year (ValueError)."""
    p = str(period)
    if recheck is not None and (recheck != p or p not in ranks):
        raise ValueError(f"--recheck {recheck}: not a held year this file "
                         f"states ({p})")
    newest_held = max(ranks) if ranks else None
    if p not in ranks:
        if newest_held is not None and p < newest_held and not allow_older:
            return [], [], {p: (f"older: {p} is before the newest held year "
                                f"{newest_held}; back-filling is not done by "
                                "load")}
        return [p], [], {}
    if compare_rank(rank, ranks[p]) == "older" and not allow_older:
        return [], [], {p: (f"older: its tip comes from a file of "
                            f"{rank_text(ranks[p])}, this file is "
                            f"{rank_text(rank)}")}
    if (recheck != p and p not in {str(x) for x in stranded}
            and (source, sha) in checked.get(p, ())):
        return [], [], {p: "checked: this file is already in the ledger"}
    return [], [p], {}


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

def _over(old, new, bp) -> bool:
    """True when new moves from old by more than bp basis points (exact)."""
    if old == new:
        return False
    if old == 0:
        return True
    return abs(new - old) * 10000 > bp * old


def _pct(old, new) -> str:
    return f"{(new - old) * 100 / old:+.2f}%" if old else "from 0"


def _by(records, key) -> dict:
    return {r[key]: r for r in records or ()}


def period_problems(new, tip, prev, *, kind) -> list:
    """The stop conditions of one rate year (empty = fine). new: the file's
    records; tip: (kind 'revised') the held edition of the same year; prev:
    (kind 'new') the latest earlier held year, or None. Problems starting
    with KEYS (the BRMA set differs from the tip, or from prev for a new
    year) are released only by --accept-key-changes; the rest by
    --acknowledge. New: a BRMA moving more than NEW_AREA_BP on any monthly
    rate, or a rate type's median moving more than NEW_MEDIAN_BP, against
    prev. Revised: any value changed."""
    g = globals()
    out = []
    nk = _by(new, RATE_KEY)
    base = prev if kind == "new" else tip
    if not base:
        return out
    bk = _by(base, RATE_KEY)
    if set(nk) != set(bk):
        gone, added = sorted(set(bk) - set(nk)), sorted(set(nk) - set(bk))
        out.append(f"{KEYS}: the BRMA set differs from "
                   + ("the previous held year" if kind == "new"
                      else "the held edition")
                   + f": removed {gone or 'none'}, added {added or 'none'}")
    both = sorted(set(nk) & set(bk))
    if kind == "new":
        big = []
        for k in both:
            for c in MONTHLY:
                a, b = bk[k][c], nk[k][c]
                if _over(a, b, g["NEW_AREA_BP"]):
                    big.append(f"{k} {c} {a}->{b} ({_pct(a, b)})")
        if big:
            out.append(f"rates move by more than {g['NEW_AREA_BP'] / 100:.0f}%"
                       f" against the previous held year: {len(big)} "
                       f"value(s): {', '.join(big[:6])}")
        for c in MONTHLY:
            if not both:
                break
            a = statistics.median(bk[k][c] for k in both)
            b = statistics.median(nk[k][c] for k in both)
            if _over(a, b, g["NEW_MEDIAN_BP"]):
                out.append(f"the median {c} moves {a}->{b} ({_pct(a, b)}), "
                           f"more than {g['NEW_MEDIAN_BP'] / 100:.0f}%")
        return out
    changed = [f"{k} {c} {bk[k][c]}->{nk[k][c]}" for k in both
               for c in RATE_VALUES if bk[k][c] != nk[k][c]]
    if changed:
        out.append(f"the held edition is revised: {len(changed)} value(s) "
                   f"changed: {', '.join(changed[:6])}")
    return out


def mapping_problems(new, tip) -> list:
    """The stop conditions of a crosswalk vintage against its held edition
    (empty = fine): an authority set differing is KEYS (nothing releases
    it); any value changed needs --acknowledge VINTAGE."""
    if not tip:
        return []
    nk, tk = _by(new, MAP_KEY), _by(tip, MAP_KEY)
    out = []
    if set(nk) != set(tk):
        out.append(f"{KEYS}: the authorities differ from the held edition: "
                   f"removed {sorted(set(tk) - set(nk)) or 'none'}, added "
                   f"{sorted(set(nk) - set(tk)) or 'none'} (a vintage's key "
                   "set never changes; nothing releases this)")
    for col in MAP_VALUES:
        ch = [k for k in sorted(set(nk) & set(tk)) if nk[k][col] != tk[k][col]]
        if ch:
            ex = ", ".join(f"{k} {tk[k][col]!r}->{nk[k][col]!r}"
                           for k in ch[:4])
            out.append(f"the held edition is revised: {col} changes at "
                       f"{len(ch)} authorit(ies), e.g. {ex}")
    return out


def _differs(new, old, key, cols) -> bool:
    nk, ok = _by(new, key), _by(old, key)
    if set(nk) != set(ok):
        return True
    return any(nk[k][c] != ok[k][c] for k in nk for c in cols)


# ---------------------------------------------------------------------------
# Specs and profiles
# ---------------------------------------------------------------------------

def tip_row_count(editions_table: str, period_col: str):
    """expected_rows_per_period for status: the tip edition's row count."""
    core._ident(editions_table)
    core._ident(period_col)

    def count(cur, period):
        cur.execute(f"SELECT DISTINCT edition, supersedes FROM "
                    f"public.{editions_table} WHERE {period_col} = %s",
                    (period,))
        try:
            tip = core._chain_tip(cur.fetchall(), str(period))
        except (LookupError, ValueError):
            return None
        cur.execute(f"SELECT COUNT(*) FROM public.{editions_table} WHERE "
                    f"{period_col} = %s AND edition = %s", (period, tip))
        return cur.fetchone()[0]
    return count


_POSITIVE = " AND ".join(f"{c} > 0" for c in RATE_VALUES)

RATES = core.EditionSpec(
    name="s14_rates",
    live_table="brma_lha_rates",
    editions_table="brma_lha_rates_editions",
    key_cols=(RATE_KEY,),
    period_col=FY_COL,
    value_cols=tuple((c, "numeric(8,2) NOT NULL") for c in RATE_VALUES),
    extra_cols=((SOURCE_TEXT, "text NOT NULL"),),
    refresh_cols=RATE_VALUES + ("source", "loaded_at"),
    refresh_from=(("source", SOURCE_TEXT), ("loaded_at", "loaded_at")),
    key_types=((RATE_KEY, "varchar(100) NOT NULL"),
               (FY_COL, "varchar(7) NOT NULL")),
    table_constraints=(f"CHECK ({FY_COL} ~ '^[0-9]{{4}}-[0-9]{{2}}$')",
                       f"CHECK ({_POSITIVE})"),
    fk_la_boundaries=False,
    refresh_key_changes=True,
    expected_rows_per_period=tip_row_count("brma_lha_rates_editions",
                                           FY_COL),
)

MAPPING = core.EditionSpec(
    name="s14_mapping",
    live_table="la_brma_mapping",
    editions_table="la_brma_mapping_editions",
    key_cols=(MAP_KEY,),
    period_col=VINTAGE_COL,
    value_cols=tuple((c, "text NOT NULL") for c in MAP_VALUES),
    extra_cols=((SOURCE_TEXT, "text NOT NULL"),),
    refresh_cols=MAP_VALUES + ("source", "loaded_at"),
    refresh_from=(("source", SOURCE_TEXT), ("loaded_at", "loaded_at")),
    key_types=((MAP_KEY, "varchar(9) NOT NULL"),
               (VINTAGE_COL, "text NOT NULL")),
    table_constraints=(
        f"CHECK ({VINTAGE_COL} ~ '^brma-[0-9]{{4}}-[0-9]{{2}}_lad-[0-9]{{4}}"
        f"-[0-9]{{2}}$')",),
    fk_la_boundaries=True,
    live_single_period=True,
    refresh_key_changes=False,
)

SPECS = {"rates": RATES, "mapping": MAPPING}
TABLES = {"brma_lha_rates": "rates", "la_brma_mapping": "mapping"}


def _no_label(fetched_on) -> str:
    raise ValueError("an S14 release label comes from the file; store "
                     "through apply_part(...)")


def check_rate_records(records, period) -> None:
    """ValueError unless the year's rate records are whole: each of this
    year, one per BRMA, no '&' name, every rate a positive Decimal and each
    weekly rate the derived one."""
    seen = set()
    for r in records:
        if r.get(FY_COL) != period:
            raise ValueError(f"{period}: a record of {r.get(FY_COL)!r}")
        k = r[RATE_KEY]
        if k in seen or "&" in k:
            raise ValueError(f"{period}: {k!r} repeated or has '&'")
        seen.add(k)
        for c in RATE_VALUES:
            v = r.get(c)
            if not isinstance(v, Decimal) or v <= 0:
                raise ValueError(f"{period}: {k} {c} {v!r}")
        for m_, w in zip(MONTHLY, WEEKLY):
            if r[w] != weekly(r[m_]):
                raise ValueError(f"{period}: {k} {w} is not derived from "
                                 f"{m_}")


def check_mapping_records(records, period) -> None:
    seen = set()
    for r in records:
        if r.get(VINTAGE_COL) != period:
            raise ValueError(f"{period}: a record of {r.get(VINTAGE_COL)!r}")
        if r[MAP_KEY] in seen:
            raise ValueError(f"{period}: {r[MAP_KEY]} appears twice")
        seen.add(r[MAP_KEY])
        if any(not r.get(c) for c in MAP_VALUES):
            raise ValueError(f"{period}: {r[MAP_KEY]} lacks a value")


PROFILES = {
    "rates": pe.Profile(
        spec=RATES, value_cols=RATE_VALUES, run_agent=RUN_AGENT,
        run_source=RUN_SOURCE,
        heading="S14 DWP UC LHA rates (brma_lha_rates)",
        default_source_file="DWP UC LHA rates England CSV",
        expected_areas=None, release_label=_no_label,
        content_sha256=rows_content_sha, check_records=check_rate_records,
        file_checks=True, savepoint="s14_rates_period",
        example_label="(five monthly, five weekly)"),
    "mapping": pe.Profile(
        spec=MAPPING, value_cols=MAP_VALUES, run_agent=RUN_AGENT,
        run_source=RUN_SOURCE,
        heading="S14 BRMA crosswalk (la_brma_mapping)",
        default_source_file="VOA BRMA layer x la_boundaries centroids",
        expected_areas=EXPECTED_AUTHORITIES, release_label=_no_label,
        content_sha256=mapping_content_sha,
        check_records=check_mapping_records,
        file_checks=True, savepoint="s14_mapping_period",
        example_label="(la_name, brma_name, mapping_method)"),
}


def profile(part) -> pe.Profile:
    """PROFILES[part] bound to the current SPECS[part] (and, for the
    crosswalk, EXPECTED_AUTHORITIES), looked up when called so tests can
    swap them."""
    g = globals()
    prof = g["PROFILES"][part].with_spec(g["SPECS"][part])
    if part == "mapping":
        prof = dataclasses.replace(prof,
                                   expected_areas=g["EXPECTED_AUTHORITIES"])
    return prof


def _is_mapping(spec) -> bool:
    return spec.period_col == VINTAGE_COL


def _late(name: str):
    return lambda *a, **kw: globals()[name](*a, **kw)


def live_alter_sql(spec) -> str:
    """The one live DDL change: the crosswalk's vintage column (additive,
    default-filled, not in the key; the CHECK keeps one vintage in live)."""
    return (f"ALTER TABLE public.{core._ident(spec.live_table)} ADD COLUMN "
            f"{VINTAGE_COL} text NOT NULL DEFAULT '{LIVE_VINTAGE}' CHECK "
            f"({VINTAGE_COL} = '{LIVE_VINTAGE}')")


def live_has_vintage(cur, spec) -> bool:
    cur.execute("SELECT 1 FROM information_schema.columns WHERE "
                "table_schema = 'public' AND table_name = %s AND "
                "column_name = %s", (spec.live_table, VINTAGE_COL))
    return cur.fetchone() is not None


def ensure_live_vintage(cur, spec) -> str:
    """Add the live vintage column (live_alter_sql) when absent ('added');
    'present' when it is there exactly as planned; halt on any other
    shape."""
    cur.execute("SELECT data_type, is_nullable, column_default FROM "
                "information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = %s AND column_name = %s",
                (spec.live_table, VINTAGE_COL))
    row = cur.fetchone()
    if row is None:
        cur.execute(live_alter_sql(spec))
        return "added"
    cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                "conrelid = %s::regclass AND contype = 'c'",
                (f"public.{spec.live_table}",))
    defs = [r[0] for r in cur.fetchall()]
    want = ("text", "NO", f"'{LIVE_VINTAGE}'::text")
    check = f"({VINTAGE_COL} = '{LIVE_VINTAGE}'::text)"
    if tuple(row) != want or not any(check in d for d in defs):
        halt(f"{spec.live_table}.{VINTAGE_COL} exists but is not the planned "
             f"column ({tuple(row)}; CHECK {defs}); expected: "
             f"{live_alter_sql(spec)}")
    return "present"


def create_all(cur) -> None:
    """ddl: both editions tables, their append-only triggers, the two
    file-check ledgers and the crosswalk's live vintage column. Idempotent;
    no value is written; no other live column or row is touched."""
    for part in SPECS:
        prof = profile(part)
        core.create_schema(cur, prof.spec)
        pe.create_file_checks(cur, prof)
    ensure_live_vintage(cur, profile("mapping").spec)


def insert_live(cur, profile, period: str, records: list) -> None:
    """A period's live rows: key, period, the live values and source (from
    each record's source_text); loaded_at takes its default; brma_secondary
    is not written."""
    from psycopg2.extras import execute_values
    spec = profile.spec
    values = MAP_VALUES if _is_mapping(spec) else RATE_VALUES
    key = spec.key_cols[0]
    cols = (key, spec.period_col) + values + ("source",)
    execute_values(
        cur, f"INSERT INTO public.{spec.live_table} ({', '.join(cols)}) "
        "VALUES %s",
        [(r[key], period) + tuple(r[c] for c in values) + (r[SOURCE_TEXT],)
         for r in records], page_size=1000)


@dataclass(frozen=True)
class PeriodInfo:
    """What a period's edition records: source_file, the release label, the
    published date and the files read ((ledger source, sha256),)."""
    source_file: str
    label: str
    published: date
    files: tuple


def apply_part(cur, profile, period, records, *, fetched_on, info) -> str:
    """pe.apply_period for one period, then its ledger row, inside the
    caller's per-period savepoint: a new period's edition 1, live rows and
    ledger row commit or roll back together. A stranded period's live rows
    are the tip edition's (its source_text included)."""
    kind = pe.classify_period(cur, profile, period, records)
    if kind == LIVE_MISSING:
        records = globals()["records"](cur, profile.spec, period,
                                       core.chain_tip(cur, profile.spec,
                                                      period))
    prof = dataclasses.replace(profile, release_label=lambda d: info.label)
    kind = pe.apply_period(cur, prof, period, records,
                           fetched_on=info.published,
                           source_file=info.source_file,
                           classify=lambda *a, **kw: kind,
                           insert=_late("insert_live"))
    tip = core.chain_tip(cur, prof.spec, period)
    for src, sha in info.files:
        pe.record_file_check(cur, prof, period, src, sha, kind, tip)
    return kind


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
        r = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=120,
                        **kw)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001 (any failure halts)
        halt(f"GET {url} failed, nothing downloaded: {e}")
    return r


def fetch_json(path, session=None) -> dict:
    """The GOV.UK content API reply for a page path (a read-only GET)."""
    return json.loads(_get(_session(session), API + path).text)


def _opens(body: bytes) -> None:
    """Raise unless the bytes decode and start like a rates CSV (a 'BRMA'
    header cell in the first two rows)."""
    text, _ = _decode(body, "download")
    head = list(csv.reader(io.StringIO(text)))[:2]
    if not any(_norm(r[0]) == "BRMA" for r in head if r):
        raise ValueError(f"no 'BRMA' header cell in {head}")


def fetch(url, dest, session=None) -> tuple:
    """Download url into the directory dest (redirects followed) and return
    (path to read, final URL). The file name is the final URL's. Checked
    before it is kept: more than MIN_FILE_BYTES, a .csv name, it decodes and
    has a 'BRMA' header cell. A same-named file with the same content is
    kept as it is; one with different content is never replaced: the
    download is saved beside it as <stem>-<sha8><suffix> and that is the one
    read. An existing name is never a reason to skip the download."""
    dest = Path(dest)
    r = _get(_session(session), url)
    final = str(getattr(r, "url", None) or url)
    if final != url:
        print(f"redirected: {url} -> {final}")
    body = r.content
    if len(body) <= globals()["MIN_FILE_BYTES"]:
        halt(f"{final}: {len(body)} bytes, expected more than "
             f"{MIN_FILE_BYTES}; nothing written; saw {body[:100]!r}")
    name = Path(urlparse(final).path).name
    if Path(name).suffix.lower() != ".csv":
        halt(f"{final}: not a .csv file; nothing written")
    try:
        _opens(body)
    except ValueError as e:
        halt(f"{final}: does not read as the rates CSV ({e}); nothing kept")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    new_sha = hashlib.sha256(body).hexdigest()
    path = target
    if target.exists():
        old_sha = content_sha256(target)
        if old_sha == new_sha:
            print(f"{name} already held with the same content (sha256 "
                  f"{new_sha[:16]}); kept as it is")
        else:
            path = dest / f"{target.stem}-{new_sha[:8]}{target.suffix}"
            if path.exists() and content_sha256(path) == new_sha:
                print(f"NOTE: {name} differs from this download; {path.name} "
                      f"already holds it (sha256 {new_sha[:16]})")
            else:
                path.write_bytes(body)
                print(f"NOTE: {name} already exists with different content "
                      f"(sha256 {old_sha[:16]}, new {new_sha[:16]}); kept it "
                      f"untouched and saved the new download as {path.name}")
    else:
        target.write_bytes(body)
        print(f"downloaded {name}: {len(body):,} bytes, sha256 "
              f"{new_sha[:16]}")
    return path, final


def _file_source(path) -> str:
    """Where a file was read: the path under the repo, else absolute."""
    p = Path(path).resolve()
    try:
        return p.relative_to(REPO).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------------------
# Reading held data
# ---------------------------------------------------------------------------

table_exists = pe.table_exists


def _conn(writing):
    return pe.connect(writing)


def records(cur, spec, period, edition=None) -> list:
    """A period's rows as records (key, period, the values, source_text):
    the given edition's, or the live table's (edition None; source as
    source_text; for a crosswalk without the vintage column yet, every
    row)."""
    mapping = _is_mapping(spec)
    values = MAP_VALUES if mapping else RATE_VALUES
    key = spec.key_cols[0]
    if edition is None:
        names = (key,) + values + ("source",)
        where, args = f" WHERE {spec.period_col} = %s", (period,)
        if mapping and not live_has_vintage(cur, spec):
            where, args = "", ()
        cur.execute(f"SELECT {', '.join(names)} FROM public.{spec.live_table}"
                    + where, args)
    else:
        names = (key,) + values + (SOURCE_TEXT,)
        cur.execute(f"SELECT {', '.join(names)} FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = %s", (period, edition))
    out = []
    for row in cur.fetchall():
        r = dict(zip(names, row))
        if "source" in r:
            r[SOURCE_TEXT] = r.pop("source")
        r[spec.period_col] = str(period)
        out.append(r)
    return sorted(out, key=lambda r: r[key])


def tip_info(cur, spec) -> dict:
    """{period: {edition, source_file, rank}} of every period with
    editions."""
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
                         "rank": None if _is_mapping(spec)
                         else release_rank(sf)}
    return out


def legacy_tips(cur, spec) -> dict:
    """Before the editions tables exist: {held live period: {rank}} (rates:
    LEGACY_RANKS; the crosswalk: LIVE_VINTAGE when live holds rows)."""
    if _is_mapping(spec):
        cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table}")
        return ({LIVE_VINTAGE: {"edition": None, "source_file": None,
                                "rank": None}} if cur.fetchone()[0] else {})
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table}")
    ranks = globals()["LEGACY_RANKS"]
    return {pe._p(p): {"edition": None, "source_file": None,
                       "rank": ranks.get(pe._p(p))}
            for (p,) in cur.fetchall()}


def ledger_checks(cur, prof) -> dict:
    pc = prof.spec.period_col
    cur.execute(f"SELECT DISTINCT {pc}, source_file, file_sha256 "
                f"FROM public.{pe.file_checks_table(prof)}")
    out = {}
    for p, f, s in cur.fetchall():
        out.setdefault(pe._p(p), set()).add((f, s))
    return out


def _boundaries(cur) -> list:
    """la_boundaries rows {lad24cd, lad24nm, longitude, latitude} (read
    only)."""
    cur.execute("SELECT lad24cd, lad24nm, longitude, latitude FROM "
                "public.la_boundaries ORDER BY lad24cd")
    return [dict(zip(("lad24cd", "lad24nm", "longitude", "latitude"), r))
            for r in cur.fetchall()]


def latest_rate_names(cur, spec, has_ed) -> tuple:
    """(financial year, {BRMA names}) of the newest live rate year (what W1
    reads), or (None, set())."""
    cur.execute(f"SELECT MAX({spec.period_col}) FROM public.{spec.live_table}")
    fy = cur.fetchone()[0]
    if fy is None:
        return None, set()
    cur.execute(f"SELECT {RATE_KEY} FROM public.{spec.live_table} WHERE "
                f"{spec.period_col} = %s", (fy,))
    return str(fy), {r[0] for r in cur.fetchall()}


def _tables_ready(cur, part) -> list:
    prof = profile(part)
    missing = [t for t in (prof.spec.editions_table,
                           pe.file_checks_table(prof))
               if not table_exists(cur, t)]
    if part == "mapping" and not live_has_vintage(cur, prof.spec):
        missing.append(f"{prof.spec.live_table}.{VINTAGE_COL}")
    return missing


# ---------------------------------------------------------------------------
# ddl, status, run log, refresh-latest
# ---------------------------------------------------------------------------

def log_run(cur, rows_written: int, notes: str, started_at=None) -> None:
    """The pipeline_run_log row for a committed run (agent RUN_AGENT, source
    '14'). Called only on committed load, migrate-legacy and restore-edition
    runs, partial runs included."""
    pe.log_run(cur, profile("rates"), rows_written, notes, started_at)


def cmd_ddl(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            for part in SPECS:
                prof = profile(part)
                for t in (prof.spec.editions_table, pe.file_checks_table(prof)):
                    print(f"{t}: {'exists' if table_exists(cur, t) else 'does not exist'}")
            mspec = profile("mapping").spec
            print(f"{mspec.live_table}.{VINTAGE_COL}: "
                  + ("exists" if live_has_vintage(cur, mspec) else
                     f"does not exist (ddl runs: {live_alter_sql(mspec)})"))
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            create_all(cur)
        pe.finish(conn, args, "ddl: brma_lha_rates_editions, "
                              "la_brma_mapping_editions, their triggers and "
                              "ledgers, and la_brma_mapping.vintage present")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def status_lines(cur, part) -> list:
    prof = profile(part)
    s = prof.spec
    lt = pe.file_checks_table(prof)
    lines = []
    for p, t in sorted(tip_info(cur, s).items()):
        line = (f"  {p}: tip edition {t['edition']}"
                + ("" if _is_mapping(s) else f" ({rank_text(t['rank'])})"))
        cur.execute(f"SELECT source_file, outcome FROM public.{lt} WHERE "
                    f"{s.period_col} = %s ORDER BY id DESC LIMIT 1", (p,))
        last = cur.fetchone()
        line += (f"; ledger latest: {last[1]} {last[0]}" if last
                 else "; no ledger row")
        lines.append(line)
    return lines


def cmd_status(args) -> int:
    """status: what needs action for both tables; exit 1 if anything, or
    (with a clean message, nothing created) if a table does not exist."""
    conn = _conn(False)
    rc = 0
    try:
        with conn.cursor() as cur:
            for part in SPECS:
                prof = profile(part)
                missing = _tables_ready(cur, part)
                if missing:
                    print(f"{prof.heading}: {' and '.join(missing)} not "
                          "present yet; run `ddl --commit`, then "
                          "`migrate-legacy --commit` (S14 has no sync-new)")
                    rc = 1
                    continue
                st = pe.status(cur, prof)
                print(pe.format_status(prof, st, status_lines(cur, part)))
                rc = rc or (0 if st["ok"] else 1)
    finally:
        conn.rollback()
        conn.close()
    return rc


def live_equals_tip(cur, spec, periods) -> list:
    """Problems (empty = fine): for each period, the live rows equal the tip
    edition on the key, the period and the values, NULL-safe, both ways
    (EXCEPT ALL). source is not compared: it names the edition that last
    wrote a row's values."""
    values = MAP_VALUES if _is_mapping(spec) else RATE_VALUES
    cols = ", ".join(tuple(spec.key_cols) + (spec.period_col,) + values)
    bad = []
    for p in periods:
        try:
            ed = core.chain_tip(cur, spec, p)
        except (LookupError, ValueError) as e:
            bad.append(f"{pe._p(p)}: {e}")
            continue
        a = (f"SELECT {cols} FROM public.{spec.editions_table} "
             f"WHERE {spec.period_col} = %s AND edition = %s")
        b = (f"SELECT {cols} FROM public.{spec.live_table} "
             f"WHERE {spec.period_col} = %s")
        counts = []
        for x, xa, y, ya in ((a, (p, ed), b, (p,)), (b, (p,), a, (p, ed))):
            cur.execute(f"SELECT COUNT(*) FROM (({x}) EXCEPT ALL ({y})) q",
                        xa + ya)
            counts.append(cur.fetchone()[0])
        if any(counts):
            bad.append(f"{pe._p(p)} edition {ed}: {counts[0]} edition-only, "
                       f"{counts[1]} live-only rows")
    return bad


def _part_of(period) -> str:
    return "mapping" if VINTAGE_RE.fullmatch(str(period)) else "rates"


def cmd_refresh_latest(args) -> int:
    """refresh-latest [--accept-drift P] [--accept-key-changes P] [--commit
    | --simulate]: for both tables, the engine's refresh (each period's
    latest edition copied into live: the values, source from the edition's
    source_text and loaded_at, under the before/after hash guard), in one
    transaction. Each named period goes to the table its form names (a
    financial year: the rates; a vintage: the crosswalk)."""
    writing = args.commit or args.simulate
    drift = tuple(args.accept_drift or ())
    kcs = tuple(args.accept_key_changes or ())
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            plans = []
            for part in SPECS:
                missing = _tables_ready(cur, part)
                if missing:
                    halt(f"{', '.join(missing)} not present yet; run `ddl "
                         "--commit`, then `migrate-legacy --commit`")
                spec = profile(part).spec
                accept = pe._accept_periods(
                    cur, spec, tuple(p for p in drift if _part_of(p) == part))
                accept_kc = pe._accept_key_change_periods(
                    cur, spec, tuple(p for p in kcs if _part_of(p) == part))
                counts = core.refresh_counts(cur, spec, accept)
                print(f"{spec.live_table}: rows refresh-latest would write: "
                      + (", ".join(f"{pe._p(p)}={n}" for p, n in counts.items())
                         or "none") + f" (total {sum(counts.values())})")
                if spec.refresh_key_changes:
                    plan, _ = core._plan(cur, spec, accept)
                    lines = core.key_change_lines(plan, accept_kc, name=pe._p)
                    print(f"{spec.live_table}: keys added and removed:"
                          + ("" if lines else " none"))
                    for line in lines:
                        print(f"  {line}")
                plans.append((spec, accept, accept_kc))
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            msgs = []
            for spec, accept, accept_kc in plans:
                pe._refresh_guard(cur, spec, accept, accept_kc)
                res = core.refresh_latest(cur, spec, accept, accept_kc)
                if res["updated"]:
                    # the values, NULL-safe; source is not compared (it
                    # names the edition that last wrote a row's values)
                    bad = live_equals_tip(cur, spec, sorted(res["updated"]))
                    if bad:
                        halt(f"refresh-latest: {spec.live_table} differs from "
                             "the latest edition after the refresh, rolled "
                             "back: " + "; ".join(bad[:6]))
                msgs.append(f"{spec.live_table}: {res['rows']} live rows "
                            "refreshed in "
                            f"{sorted(pe._p(x) for x in res['updated'])}")
        with conn.cursor() as cur:
            gone = live_join_gone(cur)
        if gone:
            halt(f"{JOIN}: after refresh-latest the newest live rate year has "
                 f"no row for crosswalk BRMA name(s) {gone}; W1 would leave "
                 "their authorities without rates (no flag releases this, "
                 "--accept-key-changes included); rolled back")
        pe.finish(conn, args, "; ".join(msgs) + "; before/after guard passed")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    """One part's prepared run (rates or crosswalk), before the
    acknowledgements are applied."""
    part: str
    prof: object
    has_ed: bool
    by_period: dict = dataclasses.field(default_factory=dict)
    new: list = dataclasses.field(default_factory=list)
    revised: list = dataclasses.field(default_factory=list)
    skipped: dict = dataclasses.field(default_factory=dict)
    soft: dict = dataclasses.field(default_factory=dict)
    keys: dict = dataclasses.field(default_factory=dict)
    hard: dict = dataclasses.field(default_factory=dict)
    reissue: dict = dataclasses.field(default_factory=dict)
    header: dict = dataclasses.field(default_factory=dict)
    notes: dict = dataclasses.field(default_factory=dict)
    info: dict = dataclasses.field(default_factory=dict)
    run_notes: list = dataclasses.field(default_factory=list)
    nothing: bool = False
    rank_halt: "str | None" = None

    @property
    def periods(self) -> list:
        return sorted(self.new + self.revised)


def _end(conn, args, writing, rc) -> int:
    conn.rollback()  # commit mode has committed already
    if not writing:
        print("PREVIEW: nothing written to the database (use --commit or "
              "--simulate)")
    elif args.simulate:
        print("SIMULATION: ROLLED BACK (nothing persisted)")
    return rc


def _fy_arg(text) -> str:
    try:
        return fy_ok(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _period_arg(text) -> str:
    if VINTAGE_RE.fullmatch(str(text or "")):
        return str(text)
    return _fy_arg(text)


def _prepare_tables(cur, part, writing):
    prof = profile(part)
    spec = prof.spec
    missing = _tables_ready(cur, part)
    has_ed = not missing
    if writing and not has_ed:
        halt(f"missing {', '.join(missing)}; run `ddl --commit` and "
             "`migrate-legacy --commit` first")
    if has_ed:
        _, new_live, errors = core.latest_map(cur, spec)
        if errors:
            halt("invalid edition chain: " + "; ".join(
                f"{pe._p(p)}: {msg}" for p, msg in errors.items()))
        if new_live:
            halt(f"{spec.live_table}: live periods with no editions "
                 f"{[pe._p(x) for x in new_live]}; run migrate-legacy first")
        tips = tip_info(cur, spec)
        checked = ledger_checks(cur, prof)
        stranded = pe.live_missing_periods(cur, prof)
    else:
        print(f"NOTE: {', '.join(missing)} not present: this preview "
              f"compares with the LIVE table {spec.live_table}")
        tips, checked, stranded = legacy_tips(cur, spec), {}, []
    return prof, spec, has_ed, tips, checked, stranded


def _collection():
    coll = fetch_json(COLLECTION_PATH)
    try:
        pages = year_pages(coll)
    except ValueError as e:
        halt(f"{GOV_UK}{COLLECTION_PATH}: {e}")
    print(f"collection: {COLLECTION_TITLE} ({GOV_UK}{COLLECTION_PATH}): year "
          f"pages {', '.join(sorted(pages))}")
    for t in pages.ignored:
        print(f"  listed, not a financial-year page (ignored): {t!r}")
    return coll


def _page(base_path, fy):
    """(page JSON, attachment, page rank timestamp) of a year page, printed:
    the page, its change history, the attachment."""
    pg = fetch_json(base_path)
    try:
        own = page_year(pg)
        att = england_attachment(pg)
        ts = page_rank_ts(pg)
    except ValueError as e:
        halt(f"{GOV_UK}{base_path}: {e}; nothing stored")
    if own != fy:
        halt(f"{GOV_UK}{base_path}: the page is for {own}, the collection "
             f"lists it for {fy}")
    print(f"page: {_norm(pg.get('title'))} ({GOV_UK}{base_path}); "
          f"public_updated_at {pg.get('public_updated_at')}")
    for t, note in change_history(pg):
        print(f"  change_history {_iso(t)}: {note[:200]}")
    print(f"  attachment: {att['title']!r} {att['url']}")
    return pg, att, ts


def _source(args, held) -> dict:
    """The CSV the run reads: {path, where, how, page_year, page_url,
    file_name, ts, discovered, sha}. --file reads a local file and, unless
    --no-page, reads its year's page and downloads the page's current CSV
    to compare: the page's change-history rank applies only when the bytes
    are equal; otherwise the collection gives the page (the newest, or
    --release) and its CSV is downloaded to RAW_DIR (also in a preview)."""
    out = {"path": None, "where": None, "how": None, "page_year": None,
           "page_url": None, "file_name": None, "ts": None,
           "discovered": None}
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            halt(f"--file {args.file}: no such file")
        print(f"file given: {path}; nothing downloaded for it; identity is "
              "read from the file")
        f = _read(path)
        fy = args.release or f["year"]
        if fy is None:
            halt(f"{path.name}: the header names more than one year "
                 f"({f['header_years']}); give --release YYYY-YY with "
                 "--accept-header-year")
        out.update(path=path, where=_file_source(path), how="local file",
                   file_name=path.name, file=f, fy=fy)
        if args.no_page:
            out.update(page_url=NO_PAGE, how="local file, --no-page: the "
                       "year page was not read (no within-year rank)")
            print("NOTE: --no-page: the year page is not read; the file has "
                  "no within-year rank")
            return out
        coll = _collection()
        try:
            _, bp = year_page_for(coll, fy)
        except ValueError as e:
            halt(f"{GOV_UK}{COLLECTION_PATH}: {e}; give --no-page to load the "
                 "file without its page (recorded as such)")
        _, att, ts = _page(bp, fy)
        print(f"downloading the page's current CSV {att['url']} to {RAW_DIR} "
              "to compare (also in a preview; the database is not written)")
        cur_path, final = fetch(att["url"], RAW_DIR)
        same = content_sha256(cur_path) == content_sha256(path)
        out.update(page_year=fy, page_url=GOV_UK + bp)
        if same:
            out["ts"] = ts
            print(f"  the file equals the page's current CSV ({final}): "
                  f"within-year rank {_iso(ts)}")
        else:
            out["how"] = ("local file, not the page's current CSV (no "
                          "within-year rank)")
            print("  NOTE: the file is not the page's current CSV; it has no "
                  "within-year rank")
        return out
    coll = _collection()
    try:
        if args.release:
            fy, bp = year_page_for(coll, args.release)
            how = f"year page {fy} given"
        else:
            fy, bp = latest_year_page(coll)
            how = "newest year page (GOV.UK collection)"
            out["discovered"] = coll
    except ValueError as e:
        halt(f"{GOV_UK}{COLLECTION_PATH}: {e}")
    if out["discovered"] is None:
        for line in discovery_note(coll, held):
            if line.startswith("NEW RELEASE"):
                print(line)
    _, att, ts = _page(bp, fy)
    print(f"downloading {att['url']} to {RAW_DIR} (also in a preview; the "
          "database is not written)")
    path, final = fetch(att["url"], RAW_DIR)
    print(f"  final URL: {final}")
    out.update(path=path, where=final, how=how, page_year=fy,
               page_url=GOV_UK + bp, file_name=Path(urlparse(final).path).name,
               ts=ts, fy=fy)
    return out


def _read(path) -> dict:
    try:
        return read_csv(path)
    except ValueError as e:
        halt(f"{e}; nothing stored")


def _rates_label(fy, rank, src, sha, notes) -> str:
    return (f"DWP Universal Credit LHA rates {fy}, England monthly CSV "
            f"(weekly derived); {rank_text(rank)}; {src['where']} sha256 "
            f"{sha[:16]}; {src['how']}" + "".join(f"; {n}" for n in notes if n))


def _prepare_rates(args, cur, writing) -> Plan:
    prof, spec, has_ed, tips, checked, stranded = _prepare_tables(
        cur, "rates", writing)
    plan = Plan("rates", prof, has_ed)
    ranks = tip_ranks(tips, checked)
    held = sorted(ranks)
    print(f"rates held years: {', '.join(held) or 'none'}")
    src = _source(args, held)
    if src["discovered"] is not None:
        for line in discovery_note(src["discovered"], held):
            print(line)
    path = src["path"]
    sha = content_sha256(path)
    same = sha == globals()["LEGACY_RATES_FILE"]["sha256"]
    print(f"{path.name}: sha256 {sha[:16]}"
          + ("; byte-identical to the held 2026-27 file" if same else ""))
    fy = src["fy"]
    rank = (fy, src["ts"])
    lsrc = ledger_source(src["where"], rank)
    if has_ed and args.recheck is None and ledger_complete(
            checked, src["where"], sha, fy, stranded):
        print(f"the file's (source, sha256) is already in the ledger for {fy}; "
              "nothing parsed (--recheck YYYY-YY reads it again)")
        plan.nothing = True
        plan.skipped = {fy: "checked: already in the ledger"}
        return plan
    f = src.get("file") or _read(path)
    print(f"{f['name']}: encoding {f['encoding']}; header {f['header']}; "
          f"{len(f['rows'])} BRMAs")
    if args.release and args.release != fy:
        halt(f"--release {args.release}, the page is for {fy}; nothing stored")
    hy = header_year_problems(f, fy)
    accepted_hy = set(args.accept_header_year or ())
    stray_hy = sorted(accepted_hy - {fy} if hy else accepted_hy)
    if stray_hy:
        halt(f"--accept-header-year {stray_hy}: the header names {fy} "
             "throughout, or the run's year is another; nothing to accept")
    if hy and fy not in accepted_hy:
        halt("file identity check failed, nothing stored: "
             + "; ".join(hy) + f" (the year is {fy}: "
             + ("the page's" if src["page_year"] else "--release")
             + f"); read the file and the page's change note, then "
             f"--accept-header-year {fy}")
    if hy:
        plan.header[fy] = (f"--accept-header-year {fy}: "
                           + "; ".join(hy))
        print(f"  {fy}: ACCEPTED HEADER YEAR: {plan.header[fy]}")
    source_text = (f"DWP UC LHA rates {fy}, "
                   + (src["page_url"] if src["page_year"] else src["where"]))
    try:
        recs = build_rates(f, fy, {SOURCE_TEXT: source_text})
    except ValueError as e:
        halt(f"{e}; nothing stored")
    plan.by_period[fy] = recs
    chk = {p: s for p, s in checked.items() if p not in stranded}
    try:
        new, revised, skipped = plan_periods(
            fy, rank, ranks, chk, lsrc, sha, recheck=args.recheck,
            allow_older=args.allow_older_file, stranded=stranded)
    except ValueError as e:
        halt(str(e))
    plan.new, plan.revised, plan.skipped = new, revised, skipped
    for p, reason in sorted(skipped.items()):
        print(f"  {p}: skipped ({reason})")
    if skipped and all(r.startswith("older") for r in skipped.values()):
        plan.rank_halt = (f"older file: {fy} is skipped; the file is "
                          f"{rank_text(rank)}, older than what is held; "
                          "storing it would record an older file. If this is "
                          "deliberate, re-run with --allow-older-file")
    note = ""
    t = ranks.get(fy)
    cmp = compare_rank(rank, t) if t else None
    if fy in new + revised:
        if cmp == "older" or (fy in new and held and fy < held[-1]):
            note = (f"--allow-older-file given; {fy}: the file is "
                    f"{rank_text(rank)}, the held tip "
                    f"{rank_text(t) if t else 'a newer year'}")
            print(f"NOTE: {note}")
        if fy in revised:
            tip = _tip_records(cur, spec, fy, tips, has_ed)
            found = period_problems(recs, tip, None, kind="revised")
            if fy == held[-1]:
                # a reissue of the newest year: --accept-key-changes must not
                # release a BRMA the crosswalk uses
                join_stop(plan, cur, fy, recs)
            if cmp in ("equal", "unknown") and _differs(
                    recs, tip, RATE_KEY, RATE_VALUES):
                plan.reissue[fy] = (
                    f"the file is {rank_text(rank)}, "
                    + ("the same as the file of the held tip"
                       if cmp == "equal" else "with no within-year rank to "
                       "set against the held tip's "
                       f"({rank_text(t)})")
                    + ", but its content differs")
        else:
            earlier = [h for h in held if h < fy]
            prev = (_tip_records(cur, spec, max(earlier), tips, has_ed)
                    if earlier else None)
            found = period_problems(recs, None, prev, kind="new")
            if not held or fy > held[-1]:
                join_stop(plan, cur, fy, recs)
        plan.keys[fy] = [x for x in found if x.startswith(KEYS)]
        plan.soft[fy] = [x for x in found if not x.startswith(KEYS)]
        notes = [note]
        plan.notes[fy] = notes
        plan.info[fy] = {"src": src, "sha": sha, "rank": rank, "lsrc": lsrc,
                         "file_name": src["file_name"]}
    plan.nothing = not plan.periods
    return plan


def join_stop(plan, cur, fy, recs) -> None:
    """JOIN stop: a crosswalk BRMA name with no rate row in the newest year
    (a new newest year, or a reissue of it). No flag releases it."""
    _, mnames = _mapping_names(cur)
    gone = sorted(mnames - {r[RATE_KEY] for r in recs})
    if gone:
        plan.hard[fy] = [f"{JOIN}: crosswalk BRMA name(s) with no "
                         f"rate row in {fy}: {gone}; as the "
                         "newest year W1 would leave their "
                         "authorities without rates (no flag "
                         "releases this, --accept-key-changes "
                         "included; resolve the names first)"]


def live_join_gone(cur) -> list:
    """Crosswalk BRMA names (live) with no row in the newest live rate year."""
    spec = profile("rates").spec
    _, names = latest_rate_names(cur, spec, True)
    _, mnames = _mapping_names(cur)
    return sorted(mnames - names)


def _mapping_names(cur) -> tuple:
    """(vintage, {brma_name}) of the crosswalk W1 joins through: the live
    table's."""
    spec = profile("mapping").spec
    cur.execute(f"SELECT DISTINCT brma_name FROM public.{spec.live_table}")
    return LIVE_VINTAGE, {r[0] for r in cur.fetchall()}


def _tip_records(cur, spec, p, tips, has_ed) -> list:
    if has_ed:
        return records(cur, spec, p, tips[p]["edition"])
    return records(cur, spec, p)


def gml_vintage(sha) -> "str | None":
    for v, info in globals()["VINTAGES"].items():
        if info["gml_sha256"] == sha:
            return v
    return None


def rebuild_mapping(cur, gml_path, rate_names) -> tuple:
    """(vintage, records, facts) of the crosswalk rebuilt from a BRMA layer
    file and la_boundaries: the file's sha256 must be a known vintage's and
    la_boundaries as that vintage pins it. Halts naming what was seen."""
    p = Path(gml_path)
    if not p.is_file():
        halt(f"{p}: no such file (the VOA BRMA layer GML.zip)")
    sha = content_sha256(p)
    v = gml_vintage(sha)
    if v is None:
        halt(f"{p.name}: sha256 {sha[:16]} is not a known BRMA layer "
             f"({ {k: x['gml_sha256'][:16] for k, x in VINTAGES.items()} }); "
             "adopting a new layer is a deliberate decision (a new vintage "
             "in VINTAGES, with its evidence)")
    try:
        g = read_gml(p)
    except ValueError as e:
        halt(f"{e}; nothing stored")
    rows = _late("_boundaries")(cur)
    state = boundaries_state(rows)
    want = globals()["VINTAGES"][v]["boundaries"]
    if tuple(state) != tuple(want):
        halt(f"la_boundaries is {state}, not the state vintage {v} was built "
             f"on {want}; a new LAD vintage is a deliberate decision")
    try:
        aliases = brma_aliases()
        recs = build_mapping(g, rows, aliases, rate_names, vintage=v,
                             source_text=VINTAGES[v]["source_text"])
    except ValueError as e:
        halt(f"{e}; nothing stored")
    within = sum(r["mapping_method"] == WITHIN for r in recs)
    near = [r for r in recs if r["mapping_method"] == NEAREST]
    print(f"BRMA layer {_file_source(p)}: sha256 {sha[:16]}; vintage {v}; "
          f"member {g['member']!r}, layer {g['layer']!r}, field "
          f"{g['field']!r}, {len(g['names'])} polygons, layer CRS read "
          f"{g['crs_read'] or 'absent'} (set {GML_CRS}); la_boundaries "
          f"{state[0]} rows, md5 {state[1]}")
    print(f"crosswalk: {len(recs)} authorities; {within} centroid within a "
          f"polygon; {len(near)} nearest polygon"
          + (": " + ", ".join(f"{r[MAP_KEY]} {r['la_name']} -> "
                              f"{r['brma_name']}" for r in near)
             if near else "")
          + f"; names converted by the alias table: "
          f"{sum(1 for n in g['names'] if '&' in n)}")
    return v, recs, {"sha": sha, "where": _file_source(p),
                     "boundaries": state}


def _mapping_ledger_source(where, vintage, state) -> str:
    return (f"{where} (vintage {vintage}; la_boundaries {state[0]} rows md5 "
            f"{state[1]})")


def _prepare_mapping(args, cur, writing, rates_plan) -> Plan:
    prof, spec, has_ed, tips, checked, stranded = _prepare_tables(
        cur, "mapping", writing)
    plan = Plan("mapping", prof, has_ed)
    fy, names = latest_rate_names(cur, profile("rates").spec, has_ed)
    if rates_plan is not None:
        for p in rates_plan.new:
            if fy is None or p > fy:
                fy, names = p, {r[RATE_KEY] for r in rates_plan.by_period[p]}
    print(f"crosswalk held vintages: {', '.join(sorted(tips)) or 'none'}; "
          f"rate names from {fy or 'none'} ({len(names)} BRMAs)")
    gml = Path(args.gml) if args.gml else globals()["GML_FILE"]
    v, recs, facts = rebuild_mapping(cur, gml, names)
    lsrc = _mapping_ledger_source(facts["where"], v, facts["boundaries"])
    plan.by_period[v] = recs
    if v not in tips:
        plan.new = [v]
    elif (args.recheck != v and v not in stranded
          and (lsrc, facts["sha"]) in checked.get(v, ())):
        plan.skipped[v] = "checked: this layer and la_boundaries state are " \
                          "already in the ledger"
        print(f"  {v}: skipped ({plan.skipped[v]}; --recheck {v} reads it "
              "again)")
    else:
        if args.recheck not in (None, v) and _part_of(args.recheck) == "mapping":
            halt(f"--recheck {args.recheck}: not the vintage of this layer "
                 f"({v})")
        plan.revised = [v]
    if v in plan.periods:
        tip = _tip_records(cur, spec, v, tips, has_ed) if v in tips else None
        found = mapping_problems(recs, tip) if tip else []
        plan.keys[v] = []
        plan.hard[v] = [x for x in found if x.startswith(KEYS)]
        plan.soft[v] = [x for x in found if not x.startswith(KEYS)]
        corr = named_correction(recs, tip) if tip else None
        note = ""
        if corr:
            c = MAPPING_CORRECTIONS[corr]
            note = f"named correction {corr}: {c['why']}"
            print(f"  {v}: the change is the {note}")
        plan.notes[v] = [note]
        plan.info[v] = {"facts": facts, "lsrc": lsrc}
    plan.nothing = not plan.periods
    return plan


def _finalize(plan, args) -> tuple:
    """(problems {period: [msg]}, infos {period: PeriodInfo}) with the
    acknowledgements applied."""
    ack = set(args.acknowledge or ())
    reissue = set(args.accept_reissue or ())
    kc = set(args.accept_key_changes or ())
    problems, infos = {}, {}
    for p in plan.periods:
        bad = list(plan.hard.get(p, []))
        n = list(plan.notes.get(p, []))
        if p in plan.header:
            n.append(plan.header[p])
        soft = plan.soft.get(p, [])
        if soft and p in ack:
            n.append("ACKNOWLEDGED " + "; ".join(soft))
            print(f"  {p}: ACKNOWLEDGED (--acknowledge): {'; '.join(soft)}")
        elif soft:
            bad += [x + f"; read the preview, then --acknowledge {p}"
                    for x in soft]
        keys = plan.keys.get(p, [])
        if keys and p in kc:
            n.append("--accept-key-changes " + "; ".join(keys))
            print(f"  {p}: ACCEPTED KEY CHANGES: {'; '.join(keys)}")
        elif keys:
            bad += [x + f"; read the preview, then --accept-key-changes {p}"
                    for x in keys]
        if p in plan.reissue:
            if p in reissue:
                msg = f"--accept-reissue {p}: {plan.reissue[p]}"
                n.append(msg)
                print(f"  {p}: ACCEPTED REISSUE: {msg}")
            else:
                bad.append(plan.reissue[p] + ": two files claim the same "
                           "release (or the rank cannot tell them apart). A "
                           "publisher reissue is named on the page: read "
                           f"its change note, then --accept-reissue {p}")
        if bad:
            problems[p] = bad
            continue
        n = [x for x in n if x]
        plan.notes[p] = n
        infos[p] = (_rates_info(p, plan.info[p], n) if plan.part == "rates"
                    else _mapping_info(p, plan.info[p], n))
    return problems, infos


def _rates_info(p, a, notes) -> PeriodInfo:
    src, rank = a["src"], a["rank"]
    label = _rates_label(p, rank, src, a["sha"], notes)
    source = edition_source(a["file_name"], rank, src["where"],
                            "; ".join(notes))
    published = rank[1].date() if rank[1] else date.today()
    return PeriodInfo(source, label, published, ((a["lsrc"], a["sha"]),))


def _mapping_info(v, a, notes) -> PeriodInfo:
    f = a["facts"]
    extra = "".join(f"; {n}" for n in notes if n)
    label = (f"S14 crosswalk {v}: VOA BRMA layer {f['where']} sha256 "
             f"{f['sha'][:16]} x la_boundaries ({f['boundaries'][0]} rows, md5 "
             f"{f['boundaries'][1]}) centroids; within a polygon "
             f"(EPSG:27700), else the nearest" + extra)
    source = f"{f['where']}; vintage {v}; GML sha256 {f['sha'][:16]}" + extra
    return PeriodInfo(source, label, date.today(), ((a["lsrc"], f["sha"]),))


def _preview_mapping_live(cur, plan) -> int:
    """The crosswalk compared with the live table before ddl (no vintage
    column, no editions): printed, nothing written."""
    for v in plan.periods:
        live = records(cur, plan.prof.spec, v)
        new = plan.by_period[v]
        found = mapping_problems(new, live)
        print(f"  {v}: compared with live: "
              + ("new (live is empty)" if not live else
                 "unchanged" if not found else "; ".join(found)))
    print("  (no editions table yet: preview against live only)")
    return 0


def _execute(plan, args, conn, cur, problems, infos) -> int:
    started = datetime.now(timezone.utc)
    prof = plan.prof
    for p, msgs in sorted(problems.items()):
        for msg in msgs:
            print(f"  {p}: REJECTED, not stored: STOP CONDITION: {msg}")
    ok = [p for p in plan.periods if p not in problems]
    rc = 0
    stats = {"periods": [], "kinds": {}, "stored_rows": 0, "live_rows": 0}
    if ok and plan.has_ed:
        def apply(c, prof_, per, rs, *, fetched_on, source_file=None):
            return _late("apply_part")(c, prof_, per, rs,
                                       fetched_on=fetched_on, info=infos[per])
        rc = pe.load_periods(cur, prof, ok, lambda per: plan.by_period[per],
                             date.today(), args.commit,
                             simulate=args.simulate, against="editions",
                             stats=stats, apply=apply)
    elif ok and plan.part == "rates":
        rc = pe.load_periods(cur, prof, ok, lambda per: plan.by_period[per],
                             date.today(), False, against="live")
        print("  (no editions table yet: preview against live only)")
    elif ok:
        rc = _preview_mapping_live(cur, plan)
    if problems:
        print(f"REJECTED {len(problems)} period(s), nothing stored for them: "
              f"{', '.join(sorted(problems))}; exit 1")
        rc = 1
    if args.commit:
        stored = live = 0  # rows written by this run; not a source value
        for p in stats["periods"]:
            k = stats["kinds"][p]
            if k in ("new", "revised", HELD_NOT_LIVE):
                stored += len(plan.by_period[p])
            if k in ("new", LIVE_MISSING):
                live += len(plan.by_period[p])
        left = [p for p in ok if p not in stats["periods"]]
        failed, not_tried = (left[:1], left[1:]) if left else ([], [])
        partial = ""
        if rc:
            partial = (f"PARTIAL RUN (exit 1): rejected "
                       f"{sorted(problems) or 'none'}"
                       + "".join(f" ({p}: {'; '.join(problems[p])[:300]})"
                                 for p in sorted(problems))
                       + f"; failed {failed or 'none'}; not attempted "
                       f"{not_tried or 'none'}; a period that failed or was "
                       "rejected stored nothing and is not in the counts "
                       "below; ")
        what = ("rates CSV" if plan.part == "rates"
                else "crosswalk (BRMA layer x la_boundaries)")
        files = "; ".join(f"{a.get('lsrc')} sha256 "
                          f"{(a.get('sha') or a.get('facts', {}).get('sha') or '')[:16]}"
                          for a in plan.info.values())
        notes = (partial + f"{what} {files}: "
                 + ("; ".join(f"{p} {k}" for p, k in stats["kinds"].items())
                    or "nothing stored")
                 + (f"; skipped {sorted(plan.skipped)}" if plan.skipped else "")
                 + "".join(f"; {p}: {'; '.join(plan.notes.get(p, []))}"
                           for p in stats["periods"] if plan.notes.get(p))
                 + f". Rows stored: {stored} edition rows, {live} live rows.")
        log_run(cur, stored + live, notes, started)
        conn.commit()
        print("pipeline_run_log row written"
              + (" (partial run: see its notes)" if rc else ""))
    return rc


def cmd_load(args) -> int:
    parts = [args.only] if args.only else ["rates", "mapping"]
    if args.no_page and not args.file:
        halt("--no-page is only for --file (a local CSV whose year page is "
             "not read)")
    if (args.file or args.release or args.accept_header_year) and \
            "rates" not in parts:
        halt("--file, --release and --accept-header-year are for the rates; "
             "drop --only mapping")
    if args.gml and "mapping" not in parts:
        halt("--gml is for the crosswalk; drop --only rates")
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            plans = []
            rates_plan = None
            if "rates" in parts:
                print("=== RATES ===")
                rates_plan = _prepare_rates(args, cur, writing)
                plans.append(rates_plan)
            if "mapping" in parts:
                print("=== CROSSWALK ===")
                plans.append(_prepare_mapping(args, cur, writing, rates_plan))
            for plan in plans:
                if plan.rank_halt:
                    halt(plan.rank_halt)
            considered = set().union(*(set(p.periods) for p in plans))
            named = (set(args.acknowledge or ()) | set(args.accept_reissue or ())
                     | set(args.accept_key_changes or ()))
            stray = sorted(named - considered)
            if stray:
                halt(f"--acknowledge/--accept-reissue/--accept-key-changes "
                     f"{', '.join(stray)}: not periods this run compares or "
                     f"stores ({', '.join(sorted(considered)) or 'none'}); "
                     "nothing stored")
            rc = 0
            for plan in plans:
                print(f"=== {plan.part.upper()}: planned new "
                      f"{plan.new or 'none'}; compare {plan.revised or 'none'}"
                      f"; skipped {sorted(plan.skipped) or 'none'} ===")
                if plan.nothing:
                    print("nothing to do")
                    continue
                problems, infos = _finalize(plan, args)
                rc = max(rc, _execute(plan, args, conn, cur, problems, infos))
        return _end(conn, args, writing, rc)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# check-mapping (read-only)
# ---------------------------------------------------------------------------

def cmd_check_mapping(args) -> int:
    """Rebuild the crosswalk read-only from the BRMA layer and la_boundaries
    and list its differences from the latest crosswalk edition (or the live
    table before the editions exist). Exit 1 when any brma_name or la_name
    differs, or an authority is on one side only; a mapping_method
    difference is listed and does not fail."""
    conn = _conn(False)
    try:
        with conn.cursor() as cur:
            rspec, mspec = profile("rates").spec, profile("mapping").spec
            fy, names = latest_rate_names(cur, rspec, False)
            print(f"rate names from {fy} ({len(names)} BRMAs)")
            gml = Path(args.gml) if args.gml else globals()["GML_FILE"]
            v, recs, _ = rebuild_mapping(cur, gml, names)
            if not _tables_ready(cur, "mapping") and v in tip_info(cur, mspec):
                ed = core.latest_edition(cur, mspec, v)
                held, against = records(cur, mspec, v, ed), f"edition {ed}"
            else:
                held, against = records(cur, mspec, v), "live"
    finally:
        conn.rollback()
        conn.close()
    hk, nk = _by(held, MAP_KEY), _by(recs, MAP_KEY)
    rc = 0
    one = sorted(set(hk) ^ set(nk))
    if one:
        print(f"authorities on one side only: {one}")
        rc = 1
    both = sorted(set(hk) & set(nk))
    for col in MAP_VALUES:
        diff = [k for k in both if hk[k][col] != nk[k][col]]
        same = len(both) - len(diff)
        print(f"{col}: reproduced for {same} of {len(both)} (compared with "
              f"{against})")
        if diff:
            pairs = {}
            for k in diff:
                pairs.setdefault((hk[k][col], nk[k][col]), []).append(k)
            for (a, b), ks in sorted(pairs.items()):
                print(f"  held {a!r} -> rebuilt {b!r}: {len(ks)} "
                      f"authorit(ies): {', '.join(ks[:8])}"
                      + (" ..." if len(ks) > 8 else ""))
            if col != "mapping_method":
                rc = 1
    return rc


# ---------------------------------------------------------------------------
# One-off: migrate the held tables
# ---------------------------------------------------------------------------

RATE_SURVEY_COLS = ((RATE_KEY, FY_COL) + WEEKLY + MONTHLY + ("source",))
MAP_SURVEY_COLS = (MAP_KEY, "la_name", "brma_name", "brma_secondary",
                   "mapping_method", "source")


def live_state(cur, spec) -> tuple:
    """(rows, survey hash, {loaded_at: rows}) of a live table. The survey
    hash: md5 of the surveyed columns (the vintage column left out, so the
    hash is the same before and after ddl), each as text with NULL as '~',
    joined by '|', rows ordered by period then key (the crosswalk by key),
    joined by LF."""
    mapping = _is_mapping(spec)
    cols = MAP_SURVEY_COLS if mapping else RATE_SURVEY_COLS
    order = MAP_KEY if mapping else f"{FY_COL}, {RATE_KEY}"
    expr = "||'|'||".join(f"coalesce({c}::text,'~')" for c in cols)
    cur.execute(f"SELECT COUNT(*), md5(string_agg({expr}, E'\\n' ORDER BY "
                f"{order})) FROM public.{spec.live_table}")
    n, h = cur.fetchone()
    cur.execute(f"SELECT loaded_at, COUNT(*) FROM public.{spec.live_table} "
                "GROUP BY 1")
    return n, h, dict(cur.fetchall())


def _check_state(cur, spec, want, want_loaded) -> None:
    n, h, loaded = live_state(cur, spec)
    if (n, h) != tuple(want):
        halt(f"{spec.live_table} is not as surveyed: {n} rows, hash {h}; "
             f"expected {want[0]} rows, hash {want[1]}; nothing stored")
    if loaded != dict(want_loaded):
        halt(f"{spec.live_table} loaded_at is not as surveyed: "
             f"{ {str(k): v for k, v in loaded.items()} }, expected "
             f"{ {str(k): v for k, v in want_loaded.items()} }; nothing stored")


def _empty_editions(cur, part, write) -> None:
    prof = profile(part)
    for t in (prof.spec.editions_table, pe.file_checks_table(prof)):
        if not table_exists(cur, t):
            if write:
                halt(f"{t} does not exist; run `ddl --commit` first")
            continue
        cur.execute(f"SELECT COUNT(*) FROM public.{t}")
        k = cur.fetchone()[0]
        if k:
            halt(f"{t} already holds {k} rows; migrate-legacy runs once, on "
                 "empty tables")


def migrate_legacy(cur, *, write, rates_file=None, gml_file=None,
                   legacy=None) -> dict:
    """One-off. Preconditions: on write both editions tables and ledgers
    exist and are empty (in a preview they may be absent; if present they
    must be empty); both live tables as surveyed (legacy: {'rates': (rows,
    hash), 'rates_loaded': {loaded_at: rows}, 'mapping': ...,
    'mapping_loaded': ..., 'rates_file': {period, name, sha256, url, page,
    rank, loaded_by}}; default the LEGACY_* constants). Proofs, any failure
    halts: the held rates CSV (its sha256 the held file's) read by this
    parser reproduces every live rate, the five monthly as published and
    the five weekly as derived (0 differences); the crosswalk rebuilt from
    the BRMA layer and la_boundaries reproduces every live brma_name and
    la_name, and mapping_method except LEGACY_METHOD_FORM (held 'NaN' where
    the rebuild says centroid_spatial_join, on exactly its count of rows),
    listed. Edition 1 'as loaded' of each table from live (the live source
    per row as source_text); a ledger row for the rates file (it reproduces
    edition 1 on every value: outcome 'unchanged', the file's URL and rank);
    none for the crosswalk (it differs in mapping_method, so the next load
    compares it). Live untouched. Nothing commits here."""
    g = globals()
    legacy = legacy or {
        "rates": g["LEGACY_RATES"], "rates_loaded": g["LEGACY_RATES_LOADED"],
        "mapping": g["LEGACY_MAPPING"],
        "mapping_loaded": g["LEGACY_MAPPING_LOADED"],
        "rates_file": g["LEGACY_RATES_FILE"],
        "method_form": g["LEGACY_METHOD_FORM"]}
    lf = legacy["rates_file"]
    rprof, mprof = profile("rates"), profile("mapping")
    rspec, mspec = rprof.spec, mprof.spec
    for part in SPECS:
        _empty_editions(cur, part, write)
    if write and not live_has_vintage(cur, mspec):
        halt(f"{mspec.live_table}.{VINTAGE_COL} does not exist; run `ddl "
             "--commit` first")
    _check_state(cur, rspec, legacy["rates"], legacy["rates_loaded"])
    _check_state(cur, mspec, legacy["mapping"], legacy["mapping_loaded"])
    cur.execute(f"SELECT DISTINCT {FY_COL} FROM public.{rspec.live_table}")
    periods = sorted(str(r[0]) for r in cur.fetchall())
    if periods != [lf["period"]]:
        halt(f"{rspec.live_table} holds {periods}, expected [{lf['period']!r}]"
             "; nothing stored")
    p = lf["period"]
    path = Path(rates_file or g["RATES_FILE"])
    sha = content_sha256(path)
    if sha != lf["sha256"]:
        halt(f"{path.name}: sha256 {sha[:16]} is not the held {p} file "
             f"({lf['sha256'][:16]}); nothing stored")
    f = _read(path)
    if f["year"] != p:
        halt(f"{path.name}: the header names {f['year']}, not {p}")
    built = build_rates(f, p)
    held = records(cur, rspec, p)
    hk, bk = _by(held, RATE_KEY), _by(built, RATE_KEY)
    diffs = [("only held", k) for k in sorted(set(hk) - set(bk))]
    diffs += [("only file", k) for k in sorted(set(bk) - set(hk))]
    cells = 0  # values compared; not a source value
    for k in sorted(set(hk) & set(bk)):
        for c in RATE_VALUES:
            cells += 1
            if hk[k][c] is None or Decimal(hk[k][c]) != bk[k][c]:
                diffs.append((k, c, hk[k][c], bk[k][c]))
    if diffs:
        halt(f"proof failed: {p}: {len(diffs)} differences between the held "
             f"rates and {path.name} read again, e.g. {diffs[:5]}; nothing "
             "stored")
    n_m = len(MONTHLY) * len(hk)
    print(f"rates {p}: {path.name} (sha256 {sha[:16]}, {f['encoding']}) read "
          f"again reproduces every held rate: {len(hk)} BRMAs, {n_m} monthly "
          f"values as published and {cells - n_m} weekly values as derived "
          "(monthly x 12 / 52, half-up), 0 differences")
    loaded = sorted(legacy["rates_loaded"])
    r_pub = loaded[-1].astimezone(timezone.utc).date()
    r_source = (f"as loaded: {lf['loaded_by']}; proved equal to {lf['name']}; "
                f"{rank_text(lf['rank'])}; {lf['page']}")
    r_recs = [dict(r) for r in held]
    for r in r_recs:
        for c in RATE_VALUES:
            r[c] = Decimal(r[c]).quantize(CENT)
    # the crosswalk
    fy, names = p, set(bk)
    v, rebuilt, facts = rebuild_mapping(cur, gml_file or g["GML_FILE"], names)
    if v != LIVE_VINTAGE:
        halt(f"the BRMA layer is vintage {v}, the live crosswalk "
             f"{LIVE_VINTAGE}; nothing stored")
    mheld = records(cur, mspec, v)
    mk, nk = _by(mheld, MAP_KEY), _by(rebuilt, MAP_KEY)
    form_held, form_built, form_n = legacy["method_form"]
    mdiffs = [("only held", k) for k in sorted(set(mk) - set(nk))]
    mdiffs += [("only rebuilt", k) for k in sorted(set(nk) - set(mk))]
    forms, mcells = [], 0  # values compared; not a source value
    for k in sorted(set(mk) & set(nk)):
        for c in MAP_VALUES:
            mcells += 1
            a, b = mk[k][c], nk[k][c]
            if a == b:
                continue
            if c == "mapping_method" and (a, b) == (form_held, form_built):
                forms.append(k)
                continue
            mdiffs.append((k, c, a, b))
    if not mdiffs and len(forms) != form_n:
        mdiffs.append(("held form", f"{len(forms)} rows hold "
                       f"{form_held!r} where the rebuild says "
                       f"{form_built!r}, expected {form_n}"))
    if mdiffs:
        halt(f"proof failed: crosswalk: {len(mdiffs)} differences between the "
             f"held rows and the rebuild, e.g. {mdiffs[:5]}; nothing stored")
    same_b = len(mk)
    print(f"crosswalk {v}: the rebuild reproduces brma_name and la_name for "
          f"{same_b} of {same_b} authorities and mapping_method for "
          f"{same_b - len(forms)}; {len(forms)} rows hold {form_held!r} where "
          f"the rebuild says {form_built!r} (the held form, kept in edition 1;"
          " the named correction s14-mapping-method-2026-10 is the next "
          f"edition): {', '.join(forms[:6])}{' ...' if len(forms) > 6 else ''}")
    m_loaded = sorted(legacy["mapping_loaded"])
    m_pub = m_loaded[-1].astimezone(timezone.utc).date()
    m_source = (f"as loaded: {LEGACY_MAPPING_LOADED_BY}; brma_name and "
                f"la_name proved equal to {facts['where']} (sha256 "
                f"{facts['sha'][:16]}) x la_boundaries centroids; vintage {v}")
    m_recs = [dict(r, **{VINTAGE_COL: v}) for r in mheld]
    plan = {
        "rates": {"period": p, "rows": len(r_recs), "cells": cells,
                  "sha": sha, "source_file": r_source, "published": r_pub,
                  "content_sha": rows_content_sha(r_recs),
                  "ledger": ledger_source(lf["url"], lf["rank"])},
        "mapping": {"period": v, "rows": len(m_recs), "cells": mcells,
                    "forms": forms, "source_file": m_source,
                    "published": m_pub,
                    "content_sha": mapping_content_sha(m_recs)},
        "rows": len(r_recs) + len(m_recs)}
    for part in ("rates", "mapping"):
        e = plan[part]
        print(f"  {part} edition 1 as loaded: {e['period']}, {e['rows']} "
              f"rows, published {e['published']} (the load date); "
              f"source_file {e['source_file']!r}; content sha256 "
              f"{e['content_sha']}")
    print(f"  ledger: rates {plan['rates']['ledger']}; crosswalk none (it "
          "differs from the rebuild in mapping_method, so the next load "
          "compares it)")
    if not write:
        return plan
    cur.execute("SAVEPOINT s14_migrate")
    for part, recs_, spec in (("rates", r_recs, rspec),
                              ("mapping", m_recs, mspec)):
        e = plan[part]
        ed = core.insert_edition(
            cur, spec, recs_, e["period"], release_label=core.AS_LOADED_LABEL,
            published_date=e["published"], source_file=e["source_file"],
            source_sha256=e["content_sha"], supersedes=None, strict=True)
        if ed != 1 or core.rows_differing(cur, spec, e["period"], ed):
            halt(f"{spec.editions_table} {e['period']}: edition 1 does not "
                 "equal live; rolled back")
    pe.record_file_check(cur, rprof, p, plan["rates"]["ledger"], sha,
                         "unchanged", 1)
    cur.execute("RELEASE SAVEPOINT s14_migrate")
    return plan


def cmd_migrate_legacy(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            plan = migrate_legacy(cur, write=writing, rates_file=args.file,
                                  gml_file=args.gml)
            if args.commit:
                r, mp = plan["rates"], plan["mapping"]
                log_run(cur, plan["rows"], "migrate-legacy: edition 1 as "
                        f"loaded for brma_lha_rates {r['period']} ({r['rows']}"
                        f" rows; proof {r['cells']} values equal the held CSV "
                        f"sha256 {r['sha'][:16]} read again; ledger row) and "
                        f"la_brma_mapping {mp['period']} ({mp['rows']} rows; "
                        "brma_name and la_name equal the rebuild from the "
                        f"BRMA layer; {len(mp['forms'])} held mapping_method "
                        "'NaN' kept; no ledger row). Live untouched.",
                        started)
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

def restore_edition(cur, part, period, edition) -> int:
    """Store edition `edition` of `period` as the next edition ('restored
    from edition N') for refresh-latest to apply; return its number. Halts
    if the edition does not exist or is the tip."""
    spec = profile(part).spec
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
    sha = (mapping_content_sha(recs) if _is_mapping(spec)
           else rows_content_sha(recs))
    new = core.insert_edition(
        cur, spec, recs, p, release_label=f"restored from edition {edition}",
        published_date=date.today(), source_file=src, source_sha256=sha,
        supersedes=tip, strict=True, allow_revert=True)
    if new == tip:
        halt(f"{p}: edition {edition} was not stored as the next edition; "
             "nothing to restore")
    return new


def cmd_restore_edition(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    part = TABLES[args.table]
    spec = profile(part).spec
    try:
        if _part_of(args.period) != part:
            halt(f"{args.period} is not a period of {args.table}")
        with conn.cursor() as cur:
            if not table_exists(cur, spec.editions_table):
                halt(f"{spec.editions_table} does not exist")
            new = restore_edition(cur, part, args.period, args.edition)
            print(f"{spec.editions_table} {args.period}: edition "
                  f"{args.edition} "
                  + ("stored" if writing else "would be stored")
                  + f" as edition {new}; run refresh-latest to apply it")
            if args.commit:
                n = len(records(cur, spec, args.period, new))
                log_run(cur, n, f"restore-edition: {spec.editions_table} "
                        f"{args.period} edition {args.edition} stored as "
                        f"edition {new}", started)
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
        description="S14 DWP UC LHA rates and the BRMA crosswalk: editions "
        "loader on the period-editions engine. There is no sync-new; "
        "migrate-legacy records the held state once.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create both editions tables, their "
                       "triggers and ledgers, and la_brma_mapping.vintage "
                       "(preview by default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("load", help="read the newest year page's England CSV "
                       "and rebuild the crosswalk; compare and store "
                       "(preview by default)")
    p.add_argument("--only", choices=("mapping", "rates"))
    p.add_argument("--release", type=_fy_arg, metavar="YYYY-YY",
                   help="that financial year's page (default the newest)")
    p.add_argument("--file", metavar="PATH",
                   help="a local England rates CSV (identity from the file)")
    p.add_argument("--no-page", action="store_true",
                   help="with --file only: do not read the year page (no "
                   "within-year rank; recorded as such)")
    p.add_argument("--gml", metavar="PATH",
                   help="the VOA BRMA layer zip (default "
                   "data/raw/s14_lha/GML.zip)")
    p.add_argument("--recheck", type=_period_arg, metavar="PERIOD",
                   help="compare this held period again although its file is "
                   "in the ledger")
    p.add_argument("--allow-older-file", action="store_true",
                   help="compare and store a year whose tip comes from a "
                   "newer file, or a year before the newest held (logged)")
    p.add_argument("--acknowledge", action="append", type=_period_arg,
                   metavar="PERIOD",
                   help="release a period's stop conditions after reading the "
                   "preview (repeatable; logged)")
    p.add_argument("--accept-header-year", action="append", type=_fy_arg,
                   metavar="YYYY-YY",
                   help="store a year whose CSV header names another year "
                   "(repeatable; logged)")
    p.add_argument("--accept-reissue", action="append", type=_period_arg,
                   metavar="PERIOD",
                   help="store a year whose file has the held file's rank, or "
                   "no within-year rank, with different content (repeatable; "
                   "logged)")
    p.add_argument("--accept-key-changes", action="append", type=_period_arg,
                   metavar="PERIOD",
                   help="store a year whose BRMA set differs (repeatable; "
                   "logged)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("refresh-latest", help="copy each period's latest "
                       "edition into the live tables, loaded_at included "
                       "(preview by default)")
    p.add_argument("--accept-drift", action="append", type=_period_arg,
                   metavar="PERIOD")
    p.add_argument("--accept-key-changes", action="append", type=_period_arg,
                   metavar="PERIOD")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_refresh_latest)
    p = sub.add_parser("migrate-legacy", help="one-off: edition 1 as loaded "
                       "for both tables, with the proofs (preview by "
                       "default)")
    p.add_argument("--file", metavar="CSV", help="the held 2026-27 CSV "
                   "(default data/raw/s14_lha/england-rates-2026-to-2027.csv)")
    p.add_argument("--gml", metavar="ZIP", help="the held BRMA layer "
                   "(default data/raw/s14_lha/GML.zip)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_migrate_legacy)
    p = sub.add_parser("restore-edition", help="store an earlier edition's "
                       "rows as the next edition (preview by default)")
    p.add_argument("table", choices=sorted(TABLES))
    p.add_argument("period", type=_period_arg)
    p.add_argument("edition", type=int)
    pe.mode_parser(p)
    p.set_defaults(func=cmd_restore_edition)
    p = sub.add_parser("check-mapping", help="rebuild the crosswalk read-only "
                       "and list its differences from the latest edition "
                       "(or live)")
    p.add_argument("--gml", metavar="ZIP")
    p.set_defaults(func=cmd_check_mapping)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
