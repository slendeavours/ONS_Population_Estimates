"""S6 - Home Office asylum support by local authority ETL.

Loads two Home Office datasets into exempt_pipeline:

  S6a  Asy_D11  Asylum seekers in receipt of Home Office support by support
                type, accommodation type and local authority. Quarterly time
                series, loaded from 2018 Q1 forward.
  S6b  Reg_02   Immigration groups by local authority. Single snapshot.

Structure: discover, download, parse, resolve geography, validate, upsert, log.
Every download URL is discovered from its GOV.UK landing page at run time.
"""

import datetime
import os
import re
import sys
import uuid
from collections import defaultdict
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

# Resolved relative to this file so the script is portable and no local path
# is baked into a public repository. Repository root first, then its parent,
# which is where the shared .env lives.
_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")
load_dotenv(_HERE.parent / ".env")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _require_env(name):
    """Credentials must come from the environment. Never fall back to a
    literal: a default in source is a published credential."""
    value = os.environ.get(name)
    if not value:
        sys.exit(f"HARD STOP: {name} is not set. Set it in the environment "
                 f"or .env. This script will not guess a credential.")
    return value


DB_CFG = dict(
    host=(os.getenv("PG_HOST") or "localhost").replace("postgres", "localhost"),
    port=int(os.getenv("PG_PORT", "5432")),
    dbname=os.getenv("PG_DATABASE", "exempt_pipeline"),
    user=_require_env("PG_USER"),
    password=_require_env("PG_PASSWORD"),
)

SOURCE_NUMBER = "6"
AGENT_NAME = "s6_asylum_build"

LANDING_TABLES = ("https://www.gov.uk/government/statistical-data-sets/"
                  "immigration-system-statistics-data-tables")
LANDING_REGIONAL = ("https://www.gov.uk/government/statistical-data-sets/"
                    "immigration-system-statistics-regional-and-local-"
                    "authority-data")

# Section 4 carries no LA geography before 2018, so earlier quarters cannot be
# aggregated consistently across support types.
FLOOR = datetime.date(2018, 1, 1)

ENGLISH_PREFIXES = ("E06", "E07", "E08", "E09")
COUNTRY_BY_PREFIX = {"S12": "Scotland", "W06": "Wales", "N09": "Northern Ireland"}

NOT_STATED = "not_stated"
COLLISION_HALT_THRESHOLD = 5


_TMP = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "s6_asylum")

REG02_PATHWAY_MAP = [
    (3,  "homes_for_ukraine",    "total"),
    (4,  "afghan_resettlement",  "total"),
    (5,  "afghan_resettlement",  "transitional"),
    (6,  "afghan_resettlement",  "settled_la_housing"),
    (7,  "afghan_resettlement",  "settled_prs_housing"),
    (8,  "supported_asylum",     "total"),
    (9,  "supported_asylum",     "initial_accommodation"),
    (10, "supported_asylum",     "dispersal"),
    (11, "supported_asylum",     "contingency"),
    (12, "supported_asylum",     "other"),
    (13, "supported_asylum",     "subsistence_only"),
    (14, "all_pathways",         "total"),
]
REG02_POPULATION_COL = 15
REG02_PCT_COL = 16

# Reg_02 totals for these two exclude Homes for Ukraine because it is
# suppressed, so the pathway columns do not sum to the published total.
REG02_TOTAL_EXEMPT = {"E09000001", "E06000053"}

ANCHORS_ENGLAND = {"Birmingham": 2142, "Liverpool": 2053, "Coventry": 1712}
ANCHORS_NON_ENGLAND = {"Glasgow City": 3870, "Belfast": 1607}
ANCHOR_PERIOD = datetime.date(2026, 3, 31)
ANCHOR_UK_TOTAL = 97519

DDL = """
CREATE TABLE IF NOT EXISTS la_asylum_support (
    period_ending       DATE        NOT NULL,
    lad24cd             TEXT        NOT NULL REFERENCES la_boundaries(lad24cd),
    published_la_name   TEXT        NOT NULL,
    support_type        TEXT        NOT NULL,
    accommodation_type  TEXT        NOT NULL,
    people              INTEGER     NOT NULL,
    source_marker       TEXT        NULL,
    source_edition      TEXT        NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (period_ending, lad24cd, support_type, accommodation_type)
);
CREATE INDEX IF NOT EXISTS idx_las_lad24cd    ON la_asylum_support (lad24cd);
CREATE INDEX IF NOT EXISTS idx_las_period     ON la_asylum_support (period_ending);
CREATE INDEX IF NOT EXISTS idx_las_period_lad ON la_asylum_support (period_ending, lad24cd);

CREATE TABLE IF NOT EXISTS la_asylum_support_unallocated (
    period_ending       DATE        NOT NULL,
    support_type        TEXT        NOT NULL,
    accommodation_type  TEXT        NOT NULL,
    people              INTEGER     NOT NULL,
    na_reason           TEXT        NOT NULL,
    source_edition      TEXT        NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (period_ending, support_type, accommodation_type, na_reason)
);
CREATE INDEX IF NOT EXISTS idx_lasu_period ON la_asylum_support_unallocated (period_ending);

CREATE TABLE IF NOT EXISTS asylum_support_non_england (
    period_ending       DATE        NOT NULL,
    lad_code            TEXT        NOT NULL,
    country             TEXT        NOT NULL,
    published_la_name   TEXT        NOT NULL,
    support_type        TEXT        NOT NULL,
    accommodation_type  TEXT        NOT NULL,
    people              INTEGER     NOT NULL,
    source_edition      TEXT        NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (period_ending, lad_code, support_type, accommodation_type)
);
CREATE INDEX IF NOT EXISTS idx_asne_period ON asylum_support_non_england (period_ending);

CREATE TABLE IF NOT EXISTS la_immigration_groups (
    period_ending             DATE         NOT NULL,
    lad24cd                   TEXT         NOT NULL REFERENCES la_boundaries(lad24cd),
    published_la_name         TEXT         NOT NULL,
    pathway                   TEXT         NOT NULL,
    sub_pathway               TEXT         NOT NULL,
    people                    INTEGER      NULL,
    suppressed                BOOLEAN      NOT NULL DEFAULT FALSE,
    source_marker             TEXT         NULL,
    population                INTEGER      NULL,
    percentage_of_population  NUMERIC(8,4) NULL,
    source_edition            TEXT         NOT NULL,
    loaded_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (period_ending, lad24cd, pathway, sub_pathway)
);
CREATE INDEX IF NOT EXISTS idx_lig_lad24cd ON la_immigration_groups (lad24cd);
CREATE INDEX IF NOT EXISTS idx_lig_period  ON la_immigration_groups (period_ending);

CREATE TABLE IF NOT EXISTS asylum_series_breaks (
    break_id        SERIAL PRIMARY KEY,
    first_period    DATE NOT NULL,
    last_period     DATE NULL,
    support_type    TEXT NULL,
    dimension       TEXT NOT NULL,
    description     TEXT NOT NULL,
    comparability   TEXT NOT NULL
);
"""

VIEW_DDL = """
CREATE OR REPLACE VIEW vw_la_asylum_support_totals AS
SELECT
    s.period_ending,
    s.lad24cd,
    b.lad24nm AS la_name,
    SUM(s.people) AS total_supported,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Dispersal Accommodation')           AS dispersal,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Initial Accommodation')             AS initial_accommodation,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Contingency Accommodation - Hotel') AS contingency_hotel,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Contingency Accommodation - Other') AS contingency_other,
    SUM(s.people) FILTER (WHERE s.accommodation_type LIKE 'Contingency Accommodation%%')    AS contingency_all,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Other Accommodation')               AS other_accommodation,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'Subsistence Only')                  AS subsistence_only,
    SUM(s.people) FILTER (WHERE s.accommodation_type = 'not_stated')                        AS accommodation_not_stated,
    SUM(s.people) FILTER (WHERE s.support_type = 'Section 4')                               AS section_4,
    SUM(s.people) FILTER (WHERE s.support_type = 'Section 95')                              AS section_95,
    SUM(s.people) FILTER (WHERE s.support_type = 'Section 98')                              AS section_98,
    MAX(s.source_edition) AS source_edition
FROM la_asylum_support s
JOIN la_boundaries b ON b.lad24cd = s.lad24cd
GROUP BY s.period_ending, s.lad24cd, b.lad24nm;
"""

SERIES_BREAKS = [
    (datetime.date(2022, 12, 31), None, "Section 98", "geography",
     "Section 98 gained local authority geography at 2022-12-31. Before that "
     "date all Section 98 people were published as a single national row with "
     "no LA and no accommodation type, and are held in "
     "la_asylum_support_unallocated.",
     "England totals are not comparable across the 2022 Q3 / Q4 boundary. "
     "England rises from 53,749 to 98,375 between 2022-09-30 and 2022-12-31. "
     "That is a reporting change, not 44,000 arrivals."),
    (datetime.date(2023, 12, 31), datetime.date(2024, 12, 31), "Section 95",
     "geography",
     "Subsistence Only lost local authority geography for five consecutive "
     "quarters, 2023-12-31 to 2024-12-31 inclusive. All subsistence-only "
     "people in those periods are held in la_asylum_support_unallocated.",
     "Local authority counts and England totals are depressed across those "
     "five periods. 32 English LAs that appeared only via subsistence-only "
     "claimants at 2023-09-30 disappear entirely from 2023-12-31."),
]

FIRST_FULLY_COMPARABLE_PERIOD = datetime.date(2025, 3, 31)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover(landing_url, link_pattern, extension):
    """Return (url, edition_label) for the newest matching asset on a page."""
    resp = requests.get(landing_url, timeout=60)
    resp.raise_for_status()
    hits = re.findall(
        r'<a[^>]+href="([^"]+\.%s)"[^>]*>(.*?)</a>' % extension,
        resp.text, re.I | re.S)
    matches = []
    for href, text in hits:
        label = re.sub(r"<[^>]+>", "", text).strip()
        if re.search(link_pattern, label, re.I):
            edition = re.search(r"year ending\s+(\w+\s+\d{4})", label, re.I)
            matches.append((href, label, edition.group(1) if edition else None))
    if not matches:
        sys.exit(f"HARD STOP: no {extension} link matching {link_pattern!r} "
                 f"on {landing_url}")
    # The landing pages list newest first; prefer the newest parseable edition.
    def key(m):
        if not m[2]:
            return datetime.date.min
        try:
            return datetime.datetime.strptime(m[2], "%B %Y").date()
        except ValueError:
            return datetime.date.min
    matches.sort(key=key, reverse=True)
    href, label, edition = matches[0]
    return href, f"year ending {edition}" if edition else label


def discover():
    print("=" * 78)
    print("STEP 1 - DISCOVERY")
    print("=" * 78)
    d11_url, d11_ed = _discover(
        LANDING_TABLES,
        r"Asylum seekers in receipt of Home Office support by local authority",
        "xlsx")
    d09_url, d09_ed = _discover(
        LANDING_TABLES,
        r"Asylum seekers in receipt of Home Office support detailed",
        "xlsx")
    reg_url, reg_ed = _discover(
        LANDING_REGIONAL,
        r"Regional and local authority data on immigration groups",
        "ods")
    for name, url, ed in (("Asy_D11", d11_url, d11_ed),
                          ("Asy_D09", d09_url, d09_ed),
                          ("Reg_02", reg_url, reg_ed)):
        print(f"  {name:<8} {ed:<28} {url}")
    return {"d11": (d11_url, d11_ed), "d09": (d09_url, d09_ed),
            "reg": (reg_url, reg_ed)}


def download(found):
    print("\n" + "=" * 78)
    print("STEP 2 - DOWNLOAD")
    print("=" * 78)
    os.makedirs(_TMP, exist_ok=True)
    paths = {}
    for key, ext in (("d11", "xlsx"), ("d09", "xlsx"), ("reg", "ods")):
        url, _ = found[key]
        path = os.path.join(_TMP, f"s6_{key}.{ext}")
        resp = requests.get(url, timeout=300)
        resp.raise_for_status()
        with open(path, "wb") as fh:
            fh.write(resp.content)
        print(f"  {key:<5} {len(resp.content):>10,} bytes -> {path}")
        paths[key] = path
    return paths


# ---------------------------------------------------------------------------
# Geography resolution
# ---------------------------------------------------------------------------

class Geography:
    def __init__(self, cur):
        cur.execute("SELECT lad24cd, lad24nm FROM la_boundaries")
        self.boundaries = dict(cur.fetchall())
        cur.execute("SELECT old_code, new_code FROM la_code_lookup")
        self.lookup = dict(cur.fetchall())

    def resolve(self, code):
        """Code-first cascade. Returns (lad24cd or None, method)."""
        code = (code or "").strip()
        if code in self.boundaries:
            return code, "code_direct"
        target = self.lookup.get(code)
        if target and target in self.boundaries:
            return target, "code_historical_forward"
        return None, "unresolved"


# ---------------------------------------------------------------------------
# Parsing and normalisation
# ---------------------------------------------------------------------------

def normalise_accommodation(value):
    """Trim and title-case. Returns (normalised, verbatim_marker_or_None)."""
    raw = (value or "").strip()
    if raw.upper().startswith("N/A"):
        return NOT_STATED, raw
    return " ".join(word.capitalize() for word in raw.split()), None


def _is_na(value):
    return (value or "").strip().upper().startswith("N/A")


def parse_asy_d11(path, edition, geo):
    """Parse, filter, classify and aggregate Asy_D11.

    Returns (england, unallocated, non_england, collisions, stats).
    """
    print("\n" + "=" * 78)
    print("STEP 3 - PARSE Asy_D11")
    print("=" * 78)
    df = pd.read_excel(path, sheet_name="Data_Asy_D11", header=1,
                       engine="openpyxl")
    cols = list(df.columns)
    print(f"  raw rows: {len(df):,}   columns: {cols}")

    c_date, c_support, c_la, c_code, c_accom, c_people = (
        cols[0], cols[1], cols[3], cols[4], cols[5], cols[6])

    df[c_date] = pd.to_datetime(df[c_date], format="%d %b %Y").dt.date
    before = len(df)
    df = df[df[c_date] >= FLOOR]
    print(f"  filtered to >= {FLOOR}: {len(df):,} rows "
          f"({before - len(df):,} excluded)")

    england = defaultdict(int)
    england_meta = {}
    unalloc = defaultdict(int)
    non_eng = defaultdict(int)
    non_eng_meta = {}
    sources = defaultdict(list)
    unresolved = []

    for row in df.itertuples(index=False):
        period = getattr(row, "_0") if not hasattr(row, "_fields") else row[0]
        period = row[0]
        support = str(row[1]).strip()
        la_name = str(row[3]).strip()
        code = str(row[4]).strip()
        accom, marker = normalise_accommodation(str(row[5]))
        people = int(row[6])

        if _is_na(la_name) or _is_na(code) or la_name == "Unknown":
            reason = la_name if (_is_na(la_name) or la_name == "Unknown") else code
            key = (period, support, accom, reason)
            unalloc[key] += people
            sources[("U",) + key].append(tuple(row))
            continue

        prefix = code[:3]
        if prefix in COUNTRY_BY_PREFIX:
            key = (period, code, support, accom)
            non_eng[key] += people
            non_eng_meta[key] = (COUNTRY_BY_PREFIX[prefix], la_name)
            sources[("N",) + key].append(tuple(row))
            continue

        if prefix not in ENGLISH_PREFIXES:
            unresolved.append((code, la_name, "unexpected code prefix"))
            continue

        target, method = geo.resolve(code)
        if target is None:
            unresolved.append((code, la_name, "no resolution"))
            continue

        key = (period, target, support, accom)
        england[key] += people
        england_meta[key] = (la_name, marker)
        sources[("E",) + key].append(tuple(row))

    if unresolved:
        for code, name, why in unresolved:
            print(f"  UNRESOLVED {code} {name!r}: {why}")
        sys.exit(f"HARD STOP: {len(unresolved)} unresolved English geographies.")

    # Two very different things collapse on the natural key, and only one of
    # them is an anomaly:
    #   reorganisation_merge - source rows carry DIFFERENT LAD codes and land
    #       on one successor unitary. This is the resolution cascade working
    #       as designed (e.g. Mendip + Sedgemoor + South Somerset -> Somerset).
    #   duplicate_key - source rows carry the SAME LAD code. A genuine source
    #       defect. Only these count against the halt threshold.
    collisions = []
    for key, rows in sources.items():
        if len(rows) < 2:
            continue
        codes = {r[4] for r in rows}
        kind = "duplicate_key" if len(codes) == 1 else "reorganisation_merge"
        collisions.append((key, rows, kind))

    merges = [c for c in collisions if c[2] == "reorganisation_merge"]
    dupes = [c for c in collisions if c[2] == "duplicate_key"]
    print(f"  England keys      : {len(england):,}  people {sum(england.values()):,}")
    print(f"  Unallocated keys  : {len(unalloc):,}  people {sum(unalloc.values()):,}")
    print(f"  Non-England keys  : {len(non_eng):,}  people {sum(non_eng.values()):,}")
    print(f"  Collisions: {len(collisions)} "
          f"({len(merges)} reorganisation merges, {len(dupes)} duplicate keys)")

    stats = {"raw_rows": before, "filtered_rows": len(df)}
    return (england, england_meta, unalloc, non_eng, non_eng_meta,
            collisions, stats)


def parse_reg_02(path, edition, geo):
    print("\n" + "=" * 78)
    print("STEP 4 - PARSE Reg_02")
    print("=" * 78)
    df = pd.read_excel(path, sheet_name="Reg_02", header=1, engine="odf")
    cols = list(df.columns)
    print(f"  raw rows: {len(df):,}   columns: {len(cols)}")

    period = ANCHOR_PERIOD
    rows = []
    skipped = []
    for r in df.itertuples(index=False):
        la_name = str(r[0]).strip()
        code = str(r[2]).strip()
        if not code or code.lower() in ("nan", "unknown"):
            skipped.append((la_name, code))
            continue
        if code[:3] not in ENGLISH_PREFIXES:
            continue
        target, method = geo.resolve(code)
        if target is None:
            sys.exit(f"HARD STOP: Reg_02 code {code} ({la_name}) unresolved.")

        population = _reg_int(r[REG02_POPULATION_COL])[0]
        pct = _reg_pct(r[REG02_PCT_COL])

        # Where a whole pathway is suppressed, the published all-pathways
        # total excludes it rather than hiding it inside, so that total is a
        # lower bound. Flag it on the row itself so it is visible to anyone
        # reading the table without the documentation.
        suppressed_pathways = [
            name for idx, name, sub in REG02_PATHWAY_MAP
            if sub == "total" and name != "all_pathways"
            and _reg_int(r[idx])[0] is None
        ]

        for idx, pathway, sub in REG02_PATHWAY_MAP:
            people, marker = _reg_int(r[idx])
            if (pathway == "all_pathways" and sub == "total"
                    and suppressed_pathways):
                marker = (
                    "LOWER BOUND: published total excludes the suppressed "
                    + ", ".join(suppressed_pathways)
                    + " pathway (fewer than 5 people, disclosure control). "
                      "True total is higher by between 1 and 4 per "
                      "suppressed pathway.")
            rows.append({
                "period_ending": period,
                "lad24cd": target,
                "published_la_name": la_name,
                "pathway": pathway,
                "sub_pathway": sub,
                "people": people,
                "suppressed": people is None,
                "source_marker": marker,
                "population": population,
                # Published once per LA, computed on the all-pathways total.
                # Populated only there so it cannot be read as a pathway share.
                "percentage_of_population":
                    pct if (pathway == "all_pathways" and sub == "total") else None,
                "source_edition": edition,
            })

    print(f"  English LAs      : {len(rows) // len(REG02_PATHWAY_MAP)}")
    print(f"  pathway rows     : {len(rows):,}")
    print(f"  suppressed cells : {sum(1 for x in rows if x['suppressed'])}")
    print(f"  skipped rows     : {skipped}")
    return rows


def _reg_int(value):
    """Reg_02 numeric cell -> (int or None, verbatim marker or None)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None, None
    text = str(value).strip()
    if text in ("", "-", "*", ":", "nan"):
        return None, text or None
    return int(float(text.replace(",", ""))), None


def _reg_pct(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().replace("%", "")
    if text in ("", "-", "*", ":", "nan"):
        return None
    return round(float(text), 4)


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def upsert(cur, england, england_meta, unalloc, non_eng, non_eng_meta,
           reg_rows, edition):
    print("\n" + "=" * 78)
    print("STEP 5 - UPSERT")
    print("=" * 78)

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO la_asylum_support
            (period_ending, lad24cd, published_la_name, support_type,
             accommodation_type, people, source_marker, source_edition, loaded_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (period_ending, lad24cd, support_type, accommodation_type)
        DO UPDATE SET published_la_name = EXCLUDED.published_la_name,
                      people            = EXCLUDED.people,
                      source_marker     = EXCLUDED.source_marker,
                      source_edition    = EXCLUDED.source_edition,
                      loaded_at         = now()
    """, [(p, c, england_meta[(p, c, s, a)][0], s, a, v,
           england_meta[(p, c, s, a)][1], edition)
          for (p, c, s, a), v in england.items()], page_size=1000)
    print(f"  la_asylum_support            : {len(england):,} rows")

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO la_asylum_support_unallocated
            (period_ending, support_type, accommodation_type, people,
             na_reason, source_edition, loaded_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (period_ending, support_type, accommodation_type, na_reason)
        DO UPDATE SET people         = EXCLUDED.people,
                      source_edition = EXCLUDED.source_edition,
                      loaded_at      = now()
    """, [(p, s, a, v, r) + (edition,)
          for (p, s, a, r), v in unalloc.items()], page_size=1000)
    print(f"  la_asylum_support_unallocated: {len(unalloc):,} rows")

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO asylum_support_non_england
            (period_ending, lad_code, country, published_la_name, support_type,
             accommodation_type, people, source_edition, loaded_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (period_ending, lad_code, support_type, accommodation_type)
        DO UPDATE SET country           = EXCLUDED.country,
                      published_la_name = EXCLUDED.published_la_name,
                      people            = EXCLUDED.people,
                      source_edition    = EXCLUDED.source_edition,
                      loaded_at         = now()
    """, [(p, c, non_eng_meta[(p, c, s, a)][0], non_eng_meta[(p, c, s, a)][1],
           s, a, v, edition)
          for (p, c, s, a), v in non_eng.items()], page_size=1000)
    print(f"  asylum_support_non_england   : {len(non_eng):,} rows")

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO la_immigration_groups
            (period_ending, lad24cd, published_la_name, pathway, sub_pathway,
             people, suppressed, source_marker, population,
             percentage_of_population, source_edition, loaded_at)
        VALUES (%(period_ending)s, %(lad24cd)s, %(published_la_name)s,
                %(pathway)s, %(sub_pathway)s, %(people)s, %(suppressed)s,
                %(source_marker)s, %(population)s,
                %(percentage_of_population)s, %(source_edition)s, now())
        ON CONFLICT (period_ending, lad24cd, pathway, sub_pathway)
        DO UPDATE SET published_la_name        = EXCLUDED.published_la_name,
                      people                   = EXCLUDED.people,
                      suppressed               = EXCLUDED.suppressed,
                      source_marker            = EXCLUDED.source_marker,
                      population               = EXCLUDED.population,
                      percentage_of_population = EXCLUDED.percentage_of_population,
                      source_edition           = EXCLUDED.source_edition,
                      loaded_at                = now()
    """, reg_rows, page_size=1000)
    print(f"  la_immigration_groups        : {len(reg_rows):,} rows")

    cur.execute("DELETE FROM asylum_series_breaks")
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO asylum_series_breaks
            (first_period, last_period, support_type, dimension,
             description, comparability)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, SERIES_BREAKS)
    print(f"  asylum_series_breaks         : {len(SERIES_BREAKS)} rows")


def checksum(cur):
    cur.execute("""
        SELECT md5(string_agg(t, '|' ORDER BY t))
          FROM (SELECT period_ending || ':' || lad24cd || ':' || support_type
                       || ':' || accommodation_type || ':' || people AS t
                  FROM la_asylum_support) s
    """)
    return cur.fetchone()[0]


def main():
    started_at = datetime.datetime.now(datetime.timezone.utc)
    run_id = uuid.uuid4()

    found = discover()
    paths = download(found)
    d11_edition = found["d11"][1]
    reg_edition = found["reg"][1]

    conn = psycopg2.connect(**DB_CFG)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute(DDL)
        cur.execute(VIEW_DDL)
        geo = Geography(cur)

        (england, england_meta, unalloc, non_eng, non_eng_meta,
         collisions, stats) = parse_asy_d11(paths["d11"], d11_edition, geo)
        reg_rows = parse_reg_02(paths["reg"], reg_edition, geo)

        upsert(cur, england, england_meta, unalloc, non_eng, non_eng_meta,
               reg_rows, d11_edition)

        def reload_fn(c):
            """Second identical load, for the idempotency check."""
            upsert(c, england, england_meta, unalloc, non_eng, non_eng_meta,
                   reg_rows, d11_edition)

        import s6_asylum_verify as verify
        results = verify.run_all(
            cur, paths, d11_edition, reg_edition, geo,
            england, unalloc, non_eng, reg_rows, collisions, checksum,
            reload_fn=reload_fn, stats=stats)

        failed = [r for r in results if not r["passed"]]
        if failed:
            conn.rollback()
            print("\nROLLED BACK. Failed checks: "
                  + ", ".join(r["name"] for r in failed))
            sys.exit(1)

        cur.execute("""
            INSERT INTO pipeline_run_log
                (run_id, agent_name, source_number, rows_written, started_at,
                 completed_at, status, notes)
            VALUES (%s, %s, %s, %s, %s, now(), %s, %s)
        """, (str(run_id), AGENT_NAME, SOURCE_NUMBER,
              len(england) + len(unalloc) + len(non_eng) + len(reg_rows),
              started_at, "success",
              f"S6 Home Office asylum support. Asy_D11 {d11_edition}, "
              f"Reg_02 {reg_edition}. Floor date applied: {FLOOR} "
              f"(Section 4 has no LA geography before 2018). "
              f"la_asylum_support {len(england)}, unallocated {len(unalloc)}, "
              f"non_england {len(non_eng)}, immigration_groups {len(reg_rows)}. "
              f"Verification: {len(results)} checks, all passed."))
        conn.commit()
        print("\nCommitted. pipeline_run_log written.")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
