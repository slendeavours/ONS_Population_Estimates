"""S4 (DfE Children looked after in England, care leaver accommodation):
edition history and loader on the period-editions engine.

care_leaver_accommodation holds one row per upper-tier authority, cohort and
reporting year: the latest layer. Two cohorts share it: 17-21 (accommodation
type, from the "accommodation by local authority" file) and 22-25
(suitability, from the "whether their accommodation is suitable by local
authority" file). DfE publishes one release a year (November) that
republishes the last five years of 17-21 and every year since 2023 of 22-25,
with revisions. This module owns care_leaver_accommodation_editions, which
keeps what each release says about each reporting year:

    period  = reporting_year (DfE's own time_period, 'Reporting year': the
              year ending 31 March; an integer, already a sortable key)
    key     = (lad24cd, age_group)
    edition = one release's figures for one reporting year, BOTH cohorts
              (the two files of one release)

and care_leaver_accommodation_editions_file_checks, the ledger of every file
read. The machinery is editions_core driven by SPEC, through period_editions
(pe) bound to PROFILE; refresh-latest uses the engine's opt-in key changes
(a revision can add or drop authorities). Importing this module needs no
database, no network and no key.

History (so the wording here stays truthful): the n8n S4 workflow loaded on
2026-03-31 twice (run log 13:47 and 13:48 UTC): it inner-joined la_code_lookup
(every county council dropped), kept the last age seen for 22-25 (so the
22-25 rows hold age 25 only) and upserted with ON CONFLICT DO UPDATE.
scripts/verify/rebuild_care_leavers.py ran at least twice on 2026-08-20 and
upserted the 17-21 rows with ON CONFLICT DO UPDATE, overwriting them, with
suppressed cells added as nought (a missing value counted as zero) and
every code recoded through a
private dict of all la_code_lookup rows (Bournemouth and Poole 2019 merged
into BCP, whose all-z row won). care_leaver_accommodation_bak_20260820 is the
state after that script's first run, not the n8n state. migrate-legacy
(one-off) records each held year as loaded (edition 1) and the documented
rule-1 correction from the same files (edition 2), with a proof.

Blanks and zeros (docs/RULES.md rule 1): c (confidential), z (not
applicable) and x (not available) are NULL, never 0; any other marker, a
blank, k or a non-integer in a count halts. A stored column built from parts
(a bucket over categories and age bands, a total over buckets) is NULL
unless every part is a published number; the reasons are in null_reasons. A
row whose every cell is z (the authority did not exist that year) is not
stored. 0 to NULL or NULL to 0 against the tip needs --acknowledge YEAR.
unsuitable_pct is no longer written (rule 7.1).

Identity (rule 3), from the file itself on every path: the header is one of
the three known schemas, the category and age sets are exact,
time_identifier is 'Reporting year', the grid is complete, and (when the
release's page was read) the year range and row count equal the page's
timePeriodRange and numDataFileRows. The release rank is the file's own
maximum time_period; it must equal the release slug and be the same in both
files.

Geography (rule 4): Barnsley and Sheffield through geography.resolve('4')
(declared 'old'); then la_code_lookup 'new_unitary' rows whose old code is an
E10 county with exactly one successor (North Yorkshire, Somerset,
Buckinghamshire: what DfE itself did from the 2024 release); never 'merger'
rows. A resolved code must be in la_boundaries, utla_lad_mapping or
PREDECESSORS (declared, with attribution); anything else is UNEXPLAINED and
stops. Two codes meeting on one key stop.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back). There is no sync-new: migrate-legacy records the held
years.
    python scripts/s4_care_leaver_editions.py ddl [--commit | --simulate]
    python scripts/s4_care_leaver_editions.py status
    python scripts/s4_care_leaver_editions.py load
        [--release SLUG] [--dataset-17-21 ID --dataset-22-25 ID]
        [--file-17-21 PATH --file-22-25 PATH --release YYYY]
        [--recheck YEAR] [--allow-older-file] [--acknowledge YEAR ...]
        [--commit | --simulate]
        # default: the latest release from the EES content API, both dataset
        # ids from the release's data guidance page. Both CSVs are downloaded
        # to data/raw/s4_cla/ (also in a preview; a preview writes nothing to
        # the database). A year breaking a stop condition is REJECTED
        # (nothing stored, exit 1).
    python scripts/s4_care_leaver_editions.py refresh-latest
        [--accept-drift YEAR] [--accept-key-changes YEAR ...]
        [--commit | --simulate]
    python scripts/s4_care_leaver_editions.py migrate-legacy FILE17_2023
        FILE17_2025 FILE22_2025 [--commit | --simulate]
    python scripts/s4_care_leaver_editions.py restore-edition YEAR N
        [--commit | --simulate]
"""
import argparse
import csv
import dataclasses
import hashlib
import io
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import geography  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402

LIVE = "care_leaver_accommodation"
TABLE = "care_leaver_accommodation_editions"
RUN_AGENT = "Source 4 - DfE Care Leaver Accommodation"   # the n8n name
RUN_SOURCE = "4"
SOURCE_FILE = "DfE Children looked after in England: care leaver accommodation"
LIVE_MISSING = pe.LIVE_MISSING

PUBLICATION = "children-looked-after-in-england-including-adoptions"
API_URL = ("https://content.explore-education-statistics.service.gov.uk/api/"
           f"publications/{PUBLICATION}")
SITE = "https://explore-education-statistics.service.gov.uk"
GUIDANCE_URL = SITE + f"/find-statistics/{PUBLICATION}/{{slug}}/data-guidance"
CSV_URL = SITE + "/data-catalogue/data-set/{id}/csv"
REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s4_cla"              # git-ignored
USER_AGENT = ("ucws-pipeline S4 loader (read-only download of the public DfE "
              "care leaver accommodation files)")
MIN_FILE_BYTES = 100 * 1024

COHORTS = ("17-21", "22-25")
COHORT_FILE = {"17-21": "1721_accommodation", "22-25": "2225_suitability"}

# ---------------------------------------------------------------------------
# The three header schemas
# ---------------------------------------------------------------------------

COMMON = ("time_period", "time_identifier", "geographic_level", "country_code",
          "country_name", "region_code", "region_name", "old_la_code",
          "la_name", "new_la_code")
LEVELS = ("National", "Regional", "Local authority")
LA_LEVEL = "Local authority"
TIME_IDENTIFIER = "Reporting year"
LA_CODE_RE = re.compile(r"E(06|08|09|10)[0-9]{6}")

SEMI = "Semi-independent, transitional accommodation"
CATS_1721 = (
    "Bed and breakfast", "Community home", "Deported",
    "Emergency accommodation", "Foyers", "Gone abroad", "In custody",
    "Independent living", "No fixed abode/homeless", "Ordinary lodgings",
    "Other accommodation", "Residence not known", SEMI, "Supported lodgings",
    "Total", "Total information not known", "With former foster carers",
    "With parents or relatives")
AGES_1721 = ("17 to 18 years", "19 to 21 years")
_C1721 = {"ages": {a: a for a in AGES_1721},
          "categories": {c: c for c in CATS_1721}}
SCHEMAS = {
    # 17-21 files of the 2023 and 2024 releases
    "accommodation_type": {
        "columns": COMMON + ("age", "accommodation_type", "number",
                             "percentage"),
        "age": "age", "category": "accommodation_type", "count": "number",
        "cohorts": {"17-21": _C1721}},
    # 22-25 file of the 2024 release
    "suitability_2024": {
        "columns": COMMON + ("age", "accommodation_suitability", "number",
                             "percentage"),
        "age": "age", "category": "accommodation_suitability",
        "count": "number",
        "cohorts": {"22-25": {
            "ages": {"Aged 22": "22", "Aged 23": "23", "Aged 24": "24",
                     "Aged 25": "25"},
            "categories": {
                "Accommodation considered suitable": "suitable",
                "Accommodation considered not suitable": "unsuitable",
                "No information": "no_information",
                "Suitability total": "total"}}}},
    # both files of the 2025 release
    "care_leaver_2025": {
        "columns": COMMON + ("care_leaver_age", "breakdown",
                             "care_leaver_count", "care_leaver_percent"),
        "age": "care_leaver_age", "category": "breakdown",
        "count": "care_leaver_count",
        "cohorts": {
            "17-21": _C1721,
            "22-25": {
                "ages": {"22 years": "22", "23 years": "23", "24 years": "24",
                         "25 years": "25"},
                "categories": {
                    "Accommodation considered suitable": "suitable",
                    "Accommodation considered unsuitable": "unsuitable",
                    "No information": "no_information",
                    "Total": "total"}}}},
}

# 17-21 buckets (docs/nodes/s4_node4_process_cla_data.md; semi_independent is
# the pipeline aggregate, semi_independent_published the DfE category)
BUCKETS_1721 = (
    ("semi_independent", (SEMI, "Foyers", "Supported lodgings")),
    ("independent_living", ("Independent living",)),
    ("with_family", ("With parents or relatives",
                     "With former foster carers")),
    ("community_home", ("Community home",)),
    ("unsuitable", ("Bed and breakfast", "Emergency accommodation",
                    "No fixed abode/homeless")),
    ("other", ("In custody", "Gone abroad", "Deported", "Ordinary lodgings",
               "Other accommodation")),
    ("not_known", ("Residence not known", "Total information not known")),
)
PARTS_1721 = (
    ("semi_independent_published", (SEMI,)),
    ("foyers", ("Foyers",)),
    ("supported_lodgings", ("Supported lodgings",)),
    ("total_published", ("Total",)),
)
COLS_2225 = (
    ("suitable_count", ("suitable",)),
    ("unsuitable", ("unsuitable",)),
    ("not_known", ("no_information",)),
    ("total_published", ("total",)),
)
TOTAL_2225 = ("suitable", "unsuitable", "no_information")
_bucketed = [c for _, cs in BUCKETS_1721 for c in cs]
if sorted(_bucketed + ["Total"]) != sorted(CATS_1721) or \
        len(set(_bucketed)) != len(_bucketed):
    raise RuntimeError("the 17-21 buckets must partition the categories")

# the 16 live value columns and their live types (information_schema,
# checked 2026-10-09), in live order
VALUE_TYPES = (
    ("total_care_leavers", "integer"),
    ("semi_independent", "integer"),
    ("independent_living", "integer"),
    ("with_family", "integer"),
    ("community_home", "integer"),
    ("unsuitable", "integer"),
    ("other", "integer"),
    ("not_known", "integer"),
    ("suitable_count", "integer"),
    ("unsuitable_pct", "numeric"),
    ("uasc_impact_flag", "boolean"),
    ("semi_independent_published", "integer"),
    ("total_published", "integer"),
    ("suppressed_flag", "boolean"),
    ("foyers", "integer"),
    ("supported_lodgings", "integer"),
)
EXTRA_TYPES = (("null_reasons", "text"), ("attribution", "text"),
               ("successor_codes", "text[]"), ("attribution_note", "text"))
VALUE_COLUMNS = tuple(c for c, _ in VALUE_TYPES)
EXTRA_COLUMNS = tuple(c for c, _ in EXTRA_TYPES)
DATA_COLUMNS = VALUE_COLUMNS + EXTRA_COLUMNS
COUNT_COLUMNS = tuple(c for c, t in VALUE_TYPES if t == "integer")
# the columns each cohort fills from the file (the others are NULL for it)
BUILT = {
    "17-21": (tuple(c for c, _ in BUCKETS_1721)
              + tuple(c for c, _ in PARTS_1721) + ("total_care_leavers",)),
    "22-25": (tuple(c for c, _ in COLS_2225) + ("total_care_leavers",)),
}
REASONS = {"c": "suppressed", "z": "not_applicable", "x": "not_available"}

# Declared predecessors (rule 4.2): abolished authorities DfE publishes on
# their own code for the years they held the duty. Carried on that code, never
# propagated; successor_codes is reference only, never a join path.
_NOT_PROPAGATED = ("DfE publishes care leaver outcomes against the authority "
                   "that held the corporate parenting duty in the reporting "
                   "year: rows for the years it existed; for later years DfE "
                   "publishes not applicable (z) and no row is stored. "
                   "Retained against the issuing code and deliberately NOT "
                   "propagated to the successors (a join by predecessor "
                   "would double count). successor_codes is reference only "
                   "and must never be used as a join path.")
PREDECESSORS = {
    "E10000006": (["E06000063", "E06000064"],
                  "Cumbria County Council, abolished 31 March 2023. "
                  + _NOT_PROPAGATED),
    "E10000009": (["E06000059", "E06000058"],
                  "Dorset County Council, abolished 31 March 2019. "
                  + _NOT_PROPAGATED),
    "E10000021": (["E06000061", "E06000062"],
                  "Northamptonshire County Council, abolished 31 March 2021. "
                  + _NOT_PROPAGATED),
    "E06000028": (["E06000058"],
                  "Bournemouth Borough Council, abolished 31 March 2019 "
                  "(merged into Bournemouth, Christchurch and Poole, not the "
                  "same area: la_code_lookup 'merger' rows are never "
                  "applied). " + _NOT_PROPAGATED),
    "E06000029": (["E06000058"],
                  "Poole Borough Council, abolished 31 March 2019 (merged "
                  "into Bournemouth, Christchurch and Poole, not the same "
                  "area: la_code_lookup 'merger' rows are never applied). "
                  + _NOT_PROPAGATED),
}

# stop conditions (calibrated on the 2023->2024 and 2024->2025 releases: at
# most 7.4% total_published change in an authority, national 0.2%)
AUTHORITIES_RANGE = (145, 160)        # per cohort, for a new year
TOTAL_CHANGE = 0.25                   # total_published change ...
TOTAL_CHANGE_AUTHORITIES = 3          # ... in more than this many
NATIONAL_CHANGE = 0.05                # national total_published per cohort

# Before the editions exist (a preview against the live table): the release
# each held year came from (the 2026-08-20 rebuild took 2019-2020 from the
# 2023 release file and 2021-2025 from the 2025 release file; the n8n 22-25
# rows came from the 2025 release file).
LEGACY_RELEASE = {2019: 2023, 2020: 2023, 2021: 2025, 2022: 2025, 2023: 2025,
                  2024: 2025, 2025: 2025}


# ---------------------------------------------------------------------------
# Discovery: content API and the release's data guidance page
# ---------------------------------------------------------------------------

def guidance_url(slug) -> str:
    return GUIDANCE_URL.format(slug=slug)


def csv_url(dataset_id) -> str:
    return CSV_URL.format(id=dataset_id)


def latest_release(api_json) -> tuple:
    """(slug, next_release) from the content API publication reply (a dict
    or its JSON text): latestRelease.slug, and nextReleaseDate as 'YYYY-MM'
    (None if absent). ValueError if there is no latestRelease slug."""
    d = json.loads(api_json) if isinstance(api_json, (str, bytes)) else api_json
    lr = (d or {}).get("latestRelease") or {}
    slug = lr.get("slug")
    if not slug:
        raise ValueError(f"the content API reply has no latestRelease slug: "
                         f"{str(d)[:200]!r}")
    nxt = (d or {}).get("nextReleaseDate") or {}
    nr = None
    if isinstance(nxt, dict) and nxt.get("year"):
        nr = f"{int(nxt['year']):04d}" + (f"-{int(nxt['month']):02d}"
                                          if nxt.get("month") else "")
    return str(slug), nr


_NEXT_RE = re.compile(r"<script[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>"
                      r"(.*?)</script>", re.S | re.I)


def page_data(html) -> dict:
    """The page's __NEXT_DATA__ JSON block. ValueError, quoting the first 200
    characters of what was seen, if the block is absent or not JSON."""
    m = _NEXT_RE.search(html or "")
    if not m:
        raise ValueError("no __NEXT_DATA__ block on the data guidance page; "
                         f"saw {(html or '')[:200]!r}")
    try:
        return json.loads(m.group(1))
    except ValueError as e:
        raise ValueError(f"the __NEXT_DATA__ block is not JSON ({e}); saw "
                         f"{m.group(1)[:200]!r}") from None


def _norm(s) -> str:
    return " ".join(str(s or "").split())


# the two title styles seen: 2023/2024 and 2025
TITLE_PATTERNS = {
    "17-21": re.compile(
        r"(17-21 year old care leavers? accommodation - LA"
        r"|Care leavers \(now 17-21 years\) - accommodation - by local "
        r"authority)", re.I),
    "22-25": re.compile(
        r"(22-25 year old care leavers? by whether their accommodation is "
        r"suitable - LA"
        r"|Care leavers \(now 22-25 years\) - whether their accommodation is "
        r"suitable - by local authority)", re.I),
}


def _page_parts(page) -> tuple:
    try:
        props = page["props"]["pageProps"]
        rvs = props["releaseVersionSummary"]
        content = props["dataContent"]
        sets = content["dataSets"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"the data guidance JSON has changed shape (missing "
                         f"{e}); saw keys "
                         f"{list((page or {}).get('props', {}).get('pageProps', {}))[:10]}"
                         ) from None
    if not isinstance(sets, list):
        raise ValueError("dataContent.dataSets is not a list")
    return rvs, content, sets


def choose_datasets(page) -> dict:
    """{'17-21': ds, '22-25': ds} from the data guidance page JSON
    (page_data): each ds holds dataSetFileId, title, timePeriodRange,
    numDataFileRows, slug, release_version_id, published (ISO date) and
    updateCount. Exactly one dataset per cohort whose title fits one of the
    two title styles and whose geographic levels include Local authority;
    anything else raises ValueError naming the candidates (no winner is
    guessed)."""
    rvs, content, sets = _page_parts(page)
    out, problems = {}, []
    for cohort, rx in TITLE_PATTERNS.items():
        hits = [d for d in sets if rx.fullmatch(_norm(d.get("title")))
                and LA_LEVEL in ((d.get("meta") or {}).get("geographicLevels")
                                 or ())]
        if len(hits) != 1:
            near = [_norm(d.get("title")) for d in sets
                    if cohort in _norm(d.get("title"))]
            problems.append(
                f"{cohort}: {len(hits)} datasets match the title patterns "
                f"({[_norm(d.get('title')) for d in hits]}); titles with "
                f"{cohort}: {near}")
            continue
        d = hits[0]
        meta = d.get("meta") or {}
        published = rvs.get("published")
        out[cohort] = {
            "dataSetFileId": d.get("dataSetFileId"),
            "title": _norm(d.get("title")),
            "timePeriodRange": meta.get("timePeriodRange"),
            "numDataFileRows": meta.get("numDataFileRows"),
            "slug": rvs.get("slug"),
            "release_version_id": (content.get("releaseVersionId")
                                   or rvs.get("id")),
            "published": str(published)[:10] if published else None,
            "updateCount": rvs.get("updateCount"),
            "cohort": cohort,
        }
    if problems:
        raise ValueError("cannot choose one dataset per cohort on the data "
                         "guidance page: " + "; ".join(problems))
    return out


def page_slug(page) -> "str | None":
    rvs, _, _ = _page_parts(page)
    return rvs.get("slug")


# ---------------------------------------------------------------------------
# Reading a file
# ---------------------------------------------------------------------------

def cell(v) -> tuple:
    """(int, None) for a published count, (None, reason) for c, z or x
    (suppressed, not_applicable, not_available). Anything else (a blank, k,
    a negative, a decimal, a thousands separator) raises ValueError."""
    s = (v or "").strip() if isinstance(v, str) else v
    if isinstance(s, str) and s in REASONS:
        return None, REASONS[s]
    if isinstance(s, str) and s.isdigit() and s.isascii():
        return int(s), None
    raise ValueError(f"count value {v!r} is not a whole number or c/z/x")


def _schema_of(header) -> "str | None":
    for name, sc in SCHEMAS.items():
        if tuple(header) == sc["columns"]:
            return name
    return None


def read_file(path) -> dict:
    """{schema, cohort, years, max_year, row_count, rows, la_names, path}
    of one DfE care leaver CSV. rows: {(new_la_code, year): {(age,
    category): (value, reason)}} of the Local authority rows, with the
    schema's canonical ages and categories. Raises ValueError (naming what
    was seen) on an unknown header (listing its columns), an age or category
    set that is not exactly the schema's, a time_identifier other than
    'Reporting year', an unknown geographic level or LA code, an incomplete
    grid (every authority in every year with every age and category), a
    duplicate row, or a count value other than a whole number or c/z/x."""
    p = Path(path)
    name = p.name
    with open(p, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            raise ValueError(f"{name}: empty file")
        schema = _schema_of(header)
        if schema is None:
            raise ValueError(f"{name}: unknown header, nothing read; columns "
                             f"{header}")
        sc = SCHEMAS[schema]
        idx = {c: i for i, c in enumerate(sc["columns"])}
        raw = []
        for n, r in enumerate(reader, start=2):
            if len(r) != len(header):
                raise ValueError(f"{name} line {n}: {len(r)} cells, the "
                                 f"header has {len(header)}")
            raw.append((n, r))
    ages = {r[idx[sc["age"]]] for _, r in raw}
    cats = {r[idx[sc["category"]]] for _, r in raw}
    cohort = None
    for c, spec in sc["cohorts"].items():
        if ages == set(spec["ages"]):
            cohort = c
    if cohort is None:
        raise ValueError(f"{name}: age set {sorted(ages)} is not one of the "
                         f"schema {schema} sets "
                         f"{[sorted(s['ages']) for s in sc['cohorts'].values()]}")
    cs = sc["cohorts"][cohort]
    if cats != set(cs["categories"]):
        raise ValueError(f"{name}: category set differs from the {cohort} "
                         f"schema: unexpected {sorted(cats - set(cs['categories']))}, "
                         f"missing {sorted(set(cs['categories']) - cats)}")
    rows, names, years = {}, {}, set()
    seen = set()
    for n, r in raw:
        where = f"{name} line {n}"
        ti = r[idx["time_identifier"]]
        if ti != TIME_IDENTIFIER:
            raise ValueError(f"{where}: time_identifier {ti!r}, expected "
                             f"{TIME_IDENTIFIER!r}")
        level = r[idx["geographic_level"]]
        if level not in LEVELS:
            raise ValueError(f"{where}: geographic_level {level!r}, expected "
                             f"one of {LEVELS}")
        tp = r[idx["time_period"]]
        if not re.fullmatch(r"[0-9]{4}", tp):
            raise ValueError(f"{where}: time_period {tp!r} is not a year")
        year = int(tp)
        years.add(year)
        try:
            value = cell(r[idx[sc["count"]]])
        except ValueError as e:
            raise ValueError(f"{where}: {e}") from None
        if level != LA_LEVEL:
            continue
        code = r[idx["new_la_code"]]
        if not LA_CODE_RE.fullmatch(code):
            raise ValueError(f"{where}: new_la_code {code!r} is not an E06, "
                             "E08, E09 or E10 code")
        age = cs["ages"][r[idx[sc["age"]]]]
        cat = cs["categories"][r[idx[sc["category"]]]]
        k = (code, year, age, cat)
        if k in seen:
            raise ValueError(f"{where}: duplicate row {code} {year} {age} "
                             f"{cat}; refusing to choose a winner")
        seen.add(k)
        rows.setdefault((code, year), {})[(age, cat)] = value
        names[code] = r[idx["la_name"]]
    grid = {(a, c) for a in cs["ages"].values()
            for c in cs["categories"].values()}
    codes = {c for c, _ in rows}
    for code in sorted(codes):
        for year in sorted(years):
            got = rows.get((code, year))
            if got is None or set(got) != grid:
                missing = sorted(grid - set(got or ()))[:4]
                raise ValueError(f"{name}: incomplete grid for {code} {year}"
                                 f" (missing {missing})")
    if not rows:
        raise ValueError(f"{name}: no Local authority rows")
    return {"path": p, "schema": schema, "cohort": cohort,
            "years": sorted(years), "max_year": max(years),
            "row_count": len(raw), "rows": rows, "la_names": names}


def check_identity(meta, page_ds, slug) -> list:
    """Problems (empty = fine) with a read file's identity: the release
    slug must be a year equal to the file's own maximum time_period (its
    release rank) and the years contiguous; with the page's dataset
    (page_ds, None when no page was read) the cohort, the year range
    (timePeriodRange) and the row count (numDataFileRows) must match."""
    out = []
    years = meta["years"]
    if years != list(range(years[0], years[-1] + 1)):
        out.append(f"years {years} are not contiguous")
    if not re.fullmatch(r"[0-9]{4}", str(slug or "")):
        out.append(f"release slug {slug!r} is not a year")
    elif int(slug) != meta["max_year"]:
        out.append(f"the file's own latest year is {meta['max_year']}, the "
                   f"release is {slug}")
    if page_ds is not None:
        if page_ds.get("cohort") not in (None, meta["cohort"]):
            out.append(f"the file is {meta['cohort']}, the page's dataset is "
                       f"{page_ds.get('cohort')}")
        rng = page_ds.get("timePeriodRange") or {}
        want = (str(rng.get("start")), str(rng.get("end")))
        got = (str(years[0]), str(years[-1]))
        if want != got:
            out.append(f"years {got[0]}-{got[1]}, the page says "
                       f"{want[0]}-{want[1]}")
        if page_ds.get("numDataFileRows") != meta["row_count"]:
            out.append(f"{meta['row_count']} rows, the page says "
                       f"{page_ds.get('numDataFileRows')}")
        if page_ds.get("slug") not in (None, slug):
            out.append(f"the page's release is {page_ds.get('slug')}, not "
                       f"{slug}")
    return out


_RANK_RES = (re.compile(rf"/find-statistics/{re.escape(PUBLICATION)}/"
                       r"([0-9]{4})/"),
             re.compile(r"\(release ([0-9]{4})\)$"))


def release_rank(source_file) -> "int | None":
    """The release an edition (or a ledger row) came from, read from its
    source_file: the slug year of the release's data guidance URL (an
    edition), or the '(release YYYY)' a ledger source ends with; None if it
    names none (an 'as loaded' edition)."""
    for rx in _RANK_RES:
        m = rx.search(str(source_file or ""))
        if m:
            return int(m.group(1))
    return None


def ledger_source(where, slug) -> str:
    """A file's ledger source_file: its CSV URL (or, for --file, its path)
    and the release it belongs to (the file's own latest year), so a
    release that confirmed a year unchanged still ranks that year."""
    return f"{where} (release {slug})"


def content_sha256(path) -> str:
    """SHA-256 of the file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

class Built(dict):
    """{year: [record]} from build_rows; .dropped lists the (code, year)
    rows not stored because every cell is z (the authority did not exist)."""
    dropped: list

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.dropped = []


def _sum(parts) -> tuple:
    """(sum, None) if every part is a published number, else (None, reason)
    with reason the parts' one reason, or 'mixed'."""
    reasons = sorted({r for v, r in parts if v is None})
    if reasons:
        return None, (reasons[0] if len(reasons) == 1 else "mixed")
    return sum(v for v, _ in parts), None


def _record_1721(cells, ages) -> tuple:
    def parts(cats):
        return [cells[(a, c)] for a in ages for c in cats]
    rec, why = {}, {}
    for col, cats in BUCKETS_1721 + PARTS_1721:
        rec[col], why[col] = _sum(parts(cats))
    rec["total_care_leavers"], why["total_care_leavers"] = _sum(
        parts([c for _, cs in BUCKETS_1721 for c in cs]))
    rec["suppressed_flag"] = any(
        r == "suppressed" for _, r in parts(BUCKETS_1721[0][1]))
    return rec, why


def _record_2225(cells, ages) -> tuple:
    def parts(cats):
        return [cells[(a, c)] for a in ages for c in cats]
    rec, why = {}, {}
    for col, cats in COLS_2225:
        rec[col], why[col] = _sum(parts(cats))
    rec["total_care_leavers"], why["total_care_leavers"] = _sum(
        parts(TOTAL_2225))
    rec["suppressed_flag"] = None          # not applicable to 22-25
    return rec, why


def _attribution(code) -> dict:
    if code in PREDECESSORS:
        succ, note = PREDECESSORS[code]
        return {"attribution": "predecessor", "successor_codes": list(succ),
                "attribution_note": note}
    return {"attribution": None, "successor_codes": None,
            "attribution_note": None}


def build_rows(file, resolve) -> Built:
    """{year: [record]} for one read file (read_file). Each record holds
    lad24cd (the publisher code through resolve, a {code: resolved code}
    map or a callable), age_group, reporting_year (as a string, the engine's
    period), every value column, null_reasons and the attribution columns
    (PREDECESSORS). A column built from parts (over categories and age bands)
    is NULL unless every part is a published number; total_care_leavers is
    the sum of the buckets (17-21) or of suitable, unsuitable and no
    information (22-25), NULL unless every part is published;
    total_published is the publisher's Total. suppressed_flag (17-21 only):
    any part of semi_independent is c. null_reasons: 'column=reason' for
    each NULL built column of the cohort, sorted, ';'-joined (None when there
    is none). unsuitable_pct is not written (NULL); uasc_impact_flag stays
    false (reserved). A row whose every cell is z is not stored and is
    listed in .dropped. Two codes resolving to one key raise ValueError."""
    get = resolve if callable(resolve) else (lambda c: resolve[c])
    cohort = file["cohort"]
    ages = sorted({a for cells in file["rows"].values() for a, _ in cells})
    out = Built()
    seen = {}
    for (code, year), cells in sorted(file["rows"].items()):
        if all(r == "not_applicable" for _, r in cells.values()):
            out.dropped.append((code, year))
            continue
        lad = get(code)
        if (lad, year) in seen:
            raise ValueError(f"{year}: {seen[(lad, year)]} and {code} both "
                             f"resolve to {lad} ({cohort}); refusing to "
                             "choose a winner")
        seen[(lad, year)] = code
        built, why = (_record_1721 if cohort == "17-21" else _record_2225)(
            cells, ages)
        rec = {c: None for c in VALUE_COLUMNS}
        rec.update(built)
        rec["uasc_impact_flag"] = False    # reserved, never populated
        rec["unsuitable_pct"] = None       # rule 7.1: no longer written
        nulls = sorted(f"{c}={why[c]}" for c in BUILT[cohort]
                       if rec[c] is None)
        rec["null_reasons"] = ";".join(nulls) if nulls else None
        rec.update(_attribution(lad))
        rec.update(lad24cd=lad, age_group=cohort, reporting_year=str(year))
        out.setdefault(year, []).append(rec)
    for recs in out.values():
        recs.sort(key=lambda r: r["lad24cd"])
    return out


def resolve_codes(cur, codes, by_year) -> tuple:
    """({publisher code: lad24cd}, problems). In this order:
    geography.resolve(cur, '4', ...) (Barnsley/Sheffield; source 4 is
    declared 'old', so E08000038/39 is a problem); then la_code_lookup
    'new_unitary' rows whose old code is an E10 county with exactly one
    successor (never 'merger' rows); then the resolved code must be in
    la_boundaries, utla_lad_mapping or PREDECESSORS, else 'UNEXPLAINED
    code'. by_year: {year: codes with data that year}; two of them
    resolving to one code in a year raise ValueError."""
    codes = set(codes)
    for cs in (by_year or {}).values():
        codes |= set(cs)
    recode, problems = geography.resolve(cur, RUN_SOURCE, codes,
                                         by_period=by_year)
    problems = list(problems)
    cur.execute("SELECT old_code, new_code FROM public.la_code_lookup "
                "WHERE change_type = 'new_unitary' AND old_code LIKE 'E10%'")
    succ = {}
    for old, new in cur.fetchall():
        succ.setdefault(old, set()).add(new)
    county = {o: next(iter(n)) for o, n in succ.items() if len(n) == 1}
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    valid = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT utla_code FROM public.utla_lad_mapping")
    valid |= {r[0] for r in cur.fetchall()}
    out = {}
    for c in sorted(codes):
        r = geography.canonical(c, recode)
        r = county.get(r, r)
        if r not in valid and r not in PREDECESSORS:
            problems.append(f"UNEXPLAINED {c}: not in la_boundaries, "
                            "utla_lad_mapping or the declared predecessors")
        out[c] = r
    for year, cs in sorted((by_year or {}).items()):
        hit = {}
        for c in sorted(cs):
            hit.setdefault(out[c], []).append(c)
        two = {k: v for k, v in hit.items() if len(v) > 1}
        if two:
            raise ValueError(f"{year}: codes {two} resolve to one key; "
                             "refusing to choose a winner")
    return out, problems


def codes_with_data(file) -> dict:
    """{year: {codes whose row is not all z}} of a read file."""
    out = {}
    for (code, year), cells in file["rows"].items():
        if not all(r == "not_applicable" for _, r in cells.values()):
            out.setdefault(year, set()).add(code)
    return out


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

def _by_key(records) -> dict:
    return {(r["lad24cd"], r["age_group"]): r for r in records}


def _published(r) -> bool:
    return any(r.get(c) is not None and r.get(c) is not False
               for c in BUILT.get(r["age_group"], ()))


def year_problems(new, tip, *, kind, all_z=()) -> list:
    """The stop conditions of one year (empty = fine). new: the year's
    records from the release; tip: for kind 'revised' the records of the
    year's tip edition, for kind 'new' those of the held latest year (or
    empty). Both: a held authority (of the tip, in a cohort the release
    covers) with a published number absent from the release, unless the
    release publishes it as all z (all_z: its (lad24cd, age_group) keys,
    listed by the caller, not stopped). New year:
    authorities per cohort outside AUTHORITIES_RANGE. Revised year: a cohort
    the tip holds that the release does not cover while the tip holds both;
    total_published changing by more than TOTAL_CHANGE in more than
    TOTAL_CHANGE_AUTHORITIES authorities of a cohort; the national
    total_published of a cohort moving by more than NATIONAL_CHANGE."""
    out = []
    nk, tk = _by_key(new), _by_key(tip or ())
    ncoh = sorted({a for _, a in nk})
    tcoh = sorted({a for _, a in tk})
    if kind == "revised" and len(tcoh) == 2 and len(ncoh) < 2:
        out.append(f"the release covers {ncoh or 'no cohort'} for this year "
                   f"but the tip holds both {tcoh}; no carry-forward")
    gone = sorted(k for k, r in tk.items() if k[1] in ncoh
                  and _published(r) and k not in nk
                  and k not in set(all_z))
    if gone:
        out.append(f"{len(gone)} held authorit(ies) with a published number "
                   f"absent from the release: "
                   + ", ".join(f"{c} {a}" for c, a in gone[:8]))
    if kind == "new":
        lo, hi = AUTHORITIES_RANGE
        for a in ncoh:
            n = sum(1 for _, x in nk if x == a)
            if not lo <= n <= hi:
                out.append(f"{a}: {n} authorities, expected {lo}-{hi}")
        return out
    for a in ncoh:
        big = []
        for k, r in sorted(nk.items()):
            if k[1] != a or k not in tk:
                continue
            o, n = tk[k].get("total_published"), r.get("total_published")
            if o is None or n is None or o == n:
                continue
            if o == 0 or abs(n - o) / o > TOTAL_CHANGE:
                big.append(f"{k[0]} {o}->{n}")
        if len(big) > TOTAL_CHANGE_AUTHORITIES:
            out.append(f"{a}: total_published changes by more than "
                       f"{TOTAL_CHANGE:.0%} in {len(big)} authorities (limit "
                       f"{TOTAL_CHANGE_AUTHORITIES}): {', '.join(big[:6])}")
        old_t = [tk[k]["total_published"] for k in tk if k[1] == a
                 and tk[k].get("total_published") is not None]
        new_t = [nk[k]["total_published"] for k in nk if k[1] == a
                 and nk[k].get("total_published") is not None]
        if old_t and new_t:
            o, n = sum(old_t), sum(new_t)
            if o and abs(n - o) / o > NATIONAL_CHANGE:
                out.append(f"{a}: national total_published {o:,} -> {n:,} "
                           f"({(n - o) / o:+.1%}, limit "
                           f"{NATIONAL_CHANGE:.0%})")
    return out


def _flips(new, tip) -> list:
    nk, tk = _by_key(new), _by_key(tip or ())
    out = []
    for k in sorted(set(nk) & set(tk)):
        for c in COUNT_COLUMNS:
            a, b = tk[k].get(c), nk[k].get(c)
            if (a == 0 and b is None) or (a is None and b == 0):
                out.append((k[0], k[1], c, a, b))
    return out


def zero_null_flips(new, tip) -> int:
    """Count cells (common keys, count columns) going from 0 to NULL or NULL
    to 0 against the tip (rule 1.10)."""
    return len(_flips(new, tip))


# ---------------------------------------------------------------------------
# Spec and profile
# ---------------------------------------------------------------------------

def tip_row_count(editions_table: str):
    """expected_rows_per_period for status: the tip edition's row count (a
    year's authorities and cohorts are its own)."""
    core._ident(editions_table)

    def count(cur, period):
        cur.execute(f"SELECT DISTINCT edition, supersedes FROM "
                    f"public.{editions_table} WHERE reporting_year = %s",
                    (period,))
        try:
            tip = core._chain_tip(cur.fetchall(), str(period))
        except (LookupError, ValueError):
            return None
        cur.execute(f"SELECT COUNT(*) FROM public.{editions_table} WHERE "
                    "reporting_year = %s AND edition = %s", (period, tip))
        return cur.fetchone()[0]
    return count


SPEC = core.EditionSpec(
    name="s4",
    live_table=LIVE,
    editions_table=TABLE,
    key_cols=("lad24cd", "age_group"),
    period_col="reporting_year",
    value_cols=VALUE_TYPES,
    extra_cols=EXTRA_TYPES,
    refresh_cols=DATA_COLUMNS + ("source", "loaded_at"),
    refresh_from=(("source", "source_file"), ("loaded_at", "loaded_at")),
    as_loaded_source_col="source",
    refresh_source_whole_period=True,   # one source per refreshed year
    key_types=(("lad24cd", "varchar(9) NOT NULL"),
               ("age_group", "varchar(5) NOT NULL"),
               ("reporting_year", "integer NOT NULL")),
    fk_la_boundaries=False,             # counties (E10) are not in it
    expected_rows_per_period=tip_row_count(TABLE),
    refresh_key_changes=True,
)


def _no_label(fetched_on) -> str:
    raise ValueError("an S4 release label comes from the release; store "
                     "through apply_year(...)")


def check_year(records, period) -> None:
    """ValueError unless the year's records are whole: each of this year,
    one per (lad24cd, age_group), cohort 17-21 or 22-25."""
    seen = set()
    for r in records:
        if r.get("reporting_year") != period:
            raise ValueError(f"{period}: a record of year "
                             f"{r.get('reporting_year')!r}")
        k = (r["lad24cd"], r["age_group"])
        if r["age_group"] not in COHORTS:
            raise ValueError(f"{period}: age_group {r['age_group']!r}")
        if k in seen:
            raise ValueError(f"{period}: {k} appears twice")
        seen.add(k)


def _late(name: str):
    """A call through this module's attribute `name`, looked up when called
    (so mock.patch.object(s4_care_leaver_editions, name, ...) reaches the
    engine)."""
    return lambda *a, **kw: globals()[name](*a, **kw)


PROFILE = pe.Profile(
    spec=SPEC, value_cols=DATA_COLUMNS, run_agent=RUN_AGENT,
    run_source=RUN_SOURCE, heading="S4 DfE care leaver accommodation",
    default_source_file=SOURCE_FILE,
    expected_areas=None,    # a year's size is its own (tip row count)
    release_label=_no_label, check_records=check_year, file_checks=True,
    savepoint="s4_year", example_label="(care leaver values)")


def _profile(spec) -> pe.Profile:
    return PROFILE.with_spec(spec)


def create_all(cur, spec=SPEC) -> None:
    """ddl: the engine's editions table and triggers, and the live column
    null_reasons (additive, NULL until refreshed). The engine adds the
    file-check ledger."""
    core.create_schema(cur, spec)
    cur.execute(f"ALTER TABLE public.{spec.live_table} "
                "ADD COLUMN IF NOT EXISTS null_reasons text")


def insert_live(cur, profile, period: str, records: list) -> None:
    """A new year's live rows: the value columns, null_reasons, the
    attribution columns and `source` (the release's data guidance URL, each
    record's 'source'); loaded_at takes its default."""
    from psycopg2.extras import execute_values
    spec = profile.spec
    cols = (tuple(spec.key_cols) + (spec.period_col,)
            + tuple(profile.value_cols) + ("source",))
    execute_values(
        cur, f"INSERT INTO public.{spec.live_table} ({', '.join(cols)}) "
        "VALUES %s",
        [tuple(r[k] for k in spec.key_cols) + (period,)
         + tuple(r.get(c) for c in profile.value_cols) + (r["source"],)
         for r in records],
        page_size=1000)


@dataclass(frozen=True)
class YearInfo:
    """What a year's edition records: source_file (the release's data
    guidance URL), the release label and the files read for it
    ((ledger source_file, sha256), one per file covering the year)."""
    source_file: str
    label: str
    files: tuple


def apply_year(cur, profile, period, records, *, fetched_on, info):
    """pe.apply_period for one year, then, in the same savepoint, a ledger
    row for each file of the release covering the year: a new year's
    edition 1, live rows and ledger rows commit or roll back together."""
    prof = dataclasses.replace(profile, release_label=lambda d: info.label)
    kind = pe.apply_period(cur, prof, period, records, fetched_on=fetched_on,
                           source_file=info.source_file,
                           insert=_late("insert_live"))
    tip = core.chain_tip(cur, prof.spec, period)
    for src, sha in info.files:
        pe.record_file_check(cur, prof, period, src, sha, kind, tip)
    return kind


# ---------------------------------------------------------------------------
# Files: download
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


def fetch_api(session=None) -> dict:
    """The content API publication reply (a read-only GET)."""
    return json.loads(_get(_session(session), API_URL).text)


def fetch_page(slug, session=None) -> str:
    """The release's data guidance page HTML (a read-only GET)."""
    return _get(_session(session), guidance_url(slug)).text


def fetch_csv(url, dest, session=None) -> Path:
    """Download a dataset CSV to dest (a file path; the caller names it
    <release>_<cohort>_<id8>.csv, since DfE's own name is the same every
    release) and return the path to read. Checked before it is kept: more
    than MIN_FILE_BYTES and a header that is one of SCHEMAS. A same-named
    file with the same content is kept as it is; one with different content
    is never replaced: the new download is saved beside it as
    <stem>-<sha8>.csv and that is the one read."""
    dest = Path(dest)
    body = _get(_session(session), url).content
    if len(body) <= MIN_FILE_BYTES:
        halt(f"{url}: {len(body)} bytes, expected more than "
             f"{MIN_FILE_BYTES}; nothing written; saw {body[:100]!r}")
    try:
        first = body.decode("utf-8-sig").splitlines()[0]
        header = next(csv.reader(io.StringIO(first)))
    except (UnicodeDecodeError, IndexError, StopIteration) as e:
        halt(f"{url}: not a readable CSV ({e}); nothing written")
    if _schema_of(header) is None:
        halt(f"{url}: unknown header, nothing written; columns {header}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    new_sha = hashlib.sha256(body).hexdigest()
    path = dest
    if dest.exists():
        old_sha = content_sha256(dest)
        if old_sha == new_sha:
            print(f"{dest.name} already held with the same content (sha256 "
                  f"{new_sha[:16]}); kept as it is")
            return dest
        path = dest.with_name(f"{dest.stem}-{new_sha[:8]}{dest.suffix}")
        if path.exists() and content_sha256(path) == new_sha:
            print(f"NOTE: {dest.name} differs from this download; "
                  f"{path.name} already holds it (sha256 {new_sha[:16]})")
            return path
        print(f"NOTE: {dest.name} already exists with different content "
              f"(sha256 {old_sha[:16]}, new {new_sha[:16]}); kept it "
              f"untouched and saved the new download as {path.name}")
    else:
        print(f"downloaded {dest.name}: {len(body):,} bytes, sha256 "
              f"{new_sha[:16]}")
    tmp = path.with_name(path.name + ".download.tmp")
    tmp.write_bytes(body)
    tmp.replace(path)
    return path


def raw_name(slug, cohort, dataset_id) -> str:
    return f"{slug}_{COHORT_FILE[cohort]}_{str(dataset_id)[:8]}.csv"


def _file_source(path: Path) -> str:
    """The ledger source_file of a --file: the path under the repo
    (data/raw/...), else the absolute path."""
    p = Path(path).resolve()
    try:
        return p.relative_to(REPO).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------------------
# Planning a load
# ---------------------------------------------------------------------------

def plan_years(files, tips, checked, *, recheck, allow_older) -> tuple:
    """(new, revised, skipped). files: the run's files [{years, rank,
    source, sha}] (one release, one rank); tips: {held year: the rank of
    its tip (release_rank), or None}; checked: {year: {(source, sha)}} of
    the ledger (stranded years left out by the caller, so they are always
    read). new: years of the files not held; revised: held years to compare
    with their tip; skipped: {year: reason} for held years whose tip came
    from a newer release (unless allow_older; the older-file guard, per
    year, on every path) or whose every file is already in the ledger for
    that year (unless --recheck names it). --recheck YEAR must be a held
    year of the files (ValueError)."""
    years = sorted({y for f in files for y in f["years"]})
    ranks = {f["rank"] for f in files}
    if len(ranks) != 1:
        raise ValueError(f"the files come from different releases {sorted(ranks)}")
    rank = ranks.pop()
    if recheck is not None and (recheck not in years or recheck not in tips):
        raise ValueError(f"--recheck {recheck}: not a held year of these "
                         f"files ({years})")
    new, revised, skipped = [], [], {}
    for y in years:
        if y not in tips:
            new.append(y)
            continue
        t = tips[y]
        if t is not None and t > rank and not allow_older:
            skipped[y] = (f"older: its tip is from the {t} release, this is "
                          f"the {rank} release")
            continue
        mine = [(f["source"], f["sha"]) for f in files if y in f["years"]]
        if recheck != y and all(m in checked.get(y, ()) for m in mine):
            skipped[y] = "checked: every file is already in the ledger"
            continue
        revised.append(y)
    return new, revised, skipped


# ---------------------------------------------------------------------------
# Reading held data
# ---------------------------------------------------------------------------

table_exists = pe.table_exists


def _conn(writing):
    return pe.connect(writing)


def column_exists(cur, table, column) -> bool:
    cur.execute("SELECT 1 FROM information_schema.columns WHERE "
                "table_schema = 'public' AND table_name = %s AND "
                "column_name = %s", (table, column))
    return cur.fetchone() is not None


def _dicts(cur, cols) -> list:
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _live_cols(cur, spec) -> tuple:
    """The data columns as a SELECT list for the live table (null_reasons
    as NULL before ddl added it)."""
    have = column_exists(cur, spec.live_table, "null_reasons")
    return tuple(c if (c != "null_reasons" or have) else "NULL::text"
                 for c in DATA_COLUMNS)


def live_records(cur, spec, year) -> list:
    sel = ", ".join(("lad24cd", "age_group") + _live_cols(cur, spec))
    cur.execute(f"SELECT {sel} FROM public.{spec.live_table} WHERE "
                "reporting_year = %s", (year,))
    recs = _dicts(cur, ("lad24cd", "age_group") + DATA_COLUMNS)
    for r in recs:
        r["reporting_year"] = str(year)
    return recs


def edition_records(cur, spec, year, edition) -> list:
    cols = ("lad24cd", "age_group") + DATA_COLUMNS
    cur.execute(f"SELECT {', '.join(cols)} FROM public.{spec.editions_table} "
                "WHERE reporting_year = %s AND edition = %s", (year, edition))
    recs = _dicts(cur, cols)
    for r in recs:
        r["reporting_year"] = str(year)
    return recs


def tip_info(cur, spec) -> dict:
    """{year: {edition, source_file, rank, cohorts}} of every year with
    editions."""
    cur.execute(f"SELECT DISTINCT reporting_year FROM public.{spec.editions_table}")
    out = {}
    for (y,) in cur.fetchall():
        tip = core.latest_edition(cur, spec, y)
        cur.execute(f"SELECT DISTINCT source_file, age_group FROM "
                    f"public.{spec.editions_table} WHERE reporting_year = %s "
                    "AND edition = %s", (y, tip))
        rows = cur.fetchall()
        src = {s for s, _ in rows}
        sf = src.pop() if len(src) == 1 else None
        rank = release_rank(sf)
        if rank is None:
            rank = LEGACY_RELEASE.get(int(y))
        out[int(y)] = {"edition": tip, "source_file": sf, "rank": rank,
                       "cohorts": sorted({a for _, a in rows})}
    return out


def legacy_tips(cur, spec) -> dict:
    """Before the editions table exists: {held live year: {rank from
    LEGACY_RELEASE, cohorts}}."""
    cur.execute(f"SELECT reporting_year, array_agg(DISTINCT age_group) FROM "
                f"public.{spec.live_table} GROUP BY 1")
    return {int(y): {"edition": None, "source_file": None,
                     "rank": LEGACY_RELEASE.get(int(y)),
                     "cohorts": sorted(a)} for y, a in cur.fetchall()}


def ledger_checks(cur, profile) -> dict:
    """{year (int): {(source_file, file_sha256)}} of the file-check
    ledger."""
    cur.execute(f"SELECT DISTINCT reporting_year, source_file, file_sha256 "
                f"FROM public.{pe.file_checks_table(profile)}")
    out = {}
    for y, f, s in cur.fetchall():
        out.setdefault(int(y), set()).add((f, s))
    return out


def held_years(cur, spec, editions_exist) -> list:
    return sorted(int(p) for p in pe.held_periods(cur, _profile(spec),
                                                  editions_exist))


def _ready(cur, spec) -> dict:
    prof = _profile(spec)
    return {spec.editions_table: table_exists(cur, spec.editions_table),
            pe.file_checks_table(prof): table_exists(
                cur, pe.file_checks_table(prof)),
            f"{spec.live_table}.null_reasons": column_exists(
                cur, spec.live_table, "null_reasons")}


# ---------------------------------------------------------------------------
# Status, run log
# ---------------------------------------------------------------------------

def status(cur, spec=SPEC) -> dict:
    return pe.status(cur, _profile(spec))


def status_lines(cur, spec=SPEC) -> list:
    """Per year: the tip's release and edition, whether live `source` is
    uniform and equals the tip's, and whether live null_reasons is
    populated."""
    lines = []
    have_nr = column_exists(cur, spec.live_table, "null_reasons")
    for y, t in sorted(tip_info(cur, spec).items()):
        cur.execute(f"SELECT COUNT(DISTINCT source), MIN(source), "
                    + ("COUNT(null_reasons)" if have_nr else "NULL")
                    + f" FROM public.{spec.live_table} WHERE "
                    "reporting_year = %s", (y,))
        n_src, src, n_nr = cur.fetchone()
        cur.execute(f"SELECT COUNT(null_reasons) FROM "
                    f"public.{spec.editions_table} WHERE reporting_year = %s "
                    "AND edition = %s", (y, t["edition"]))
        want_nr = cur.fetchone()[0]
        uniform = (n_src == 1 and src == t["source_file"])
        rel = f"release {t['rank']}" if t["rank"] else "as loaded"
        lines.append(
            f"  {y}: tip edition {t['edition']} ({rel}; cohorts "
            f"{', '.join(t['cohorts'])}); live source "
            + ("uniform, the tip's" if uniform else
               f"{n_src} value(s), not the tip's")
            + "; null_reasons "
            + ("column absent (run ddl)" if not have_nr else
               (f"populated ({n_nr} rows with reasons, as the tip)"
                if n_nr == want_nr else
                f"not populated ({n_nr} rows with reasons, the tip "
                f"{want_nr}; NULL until refresh-latest)")))
    return lines


def format_status(st: dict, extra=()) -> str:
    return pe.format_status(PROFILE, st, extra)


def log_run(cur, rows_written: int, notes: str, started_at=None) -> None:
    """The pipeline_run_log row for a committed run (agent RUN_AGENT, source
    '4'). Called only on load, migrate-legacy and restore-edition with
    --commit."""
    pe.log_run(cur, PROFILE, rows_written, notes, started_at)


def cmd_ddl(args) -> int:
    return pe.run_ddl(_profile(SPEC), args, connect=_late("_conn"),
                      table_exists=_late("table_exists"),
                      create_schema=lambda cur: create_all(cur, SPEC))


def cmd_status(args) -> int:
    """status: what needs action; exit 1 if anything, or (with a clean
    message, nothing created) if the editions table does not exist."""
    spec = SPEC
    conn = _conn(False)
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, spec.editions_table):
                print(f"status: {spec.editions_table} does not exist yet; "
                      "run `ddl --commit`, then `migrate-legacy FILE17_2023 "
                      "FILE17_2025 FILE22_2025 --commit` (S4 has no "
                      "sync-new)")
                return 1
            lines = status_lines(cur, spec)
            st = status(cur, spec)
    finally:
        conn.rollback()
        conn.close()
    print(format_status(st, lines))
    return 0 if st["ok"] else 1


def cmd_refresh_latest(args) -> int:
    return pe.run_refresh_latest(_profile(SPEC), args, connect=_late("_conn"),
                                 table_exists=_late("table_exists"))


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


def _year_arg(text) -> int:
    if not re.fullmatch(r"[0-9]{4}", str(text or "")):
        raise argparse.ArgumentTypeError(f"not a year YYYY: {text!r}")
    return int(text)


def _sources(args, held) -> dict:
    """The run's release and its files, by path: {slug, page, datasets
    {cohort: ds or None}, files {cohort: (path, ledger source, url)},
    next_release, how}. Downloads (also in a preview) to RAW_DIR."""
    by_file = args.file_17_21 or args.file_22_25
    by_id = args.dataset_17_21 or args.dataset_22_25
    if by_file and by_id:
        halt("give --file-17-21/--file-22-25 or --dataset-17-21/"
             "--dataset-22-25, not both")
    if (by_file or by_id) and not args.release:
        halt("--file-* and --dataset-* need --release YYYY (the release the "
             "files belong to; checked against each file's own latest year)")
    out = {"slug": None, "page": None, "datasets": {}, "files": {},
           "next_release": None, "how": None}
    if by_file:
        out.update(slug=str(args.release), how="local files")
        for c, p in (("17-21", args.file_17_21), ("22-25", args.file_22_25)):
            if p:
                path = Path(p)
                out["files"][c] = (path, ledger_source(_file_source(path),
                                                       out["slug"]), None)
        print("files given: nothing downloaded")
        return out
    if by_id:
        out.update(slug=str(args.release), how="dataset ids given")
        ids = {"17-21": args.dataset_17_21, "22-25": args.dataset_22_25}
        print("dataset ids given: the data guidance page is not read")
    else:
        if args.release:
            slug, how = str(args.release), "release given"
        else:
            slug, nxt = latest_release(fetch_api())
            out["next_release"] = nxt
            how = "latest release (content API)"
            print(f"content API: latest release {slug}; next release "
                  f"{nxt or '-'}")
            if held and re.fullmatch(r"[0-9]{4}", slug) and \
                    int(slug) < max(held):
                msg = (f"the latest release {slug} is older than the held "
                       f"latest year {max(held)}")
                if not args.allow_older_file:
                    halt(msg + ". If this is deliberate, re-run with "
                         "--allow-older-file")
                print(f"NOTE: --allow-older-file given; {msg}")
        try:
            page = page_data(fetch_page(slug))
            ds = choose_datasets(page)
            pslug = page_slug(page)
        except ValueError as e:
            halt(f"data guidance page {guidance_url(slug)}: {e}. Fallback: "
                 "--release YYYY --dataset-17-21 ID --dataset-22-25 ID")
        if pslug != slug:
            halt(f"the content API names release {slug} but the data "
                 f"guidance page is release {pslug}")
        out.update(slug=slug, page=page, datasets=ds, how=how)
        ids = {c: d["dataSetFileId"] for c, d in ds.items()}
        any_ds = next(iter(ds.values()))
        print(f"release {slug}: {guidance_url(slug)}; published "
              f"{any_ds['published']}; release version "
              f"{any_ds['release_version_id']}; updateCount "
              f"{any_ds['updateCount']}")
        for c, d in sorted(ds.items()):
            print(f"  {c}: dataset {d['dataSetFileId']} {d['title']!r}; "
                  f"years {d['timePeriodRange']}; rows "
                  f"{d['numDataFileRows']}")
    for c, i in sorted(ids.items()):
        if not i:
            continue
        url = csv_url(i)
        path = fetch_csv(url, RAW_DIR / raw_name(out["slug"], c, i))
        out["files"][c] = (path, ledger_source(url, out["slug"]), url)
    return out


def tip_ranks(tips, checked) -> dict:
    """{held year: the newest release that stated it}: the tip edition's
    rank, or a later release whose file the ledger records for the year
    (a release that found the year unchanged stores no edition)."""
    out = {}
    for y, t in tips.items():
        ranks = [t["rank"]] + [release_rank(s) for s, _ in checked.get(y, ())]
        ranks = [r for r in ranks if r is not None]
        out[y] = max(ranks) if ranks else None
    return out


def release_label(slug, src, files, year, how, *, acknowledged="",
                  older="") -> str:
    ds = next(iter(src["datasets"].values()), None) if src["datasets"] else None
    head = (f"DfE CLA care leaver accommodation, release {slug}"
            + (f" (published {ds['published']}, release version "
               f"{ds['release_version_id']}, updateCount {ds['updateCount']})"
               if ds else "")
            + f"; reporting year {year}; ")
    parts = [f"{f['cohort']} {f['source']} sha256 {f['sha'][:16]}"
             for f in files if year in f["years"]]
    return (head + "; ".join(parts) + f"; {how}"
            + (f"; {older}" if older else "")
            + (f"; ACKNOWLEDGED: {acknowledged}" if acknowledged else ""))


def cmd_load(args) -> int:
    spec = SPEC
    base = _profile(spec)
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            ready = _ready(cur, spec)
            has_ed = all(ready.values())
            if writing and not has_ed:
                halt("missing " + ", ".join(t for t, ok in ready.items()
                                           if not ok)
                     + "; run `ddl --commit` (and migrate-legacy) first")
            if has_ed:
                _, new_live, errors = core.latest_map(cur, spec)
                if errors:
                    halt("invalid edition chain: " + "; ".join(
                        f"{pe._p(p)}: {msg}" for p, msg in errors.items()))
                if new_live:
                    halt(f"live years with no editions "
                         f"{[pe._p(x) for x in new_live]}; run "
                         "migrate-legacy first")
                tips = tip_info(cur, spec)
                checked = ledger_checks(cur, base)
                stranded = [int(p) for p in pe.live_missing_periods(cur, base)]
            else:
                print("NOTE: " + ", ".join(t for t, ok in ready.items()
                                           if not ok)
                      + " not present yet; this preview compares each year "
                      f"with the LIVE table {spec.live_table} (tips' "
                      "releases as the old builds read them)")
                tips = legacy_tips(cur, spec)
                checked, stranded = {}, []
            held = held_years(cur, spec, has_ed)
            print("held years: " + (f"{held[0]} .. {held[-1]}" if held
                                    else "none"))
            src = _sources(args, held)
            slug = src["slug"]
            metas = []
            for c in COHORTS:
                if c not in src["files"]:
                    continue
                path, ledger_src, url = src["files"][c]
                metas.append({"cohort": c, "path": path, "source": ledger_src,
                              "url": url, "sha": content_sha256(path)})
            if not metas:
                halt("no file to read")
            # a file pair already in the ledger is not parsed again
            if has_ed and args.recheck is None:
                seen = {}
                for y, pairs in checked.items():
                    for pr in pairs:
                        seen.setdefault(pr, set()).add(y)
                yrs = [seen.get((m["source"], m["sha"])) for m in metas]
                if all(yrs) and not (set().union(*yrs) & set(stranded)):
                    print("both files' (URL, sha256) are already in the "
                          "ledger for " + ", ".join(
                              f"{m['cohort']} {sorted(s)}"
                              for m, s in zip(metas, yrs))
                          + "; nothing parsed (--recheck YEAR reads them "
                          "again)")
                    print("nothing to do")
                    return _end(conn, args, writing, rc=0)
            for m in metas:
                try:
                    meta = read_file(m["path"])
                except ValueError as e:
                    halt(f"{e}; nothing stored")
                if meta["cohort"] != m["cohort"]:
                    halt(f"{m['path'].name}: the file is {meta['cohort']}, "
                         f"given as {m['cohort']}; nothing stored")
                bad = check_identity(meta, src["datasets"].get(m["cohort"]),
                                     slug)
                if bad:
                    halt(f"file identity check failed for {m['path'].name}, "
                         "nothing stored: " + "; ".join(bad))
                m.update(meta=meta, years=meta["years"],
                         rank=meta["max_year"])
                print(f"{m['path'].name}: {m['cohort']} ({meta['schema']}), "
                      f"years {meta['years'][0]}-{meta['years'][-1]}, "
                      f"{meta['row_count']:,} rows, sha256 {m['sha'][:16]}")
            if len({m["rank"] for m in metas}) != 1:
                halt("the files come from different releases (their own "
                     "latest years " + ", ".join(
                         f"{m['cohort']} {m['rank']}" for m in metas)
                     + "); one edition is one release; nothing stored")
            rank = metas[0]["rank"]
            rc = _run_years(args, conn, cur, spec, base, has_ed, src, metas,
                            rank, tips, checked, stranded, held)
        return _end(conn, args, writing, rc)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _build_all(cur, metas) -> dict:
    """{year: records of both cohorts} of the run's files, codes resolved."""
    codes, by_year = set(), {}
    for m in metas:
        codes |= {c for c, _ in m["meta"]["rows"]}
        for y, cs in codes_with_data(m["meta"]).items():
            by_year.setdefault(y, set()).update(cs)
    try:
        rmap, problems = resolve_codes(cur, codes, by_year)
    except ValueError as e:
        halt(f"geography: {e}; nothing stored")
    if problems:
        halt("geography check failed, nothing stored: " + "; ".join(problems))
    note = geography.confirm_note(RUN_SOURCE)
    if note:
        print(note)
    out = {}
    for m in metas:
        try:
            built = build_rows(m["meta"], rmap)
        except ValueError as e:
            halt(f"{m['path'].name}: {e}; nothing stored")
        m["dropped"] = [(rmap[c], y) for c, y in built.dropped]
        for y, recs in built.items():
            out.setdefault(y, []).extend(recs)
    recoded = sorted((c, r) for c, r in rmap.items() if c != r)
    if recoded:
        print("codes resolved: " + ", ".join(f"{c} -> {r}"
                                             for c, r in recoded))
    return out


def _run_years(args, conn, cur, spec, base, has_ed, src, metas, rank, tips,
               checked, stranded, held) -> int:
    started = datetime.now(timezone.utc)
    slug = src["slug"]
    by_year = _build_all(cur, metas)
    for m in metas:
        if m["dropped"]:
            print(f"  {m['cohort']}: {len(m['dropped'])} all-z rows not "
                  "stored (the authority did not exist that year): "
                  + ", ".join(f"{c} {y}" for c, y in m["dropped"][:12])
                  + (" ..." if len(m["dropped"]) > 12 else ""))
    chk = {y: s for y, s in checked.items() if y not in stranded}
    ranks = tip_ranks(tips, checked)
    try:
        new, revised, skipped = plan_years(
            metas, ranks, chk,
            recheck=args.recheck, allow_older=args.allow_older_file)
    except ValueError as e:
        halt(str(e))
    older_note = {}
    for y in revised:
        t = ranks[y]
        if t is not None and t > rank:
            older_note[y] = (f"NOTE: --allow-older-file given; {y}'s tip is "
                             f"from the {t} release, this is the {rank} "
                             "release")
            print(older_note[y])
    print(f"planned: new {new or 'none'}; compare {revised or 'none'}; "
          f"skipped " + (", ".join(f"{y} ({r})" for y, r in sorted(
              skipped.items())) or "none"))
    years = sorted(new + revised)
    if not years:
        if any(r.startswith("older") for r in skipped.values()):
            halt(f"every year in the files is skipped: the {rank} release is "
                 "older than the tips of all its years; storing it would "
                 "record an older release. If this is deliberate, re-run "
                 "with --allow-older-file")
        print("nothing to do")
        return 0
    ack = set(args.acknowledge or ())
    stray = sorted(ack - set(revised))
    if stray:
        halt(f"--acknowledge {stray}: not revised years of this run "
             f"({revised or 'none'})")
    url = guidance_url(slug)
    for recs in by_year.values():
        for r in recs:
            r["source"] = url
    latest = max(held) if held else None
    problems, acked, labels = {}, {}, {}
    for y in years:
        recs = by_year.get(y, [])
        if not recs:
            problems[y] = ["the release has no stored row for this year"]
            continue
        if y in revised:
            if has_ed:
                tiprecs = edition_records(cur, spec, y, tips[y]["edition"])
            else:
                tiprecs = live_records(cur, spec, y)
            kind = "revised"
        else:
            tiprecs = ((edition_records(cur, spec, latest,
                                        tips[latest]["edition"])
                        if has_ed else live_records(cur, spec, latest))
                       if latest is not None and latest < y else [])
            kind = "new"
        allz = {(c, m["cohort"]) for m in metas for c, yy in m["dropped"]
                if yy == y}
        bad = year_problems(recs, tiprecs, kind=kind, all_z=allz)
        note = ""
        if kind == "revised":
            fl = _flips(recs, tiprecs)
            if fl:
                msg = (f"{len(fl)} cell(s) go from 0 to NULL or NULL to 0 "
                       f"against the tip (rule 1.10), e.g. "
                       + ", ".join(f"{c} {a} {col} {o}->{n}"
                                   for c, a, col, o, n in fl[:3]))
                if y in ack:
                    acked[y] = note = msg
                    print(f"  {y}: ACKNOWLEDGED (--acknowledge): {msg}")
                else:
                    bad.append(msg + f"; read them, then --acknowledge {y}")
        if bad:
            problems[y] = bad
            continue
        how = ("new year" if kind == "new" else "republished year") + \
            f" ({src['how']})"
        labels[y] = YearInfo(
            url, release_label(slug, src, metas, y, how, acknowledged=note,
                               older=older_note.get(y, "").removeprefix(
                                   "NOTE: ")),
            tuple((m["source"], m["sha"]) for m in metas
                  if y in m["years"]))
    for y, msgs in sorted(problems.items()):
        for msg in msgs:
            print(f"  {y}: REJECTED, not stored: STOP CONDITION: {msg}")
    ok = [y for y in years if y not in problems]
    rc = 0
    stats = {"periods": [], "kinds": {}, "stored_rows": 0, "live_rows": 0}
    if ok and has_ed:
        rc = pe.load_periods(
            cur, base, [str(y) for y in ok], lambda p: by_year[int(p)],
            date.fromisoformat(next(iter(src["datasets"].values()))
                               ["published"])
            if src["datasets"] else date.today(),
            args.commit, simulate=args.simulate, against="editions",
            stats=stats,
            apply=lambda c, prof, p, recs, *, fetched_on, source_file=None:
            _late("apply_year")(c, prof, p, recs, fetched_on=fetched_on,
                                info=labels[int(p)]))
    elif ok:
        prof = dataclasses.replace(base, value_cols=tuple(
            c for c in DATA_COLUMNS if c != "null_reasons"))
        rc = pe.load_periods(cur, prof, [str(y) for y in ok],
                             lambda p: by_year[int(p)], date.today(), False,
                             against="live")
        print("  (editions tables not created yet: preview against live "
              "only)")
    if problems:
        print(f"REJECTED {len(problems)} year(s), nothing stored for them: "
              f"{', '.join(str(y) for y in sorted(problems))}; exit 1")
        rc = 1
    if args.commit:
        if rc == 0:
            kinds = stats["kinds"]
            notes = (f"release {slug} ({src['how']}): "
                     + "; ".join(f"{p} {k}" for p, k in kinds.items())
                     + "; files " + "; ".join(
                         f"{m['cohort']} {m['source']} sha256 {m['sha'][:16]}"
                         for m in metas)
                     + (f"; skipped {sorted(skipped)}" if skipped else "")
                     + "".join(f"; {older_note[y]}" for y in sorted(older_note))
                     + "".join(f"; ACKNOWLEDGED {y}: {acked[y]}"
                               for y in sorted(acked))
                     + f". Rows stored: {stats['stored_rows']} edition rows, "
                     f"{stats['live_rows']} live rows.")
            log_run(cur, stats["stored_rows"] + stats["live_rows"],  # not a source value
                    notes, started)
            conn.commit()
            print("pipeline_run_log row written")
        else:
            print("pipeline_run_log: no row written (a year failed or was "
                  "rejected; years committed before are in the editions "
                  "table)")
    return rc


# ---------------------------------------------------------------------------
# One-off: migrate the held table
# ---------------------------------------------------------------------------

# the held table as surveyed on 2026-10-09 (rows; md5 of every row as jsonb
# without loaded_at and null_reasons, ordered by key)
LIVE_ROWS = 1481
LIVE_HASH = "96ad4b486af5b6c9" "c12d3152066b406f"
# the three files the old builds read (sha256, split so the credential scan
# does not flag them), their dataset ids and releases
LEGACY_FILES = {
    "f17_2023": {"cohort": "17-21", "release": 2023,
                 "published": "2023-11-16",
                 "dataset": "888d6288-a4c8-4d00-86a9-ddb01c46b34d",
                 "sha256": "6df781a4709d63d8e9c77491667a876e"
                           "892de0eb5f6f80fbef510adbf91c951f",
                 "years": (2019, 2020)},
    "f17_2025": {"cohort": "17-21", "release": 2025,
                 "published": "2025-11-26",
                 "dataset": "a504e4b8-79ef-4196-8e2a-977fdc039dc3",
                 "sha256": "32d762908f8b859899d84e21d891ba01"
                           "14d5f8cc01c543198be4547609f40c71",
                 "years": (2021, 2022, 2023, 2024, 2025)},
    "f22_2025": {"cohort": "22-25", "release": 2025,
                 "published": "2025-11-26",
                 "dataset": "bd5240e0-76f9-4aa2-a307-7f3129a947a4",
                 "sha256": "0c52e756c6c8e9967cf884d6742cd7e0"
                           "29a867d18899abbf4f9ad6052e86ec41",
                 "years": (2023, 2024, 2025)},
}
# held 17-21 columns the proof compares cell for cell
PROOF_COLS_1721 = BUILT["17-21"]
# held 22-25 column -> the file's age-25 category (Node 4 kept the last age)
AGE25 = (("total_care_leavers", "total"), ("suitable_count", "suitable"),
         ("unsuitable", "unsuitable"), ("not_known", "no_information"))
# held rows the correction may drop without being all-z in the file
STALE_KEYS = {("E06000063", 2020, "17-21"): "stale n8n row (2026-03-31), "
              "absent from the 2023 release file",
              ("E06000064", 2020, "17-21"): "stale n8n row (2026-03-31), "
              "absent from the 2023 release file",
              ("E06000058", 2019, "17-21"): "BCP 2019, all z in the file "
              "(Bournemouth and Poole are added on their own codes)"}
# keys the correction may add
ADDED_17_21 = {("E06000028", 2019), ("E06000029", 2019)}


def live_state(cur, spec=SPEC) -> tuple:
    """(rows, md5) of the live table: every row as jsonb without loaded_at
    and null_reasons, ordered by key."""
    cur.execute(f"""SELECT COUNT(*), md5(string_agg((to_jsonb(t)
                    - ARRAY['loaded_at', 'null_reasons']::text[])::text,
                    E'\\n' ORDER BY lad24cd, reporting_year, age_group))
                    FROM public.{spec.live_table} t""")
    n, h = cur.fetchone()
    return n, h


def _held(cur, spec) -> dict:
    """{(lad24cd, year, age_group): record} of the live table, plus the
    'source' and load date of each row."""
    sel = ", ".join(("lad24cd", "age_group", "reporting_year")
                    + _live_cols(cur, spec)
                    + ("source", "(loaded_at AT TIME ZONE 'UTC')::date"))
    cur.execute(f"SELECT {sel} FROM public.{spec.live_table}")
    out = {}
    for row in cur.fetchall():
        lad, age, y = row[0], row[1], int(row[2])
        rec = dict(zip(DATA_COLUMNS, row[3:3 + len(DATA_COLUMNS)]))
        rec.update(lad24cd=lad, age_group=age, reporting_year=str(y),
                   _source=row[-2], _loaded=row[-1])
        out[(lad, y, age)] = rec
    return out


def _clean(r) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def migration_plan(cur, f17_2023, f17_2025, f22_2025, *, spec=SPEC,
                   live_rows=None, live_hash=None, files=None) -> dict:
    """Read-only: the preconditions, both editions per year and the proof.
    Halts on any failure (nothing is written here). live_rows, live_hash and
    files default to LIVE_ROWS, LIVE_HASH and LEGACY_FILES."""
    live_rows = LIVE_ROWS if live_rows is None else live_rows
    live_hash = LIVE_HASH if live_hash is None else live_hash
    files = LEGACY_FILES if files is None else files
    n, h = live_state(cur, spec)
    if (n, h) != (live_rows, live_hash):
        halt(f"{spec.live_table} is not as surveyed: {n} rows, hash {h}; "
             f"expected {live_rows} rows, hash {live_hash}; nothing stored")
    given = {"f17_2023": f17_2023, "f17_2025": f17_2025,
             "f22_2025": f22_2025}
    metas = {}
    for k, p in given.items():
        want = files[k]
        p = Path(p)
        sha = content_sha256(p)
        if sha != want["sha256"]:
            halt(f"{p.name}: sha256 {sha[:16]}, expected "
                 f"{want['sha256'][:16]} ({k}); nothing stored")
        try:
            meta = read_file(p)
        except ValueError as e:
            halt(f"{e}; nothing stored")
        bad = check_identity(meta, None, str(want["release"]))
        if meta["cohort"] != want["cohort"]:
            bad.append(f"the file is {meta['cohort']}, expected "
                       f"{want['cohort']}")
        if bad:
            halt(f"file identity check failed for {p.name}: "
                 + "; ".join(bad))
        metas[k] = {"meta": meta, "sha": sha, "path": p, "cohort":
                    meta["cohort"],
                    "url": ledger_source(csv_url(want["dataset"]),
                                         want["release"])}
    codes, by_year = set(), {}
    for k, m in metas.items():
        codes |= {c for c, _ in m["meta"]["rows"]}
        for y, cs in codes_with_data(m["meta"]).items():
            if y in files[k]["years"]:
                by_year.setdefault((m["cohort"], y), set()).update(cs)
    try:
        rmap, problems = resolve_codes(cur, codes, by_year)
    except ValueError as e:
        halt(f"geography: {e}; nothing stored")
    if problems:
        halt("geography check failed, nothing stored: " + "; ".join(problems))
    corr, dropped_file = {}, {}
    used = {}
    for k, m in metas.items():
        built = build_rows(m["meta"], rmap)
        for code, y in built.dropped:
            if y in files[k]["years"]:
                dropped_file[(rmap[code], y, m["cohort"])] = code
        for y in files[k]["years"]:
            used.setdefault(y, []).append(k)
            for r in built.get(y, []):
                corr[(r["lad24cd"], y, r["age_group"])] = {
                    **r, "source": guidance_url(files[k]["release"])}
    held = _held(cur, spec)
    # proof (a): every non-NULL corrected 17-21 cell equals the held cell
    cells, diffs, to_null = 0, [], Counter()
    for key in sorted(set(held) & set(corr)):
        hr, cr = held[key], corr[key]
        cols = PROOF_COLS_1721 if key[2] == "17-21" else BUILT["22-25"]
        for c in cols:
            if hr[c] is not None and cr[c] is None:
                to_null[c] += 1
        if key[2] != "17-21":
            continue
        for c in PROOF_COLS_1721:
            if cr[c] is None:
                continue
            cells += 1  # not a source value
            if cr[c] != hr[c]:
                diffs.append((key, c, hr[c], cr[c]))
    if diffs:
        halt(f"proof failed: {len(diffs)} corrected 17-21 cells differ from "
             f"the held cells (held -> corrected), e.g. {diffs[:5]}; nothing "
             "stored")
    # proof (b): every held 22-25 row equals the file's age-25 row
    f22 = metas["f22_2025"]["meta"]["rows"]
    back = {}
    for code in {c for c, _ in f22}:
        back[rmap[code]] = code
    rows25, bad25 = 0, []
    for key in sorted(k for k in held if k[2] == "22-25"):
        lad, y, _ = key
        cells25 = f22.get((back.get(lad), y))
        if cells25 is None:
            bad25.append((key, "not in the file"))
            continue
        for c, cat in AGE25:
            if held[key][c] != cells25[("25", cat)][0]:
                bad25.append((key, c, held[key][c], cells25[("25", cat)][0]))
                break
        else:
            rows25 += 1  # not a source value
    if bad25:
        halt(f"proof failed: {len(bad25)} held 22-25 rows differ from the "
             f"file's age-25 row, e.g. {bad25[:5]}; nothing stored")
    # every dropped and added key accounted for
    dropped, added, unexplained = [], [], []
    for key in sorted(set(held) - set(corr)):
        hr = held[key]
        zero = all(hr[c] is None or hr[c] == 0
                   for c in BUILT["17-21"] + BUILT["22-25"])
        if key in STALE_KEYS:
            dropped.append((key, STALE_KEYS[key]))
        elif zero and key in dropped_file:
            dropped.append((key, "all z in the file (not applicable), held "
                                 "as zeros or NULL"))
        else:
            unexplained.append(("dropped", key))
    valid_utla = set()
    cur.execute("SELECT DISTINCT utla_code FROM public.utla_lad_mapping")
    valid_utla = {r[0] for r in cur.fetchall()}
    for key in sorted(set(corr) - set(held)):
        lad, y, age = key
        if age == "22-25" and lad.startswith("E10") and (
                lad in valid_utla or lad in PREDECESSORS):
            added.append((key, "22-25 county council (dropped by the n8n "
                               "inner join)"))
        elif age == "17-21" and (lad, y) in ADDED_17_21:
            added.append((key, "declared predecessor, its own 2019 figures "
                               "(lost to the BCP recode)"))
        else:
            unexplained.append(("added", key))
    if unexplained:
        halt(f"proof failed: {len(unexplained)} keys dropped or added that "
             f"the correction does not explain: {unexplained[:8]}; nothing "
             "stored")
    years = sorted({k[1] for k in held} | {k[1] for k in corr})
    per_year = {}
    for y in years:
        h1 = [_clean(held[k]) for k in sorted(held) if k[1] == y]
        c2 = [corr[k] for k in sorted(corr) if k[1] == y]
        srcs = sorted({held[k]["_source"] for k in held if k[1] == y})
        loads = sorted({held[k]["_loaded"] for k in held if k[1] == y})
        ks = [k for k in used.get(y, [])]
        per_year[y] = {
            "edition1": h1, "edition2": c2,
            "as_loaded_source": ("as loaded: " + " | ".join(srcs)
                                 + f" (loaded {', '.join(str(d) for d in loads)})"),
            "loaded": max(loads) if loads else None,
            "release": max(files[k]["release"] for k in ks),
            "published": max(files[k]["published"] for k in ks),
            "files": [(metas[k]["url"], metas[k]["sha"]) for k in ks],
            "dropped": [d for d in dropped if d[0][1] == y],
            "added": [a for a in added if a[0][1] == y],
        }
    return {"years": per_year, "cells": cells, "rows25": rows25,
            "to_null": to_null, "dropped": dropped, "added": added,
            "metas": metas}


def migration_label(y, plan_year) -> str:
    return (f"rule 1 correction, F4: reporting year {y} rebuilt from the "
            f"files the old builds read (release {plan_year['release']}"
            f"{', published ' + plan_year['published']}): c/z/x NULL, never "
            "0; built columns NULL unless every part is published; 22-25 "
            "summed over the four ages, with county councils; all-z rows not "
            "stored; Bournemouth/Poole 2019 on their own codes; files "
            + "; ".join(f"{u} sha256 {s[:16]}" for u, s in plan_year["files"])
            + ". Proof: every non-NULL 17-21 cell equals the held cell; every "
            "held 22-25 row equals the file's age-25 row; dropped and added "
            "keys listed. Rule 1.10: the 0 to NULL changes against edition 1 "
            "are this correction (acknowledged)")


def migrate_legacy(cur, f17_2023, f17_2025, f22_2025, *, write, spec=SPEC,
                   live_rows=None, live_hash=None, files=None) -> dict:
    """One-off. Preconditions: on write the editions table and ledger exist
    and are empty (in a preview they may be absent); live as surveyed
    (live_rows rows, live_hash); the three files' sha256 as LEGACY_FILES.
    Per year: edition 1 'as loaded' from live (both cohorts), then edition
    2, the rule-1 correction from the same files (2019-2020 from the 2023
    release, 2021-2025 and 22-25 from the 2025 release) through the same
    parser and resolution as load, with the proof (migration_plan). On
    write each year goes in its own savepoint with its ledger rows; live is
    untouched. Raises (halt) on any failure; the caller rolls everything
    back."""
    exists = table_exists(cur, spec.editions_table)
    if write and not all(_ready(cur, spec).values()):
        halt("missing " + ", ".join(t for t, ok in _ready(cur, spec).items()
                                   if not ok) + "; run `ddl --commit` first")
    if exists:
        cur.execute(f"SELECT COUNT(*) FROM public.{spec.editions_table}")
        n = cur.fetchone()[0]
        if n:
            halt(f"{spec.editions_table} already holds {n} rows; "
                 "migrate-legacy runs once, on an empty table")
        prof = _profile(spec)
        if table_exists(cur, pe.file_checks_table(prof)):
            cur.execute(f"SELECT COUNT(*) FROM "
                        f"public.{pe.file_checks_table(prof)}")
            if cur.fetchone()[0]:
                halt(f"{pe.file_checks_table(prof)} is not empty")
    plan = migration_plan(cur, f17_2023, f17_2025, f22_2025, spec=spec,
                          live_rows=live_rows, live_hash=live_hash,
                          files=files)
    print(f"proof: {plan['cells']:,} non-NULL corrected 17-21 cells equal the "
          f"held cells (0 differences); {plan['rows25']} held 22-25 rows "
          "equal the file's age-25 row")
    print("cells held as a number that the correction makes NULL: "
          + (", ".join(f"{c} {n}" for c, n in sorted(plan["to_null"].items()))
             or "none"))
    for y, py in sorted(plan["years"].items()):
        e1, e2 = py["edition1"], py["edition2"]
        cnt = Counter(r["age_group"] for r in e2)
        print(f"  {y}: edition 1 as loaded {len(e1)} rows; edition 2 "
              f"(release {py['release']}) {len(e2)} rows "
              f"({', '.join(f'{a} {n}' for a, n in sorted(cnt.items()))}); "
              f"keys dropped {len(py['dropped'])}, added {len(py['added'])}")
        for k, why in py["dropped"]:
            print(f"      dropped {k[0]} {k[2]}: {why}")
        for k, why in py["added"]:
            print(f"      added {k[0]} {k[2]}: {why}")
    if not write:
        return plan
    prof = _profile(spec)
    for y, py in sorted(plan["years"].items()):
        p = str(y)
        cur.execute(f"SAVEPOINT {prof.savepoint}")
        e1 = py["edition1"]
        ed1 = None
        if e1:
            ed1 = core.insert_edition(
                cur, spec, e1, p, release_label=core.AS_LOADED_LATEST_LABEL,
                published_date=py["loaded"],
                source_file=py["as_loaded_source"],
                source_sha256=core.rows_sha256(spec, e1), supersedes=None,
                strict=True)
            if ed1 != 1 or core.rows_differing(cur, spec, p, ed1):
                halt(f"{y}: edition 1 does not equal live; rolled back")
        if not py["edition2"]:
            halt(f"{y}: the correction holds no rows")
        ed2 = core.insert_edition(
            cur, spec, py["edition2"], p,
            release_label=migration_label(y, py),
            published_date=date.fromisoformat(py["published"]),
            source_file=guidance_url(py["release"]),
            source_sha256=core.rows_sha256(spec, py["edition2"]),
            supersedes=ed1, strict=True, allow_revert=True)
        if ed2 != (2 if ed1 else 1) or core.chain_tip(cur, spec, p) != ed2:
            halt(f"{y}: the correction was not stored as the next edition")
        for url, sha in py["files"]:
            pe.record_file_check(cur, prof, p, url, sha, "revised", ed2)
        cur.execute(f"RELEASE SAVEPOINT {prof.savepoint}")
    return plan


def cmd_migrate_legacy(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            plan = migrate_legacy(cur, args.f17_2023, args.f17_2025,
                                  args.f22_2025, write=writing, spec=SPEC)
            if args.commit:
                ys = plan["years"]
                n = sum(len(v["edition1"]) + len(v["edition2"])
                        for v in ys.values())
                log_run(cur, n, "migrate-legacy: years "
                        + ", ".join(str(y) for y in sorted(ys))
                        + " edition 1 as loaded and edition 2 the rule 1 "
                        f"correction (F4); proof {plan['cells']} 17-21 cells "
                        f"equal held, {plan['rows25']} 22-25 rows equal the "
                        "age-25 rows; cells to NULL "
                        + ", ".join(f"{c} {k}" for c, k in
                                    sorted(plan["to_null"].items()))
                        + f"; keys dropped {len(plan['dropped'])}, added "
                        f"{len(plan['added'])}. Live untouched.", started)
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

def restore_edition(cur, year, edition, *, spec=SPEC) -> int:
    """Store edition `edition`'s rows of `year` as the next edition
    ('restored from edition N'), for refresh-latest to apply; return its
    number. Halts if the edition does not exist or already equals the
    tip."""
    p = str(year)
    tip = core.latest_edition(cur, spec, p)
    recs = edition_records(cur, spec, year, edition)
    if not recs:
        halt(f"{year}: no edition {edition}")
    if edition == tip:
        halt(f"{year}: edition {edition} is the tip; nothing to restore")
    cur.execute(f"SELECT DISTINCT source_file FROM public.{spec.editions_table}"
                " WHERE reporting_year = %s AND edition = %s", (p, edition))
    src = cur.fetchone()[0]
    new = core.insert_edition(
        cur, spec, recs, p, release_label=f"restored from edition {edition}",
        published_date=date.today(), source_file=src,
        source_sha256=core.rows_sha256(spec, recs), supersedes=tip,
        strict=True, allow_revert=True)
    if new == tip:
        halt(f"{year}: edition {edition} has the tip's content; nothing to "
             "restore")
    return new


def cmd_restore_edition(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, SPEC.editions_table):
                halt(f"{SPEC.editions_table} does not exist")
            cur.execute("SAVEPOINT s4_restore")
            new = restore_edition(cur, args.year, args.edition, spec=SPEC)
            print(f"{args.year}: edition {args.edition} "
                  + ("stored" if writing else "would be stored")
                  + f" as edition {new}; run refresh-latest to apply it")
            if args.commit:
                log_run(cur, len(edition_records(cur, SPEC, args.year, new)),
                        f"restore-edition: {args.year} edition "
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
        description="S4 DfE care leaver accommodation: editions loader on the "
        "period-editions engine. There is no sync-new; migrate-legacy "
        "records the held years once.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create the editions table, its triggers "
                       "and the file-check ledger, and add null_reasons to "
                       "the live table (preview by default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("load", help="read the release's two files, compare "
                       "and store each reporting year (preview by default)")
    p.add_argument("--release", metavar="SLUG",
                   help="the release (YYYY); default the latest release")
    p.add_argument("--dataset-17-21", dest="dataset_17_21", metavar="ID",
                   help="manual fallback: the 17-21 dataset id (with "
                   "--release)")
    p.add_argument("--dataset-22-25", dest="dataset_22_25", metavar="ID",
                   help="manual fallback: the 22-25 dataset id (with "
                   "--release)")
    p.add_argument("--file-17-21", dest="file_17_21", metavar="PATH",
                   help="a local 17-21 file (with --release; nothing is "
                   "downloaded; identity is read from the file)")
    p.add_argument("--file-22-25", dest="file_22_25", metavar="PATH",
                   help="a local 22-25 file (with --release)")
    p.add_argument("--recheck", type=_year_arg, metavar="YEAR",
                   help="read this held year again although its files are "
                   "in the ledger")
    p.add_argument("--allow-older-file", action="store_true",
                   help="compare and store years whose tip came from a newer "
                   "release (logged)")
    p.add_argument("--acknowledge", action="append", type=_year_arg,
                   metavar="YEAR",
                   help="release a year's 0/NULL changes after reading the "
                   "preview (repeatable)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("refresh-latest", help="copy each year's latest "
                       "edition into the live table, source and loaded_at "
                       "included (preview by default)")
    pe.mode_parser(p)
    p.add_argument("--accept-drift", action="append", metavar="YEAR",
                   help="overwrite this year although its live rows equal no "
                   "stored edition (repeatable)")
    p.add_argument("--accept-key-changes", action="append", metavar="YEAR",
                   help="apply this year although authorities are added or "
                   "removed (repeatable; each year must be named)")
    p.set_defaults(func=cmd_refresh_latest)
    p = sub.add_parser("migrate-legacy", help="one-off: edition 1 as loaded "
                       "and edition 2 the rule 1 correction for every held "
                       "year (preview by default)")
    p.add_argument("f17_2023", metavar="FILE17_2023")
    p.add_argument("f17_2025", metavar="FILE17_2025")
    p.add_argument("f22_2025", metavar="FILE22_2025")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_migrate_legacy)
    p = sub.add_parser("restore-edition", help="store an earlier edition's "
                       "rows as the next edition (preview by default)")
    p.add_argument("year", type=_year_arg)
    p.add_argument("edition", type=int)
    pe.mode_parser(p)
    p.set_defaults(func=cmd_restore_edition)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
